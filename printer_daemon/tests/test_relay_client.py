"""The daemon's side of the protocol, with `requests` replaced by a fake session.

No network is touched. The wire shapes here must match tests/test_printer_routes.py."""
import json
import os
import tempfile
import unittest

import requests

from printer_daemon import USER_AGENT
from printer_daemon.relay_client import (
    ChallengeDetected, RateLimited, RelayClient, RelayError, TokenRejected,
)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=None, headers=None,
                 chunks=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text is not None else json.dumps(payload or {})
        self.headers = headers or {'Content-Type': 'application/json'}
        self._chunks = chunks or [b'']
        self.closed = False

    def json(self):
        if self._payload is None:
            raise ValueError('not json')
        return self._payload

    def iter_content(self, chunk_size=8192):
        return iter(self._chunks)

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class BrokenStream(FakeResponse):
    """A 200 whose body fails partway through, the way a dropped connection does."""

    def iter_content(self, chunk_size=8192):
        yield b'3mf-'
        raise requests.ConnectionError('connection reset by peer')


class FakeSession:
    """Records calls and replays queued responses."""

    def __init__(self, *responses):
        self.queue = list(responses)
        self.calls = []
        self.headers = {}

    def _next(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if not self.queue:
            raise AssertionError(f'unexpected {method} {url}')
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def post(self, url, **kwargs):
        return self._next('POST', url, **kwargs)

    def get(self, url, **kwargs):
        return self._next('GET', url, **kwargs)


class TestExchange(unittest.TestCase):
    def test_a_token_is_returned_and_stored(self):
        session = FakeSession(FakeResponse(payload={'token': 'signed'}))
        client = RelayClient('https://example.test', session=session)
        self.assertEqual(client.exchange('7K3MQ9XW'), 'signed')
        self.assertEqual(client.token, 'signed')
        method, url, kwargs = session.calls[0]
        self.assertEqual((method, url), ('POST', 'https://example.test/printer/daemon/pair'))
        self.assertEqual(kwargs['json'], {'code': '7K3MQ9XW'})

    def test_a_404_means_wait_for_the_code_to_appear(self):
        session = FakeSession(FakeResponse(404, payload={'error': 'not paired'}))
        client = RelayClient('https://example.test', session=session)
        self.assertIsNone(client.exchange('7K3MQ9XW'))
        self.assertIsNone(client.token)

    def test_a_429_raises_with_the_retry_after(self):
        session = FakeSession(FakeResponse(429, payload={}, headers={
            'Content-Type': 'application/json', 'Retry-After': '17'}))
        client = RelayClient('https://example.test', session=session)
        with self.assertRaises(RateLimited) as caught:
            client.exchange('7K3MQ9XW')
        self.assertEqual(caught.exception.retry_after, 17.0)

    def test_an_html_body_on_a_json_route_is_a_challenge(self):
        """Pairing is the one call a mentor runs interactively, so a challenge page met here
        must be named rather than reported as a bare status code."""
        session = FakeSession(FakeResponse(403, payload=None,
                                           text='<!DOCTYPE html><html>...',
                                           headers={'Content-Type': 'text/html'}))
        client = RelayClient('https://example.test', session=session)
        with self.assertRaises(ChallengeDetected):
            client.exchange('7K3MQ9XW')
        self.assertIsNone(client.token)

    def test_a_missing_retry_after_falls_back_to_sixty_seconds(self):
        session = FakeSession(FakeResponse(429, payload={}))
        client = RelayClient('https://example.test', session=session)
        with self.assertRaises(RateLimited) as caught:
            client.exchange('7K3MQ9XW')
        self.assertEqual(caught.exception.retry_after, 60.0)

    def test_an_absurd_retry_after_is_clamped_to_five_minutes(self):
        """A misconfigured proxy can answer with a day. The run loop should retry long
        before that, so the wait is capped."""
        session = FakeSession(FakeResponse(429, payload={}, headers={
            'Content-Type': 'application/json', 'Retry-After': '86400'}))
        client = RelayClient('https://example.test', session=session)
        with self.assertRaises(RateLimited) as caught:
            client.exchange('7K3MQ9XW')
        self.assertEqual(caught.exception.retry_after, 300.0)

    def test_a_negative_retry_after_becomes_zero(self):
        session = FakeSession(FakeResponse(429, payload={}, headers={
            'Content-Type': 'application/json', 'Retry-After': '-5'}))
        client = RelayClient('https://example.test', session=session)
        with self.assertRaises(RateLimited) as caught:
            client.exchange('7K3MQ9XW')
        self.assertEqual(caught.exception.retry_after, 0.0)


class TestSync(unittest.TestCase):
    SNAPSHOT = {'state': 'idle', 'file': '', 'percent': None}

    def test_a_snapshot_is_posted_with_the_bearer_token(self):
        session = FakeSession(FakeResponse(payload={'job': None}))
        client = RelayClient('https://example.test', token='signed', session=session)
        self.assertIsNone(client.sync(self.SNAPSHOT))
        method, url, kwargs = session.calls[0]
        self.assertEqual(url, 'https://example.test/printer/daemon/sync')
        self.assertEqual(kwargs['json'], self.SNAPSHOT)
        self.assertEqual(kwargs['headers']['Authorization'], 'Bearer signed')
        self.assertEqual(kwargs['headers']['User-Agent'], USER_AGENT)

    def test_a_job_is_returned(self):
        job = {'id': 'j8f3a2c1', 'download_path': '/download/tok',
               'filename': 'sample_part.gcode.3mf', 'state': 'handed_out'}
        session = FakeSession(FakeResponse(payload={'job': job}))
        client = RelayClient('https://example.test', token='signed', session=session)
        self.assertEqual(client.sync(self.SNAPSHOT)['id'], 'j8f3a2c1')

    def test_a_401_repair_raises_token_rejected(self):
        session = FakeSession(FakeResponse(401, payload={'error': 'repair'}))
        client = RelayClient('https://example.test', token='stale', session=session)
        with self.assertRaises(TokenRejected):
            client.sync(self.SNAPSHOT)

    def test_an_html_body_on_a_json_route_is_a_challenge(self):
        """A Cloudflare challenge is an ordinary HTTP response carrying an HTML page. It
        must be named, not parsed as JSON, so `status` can tell the mentor what happened."""
        session = FakeSession(FakeResponse(403, payload=None,
                                           text='<!DOCTYPE html><html>...',
                                           headers={'Content-Type': 'text/html'}))
        client = RelayClient('https://example.test', token='signed', session=session)
        with self.assertRaises(ChallengeDetected):
            client.sync(self.SNAPSHOT)

    def test_a_server_error_raises_a_plain_relay_error(self):
        session = FakeSession(FakeResponse(502, payload={'error': 'bad gateway'}))
        client = RelayClient('https://example.test', token='signed', session=session)
        with self.assertRaises(RelayError):
            client.sync(self.SNAPSHOT)

    def test_a_connection_error_becomes_a_relay_error(self):
        """The shop's uplink drops constantly. The run loop should see one exception type."""
        session = FakeSession(requests.ConnectionError('name resolution failed'))
        client = RelayClient('https://example.test', token='signed', session=session)
        with self.assertRaises(RelayError):
            client.sync(self.SNAPSHOT)


class TestAck(unittest.TestCase):
    def test_a_successful_ack_returns_true(self):
        session = FakeSession(FakeResponse(payload={'ok': True}))
        client = RelayClient('https://example.test', token='signed', session=session)
        self.assertTrue(client.ack('j8f3a2c1', True))
        method, url, kwargs = session.calls[0]
        self.assertEqual(url,
                         'https://example.test/printer/daemon/jobs/j8f3a2c1/ack')
        self.assertEqual(kwargs['json'], {'ok': True, 'error': None})

    def test_a_failed_ack_carries_one_line_of_reason(self):
        session = FakeSession(FakeResponse(payload={'ok': True}))
        client = RelayClient('https://example.test', token='signed', session=session)
        client.ack('j8f3a2c1', False, 'Upload to printer failed: connection refused')
        self.assertEqual(session.calls[0][2]['json']['ok'], False)
        self.assertEqual(session.calls[0][2]['json']['error'],
                         'Upload to printer failed: connection refused')

    def test_a_404_returns_false_rather_than_raising(self):
        """The relay no longer holds that job. The daemon treats it as final."""
        session = FakeSession(FakeResponse(404, payload={'error': 'unknown job'}))
        client = RelayClient('https://example.test', token='signed', session=session)
        self.assertFalse(client.ack('j8f3a2c1', True))

    def test_a_429_raises_with_the_retry_after(self):
        session = FakeSession(FakeResponse(429, payload={}, headers={
            'Content-Type': 'application/json', 'Retry-After': '17'}))
        client = RelayClient('https://example.test', token='signed', session=session)
        with self.assertRaises(RateLimited) as caught:
            client.ack('j8f3a2c1', True)
        self.assertEqual(caught.exception.retry_after, 17.0)

    def test_an_html_rate_limit_page_is_still_a_rate_limit(self):
        """A 429 served as an HTML page is the rate limit it says it is, not a challenge."""
        session = FakeSession(FakeResponse(429, payload=None,
                                           text='<!DOCTYPE html><html>slow down</html>',
                                           headers={'Content-Type': 'text/html',
                                                    'Retry-After': '30'}))
        client = RelayClient('https://example.test', token='signed', session=session)
        with self.assertRaises(RateLimited) as caught:
            client.ack('j8f3a2c1', True)
        self.assertEqual(caught.exception.retry_after, 30.0)


class TestPingAndDownload(unittest.TestCase):
    def test_ping_returns_the_flags(self):
        session = FakeSession(FakeResponse(payload={'ok': True, 'waiting': False,
                                                    'online': True}))
        client = RelayClient('https://example.test', token='signed', session=session)
        self.assertEqual(client.ping()['online'], True)
        self.assertEqual(session.calls[0][1],
                         'https://example.test/printer/daemon/ping')

    def test_download_joins_the_path_with_the_configured_backend(self):
        """The job carries a path, not a URL, so the relay never needs to know the name it
        is reached by."""
        session = FakeSession(FakeResponse(chunks=[b'3mf-', b'bytes']))
        client = RelayClient('http://192.168.1.5:6238', token='signed', session=session)
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, 'job.gcode.3mf')
            client.download('/download/tok', dest)
            with open(dest, 'rb') as fh:
                self.assertEqual(fh.read(), b'3mf-bytes')
        self.assertEqual(session.calls[0][1], 'http://192.168.1.5:6238/download/tok')

    def test_a_429_on_ping_raises_with_the_retry_after(self):
        session = FakeSession(FakeResponse(429, payload={}, headers={
            'Content-Type': 'application/json', 'Retry-After': '17'}))
        client = RelayClient('https://example.test', token='signed', session=session)
        with self.assertRaises(RateLimited) as caught:
            client.ping()
        self.assertEqual(caught.exception.retry_after, 17.0)

    def test_the_download_route_carries_no_bearer_token(self):
        """The token in the path is the whole authority; the session's is not the relay's
        business."""
        session = FakeSession(FakeResponse(chunks=[b'3mf']))
        client = RelayClient('https://example.test', token='signed', session=session)
        with tempfile.TemporaryDirectory() as tmp:
            client.download('/download/tok', os.path.join(tmp, 'job.3mf'))
        self.assertNotIn('Authorization', session.calls[0][2]['headers'])

    def test_a_429_on_download_raises_with_the_retry_after(self):
        session = FakeSession(FakeResponse(429, payload={}, headers={
            'Content-Type': 'application/json', 'Retry-After': '17'}))
        client = RelayClient('https://example.test', token='signed', session=session)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RateLimited) as caught:
                client.download('/download/tok', os.path.join(tmp, 'job.3mf'))
        self.assertEqual(caught.exception.retry_after, 17.0)

    def test_a_body_that_fails_mid_stream_leaves_no_file_behind(self):
        """A truncated .3mf handed to the printer is worse than no file at all, so the
        bytes land in a .part file that is renamed only once the last chunk arrives."""
        session = FakeSession(BrokenStream(chunks=[b'3mf-']))
        client = RelayClient('https://example.test', token='signed', session=session)
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, 'job.gcode.3mf')
            with self.assertRaises(RelayError):
                client.download('/download/tok', dest)
            self.assertEqual(os.listdir(tmp), [])

    def test_an_undeliverable_destination_raises_a_relay_error(self):
        """An unwritable directory is the daemon's own fault, but the run loop should still
        see one exception type."""
        session = FakeSession(FakeResponse(chunks=[b'3mf']))
        client = RelayClient('https://example.test', token='signed', session=session)
        with tempfile.TemporaryDirectory() as tmp:
            blocker = os.path.join(tmp, 'jobs')
            with open(blocker, 'w') as fh:       # a file where a directory must go
                fh.write('')
            with self.assertRaises(RelayError):
                client.download('/download/tok', os.path.join(blocker, 'job.3mf'))

    def test_a_failed_download_raises(self):
        session = FakeSession(FakeResponse(404, payload={'error': 'gone'}))
        client = RelayClient('https://example.test', token='signed', session=session)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RelayError):
                client.download('/download/tok', os.path.join(tmp, 'job.3mf'))


if __name__ == '__main__':
    unittest.main()

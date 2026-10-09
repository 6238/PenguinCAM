"""Tests for testbed/adapters.py: recording and replaying Onshape exchanges in OnshapeClient.

No test touches the network: record-mode tests talk to a local http.server thread, and
replay-mode tests answer from cassettes.
"""
import json
import os
import socket
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from onshape_integration import OnshapeClient
from testbed import adapters, settings
from testbed.adapters import (LedgerRetry, RecordingAdapter, ReplayAdapter, begin_recording,
                              current_scenario, finish_recording, install_testbed_adapters,
                              set_current_scenario)
from testbed.cassette import Cassette, Exchange
from testbed.ledger import AllowanceExhausted
from testbed.scrub import STANDIN_EMAIL, STANDIN_USER_ID, standin_company_id

FIXTURE_CASSETTES = Path(__file__).resolve().parent / 'fixtures' / 'cassettes'
REAL_EMAIL = 'real.owner@school.example.org'
REAL_USER_ID = '5f0e1d2c3b4a59687766aabb'
REAL_COMPANY_ID = '6a1b2c3d4e5f60718293a4b5'
JSON = {'Content-Type': 'application/json'}


class _ScriptedHandler(BaseHTTPRequestHandler):
    """Answers each path from server.routes[path]: a list of (status, headers, body).

    Each answer is used once; the last one repeats.
    """
    def _answer(self):
        length = int(self.headers.get('Content-Length') or 0)
        self.rfile.read(length)
        queue = self.server.routes.get(urlsplit(self.path).path, [(404, {}, b'')])
        status, headers, body = queue.pop(0) if len(queue) > 1 else queue[0]
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = _answer

    def log_message(self, *args):
        pass


class _TestbedCase(unittest.TestCase):
    mode = None

    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        env = {k: v for k, v in os.environ.items()
               if k not in ('PENGUINCAM_TESTBED', 'ONSHAPE_ACCESS_KEY', 'ONSHAPE_SECRET_KEY',
                            'TESTBED_BUDGET', 'TESTBED_CYCLE_START', 'TESTBED_RUN_CAP')}
        env['XDG_STATE_HOME'] = str(self.tmp / 'state')
        if self.mode:
            env['PENGUINCAM_TESTBED'] = self.mode
        self.enterContext(mock.patch.dict(os.environ, env, clear=True))
        settings.reset_testbed_settings_for_tests()
        self.addCleanup(settings.reset_testbed_settings_for_tests)
        adapters.reset_testbed_adapters_for_tests()
        self.addCleanup(adapters.reset_testbed_adapters_for_tests)

    def ledger_lines(self):
        path = self.tmp / 'state' / 'penguincam-testbed' / 'ledger.jsonl'
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]


class OffModeTest(_TestbedCase):
    def test_off_mode_installs_nothing(self):
        client = OnshapeClient()
        for prefix in ('https://cad.onshape.com', 'http://localhost'):
            adapter = client.session.get_adapter(prefix)
            self.assertIs(type(adapter), HTTPAdapter)
            self.assertIs(type(adapter.max_retries), Retry)
        session = requests.Session()
        before = dict(session.adapters)
        install_testbed_adapters(session, Retry(total=1))
        self.assertEqual(session.adapters, before)

    def test_from_api_keys_placeholder_only_in_replay(self):
        with self.assertRaises(ValueError):
            OnshapeClient.from_api_keys()
        for mode, expect_placeholder in (('record', False), ('replay', True)):
            with self.subTest(mode=mode):
                os.environ['PENGUINCAM_TESTBED'] = mode
                settings.reset_testbed_settings_for_tests()
                if expect_placeholder:
                    client = OnshapeClient.from_api_keys()
                    self.assertEqual((client.api_access_key, client.api_secret_key),
                                     ('testbed-replay', 'testbed-replay'))
                    given = OnshapeClient.from_api_keys('given-a', 'given-b')
                    self.assertEqual(given.api_access_key, 'given-a')
                else:
                    with self.assertRaises(ValueError):
                        OnshapeClient.from_api_keys()

    def test_current_scenario_shared_across_threads(self):
        set_current_scenario('part-select')
        seen = []
        worker = threading.Thread(target=lambda: seen.append(current_scenario()))
        worker.start()
        worker.join()
        self.assertEqual(seen, ['part-select'])
        worker = threading.Thread(target=set_current_scenario, args=('print-job',))
        worker.start()
        worker.join()
        self.assertEqual(current_scenario(), 'print-job')


class RecordModeTest(_TestbedCase):
    mode = 'record'

    def setUp(self):
        super().setUp()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), _ScriptedHandler)
        self.server.routes = {
            '/api/v13/users/sessioninfo': [(200, JSON, json.dumps(
                {'id': REAL_USER_ID, 'email': REAL_EMAIL, 'name': 'Real Owner'}).encode())],
            '/api/v13/companies': [(200, JSON, json.dumps(
                {'items': [{'id': REAL_COMPANY_ID, 'name': 'Classroom'}]}).encode())],
        }
        threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.05},
                         daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f'http://127.0.0.1:{self.server.server_address[1]}'
        self.enterContext(mock.patch.object(settings, 'FRESH_DIR', self.tmp / 'fresh'))
        self.client = OnshapeClient.from_api_keys('local-access', 'local-secret')
        self.client.API_BASE = f'{self.base}/api/v13'

    def fast_session(self):
        """A session with the record adapter and a retry that does not wait."""
        session = requests.Session()
        install_testbed_adapters(session, Retry(total=3, status=3, backoff_factor=0,
                                                status_forcelist=(503,), raise_on_status=False))
        return session

    def test_record_keeps_retry_parameters(self):
        client = OnshapeClient()
        for prefix in ('https://cad.onshape.com', 'http://localhost'):
            adapter = client.session.get_adapter(prefix)
            self.assertIsInstance(adapter, RecordingAdapter)
        retry = adapter.max_retries
        self.assertIsInstance(retry, LedgerRetry)
        self.assertEqual((retry.total, retry.connect, retry.read, retry.status), (4, 4, 4, 4))
        self.assertEqual(retry.backoff_factor, 0.5)
        self.assertEqual(tuple(retry.status_forcelist), (429, 500, 502, 503, 504))
        self.assertEqual(retry.allowed_methods, frozenset(['GET', 'POST', 'PUT', 'DELETE']))
        self.assertFalse(retry.raise_on_status)
        self.assertIsNotNone(retry.ledger)
        self.assertIs(retry.new(total=1).ledger, retry.ledger)
        self.assertIsInstance(retry.new(), LedgerRetry)

    def test_record_buffers_exchange_and_writes_scrubbed_on_finish(self):
        self.server.routes['/api/v13/documents'] = [(200, JSON, json.dumps({'items': [
            {'name': 'tb-plate', 'owner': {'id': REAL_USER_ID, 'email': REAL_EMAIL}}]}).encode())]
        begin_recording('rec-test', self.client)
        self.assertEqual(current_scenario(), 'rec-test')
        response = self.client._make_api_request('GET', '/documents?q=tb-')
        self.assertIn(REAL_EMAIL, response.text)       # the caller sees the real answer

        path = finish_recording()
        self.assertEqual(path, self.tmp / 'fresh' / 'rec-test.json')
        text = path.read_text()
        for real in (REAL_EMAIL, REAL_USER_ID, REAL_COMPANY_ID, 'local-secret', 'Authorization'):
            self.assertNotIn(real, text)
        cassette = Cassette.load(self.tmp / 'fresh', 'rec-test')
        self.assertEqual(len(cassette.exchanges), 1)    # the owner lookups are not in it
        ex = cassette.exchanges[0]
        self.assertEqual((ex.method, ex.path, ex.query, ex.status),
                         ('GET', '/api/v13/documents', {'q': ['tb-']}, 200))
        self.assertEqual(ex.headers, {'Content-Type': 'application/json'})
        self.assertIn(STANDIN_EMAIL, ex.body_text)
        self.assertIn(STANDIN_USER_ID, ex.body_text)
        lines = self.ledger_lines()
        self.assertEqual([l['path'] for l in lines], ['/api/v13/users/sessioninfo',
                                                      '/api/v13/companies', '/api/v13/documents'])
        self.assertTrue(all(l['counted'] and l['scenario'] == 'rec-test' for l in lines))
        with self.assertRaises(RuntimeError):
            finish_recording()                          # the buffer was cleared

    def test_record_keeps_binary_body(self):
        stl = bytes(range(256)) * 8
        self.server.routes['/part.stl'] = [(200, {'Content-Type': 'application/octet-stream'}, stl)]
        begin_recording('rec-binary', self.client)
        self.assertEqual(self.client.session.get(f'{self.base}/part.stl').content, stl)
        finish_recording()
        cassette = Cassette.load(self.tmp / 'fresh', 'rec-binary')
        self.assertEqual(cassette.exchanges[0].body(), stl)

    def test_record_each_redirect_hop(self):
        self.server.routes['/a'] = [(307, {'Location': f'{self.base}/b'}, b'')]
        self.server.routes['/b'] = [(200, {'Content-Type': 'text/plain'}, b'arrived')]
        begin_recording('rec-redirect', self.client)
        response = self.client.session.get(f'{self.base}/a')
        self.assertEqual(response.text, 'arrived')
        finish_recording()
        cassette = Cassette.load(self.tmp / 'fresh', 'rec-redirect')
        self.assertEqual([(ex.path, ex.status) for ex in cassette.exchanges],
                         [('/a', 307), ('/b', 200)])
        self.assertEqual(cassette.exchanges[0].headers['Location'], f'{self.base}/b')
        hops = [(l['path'], l['status']) for l in self.ledger_lines()[2:]]
        self.assertEqual(hops, [('/a', 307), ('/b', 200)])

    def test_retry_attempts_reach_ledger(self):
        self.server.routes['/flaky'] = [(503, {}, b''), (503, {}, b''),
                                        (200, {'Content-Type': 'text/plain'}, b'ok')]
        begin_recording('rec-retry', self.client)
        self.assertEqual(self.fast_session().get(f'{self.base}/flaky').text, 'ok')
        lines = [(l['kind'], l['status'], l['counted']) for l in self.ledger_lines()[2:]]
        self.assertEqual(lines, [('retry', 503, False), ('retry', 503, False),
                                 ('response', 200, True)])

    def test_402_stops_recording(self):
        self.server.routes['/api/v13/documents'] = [(402, JSON, b'{"message": "limit"}')]
        begin_recording('rec-402', self.client)
        with self.assertRaises(AllowanceExhausted):
            self.client._make_api_request('GET', '/documents')
        last = self.ledger_lines()[-1]
        self.assertEqual((last['status'], last['counted']), (402, False))


class ReplayModeTest(_TestbedCase):
    mode = 'replay'

    def setUp(self):
        super().setUp()
        self.cassettes = self.tmp / 'cassettes'
        self.enterContext(mock.patch.object(settings, 'CASSETTE_DIR', self.cassettes))
        # Any attempt to open a connection fails the test.
        self.enterContext(mock.patch.object(socket.socket, 'connect',
                                            side_effect=AssertionError('network used')))
        self.client = OnshapeClient.from_api_keys()

    def save(self, scenario, *exchanges):
        Cassette(scenario, list(exchanges), synthetic=True).save(self.cassettes)
        set_current_scenario(scenario)

    def ex(self, path, body='', status=200, method='GET', host='cad.onshape.com', query=None,
           request_body=None, headers=None):
        return Exchange(method=method, host=host, path=path, query=query or {},
                        request_body=request_body, status=status, headers=headers or {},
                        body_text=body, body_file=None)

    def test_replay_installs_replay_adapter(self):
        self.assertIsInstance(self.client.session.get_adapter('https://x'), ReplayAdapter)
        self.assertIsInstance(self.client.session.get_adapter('http://x'), ReplayAdapter)

    def test_replay_returns_recorded_body(self):
        with mock.patch.object(settings, 'CASSETTE_DIR', FIXTURE_CASSETTES):
            set_current_scenario('synthetic-sessioninfo')
            info = self.client.get_user_session_info()
            companies = self.client.get_companies()
        self.assertEqual(info['email'], STANDIN_EMAIL)
        self.assertEqual(info['id'], STANDIN_USER_ID)
        self.assertEqual([c['id'] for c in companies], [standin_company_id(1)])

    def test_replay_ignores_volatile_query_keys(self):
        self.save('volatile',
                  self.ex('/api/v13/x', 'by query', query={'microversionId': ['m1'], 'a': ['1']}),
                  self.ex('/api/v13/y', 'by body', method='POST',
                          request_body='{"k": "v", "timestamp": 1, "inner": {"createdAt": "t1"}}'))
        got = self.client.session.get('https://cad.onshape.com/api/v13/x?a=1&microversionId=m2')
        self.assertEqual((got.status_code, got.text), (200, 'by query'))
        got = self.client.session.post('https://cad.onshape.com/api/v13/y',
                                       json={'timestamp': 2, 'k': 'v', 'inner': {'createdAt': 't2'}})
        self.assertEqual((got.status_code, got.text), (200, 'by body'))
        got = self.client.session.get('https://cad.onshape.com/api/v13/x?a=2')
        self.assertEqual(got.status_code, 599)

    def test_unmatched_request_names_method_host_path(self):
        self.save('miss', self.ex('/api/v13/x', 'x'))
        got = self.client.session.get('https://cad.onshape.com/api/v13/nope?b=2')
        self.assertEqual(got.status_code, 599)
        self.assertEqual(got.text, 'not in the cassette: GET cad.onshape.com/api/v13/nope?b=2 '
                                   '(scenario miss)')
        set_current_scenario('no-such-cassette')
        got = self.client.session.get('https://cad.onshape.com/api/v13/x')
        self.assertEqual(got.status_code, 599)
        self.assertTrue(got.text.startswith('not in the cassette: GET cad.onshape.com/api/v13/x'))

    def test_exhausted_match_repeats_last(self):
        self.save('repeat', self.ex('/api/v13/x', 'one'), self.ex('/api/v13/x', 'two'))
        bodies = [self.client.session.get('https://cad.onshape.com/api/v13/x').text
                  for _ in range(4)]
        self.assertEqual(bodies, ['one', 'two', 'two', 'two'])
        set_current_scenario('repeat')                  # a new run starts from the top
        self.assertEqual(self.client.session.get('https://cad.onshape.com/api/v13/x').text, 'one')

    def test_translation_polls_replay_in_order(self):
        path = '/api/v13/translations/t1'
        self.save('translate', self.ex(path, '{"requestState": "ACTIVE"}'),
                  self.ex(path, '{"requestState": "ACTIVE"}'),
                  self.ex(path, '{"requestState": "DONE"}'))
        states = [self.client.session.get(f'https://cad.onshape.com{path}').json()['requestState']
                  for _ in range(3)]
        self.assertEqual(states, ['ACTIVE', 'ACTIVE', 'DONE'])
        self.assertEqual(self.client.session.get(f'https://cad.onshape.com{path}').status_code, 599)

    def test_replay_follows_recorded_redirect(self):
        self.save('redirect',
                  self.ex('/api/v13/parts/stl', status=307,
                          headers={'Location': 'https://cad-files.onshape.com/blob/part.stl'}),
                  self.ex('/blob/part.stl', 'solid part', host='cad-files.onshape.com'))
        got = self.client.session.get('https://cad.onshape.com/api/v13/parts/stl')
        self.assertEqual((got.status_code, got.text), (200, 'solid part'))
        self.assertEqual([r.status_code for r in got.history], [307])
        self.assertEqual(got.url, 'https://cad-files.onshape.com/blob/part.stl')

    def test_replay_answers_any_host_without_network(self):
        self.save('hosts', self.ex('/bucket/part.stl', 'from s3', host='s3.amazonaws.com'))
        got = self.client.session.get('https://s3.amazonaws.com/bucket/part.stl')
        self.assertEqual(got.text, 'from s3')
        for url in ('https://example.com/anything', 'http://127.0.0.1:9/x'):
            self.assertEqual(self.client.session.get(url).status_code, 599)


if __name__ == '__main__':
    unittest.main()

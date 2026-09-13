"""The daemon's side of the PenguinCAM protocol. Outbound HTTPS only; no ports are opened
on the Pi and nothing on the school network has to be reachable from outside.

Every method translates an HTTP answer into one of four outcomes the run loop knows how to
handle: a value, TokenRejected (re-exchange), RateLimited (honour Retry-After), or a plain
RelayError (log, back off, try again).
"""

import os

import requests

from printer_daemon import USER_AGENT


class RelayError(Exception):
    """Any failure talking to the backend. The run loop logs it and backs off."""


class TokenRejected(RelayError):
    """The backend answered 401 with {"error": "repair"}: blank the token and re-exchange.

    Happens after a redeploy without FLASK_SECRET_KEY set, or when a Pi's code was retired.
    """


class RateLimited(RelayError):
    def __init__(self, retry_after):
        super().__init__(f"rate limited, retry after {retry_after}s")
        self.retry_after = retry_after


class ChallengeDetected(RelayError):
    """An HTML body arrived on a JSON route -- almost always a Cloudflare challenge page.

    Named rather than parsed, so `penguincam-printer status` can tell a mentor what is
    actually happening instead of reporting a JSON parse error."""


class RelayClient:
    """One long-lived HTTP session to the backend.

    `backend_url` comes from the Pi's config file, and the daemon joins it with the path a
    job carries, so a development server reached as localhost by the browser and by a LAN
    address from the Pi works with no extra configuration."""

    def __init__(self, backend_url, token=None, *, session=None, timeout=20.0):
        self.backend_url = (backend_url or '').rstrip('/')
        self.token = token or None
        self.timeout = timeout
        self._session = session if session is not None else requests.Session()

    # -- plumbing ----------------------------------------------------------------------

    def _url(self, path):
        return f"{self.backend_url}{path}"

    def _headers(self, *, authorized=True):
        headers = {'User-Agent': USER_AGENT}
        if authorized and self.token:
            headers['Authorization'] = f'Bearer {self.token}'
        return headers

    #: A rate limit longer than this is a configuration accident, not a wait worth
    #: honouring: the run loop sleeps and retries rather than stalling for hours.
    MAX_RETRY_AFTER = 300.0

    @classmethod
    def _retry_after(cls, response):
        """Seconds to wait before retrying, clamped to something a run loop can sleep on."""
        try:
            value = float(response.headers.get('Retry-After', ''))
        except (TypeError, ValueError):
            return 60.0
        return max(0.0, min(value, cls.MAX_RETRY_AFTER))

    def _payload(self, response):
        """The JSON body, or an exception naming what arrived instead."""
        content_type = (response.headers.get('Content-Type') or '').lower()
        if 'json' not in content_type:
            body = (response.text or '').lstrip()[:200]
            raise ChallengeDetected(
                f"expected JSON from the backend, got {content_type or 'no content type'} "
                f"({body!r}). If this is a Cloudflare challenge page, see "
                f"docs/PRINTER_SETUP.md.")
        try:
            return response.json()
        except ValueError as e:
            raise RelayError(f"could not parse the backend's reply: {e}") from e

    def _check(self, response, what, *, handled=(), parse_body=True):
        """Everything all five calls answer alike, in one place: a rate limit, a challenge
        page, and the generic failure. Returns the status code, so a caller can act on the
        one or two answers it handles itself (`handled`, alongside 200).

        429 is translated before the body is inspected, so a rate-limit page served as HTML
        is reported as the rate limit it is and not as a challenge. `parse_body` is off for
        the download route, whose successful body is a file and never JSON."""
        status = response.status_code
        if status == 429:
            raise RateLimited(self._retry_after(response))
        if status == 200 or status in handled:
            return status
        if parse_body:
            self._payload(response)      # names a challenge page before the generic error
        raise RelayError(f"{what} answered {status}")

    def _request(self, method, path, *, authorized=True, **kwargs):
        try:
            fn = self._session.post if method == 'POST' else self._session.get
            return fn(self._url(path), headers=self._headers(authorized=authorized),
                      timeout=self.timeout, **kwargs)
        except requests.RequestException as e:
            raise RelayError(f"{method} {path} failed: {e}") from e

    # -- protocol ----------------------------------------------------------------------

    def exchange(self, pairing_code):
        """Trade the pairing code for a signed token. None means "not yet" -- no session has
        presented this code since the relay started, or another Pi holds it."""
        response = self._request('POST', '/printer/daemon/pair', authorized=False,
                                 json={'code': pairing_code})
        if self._check(response, 'pair', handled=(404,)) == 404:
            return None
        token = self._payload(response).get('token')
        if not token:
            raise RelayError("pair answered 200 with no token")
        self.token = token
        return token

    def sync(self, snapshot):
        """Post the status snapshot; return the job to act on, or None."""
        response = self._request('POST', '/printer/daemon/sync', json=snapshot)
        if response.status_code == 401:
            raise TokenRejected("the backend rejected the token; re-pairing")
        self._check(response, 'sync')
        return self._payload(response).get('job')

    def ack(self, job_id, ok, error=None):
        """Report a job's outcome. False means the relay no longer holds it: final."""
        response = self._request('POST', f'/printer/daemon/jobs/{job_id}/ack',
                                 json={'ok': bool(ok), 'error': error})
        if response.status_code == 401:
            raise TokenRejected("the backend rejected the token; re-pairing")
        return self._check(response, 'ack', handled=(404,)) != 404

    def ping(self):
        """Read-only health check for `penguincam-printer status`. Stores nothing on the
        relay, so it cannot disturb the running service."""
        response = self._request('GET', '/printer/daemon/ping')
        if response.status_code == 401:
            raise TokenRejected("the backend rejected the token")
        self._check(response, 'ping')
        return self._payload(response)

    def download(self, download_path, dest_path):
        """Fetch the sliced file to `dest_path`. The token in the path is the whole
        authority, so no bearer header is sent, and the route is fetched once per job.

        The bytes land in a sibling `.part` file that is renamed only after the last chunk
        arrives: a connection dropped mid-stream leaves no truncated .3mf for the printer to
        choke on."""
        response = self._request('GET', download_path, authorized=False, stream=True)
        with response:
            self._check(response, 'download', parse_body=False)
            part_path = f"{dest_path}.part"
            try:
                directory = os.path.dirname(dest_path)
                if directory:
                    os.makedirs(directory, exist_ok=True)
                with open(part_path, 'wb') as fh:
                    for chunk in response.iter_content(chunk_size=64 * 1024):
                        if chunk:
                            fh.write(chunk)
                os.replace(part_path, dest_path)
            except (requests.RequestException, OSError) as e:
                self._discard(part_path)
                raise RelayError(f"download of {download_path} failed: {e}") from e

    @staticmethod
    def _discard(path):
        """Remove a half-written part file, and never mask the failure that caused it."""
        try:
            os.unlink(path)
        except OSError:
            pass

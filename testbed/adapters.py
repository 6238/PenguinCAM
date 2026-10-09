"""Record and replay transport adapters, and the per-process current scenario.

OnshapeClient builds a new requests.Session for every client, so install_testbed_adapters()
is called from OnshapeClient.__init__ and mounts one adapter per session:

- record mode: RecordingAdapter makes the live call, writes it to the call ledger and
  buffers the exchange for the current scenario; finish_recording() scrubs the buffer and
  writes a cassette to testbed/.fresh/.
- replay mode: ReplayAdapter answers every request, on any host, from the current
  scenario's cassette and never opens a connection. A miss is a 599.

The current scenario is one value per process: every request thread reads it, so each
Onshape call is filed under the scenario that is current.
"""
import json
import threading
from http.client import responses as reason_phrases
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.response import HTTPResponse
from urllib3.util.retry import Retry

from testbed import settings
from testbed.cassette import KEPT_HEADERS, Cassette, Exchange
from testbed.ledger import AllowanceExhausted, CallLedger
from testbed.scrub import Scrubber, host_is_onshape

VOLATILE_KEYS = {'microversionId', 'timestamp', 'createdAt', 'modifiedAt'}
MISS_STATUS = 599                    # not in the client's retry list, so it fails at once
OWNER_LOOKUPS = ('/users/sessioninfo', '/companies?activeOnly=true&includeAll=false')

_state_lock = threading.Lock()
_current_scenario = None
_recording = None                    # the ScenarioRecording in record mode, or None
_replay_states = {}                  # (cassette dir, scenario) -> _ReplayState
# Set by the first 402. The client's broad `except Exception` handlers swallow the
# AllowanceExhausted a 402 raises, so this latch is what really stops a recording: every
# later send refuses without touching the network, and record commands check it.
_allowance_exhausted = threading.Event()


# ---------------------------------------------------------------------------
# The current scenario
# ---------------------------------------------------------------------------

def set_current_scenario(name):
    """Make `name` current. Replay restarts from the top of every cassette."""
    global _current_scenario
    with _state_lock:
        _current_scenario = name
        _replay_states.clear()


def current_scenario():
    with _state_lock:
        return _current_scenario


def allowance_exhausted():
    """True once Onshape has answered 402 in this process; nothing live runs after it."""
    return _allowance_exhausted.is_set()


def reset_testbed_adapters_for_tests():
    global _current_scenario, _recording
    with _state_lock:
        _current_scenario = None
        _recording = None
        _replay_states.clear()
    _allowance_exhausted.clear()


# ---------------------------------------------------------------------------
# Record mode
# ---------------------------------------------------------------------------

class ScenarioRecording:
    """The exchanges of the scenario being recorded, unscrubbed until finish_recording()."""

    def __init__(self, scenario, scrubber):
        self.scenario = scenario
        self.scrubber = scrubber
        self.exchanges = []
        self._lock = threading.Lock()

    def add(self, exchange):
        with self._lock:
            self.exchanges.append(exchange)


def _active_recording():
    with _state_lock:
        return _recording


def _owner_lookup(client, endpoint):
    response = client._make_api_request('GET', endpoint)
    if response.status_code != 200:
        raise RuntimeError(f'a recording needs {endpoint} to learn what to scrub; '
                           f'Onshape answered HTTP {response.status_code}')
    return response.json()


def current_recording_scrubber():
    """The Scrubber of the recording in progress, or None. Message logs are saved with it."""
    recording = _active_recording()
    return recording.scrubber if recording else None


def begin_recording(name, client):
    """Make `name` current and learn the owner's values to scrub, using `client`.

    The two owner lookups are live, counted calls in the ledger, but not part of the
    scenario's cassette.
    """
    global _recording
    set_current_scenario(name)
    with _state_lock:
        _recording = None
    sessioninfo = _owner_lookup(client, OWNER_LOOKUPS[0])
    companies = _owner_lookup(client, OWNER_LOOKUPS[1]).get('items', [])
    with _state_lock:
        _recording = ScenarioRecording(name, Scrubber.from_owner(sessioninfo, companies))


def finish_recording() -> Path:
    """Scrub the buffer, write it to settings.FRESH_DIR, clear it and return the path.

    After a 402 the buffer is discarded and AllowanceExhausted raised instead: a partial
    recording must not reach the drift report as if it were complete.
    """
    global _recording
    with _state_lock:
        recording, _recording = _recording, None
    if recording is None:
        raise RuntimeError('no recording in progress; call begin_recording first')
    if allowance_exhausted():
        raise AllowanceExhausted(f'recording {recording.scenario} discarded: Onshape answered '
                                 '402, the annual API allowance is used up')
    with recording._lock:                  # a late request thread may still be appending
        exchanges = list(recording.exchanges)
    scrubbed = [recording.scrubber.scrub_exchange(ex) for ex in exchanges]
    return Cassette(recording.scenario, scrubbed).save(settings.FRESH_DIR)


class LedgerRetry(Retry):
    """The client's Retry, with every retry attempt written to the call ledger.

    A retry inside urllib3 never reaches the adapter, so this is the only place that
    sees it. A line is written only once the attempt is going to be retried: when the
    retries run out, super() raises and the last response goes back to the adapter,
    which records it as the response.
    """

    def __init__(self, *args, ledger=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.ledger = ledger

    @classmethod
    def from_retry(cls, retry, ledger):
        """A LedgerRetry with every parameter of `retry`, whatever the urllib3 version."""
        template = cls.__new__(cls)
        template.__dict__.update(vars(retry))
        template.ledger = ledger
        return template.new()

    def new(self, **kw):
        following = super().new(**kw)
        following.ledger = self.ledger
        return following

    def increment(self, method=None, url=None, response=None, error=None, _pool=None,
                  _stacktrace=None):
        following = super().increment(method, url, response, error, _pool, _stacktrace)
        if self.ledger is not None:
            status = response.status if response is not None else 0
            self.ledger.record_call(current_scenario(), method, urlsplit(url or '').path,
                                    status, kind='retry')
        return following


def _request_body_text(request):
    body = request.body
    if isinstance(body, bytes):
        return body.decode('utf-8', errors='replace')
    return body if isinstance(body, str) else None


def _response_body(response):
    """(body_text, body_bytes): text for JSON, text and YAML answers, bytes otherwise."""
    data = response.content or b''
    if not data:
        return None, None
    content_type = response.headers.get('Content-Type', '').lower()
    if any(kind in content_type for kind in ('json', 'text', 'xml', 'yaml', 'javascript')):
        try:
            return data.decode('utf-8'), None
        except UnicodeDecodeError:
            pass
    return None, data


def _exchange_from(request, response):
    parts = urlsplit(request.url)
    body_text, body_bytes = _response_body(response)
    return Exchange(
        method=request.method, host=parts.netloc.lower(), path=parts.path,
        query=parse_qs(parts.query, keep_blank_values=True),
        request_body=_request_body_text(request), status=response.status_code,
        headers={name: response.headers[name] for name in KEPT_HEADERS
                 if name in response.headers},
        body_text=body_text, body_file=None, body_bytes=body_bytes)


class RecordingAdapter(HTTPAdapter):
    """Makes the live call, writes it to the ledger and buffers it for the scenario."""

    def __init__(self, ledger=None, **kwargs):
        self.ledger = ledger or CallLedger()
        super().__init__(**kwargs)

    def send(self, request, **kwargs):
        if allowance_exhausted():
            raise AllowanceExhausted('refusing a live Onshape call: an earlier call got 402, '
                                     'the annual API allowance is used up')
        response = super().send(request, **kwargs)
        # Every live exchange is a ledger line, the 402 included, before anything stops.
        self.ledger.record_call(current_scenario(), request.method,
                                urlsplit(request.url).path, response.status_code)
        if response.status_code == 402:
            _allowance_exhausted.set()
            response.close()
        self.ledger.raise_if_allowance_exhausted(response.status_code)
        recording = _active_recording()
        if recording is not None:
            recording.add(_exchange_from(request, response))   # reads the whole body
        return response


# ---------------------------------------------------------------------------
# Replay mode
# ---------------------------------------------------------------------------

def _without_volatile(value):
    if isinstance(value, dict):
        return {k: _without_volatile(v) for k, v in value.items() if k not in VOLATILE_KEYS}
    if isinstance(value, list):
        return [_without_volatile(v) for v in value]
    return value


def _normal_query(query):
    return tuple(sorted((key, tuple(values)) for key, values in query.items()
                        if key not in VOLATILE_KEYS))


def _normal_body(text):
    # JSON compared without volatile keys, anything else as exact text: a multipart upload
    # with a random boundary would never match, so one needs its own rule first.
    if not text:
        return None
    try:
        return json.dumps(_without_volatile(json.loads(text)), sort_keys=True)
    except ValueError:
        return text


def _match_key(method, host, path, query, body_text):
    # A foreign host's query was dropped when it was recorded (it may be a signature), so
    # a request to one matches on method, host and path alone.
    query = _normal_query(query) if host_is_onshape(host) else ()
    return (method.upper(), host.lower(), path, query, _normal_body(body_text))


class _ReplayState:
    """One scenario's cassette and which of its exchanges have been answered."""

    def __init__(self, cassette):
        self.exchanges = cassette.exchanges
        self.keys = [_match_key(ex.method, ex.host, ex.path, ex.query, ex.request_body)
                     for ex in cassette.exchanges]
        self.used = set()
        self.lock = threading.Lock()

    def answer(self, key, path):
        """The recorded exchange for `key`, or None.

        Each exchange answers once, in recorded order. When all are used the last one
        repeats, except for translation status polls, which replay strictly in order.
        """
        with self.lock:
            matches = [i for i, k in enumerate(self.keys) if k == key]
            for index in matches:
                if index not in self.used:
                    self.used.add(index)
                    return self.exchanges[index]
            if matches and '/translations/' not in path:
                return self.exchanges[matches[-1]]
            return None


def _replay_state(scenario):
    if scenario is None:
        return None
    directory = settings.CASSETTE_DIR
    with _state_lock:
        state = _replay_states.get((directory, scenario))
        if state is None:
            try:
                cassette = Cassette.load(directory, scenario)
            except FileNotFoundError:
                return None
            state = _replay_states[(directory, scenario)] = _ReplayState(cassette)
        return state


class ReplayAdapter(HTTPAdapter):
    """Answers every request from the current scenario's cassette; never opens a socket."""

    def send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):
        scenario = current_scenario()
        parts = urlsplit(request.url)
        key = _match_key(request.method, parts.netloc, parts.path,
                         parse_qs(parts.query, keep_blank_values=True),
                         _request_body_text(request))
        state = _replay_state(scenario)
        exchange = state.answer(key, parts.path) if state else None
        if exchange is None:
            query = f'?{parts.query}' if parts.query else ''
            status, headers = MISS_STATUS, {'Content-Type': 'text/plain'}
            missing = '' if state or scenario is None else f'; no cassette file {scenario}.json'
            body = (f'not in the cassette: {request.method} {parts.netloc}{parts.path}{query} '
                    f'(scenario {scenario}{missing})').encode()
        else:
            status, headers, body = exchange.status, dict(exchange.headers), exchange.body()
        raw = HTTPResponse(body=BytesIO(body), headers=headers, status=status,
                           reason=reason_phrases.get(status, ''), preload_content=False,
                           decode_content=False, request_method=request.method)
        return self.build_response(request, raw)


def install_testbed_adapters(session: requests.Session, retry: Retry) -> None:
    """Mount the test bed mode's adapter on `session`; do nothing when the test bed is off."""
    mode = settings.testbed_mode()
    if mode is None:
        return
    if mode == 'record':
        ledger = CallLedger()
        adapter = RecordingAdapter(ledger=ledger, max_retries=LedgerRetry.from_retry(retry, ledger))
    else:
        adapter = ReplayAdapter(max_retries=retry)     # carried for symmetry; never used
    session.mount('https://', adapter)
    session.mount('http://', adapter)

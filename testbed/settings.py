"""The test bed's switch, its import guards and every test bed setting.

The test bed is on only when PENGUINCAM_TESTBED is 'replay' or 'record'. Every test bed
setting is read here and nowhere else. This module imports only the standard library, so
importing it can never fail in a way that silently disables the Onshape integration.
"""
import os
from datetime import date
from pathlib import Path

MODE_VARIABLE = 'PENGUINCAM_TESTBED'
MODES = ('replay', 'record')

RECORD_PORT = 6238            # the development server, pinned by the Onshape dev app
REPLAY_PORT = 6240            # the replay development server the browser scenarios start
FAKE_HOST_ORIGIN = 'https://cad-testbed.onshape.com'
# The API key pair a replay-mode client gets when none is set; replay never sends it anywhere.
REPLAY_PLACEHOLDER_KEYS = ('testbed-replay', 'testbed-replay')

TESTBED_DIR = Path(__file__).resolve().parent
CASSETTE_DIR = TESTBED_DIR / 'cassettes'
MESSAGE_DIR = TESTBED_DIR / 'messages'
FRESH_DIR = TESTBED_DIR / '.fresh'
FIXTURE_DIR = TESTBED_DIR / 'fixtures'
# The test documents' ids, written by the documents builder and read by the scenarios.
DOCUMENTS_FILE = TESTBED_DIR / 'documents.json'

# The production Onshape app's client id starts with this; a recording must never use it.
_PRODUCTION_CLIENT_ID_PREFIX = 'VKDK'

_cached_mode = None
_mode_was_read = False


def _read_mode(environ):
    value = environ.get(MODE_VARIABLE, '').strip()
    if not value:
        return None
    if value not in MODES:
        raise ValueError(f"{MODE_VARIABLE} must be 'replay' or 'record', not {value!r}")
    return value


def testbed_mode():
    """'replay', 'record' or None. Read once; reset_testbed_settings_for_tests() clears it."""
    global _cached_mode, _mode_was_read
    if not _mode_was_read:
        _cached_mode = _read_mode(os.environ)
        _mode_was_read = True
    return _cached_mode


def testbed_is_on():
    return testbed_mode() is not None


def reset_testbed_settings_for_tests():
    global _cached_mode, _mode_was_read
    _cached_mode = None
    _mode_was_read = False


def _looks_deployed(environ):
    # The signals the app already uses to tell a deployed server from a development one.
    return bool(environ.get('FLASK_ENV') == 'production'
                or environ.get('RAILWAY_ENVIRONMENT') or environ.get('VERCEL'))


def assert_testbed_allowed(environ=os.environ):
    """Refuse a test bed on a deployed server, and a recording against any but the dev app.

    Called when frc_cam_gui_app is imported, so gunicorn hits it too. Reads `environ`
    directly rather than the cached mode, so a test can pass its own environment.
    """
    mode = _read_mode(environ)
    if mode is None:
        return
    if _looks_deployed(environ):
        raise RuntimeError(f'{MODE_VARIABLE} must not be set on a deployed server')
    if mode == 'record':
        for name in ('ONSHAPE_CLIENT_ID', 'ONSHAPE_CLIENT_SECRET'):
            if not environ.get(name):
                raise RuntimeError(f'record mode needs {name}')
        if environ['ONSHAPE_CLIENT_ID'].startswith(_PRODUCTION_CLIENT_ID_PREFIX):
            raise RuntimeError('record mode refuses the production Onshape app')


def state_dir():
    """$XDG_STATE_HOME/penguincam-testbed (default ~/.local/state/...), created on demand."""
    base = os.environ.get('XDG_STATE_HOME') or Path.home() / '.local' / 'state'
    path = Path(base) / 'penguincam-testbed'
    path.mkdir(parents=True, exist_ok=True)
    return path


def _env_int(name, default):
    value = os.environ.get(name, '').strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        raise ValueError(f'{name} must be a whole number, not {value!r}') from None


def _env_date(name):
    value = os.environ.get(name, '').strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError(f'{name} must be an ISO date (YYYY-MM-DD), not {value!r}') from None


def budget_settings():
    """(annual budget, cycle start date or None, per-run cap) of counted Onshape calls."""
    return (_env_int('TESTBED_BUDGET', 250), _env_date('TESTBED_CYCLE_START'),
            _env_int('TESTBED_RUN_CAP', 150))

"""The test bed's routes in the development server: sign-in, scenarios and message logs.

register_testbed_routes() is called from frc_cam_gui_app only when the test bed is on, so
on any other server none of these routes exists (spec 5.5, 5.6 and 6):

- GET  /testbed/sign-in   replay and local modes: development sign-in with the API keys
- GET  /testbed/scenarios the scenarios, their steps and the current scenario
- POST /testbed/scenario  {"name"}: make a scenario current (record mode starts recording)
- POST /testbed/messages  {"step", "events"}: append to the current message log
- POST /testbed/done      save the message log and, in record mode, the cassette
- GET  /testbed/panel.js  the checklist strip

Local mode has none of the scenario routes, and instead:

- GET  /testbed/local          the stand-in Onshape Part Studio page
- GET  /testbed/local/state    this session's stand-in ids and uploaded parts
- POST /testbed/local/parts    multipart `files`: upload STL files as parts
- POST /testbed/local/clear    remove every uploaded part
- GET  /testbed/local/<script>.js  the stand-in's scripts

In local mode the panel's frame-ancestors also allows the server's own origin ('self'),
the print page is told that origin (testbed_local_origin) so it accepts the stand-in's
messages, and Onshape's OAuth routes answer 403: local mode never talks to Onshape.
"""
import json
import threading
from dataclasses import asdict

from flask import Blueprint, abort, jsonify, request, send_file, session

from onshape_integration import session_manager
from testbed import local, settings
from testbed.adapters import begin_recording, current_scenario, finish_recording, set_current_scenario
from testbed.messages import save_message_log
from testbed.scenarios import SCENARIOS, scenarios_from_json
from testbed.scrub import STANDIN_EMAIL

PANEL_SCRIPT = settings.TESTBED_DIR / 'panel' / 'testbed_panel.js'
HOST_DIR = settings.TESTBED_DIR / 'host'
LOCAL_PAGE = HOST_DIR / 'local.html'
LOCAL_SCRIPTS = ('local.js', 'onshape_shapes.js')
LOCAL_IDS_KEY = 'testbed_local_ids'
# The panel's framing rule (the CNC panel and the print page set the same one), and what
# local mode makes of it so the stand-in page on the server's own origin may frame it.
PANEL_FRAME_ANCESTORS = 'frame-ancestors https://*.onshape.com'
LOCAL_FRAME_ANCESTORS = PANEL_FRAME_ANCESTORS + " 'self'"
# Onshape's OAuth routes: refused in local mode, which never sends anyone to Onshape.
ONSHAPE_OAUTH_PATHS = ('/onshape/auth', '/onshape/oauth/callback')
LOCAL_OAUTH_REFUSED = ('Local test bed mode never signs in to Onshape. Open /testbed/local, '
                       'which signs the panel in with the stand-in user.')

_log_lock = threading.Lock()
_message_log = None          # the current scenario's message log, until Done saves it


def reset_message_log_for_tests():
    global _message_log
    with _log_lock:
        _message_log = None


def _error(message, status):
    return jsonify({'error': message}), status


def sign_in():
    """Mark the session signed in with the API keys. The keys never enter the cookie."""
    session['testbed_apikey'] = True
    session['user_email'] = STANDIN_EMAIL
    return '', 204


def known_scenarios() -> dict:
    """SCENARIOS, and in replay mode the browser self-tests' synthetic copies, if a self-test
    started this server with TESTBED_SYNTHETIC_SCENARIOS."""
    if settings.testbed_mode() != 'replay' or not settings.SYNTHETIC_SCENARIOS_FILE:
        return SCENARIOS
    with open(settings.SYNTHETIC_SCENARIOS_FILE) as f:
        return {**SCENARIOS, **scenarios_from_json(json.load(f))}


def list_scenarios():
    return jsonify({'current': current_scenario(), 'scenarios': [
        {'name': s.name, 'document': s.document, 'panel': s.panel,
         'steps': [asdict(step) for step in s.steps]} for s in known_scenarios().values()]})


def choose_scenario():
    global _message_log
    name = (request.get_json(silent=True) or {}).get('name')
    if name not in known_scenarios():
        return _error(f'no scenario {name!r}', 400)
    if settings.testbed_mode() == 'record':
        client = session_manager.get_client(session.get('user_email', 'default_user'))
        if client is None:
            return _error('connect the panel to Onshape before recording', 401)
        try:
            begin_recording(name, client)
        except Exception as exc:          # the owner lookups failed: say why, in the strip
            return _error(f'could not start recording {name}: {exc}', 502)
    else:
        set_current_scenario(name)
    with _log_lock:
        _message_log = {'scenario': name, 'synthetic': False, 'steps': []}
    return jsonify({'scenario': name})


def add_messages():
    body = request.get_json(silent=True) or {}
    step, events = body.get('step'), body.get('events')
    if not isinstance(step, str) or not isinstance(events, list):
        return _error('expected {"step": str, "events": list}', 400)
    with _log_lock:
        if _message_log is None:
            return _error('choose a scenario first', 409)
        for entry in _message_log['steps']:
            if entry['step'] == step:
                entry['events'].extend(events)
                break
        else:
            _message_log['steps'].append({'step': step, 'events': list(events)})
    return '', 204


def done():
    global _message_log
    with _log_lock:
        log = _message_log
    if log is None:
        return _error('choose a scenario first', 409)
    # Saved before finish_recording(), which drops the scrubber that knows the owner.
    result = {'messages': str(save_message_log(log)), 'cassette': None}
    if settings.testbed_mode() == 'record':
        try:
            result['cassette'] = str(finish_recording())
        except Exception as exc:          # a 402, or no recording: the strip shows it
            # finish_recording() has discarded the buffer either way, so Done cannot retry;
            # the message log is kept and saved again by the next Done.
            return _error(f'the message log was saved but the cassette was not: {exc}. '
                          'Choose the scenario again to record it afresh.', 500)
    with _log_lock:
        if _message_log is log:
            _message_log = None
    return jsonify(result)


def panel_script():
    return send_file(str(PANEL_SCRIPT), mimetype='text/javascript', max_age=0)


# ---------------------------------------------------------------------------
# Local mode
# ---------------------------------------------------------------------------

def _local_ids():
    """This session's stand-in Part Studio ids, made on first use."""
    ids = session.get(LOCAL_IDS_KEY)
    if not local.valid_ids(ids):
        ids = session[LOCAL_IDS_KEY] = local.new_part_studio_ids()
    return ids


def local_page():
    _local_ids()
    return send_file(str(LOCAL_PAGE), mimetype='text/html', max_age=0)


def local_script(name):
    if name not in LOCAL_SCRIPTS:
        abort(404)
    return send_file(str(HOST_DIR / name), mimetype='text/javascript', max_age=0)


def _local_state(ids, errors=()):
    return jsonify({'ids': ids, 'origin': f'{request.scheme}://{request.host}',
                    'parts': [{'partId': p['partId'], 'name': p['name'], 'bytes': p['bytes']}
                              for p in local.list_parts(ids)],
                    'errors': list(errors)})


def local_state():
    return _local_state(_local_ids())


def local_upload():
    ids = _local_ids()
    files = request.files.getlist('files')
    if not files:
        return _error('choose one or more STL files', 400)
    errors = []
    for upload in files:
        try:
            local.add_part(ids, upload.filename, upload.read())
        except local.UploadRefused as exc:
            errors.append(f'{upload.filename or "a file"}: {exc}')
    return _local_state(ids, errors)


def local_clear():
    ids = _local_ids()
    local.clear_parts(ids)
    return _local_state(ids)


def _register_local(app, bp):
    bp.add_url_rule('/testbed/local', view_func=local_page, methods=['GET'])
    bp.add_url_rule('/testbed/local/state', view_func=local_state, methods=['GET'])
    bp.add_url_rule('/testbed/local/parts', view_func=local_upload, methods=['POST'])
    bp.add_url_rule('/testbed/local/clear', view_func=local_clear, methods=['POST'])
    bp.add_url_rule('/testbed/local/<name>', view_func=local_script, methods=['GET'])

    @app.before_request
    def _refuse_onshape_oauth():
        if request.path in ONSHAPE_OAUTH_PATHS:
            return LOCAL_OAUTH_REFUSED, 403, {'Content-Type': 'text/plain; charset=utf-8'}
        return None

    @app.after_request
    def _let_the_stand_in_frame_the_panel(response):
        if response.headers.get('Content-Security-Policy') == PANEL_FRAME_ANCESTORS:
            response.headers['Content-Security-Policy'] = LOCAL_FRAME_ANCESTORS
        return response


def register_testbed_routes(app, limiter=None) -> None:
    """Add the routes and the template flag. `limiter`, the app's Flask-Limiter, exempts
    them: they exist only on a development server, and a browser run calls them in bursts."""
    mode = settings.testbed_mode()
    bp = Blueprint('testbed', __name__)
    if mode in ('replay', 'local'):
        # Never in record mode, where the server holds real keys on all interfaces.
        bp.add_url_rule('/testbed/sign-in', view_func=sign_in, methods=['GET'])
    if mode == 'local':
        _register_local(app, bp)
    else:
        bp.add_url_rule('/testbed/scenarios', view_func=list_scenarios, methods=['GET'])
        bp.add_url_rule('/testbed/scenario', view_func=choose_scenario, methods=['POST'])
        bp.add_url_rule('/testbed/messages', view_func=add_messages, methods=['POST'])
        bp.add_url_rule('/testbed/done', view_func=done, methods=['POST'])
        bp.add_url_rule('/testbed/panel.js', view_func=panel_script, methods=['GET'])
    if limiter is not None:
        limiter.exempt(bp)
    app.register_blueprint(bp)

    @app.context_processor
    def _testbed_template_flag():
        # testbed_mode loads the checklist strip, which local mode has no use for: the
        # print page there looks exactly as it does in Onshape.
        if mode == 'local':
            return {'testbed_mode': None,
                    'testbed_local_origin': f'{request.scheme}://{request.host}'}
        return {'testbed_mode': mode}

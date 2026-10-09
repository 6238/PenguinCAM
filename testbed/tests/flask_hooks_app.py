"""Exercises the test bed's hooks in the full app; run in a subprocess by test_flask_hooks.

The app cannot be imported twice in one process (Flask refuses to re-add blueprint
rules), so each test bed mode gets a fresh process. The caller sets PENGUINCAM_TESTBED or
leaves it unset. Usage: flask_hooks_app.py CASSETTE_DIR. Prints one line,
`TESTBED-RESULT <json>`, after whatever the app logs while importing.
"""
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from flask import session  # noqa: E402

import frc_cam_gui_app  # noqa: E402
from onshape_integration import session_manager  # noqa: E402
from testbed import settings  # noqa: E402
from testbed.adapters import set_current_scenario  # noqa: E402

PANEL_QUERY = 'documentId=d1&workspaceId=w1&elementId=e1&server=https%3A%2F%2Fcad.onshape.com'


def check_replay(app, results):
    with app.test_client() as c:
        results['sign_in_status'] = c.get('/testbed/sign-in').status_code
        c.get('/testbed/scenarios')
        # The context of the last request is kept inside `with c`, so its session is live.
        client = session_manager.get_client(None)
        results['client_auth_mode'] = client.auth_mode if client else None
        results['client_cached'] = client is session_manager.get_client(None)
        session_manager.update_session_tokens(client)
        results['tokens_after_update'] = 'onshape_tokens' in session

        set_current_scenario('synthetic-sessioninfo')
        results['status'] = c.get('/onshape/status').get_json()
        with c.session_transaction() as cookie:
            results['tokens_in_cookie'] = 'onshape_tokens' in cookie
            results['user_email'] = cookie.get('user_email')

        page = c.get(f'/print?source=onshape&return=/onshape-panel?{PANEL_QUERY}')
        results['print_status'] = page.status_code
        results['print_html'] = page.get_data(as_text=True)
        cnc = c.get(f'/onshape-panel?{PANEL_QUERY}')
        results['cnc_status'] = cnc.status_code
        results['cnc_html'] = cnc.get_data(as_text=True)
        script = c.get('/testbed/panel.js')
        results['panel_js_status'] = script.status_code
        results['panel_js_type'] = script.mimetype


def check_off(app, results):
    with app.test_client() as c:
        with c.session_transaction() as cookie:      # a forged flag, as a crafted cookie
            cookie['testbed_apikey'] = True
        c.get('/onshape/status')
        results['client_is_none'] = session_manager.get_client(None) is None
        results['sign_in_status'] = c.get('/testbed/sign-in').status_code
        results['panel_js_status'] = c.get('/testbed/panel.js').status_code
        results['cnc_html'] = c.get(f'/onshape-panel?{PANEL_QUERY}').get_data(as_text=True)


def main():
    settings.CASSETTE_DIR = Path(sys.argv[1])
    app = frc_cam_gui_app.app
    app.config['TESTING'] = True
    results = {'mode': settings.testbed_mode()}
    (check_replay if settings.testbed_mode() == 'replay' else check_off)(app, results)
    print('TESTBED-RESULT ' + json.dumps(results))


if __name__ == '__main__':
    main()

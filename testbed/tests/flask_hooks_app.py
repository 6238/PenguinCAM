"""Exercises the test bed's hooks in the full app; run in a subprocess by test_flask_hooks.

The app cannot be imported twice in one process (Flask refuses to re-add blueprint
rules), so each test bed mode gets a fresh process. The caller sets PENGUINCAM_TESTBED or
leaves it unset. Usage: flask_hooks_app.py CASSETTE_DIR. Prints one line,
`TESTBED-RESULT <json>`, after whatever the app logs while importing.
"""
import io
import json
import re
import struct
import sys
from pathlib import Path
from urllib.parse import quote

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from flask import session  # noqa: E402

import frc_cam_gui_app  # noqa: E402
from onshape_integration import session_manager  # noqa: E402
from testbed import settings  # noqa: E402
from testbed.adapters import set_current_scenario  # noqa: E402
from print import limits  # noqa: E402
from testbed.tests.fixtures.make_print_fixtures import box_stl, cylinder_stl  # noqa: E402

PANEL_QUERY = 'documentId=d1&workspaceId=w1&elementId=e1&server=https%3A%2F%2Fcad.onshape.com'


def many_triangles_stl(count):
    """A binary STL of `count` tiny triangles along x: only its triangle count matters."""
    body = b''.join(struct.pack('<12fH', 0, 0, 1, i, 0, 0, i + 1, 0, 0, i, 1, 0, 0)
                    for i in range(count))
    return b'many'.ljust(80, b' ') + struct.pack('<I', count) + body


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

        page = c.get('/print?source=onshape&return='
                     + quote(f'/onshape-panel?{PANEL_QUERY}', safe=''))
        results['print_status'] = page.status_code
        results['print_html'] = page.get_data(as_text=True)
        cnc = c.get(f'/onshape-panel?{PANEL_QUERY}')
        results['cnc_status'] = cnc.status_code
        results['cnc_html'] = cnc.get_data(as_text=True)
        panel_pages(c, results)
        script = c.get('/testbed/panel.js')
        results['panel_js_status'] = script.status_code
        results['panel_js_type'] = script.mimetype

        # The /testbed routes are exempt from the app's limiter (200 per hour by default)...
        results['burst_statuses'] = sorted({c.get('/testbed/scenarios').status_code
                                            for _ in range(250)})
        # ...which is live here: /onshape/status allows 30 per minute.
        results['status_burst_statuses'] = sorted({c.get('/onshape/status').status_code
                                                   for _ in range(35)})


def panel_pages(c, results, query=PANEL_QUERY):
    """The CNC panel's and the print page's framing rule, and the print page's local origin."""
    cnc = c.get(f'/onshape-panel?{query}')
    page = c.get('/print?source=onshape&return=' + quote(f'/onshape-panel?{query}', safe=''))
    html = page.get_data(as_text=True)
    results['cnc_csp'] = cnc.headers.get('Content-Security-Policy')
    results['print_csp'] = page.headers.get('Content-Security-Policy')
    match = re.search(r'testbedLocalOrigin: (.*),\n', html)
    results['local_origin'] = json.loads(match.group(1)) if match else 'missing'
    results['oauth_status'] = c.get('/onshape/auth').status_code
    return html


def check_local(app, results):
    with app.test_client() as c:
        results['local_page_status'] = c.get('/testbed/local').status_code
        results['sign_in_status'] = c.get('/testbed/sign-in').status_code
        ids = c.get('/testbed/local/state').get_json()['ids']
        results['scenarios_status'] = c.get('/testbed/scenarios').status_code
        results['panel_js_status'] = c.get('/testbed/panel.js').status_code
        upload = c.post('/testbed/local/parts', content_type='multipart/form-data', data={
            'files': [(io.BytesIO(box_stl()), 'Box.stl'), (io.BytesIO(cylinder_stl()), 'Cylinder.stl')]})
        results['uploaded'] = [p['name'] for p in upload.get_json()['parts']]
        query = (f"documentId={ids['did']}&workspaceId={ids['wid']}&elementId={ids['eid']}"
                 '&server=http%3A%2F%2Flocalhost&theme=light')
        html = panel_pages(c, results, query)
        results['strip_in_print'] = '/testbed/panel.js' in html
        results['status'] = c.get('/onshape/status').get_json()
        context = {'documentId': ids['did'], 'workspaceId': ids['wid'], 'elementId': ids['eid']}
        headers = {'X-Print-Page': '0123456789ab'}
        answer = c.post('/print/onshape/parts', headers=headers, json={
            'via': 'selection', 'context': context,
            'selections': [{'partId': 'L1'}, {'partId': 'L2'}]}).get_json()
        results['selected'] = [(p['name'], p['triangles']) for p in answer['parts']]
        results['selected_errors'] = answer['errors']
        dialog = c.post('/print/onshape/parts', headers=headers, json={
            'via': 'dialog', 'context': context, 'selections': [{
                **context, 'idTag': 'L2', 'partName': 'Cylinder'}]}).get_json()
        results['dialog'] = [p['name'] for p in dialog['parts']]
        # The existing caps, through the existing code: a part past the triangle cap...
        big = c.post('/testbed/local/parts', content_type='multipart/form-data', data={
            'files': [(io.BytesIO(many_triangles_stl(limits.MAX_PART_TRIANGLES + 1)), 'Big.stl')]})
        big_id = big.get_json()['parts'][-1]['partId']
        refused = c.post('/print/onshape/parts', headers=headers, json={
            'via': 'selection', 'context': context, 'selections': [{'partId': big_id}]}).get_json()
        results['big_errors'] = [e['message'] for e in refused['errors']]
        # ...and more parts on one page than the part store allows.
        many = [(io.BytesIO(box_stl()), f'Box{i}.stl') for i in range(limits.MAX_PARTS_PER_PAGE)]
        listed = c.post('/testbed/local/parts', content_type='multipart/form-data',
                        data={'files': many}).get_json()['parts']
        crowd = c.post('/print/onshape/parts', headers={'X-Print-Page': 'ba9876543210'}, json={
            'via': 'selection', 'context': context,
            'selections': [{'partId': p['partId']} for p in listed if p['name'] != 'Big']}).get_json()
        results['crowd_errors'] = [e['message'] for e in crowd['errors']]


def check_off(app, results):
    with app.test_client() as c:
        with c.session_transaction() as cookie:      # a forged flag, as a crafted cookie
            cookie['testbed_apikey'] = True
        c.get('/onshape/status')
        results['client_is_none'] = session_manager.get_client(None) is None
        results['sign_in_status'] = c.get('/testbed/sign-in').status_code
        results['panel_js_status'] = c.get('/testbed/panel.js').status_code
        results['cnc_html'] = c.get(f'/onshape-panel?{PANEL_QUERY}').get_data(as_text=True)
        results['local_page_status'] = c.get('/testbed/local').status_code
        with c.session_transaction() as cookie:      # past the upload flow's gate, unsigned
            cookie['app_verified'] = True
        panel_pages(c, results)


def main():
    settings.CASSETTE_DIR = Path(sys.argv[1])
    app = frc_cam_gui_app.app
    app.config['TESTING'] = True
    results = {'mode': settings.testbed_mode()}
    check = {'replay': check_replay, 'local': check_local}.get(settings.testbed_mode(), check_off)
    check(app, results)
    print('TESTBED-RESULT ' + json.dumps(results))


if __name__ == '__main__':
    main()

"""Tests for testbed/flask_hooks.py and its changes to the app's sign-in plumbing.

Hook-level tests use a minimal Flask app with register_testbed_routes applied. Full-app
tests run flask_hooks_app.py in a fresh process per mode, since the app cannot be
imported twice in one process.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from flask import Flask

from testbed import adapters, flask_hooks, settings
from testbed.adapters import current_scenario
from testbed.scenarios import SCENARIOS
from testbed.scrub import STANDIN_EMAIL, STANDIN_NAME

REPO = Path(__file__).resolve().parents[2]
HELPER = Path(__file__).resolve().parent / 'flask_hooks_app.py'
FIXTURE_CASSETTES = Path(__file__).resolve().parent / 'fixtures' / 'cassettes'
# Variables that would change the app's mode, cookies or credentials in the subprocess.
_DROPPED = ('PENGUINCAM_TESTBED', 'ONSHAPE_CLIENT_ID', 'ONSHAPE_CLIENT_SECRET',
            'ONSHAPE_ACCESS_KEY', 'ONSHAPE_SECRET_KEY', 'FLASK_ENV', 'RAILWAY_ENVIRONMENT',
            'VERCEL', 'EMBED_COOKIES', 'PORT', 'FLASK_SECRET_KEY')
_full_app_results = {}


def full_app_results(mode):
    """The helper's results with PENGUINCAM_TESTBED=mode (None: unset), run once per mode."""
    if mode not in _full_app_results:
        with tempfile.TemporaryDirectory() as state:
            env = {k: v for k, v in os.environ.items() if k not in _DROPPED}
            env['XDG_STATE_HOME'] = state
            if mode:
                env['PENGUINCAM_TESTBED'] = mode
            run = subprocess.run([sys.executable, str(HELPER), str(FIXTURE_CASSETTES)],
                                 cwd=REPO, env=env, capture_output=True, text=True, timeout=180)
        lines = [line for line in run.stdout.splitlines() if line.startswith('TESTBED-RESULT ')]
        if run.returncode != 0 or not lines:
            raise AssertionError(f'helper failed ({run.returncode}): {run.stderr[-3000:]}')
        _full_app_results[mode] = json.loads(lines[-1].split(' ', 1)[1])
    return _full_app_results[mode]


class _MinimalAppCase(unittest.TestCase):
    mode = 'replay'

    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        env = {k: v for k, v in os.environ.items() if k not in _DROPPED}
        env['XDG_STATE_HOME'] = str(self.tmp / 'state')
        env['PENGUINCAM_TESTBED'] = self.mode
        self.enterContext(mock.patch.dict(os.environ, env, clear=True))
        settings.reset_testbed_settings_for_tests()
        self.addCleanup(settings.reset_testbed_settings_for_tests)
        adapters.reset_testbed_adapters_for_tests()
        self.addCleanup(adapters.reset_testbed_adapters_for_tests)
        flask_hooks.reset_message_log_for_tests()
        self.addCleanup(flask_hooks.reset_message_log_for_tests)
        self.enterContext(mock.patch.object(settings, 'FRESH_DIR', self.tmp / 'fresh'))
        self.app = Flask(__name__)
        self.app.secret_key = 'test'
        flask_hooks.register_testbed_routes(self.app)
        self.client = self.app.test_client()


class ReplayHooksTest(_MinimalAppCase):
    def test_sign_in_sets_flag_and_standin_user(self):
        response = self.client.get('/testbed/sign-in')
        self.assertEqual(response.status_code, 204)
        with self.client.session_transaction() as cookie:
            self.assertIs(cookie['testbed_apikey'], True)
            self.assertEqual(cookie['user_email'], STANDIN_EMAIL)
            self.assertEqual(set(cookie), {'testbed_apikey', 'user_email'})   # no keys

    def test_scenario_route_sets_current_scenario(self):
        listing = self.client.get('/testbed/scenarios').get_json()
        names = [s['name'] for s in listing['scenarios']]
        self.assertEqual(names, list(SCENARIOS))
        part_select = listing['scenarios'][names.index('part-select')]
        self.assertEqual(part_select['steps'][2],
                         {'name': 'ask for a part', 'instruction': 'Press Ask for a part.',
                          'action': 'ask-part', 'select': [], 'recorded_only': False})
        self.assertIsNone(listing['current'])

        response = self.client.post('/testbed/scenario', json={'name': 'part-select'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(current_scenario(), 'part-select')
        self.assertEqual(self.client.get('/testbed/scenarios').get_json()['current'],
                         'part-select')
        self.assertEqual(self.client.post('/testbed/scenario', json={'name': 'nope'}).status_code,
                         400)
        self.assertEqual(current_scenario(), 'part-select')

    def test_messages_appended_and_saved_on_done(self):
        self.assertEqual(self.client.post('/testbed/messages',
                                          json={'step': 'x', 'events': []}).status_code, 409)
        self.client.post('/testbed/scenario', json={'name': 'part-select'})
        event = {'dir': 'out', 'origin': 'https://cad.onshape.com', 't_ms': 1,
                 'data': {'messageName': 'requestSelection', 'note': 'Bearer abc123'}}
        for step, events in (('ask for a part', [event]), ('select part Cylinder', []),
                             ('ask for a part', [event])):
            response = self.client.post('/testbed/messages', json={'step': step, 'events': events})
            self.assertEqual(response.status_code, 204)
        self.assertEqual(self.client.post('/testbed/messages', json={'step': 'x'}).status_code, 400)
        with mock.patch.object(flask_hooks, 'finish_recording') as finish:
            done = self.client.post('/testbed/done')
        finish.assert_not_called()                      # replay records no cassette
        self.assertEqual(done.status_code, 200)
        saved = json.loads((self.tmp / 'fresh' / 'messages' / 'part-select.json').read_text())
        self.assertEqual(saved['scenario'], 'part-select')
        self.assertIs(saved['synthetic'], False)
        self.assertEqual([(s['step'], len(s['events'])) for s in saved['steps']],
                         [('ask for a part', 2), ('select part Cylinder', 0)])
        self.assertNotIn('abc123', json.dumps(saved))


class RecordHooksTest(_MinimalAppCase):
    mode = 'record'

    def test_sign_in_absent_in_record_mode(self):
        self.assertEqual(self.client.get('/testbed/sign-in').status_code, 404)
        with self.client.session_transaction() as cookie:
            self.assertNotIn('testbed_apikey', cookie)
        self.assertEqual(self.client.get('/testbed/scenarios').status_code, 200)

    def test_record_scenario_and_done_use_the_session_client(self):
        client = mock.Mock()
        with mock.patch.object(flask_hooks.session_manager, 'get_client', return_value=None):
            response = self.client.post('/testbed/scenario', json={'name': 'panel-load'})
        self.assertEqual(response.status_code, 401)
        with mock.patch.object(flask_hooks.session_manager, 'get_client', return_value=client), \
                mock.patch.object(flask_hooks, 'begin_recording') as begin:
            response = self.client.post('/testbed/scenario', json={'name': 'panel-load'})
        self.assertEqual(response.status_code, 200)
        begin.assert_called_once_with('panel-load', client)
        cassette = self.tmp / 'fresh' / 'cassette.json'
        with mock.patch.object(flask_hooks, 'finish_recording',
                               side_effect=RuntimeError('402')):
            failed = self.client.post('/testbed/done')
        self.assertEqual(failed.status_code, 500)
        self.assertIn('Choose the scenario again', failed.get_json()['error'])
        with mock.patch.object(flask_hooks, 'finish_recording', return_value=cassette) as finish:
            done = self.client.post('/testbed/done').get_json()     # the log was kept
        finish.assert_called_once_with()
        self.assertEqual(self.client.post('/testbed/done').status_code, 409)
        self.assertEqual(done['cassette'], str(cassette))
        self.assertTrue(Path(done['messages']).is_file())


class FullAppReplayTest(unittest.TestCase):
    def test_get_client_returns_apikey_client_after_sign_in(self):
        results = full_app_results('replay')
        self.assertEqual(results['sign_in_status'], 204)
        self.assertEqual(results['client_auth_mode'], 'apikey')
        self.assertTrue(results['client_cached'])

    def test_update_session_tokens_noop_for_apikey(self):
        results = full_app_results('replay')
        self.assertFalse(results['tokens_after_update'])
        self.assertFalse(results['tokens_in_cookie'])     # /onshape/status ran it too
        self.assertEqual(results['user_email'], STANDIN_EMAIL)

    def test_status_reports_apikey_client_connected(self):
        status = full_app_results('replay')['status']
        self.assertEqual(status, {'available': True, 'connected': True, 'user': STANDIN_NAME})

    def test_print_page_includes_script_when_on_and_cnc_page_does_not(self):
        results = full_app_results('replay')
        self.assertEqual(results['print_status'], 200)
        self.assertIn('<script src="/testbed/panel.js" data-mode="replay"></script>',
                      results['print_html'])
        self.assertEqual(results['cnc_status'], 200)
        self.assertNotIn('testbed', results['cnc_html'])
        self.assertEqual((results['panel_js_status'], results['panel_js_type']),
                         (200, 'text/javascript'))
        # The strip reads the Onshape context from the return URL, so all of it arrives.
        self.assertIn('elementId=e1', results['print_html'])
        self.assertIn('server=https', results['print_html'])

    def test_testbed_routes_not_rate_limited(self):
        results = full_app_results('replay')
        self.assertEqual(results['burst_statuses'], [200])          # 250 quick requests
        self.assertIn(429, results['status_burst_statuses'])        # the limiter is on


class FullAppOffTest(unittest.TestCase):
    def test_flag_cookie_ignored_when_off(self):
        results = full_app_results(None)
        self.assertIsNone(results['mode'])
        self.assertTrue(results['client_is_none'])
        self.assertEqual(results['sign_in_status'], 404)
        self.assertEqual(results['panel_js_status'], 404)
        self.assertNotIn('testbed', results['cnc_html'])


if __name__ == '__main__':
    unittest.main()

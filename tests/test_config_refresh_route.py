"""Flask test-client coverage for /config/refresh (the Onshape config reload glyph).

The button exists so a team that has been driving PenguinCAM on the built-in defaults can
say "go look again" the moment they add PenguinCAM-config.yaml to their classroom. It used
to be rendered ONLY once a config had already been found, so the one situation that needed
it most had no way to trigger it -- these tests pin the fix in place."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import frc_cam_gui_app as gui
from frc_cam_gui_app import app


class _FakeClient:
    """Stands in for an authenticated OnshapeClient. `yaml` None = nothing found."""
    def __init__(self, yaml=None, error=None, url=None):
        self._yaml, self.last_config_error, self.last_config_url = yaml, error, url

    def fetch_config_file(self):
        return self._yaml


TEAM_YAML = """
team:
  number: 4321
  name: "Test Robotics"
"""


class TestConfigRefreshButtonRendering(unittest.TestCase):
    """The glyph must render in BOTH header states, not just the config-loaded one."""

    def setUp(self):
        app.config['TESTING'] = True

    def _panel_html(self, team_config_data):
        client = app.test_client()
        with client.session_transaction() as sess:
            sess['team_config_data'] = team_config_data
        with mock.patch.object(gui, 'ONSHAPE_AVAILABLE', False):
            return client.get('/onshape-panel').get_data(as_text=True)

    def test_refresh_link_offered_while_on_default_config(self):
        html = self._panel_html({})
        self.assertIn('Using default configuration', html)
        self.assertIn('/config/refresh', html)

    def test_refresh_link_still_offered_with_a_config_loaded(self):
        html = self._panel_html({'team': {'number': 4321, 'name': 'Test Robotics'}})
        self.assertIn('/config/refresh', html)

    def test_upload_flow_has_no_onshape_refresh_link(self):
        """The upload flow gets its config from a URL box, not an Onshape search."""
        client = app.test_client()
        html = client.get('/app').get_data(as_text=True)
        self.assertNotIn('/config/refresh', html)


class TestConfigRefreshOutcome(unittest.TestCase):
    """A refresh that finds nothing must SAY so; a silent identical page reads as a dead
    button, which is what made teams think the feature was broken."""

    def setUp(self):
        app.config['TESTING'] = True
        self.client = app.test_client()

    def _refresh_with(self, client_obj):
        with mock.patch.object(gui, 'ONSHAPE_AVAILABLE', True), \
             mock.patch.object(gui.session_manager, 'get_client', return_value=client_obj), \
             mock.patch.object(gui.session_manager, 'update_session_tokens', return_value=None):
            self.client.get('/config/refresh')
        with self.client.session_transaction() as sess:
            return sess.get('config_refresh_result')

    def test_finds_a_config_and_says_which(self):
        result = self._refresh_with(_FakeClient(yaml=TEAM_YAML))
        self.assertTrue(result['ok'])
        self.assertIn('4321', result['message'])

    def test_reports_the_reason_when_nothing_is_found(self):
        reason = 'No document named PenguinCAM-config.yaml turned up in your classrooms.'
        result = self._refresh_with(_FakeClient(yaml=None, error=reason))
        self.assertFalse(result['ok'])
        self.assertIn('Still using defaults', result['message'])
        self.assertIn(reason, result['message'])

    def test_reports_a_generic_reason_when_none_was_captured(self):
        result = self._refresh_with(_FakeClient(yaml=None, error=None))
        self.assertFalse(result['ok'])
        self.assertIn('No PenguinCAM-config.yaml was found', result['message'])

    def test_unauthenticated_refresh_explains_itself(self):
        result = self._refresh_with(None)
        self.assertFalse(result['ok'])
        self.assertIn('not signed in', result['message'])

    def test_result_is_consumed_by_the_next_render(self):
        """One-shot: the message must not persist onto every later page load."""
        self._refresh_with(_FakeClient(yaml=None, error='nope'))
        with mock.patch.object(gui, 'ONSHAPE_AVAILABLE', False):
            html = self.client.get('/onshape-panel').get_data(as_text=True)
        self.assertIn('nope', html)
        with self.client.session_transaction() as sess:
            self.assertIsNone(sess.get('config_refresh_result'))


class TestDefaultConfigDetection(unittest.TestCase):
    def test_session_without_a_completed_lookup_reports_defaults(self):
        """`using_default_config` used to default to False when the key was absent, so a
        session that never completed a lookup claimed a real team config in the header."""
        app.config['TESTING'] = True
        client = app.test_client()
        with mock.patch.object(gui, 'ONSHAPE_AVAILABLE', False):
            html = client.get('/onshape-panel').get_data(as_text=True)
        self.assertIn('Using default configuration', html)


if __name__ == '__main__':
    unittest.main()

"""Tests for testbed/settings.py and the import guards in frc_cam_gui_app."""
import os
import subprocess
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from testbed import settings

REPO = Path(__file__).resolve().parents[2]        # the repository root
DEPLOYED_SIGNALS = {'FLASK_ENV': 'production', 'RAILWAY_ENVIRONMENT': 'production', 'VERCEL': '1'}
DEV_CLIENT_ID = 'devclientid0000'


def _clean_env(**extra):
    """The environment without any test bed, deployment or Onshape credential variable."""
    drop = {'PENGUINCAM_TESTBED', 'ONSHAPE_CLIENT_ID', 'ONSHAPE_CLIENT_SECRET', *DEPLOYED_SIGNALS}
    env = {k: v for k, v in os.environ.items() if k not in drop}
    env.update(extra)
    return env


class TestbedSettingsTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, _clean_env(), clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        settings.reset_testbed_settings_for_tests()
        self.addCleanup(settings.reset_testbed_settings_for_tests)

    def test_off_by_default(self):
        self.assertIsNone(settings.testbed_mode())
        self.assertFalse(settings.testbed_is_on())
        settings.assert_testbed_allowed()          # nothing to refuse

    def test_modes_are_read_once(self):
        os.environ['PENGUINCAM_TESTBED'] = 'replay'
        self.assertEqual(settings.testbed_mode(), 'replay')
        self.assertTrue(settings.testbed_is_on())
        os.environ['PENGUINCAM_TESTBED'] = 'record'
        self.assertEqual(settings.testbed_mode(), 'replay')     # cached
        settings.reset_testbed_settings_for_tests()
        self.assertEqual(settings.testbed_mode(), 'record')

    def test_bad_value_raises_naming_variable(self):
        os.environ['PENGUINCAM_TESTBED'] = 'yes'
        with self.assertRaises(ValueError) as caught:
            settings.testbed_mode()
        self.assertIn('PENGUINCAM_TESTBED', str(caught.exception))

    def test_refuses_each_deployed_signal(self):
        for name, value in DEPLOYED_SIGNALS.items():
            with self.subTest(signal=name):
                environ = _clean_env(PENGUINCAM_TESTBED='replay', **{name: value})
                with self.assertRaises(RuntimeError) as caught:
                    settings.assert_testbed_allowed(environ)
                self.assertEqual(str(caught.exception),
                                 'PENGUINCAM_TESTBED must not be set on a deployed server')

    def test_deployed_signal_alone_is_fine(self):
        for name, value in DEPLOYED_SIGNALS.items():
            with self.subTest(signal=name):
                settings.assert_testbed_allowed(_clean_env(**{name: value}))

    def test_flask_env_development_is_not_deployed(self):
        settings.assert_testbed_allowed(_clean_env(PENGUINCAM_TESTBED='replay',
                                                   FLASK_ENV='development'))

    def test_record_mode_needs_dev_app_credentials(self):
        cases = {
            'ONSHAPE_CLIENT_ID': _clean_env(PENGUINCAM_TESTBED='record',
                                            ONSHAPE_CLIENT_SECRET='s'),
            'ONSHAPE_CLIENT_SECRET': _clean_env(PENGUINCAM_TESTBED='record',
                                                ONSHAPE_CLIENT_ID=DEV_CLIENT_ID),
        }
        for missing, environ in cases.items():
            with self.subTest(missing=missing):
                with self.assertRaises(RuntimeError) as caught:
                    settings.assert_testbed_allowed(environ)
                self.assertEqual(str(caught.exception), f'record mode needs {missing}')
        settings.assert_testbed_allowed(_clean_env(
            PENGUINCAM_TESTBED='record', ONSHAPE_CLIENT_ID=DEV_CLIENT_ID,
            ONSHAPE_CLIENT_SECRET='s'))

    def test_replay_mode_needs_no_credentials(self):
        settings.assert_testbed_allowed(_clean_env(PENGUINCAM_TESTBED='replay'))

    def test_local_mode_needs_no_credentials_and_is_refused_when_deployed(self):
        os.environ['PENGUINCAM_TESTBED'] = 'local'
        self.assertEqual(settings.testbed_mode(), 'local')
        settings.assert_testbed_allowed(_clean_env(PENGUINCAM_TESTBED='local'))
        for name, value in DEPLOYED_SIGNALS.items():
            with self.subTest(signal=name):
                with self.assertRaises(RuntimeError):
                    settings.assert_testbed_allowed(
                        _clean_env(PENGUINCAM_TESTBED='local', **{name: value}))

    def test_local_mode_uses_the_forwarded_port(self):
        self.assertEqual(settings.LOCAL_PORT, 6238)
        self.assertEqual(settings.LOCAL_PORT, settings.RECORD_PORT)
        self.assertNotIn(settings.LOCAL_TEST_PORT, (settings.LOCAL_PORT, settings.REPLAY_PORT))

    def test_record_mode_refuses_production_client_id(self):
        environ = _clean_env(PENGUINCAM_TESTBED='record', ONSHAPE_CLIENT_ID='VKDKexample',
                             ONSHAPE_CLIENT_SECRET='s')
        with self.assertRaises(RuntimeError) as caught:
            settings.assert_testbed_allowed(environ)
        self.assertEqual(str(caught.exception), 'record mode refuses the production Onshape app')

    def test_bad_value_refused_by_guard(self):
        with self.assertRaises(ValueError):
            settings.assert_testbed_allowed(_clean_env(PENGUINCAM_TESTBED='on'))

    def test_constants(self):
        self.assertEqual(settings.RECORD_PORT, 6238)
        self.assertEqual(settings.REPLAY_PORT, 6240)
        self.assertEqual(settings.FAKE_HOST_ORIGIN, 'https://cad-testbed.onshape.com')
        self.assertEqual(settings.TESTBED_DIR, REPO / 'testbed')
        self.assertEqual(settings.FRESH_DIR, REPO / 'testbed' / '.fresh')
        for path in (settings.CASSETTE_DIR, settings.MESSAGE_DIR, settings.FIXTURE_DIR):
            self.assertEqual(path.parent, settings.TESTBED_DIR)

    def test_cassette_and_message_dirs_from_environment(self):
        # Read when settings is imported, so a fresh process sees them.
        code = ('from testbed import settings; '
                'print(settings.CASSETTE_DIR); print(settings.MESSAGE_DIR)')
        run = subprocess.run([sys.executable, '-c', code], cwd=REPO, capture_output=True,
                             text=True, env=_clean_env(TESTBED_CASSETTE_DIR='/x/cassettes',
                                                       TESTBED_MESSAGE_DIR='/x/messages'))
        self.assertEqual(run.stdout.split(), ['/x/cassettes', '/x/messages'], run.stderr)

    def test_state_dir_follows_xdg_and_is_created(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            os.environ['XDG_STATE_HOME'] = tmp
            path = settings.state_dir()
            self.assertEqual(path, Path(tmp) / 'penguincam-testbed')
            self.assertTrue(path.is_dir())

    def test_state_dir_default(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            os.environ.pop('XDG_STATE_HOME', None)
            os.environ['HOME'] = tmp
            self.assertEqual(settings.state_dir(),
                             Path(tmp) / '.local' / 'state' / 'penguincam-testbed')

    def test_budget_defaults(self):
        for name in ('TESTBED_BUDGET', 'TESTBED_CYCLE_START', 'TESTBED_RUN_CAP'):
            os.environ.pop(name, None)
        self.assertEqual(settings.budget_settings(), (250, None, 150))

    def test_budget_from_environment(self):
        os.environ.update(TESTBED_BUDGET='300', TESTBED_CYCLE_START='2026-03-01',
                          TESTBED_RUN_CAP='40')
        self.assertEqual(settings.budget_settings(), (300, date(2026, 3, 1), 40))

    def test_bad_budget_names_variable(self):
        for name, value in (('TESTBED_BUDGET', 'lots'), ('TESTBED_CYCLE_START', 'March'),
                            ('TESTBED_RUN_CAP', '1.5')):
            with self.subTest(variable=name):
                with mock.patch.dict(os.environ, {name: value}):
                    with self.assertRaises(ValueError) as caught:
                        settings.budget_settings()
                    self.assertIn(name, str(caught.exception))

    def test_settings_import_only_the_standard_library(self):
        code = ('import sys; before = set(sys.modules); import testbed.settings; '
                'new = {m.split(".")[0] for m in set(sys.modules) - before}; '
                'stdlib = sys.stdlib_module_names; '
                'print(sorted(m for m in new if m not in stdlib and m != "testbed"))')
        result = subprocess.run([sys.executable, '-c', code], cwd=REPO, env=_clean_env(),
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), '[]')


class AppImportGuardTest(unittest.TestCase):
    """Each case imports the app in a fresh process, as gunicorn would."""

    def _import_app(self, env, code='import frc_cam_gui_app'):
        return subprocess.run([sys.executable, '-c', code], cwd=REPO, env=env,
                              capture_output=True, text=True, timeout=120)

    def test_import_refuses_when_deployed(self):
        result = self._import_app(_clean_env(PENGUINCAM_TESTBED='replay', RAILWAY_ENVIRONMENT='x'))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('PENGUINCAM_TESTBED must not be set on a deployed server', result.stderr)

    def test_import_refuses_record_with_production_client_id(self):
        result = self._import_app(_clean_env(PENGUINCAM_TESTBED='record',
                                             ONSHAPE_CLIENT_ID='VKDKexample',
                                             ONSHAPE_CLIENT_SECRET='s'))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('record mode refuses the production Onshape app', result.stderr)

    def test_app_without_testbed_has_no_testbed_routes(self):
        code = ('import frc_cam_gui_app; '
                'print("\\n".join(r.rule for r in frc_cam_gui_app.app.url_map.iter_rules()))')
        result = self._import_app(_clean_env(), code)
        self.assertEqual(result.returncode, 0, result.stderr)
        rules = result.stdout.split()
        self.assertIn('/', rules)                   # the listing really is the route table
        self.assertEqual([r for r in rules if r.startswith('/testbed')], [])


if __name__ == '__main__':
    unittest.main()

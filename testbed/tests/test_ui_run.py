"""Tests for testbed/ui_run.py that need no browser: credentials, bot checks and sign-in.

The pages here are stubs built from the HTML fixtures; browser_chrome.py runs the same
fixtures through a real browser.
"""
import contextlib
import io
import json
import logging
import os
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest import mock

from testbed import settings, ui_run

HTML = Path(__file__).resolve().parent / 'fixtures' / 'html'
BOT_CHECK_PAGES = ('recaptcha.html', 'hcaptcha.html', 'cloudflare.html', 'verify-human.html',
                   'unusual-activity.html', 'verify-email.html', 'security-check.html')
PLAIN_PAGES = ('signin.html', 'documents.html')
CREDENTIALS = {'ONSHAPE_USERNAME': 'testbed@example.invalid',
               'ONSHAPE_PASSWORD': 'pw-sentinel-7f3a9c',
               'ONSHAPE_CLIENT_ID': 'devclientid0000',
               'ONSHAPE_CLIENT_SECRET': 'devclientsecret0000'}


class _Parsed(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.iframes, self.text, self._skip = [], [], 0
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag == 'iframe':
            self.iframes.append(dict(attrs).get('src') or '')
        if tag in ('script', 'style', 'title'):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'title'):
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.text.append(data)


class StubLocator:
    def __init__(self, page, selector):
        self.page, self.selector = page, selector
        self.first = self

    def _maybe_fail(self, what, value=''):
        if self.selector in self.page.fail:
            # Playwright's call log carries the value being filled, as the real error does.
            raise RuntimeError(f'Locator.{what}: Timeout 30000ms exceeded.\nCall log:\n'
                               f'  - {what}("{value}")')

    def fill(self, value, **kwargs):
        self._maybe_fail('fill', value)
        self.page.actions.append(('fill', self.selector, value))

    def click(self, **kwargs):
        self._maybe_fail('click')
        self.page.actions.append(('click', self.selector))
        if self.selector == self.page.navigates_on:
            self.page.navigating = True

    def check(self, **kwargs):
        self.click()

    def select_option(self, value, **kwargs):
        self._maybe_fail('select_option', value)
        self.page.actions.append(('select', self.selector, value))

    def count(self):
        self._maybe_fail('count')
        return self.page.counts.get(self.selector, 1)

    def wait_for(self, **kwargs):
        self._maybe_fail('wait_for')

    def get_attribute(self, name):
        return self.page.attributes.get((self.selector, name))

    def input_value(self, **kwargs):
        self._maybe_fail('input_value')
        return self.page.values.get(self.selector, '')


class StubFrame:
    def __init__(self, page):
        self.page = page

    def locator(self, selector):
        return StubLocator(self.page, 'panel ' + selector)


class StubPage:
    """Just enough of a Playwright page for the UI run's steps, with no browser.

    `fail` holds the selectors (or 'goto') whose action raises; `signed_in` decides where
    /documents lands.
    """

    def __init__(self, html='', fail=(), signed_in=False):
        self.parsed = _Parsed(html)
        self.actions = []
        self.fail = set(fail)
        self.signed_in = signed_in
        self.counts, self.attributes = {}, {}
        # input_value() answers; the length unit reads inches unless a test says otherwise.
        self.values = {ui_run.SELECTORS['units_length']: 'Inch'}
        # Clicking this selector starts a navigation: page scripts fail until it ends.
        self.navigates_on, self.navigating = None, False
        self.url = 'https://cad.onshape.com/signin'
        self.keyboard = mock.Mock()

    def goto(self, url, **kwargs):
        if 'goto' in self.fail:
            raise RuntimeError(f'Page.goto: net::ERR_TIMED_OUT at {url}')
        self.actions.append(('goto', url))
        self.url = url if self.signed_in or not url.endswith('/documents') else ui_run.SIGN_IN_URL

    def locator(self, selector):
        return StubLocator(self, selector)

    def frame_locator(self, selector):
        return StubFrame(self)

    def eval_on_selector_all(self, selector, expression):
        assert selector == 'iframe', selector
        if self.navigating:
            raise RuntimeError('Page.eval_on_selector_all: Execution context was destroyed, '
                               'most likely because of a navigation')
        return list(self.parsed.iframes)

    def inner_text(self, selector, **kwargs):
        assert selector == 'body', selector
        return ' '.join(self.parsed.text)

    def wait_for_selector(self, selector, **kwargs):
        StubLocator(self, selector).wait_for()
        self.actions.append(('wait', selector))

    def is_visible(self, selector, **kwargs):
        if selector == ui_run.SELECTORS['email']:
            return self.url.startswith(ui_run.SIGN_IN_URL)
        return True

    def fill(self, selector, value, **kwargs):
        StubLocator(self, selector).fill(value)

    def click(self, selector, **kwargs):
        StubLocator(self, selector).click()

    def wait_for_load_state(self, *args, **kwargs):
        pass

    def wait_for_timeout(self, ms):
        pass

    def set_default_timeout(self, ms):
        pass

    def screenshot(self, path=None, **kwargs):
        Path(path).write_bytes(b'png')


class StubContext:
    def __init__(self, page):
        self.pages = [page]

    def grant_permissions(self, *args, **kwargs):
        pass

    def close(self):
        pass


class StubServer:
    log_path = Path('/nonexistent/record-devserver.log')

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _page(name):
    return StubPage((HTML / name).read_text())


class CredentialsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(mock.patch.dict(os.environ, {'XDG_STATE_HOME': self.tmp}, clear=True))
        # Nothing may get as far as a server or a browser.
        self.enterContext(mock.patch.object(ui_run, 'launch_persistent_chrome',
                                            side_effect=AssertionError('browser launched')))
        self.enterContext(mock.patch.object(ui_run, 'RecordDevServer',
                                            side_effect=AssertionError('server started')))

    def test_missing_credentials_named(self):
        for missing in CREDENTIALS:
            with self.subTest(missing=missing):
                env = {k: v for k, v in CREDENTIALS.items() if k != missing}
                with mock.patch.dict(os.environ, env):
                    with self.assertRaises(settings.MissingEnvironmentVariable) as caught:
                        ui_run.run_onshape_ui(['panel-load'])
                self.assertEqual(caught.exception.name, missing)
                self.assertNotIn(CREDENTIALS['ONSHAPE_PASSWORD'], str(caught.exception))

    def test_production_client_id_refused(self):
        with mock.patch.dict(os.environ, dict(CREDENTIALS, ONSHAPE_CLIENT_ID='VKDKprod000')):
            with self.assertRaises(RuntimeError) as caught:
                ui_run.run_onshape_ui(['panel-load'])
        self.assertIn('production', str(caught.exception))
        self.assertNotIn('VKDKprod000', str(caught.exception))


class BotCheckTest(unittest.TestCase):
    def test_detect_bot_check_patterns(self):
        for name in BOT_CHECK_PAGES:
            with self.subTest(page=name):
                self.assertTrue(ui_run.detect_onshape_bot_check(_page(name)))
        for name in PLAIN_PAGES:
            with self.subTest(page=name):
                self.assertIsNone(ui_run.detect_onshape_bot_check(_page(name)))

    def test_reason_names_what_was_seen(self):
        self.assertIn('challenges.cloudflare.com',
                      ui_run.detect_onshape_bot_check(_page('cloudflare.html')))
        self.assertIn("verify you're a human",
                      ui_run.detect_onshape_bot_check(_page('verify-human.html')).lower())

    def test_bot_check_stops_with_screenshot(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {'XDG_STATE_HOME': tmp}):
            with self.assertRaises(ui_run.OnshapeCheckStopped) as caught:
                ui_run.stop_on_onshape_check(_page('recaptcha.html'), 'sign in')
            shots = list((settings.state_dir() / 'screenshots').glob('*.png'))
        self.assertEqual(len(shots), 1)
        self.assertIn('recaptcha', str(caught.exception))
        self.assertIn(str(shots[0]), str(caught.exception))


class SignInTest(unittest.TestCase):
    def test_password_never_logged(self):
        page = _page('signin.html')
        out, err = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, dict(CREDENTIALS, XDG_STATE_HOME=tmp)), \
                self.assertLogs(level=logging.DEBUG) as logs, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            logging.getLogger().debug('captured')     # assertLogs needs one record
            ui_run.sign_in_to_onshape(page)
            files = [p.read_bytes() for p in Path(tmp).rglob('*') if p.is_file()]
        password = CREDENTIALS['ONSHAPE_PASSWORD']
        self.assertIn(('fill', ui_run.SELECTORS['password'], password), page.actions)
        self.assertIn(('fill', ui_run.SELECTORS['email'], CREDENTIALS['ONSHAPE_USERNAME']),
                      page.actions)
        self.assertIn(('click', ui_run.SELECTORS['sign_in_submit']), page.actions)
        captured = out.getvalue() + err.getvalue() + '\n'.join(logs.output)
        self.assertNotIn(password, captured)
        for data in files:
            self.assertNotIn(password.encode(), data)

    def test_failure_after_submit_names_the_step(self):
        page = _page('signin.html')
        page.navigates_on = ui_run.SELECTORS['sign_in_submit']
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, dict(CREDENTIALS, XDG_STATE_HOME=tmp)), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(ui_run.OnshapeStepFailed) as caught:
                ui_run.sign_in_to_onshape(page)
            shots = list((settings.state_dir() / 'screenshots').glob('*.png'))
        self.assertIn("step 'sign-in: after submit' failed", str(caught.exception))
        self.assertIn('Execution context was destroyed', str(caught.exception))
        self.assertEqual(len(shots), 1)
        self.assertIn(str(shots[0]), str(caught.exception))

    def test_selectors_are_one_table(self):
        for key in ('email', 'password', 'sign_in_submit', 'panel_frame'):
            self.assertIn(key, ui_run.SELECTORS)


class UiRunEndToEndTest(unittest.TestCase):
    """main() through run_onshape_ui with stub pages: what the owner would see."""

    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        env = dict(CREDENTIALS, XDG_STATE_HOME=str(self.tmp / 'state'))
        env.update({k: v for k, v in os.environ.items() if k in ('PATH', 'HOME')})
        self.enterContext(mock.patch.dict(os.environ, env, clear=True))
        documents = {name: {'did': f'd{i}', 'wid': f'w{i}', 'eid': f'e{i}', 'parts': {}}
                     for i, name in enumerate(('tb-box', 'tb-inch'), start=1)}
        (self.tmp / 'documents.json').write_text(json.dumps(documents))
        for name, sub in (('FRESH_DIR', 'fresh'), ('CASSETTE_DIR', 'cassettes'),
                          ('MESSAGE_DIR', 'messages'), ('DOCUMENTS_FILE', 'documents.json')):
            self.enterContext(mock.patch.object(settings, name, self.tmp / sub))
        self.enterContext(mock.patch.object(ui_run, 'RecordDevServer', StubServer))
        self.enterContext(mock.patch('playwright.sync_api.sync_playwright',
                                     lambda: contextlib.nullcontext(object())))

    def run_main(self, page):
        from testbed.__main__ import main
        self.enterContext(mock.patch.object(ui_run, 'launch_persistent_chrome',
                                            lambda playwright, headed=True: (StubContext(page),
                                                                             'stub')))
        out, err = io.StringIO(), io.StringIO()
        with self.assertLogs(level=logging.DEBUG) as logs, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            logging.getLogger().debug('captured')
            code = main(['record', '--ui', 'panel-load'])
        return code, out.getvalue(), err.getvalue() + '\n'.join(logs.output)

    def screenshots(self):
        return sorted((settings.state_dir() / 'screenshots').glob('*.png'))

    def test_failed_password_fill_never_leaks_the_password(self):
        password = CREDENTIALS['ONSHAPE_PASSWORD']
        page = StubPage(fail={ui_run.SELECTORS['password']})
        code, out, err = self.run_main(page)
        self.assertEqual(code, 1)
        self.assertIn("step 'sign-in: password' failed", err)
        self.assertIn('could not be filled', err)
        self.assertEqual(len(self.screenshots()), 1)
        for text in (out, err, *(p.name for p in self.screenshots())):
            self.assertNotIn(password, text)

    def test_any_error_text_is_redacted(self):
        password = CREDENTIALS['ONSHAPE_PASSWORD']
        page = StubPage()
        self.enterContext(mock.patch.object(ui_run, 'launch_persistent_chrome',
                                            lambda playwright, headed=True: (StubContext(page),
                                                                             'stub')))
        leaky = RuntimeError(f'boom with {password}')
        with mock.patch.object(ui_run, '_ensure_signed_in', side_effect=leaky):
            with self.assertRaises(ui_run.OnshapeStepFailed) as caught:
                ui_run.run_onshape_ui(['panel-load'])
        self.assertNotIn(password, str(caught.exception))
        self.assertIsNone(caught.exception.__context__)
        self.assertIn('***', str(caught.exception))
        self.assertEqual(ui_run.redact_password(f'x {password} y'), 'x *** y')
        with mock.patch.dict(os.environ, {'ONSHAPE_PASSWORD': 'a b&c"d'}):
            self.assertEqual(ui_run.redact_password('1 a b&c"d 2 a%20b%26c%22d 3 a+b%26c%22d '
                                                    '4 a b&c\\"d'), '1 *** 2 *** 3 *** 4 ***')

    def test_documents_page_failure_stops_with_screenshot(self):
        code, out, err = self.run_main(StubPage(fail={'goto'}))
        self.assertEqual(code, 1)
        self.assertIn("step 'documents page' failed: Page.goto: net::ERR_TIMED_OUT", err)
        self.assertNotIn('Traceback', err)
        self.assertEqual(len(self.screenshots()), 1)

    def test_bot_check_after_sign_in_exits_3(self):
        page = StubPage((HTML / 'verify-human.html').read_text())
        code, out, err = self.run_main(page)
        self.assertEqual(code, 3)
        self.assertIn('stopped by an Onshape check', err)
        self.assertEqual(len(self.screenshots()), 1)

    def test_tb_inch_units_failure_tells_owner_to_do_it_by_hand(self):
        page = StubPage(signed_in=True, fail={ui_run.SELECTORS['workspace_units']})
        code, out, err = self.run_main(page)
        self.assertEqual(code, 1)
        self.assertIn("step 'tb-inch units' failed", err)
        self.assertIn('by hand', err)
        self.assertIn(str(ui_run.tb_inch_units_file()), err)
        self.assertEqual(len(self.screenshots()), 1)

    def test_panel_step_failure_names_step_and_records_nothing_else(self):
        ui_run.tb_inch_units_file().write_text('inch\n')
        page = StubPage(signed_in=True, fail={ui_run.SELECTORS['panel_icon']})
        page.counts[ui_run.SELECTORS['panel_frame']] = 0
        code, out, err = self.run_main(page)
        self.assertEqual(code, 1)
        self.assertIn("step 'panel-load: open panel' failed", err)
        self.assertEqual(len(self.screenshots()), 1)
        self.assertNotIn(('goto', 'https://cad.onshape.com/documents/d2/w/w2/e/e2'),
                         page.actions)          # the units step was skipped: marker present

    def test_strip_failure_names_step(self):
        ui_run.tb_inch_units_file().write_text('inch\n')
        page = StubPage(signed_in=True, fail={'panel ' + ui_run.SELECTORS['strip_scenario']})
        page.attributes[(ui_run.SELECTORS['panel_frame'], 'src')] = \
            'http://localhost:6238/onshape-panel?documentId=d1'
        page.counts['panel ' + ui_run.SELECTORS['panel_connect']] = 0
        code, out, err = self.run_main(page)
        self.assertEqual(code, 1)
        self.assertIn("step 'panel-load: choose print' failed", err)
        # The panel URL was recorded and is in the run's report.
        self.assertEqual(ui_run.panel_url_file().read_text().strip(),
                         'http://localhost:6238/onshape-panel')
        self.assertIn('panel URL the development app opens: '
                      'http://localhost:6238/onshape-panel', out)

    def test_tb_inch_units_switched_once(self):
        page = StubPage(signed_in=True)
        self.assertTrue(ui_run.switch_tb_inch_units(page, {'did': 'd2', 'wid': 'w2',
                                                           'eid': 'e2'}))
        self.assertIn(('click', ui_run.SELECTORS['units_ok']), page.actions)
        self.assertTrue(ui_run.tb_inch_units_file().is_file())
        self.assertFalse(ui_run.switch_tb_inch_units(page, {'did': 'd2', 'wid': 'w2',
                                                            'eid': 'e2'}))


    def test_tb_inch_marker_only_after_the_units_read_inches(self):
        page = StubPage(signed_in=True)
        page.values[ui_run.SELECTORS['units_length']] = 'Millimeter'
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(ui_run.OnshapeStepFailed) as caught:
                ui_run.switch_tb_inch_units(page, {'did': 'd2', 'wid': 'w2', 'eid': 'e2'})
        self.assertIn("step 'tb-inch units: check' failed", str(caught.exception))
        self.assertIn('Millimeter', str(caught.exception))
        self.assertIn('by hand', str(caught.exception))
        self.assertFalse(ui_run.tb_inch_units_file().exists())
        self.assertEqual(len(self.screenshots()), 1)

    def test_pending_documents_refused_before_the_browser(self):
        documents = json.loads(settings.DOCUMENTS_FILE.read_text())
        documents['tb-inch'] = {'did': 'd9', 'wid': 'w9', 'parts': {},
                                'pending': 'not in the test folder'}
        settings.DOCUMENTS_FILE.write_text(json.dumps(documents))
        page = StubPage(signed_in=True)
        code, out, err = self.run_main(page)
        self.assertEqual(code, 1)
        self.assertIn('tb-inch (not in the test folder)', err)
        self.assertIn('build-docs', err)
        self.assertNotIn("'eid'", err)
        self.assertEqual(page.actions, [])
        self.assertEqual(self.screenshots(), [])

    def test_stale_panel_url_is_not_reported(self):
        ui_run.panel_url_file().write_text('http://localhost:6238/stale-panel\n')
        code, out, err = self.run_main(StubPage(fail={'goto'}))
        self.assertEqual(code, 1)
        self.assertNotIn('stale-panel', out)
        self.assertFalse(ui_run.panel_url_file().exists())


class RedactionTest(unittest.TestCase):
    def test_stripped_password_redacted_too(self):
        # required_environment() strips the value, so the stripped form is what is filled.
        with mock.patch.dict(os.environ, {'ONSHAPE_PASSWORD': ' pw-sentinel-x&y \n'}):
            self.assertEqual(ui_run.redact_password('a pw-sentinel-x&y b'), 'a *** b')
            self.assertEqual(ui_run.redact_password('a pw-sentinel-x%26y b'), 'a *** b')
            self.assertEqual(ui_run.redact_password('a  pw-sentinel-x&y \n b'), 'a *** b')


class RecordServerEnvironmentTest(unittest.TestCase):
    def test_record_server_gets_no_onshape_sign_in_credentials(self):
        from testbed import browser
        popen = mock.Mock()
        popen.return_value.poll.return_value = None
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, dict(CREDENTIALS, XDG_STATE_HOME=tmp,
                                                 PATH=os.environ.get('PATH', '')),
                                clear=True), \
                mock.patch.object(browser.subprocess, 'Popen', popen), \
                mock.patch.object(browser, '_answers', side_effect=[None, 200]), \
                mock.patch.object(browser.os, 'killpg'):
            with browser.RecordDevServer():
                pass
        env = popen.call_args.kwargs['env']
        # Compared as names only, so a failure never prints the environment's values.
        self.assertEqual({'ONSHAPE_USERNAME', 'ONSHAPE_PASSWORD'} & set(env), set())
        self.assertEqual(env['ONSHAPE_CLIENT_ID'], CREDENTIALS['ONSHAPE_CLIENT_ID'])
        self.assertEqual(env['PENGUINCAM_TESTBED'], 'record')


class PasswordSavingTest(unittest.TestCase):
    def test_profile_never_offers_to_save_passwords(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp)
            (profile / 'Default').mkdir()
            (profile / 'Default' / 'Preferences').write_text(json.dumps(
                {'profile': {'name': 'Person 1', 'password_manager_enabled': True},
                 'browser': {'has_seen_welcome_page': True}}))
            ui_run.disable_password_saving(profile)
            prefs = json.loads((profile / 'Default' / 'Preferences').read_text())
        self.assertIs(prefs['credentials_enable_service'], False)
        self.assertIs(prefs['profile']['password_manager_enabled'], False)
        self.assertEqual(prefs['profile']['name'], 'Person 1')
        self.assertTrue(prefs['browser']['has_seen_welcome_page'])

    def test_new_profile_gets_the_preferences_before_launch(self):
        seen = {}

        def launch(user_data_dir, **kwargs):
            seen.update(json.loads((Path(user_data_dir) / 'Default' / 'Preferences')
                                   .read_text()))
            return mock.Mock()

        playwright = mock.Mock()
        playwright.chromium.launch_persistent_context.side_effect = launch
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {'XDG_STATE_HOME': tmp}), \
                mock.patch.object(ui_run, 'chrome_executable', return_value=None), \
                contextlib.redirect_stderr(io.StringIO()):
            ui_run.launch_persistent_chrome(playwright)
        self.assertIs(seen['credentials_enable_service'], False)
        self.assertIs(seen['profile']['password_manager_enabled'], False)


class ExecutableTest(unittest.TestCase):
    def test_chrome_executable_none_when_not_installed(self):
        with mock.patch.object(ui_run, 'CHROME_BINARY', Path('/nonexistent/chrome')):
            self.assertIsNone(ui_run.chrome_executable())


if __name__ == '__main__':
    unittest.main()

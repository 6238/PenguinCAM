"""Tests for testbed/ui_run.py that need no browser: credentials, bot checks and sign-in.

The pages here are stubs built from the HTML fixtures; browser_chrome.py runs the same
fixtures through a real browser.
"""
import contextlib
import io
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


class StubPage:
    """Just enough of a Playwright page for detect_onshape_bot_check and sign-in."""

    def __init__(self, html):
        self.parsed = _Parsed(html)
        self.actions = []
        self.url = 'https://cad.onshape.com/signin'

    def eval_on_selector_all(self, selector, expression):
        assert selector == 'iframe', selector
        return list(self.parsed.iframes)

    def inner_text(self, selector, **kwargs):
        assert selector == 'body', selector
        return ' '.join(self.parsed.text)

    def wait_for_selector(self, selector, **kwargs):
        self.actions.append(('wait', selector))

    def is_visible(self, selector, **kwargs):
        return True

    def fill(self, selector, value, **kwargs):
        self.actions.append(('fill', selector, value))

    def click(self, selector, **kwargs):
        self.actions.append(('click', selector))

    def wait_for_load_state(self, *args, **kwargs):
        pass

    def wait_for_timeout(self, ms):
        pass

    def screenshot(self, path=None, **kwargs):
        Path(path).write_bytes(b'png')


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

    def test_selectors_are_one_table(self):
        for key in ('email', 'password', 'sign_in_submit', 'panel_frame'):
            self.assertIn(key, ui_run.SELECTORS)


class ExecutableTest(unittest.TestCase):
    def test_chrome_executable_none_when_not_installed(self):
        with mock.patch.object(ui_run, 'CHROME_BINARY', Path('/nonexistent/chrome')):
            self.assertIsNone(ui_run.chrome_executable())


if __name__ == '__main__':
    unittest.main()

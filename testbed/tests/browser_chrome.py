"""Browser tests of the automated Onshape UI run's browser (spec 5.9); headed, under Xvfb.

  xvfb-run -a uv run python -m unittest testbed.tests.browser_chrome -v

The first test launches Google Chrome, or the /usr/bin/chromium fallback, on a throwaway
profile with every request refused, and runs the bot check detector over the HTML
fixtures in a real page. The second runs only with TESTBED_ONLINE=1: it opens Onshape's
sign-in page on the kept profile and waits for the email field. It never fills or submits
the form.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from testbed import settings, ui_run

HTML = Path(__file__).resolve().parent / 'fixtures' / 'html'
BOT_CHECK_PAGES = ('recaptcha.html', 'hcaptcha.html', 'cloudflare.html', 'verify-human.html',
                   'unusual-activity.html', 'verify-email.html', 'security-check.html')
PLAIN_PAGES = ('signin.html', 'documents.html')


def _needs_display(test):
    if not os.environ.get('DISPLAY'):
        test.skipTest('needs a display: run under xvfb-run -a')


def _which(executable):
    return 'Google Chrome' if executable == str(ui_run.CHROME_BINARY) else 'Chromium fallback'


class ChromeTest(unittest.TestCase):
    def test_chrome_or_fallback_starts_under_xvfb(self):
        _needs_display(self)
        from playwright.sync_api import sync_playwright
        with tempfile.TemporaryDirectory() as state, \
                mock.patch.dict(os.environ, {'XDG_STATE_HOME': state}), \
                sync_playwright() as playwright:
            context, executable = ui_run.launch_persistent_chrome(playwright, headed=True)
            try:
                self.assertTrue((settings.state_dir() / 'chrome-profile').is_dir())
                print(f'\nbrowser_chrome: {_which(executable)} ran: {executable} '
                      f'{context.browser.version if context.browser else ""}',
                      file=sys.stderr)
                # Nothing leaves the machine: every fixture's frames are refused.
                context.route('**/*', lambda route: route.abort('blockedbyclient'))
                page = context.new_page()
                for name in BOT_CHECK_PAGES + PLAIN_PAGES:
                    with self.subTest(page=name):
                        page.set_content((HTML / name).read_text())
                        reason = ui_run.detect_onshape_bot_check(page)
                        if name in BOT_CHECK_PAGES:
                            self.assertTrue(reason, name)
                        else:
                            self.assertIsNone(reason, name)
            finally:
                context.close()

    def test_chromium_fallback_when_chrome_is_missing(self):
        _needs_display(self)
        from playwright.sync_api import sync_playwright
        with tempfile.TemporaryDirectory() as state, \
                mock.patch.dict(os.environ, {'XDG_STATE_HOME': state}), \
                mock.patch.object(ui_run, 'CHROME_BINARY', Path(state) / 'no-chrome'), \
                sync_playwright() as playwright:
            context, executable = ui_run.launch_persistent_chrome(playwright, headed=True)
            try:
                self.assertEqual(executable, ui_run.FALLBACK_CHROMIUM)
                page = context.new_page()
                page.set_content('<p>fallback</p>')
                self.assertEqual(page.inner_text('body'), 'fallback')
            finally:
                context.close()

    @unittest.skipUnless(os.environ.get('TESTBED_ONLINE') == '1',
                         'loads cad.onshape.com: set TESTBED_ONLINE=1')
    def test_onshape_sign_in_page_loads_without_bot_check(self):
        _needs_display(self)
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            context, executable = ui_run.launch_persistent_chrome(playwright, headed=True)
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(ui_run.SIGN_IN_URL)
                # Waited for, never filled or submitted.
                page.wait_for_selector(ui_run.SELECTORS['email'], state='visible',
                                       timeout=60000)
                page.wait_for_timeout(ui_run.PACE_MS)
                shot = ui_run.screenshot(page, 'sign-in-page')
                reason = ui_run.detect_onshape_bot_check(page)
                print(f'\nbrowser_chrome: {_which(executable)} loaded {page.url}; '
                      f'bot check: {reason or "none"}; screenshot: {shot}', file=sys.stderr)
                self.assertIsNone(reason)
                self.assertEqual(page.input_value(ui_run.SELECTORS['email']), '')
            finally:
                context.close()


if __name__ == '__main__':
    unittest.main()

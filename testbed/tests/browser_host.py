"""Browser tests of the fake Onshape host and the harness (spec 5.4); run by make testbed-replay.

One replay development server and one test browser serve the whole module. They use the
synthetic fixtures, staged under the part-select scenario's name: the server answers the
panel's Onshape calls from synthetic-panel.json, and the fake Onshape host answers the
panel's messages from synthetic-part-select.json.

A failure to frame the panel is spec 10's "cannot frame the panel" row: the assertion
messages name the check that failed.
"""
import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import quote

from testbed import settings
from testbed.browser import (ReplayDevServer, grant_local_network, host_url,
                             launch_test_browser, serve_fake_host, sign_in_for_replay)

FIXTURES = Path(__file__).resolve().parent / 'fixtures'
SCENARIO = 'part-select'
# The ids the synthetic fixtures were written with.
SYNTHETIC_DOCUMENT = {'did': '0000000000000000000000d1', 'wid': '0000000000000000000000a1',
                      'eid': '0000000000000000000000e1'}
WAIT_S = 20


def stage_synthetic_fixtures(directory) -> tuple:
    """(cassette dir, message dir) under `directory`, holding the synthetic fixtures as the
    part-select scenario's cassette and message log."""
    cassettes, messages = Path(directory) / 'cassettes', Path(directory) / 'messages'
    cassettes.mkdir()
    messages.mkdir()
    shutil.copy(FIXTURES / 'cassettes' / 'synthetic-panel.json', cassettes / f'{SCENARIO}.json')
    shutil.copy(FIXTURES / 'messages' / 'synthetic-part-select.json',
                messages / f'{SCENARIO}.json')
    return cassettes, messages


def wait_for(page, probe, what, timeout=WAIT_S):
    """Call `probe` until it returns something truthy; fail naming `what` if it never does.

    A probe that raises counts as not yet: a frame between two pages has no context. The
    pause is the page's, not time.sleep: Playwright's synchronous API takes in the
    browser's events (a frame's new URL among them) only while one of its calls runs.
    """
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            value = probe()
            if value:
                return value
        except Exception as exc:          # the frame is navigating; try again
            last_error = exc
        page.wait_for_timeout(100)
    raise AssertionError(f'timed out waiting for {what}'
                         + (f' (last error: {last_error})' if last_error else ''))


def panel_frame(page, path='/onshape-panel'):
    """The panel's frame once it shows `path` and its page script has run."""
    def probe():
        for frame in page.frames:
            if frame.url.startswith(f'http://localhost:{settings.REPLAY_PORT}{path}'):
                if frame.evaluate('!!window.PenguinCAM'):
                    return frame
        return None
    return wait_for(page, probe, f'the panel frame to load {path} (cannot frame the panel: check '
                           'the local network permission and frame-ancestors)')


def host_log(page):
    return page.evaluate('window.__testbed.log')


def messages_named(page, name):
    return [entry for entry in host_log(page) if (entry.get('data') or {}).get('messageName') == name]


class _BrowserCase(unittest.TestCase):
    """Shares one replay development server and one test browser across the module."""

    @classmethod
    def setUpClass(cls):
        cls._stack = []
        cls._tmp = tempfile.TemporaryDirectory()
        cls.cassette_dir, cls.message_dir = stage_synthetic_fixtures(cls._tmp.name)
        cls.server = ReplayDevServer(cassette_dir=cls.cassette_dir, message_dir=cls.message_dir)
        cls.server.__enter__()
        try:
            cls.playwright, cls.browser, cls.context = launch_test_browser(
                message_dir=cls.message_dir)
            sign_in_for_replay(cls.context, settings.REPLAY_PORT, SCENARIO)
        except BaseException:
            cls.server.__exit__(None, None, None)
            raise

    @classmethod
    def tearDownClass(cls):
        try:
            cls.browser.close()
            cls.playwright.stop()
        finally:
            cls.server.__exit__(None, None, None)
            cls._tmp.cleanup()

    def open_host(self, **kwargs):
        page = self.context.new_page()
        self.addCleanup(page.close)
        page.goto(host_url(SYNTHETIC_DOCUMENT, SCENARIO, **kwargs))
        wait_for(page, lambda: page.evaluate('!!(window.__testbed && window.__testbed.ready)'),
                 'the fake Onshape host to load its message log')
        return page


class FakeHostTest(_BrowserCase):
    def test_host_has_onshape_origin(self):
        page = self.open_host()
        self.assertEqual(page.evaluate('location.origin'), settings.FAKE_HOST_ORIGIN)
        self.assertEqual(page.evaluate('window.isSecureContext'), True)

    def test_panel_frames_and_sends_application_init(self):
        page = self.open_host()
        frame = panel_frame(page)
        self.assertEqual(frame.evaluate('window.PenguinCAM.source'), 'onshape')
        self.assertEqual(frame.evaluate('window.PenguinCAM.onshape.server'),
                         settings.FAKE_HOST_ORIGIN)
        inits = wait_for(page, lambda: messages_named(page, 'applicationInit'),
                         "the panel's applicationInit at the fake Onshape host")
        self.assertEqual(inits[0]['origin'], f'http://localhost:{settings.REPLAY_PORT}')
        self.assertEqual(inits[0]['data']['documentId'], SYNTHETIC_DOCUMENT['did'])
        self.assertEqual(inits[0]['data']['workspaceId'], SYNTHETIC_DOCUMENT['wid'])

    def test_session_cookie_survives_reload(self):
        page = self.open_host()
        frame = panel_frame(page)
        self.assertTrue(frame.evaluate('window.PenguinCAM.authenticated'),
                        'the panel is not signed in after /testbed/sign-in')
        frame.evaluate('window.__beforeReload = true')
        page.evaluate('window.__testbed.reloadPanel()')
        wait_for(page, lambda: frame.evaluate('!!window.PenguinCAM && !window.__beforeReload'),
                 'the panel to reload')
        self.assertTrue(frame.evaluate('window.PenguinCAM.authenticated'),
                        'the session cookie did not survive a reload of the panel')

    def test_print_page_strip_appears_and_reply_carries_live_message_id(self):
        page = self.open_host()
        frame = panel_frame(page)
        panel_path = frame.evaluate('location.pathname + location.search')
        frame.evaluate('url => { location.href = url; }',
                       f'/print?source=onshape&return={quote(panel_path, safe="")}')
        frame = panel_frame(page, '/print')
        wait_for(page, lambda: frame.evaluate("!!document.getElementById('testbed-strip')"),
                 'the checklist strip on the print page')
        # The strip starts at the scenario's first step; Ask for a part is the third.
        while frame.evaluate('window.PenguinCAMTestbed.currentStep()') != 'ask for a part':
            step = frame.evaluate('window.PenguinCAMTestbed.currentStep()')
            frame.click('#testbed-strip button:has-text("Next step")')
            wait_for(page, lambda: frame.evaluate('window.PenguinCAMTestbed.currentStep()') != step,
                     f'the strip to move on from {step}')
        page.evaluate("window.__testbed.setStep('ask for a part')")
        frame.click('#testbed-strip button:has-text("Ask for a part")')
        page.evaluate("window.__testbed.selectPart('Cylinder')")

        def selection_events():
            recorded = frame.evaluate('window.PenguinCAMTestbed.recorded')
            return [r['event'] for r in recorded if r['event']['dir'] == 'in'
                    and (r['event']['data'] or {}).get('messageName') == 'REQUESTED_SELECTION'
                    and r['event']['data'].get('selections')]
        events = wait_for(page, selection_events, 'the selection to reach the strip')
        self.assertEqual(events[0]['data']['messageId'], 'testbed-1')
        self.assertEqual(events[0]['origin'], settings.FAKE_HOST_ORIGIN)
        asks = messages_named(page, 'requestSelection')
        self.assertEqual([a['data']['messageId'] for a in asks], ['testbed-1'])
        recorded = json.dumps(frame.evaluate('window.PenguinCAMTestbed.recorded'))
        self.assertNotIn('testbed-3', recorded, "the recorded messageId leaked into a reply")

    def test_host_url_keeps_placeholder_workspace(self):
        url = host_url(SYNTHETIC_DOCUMENT, SCENARIO, version_id='0000000000000000000000b1')
        self.assertIn('/documents/0000000000000000000000d1/v/0000000000000000000000b1/'
                      'e/0000000000000000000000e1', url)
        page = self.open_host(version_id='0000000000000000000000b1')
        src = page.evaluate("document.getElementById('panel').getAttribute('src')")
        self.assertIn('workspaceId={$workspaceId}&versionId=0000000000000000000000b1', src)
        frame = panel_frame(page)
        self.assertEqual(frame.evaluate('window.PenguinCAM.onshape.workspaceId'), '{$workspaceId}')
        self.assertEqual(frame.evaluate('window.PenguinCAM.onshape.versionId'),
                         '0000000000000000000000b1')
        init = wait_for(page, lambda: messages_named(page, 'applicationInit'),
                        "the panel's applicationInit")[0]
        self.assertEqual(init['data']['workspaceId'], '{$workspaceId}')
        # Matching goes by name and keys, so the placeholder does not stop an answer.
        ask = {'messageName': 'requestSelection', 'messageId': 'testbed-9',
               'entityTypeSpecifier': ['BODY'], 'requiredSelectionCount': 1,
               'documentId': SYNTHETIC_DOCUMENT['did'], 'workspaceId': '{$workspaceId}',
               'elementId': SYNTHETIC_DOCUMENT['eid']}
        replies = page.evaluate("m => window.__testbed.match(m, 'ask for a part')", ask)
        self.assertEqual([r['messageId'] for r in replies], ['testbed-9'])

    def test_cdp_fallback_grant_frames_the_panel(self):
        context = self.browser.new_context()
        self.addCleanup(context.close)

        def refuse(*args, **kwargs):
            raise RuntimeError('Unknown permission: local-network-access')
        context.grant_permissions = refuse            # as on a Playwright before 1.58
        self.assertEqual(grant_local_network(self.browser, context), 'cdp')
        context.route(settings.FAKE_HOST_ORIGIN + '/**',
                      lambda route: serve_fake_host(route, self.message_dir))
        sign_in_for_replay(context, settings.REPLAY_PORT)
        page = context.new_page()
        page.goto(host_url(SYNTHETIC_DOCUMENT, SCENARIO))
        self.assertTrue(panel_frame(page).evaluate('window.PenguinCAM.authenticated'))

    def test_unrouted_request_to_fake_host_fails_safe(self):
        page = self.open_host()
        answer = page.evaluate("""async () => {
            const r = await fetch('/api/v13/documents');
            return {status: r.status, text: await r.text()};
        }""")
        self.assertEqual(answer['status'], 404)
        self.assertTrue(answer['text'].startswith('not served by the fake Onshape host'),
                        answer['text'])
        missing = page.evaluate("fetch('/_testbed/messages/no-such-scenario.json')"
                                '.then(r => r.status)')
        self.assertEqual(missing, 404)
        # Any other Onshape host is refused inside the browser, before the network.
        failures = []
        page.on('requestfailed', lambda request: failures.append((request.url, request.failure)))
        escaped = page.evaluate("""() => fetch('https://cad.onshape.com/api/v13/users/sessioninfo')
            .then(() => 'reached', () => 'refused')""")
        self.assertEqual(escaped, 'refused')
        blocked = [url for url, failure in failures
                   if (failure or '').startswith('net::ERR_BLOCKED_BY_CLIENT')]
        self.assertEqual(blocked, ['https://cad.onshape.com/api/v13/users/sessioninfo'])


if __name__ == '__main__':
    unittest.main()

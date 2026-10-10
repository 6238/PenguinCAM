"""Browser checks of the print page in Onshape (spec 8.2); run by make testbed-replay.

The five print checks drive print-select-parts on the fake Onshape host against a replay
development server, as the owner drove it while recording, and then use the print page:
both parts listed with sizes, quantity 2 giving three copies, rotate and lay on its side,
a real Orca slice of the replayed meshes, and the download. Each is skipped with "needs a
recording" until print-select-parts is recorded, never passed.

The self-tests run on synthetic fixtures (the fake host's own check, spec 8.2 allows it):
the checklist strip defers to the print page's own messages.
"""
import json
import tempfile
import unittest
from urllib.parse import quote

from testbed import settings
from testbed.browser import ReplayDevServer, host_url, launch_test_browser, sign_in_for_replay
from testbed.scenarios import SCENARIOS, scenario_is_recorded
from testbed.tests.browser_host import (SYNTHETIC_DOCUMENT, messages_named, panel_frame,
                                        stage_synthetic_fixtures, wait_for)
from testbed.tests.browser_scenarios import ScenarioDriver

SCENARIO = 'print-select-parts'
SLICE_TIMEOUT_S = 300
# A copy is selected by clicking the plate; these fractions of the canvas are tried in turn.
_GRID = [(x / 10, y / 8) for y in range(1, 8) for x in range(1, 10)]


def _strip_index(frame):
    return frame.evaluate('window.PenguinCAMTestbed.currentStep()')


def advance_strip_to(page, frame, name):
    """Press the strip's Next step until its current step is `name`."""
    while _strip_index(frame) != name:
        before = _strip_index(frame)
        frame.click('#testbed-next')
        wait_for(page, lambda: _strip_index(frame) != before, f'the strip to move on from {before}')


class PrintScenarioTest(unittest.TestCase):
    """The print checks on the recorded print-select-parts; one server and browser for all."""

    server = None

    @classmethod
    def setUpClass(cls):
        if not scenario_is_recorded(SCENARIO):
            return                       # each test skips itself
        documents = json.loads(settings.DOCUMENTS_FILE.read_text())
        cls.document = documents[SCENARIOS[SCENARIO].document]
        cls.server = ReplayDevServer().__enter__()
        try:
            cls.playwright, cls.browser, cls.context = launch_test_browser()
            sign_in_for_replay(cls.context, settings.REPLAY_PORT, SCENARIO)
        except BaseException:
            cls.server.__exit__(None, None, None)
            cls.server = None
            raise

    @classmethod
    def tearDownClass(cls):
        if cls.server is None:
            return
        try:
            cls.browser.close()
            cls.playwright.stop()
        finally:
            cls.server.__exit__(None, None, None)

    def setUp(self):
        if self.server is None:
            self.skipTest('needs a recording')

    def two_parts(self):
        """(page, print page frame) once print-select-parts has listed Box and Cylinder."""
        page = self.context.new_page()
        self.addCleanup(page.close)
        page.goto(host_url(self.document, SCENARIO))
        wait_for(page, lambda: page.evaluate('window.__testbed.ready'),
                 f'the fake Onshape host to load the message log of {SCENARIO}')
        driver = ScenarioDriver(page, SCENARIOS[SCENARIO])
        driver.run()
        frame = driver.frame
        wait_for(page, lambda: frame.evaluate("document.querySelectorAll('#parts-list li').length") == 2,
                 'both parts in the parts list')
        return page, frame

    def to_layout(self, page, frame):
        frame.click('#btn-next')
        wait_for(page, lambda: frame.evaluate(
            "!document.querySelector('.step[data-step=\"layout\"]').hidden"), 'the Layout step')

    def summary(self, frame):
        return frame.evaluate("document.getElementById('wiz-summary').textContent")

    def select_a_copy(self, page, frame):
        """Click the plate until a copy is selected; return its name line."""
        box = frame.locator('#layout-canvas').bounding_box()
        unselected = frame.evaluate("document.getElementById('layout-copy-name').textContent")
        for fx, fy in _GRID:
            frame.locator('#layout-canvas').click(position={'x': box['width'] * fx,
                                                            'y': box['height'] * fy})
            name = frame.evaluate("document.getElementById('layout-copy-name').textContent")
            if name != unselected:
                return name
        self.fail('no copy could be selected by clicking the plate')

    def slice(self, page, frame):
        self.to_layout(page, frame)
        wait_for(page, lambda: frame.evaluate("!document.getElementById('btn-next').disabled"),
                 'Next on the Layout step')
        frame.click('#btn-next')
        state = wait_for(page, lambda: frame.evaluate(
            "['done', 'failed'].indexOf(window.PenguinCAM.getJobState()) >= 0 && "
            'window.PenguinCAM.getJobState()'), 'the slice to finish', timeout=SLICE_TIMEOUT_S)
        self.assertEqual(state, 'done', frame.evaluate(
            "document.getElementById('preview-errors').textContent"))

    def test_select_two_parts_lists_both_with_sizes(self):
        page, frame = self.two_parts()
        rows = frame.evaluate("""[...document.querySelectorAll('#parts-list li')].map(li => ({
            name: li.querySelector('.name').textContent,
            dims: li.querySelector('.dims').textContent}))""")
        self.assertEqual(sorted(r['name'] for r in rows), ['Box', 'Cylinder'])
        sizes = {r['name']: sorted(float(v) for v in r['dims'].replace(' mm', '').split(' x '))
                 for r in rows}
        # tb-two-parts: a 40 x 30 x 10 mm box and a 20 mm diameter, 20 mm tall cylinder.
        for got, want in ((sizes['Box'], [10, 30, 40]), (sizes['Cylinder'], [20, 20, 20])):
            for g, w in zip(got, want):
                self.assertAlmostEqual(g, w, delta=0.2)
        self.assertEqual(frame.evaluate("document.getElementById('parts-errors').textContent"), '')

    def test_quantity_two_gives_three_copies_arranged(self):
        page, frame = self.two_parts()
        quantity = frame.locator('#parts-list li .part-qty').first
        quantity.fill('2')
        quantity.dispatch_event('change')
        self.to_layout(page, frame)
        wait_for(page, lambda: '2 parts, 3 copies' in self.summary(frame),
                 'the summary to read 2 parts, 3 copies')
        # Arranged on the plate: no sentence, and Next is open.
        self.assertEqual(frame.evaluate("document.getElementById('layout-errors').textContent"), '')
        self.assertFalse(frame.evaluate("document.getElementById('btn-next').disabled"))

    def test_rotate_then_lay_on_side(self):
        page, frame = self.two_parts()
        self.to_layout(page, frame)
        self.select_a_copy(page, frame)
        before = int(frame.evaluate("document.getElementById('layout-angle').value") or 0)
        frame.click('#btn-rot-plus')
        self.assertEqual(int(frame.evaluate("document.getElementById('layout-angle').value")),
                         (before + 90) % 360)
        current = frame.evaluate("document.getElementById('layout-flat').value")
        side = next(o for o in ('+x', '-x', '+y', '-y') if o != current)
        frame.select_option('#layout-flat', side)
        self.assertEqual(frame.evaluate("document.getElementById('layout-flat').value"), side)
        self.assertIn('2 parts, 2 copies', self.summary(frame))

    def test_slice(self):
        page, frame = self.two_parts()
        self.slice(page, frame)

    def test_download(self):
        page, frame = self.two_parts()
        self.slice(page, frame)
        answer = frame.evaluate("""async () => {
            const r = await fetch('/download/' + window.PenguinCAM.getDownloadToken(),
                                  {credentials: 'same-origin'});
            return {status: r.status, disposition: r.headers.get('Content-Disposition') || ''};
        }""")
        self.assertEqual(answer['status'], 200)
        self.assertIn('.gcode.3mf', answer['disposition'])


class PrintPageSelfTest(unittest.TestCase):
    """The strip on the print page, against synthetic fixtures."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.cassette_dir, cls.message_dir = stage_synthetic_fixtures(cls._tmp.name)
        cls.server = ReplayDevServer(cassette_dir=cls.cassette_dir, message_dir=cls.message_dir)
        cls.server.__enter__()
        try:
            cls.playwright, cls.browser, cls.context = launch_test_browser(
                message_dir=cls.message_dir)
            sign_in_for_replay(cls.context, settings.REPLAY_PORT, 'part-select')
        except BaseException:
            cls.server.__exit__(None, None, None)
            cls._tmp.cleanup()
            raise

    @classmethod
    def tearDownClass(cls):
        try:
            cls.browser.close()
            cls.playwright.stop()
        finally:
            cls.server.__exit__(None, None, None)
            cls._tmp.cleanup()

    def test_strip_defers_to_print_page_messages(self):
        page = self.context.new_page()
        self.addCleanup(page.close)
        page.goto(host_url(SYNTHETIC_DOCUMENT, 'part-select'))
        wait_for(page, lambda: page.evaluate('window.__testbed.ready'), 'the fake Onshape host')
        frame = panel_frame(page)
        wait_for(page, lambda: messages_named(page, 'applicationInit'), "the panel's applicationInit")
        inits = len(messages_named(page, 'applicationInit'))
        path = frame.evaluate('location.pathname + location.search')
        frame.evaluate('url => { location.href = url; }',
                       f'/print?source=onshape&return={quote(path, safe="")}')
        frame = panel_frame(page, '/print')
        wait_for(page, lambda: frame.evaluate("!!document.getElementById('testbed-strip')"),
                 'the checklist strip on the print page')
        self.assertTrue(frame.evaluate('window.PenguinCAMTestbed.deferred'))
        # One applicationInit, the print page's own; the strip sends none.
        wait_for(page, lambda: len(messages_named(page, 'applicationInit')) == inits + 1,
                 "the print page's applicationInit")
        page.wait_for_timeout(500)
        self.assertEqual(len(messages_named(page, 'applicationInit')), inits + 1)
        # The strip's ask and dialog buttons are hidden.
        advance_strip_to(page, frame, 'ask for a part')
        self.assertFalse(frame.is_visible('#testbed-action'))
        # The page's own request is recorded on its way out.
        wait_for(page, lambda: not frame.evaluate("document.getElementById('btn-next').disabled"),
                 'Next on the Setup step')
        frame.click('#btn-next')

        def recorded(direction, name):
            return [r['event'] for r in frame.evaluate('window.PenguinCAMTestbed.recorded')
                    if r['event']['dir'] == direction
                    and (r['event']['data'] or {}).get('messageName') == name]
        asks = wait_for(page, lambda: recorded('out', 'requestSelection'),
                        "the print page's requestSelection in the strip's record")
        self.assertEqual(asks[0]['data']['messageId'], 'penguincam-print-1')
        self.assertEqual(asks[0]['data']['requiredSelectionCount'], 1)
        self.assertEqual([m['data']['messageId'] for m in messages_named(page, 'requestSelection')],
                         ['penguincam-print-1'])
        # Every received message is still recorded; the answer reached the page too.
        page.evaluate("window.__testbed.selectPart('Cylinder')")
        answers = wait_for(page, lambda: [e for e in recorded('in', 'REQUESTED_SELECTION')
                                          if e['data'].get('selections')],
                           'the selection in the strip\'s record')
        self.assertEqual(answers[0]['data']['messageId'], 'penguincam-print-1')
        wait_for(page, lambda: len(recorded('out', 'requestSelection')) == 2,
                 'the print page to ask again after the answer')


if __name__ == '__main__':
    unittest.main()

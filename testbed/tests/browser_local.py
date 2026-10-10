"""Browser check of local mode (run by make testbed-replay): the stand-in Onshape Part
Studio at /testbed/local, driven as a person would drive it.

A local-mode development server on LOCAL_TEST_PORT (never 6238), with its own state
directory, and the test browser: upload two STL files through the file input, choose 3D
Printing in the panel, select both parts in the Parts list with a click and a shift-click,
open and close the part picker, reach Layout, and, when Orca is installed, slice and fetch
the .gcode.3mf. No Onshape host is contacted: launch_test_browser refuses every
*.onshape.com request, and local mode answers the print path's API calls itself.
"""
import tempfile
import unittest
from pathlib import Path

from print.slicer import ORCA_BIN
from testbed import settings
from testbed.browser import LocalDevServer, launch_test_browser
from testbed.tests.browser_host import wait_for
from testbed.tests.fixtures.make_print_fixtures import box_stl, cylinder_stl

PORT = settings.LOCAL_TEST_PORT
ORIGIN = f'http://localhost:{PORT}'
SLICE_TIMEOUT_S = 300
PANEL_WIDTH = 320


def frame_at(page, path):
    """The panel's frame once it shows `path` and its page script has run."""
    def probe():
        for frame in page.frames:
            if frame.url.startswith(ORIGIN + path) and frame.evaluate('!!window.PenguinCAM'):
                return frame
        return None
    return wait_for(page, probe, f'the panel frame to load {path}')


class LocalModeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.stls = [root / 'Bracket.stl', root / 'Spacer.stl']
        cls.stls[0].write_bytes(box_stl(40, 30, 10))
        cls.stls[1].write_bytes(cylinder_stl(10, 20))
        cls.server = LocalDevServer(state_home=root / 'state').__enter__()
        try:
            cls.playwright, cls.browser, cls.context = launch_test_browser()
        except BaseException:
            cls.server.__exit__(None, None, None)
            cls.tmp.cleanup()
            raise

    @classmethod
    def tearDownClass(cls):
        try:
            cls.browser.close()
            cls.playwright.stop()
        finally:
            cls.server.__exit__(None, None, None)
            cls.tmp.cleanup()

    def test_upload_select_layout_and_slice(self):
        page = self.context.new_page()
        self.addCleanup(page.close)
        page.set_viewport_size({'width': 1400, 'height': 900})
        page.goto(f'{ORIGIN}/testbed/local')
        wait_for(page, lambda: page.evaluate('window.__local.ready || window.__local.error'),
                 'the stand-in to sign in and load')
        self.assertIsNone(page.evaluate('window.__local.error || null'))

        # Upload both files as a person would, through the file input.
        page.set_input_files('#file-input', [str(p) for p in self.stls])
        wait_for(page, lambda: page.evaluate("document.querySelectorAll('#parts li[data-part-id]').length") == 2,
                 'both uploaded parts in the Parts list')
        self.assertEqual(page.evaluate("[...document.querySelectorAll('#parts li .name')].map(n => n.textContent)"),
                         ['Bracket', 'Spacer'])

        # The panel is the narrow right panel, framed with the extension's query.
        box = page.locator('#panel').bounding_box()
        self.assertAlmostEqual(box['width'], PANEL_WIDTH, delta=2)
        self.assertGreater(box['height'], 600)
        url = page.evaluate('window.__local.panelUrl()')
        for key in ('documentId=', 'workspaceId=', 'elementId=', 'server=' + 'http%3A%2F%2Flocalhost'):
            self.assertIn(key, url)

        panel = frame_at(page, '/onshape-panel')
        panel.check('input[name="mode"][value="print"]')
        frame = frame_at(page, '/print')
        self.assertFalse(frame.evaluate("!!document.querySelector('script[src=\"/testbed/panel.js\"]')"))
        frame.click('#btn-next')                                 # Setup -> Parts
        wait_for(page, lambda: page.evaluate('window.__local.hasRequest()'),
                 "the print page's selection request")

        page.click('#parts li[data-part-id="L1"]')
        wait_for(page, lambda: frame.evaluate("document.querySelectorAll('#parts-list li').length") == 1,
                 'Bracket in the print page parts list')
        wait_for(page, lambda: page.evaluate('window.__local.hasRequest()'), 'the request re-armed')
        page.click('#parts li[data-part-id="L2"]', modifiers=['Shift'])
        wait_for(page, lambda: frame.evaluate("document.querySelectorAll('#parts-list li').length") == 2,
                 'both parts in the print page parts list')
        self.assertEqual(page.evaluate("document.querySelectorAll('#parts li.selected').length"), 2)
        self.assertEqual(frame.evaluate("document.getElementById('parts-errors').textContent"), '')
        rows = frame.evaluate("""[...document.querySelectorAll('#parts-list li')].map(li => ({
            name: li.querySelector('.name').textContent, dims: li.querySelector('.dims').textContent}))""")
        sizes = {r['name']: sorted(float(v) for v in r['dims'].replace(' mm', '').split(' x ')) for r in rows}
        self.assertEqual(set(sizes), {'Bracket', 'Spacer'})
        for got, want in ((sizes['Bracket'], [10, 30, 40]), (sizes['Spacer'], [20, 20, 20])):
            for g, w in zip(got, want):
                self.assertAlmostEqual(g, w, delta=0.2)

        # The part picker: opened by the panel, a pick of a listed part adds nothing new.
        frame.click('#btn-add-from-document')
        wait_for(page, lambda: page.evaluate("!document.getElementById('dialog-back').hidden"),
                 'the stand-in part picker')
        page.click('#dialog-parts li[data-part-id="L1"]')
        page.click('#dialog-close')
        wait_for(page, lambda: page.evaluate('window.__local.hasRequest()'), 'the request re-armed after Close')
        self.assertEqual(frame.evaluate("document.querySelectorAll('#parts-list li').length"), 2)
        names = [e['data'].get('messageName') for e in page.evaluate('window.__local.sent')]
        self.assertIn('itemSelectedInSelectItemDialog', names)
        self.assertIn('selectItemDialogClosed', names)

        frame.click('#btn-next')                                 # Parts -> Layout
        wait_for(page, lambda: frame.evaluate(
            "!document.querySelector('.step[data-step=\"layout\"]').hidden"), 'the Layout step')
        wait_for(page, lambda: '2 parts, 2 copies' in frame.evaluate(
            "document.getElementById('wiz-summary').textContent"), 'the summary to read 2 parts, 2 copies')
        frame.click('#btn-arrange')
        wait_for(page, lambda: not frame.evaluate("document.getElementById('btn-next').disabled"),
                 'Next on the Layout step')

        if not ORCA_BIN.is_file():
            self.skipTest(f'Orca is not installed at {ORCA_BIN}; the slice was not run')
        frame.click('#btn-next')                                 # Layout -> Preview: slice
        state = wait_for(page, lambda: frame.evaluate(
            "['done', 'failed'].indexOf(window.PenguinCAM.getJobState()) >= 0 && "
            'window.PenguinCAM.getJobState()'), 'the slice to finish', timeout=SLICE_TIMEOUT_S)
        self.assertEqual(state, 'done', frame.evaluate("document.getElementById('preview-errors').textContent"))
        answer = frame.evaluate("""async () => {
            const r = await fetch('/download/' + window.PenguinCAM.getDownloadToken(), {credentials: 'same-origin'});
            const body = await r.arrayBuffer();
            return {status: r.status, disposition: r.headers.get('Content-Disposition') || '',
                    bytes: body.byteLength};
        }""")
        self.assertEqual(answer['status'], 200)
        self.assertIn('.gcode.3mf', answer['disposition'])
        self.assertGreater(answer['bytes'], 1000)


if __name__ == '__main__':
    unittest.main()

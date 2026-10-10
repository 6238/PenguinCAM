"""The rendered print wizard page carries the elements the JS and the acceptance steps rely on."""
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]        # the repository root
sys.path.insert(0, str(REPO))

from frc_cam_gui_app import app, limiter  # noqa: E402


class PrintWizardPageTest(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        self.client = app.test_client()
        with self.client.session_transaction() as sess:
            sess["app_verified"] = True
        limiter.reset()
        limiter.enabled = False

    def tearDown(self):
        limiter.enabled = True

    def test_page_has_header_stepbar_radios_fields_and_no_machine_dropdown(self):
        html = self.client.get("/print?source=upload&theme=dark&return=/app").get_data(as_text=True)
        self.assertIn('id="wiz-header"', html)
        self.assertIn('id="stepbar"', html)
        for step in ("setup", "parts", "layout", "preview"):
            self.assertIn(f'data-step="{step}"', html)
        for value in ("2d", "2.5d", "tubing", "print"):
            self.assertIn(f'name="mode" value="{value}" autocomplete="off"', html)
        self.assertRegex(html, r'value="print"[^>]*checked')
        for el in ("p-printer", "p-filament", "p-process", "setup-errors", "parts-list", "parts-errors",
                   "layout-info", "layout-canvas",
                   "layout-errors", "gen-status", "preview-stats", "print-canvas", "preview-errors",
                   "preview-notes", "btn-do", "btn-do-caret", "do-menu", "btn-reset-view"):
            self.assertIn(f'id="{el}"', html, el)
        self.assertNotIn('id="f-machine"', html)
        self.assertIn("print_viewer.js", html)
        self.assertIn("print_wizard.js", html)
        self.assertIn("three.js/r128/three.min.js", html)
        self.assertIn("OrbitControls.js", html)
        self.assertNotIn("gcode_viewer.js", html)
        self.assertIn("returnUrl: \"/app\"", html)
        self.assertIn("driveEnabled:", html)

    def test_theme_and_source_reach_the_document(self):
        html = self.client.get("/print?source=onshape&theme=light").get_data(as_text=True)
        self.assertIn('data-theme="light"', html)
        self.assertIn('data-source="onshape"', html)
        self.assertIn('source: "onshape"', html)


class CncSetupRadioTest(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        self.client = app.test_client()
        limiter.reset()
        limiter.enabled = False

    def tearDown(self):
        limiter.enabled = True

    def test_cnc_setup_offers_3d_printing(self):
        html = self.client.get("/app").get_data(as_text=True)
        self.assertRegex(
            html, r'<input type="radio" name="mode" value="print" autocomplete="off">\s*3D Printing')
        js = (REPO / "static" / "wizard.js").read_text()
        self.assertIn("'/print?source='", js)
        self.assertIn("location.pathname + location.search", js)


if __name__ == "__main__":
    unittest.main()


class PrinterPanelWiringTest(PrintWizardPageTest):
    """The printer half of the Preview step: the panel script is loaded, and the bootstrap
    says whether this session's Onshape config carries a pairing code."""

    def test_the_panel_script_is_loaded_after_the_wizard_script(self):
        html = self.client.get("/print?source=onshape").get_data(as_text=True)
        self.assertIn("printer_panel.js", html)
        self.assertLess(html.index("print_wizard.js"), html.index("printer_panel.js"))

    def test_printer_enabled_follows_the_onshape_config(self):
        with self.client.session_transaction() as sess:
            sess["team_config_data"] = {"team": {"number": 6238},
                                        "printing": {"pairing_code": "7K3M-Q9XW"}}
        html = self.client.get("/print?source=onshape").get_data(as_text=True)
        self.assertIn("printerEnabled: true", html)

    def test_printer_enabled_is_false_without_a_code(self):
        html = self.client.get("/print?source=onshape").get_data(as_text=True)
        self.assertIn("printerEnabled: false", html)

    def test_the_upload_flow_never_reports_a_printer(self):
        """force_defaults must not read the Onshape config, even when the browser has one."""
        with self.client.session_transaction() as sess:
            sess["team_config_data"] = {"team": {"number": 6238},
                                        "printing": {"pairing_code": "7K3M-Q9XW"}}
            sess["upload_config_data"] = {"team": {"number": 6238},
                                          "printing": {"pairing_code": "7K3M-Q9XW"}}
        html = self.client.get("/print?source=upload").get_data(as_text=True)
        self.assertIn("printerEnabled: false", html)

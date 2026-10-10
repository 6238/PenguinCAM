"""print/print_config.py: the team's printing section against the catalog, and the four
student overrides. Also the two routes that read it. No Orca run."""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[2]        # the repository root
sys.path.insert(0, str(REPO))

from print import print_config  # noqa: E402
from print.print_config import (  # noqa: E402
    DROPPED_MESSAGE, EMPTY_LIST_MESSAGE, OVERRIDE_CHOICES, UNKNOWN_PRINTER_MESSAGE, Catalog, OverrideError,
    choices_for_request, default_choices, load_catalog, resolve_print_config, validate_overrides,
    write_process_with_overrides,
)
from print.slicer import DEFAULT_PROFILES  # noqa: E402
from print.tests.test_routes_jobs import JobRouteBase, copy  # noqa: E402
from team_config import TeamConfig  # noqa: E402

P4 = "Bambu Lab H2S 0.4 nozzle"
SPEC_PRINTING = {
    "pairing_code": "ABCD2345",
    "printer": P4,
    "filaments": ["Bambu PLA Basic @BBL H2S", "Bambu PETG HF @BBL H2S"],
    "processes": ["0.20mm Standard @BBL H2S", "0.16mm High Quality @BBL H2S"],
    "allow_overrides": True,
}


def team(printing):
    return {"team": {"number": 1234, "name": "Test"}, "printing": printing}


class PrintingSectionTest(unittest.TestCase):
    def test_printing_section_v2_root_and_v1_machine(self):
        v2 = TeamConfig.from_dict({"version": 2, "team": {"number": 1}, "printing": {"printer": P4},
                                   "machines": {"m": {"machine": {"name": "M"}}}})
        self.assertEqual(v2.printing_section(), {"printer": P4})
        v1 = TeamConfig.from_dict({"team": {"number": 1}, "printing": {"printer": P4, "pairing_code": "ABCD2345"}})
        self.assertEqual(v1.printing_section()["printer"], P4)
        self.assertEqual(v1.pairing_code, "ABCD2345")
        self.assertIsNone(TeamConfig.from_dict({"team": {"number": 1}}).printing_section())
        self.assertIsNone(TeamConfig.from_dict({"team": {"number": 1}}).pairing_code)
        self.assertIsNone(TeamConfig.from_dict({"printing": {"printer": P4}}).pairing_code)


class ResolveTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = Catalog.load()

    def test_no_printer_key_gives_default_set(self):
        for printing in (None, {}, {"pairing_code": "ABCD2345"}, {"filaments": ["Bambu PLA Basic @BBL H2S"]}):
            with self.subTest(printing=printing):
                choices = resolve_print_config(printing, self.catalog)
                self.assertTrue(choices.is_default)
                self.assertEqual(choices.printer, "Bambu Lab H2S 0.6 nozzle")
                self.assertEqual(choices.filaments, ["Generic PETG @BBL H2S"])
                self.assertEqual(choices.processes, ["0.24mm Balanced Quality @BBL H2S 0.6 nozzle"])
                self.assertTrue(choices.allow_overrides)
                self.assertIsNone(choices.error)
                self.assertEqual(choices.slice_profiles(choices.filaments[0], choices.processes[0]),
                                 DEFAULT_PROFILES)

    def test_unknown_printer_fails_closed_naming_key(self):
        choices = resolve_print_config(dict(SPEC_PRINTING, printer="Bambu Lab H2Z 0.4 nozzle"), self.catalog)
        self.assertEqual(choices.error, "The team configuration names printer 'Bambu Lab H2Z 0.4 nozzle', which "
                                        "PenguinCAM does not know. Ask a mentor to fix printing.printer.")
        self.assertEqual(choices.error, UNKNOWN_PRINTER_MESSAGE.format(name="Bambu Lab H2Z 0.4 nozzle"))
        self.assertEqual((choices.filaments, choices.processes), ([], []))
        self.assertFalse(choices.is_default)
        self.assertEqual(choices.printer, "Bambu Lab H2Z 0.4 nozzle")
        with self.assertRaises(ValueError):
            choices.slice_profiles("Bambu PLA Basic @BBL H2S", "0.20mm Standard @BBL H2S")
        with self.assertRaises(ValueError):
            choices.slice_profiles("Generic PETG @BBL H2S", "0.24mm Balanced Quality @BBL H2S 0.6 nozzle")

    def test_spec_example_resolves(self):
        choices = resolve_print_config(SPEC_PRINTING, self.catalog)
        self.assertIsNone(choices.error)
        self.assertEqual(choices.warnings, [])
        self.assertEqual(choices.skipped, [])
        self.assertEqual(choices.filaments, SPEC_PRINTING["filaments"])
        self.assertEqual(choices.processes, SPEC_PRINTING["processes"])
        profiles = choices.slice_profiles("Bambu PETG HF @BBL H2S", "0.16mm High Quality @BBL H2S")
        self.assertEqual(json.loads(profiles.printer.read_text())["name"], P4)
        self.assertEqual(json.loads(profiles.filament.read_text())["name"], "Bambu PETG HF @BBL H2S")
        self.assertEqual(json.loads(profiles.process.read_text())["name"], "0.16mm High Quality @BBL H2S")
        self.assertEqual(choices.printer_profile()["name"], P4)
        data = choices.to_json()
        self.assertNotIn("/", json.dumps(data).replace("0.20mm", "").replace("@BBL", ""))   # no paths
        self.assertEqual(data["printer"], P4)
        with self.assertRaises(ValueError):
            choices.slice_profiles("Generic PETG @BBL H2S", "0.20mm Standard @BBL H2S")   # in the catalog, not offered

    def test_incompatible_filament_dropped_with_warning(self):
        printing = dict(SPEC_PRINTING, processes=["0.24mm Balanced Quality @BBL H2S 0.6 nozzle",
                                                  "0.20mm Standard @BBL H2S"],
                        filaments=["Bambu PLA Basic @BBL H2S", "No Such Filament"])
        choices = resolve_print_config(printing, self.catalog)
        self.assertIsNone(choices.error)
        self.assertEqual(choices.processes, ["0.20mm Standard @BBL H2S"])
        self.assertEqual(choices.filaments, ["Bambu PLA Basic @BBL H2S"])
        self.assertEqual(choices.warnings, [
            DROPPED_MESSAGE.format(key="filaments", name="No Such Filament", printer=P4),
            DROPPED_MESSAGE.format(key="processes", name="0.24mm Balanced Quality @BBL H2S 0.6 nozzle", printer=P4)])
        self.assertIn("printing.processes names '0.24mm Balanced Quality @BBL H2S 0.6 nozzle'", choices.warnings[1])
        self.assertEqual(choices.skipped, ["No Such Filament", "0.24mm Balanced Quality @BBL H2S 0.6 nozzle"])

    def test_list_empty_after_drops_disables_slicing(self):
        choices = resolve_print_config(dict(SPEC_PRINTING, processes=["0.24mm Balanced Quality @BBL H2S 0.6 nozzle"]),
                                       self.catalog)
        self.assertEqual(choices.error, EMPTY_LIST_MESSAGE.format(what="process", printer=P4, key="processes"))
        with self.assertRaises(ValueError):
            choices.slice_profiles("Bambu PLA Basic @BBL H2S", "0.24mm Balanced Quality @BBL H2S 0.6 nozzle")
        for bad in ("Bambu PLA Basic @BBL H2S", None, 7):       # not a list counts as empty
            with self.subTest(filaments=bad):
                choices = resolve_print_config(dict(SPEC_PRINTING, filaments=bad), self.catalog)
                self.assertEqual(choices.error,
                                 EMPTY_LIST_MESSAGE.format(what="filament", printer=P4, key="filaments"))

    def test_allow_overrides_defaults_to_true(self):
        printing = {k: v for k, v in SPEC_PRINTING.items() if k != "allow_overrides"}
        self.assertTrue(resolve_print_config(printing, self.catalog).allow_overrides)
        self.assertFalse(resolve_print_config(dict(printing, allow_overrides=False), self.catalog).allow_overrides)

    def test_catalog_loaded_once(self):
        load_catalog.cache_clear()
        self.addCleanup(load_catalog.cache_clear)
        with mock.patch.object(Catalog, "load", wraps=Catalog.load) as spy:
            first = choices_for_request(team(SPEC_PRINTING), force_defaults=False)
            second = choices_for_request(team(SPEC_PRINTING), force_defaults=False)
        self.assertEqual(spy.call_count, 1)
        self.assertEqual(first.filaments, second.filaments)
        self.assertTrue(choices_for_request(team(SPEC_PRINTING), force_defaults=True).is_default)
        self.assertTrue(choices_for_request(None, force_defaults=False).is_default)

    def test_default_choices_names_come_from_the_default_files(self):
        choices = default_choices()
        self.assertEqual(choices.printer_profile()["name"], "Bambu Lab H2S 0.6 nozzle")
        self.assertEqual(choices.to_json()["is_default"], True)


class OverridesTest(unittest.TestCase):
    def test_overrides_mapped_to_orca_keys(self):
        self.assertEqual(OVERRIDE_CHOICES, {"infill": [10, 15, 20, 30, 40, 60, 100], "walls": [2, 3, 4, 6],
                                            "supports": ["off", "on"], "brim": ["auto", "off", "outer"]})
        self.assertEqual(validate_overrides({"infill": 40, "walls": 4, "supports": "on", "brim": "off"}, True),
                         {"sparse_infill_density": "40%", "wall_loops": "4", "enable_support": "1",
                          "support_type": "tree(auto)", "brim_type": "no_brim"})
        self.assertEqual(validate_overrides({"supports": "off", "brim": "auto"}, True),
                         {"enable_support": "0", "brim_type": "auto_brim"})
        self.assertEqual(validate_overrides({"brim": "outer"}, True), {"brim_type": "outer_only"})
        self.assertEqual(validate_overrides(None, True), {})
        self.assertEqual(validate_overrides({}, False), {})

    def test_override_outside_list_refused(self):
        for raw in ({"infill": 25}, {"walls": "3"}, {"colour": "red"}, {"infill": True}, {"supports": 1}, [1]):
            with self.subTest(raw=raw):
                with self.assertRaises(OverrideError) as ctx:
                    validate_overrides(raw, True)
                self.assertTrue(ctx.exception.message.endswith("."))

    def test_overrides_refused_when_not_allowed(self):
        with self.assertRaises(OverrideError):
            validate_overrides({"infill": 40}, False)

    def test_write_process_with_overrides(self):
        tmp = Path(tempfile.mkdtemp(prefix="print-config-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        out = write_process_with_overrides(DEFAULT_PROFILES.process, {"wall_loops": "4"}, tmp / "process.json")
        merged = json.loads(out.read_text())
        self.assertEqual(merged["wall_loops"], "4")
        original = json.loads(DEFAULT_PROFILES.process.read_text())
        self.assertEqual({k: v for k, v in merged.items() if k != "wall_loops"},
                         {k: v for k, v in original.items() if k != "wall_loops"})


class ConfigRoutesTest(JobRouteBase):
    def team_session(self, printing):
        with self.client.session_transaction() as sess:
            sess["team_number"] = 1234
            sess["using_default_config"] = False
            sess["team_config_data"] = team(printing)

    def test_route_page_answers_config_and_logs_config_event(self):
        self.team_session(dict(SPEC_PRINTING, processes=SPEC_PRINTING["processes"] + ["Bogus"]))
        resp = self.client.post("/print/page", json={"source": "onshape", "theme": "dark"})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["config"]["printer"], P4)
        self.assertEqual(data["config"]["processes"], SPEC_PRINTING["processes"])
        self.assertEqual(data["config"]["skipped"], ["Bogus"])
        self.assertEqual(data["overrides"], OVERRIDE_CHOICES)
        self.assertEqual(data["plate"]["printer"], P4)
        lines = [line for line in self.lines if line.startswith("[PRINT] config ")]
        self.assertEqual(lines, [
            f'[PRINT] config pid=abcdef012345 team=1234 job=- printer="{P4}" '
            'filaments="Bambu PLA Basic @BBL H2S,Bambu PETG HF @BBL H2S" '
            'processes="0.20mm Standard @BBL H2S,0.16mm High Quality @BBL H2S" skipped=Bogus error=-'])
        # Upload mode prints with the defaults, whatever the session holds (R8).
        data = self.client.post("/print/page", json={"source": "upload"}).get_json()
        self.assertTrue(data["config"]["is_default"])

    def test_route_page_unknown_printer_answers_error(self):
        self.team_session(dict(SPEC_PRINTING, printer="Nope"))
        data = self.client.post("/print/page", json={"source": "onshape"}).get_json()
        self.assertEqual(data["config"]["error"], UNKNOWN_PRINTER_MESSAGE.format(name="Nope"))

    def test_route_job_refuses_filament_not_offered(self):
        self.team_session(SPEC_PRINTING)
        copies = [copy(self.box.ref, 50, 50)]
        resp = self.post(copies, filament="Generic PETG @BBL H2S", process="0.20mm Standard @BBL H2S")
        self.assert_refused(resp, "Choose a filament and print settings from the list.")
        resp = self.post(copies, filament="Bambu PETG HF @BBL H2S", process="0.16mm High Quality @BBL H2S",
                         overrides={"infill": 40, "brim": "off"})
        self.assertEqual(resp.status_code, 202, resp.get_json())
        job = self.recorder.calls[0][1]
        self.assertEqual((job.filament, job.process), ("Bambu PETG HF @BBL H2S", "0.16mm High Quality @BBL H2S"))
        self.assertEqual(job.overrides, {"sparse_infill_density": "40%", "brim_type": "no_brim"})
        self.assertEqual(json.loads(job.profiles.printer.read_text())["name"], P4)

    def test_route_job_refuses_bad_overrides_and_config_error(self):
        self.team_session(dict(SPEC_PRINTING, allow_overrides=False))
        copies = [copy(self.box.ref, 50, 50)]
        resp = self.post(copies, filament="Bambu PLA Basic @BBL H2S", process="0.20mm Standard @BBL H2S",
                         overrides={"infill": 40})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"], print_config.OVERRIDES_NOT_ALLOWED_MESSAGE)
        self.team_session(dict(SPEC_PRINTING, printer="Nope"))
        resp = self.post(copies, filament="Bambu PLA Basic @BBL H2S", process="0.20mm Standard @BBL H2S")
        self.assert_refused(resp, UNKNOWN_PRINTER_MESSAGE.format(name="Nope"))

    def test_route_job_without_choice_uses_first_offered(self):
        self.team_session(SPEC_PRINTING)
        self.assertEqual(self.post([copy(self.box.ref, 50, 50)]).status_code, 202)
        job = self.recorder.calls[0][1]
        self.assertEqual((job.filament, job.process), ("Bambu PLA Basic @BBL H2S", "0.20mm Standard @BBL H2S"))


if __name__ == "__main__":
    unittest.main()

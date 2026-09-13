"""Tests for print/scripts/flatten_orca_profiles.py against a tiny fake profile tree."""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]        # the repository root
sys.path.insert(0, str(REPO / "print" / "scripts"))

from flatten_orca_profiles import flatten_profile, write_profiles, PROFILE_SOURCES  # noqa: E402


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


class FlattenProfileTest(unittest.TestCase):
    def setUp(self):
        self.tree = Path(tempfile.mkdtemp(prefix="orca-profiles-"))
        self.addCleanup(shutil.rmtree, self.tree, ignore_errors=True)
        _write(self.tree / "machine" / "fdm_machine_common.json",
               {"name": "fdm_machine_common", "type": "machine", "instantiation": "false",
                "printable_height": "100", "gcode_flavor": "marlin", "printable_area": ["0x0", "100x0", "100x100", "0x100"]})
        _write(self.tree / "machine" / "Family.json",
               {"name": "Family", "type": "machine", "inherits": "fdm_machine_common", "instantiation": "false",
                "printable_height": "250", "setting_id": "GM000"})
        _write(self.tree / "machine" / "Leaf 0.4 nozzle.json",
               {"name": "Leaf 0.4 nozzle", "type": "machine", "inherits": "Family", "from": "system",
                "instantiation": "true", "setting_id": "GM001", "nozzle_diameter": ["0.4"]})

    def test_child_keys_win_over_parent_keys(self):
        flat = flatten_profile(self.tree, "machine", "Leaf 0.4 nozzle")
        self.assertEqual(flat["printable_height"], "250")   # Family over common
        self.assertEqual(flat["gcode_flavor"], "marlin")     # inherited from the root
        self.assertEqual(flat["nozzle_diameter"], ["0.4"])   # leaf
        self.assertEqual(flat["name"], "Leaf 0.4 nozzle")

    def test_result_is_self_contained_system_profile(self):
        flat = flatten_profile(self.tree, "machine", "Leaf 0.4 nozzle")
        self.assertEqual(flat["inherits"], "")
        self.assertEqual(flat["from"], "system")
        self.assertNotIn("instantiation", flat)
        self.assertNotIn("setting_id", flat)

    def test_missing_profile_raises_with_its_name(self):
        with self.assertRaises(FileNotFoundError) as ctx:
            flatten_profile(self.tree, "machine", "No Such Printer")
        self.assertIn("No Such Printer", str(ctx.exception))

    def test_write_profiles_writes_sorted_indented_json(self):
        _write(self.tree / "filament" / "F.json", {"name": "F", "type": "filament", "filament_density": ["1.26"]})
        _write(self.tree / "process" / "P.json", {"name": "P", "type": "process", "layer_height": "0.2"})
        out = self.tree / "out"
        sources = {"printer": ("machine", "Leaf 0.4 nozzle"), "filament": ("filament", "F"), "process": ("process", "P")}
        write_profiles(self.tree, out, sources)
        text = (out / "printer.json").read_text()
        self.assertTrue(text.endswith("}\n"))
        self.assertEqual(json.loads(text), flatten_profile(self.tree, "machine", "Leaf 0.4 nozzle"))
        self.assertEqual(list(json.loads(text).keys()), sorted(json.loads(text).keys()))
        self.assertIn('"layer_height": "0.2"', (out / "process.json").read_text())

    def test_default_sources_are_the_h2s_set(self):
        self.assertEqual(PROFILE_SOURCES["printer"], ("machine", "Bambu Lab H2S 0.4 nozzle"))
        self.assertEqual(PROFILE_SOURCES["filament"], ("filament", "Bambu PLA Basic @BBL H2S"))
        self.assertEqual(PROFILE_SOURCES["process"], ("process", "0.20mm Standard @BBL H2S"))


if __name__ == "__main__":
    unittest.main()

"""Tests for print/scripts/flatten_orca_profiles.py against a tiny fake profile tree."""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]        # the repository root
sys.path.insert(0, str(REPO / "print" / "scripts"))

from flatten_orca_profiles import (  # noqa: E402
    EXTRUDER_VARIANT, H2S_PRINTERS, PROFILE_SOURCES, apply_selections, audit_rows, build_catalog,
    build_name_index, build_profiles, check_selection, flatten_indexed, flatten_profile,
    profile_file_stem, variant_index, write_catalog, write_profiles,
)

FAKE_TREE = REPO / "print" / "tests" / "fixtures" / "orca_tree"
P4, P8 = "Bambu Lab H2S 0.4 nozzle", "Bambu Lab H2S 0.8 nozzle"


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
                "instantiation": "true", "setting_id": "GM001", "nozzle_diameter": ["0.4"],
                "extruder_type": ["Direct Drive"],
                "printer_extruder_variant": ["Direct Drive Standard", "Direct Drive High Flow"],
                "nozzle_volume": ["145", "148"]})

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
        expected = apply_selections("machine", flatten_profile(self.tree, "machine", "Leaf 0.4 nozzle"))
        self.assertEqual(json.loads(text), expected)
        self.assertEqual(list(json.loads(text).keys()), sorted(json.loads(text).keys()))
        self.assertIn('"layer_height": "0.2"', (out / "process.json").read_text())

    def test_default_sources_are_the_h2s_0_6_set(self):
        self.assertEqual(PROFILE_SOURCES["printer"], ("machine", "Bambu Lab H2S 0.6 nozzle"))
        self.assertEqual(PROFILE_SOURCES["filament"], ("filament", "Generic PETG @BBL H2S"))
        self.assertEqual(PROFILE_SOURCES["process"],
                         ("process", "0.24mm Balanced Quality @BBL H2S 0.6 nozzle"))


class SelectionTest(unittest.TestCase):
    """The High Flow nozzle and the textured plate, which no vendor profile carries."""

    def test_apply_selections_writes_the_two_choices_into_the_machine(self):
        flat = apply_selections("machine", {"name": "M"})
        self.assertEqual(flat["nozzle_volume_type"], ["High Flow"])
        self.assertEqual(flat["curr_bed_type"], "Textured PEI Plate")
        self.assertEqual(flat["default_bed_type"], "4")   # atoi()-safe, see the script

    def test_apply_selections_leaves_filament_and_process_alone(self):
        for subdir in ("filament", "process"):
            self.assertEqual(apply_selections(subdir, {"name": "X"}), {"name": "X"})

    def test_variant_index_finds_high_flow(self):
        flat = {"name": "M", "printer_extruder_variant": ["Direct Drive Standard", EXTRUDER_VARIANT]}
        self.assertEqual(variant_index("machine", flat), 1)

    def test_variant_index_refuses_a_profile_without_the_variant(self):
        flat = {"name": "Single", "printer_extruder_variant": ["Direct Drive Standard"]}
        with self.assertRaises(ValueError) as ctx:
            variant_index("machine", flat)
        self.assertIn(EXTRUDER_VARIANT, str(ctx.exception))
        self.assertIn("Single", str(ctx.exception))

    def test_variant_index_refuses_a_profile_with_no_variant_array(self):
        with self.assertRaises(ValueError):
            variant_index("filament", {"name": "F"})

    def test_check_selection_rejects_a_mismatched_index(self):
        profiles = {
            "printer": {"name": "M", "extruder_type": ["Direct Drive"],
                        "nozzle_volume_type": ["High Flow"],
                        "printer_extruder_variant": ["Direct Drive Standard", EXTRUDER_VARIANT]},
            "filament": {"name": "F", "filament_extruder_variant": [EXTRUDER_VARIANT, "Direct Drive Standard"]},
            "process": {"name": "P", "print_extruder_variant": ["Direct Drive Standard", EXTRUDER_VARIANT]},
        }
        with self.assertRaises(ValueError) as ctx:
            check_selection(profiles)
        self.assertIn("different indexes", str(ctx.exception))


class AuditTest(unittest.TestCase):
    def test_audit_reports_the_high_flow_element_and_its_source(self):
        profiles = {
            "printer": {"name": "M", "nozzle_diameter": ["0.6"],
                        "nozzle_type": ["hardened_steel", "hardened_steel"],
                        "nozzle_volume_type": ["High Flow"], "nozzle_volume": ["145", "148"],
                        "curr_bed_type": "Textured PEI Plate",
                        "printer_extruder_variant": ["Direct Drive Standard", EXTRUDER_VARIANT]},
            "filament": {"name": "F", "filament_type": ["PETG"],
                         "filament_max_volumetric_speed": ["12", "12"],
                         "nozzle_temperature": ["250", "255"],
                         "nozzle_temperature_initial_layer": ["250", "255"],
                         "filament_flow_ratio": ["0.95", "0.95"],
                         "textured_plate_temp": ["70"], "textured_plate_temp_initial_layer": ["70"],
                         "filament_extruder_variant": ["Direct Drive Standard", EXTRUDER_VARIANT]},
            "process": {"name": "P", "layer_height": "0.24", "initial_layer_print_height": "0.3",
                        "print_extruder_variant": ["Direct Drive Standard", EXTRUDER_VARIANT]},
        }
        provenance = {"printer": {"nozzle_volume": "Machine Base"}, "filament": {}, "process": {}}
        rows = dict((label, (value, source)) for label, value, source in audit_rows(profiles, provenance, 1))
        self.assertEqual(rows["nozzle volume"], ("148 mm3", "Machine Base"))   # index 1, not 145
        self.assertEqual(rows["nozzle temperature"][0], "255 C")
        self.assertEqual(rows["filament MVS"][0], "12 mm3/s")
        self.assertEqual(rows["bed type"][0], "Textured PEI Plate")
        self.assertEqual(rows["nozzle diameter"][0], "0.6 mm")
        self.assertIn("index 1 of 2", rows["extruder variant"][0])


class CatalogTest(unittest.TestCase):
    """The catalog over print/tests/fixtures/orca_tree, a BBL tree and a filament library."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="orca-catalog-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.tree = self.tmp / "profiles"
        shutil.copytree(FAKE_TREE, self.tree)
        self.bbl, self.library = self.tree / "BBL", self.tree / "OrcaFilamentLibrary"

    def catalog(self):
        return build_catalog(self.bbl, self.library, printers=[P4, P8])

    def test_index_walks_subfolders_and_library(self):
        index, _shadowed = build_name_index([self.bbl, self.library], "filament")
        self.assertEqual(index["AliZ PLA @BBL H2S"], self.bbl / "filament" / "AliZ" / "AliZ PLA @BBL H2S.json")
        self.assertEqual(index["AliZ PLA @base"], self.library / "filament" / "base" / "AliZ PLA @base.json")
        self.assertIn("Generic PETG @System", index)

    def test_duplicate_inside_one_tree_fails(self):
        _write(self.bbl / "filament" / "Copy" / "again.json",
               {"type": "filament", "name": "Generic PLA @base", "instantiation": "false"})
        with self.assertRaises(ValueError) as ctx:
            build_name_index([self.bbl, self.library], "filament")
        self.assertIn("Generic PLA @base.json", str(ctx.exception))
        self.assertIn("again.json", str(ctx.exception))

    def test_bbl_name_shadows_library_name(self):
        index, shadowed = build_name_index([self.bbl, self.library], "filament")
        self.assertEqual(index["Bambu ABS @base"], self.bbl / "filament" / "Bambu ABS @base.json")
        self.assertEqual(shadowed, 2)          # Bambu ABS @base and fdm_filament_common
        flat = self.catalog()["profiles"]["filament"]["Bambu ABS @BBL H2S"]
        self.assertEqual(flat["nozzle_temperature"], ["260"])   # the BBL copy, not the library's 250
        self.assertEqual(flat["filament_density"], ["1.24"])

    def test_bbl_filament_inherits_library_base(self):
        index, _ = build_name_index([self.bbl, self.library], "filament")
        flat = flatten_indexed(index, "filament", "AliZ PLA @BBL H2S")
        self.assertEqual(flat["nozzle_temperature"], ["215"])  # from the library base
        self.assertEqual(flat["filament_vendor"], ["AliZ"])
        self.assertEqual(flat["inherits"], "")
        self.assertEqual(flat["from"], "system")
        self.assertNotIn("instantiation", flat)
        self.assertNotIn("setting_id", flat)

    def test_profile_without_high_flow_skipped_with_reason(self):
        catalog = self.catalog()
        self.assertNotIn("addnorth PLA @BBL H2S", catalog["profiles"]["filament"])
        skipped = {(s["kind"], s["name"]): s["reason"] for s in catalog["index"]["skipped"]}
        self.assertIn(EXTRUDER_VARIANT, skipped[("filament", "addnorth PLA @BBL H2S")])

    def test_mismatched_variant_index_dropped_per_printer(self):
        catalog = self.catalog()
        entries = {e["name"]: e for e in catalog["index"]["filaments"]}
        # High Flow is index 1 on the filament and the 0.4 nozzle, index 0 on the 0.8.
        self.assertEqual(entries["Generic PLA @BBL H2S"]["compatible_printers"], [P4])
        dropped = [(d["printer"], d["kind"], d["name"]) for d in catalog["index"]["dropped"]]
        self.assertIn((P8, "filament", "Generic PLA @BBL H2S"), dropped)
        self.assertNotIn((P4, "filament", "Generic PLA @BBL H2S"), dropped)
        processes = {e["name"]: e["compatible_printers"] for e in catalog["index"]["processes"]}
        self.assertEqual(processes["0.40mm Standard @BBL H2S 0.8 nozzle"], [P8])

    def test_library_bases_never_listed(self):
        catalog = self.catalog()
        names = set(catalog["profiles"]["filament"])
        self.assertEqual(names, {"Generic PLA @BBL H2S", "Bambu ABS @BBL H2S", "AliZ PLA @BBL H2S"})
        self.assertNotIn("Generic PETG @System", names)
        self.assertNotIn("Other PLA @BBL X1C", names)   # names no H2S printer

    def test_index_json_lists_skipped_and_dropped(self):
        out = self.tmp / "catalog"
        write_catalog(self.catalog(), out)
        index = json.loads((out / "index.json").read_text())
        self.assertEqual(set(index), {"orca_version", "printers", "filaments", "processes", "skipped", "dropped"})
        self.assertEqual(index["printers"], [{"name": P4, "file": "printer/Bambu_Lab_H2S_0.4_nozzle.json"},
                                             {"name": P8, "file": "printer/Bambu_Lab_H2S_0.8_nozzle.json"}])
        self.assertEqual(index["skipped"], [{"kind": "filament", "name": "addnorth PLA @BBL H2S",
                                             "reason": index["skipped"][0]["reason"]}])
        self.assertEqual(index["dropped"][0]["name"], "Generic PLA @BBL H2S")
        aliz = next(e for e in index["filaments"] if e["name"] == "AliZ PLA @BBL H2S")
        self.assertEqual(aliz["file"], "filament/AliZ_PLA__BBL_H2S.json")
        text = (out / aliz["file"]).read_text()
        self.assertEqual(json.loads(text)["name"], "AliZ PLA @BBL H2S")
        self.assertEqual(list(json.loads(text)), sorted(json.loads(text)))
        printer = json.loads((out / "printer" / "Bambu_Lab_H2S_0.8_nozzle.json").read_text())
        self.assertEqual(printer["nozzle_volume_type"], ["High Flow"])   # apply_selections ran

    def test_profile_file_stem_and_collisions(self):
        self.assertEqual(profile_file_stem("0.20mm Standard @BBL H2S"), "0.20mm_Standard__BBL_H2S")
        _write(self.bbl / "filament" / "Clash" / "a.json",
               {"type": "filament", "name": "Generic PLA_@BBL H2S", "inherits": "Generic PLA @base",
                "instantiation": "true", "filament_extruder_variant": ["Direct Drive Standard", EXTRUDER_VARIANT],
                "compatible_printers": [P4]})
        with self.assertRaises(ValueError) as ctx:
            self.catalog()
        self.assertIn("Generic_PLA__BBL_H2S", str(ctx.exception))

    def test_default_printer_list_is_the_h2s_family(self):
        self.assertEqual(H2S_PRINTERS, ["Bambu Lab H2S 0.2 nozzle", P4, "Bambu Lab H2S 0.6 nozzle", P8])


if __name__ == "__main__":
    unittest.main()

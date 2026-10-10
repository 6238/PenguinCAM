"""Slow tests that run the real Orca Slicer binary. Not matched by discover's
test_*.py pattern; `make test` runs this module explicitly."""
import filecmp
import json
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]        # the repository root
ORCA = REPO / "tools" / "orca" / "orca-slicer"

sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "print" / "scripts"))

from print.slicer import SAMPLE_STL, PROFILE_DIR, slice_stl  # noqa: E402
from flatten_orca_profiles import (  # noqa: E402
    CATALOG_OUT, bbl_profile_dir, build_catalog, library_profile_dir, write_catalog, write_profiles,
)


class OrcaInstalledTest(unittest.TestCase):
    def test_wrapper_exists_and_runs(self):
        self.assertTrue(ORCA.is_file(), f"{ORCA} missing: run `make install`")
        # Orca writes a result.json into the working directory on every run,
        # even for --help, so run it somewhere disposable.
        with tempfile.TemporaryDirectory() as work:
            result = subprocess.run([str(ORCA), "--help"], cwd=work,
                                    capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        self.assertIn("--export-3mf", result.stdout)

    def test_version_stamp_matches_pinned_release(self):
        stamp = (REPO / "tools" / "orca" / "VERSION").read_text().split()
        self.assertEqual(stamp[0], "2.4.2")
        self.assertIn(stamp[1], {"linux-x86_64", "linux-aarch64", "darwin-arm64"})


class RealSliceTest(unittest.TestCase):
    """Slices the fixed sample part with the pinned Orca. Slow by design."""

    def test_sample_part_slices_to_a_valid_archive(self):
        out = Path(tempfile.mkdtemp(prefix="orca-slice-")).resolve()
        self.addCleanup(shutil.rmtree, out, ignore_errors=True)
        started = time.monotonic()
        result = slice_stl(SAMPLE_STL, out)
        elapsed = time.monotonic() - started
        print(f"\n[orca] sliced {SAMPLE_STL.name} in {elapsed:.1f} s")
        self.assertEqual(result.output_path, out / "sample_part.gcode.3mf")
        self.assertTrue(zipfile.is_zipfile(result.output_path))
        with zipfile.ZipFile(result.output_path) as zf:
            names = zf.namelist()
        self.assertIn("Metadata/plate_1.gcode", names)
        self.assertIn("Metadata/slice_info.config", names)
        self.assertGreater(result.print_time_s, 0)
        self.assertGreater(result.filament_g, 0)
        self.assertGreater(result.filament_m, 0)
        self.assertGreater(result.layer_count, 0)
        # 12 mm: a 0.3 mm initial layer then ceil(11.7 / 0.24) = 49 more.
        self.assertEqual(result.layer_count, 50)
        self.assertIsInstance(result.warnings, list)

    def test_sliced_output_resolved_the_high_flow_nozzle_and_textured_plate(self):
        """The proof that the variant selection reached Orca, not just our JSON.

        Orca writes the resolved configuration into the G-code as comments. The
        *_extruder_variant lines and nozzle_volume are the tell: with no
        nozzle_volume_type Orca falls back to Standard, and nozzle_volume would
        read 145 rather than 148.
        """
        out = Path(tempfile.mkdtemp(prefix="orca-variant-"))
        self.addCleanup(shutil.rmtree, out, ignore_errors=True)
        result = slice_stl(SAMPLE_STL, out)
        with zipfile.ZipFile(result.output_path) as zf:
            gcode = zf.read("Metadata/plate_1.gcode").decode("utf-8", "replace")
            plate = json.loads(zf.read("Metadata/plate_1.json").decode("utf-8"))

        def setting(key):
            for line in gcode.splitlines():
                if line.startswith(f"; {key} = "):
                    return line.split(" = ", 1)[1].strip()
            self.fail(f"{key} not found in the sliced G-code")

        self.assertEqual(setting("nozzle_volume_type"), "High Flow")
        for key in ("printer_extruder_variant", "filament_extruder_variant", "print_extruder_variant"):
            self.assertEqual(setting(key), '"Direct Drive High Flow"', key)
        self.assertEqual(setting("nozzle_volume"), "148")          # index 1; Standard is 145
        self.assertEqual(setting("nozzle_flush_dataset"), "2")     # index 1; Standard is 1
        self.assertEqual(setting("nozzle_diameter"), "0.6")
        self.assertEqual(setting("nozzle_type"), "hardened_steel")
        self.assertEqual(setting("layer_height"), "0.24")
        self.assertEqual(setting("filament_type"), "PETG")
        self.assertEqual(setting("filament_max_volumetric_speed"), "12")
        self.assertEqual(setting("nozzle_temperature"), "255")
        self.assertEqual(setting("curr_bed_type"), "Textured PEI Plate")
        self.assertEqual(setting("textured_plate_temp"), "70")
        self.assertEqual(plate["bed_type"], "textured_plate")
        # The textured-plate temperature is what actually reaches the machine.
        self.assertIn("M190 S70", gcode)


class ProfileDriftTest(unittest.TestCase):
    """The checked-in profiles must equal what the flattening script produces
    from the installed Orca, so an Orca upgrade forces a regeneration."""

    def test_checked_in_profiles_match_installed_orca(self):
        out = Path(tempfile.mkdtemp(prefix="orca-profiles-"))
        self.addCleanup(shutil.rmtree, out, ignore_errors=True)
        write_profiles(bbl_profile_dir(), out)
        for name in ("printer.json", "filament.json", "process.json"):
            self.assertTrue(filecmp.cmp(out / name, PROFILE_DIR / name, shallow=False),
                            f"{name} differs from the installed Orca's profile tree; "
                            f"run `uv run python print/scripts/flatten_orca_profiles.py`")


class CatalogTest(unittest.TestCase):
    """The committed catalog must equal a rebuild from the installed Orca."""

    @classmethod
    def setUpClass(cls):
        cls.catalog = build_catalog(bbl_profile_dir(), library_profile_dir())

    def test_catalog_matches_installed_orca(self):
        out = Path(tempfile.mkdtemp(prefix="orca-catalog-"))
        self.addCleanup(shutil.rmtree, out, ignore_errors=True)
        write_catalog(self.catalog, out)
        built = sorted(p.relative_to(out).as_posix() for p in out.rglob("*.json"))
        committed = sorted(p.relative_to(CATALOG_OUT).as_posix() for p in CATALOG_OUT.rglob("*.json"))
        hint = "the catalog differs from the installed Orca; run `uv run python print/scripts/flatten_orca_profiles.py`"
        self.assertEqual(built, committed, hint)
        for rel in built:
            self.assertTrue(filecmp.cmp(out / rel, CATALOG_OUT / rel, shallow=False), f"{rel}: {hint}")

    def test_skipped_list_matches_index(self):
        committed = json.loads((CATALOG_OUT / "index.json").read_text())
        self.assertEqual([(s["kind"], s["name"]) for s in self.catalog["index"]["skipped"]],
                         [(s["kind"], s["name"]) for s in committed["skipped"]])
        self.assertTrue(committed["skipped"])           # the addnorth family, today


class PlateSliceTest(unittest.TestCase):
    """Plates written by print/plate_3mf.py and sliced with --arrange 0 --orient 0."""

    def setUp(self):
        from print.tests.test_plate_3mf import make_part
        self.tmp = Path(tempfile.mkdtemp(prefix="orca-plate-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.stls = self.tmp / "stls"
        self.stls.mkdir()
        self.make_part = lambda *args, **kwargs: make_part(self.stls, *args, **kwargs)

    def write(self, copies, parts):
        from print.plate_3mf import write_plate_3mf
        model = self.tmp / "plate.3mf"
        return model, write_plate_3mf(model, copies, parts)

    def test_two_part_placement_slices_with_every_name(self):
        from print.plate_3mf import plate_object_names
        from print.slicer import DEFAULT_PROFILES, slice_plate
        from print.tests.test_plate_3mf import cylinder_part
        parts = {"a": self.make_part("Bracket", (30, 12, 6)), "b": cylinder_part(self.stls, "Spacer", 8, 10)}
        copies = [{"ref": "a", "orientation": "+z", "angle": 0, "x": 80.0, "y": 80.0},
                  {"ref": "a", "orientation": "+x", "angle": 45, "x": 150.0, "y": 80.0},
                  {"ref": "b", "orientation": "+z", "angle": 0, "x": 220.0, "y": 200.0}]
        model, names = self.write(copies, parts)
        result = slice_plate(model, DEFAULT_PROFILES, self.tmp / "out")
        self.assertEqual(sorted(plate_object_names(result.output_path)), sorted(names))
        self.assertEqual(names, ["Bracket #1", "Bracket #2", "Spacer #1"])
        self.assertGreater(result.layer_count, 0)

    def test_rotated_footprint_matches_without_brim(self):
        from print.plate import copy_hull
        from print.slicer import PROFILE_DIR, SliceProfiles, slice_plate
        process = json.loads((PROFILE_DIR / "process.json").read_text())
        process["brim_type"] = "no_brim"
        (self.tmp / "process.json").write_text(json.dumps(process, indent=4))
        profiles = SliceProfiles(PROFILE_DIR / "printer.json", PROFILE_DIR / "filament.json",
                                 self.tmp / "process.json")
        parts = {"a": self.make_part("Bar", (40, 10, 10))}
        copy = {"ref": "a", "orientation": "+z", "angle": 30, "x": 150.0, "y": 150.0}
        model, _names = self.write([copy], parts)
        result = slice_plate(model, profiles, self.tmp / "out")
        with zipfile.ZipFile(result.output_path) as zf:
            plate = json.loads(zf.read("Metadata/plate_1.json").decode("utf-8"))
        bbox = plate["bbox_objects"][0]["bbox"]
        hull = copy_hull(copy, parts["a"]["footprints"]["+z"])
        expected = [min(p[0] for p in hull), min(p[1] for p in hull), max(p[0] for p in hull), max(p[1] for p in hull)]
        for got, want in zip(bbox, expected):
            self.assertAlmostEqual(got, want, delta=0.5)

    def test_overlap_maps_to_too_close_message(self):
        from print.slicer import DEFAULT_PROFILES, SliceError, slice_plate
        parts = {"a": self.make_part("Box", (20, 20, 10))}
        copies = [{"ref": "a", "orientation": "+z", "angle": 0, "x": 100.0, "y": 100.0},
                  {"ref": "a", "orientation": "+z", "angle": 0, "x": 110.0, "y": 100.0}]
        model, _names = self.write(copies, parts)
        with self.assertRaises(SliceError) as ctx:
            slice_plate(model, DEFAULT_PROFILES, self.tmp / "out")
        self.assertEqual(ctx.exception.code, -101)
        self.assertEqual(ctx.exception.message, "Parts are too close together; spread them out.")


class TeamProfileSliceTest(unittest.TestCase):
    """A team's catalog profiles, not the defaults, with all four overrides, through run_plate_job."""

    def test_profile_with_overrides_slices(self):
        from print.plate_job import JobPart, PlateJob, run_plate_job
        from print.print_config import Catalog, resolve_print_config, validate_overrides
        from print.tests.test_plate_3mf import make_part
        tmp = Path(tempfile.mkdtemp(prefix="orca-team-profile-")).resolve()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (tmp / "stls").mkdir()
        printing = {"printer": "Bambu Lab H2S 0.4 nozzle",
                    "filaments": ["Bambu PLA Basic @BBL H2S", "Bambu PETG HF @BBL H2S"],
                    "processes": ["0.20mm Standard @BBL H2S", "0.16mm High Quality @BBL H2S"]}
        choices = resolve_print_config(printing, Catalog.load())
        self.assertIsNone(choices.error)
        overrides = validate_overrides({"infill": 40, "walls": 4, "supports": "on", "brim": "off"}, True)
        part = make_part(tmp / "stls", "Box", (20, 20, 10))
        job = PlateJob(copies=[{"ref": "a", "orientation": "+z", "angle": 0, "x": 100.0, "y": 100.0}],
                       parts={"a": JobPart(part["name"], part["stl_path"], part["footprints"], 12)},
                       profiles=choices.slice_profiles("Bambu PETG HF @BBL H2S", "0.16mm High Quality @BBL H2S"),
                       overrides=overrides, filament="Bambu PETG HF @BBL H2S",
                       process="0.16mm High Quality @BBL H2S", delivered_name="Box-20261009-1432.gcode.3mf",
                       pid=None, team=None)
        scratch = tmp / "scratch"
        scratch.mkdir()
        result = run_plate_job(job, scratch)
        with zipfile.ZipFile(result.output_path) as zf:
            gcode = zf.read("Metadata/plate_1.gcode").decode("utf-8", "replace")
        settings = dict(line[2:].split(" = ", 1) for line in gcode.splitlines()
                        if line.startswith("; ") and " = " in line)
        self.assertEqual(settings["sparse_infill_density"], "40%")
        self.assertEqual(settings["wall_loops"], "4")
        self.assertEqual(settings["enable_support"], "1")
        self.assertEqual(settings["support_type"], "tree(auto)")
        self.assertEqual(settings["brim_type"], "no_brim")
        self.assertEqual(settings["nozzle_diameter"], "0.4")
        self.assertEqual(settings["layer_height"], "0.16")
        self.assertEqual(settings["filament_settings_id"], '"Bambu PETG HF @BBL H2S"')


class PlateJobTest(unittest.TestCase):
    def test_plate_job_end_to_end(self):
        """Two synthetic parts in a temporary part store, through run_plate_job and real Orca."""
        from print.part_store import PartStore
        from print.plate_3mf import plate_object_names
        from print.plate_job import JobPart, PlateJob, run_plate_job
        from print.slicer import DEFAULT_PROFILES
        from print.tests.test_routes_jobs import store_part
        tmp = Path(tempfile.mkdtemp(prefix="orca-plate-job-")).resolve()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        store = PartStore(tmp / "parts")
        box = store_part(store, "abcdef012345", "Box", (20, 20, 10))
        peg = store_part(store, "abcdef012345", "Peg", (8, 8, 30))
        copies = [{"ref": box.ref, "orientation": "+z", "angle": 0, "x": 60.0, "y": 60.0},
                  {"ref": box.ref, "orientation": "-z", "angle": 30, "x": 120.0, "y": 60.0},
                  {"ref": peg.ref, "orientation": "+x", "angle": 90, "x": 200.0, "y": 150.0}]
        job = PlateJob(copies=copies,
                       parts={e.ref: JobPart(e.name, e.stl_path, e.footprints, e.triangles) for e in (box, peg)},
                       profiles=DEFAULT_PROFILES, overrides={}, filament="PETG", process="0.24mm",
                       delivered_name="Box_plus1-20261009-1432.gcode.3mf", pid="abcdef012345", team=None)
        scratch = tmp / "scratch"
        scratch.mkdir()
        result = run_plate_job(job, scratch)
        self.assertEqual(sorted(plate_object_names(result.output_path)), ["Box #1", "Box #2", "Peg #1"])
        self.assertGreater(result.layer_count, 0)


class RealJobRouteTest(unittest.TestCase):
    """POST /print-job with the real pool and Orca, then read the result and download it."""

    def test_job_runs_to_done_and_download_is_a_zip(self):
        from frc_cam_gui_app import app, limiter
        self.addCleanup(app.config.__setitem__, "TESTING", app.config["TESTING"])
        app.config["TESTING"] = True
        limiter.reset()
        client = app.test_client()
        client.environ_base["HTTP_X_PRINT_PAGE"] = "abcdef012345"   # the print page's page id
        with client.session_transaction() as sess:
            sess["app_verified"] = True
        resp = client.post("/print-job")
        self.assertEqual(resp.status_code, 202, resp.get_json())
        job_id = resp.get_json()["job_id"]
        # The poll loop outruns the app's 200/hour default limit on a slow slice,
        # and a 429 has no JSON body to read a state out of.
        limiter.enabled = False
        self.addCleanup(setattr, limiter, "enabled", True)
        deadline = time.monotonic() + 130
        while True:
            data = client.get(f"/print-job/{job_id}").get_json()
            if data["state"] in ("done", "failed"):
                break
            self.assertLess(time.monotonic(), deadline, "job did not finish")
            time.sleep(0.2)
        self.assertEqual(data["state"], "done", data)
        self.assertGreater(data["summary"]["layer_count"], 0)
        resp = client.get(f"/download/{data['token']}")
        self.addCleanup(resp.close)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, "application/octet-stream")
        self.assertIn("sample_part.gcode.3mf", resp.headers["Content-Disposition"])
        self.assertTrue(resp.data.startswith(b"PK"))


if __name__ == "__main__":
    unittest.main()

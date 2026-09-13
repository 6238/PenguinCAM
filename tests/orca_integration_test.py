"""Slow tests that run the real Orca Slicer binary. Not matched by discover's
test_*.py pattern; `make test` runs this module explicitly."""
import filecmp
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ORCA = REPO / "tools" / "orca" / "orca-slicer"

sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from print_slicer import SAMPLE_STL, PROFILE_DIR, slice_stl  # noqa: E402
from flatten_orca_profiles import bbl_profile_dir, write_profiles  # noqa: E402


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
        out = Path(tempfile.mkdtemp(prefix="orca-slice-"))
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
        self.assertEqual(result.layer_count, 60)   # 12 mm at 0.2 mm layers
        self.assertEqual(result.warnings, [])


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
                            f"run `uv run python scripts/flatten_orca_profiles.py`")


class RealJobRouteTest(unittest.TestCase):
    """POST /print-job with the real pool and Orca, then read the result and download it."""

    def test_job_runs_to_done_and_download_is_a_zip(self):
        from frc_cam_gui_app import app, limiter
        app.config["TESTING"] = True
        limiter.reset()
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["app_verified"] = True
        job_id = client.post("/print-job").get_json()["job_id"]
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
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, "application/octet-stream")
        self.assertIn("sample_part.gcode.3mf", resp.headers["Content-Disposition"])
        self.assertTrue(resp.data.startswith(b"PK"))


if __name__ == "__main__":
    unittest.main()

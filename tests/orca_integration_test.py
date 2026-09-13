"""Slow tests that run the real Orca Slicer binary. Not matched by discover's
test_*.py pattern; `make test` runs this module explicitly."""
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ORCA = REPO / "tools" / "orca" / "orca-slicer"


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


if __name__ == "__main__":
    unittest.main()

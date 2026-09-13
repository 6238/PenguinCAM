"""Fast tests for print_slicer.py. Orca is never run: subprocess.run is replaced."""
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
FIXTURES = REPO / "tests" / "fixtures" / "print"

import print_slicer  # noqa: E402
from print_slicer import SliceError, build_command, part_info, read_summary, slice_stl, stl_bounds  # noqa: E402


def make_archive(path, gcode_header=True, slice_info=True):
    with zipfile.ZipFile(path, "w") as zf:
        if gcode_header:
            zf.writestr("Metadata/plate_1.gcode", (FIXTURES / "plate_1_header.gcode").read_text())
        if slice_info:
            zf.writestr("Metadata/slice_info.config", (FIXTURES / "slice_info.config").read_text())
        zf.writestr("[Content_Types].xml", "<Types/>")


def fake_run_factory(returncode=0, write_archive=True, write_result_json=True, stdout="log line\n", raise_timeout=False):
    """Return a stand-in for subprocess.run that behaves like Orca's exit."""
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        if raise_timeout:
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))
        outdir = Path(cmd[cmd.index("--outputdir") + 1])
        name = cmd[cmd.index("--export-3mf") + 1]
        if write_archive:
            make_archive(outdir / name)
        if write_result_json:
            shutil.copy(FIXTURES / "result.json", outdir / "result.json")
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr="")

    fake_run.calls = calls
    return fake_run


class CommandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="print-slicer-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.out = self.tmp / "out"

    def test_command_uses_fixed_binary_profiles_and_flags(self):
        cmd = build_command(print_slicer.SAMPLE_STL, self.out)
        self.assertEqual(Path(cmd[0]), print_slicer.ORCA_BIN)
        self.assertTrue(str(print_slicer.ORCA_BIN).endswith("tools/orca/orca-slicer"))
        self.assertIn("--load-settings", cmd)
        settings = cmd[cmd.index("--load-settings") + 1]
        self.assertEqual(settings, f"{print_slicer.PROFILE_DIR / 'printer.json'};{print_slicer.PROFILE_DIR / 'process.json'}")
        self.assertEqual(cmd[cmd.index("--load-filaments") + 1], str(print_slicer.PROFILE_DIR / "filament.json"))
        self.assertEqual(cmd[cmd.index("--arrange") + 1], "1")
        self.assertEqual(cmd[cmd.index("--slice") + 1], "0")
        self.assertEqual(cmd[cmd.index("--export-3mf") + 1], "sample_part.gcode.3mf")
        self.assertEqual(cmd[cmd.index("--outputdir") + 1], str(self.out))
        self.assertEqual(cmd[cmd.index("--datadir") + 1], str(self.out / "datadir"))
        self.assertEqual(cmd[-1], str(print_slicer.SAMPLE_STL))

    def test_slice_creates_output_dir_and_isolated_xdg_environment(self):
        fake = fake_run_factory()
        with mock.patch.object(print_slicer.subprocess, "run", fake):
            result = slice_stl(print_slicer.SAMPLE_STL, self.out)
        self.assertTrue(self.out.is_dir())
        cmd, kwargs = fake.calls[0]
        env = kwargs["env"]
        self.assertEqual(env["XDG_CONFIG_HOME"], str(self.out / "xdg-config"))
        self.assertEqual(env["XDG_RUNTIME_DIR"], str(self.out / "xdg-runtime"))
        self.assertEqual(env["LC_ALL"], "C")
        self.assertTrue((self.out / "xdg-runtime").is_dir())
        self.assertEqual(oct((self.out / "xdg-runtime").stat().st_mode & 0o777), "0o700")
        self.assertEqual(kwargs["timeout"], 120)
        self.assertEqual(result.output_path, self.out / "sample_part.gcode.3mf")

    def test_summary_from_archive_and_result_json(self):
        fake = fake_run_factory()
        with mock.patch.object(print_slicer.subprocess, "run", fake):
            result = slice_stl(print_slicer.SAMPLE_STL, self.out)
        self.assertEqual(result.print_time_s, 790.0)
        self.assertEqual(result.filament_g, 3.31)
        self.assertEqual(result.filament_m, 1.09)
        self.assertEqual(result.layer_count, 50)
        self.assertEqual(result.warnings, [])
        self.assertEqual(result.summary(), {"print_time_s": 790.0, "filament_m": 1.09, "filament_g": 3.31,
                                            "layer_count": 50, "warnings": []})

    def test_warnings_come_from_result_json(self):
        fake = fake_run_factory(write_result_json=False)
        with mock.patch.object(print_slicer.subprocess, "run", fake):
            result = slice_stl(print_slicer.SAMPLE_STL, self.out)
        self.assertEqual(result.warnings, [])   # macOS writes no result.json
        self.out.mkdir(exist_ok=True)
        make_archive(self.out / "x.gcode.3mf")
        data = json.loads((FIXTURES / "result.json").read_text())
        data["sliced_plates"][0]["warning_message"] = "Object is out of the bed"
        (self.out / "result.json").write_text(json.dumps(data))
        summary = read_summary(self.out / "x.gcode.3mf", self.out)
        self.assertEqual(summary["warnings"], ["Object is out of the bed"])

    def test_nonzero_exit_raises_with_last_twenty_lines(self):
        stdout = "".join(f"line {i}\n" for i in range(30))
        fake = fake_run_factory(returncode=255, write_archive=False, stdout=stdout)
        with mock.patch.object(print_slicer.subprocess, "run", fake):
            with self.assertRaises(SliceError) as ctx:
                slice_stl(print_slicer.SAMPLE_STL, self.out)
        self.assertNotIn("line 9\n", ctx.exception.details + "\n")
        self.assertIn("line 10", ctx.exception.details)
        self.assertIn("line 29", ctx.exception.details)
        self.assertEqual(len(ctx.exception.details.splitlines()), 20)
        self.assertTrue(ctx.exception.message.endswith("."))
        self.assertNotIn("line 29", ctx.exception.message)

    def test_timeout_raises_student_message(self):
        fake = fake_run_factory(raise_timeout=True)
        with mock.patch.object(print_slicer.subprocess, "run", fake):
            with self.assertRaises(SliceError) as ctx:
                slice_stl(print_slicer.SAMPLE_STL, self.out, timeout_s=7)
        self.assertEqual(ctx.exception.message, "The slicer took too long on this part.")
        self.assertEqual(fake.calls[0][1]["timeout"], 7)

    def test_missing_archive_after_exit_zero_raises(self):
        fake = fake_run_factory(returncode=0, write_archive=False)
        with mock.patch.object(print_slicer.subprocess, "run", fake):
            with self.assertRaises(SliceError) as ctx:
                slice_stl(print_slicer.SAMPLE_STL, self.out)
        self.assertIn("no output", ctx.exception.message.lower())

    def test_archive_without_gcode_entry_names_contents(self):
        self.out.mkdir()
        make_archive(self.out / "p.gcode.3mf", gcode_header=False)
        with self.assertRaises(SliceError) as ctx:
            read_summary(self.out / "p.gcode.3mf", self.out)
        self.assertIn("Metadata/slice_info.config", ctx.exception.details)
        self.assertIn("[Content_Types].xml", ctx.exception.details)

    def test_missing_summary_fields_are_none(self):
        self.out.mkdir()
        make_archive(self.out / "p.gcode.3mf", slice_info=False)
        summary = read_summary(self.out / "p.gcode.3mf", self.out)
        self.assertIsNone(summary["print_time_s"])
        self.assertIsNone(summary["filament_g"])
        self.assertIsNone(summary["filament_m"])
        self.assertEqual(summary["layer_count"], 50)


class PartInfoTest(unittest.TestCase):
    def test_bounds_of_binary_stl(self):
        tri = struct.pack("<12fH", 0, 0, 1, 0, 0, 0, 3, 0, 0, 0, 5, 0, 0)
        tri2 = struct.pack("<12fH", 0, 0, 1, -1, 0, 2, 3, 0, 0, 0, 5, 0, 0)
        data = b"\0" * 80 + struct.pack("<I", 2) + tri + tri2
        lo, hi, count = stl_bounds(data)
        self.assertEqual((lo, hi, count), ((-1.0, 0.0, 0.0), (3.0, 5.0, 2.0), 2))

    def test_bounds_of_ascii_stl(self):
        text = ("solid t\n facet normal 0 0 1\n  outer loop\n   vertex 0 0 0\n   vertex 10 0 0\n"
                "   vertex 0 20 5\n  endloop\n endfacet\nendsolid t\n")
        lo, hi, count = stl_bounds(text.encode())
        self.assertEqual((lo, hi, count), ((0.0, 0.0, 0.0), (10.0, 20.0, 5.0), 1))

    def test_part_info_reads_sample_part_and_profiles(self):
        info = part_info()
        self.assertEqual(info["part"]["name"], "sample_part")
        self.assertEqual(info["part"]["file"], "sample_part.stl")
        self.assertEqual(info["part"]["size_mm"], {"x": 40.0, "y": 30.0, "z": 12.0})
        self.assertEqual(info["part"]["triangles"], 20)
        self.assertEqual(info["printer"]["name"], "Bambu Lab X1 Carbon 0.4 nozzle")
        self.assertEqual(info["printer"]["bed_mm"], {"x": 256.0, "y": 256.0})
        self.assertEqual(info["printer"]["height_mm"], 250.0)
        self.assertEqual(info["filament"]["name"], "Bambu PLA Basic @BBL X1C")
        self.assertEqual(info["process"]["name"], "0.20mm Standard @BBL X1C")
        self.assertEqual(info["process"]["layer_height_mm"], 0.2)


class ProfileFilesTest(unittest.TestCase):
    REQUIRED = {
        "printer.json": ("name", "printable_area", "printable_height"),
        "filament.json": ("name", "filament_density", "compatible_printers"),
        "process.json": ("name", "layer_height", "compatible_printers"),
    }

    def test_profiles_are_flat_and_carry_required_keys(self):
        for fname, keys in self.REQUIRED.items():
            data = json.loads((print_slicer.PROFILE_DIR / fname).read_text())
            self.assertEqual(data.get("inherits"), "", fname)
            self.assertEqual(data.get("from"), "system", fname)
            for key in keys:
                self.assertIn(key, data, f"{fname} lacks {key}")

    def test_process_and_filament_are_compatible_with_the_printer(self):
        printer = json.loads((print_slicer.PROFILE_DIR / "printer.json").read_text())["name"]
        for fname in ("filament.json", "process.json"):
            data = json.loads((print_slicer.PROFILE_DIR / fname).read_text())
            self.assertIn(printer, data["compatible_printers"], fname)


class ImportGuardTest(unittest.TestCase):
    def test_import_fails_without_binary(self):
        with mock.patch.object(Path, "is_file", return_value=False):
            with self.assertRaises(RuntimeError) as ctx:
                print_slicer._require_binary()
        self.assertIn("make install", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()

"""Fast tests for print/slicer.py. Orca is never run: subprocess.run is replaced."""
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

REPO = Path(__file__).resolve().parents[2]        # the repository root
sys.path.insert(0, str(REPO))
FIXTURES = Path(__file__).resolve().parent / "fixtures"

from print import slicer as print_slicer  # noqa: E402
from print.slicer import SliceError, build_command, part_info, read_summary, slice_stl, stl_bounds  # noqa: E402


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
        # .resolve(): slice_stl resolves its arguments, and on macOS /tmp is a
        # symlink, so an unresolved mkdtemp path would not compare equal.
        self.tmp = Path(tempfile.mkdtemp(prefix="print-slicer-")).resolve()
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
        with mock.patch.dict(os.environ, {"PENGUINCAM_SECRET_MARKER": "x"}):
            with mock.patch.object(print_slicer.subprocess, "run", fake):
                result = slice_stl(print_slicer.SAMPLE_STL, self.out)
        self.assertTrue(self.out.is_dir())
        cmd, kwargs = fake.calls[0]
        env = kwargs["env"]
        # The server's environment holds OAuth secrets; Orca sees an allowlist.
        self.assertNotIn("PENGUINCAM_SECRET_MARKER", env)
        self.assertEqual(env["PATH"], os.environ["PATH"])
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
        # The exit code takes the first of the twenty lines, so the tail of
        # the output starts one line later than the output alone would.
        self.assertEqual(ctx.exception.details.splitlines()[0], "exit code 255")
        self.assertNotIn("line 9\n", ctx.exception.details + "\n")
        self.assertNotIn("line 10\n", ctx.exception.details + "\n")
        self.assertIn("line 11", ctx.exception.details)
        self.assertIn("line 29", ctx.exception.details)
        self.assertEqual(len(ctx.exception.details.splitlines()), 20)
        self.assertTrue(ctx.exception.message.endswith("."))
        self.assertNotIn("line 29", ctx.exception.message)

    def test_relative_arguments_reach_orca_as_absolute_paths(self):
        # Orca runs with cwd=output_dir, so a relative argument would be read
        # against the wrong directory; slice_stl resolves before it builds.
        shutil.copy(print_slicer.SAMPLE_STL, self.tmp / "sample_part.stl")
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)
        os.chdir(self.tmp)
        fake = fake_run_factory()
        with mock.patch.object(print_slicer.subprocess, "run", fake):
            result = slice_stl("sample_part.stl", "out")
        cmd, kwargs = fake.calls[0]
        for token in cmd:
            for piece in token.split(";"):
                if "/" in piece:
                    self.assertTrue(Path(piece).is_absolute(), piece)
        self.assertTrue(Path(cmd[cmd.index("--outputdir") + 1]).is_absolute())
        self.assertTrue(Path(cmd[cmd.index("--datadir") + 1]).is_absolute())
        self.assertTrue(Path(cmd[-1]).is_absolute())
        self.assertTrue(Path(kwargs["cwd"]).is_absolute())
        # --export-3mf is a bare name on purpose: Orca writes it into --outputdir.
        self.assertEqual(cmd[cmd.index("--export-3mf") + 1], "sample_part.gcode.3mf")
        self.assertEqual(result.output_path, (self.tmp / "out" / "sample_part.gcode.3mf").resolve())

    def test_build_command_resolves_relative_paths(self):
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)
        os.chdir(REPO)
        cmd = build_command(Path("print/sample_part.stl"), Path("out"))
        self.assertEqual(cmd[-1], str(print_slicer.SAMPLE_STL))
        self.assertEqual(cmd[cmd.index("--outputdir") + 1], str(REPO / "out"))

    def test_unstartable_slicer_raises_student_message(self):
        def boom(cmd, **kwargs):
            raise OSError(8, "Exec format error", cmd[0])

        with mock.patch.object(print_slicer.subprocess, "run", boom):
            with self.assertRaises(SliceError) as ctx:
                slice_stl(print_slicer.SAMPLE_STL, self.out)
        self.assertEqual(ctx.exception.message, "The slicer could not be started.")
        self.assertIn("Exec format error", ctx.exception.details)

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

    def test_gcode_header_is_read_without_decompressing_the_whole_entry(self):
        self.out.mkdir()
        archive = self.out / "big.gcode.3mf"
        filler = "".join(f"G1 X{i % 100}.0 Y{i % 97}.0 E0.01\n" for i in range(110000))
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("Metadata/plate_1.gcode", (FIXTURES / "plate_1_header.gcode").read_text() + filler)
            zf.writestr("Metadata/slice_info.config", (FIXTURES / "slice_info.config").read_text())
        self.assertGreater(zipfile.ZipFile(archive).getinfo("Metadata/plate_1.gcode").file_size, 2_000_000)

        real_read = zipfile.ZipFile.read

        def guarded_read(zf_self, name, *args, **kwargs):
            entry = name.filename if isinstance(name, zipfile.ZipInfo) else name
            if entry == "Metadata/plate_1.gcode":
                raise AssertionError("read_summary decompressed the whole G-code entry")
            return real_read(zf_self, name, *args, **kwargs)

        with mock.patch.object(zipfile.ZipFile, "read", guarded_read):
            summary = read_summary(archive, self.out)
        self.assertEqual(summary["layer_count"], 50)
        self.assertEqual(summary["print_time_s"], 790.0)

    def test_corrupt_archive_raises_slice_error(self):
        self.out.mkdir()
        archive = self.out / "x.gcode.3mf"
        archive.write_bytes(b"not a zip at all\x00\x01\x02")
        with self.assertRaises(SliceError) as ctx:
            read_summary(archive, self.out)
        self.assertEqual(ctx.exception.message, "The slicer output could not be read.")
        self.assertIn("x.gcode.3mf", ctx.exception.details)

    def test_missing_summary_fields_are_none(self):
        self.out.mkdir()
        make_archive(self.out / "p.gcode.3mf", slice_info=False)
        summary = read_summary(self.out / "p.gcode.3mf", self.out)
        self.assertIsNone(summary["print_time_s"])
        self.assertIsNone(summary["filament_g"])
        self.assertIsNone(summary["filament_m"])
        self.assertEqual(summary["layer_count"], 50)


def fake_result_run(returncode, return_code=None):
    """A stand-in for subprocess.run that writes no archive, and a result.json carrying
    `return_code` unless it is None."""
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        if return_code is not None:
            outdir = Path(cmd[cmd.index("--outputdir") + 1])
            (outdir / "result.json").write_text(json.dumps({"return_code": return_code, "error_string": "x"}))
        return subprocess.CompletedProcess(cmd, returncode, stdout="orca says no\n", stderr="")

    fake_run.calls = calls
    return fake_run


class PlateSliceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="print-plate-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.out = self.tmp / "out"
        self.model = self.tmp / "plate.3mf"
        self.model.write_bytes(b"PK")

    def test_plate_command_disables_arrange_and_orient(self):
        profiles = print_slicer.SliceProfiles(self.tmp / "p.json", self.tmp / "f.json", self.tmp / "q.json")
        cmd = print_slicer.build_plate_command(self.model, profiles, self.out)
        self.assertEqual(Path(cmd[0]), print_slicer.ORCA_BIN)
        self.assertEqual(cmd[cmd.index("--arrange") + 1], "0")
        self.assertEqual(cmd[cmd.index("--orient") + 1], "0")
        self.assertEqual(cmd[cmd.index("--load-settings") + 1], f"{self.tmp / 'p.json'};{self.tmp / 'q.json'}")
        self.assertEqual(cmd[cmd.index("--load-filaments") + 1], str(self.tmp / "f.json"))
        self.assertEqual(cmd[cmd.index("--slice") + 1], "0")
        self.assertEqual(cmd[cmd.index("--export-3mf") + 1], "plate.gcode.3mf")
        self.assertEqual(cmd[cmd.index("--outputdir") + 1], str(self.out))
        self.assertEqual(cmd[cmd.index("--datadir") + 1], str(self.out / "datadir"))
        self.assertEqual(cmd[-1], str(self.model))
        self.assertEqual(print_slicer.DEFAULT_PROFILES, print_slicer.SliceProfiles(
            print_slicer.PROFILE_DIR / "printer.json", print_slicer.PROFILE_DIR / "filament.json",
            print_slicer.PROFILE_DIR / "process.json"))

    def test_slice_plate_default_timeout_is_plate_timeout(self):
        from print.limits import PLATE_SLICE_TIMEOUT_S
        self.assertEqual(PLATE_SLICE_TIMEOUT_S, 240)
        fake = fake_run_factory()
        with mock.patch.object(print_slicer.subprocess, "run", fake):
            result = print_slicer.slice_plate(self.model, print_slicer.DEFAULT_PROFILES, self.out)
        self.assertEqual(fake.calls[0][1]["timeout"], 240)
        self.assertEqual(result.output_path, self.out / "plate.gcode.3mf")
        self.assertEqual(result.layer_count, 50)
        self.assertEqual(fake.calls[0][1]["env"]["LC_ALL"], "C")
        self.assertEqual(fake.calls[0][1]["cwd"], str(self.out))
        fake = fake_run_factory()
        with mock.patch.object(print_slicer.subprocess, "run", fake):
            slice_stl(print_slicer.SAMPLE_STL, self.tmp / "stl")
        self.assertEqual(fake.calls[0][1]["timeout"], 120)
        self.assertEqual(print_slicer.DEFAULT_TIMEOUT_S, 120)

    def test_orca_codes_map_to_messages(self):
        cases = {-101: "Parts are too close together; spread them out.",
                 -52: "A part crosses the edge of the plate.",
                 -100: "A part has no flat face on the plate; choose another Lay flat.",
                 -50: "No part is fully on the plate.",
                 -6: "The slicer could not process this part."}
        self.assertEqual(print_slicer.ORCA_CODE_MESSAGES, {k: v for k, v in cases.items() if k != -6})
        for code, message in cases.items():
            with self.subTest(code=code):
                out = self.tmp / f"out{-code}"
                fake = fake_result_run(returncode=(code + 256) % 256, return_code=code)
                with mock.patch.object(print_slicer.subprocess, "run", fake):
                    with self.assertRaises(SliceError) as ctx:
                        print_slicer.slice_plate(self.model, print_slicer.DEFAULT_PROFILES, out)
                self.assertEqual(ctx.exception.message, message)
                self.assertEqual(ctx.exception.code, code)
                self.assertIn("orca says no", ctx.exception.details)

    def test_return_code_from_exit_status_when_no_result_json(self):
        self.out.mkdir()
        self.assertEqual(print_slicer.orca_return_code(self.out, 155), -101)
        self.assertIsNone(print_slicer.orca_return_code(self.out, 1))
        (self.out / "result.json").write_text(json.dumps({"return_code": -52}))
        self.assertEqual(print_slicer.orca_return_code(self.out, 155), -52)
        (self.out / "result.json").write_text("not json")
        self.assertEqual(print_slicer.orca_return_code(self.out, 204), -52)
        fake = fake_result_run(returncode=155)
        with mock.patch.object(print_slicer.subprocess, "run", fake):
            with self.assertRaises(SliceError) as ctx:
                print_slicer.slice_plate(self.model, print_slicer.DEFAULT_PROFILES, self.tmp / "o2")
        self.assertEqual(ctx.exception.code, -101)
        self.assertEqual(ctx.exception.message, "Parts are too close together; spread them out.")


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
        self.assertEqual(info["printer"]["name"], "Bambu Lab H2S 0.6 nozzle")
        self.assertEqual(info["printer"]["bed_mm"], {"x": 340.0, "y": 320.0})
        self.assertEqual(info["printer"]["height_mm"], 340.0)
        self.assertEqual(info["filament"]["name"], "Generic PETG @BBL H2S")
        self.assertEqual(info["process"]["name"], "0.24mm Balanced Quality @BBL H2S 0.6 nozzle")
        self.assertEqual(info["process"]["layer_height_mm"], 0.24)


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


class NozzleVariantTest(unittest.TestCase):
    """The checked-in profiles must select the High Flow nozzle and the textured plate.

    Orca resolves every variant-aware value by matching
    "<extruder_type> <nozzle_volume_type>" against each profile's
    *_extruder_variant array. With no nozzle_volume_type the command line falls
    back to Standard, which is index 0, silently.
    """
    VARIANT = "Direct Drive High Flow"

    def setUp(self):
        self.machine = json.loads((print_slicer.PROFILE_DIR / "printer.json").read_text())
        self.filament = json.loads((print_slicer.PROFILE_DIR / "filament.json").read_text())
        self.process = json.loads((print_slicer.PROFILE_DIR / "process.json").read_text())

    def test_machine_selects_high_flow(self):
        self.assertEqual(self.machine["nozzle_volume_type"], ["High Flow"])
        self.assertEqual(self.machine["extruder_type"], ["Direct Drive"])
        self.assertEqual(self.machine["nozzle_diameter"], ["0.6"])
        self.assertEqual(self.machine["nozzle_type"], ["hardened_steel", "hardened_steel"])

    def test_every_profile_offers_the_variant_at_the_same_index(self):
        indexes = {
            "printer": self.machine["printer_extruder_variant"].index(self.VARIANT),
            "filament": self.filament["filament_extruder_variant"].index(self.VARIANT),
            "process": self.process["print_extruder_variant"].index(self.VARIANT),
        }
        self.assertEqual(set(indexes.values()), {1}, indexes)

    def test_dual_valued_keys_are_not_collapsed_to_one_element(self):
        # Collapsing would hide the choice and hard-code index 0.
        self.assertEqual(self.machine["nozzle_volume"], ["145", "148"])
        self.assertEqual(self.machine["nozzle_flush_dataset"], ["1", "2"])
        self.assertEqual(len(self.filament["filament_extruder_variant"]), 2)

    def test_petg_values_are_the_profile_values_not_invented_ones(self):
        # Generic PETG defines the same numbers for both nozzles; High Flow must
        # not silently raise them.
        self.assertEqual(self.filament["filament_max_volumetric_speed"], ["12", "12"])
        self.assertEqual(self.filament["nozzle_temperature"], ["255", "255"])
        self.assertEqual(self.filament["nozzle_temperature_initial_layer"], ["255", "255"])
        self.assertEqual(self.filament["filament_flow_ratio"], ["0.95", "0.95"])
        self.assertEqual(self.filament["filament_type"], ["PETG"])

    def test_textured_plate_is_selected_by_name_and_number(self):
        self.assertEqual(self.machine["curr_bed_type"], "Textured PEI Plate")
        # Preset::get_default_bed_type runs atoi() on this, so a name would
        # parse as 0 and fall back to the smooth High Temp Plate.
        self.assertEqual(self.machine["default_bed_type"], "4")
        self.assertEqual(self.filament["textured_plate_temp"], ["70"])
        self.assertEqual(self.filament["textured_plate_temp_initial_layer"], ["70"])


class ImportGuardTest(unittest.TestCase):
    def test_import_fails_without_binary(self):
        with mock.patch.object(Path, "is_file", return_value=False):
            with self.assertRaises(RuntimeError) as ctx:
                print_slicer._require_binary()
        self.assertIn("make install", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()

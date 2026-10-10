"""Run Orca Slicer's command line on an STL and read back a slice summary.

Flask-free. Orca lives at the fixed path tools/orca/orca-slicer (installed by
print/scripts/install-orca.sh); there is no environment variable and no PATH lookup.
The profiles are the fixed set in print/profiles/. See docs/3D_PRINTING.md.
"""
import json
import os
import re
import struct
import subprocess
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from .limits import PLATE_SLICE_TIMEOUT_S

PKG = Path(__file__).resolve().parent      # the print/ directory
REPO = PKG.parent
ORCA_BIN = REPO / "tools" / "orca" / "orca-slicer"
PROFILE_DIR = PKG / "profiles"
SAMPLE_STL = PKG / "sample_part.stl"
DEFAULT_TIMEOUT_S = 120
ARCHIVE_SUFFIX = ".gcode.3mf"
GCODE_ENTRY = "Metadata/plate_1.gcode"
SLICE_INFO_ENTRY = "Metadata/slice_info.config"
DETAIL_LINES = 20
GCODE_HEAD_BYTES = 4096       # enough for Orca's header block
BINARY_HEADER_BYTES = 84      # 80-byte comment plus a uint32 triangle count
BINARY_TRIANGLE_BYTES = 50    # 12 floats plus a uint16 attribute word
PLATE_ARCHIVE = "plate" + ARCHIVE_SUFFIX
SLICE_FAILED_MESSAGE = "The slicer could not process this part."
# Orca's return codes for a plate it refused (observed with Orca 2.4.2, --arrange 0).
# A part entirely off the plate is not among them: Orca leaves it out silently, which
# print/plate_3mf.py's name guard catches.
ORCA_CODE_MESSAGES = {
    -101: "Parts are too close together; spread them out.",
    -52: "A part crosses the edge of the plate.",
    -100: "A part has no flat face on the plate; choose another Lay flat.",
    -50: "No part is fully on the plate.",
}
# Orca gets only these variables from the server's environment; anything else
# (tokens, client secrets) stays out of the subprocess. The wrapper
# tools/orca/orca-slicer sets APPDIR and LD_LIBRARY_PATH itself.
ENV_ALLOWLIST = ("PATH", "HOME", "TMPDIR", "LANG", "USER", "LOGNAME")


def _require_binary():
    if not ORCA_BIN.is_file():
        raise RuntimeError(f"Orca Slicer is missing at {ORCA_BIN}; run `make install`")


_require_binary()


class SliceError(Exception):
    """A slice that produced no usable archive.

    `message` is one sentence fit for the student; `details` is the last
    twenty lines of Orca's output (or the archive listing) for the server log;
    `code` is Orca's exit code (slice_stl) or its return code (slice_plate), "timeout",
    or None when Orca did not report one.
    """

    def __init__(self, message, details="", code=None):
        super().__init__(message)
        self.message = message
        self.details = details
        self.code = code


@dataclass
class SliceResult:
    output_path: Path
    print_time_s: float | None
    filament_m: float | None
    filament_g: float | None
    layer_count: int | None
    warnings: list = field(default_factory=list)

    def summary(self):
        return {"print_time_s": self.print_time_s, "filament_m": self.filament_m,
                "filament_g": self.filament_g, "layer_count": self.layer_count,
                "warnings": list(self.warnings)}


@dataclass(frozen=True)
class SliceProfiles:
    printer: Path
    filament: Path
    process: Path


DEFAULT_PROFILES = SliceProfiles(PROFILE_DIR / "printer.json", PROFILE_DIR / "filament.json",
                                 PROFILE_DIR / "process.json")


def build_command(stl_path, output_dir):
    # Orca runs with cwd=output_dir, so every path handed to it must be
    # absolute: a relative one would be read against the wrong directory.
    stl_path = Path(stl_path).resolve()
    output_dir = Path(output_dir).resolve()
    archive_name = stl_path.stem + ARCHIVE_SUFFIX
    return [
        str(ORCA_BIN),
        "--datadir", str(output_dir / "datadir"),
        "--load-settings", f"{PROFILE_DIR / 'printer.json'};{PROFILE_DIR / 'process.json'}",
        "--load-filaments", str(PROFILE_DIR / "filament.json"),
        "--arrange", "1",
        "--slice", "0",
        "--export-3mf", archive_name,
        "--outputdir", str(output_dir),
        str(stl_path),
    ]


def build_plate_command(model_3mf, profiles, output_dir):
    """The plate's command: placement is the 3MF's, so Orca must neither arrange nor orient
    (leaving --arrange out arranges). Paths absolute, as in build_command."""
    model_3mf = Path(model_3mf).resolve()
    output_dir = Path(output_dir).resolve()
    return [
        str(ORCA_BIN),
        "--datadir", str(output_dir / "datadir"),
        "--load-settings", f"{Path(profiles.printer).resolve()};{Path(profiles.process).resolve()}",
        "--load-filaments", str(Path(profiles.filament).resolve()),
        "--arrange", "0",
        "--orient", "0",
        "--slice", "0",
        "--export-3mf", PLATE_ARCHIVE,
        "--outputdir", str(output_dir),
        str(model_3mf),
    ]


def _tail(text, n=DETAIL_LINES):
    return "\n".join(text.splitlines()[-n:])


def orca_env(output_dir):
    """Orca's environment: the allowlist, plus private XDG directories under output_dir."""
    output_dir = Path(output_dir)
    config_home = output_dir / "xdg-config"
    runtime_dir = output_dir / "xdg-runtime"
    config_home.mkdir(exist_ok=True)
    runtime_dir.mkdir(exist_ok=True)
    os.chmod(runtime_dir, 0o700)
    env = {name: os.environ[name] for name in ENV_ALLOWLIST if name in os.environ}
    env.update({"XDG_CONFIG_HOME": str(config_home), "XDG_RUNTIME_DIR": str(runtime_dir), "LC_ALL": "C"})
    return env


def _run_orca(cmd, output_dir, timeout_s):
    """Run Orca in output_dir with its private environment; the completed process and its
    combined output. Raises SliceError when Orca cannot start or runs out of time."""
    env = orca_env(output_dir)
    try:
        proc = subprocess.run(cmd, env=env, cwd=str(output_dir), capture_output=True, text=True,
                              timeout=timeout_s, stdin=subprocess.DEVNULL)
    except OSError as exc:
        # The wrapper is missing, not executable, or not this architecture.
        raise SliceError("The slicer could not be started.", str(exc)) from None
    except subprocess.TimeoutExpired as exc:
        captured = exc.stdout or ""
        if isinstance(captured, bytes):
            captured = captured.decode(errors="replace")
        raise SliceError("The slicer took too long on this part.", _tail(captured), code="timeout")
    return proc, (proc.stdout or "") + (proc.stderr or "")


def _failure_details(exit_code, output):
    # The exit code leads, then the last DETAIL_LINES - 1 lines of output:
    # `details` stays at DETAIL_LINES lines and the code is always visible.
    return f"exit code {exit_code}\n" + _tail(output, DETAIL_LINES - 1)


def _result(archive, output_dir, output):
    if not archive.is_file():
        raise SliceError("The slicer produced no output file.", _tail(output))
    return SliceResult(output_path=archive, **read_summary(archive, output_dir))


def slice_stl(stl_path, output_dir, timeout_s=DEFAULT_TIMEOUT_S):
    """Slice `stl_path` into `output_dir/<stem>.gcode.3mf` and return a SliceResult."""
    stl_path = Path(stl_path).resolve()
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    proc, output = _run_orca(build_command(stl_path, output_dir), output_dir, timeout_s)
    if proc.returncode != 0:
        raise SliceError(SLICE_FAILED_MESSAGE, _failure_details(proc.returncode, output), code=proc.returncode)
    return _result(output_dir / (stl_path.stem + ARCHIVE_SUFFIX), output_dir, output)


def orca_return_code(output_dir, exit_code):
    """Orca's own return code: result.json's return_code, else the exit status read as a
    negative byte (exit 155 is -101), else None."""
    try:
        data = json.loads((Path(output_dir) / "result.json").read_text(encoding="utf-8"))
        code = data.get("return_code")
        if isinstance(code, int) and not isinstance(code, bool) and code != 0:
            return code
    except (OSError, ValueError, AttributeError):
        pass
    if exit_code > 127:
        return exit_code - 256
    return None


def slice_plate(model_3mf, profiles, output_dir, timeout_s=PLATE_SLICE_TIMEOUT_S):
    """Slice a plate 3MF (print/plate_3mf.py) into `output_dir/plate.gcode.3mf` with the
    placement as written; return a SliceResult."""
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    proc, output = _run_orca(build_plate_command(model_3mf, profiles, output_dir), output_dir, timeout_s)
    if proc.returncode != 0:
        code = orca_return_code(output_dir, proc.returncode)
        raise SliceError(ORCA_CODE_MESSAGES.get(code, SLICE_FAILED_MESSAGE),
                         _failure_details(proc.returncode, output), code)
    return _result(output_dir / PLATE_ARCHIVE, output_dir, output)


def _float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def read_summary(archive_path, output_dir):
    """Read print time, filament and layer count from the archive and result.json."""
    archive_path = Path(archive_path)
    try:
        zf = zipfile.ZipFile(archive_path)
    except zipfile.BadZipFile:
        raise SliceError("The slicer output could not be read.",
                         f"{archive_path.name} is not a zip archive") from None
    with zf:
        names = zf.namelist()
        if GCODE_ENTRY not in names:
            raise SliceError("The slicer output is missing its G-code.",
                             "archive entries:\n" + "\n".join(names))
        # A real plate's G-code runs to hundreds of megabytes; the header we
        # want is the first few lines, so decompress only that much of it.
        with zf.open(GCODE_ENTRY) as fh:
            gcode_head = fh.read(GCODE_HEAD_BYTES).decode("utf-8", errors="replace")
        info = zf.read(SLICE_INFO_ENTRY).decode("utf-8", errors="replace") if SLICE_INFO_ENTRY in names else ""

    def meta(key):
        m = re.search(r'<metadata key="%s" value="([^"]*)"' % re.escape(key), info)
        return _float(m.group(1)) if m else None

    used_m = None
    m = re.search(r'<filament [^>]*used_m="([^"]*)"', info)
    if m:
        used_m = _float(m.group(1))
    layers = None
    m = re.search(r"^; total layer number: (\d+)", gcode_head, re.M)
    if m:
        layers = int(m.group(1))

    warnings = []
    result_json = Path(output_dir) / "result.json"
    if result_json.is_file():
        try:
            data = json.loads(result_json.read_text(encoding="utf-8"))
            for plate in data.get("sliced_plates", []):
                text = (plate.get("warning_message") or "").strip()
                if text:
                    warnings.append(text)
        except (ValueError, OSError):
            pass

    return {"print_time_s": meta("prediction"), "filament_g": meta("weight"),
            "filament_m": used_m, "layer_count": layers, "warnings": warnings}


def _binary_triangle_count(data):
    """Triangle count if `data` is a binary STL, else None.

    A binary STL is exactly an 84-byte header plus 50 bytes per triangle. The
    leading word is not a reliable test: binary files written by some tools
    begin with "solid" too, so only the length decides.
    """
    if len(data) < BINARY_HEADER_BYTES:
        return None
    count = struct.unpack("<I", data[80:84])[0]
    if len(data) != BINARY_HEADER_BYTES + BINARY_TRIANGLE_BYTES * count:
        return None
    return count


def stl_bounds(data):
    """Return (min_corner, max_corner, triangle_count) of a binary or ASCII STL."""
    count = _binary_triangle_count(data)
    vertices = []
    if count is not None:
        for i in range(count):
            start = BINARY_HEADER_BYTES + i * BINARY_TRIANGLE_BYTES
            rec = struct.unpack("<12fH", data[start: start + BINARY_TRIANGLE_BYTES])
            vertices.extend((rec[3:6], rec[6:9], rec[9:12]))
    else:
        text = data.decode("utf-8", errors="replace")
        for m in re.finditer(r"vertex\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)", text):
            vertices.append(tuple(float(v) for v in m.groups()))
        count = len(vertices) // 3
    if not vertices:
        raise ValueError("STL has no triangles")
    lo = tuple(min(v[i] for v in vertices) for i in range(3))
    hi = tuple(max(v[i] for v in vertices) for i in range(3))
    return lo, hi, count


def _load_profile(name):
    with open(PROFILE_DIR / name, encoding="utf-8") as fh:
        return json.load(fh)


def default_printer_profile():
    """The printer profile every slice uses today (print/profiles/printer.json)."""
    return _load_profile("printer.json")


def _bed_size(printer):
    corners = []
    for corner in printer.get("printable_area", []):
        x, y = corner.lower().split("x")
        corners.append((float(x), float(y)))
    if not corners:
        return {"x": None, "y": None}
    return {"x": max(c[0] for c in corners) - min(c[0] for c in corners),
            "y": max(c[1] for c in corners) - min(c[1] for c in corners)}


def part_info(stl_path=SAMPLE_STL):
    """Names and numbers the print wizard shows before slicing."""
    stl_path = Path(stl_path)
    lo, hi, count = stl_bounds(stl_path.read_bytes())
    printer = _load_profile("printer.json")
    filament = _load_profile("filament.json")
    process = _load_profile("process.json")
    return {
        "part": {"name": stl_path.stem, "file": stl_path.name,
                 "size_mm": {"x": round(hi[0] - lo[0], 3), "y": round(hi[1] - lo[1], 3), "z": round(hi[2] - lo[2], 3)},
                 "triangles": count},
        "printer": {"name": printer.get("name"), "bed_mm": _bed_size(printer),
                    "height_mm": _float(printer.get("printable_height"))},
        "filament": {"name": filament.get("name")},
        "process": {"name": process.get("name"), "layer_height_mm": _float(process.get("layer_height"))},
    }

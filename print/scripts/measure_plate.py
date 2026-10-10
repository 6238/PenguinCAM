#!/usr/bin/env python3
"""Slice a synthetic plate at the print path's limits and report time and peak memory.

Flask-free: standard library and numpy. One plate per invocation, because the peak
comes from getrusage(RUSAGE_CHILDREN), a maximum over every child this process has
waited for.

    uv run python print/scripts/measure_plate.py --plate thirty
    uv run python print/scripts/measure_plate.py --plate two-big
    uv run python print/scripts/measure_plate.py --plate thirty --triangles 275000

Prints one line, for example

    plate=thirty copies=30 triangles=300000 seconds=41.2 peak_kb=700000 exit=0 names=ok verdict=ok

and exits 1 when the verdict is not ok. The verdict is `over` when the slice passes
MEASURE_MAX_PEAK_KB or MEASURE_MAX_SECONDS (print/limits.py), `failed` when Orca
exits non-zero or leaves a copy out of Metadata/plate_1.json. The results are
recorded in print/jobs.py's sizing note.
"""
import argparse
import json
import os
import resource
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from xml.sax.saxutils import quoteattr

import numpy as np

REPO = Path(__file__).resolve().parents[2]     # the repository root
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from print.limits import MEASURE_MAX_PEAK_KB, MEASURE_MAX_SECONDS, PLATE_SLICE_TIMEOUT_S  # noqa: E402
from print.slicer import ARCHIVE_SUFFIX, ENV_ALLOWLIST, build_command  # noqa: E402

HUB_RADIUS_MM = 18.0       # 36 mm across
HUB_HEIGHT_MM = 20.0
TRIANGLES_PER_SEGMENT = 4  # top fan, bottom fan, two side triangles
PLATES = {
    # 30 copies of 10,000 triangles: 300,000 in all, in a 6 x 5 grid at 50 mm pitch.
    "thirty": {"segments": 2_500,
               "centres": [(40.0 + 50.0 * col, 40.0 + 50.0 * row) for row in range(5) for col in range(6)]},
    # Two copies of one 150,000-triangle part: 300,000 in all.
    "two-big": {"segments": 37_500, "centres": [(100.0, 100.0), (200.0, 100.0)]},
}

CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
 <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
 <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>
</Types>
"""
RELS = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Target="/3D/3dmodel.model" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>
</Relationships>
"""


def cylinder_stl(radius_mm, height_mm, segments):
    """A closed binary STL cylinder centred on the z axis, z from 0 to height_mm.

    4 * segments triangles: a fan on each cap from its centre and two triangles per
    side facet, wound counter-clockwise seen from outside, with real normals.
    """
    angles = np.linspace(0.0, 2.0 * np.pi, segments, endpoint=False)
    ring = np.stack([radius_mm * np.cos(angles), radius_mm * np.sin(angles)], axis=1)
    nxt = np.roll(ring, -1, axis=0)
    zeros = np.zeros(segments)
    tops = np.full(segments, height_mm)

    def points(xy, z):
        return np.column_stack([xy, z])

    a0, b0 = points(ring, zeros), points(nxt, zeros)
    a1, b1 = points(ring, tops), points(nxt, tops)
    c0 = np.zeros((segments, 3))
    c1 = np.tile([0.0, 0.0, height_mm], (segments, 1))
    tris = np.concatenate([
        np.stack([c1, a1, b1], axis=1),       # top cap, facing +z
        np.stack([c0, b0, a0], axis=1),       # bottom cap, facing -z
        np.stack([a0, b0, b1], axis=1),       # sides
        np.stack([a0, b1, a1], axis=1),
    ])
    normals = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
    normals /= np.linalg.norm(normals, axis=1, keepdims=True)
    record = np.dtype([("normal", "<f4", 3), ("v", "<f4", (3, 3)), ("attr", "<u2")])
    body = np.zeros(len(tris), dtype=record)
    body["normal"] = normals
    body["v"] = tris
    header = b"PenguinCAM measure_plate hub".ljust(80, b"\0")
    return header + np.uint32(len(tris)).tobytes() + body.tobytes()


def _indexed_mesh(stl):
    """Vertices and triangle indices of a binary STL, merging identical vertices."""
    count = int(np.frombuffer(stl, dtype="<u4", count=1, offset=80)[0])
    record = np.dtype([("normal", "<f4", 3), ("v", "<f4", (3, 3)), ("attr", "<u2")])
    tris = np.frombuffer(stl, dtype=record, count=count, offset=84)["v"].reshape(-1, 3)
    vertices, index = np.unique(tris, axis=0, return_inverse=True)
    return vertices, index.reshape(-1, 3)


def _mesh_xml(vertices, triangles):
    vertex_lines = "".join(f'<vertex x="{x:.4f}" y="{y:.4f}" z="{z:.4f}"/>' for x, y, z in vertices.tolist())
    triangle_lines = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in triangles.tolist())
    return f"<mesh><vertices>{vertex_lines}</vertices><triangles>{triangle_lines}</triangles></mesh>"


def write_measure_3mf(path, stl, centres):
    """A minimal 3MF with one named <object> per copy, each translated to its centre.

    One object per copy, not one shared object, because Orca blanks the names of
    copies that share an object. Returns the object names, "Hub #1" onwards.
    """
    mesh = _mesh_xml(*_indexed_mesh(stl))
    names = [f"Hub #{n}" for n in range(1, len(centres) + 1)]
    objects, items = [], []
    for object_id, (name, (x, y)) in enumerate(zip(names, centres), start=1):
        objects.append(f'<object id="{object_id}" type="model" name={quoteattr(name)}>{mesh}</object>')
        items.append(f'<item objectid="{object_id}" transform="1 0 0 0 1 0 0 0 1 {float(x)} {float(y)} 0.0"/>')
    model = ('<?xml version="1.0" encoding="UTF-8"?>\n'
             '<model unit="millimeter" xml:lang="en-US" '
             'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
             f'<resources>{"".join(objects)}</resources><build>{"".join(items)}</build></model>\n')
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES)
        zf.writestr("_rels/.rels", RELS)
        zf.writestr("3D/3dmodel.model", model)
    return names


def _plate_command(model_path, output_dir):
    """print/slicer.py's command with placement kept: --arrange 0 --orient 0."""
    cmd = build_command(model_path, output_dir)
    at = cmd.index("--arrange")
    cmd[at + 1] = "0"
    cmd[at + 2:at + 2] = ["--orient", "0"]
    return cmd


def _slicer_env(output_dir):
    """The environment print/slicer.py gives Orca."""
    config_home = output_dir / "xdg-config"
    runtime_dir = output_dir / "xdg-runtime"
    config_home.mkdir(exist_ok=True)
    runtime_dir.mkdir(exist_ok=True)
    os.chmod(runtime_dir, 0o700)
    env = {name: os.environ[name] for name in ENV_ALLOWLIST if name in os.environ}
    env.update({"XDG_CONFIG_HOME": str(config_home), "XDG_RUNTIME_DIR": str(runtime_dir), "LC_ALL": "C"})
    return env


def _plate_names(archive):
    with zipfile.ZipFile(archive) as zf:
        plate = json.loads(zf.read("Metadata/plate_1.json").decode("utf-8"))
    return [obj.get("name", "") for obj in plate.get("bbox_objects", [])]


def measure(plate, triangles=None):
    spec = PLATES[plate]
    segments = spec["segments"]
    centres = spec["centres"]
    if triangles is not None:
        if plate != "thirty":
            raise SystemExit("--triangles applies to the thirty plate only")
        segments = triangles // (TRIANGLES_PER_SEGMENT * len(centres))
    stl = cylinder_stl(HUB_RADIUS_MM, HUB_HEIGHT_MM, segments)
    total = TRIANGLES_PER_SEGMENT * segments * len(centres)
    with tempfile.TemporaryDirectory(prefix="measure_plate_") as tmp:
        out = Path(tmp)
        model = out / "plate.3mf"
        names = write_measure_3mf(model, stl, centres)
        started = time.monotonic()
        try:
            proc = subprocess.run(_plate_command(model, out), env=_slicer_env(out), cwd=str(out),
                                  capture_output=True, text=True, timeout=PLATE_SLICE_TIMEOUT_S,
                                  stdin=subprocess.DEVNULL)
            code = proc.returncode
        except subprocess.TimeoutExpired:
            code = "timeout"
        seconds = time.monotonic() - started
        peak_kb = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
        archive = out / ("plate" + ARCHIVE_SUFFIX)
        listed = _plate_names(archive) if code == 0 and archive.is_file() else []
        names_ok = sorted(listed) == sorted(names)
    if code == "timeout" or peak_kb > MEASURE_MAX_PEAK_KB or seconds > MEASURE_MAX_SECONDS:
        verdict = "over"
    elif code != 0 or not names_ok:
        verdict = "failed"
    else:
        verdict = "ok"
    line = (f"plate={plate} copies={len(centres)} triangles={total} seconds={seconds:.1f} "
            f"peak_kb={peak_kb} exit={code} names={'ok' if names_ok else 'missing'} verdict={verdict}")
    return line, verdict


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plate", choices=sorted(PLATES), required=True)
    parser.add_argument("--triangles", type=int, help="scale the thirty plate to about N triangles in all")
    args = parser.parse_args(argv)
    line, verdict = measure(args.plate, args.triangles)
    print(line)
    return 0 if verdict == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())

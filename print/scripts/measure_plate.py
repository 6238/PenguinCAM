#!/usr/bin/env python3
"""Slice a synthetic plate at the print path's limits and report time and peak memory.

Flask-free: standard library and numpy. One plate per invocation, because the peak
comes from getrusage(RUSAGE_CHILDREN), a maximum over every child this process has
waited for.

    uv run python print/scripts/measure_plate.py --plate max-copies
    uv run python print/scripts/measure_plate.py --plate max-part
    uv run python print/scripts/measure_plate.py --plate max-part --part-triangles 125000
    uv run python print/scripts/measure_plate.py --plate max-copies --triangles 275000

The part is a hub: a 40 mm x 20 mm ring with a 12 mm bore and 3 mm fillets on all
four edges, revolved, so the mesh is near-square quads with no fans or slivers, the
way a finely tessellated Onshape part looks. `max-copies` is MAX_COPIES
copies of one hub sharing MAX_JOB_TRIANGLES; `max-part` is
ceil(MAX_JOB_TRIANGLES / P) copies of a P-triangle hub, P = MAX_PART_TRIANGLES
unless --part-triangles says otherwise.

Prints one line, for example

    plate=max-copies copies=25 triangles=300000 seconds=41.2 peak_kb=700000 exit=0 names=ok verdict=ok

and exits 1 when the verdict is not ok. The verdict is `over` when the slice passes
MEASURE_MAX_PEAK_KB or MEASURE_MAX_SECONDS (print/limits.py), `failed` when Orca
exits non-zero or leaves a copy out of Metadata/plate_1.json. The results are
recorded in print/jobs.py's sizing note.

The plate goes through the code that ships: print/plate_3mf.py writes it and
print/slicer.py's slice_plate slices it (PLATE_SLICE_TIMEOUT_S), and the names are
checked against Metadata/plate_1.json as a plate job checks them. `exit` is 0, or
Orca's return code when slice_plate refused the plate.
"""
import argparse
import math
import resource
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]     # the repository root
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from print.limits import (MAX_COPIES, MAX_JOB_TRIANGLES, MAX_PART_TRIANGLES,  # noqa: E402
                          MEASURE_MAX_PEAK_KB, MEASURE_MAX_SECONDS)
from print.mesh import orientation_footprints, read_binary_stl  # noqa: E402
from print.plate_3mf import plate_object_names, write_plate_3mf  # noqa: E402
from print.slicer import DEFAULT_PROFILES, SliceError, slice_plate  # noqa: E402

HUB_OUTER_MM = 20.0        # radius: 40 mm across
HUB_BORE_MM = 6.0          # radius: a 12 mm bore
HUB_HEIGHT_MM = 20.0
HUB_FILLET_MM = 3.0
PITCH_MM = 50.0            # grid pitch: a 10 mm gap between 40 mm hubs
GRID_ORIGIN_MM = 40.0
GRID_COLUMNS = 6           # 6 x 5 covers MAX_COPIES on the 340 x 320 mm plate
PLATES = ("max-copies", "max-part")

def _hub_profile():
    """The hub's cross-section in (r, z): a rounded rectangle, as a dense closed polyline
    running bottom, outer wall, top, bore, and its total length."""
    r0, r1, h, f = HUB_BORE_MM, HUB_OUTER_MM, HUB_HEIGHT_MM, HUB_FILLET_MM
    # Fillet centres and the angle each arc starts at, in loop order.
    corners = [((r1 - f, f), -math.pi / 2), ((r1 - f, h - f), 0.0),
               ((r0 + f, h - f), math.pi / 2), ((r0 + f, f), math.pi)]
    points = []
    for (cr, cz), a0 in corners:
        for t in np.linspace(a0, a0 + math.pi / 2, 64):
            points.append((cr + f * math.cos(t), cz + f * math.sin(t)))
    points.append(points[0])
    poly = np.array(points)
    seg = np.hypot(*np.diff(poly, axis=0).T)
    return poly, np.concatenate([[0.0], np.cumsum(seg)])


def hub_mesh(triangles):
    """A closed hub mesh of at most `triangles` triangles: vertices (n, 3), faces (m, 3).

    `profile` points around the cross-section times `segments` around the axis, two
    triangles per quad, with the two counts chosen so the quads are near square.
    """
    poly, along = _hub_profile()
    length = along[-1]
    mean_circumference = math.pi * (HUB_OUTER_MM + HUB_BORE_MM)
    quads = triangles // 2
    segments = max(8, round(math.sqrt(quads * mean_circumference / length)))
    profile = max(8, quads // segments)
    stations = np.linspace(0.0, length, profile, endpoint=False)
    r = np.interp(stations, along, poly[:, 0])
    z = np.interp(stations, along, poly[:, 1])
    z[np.abs(z) < 1e-9] = 0.0                     # the flat base is exactly on z = 0
    angles = np.linspace(0.0, 2.0 * np.pi, segments, endpoint=False)
    vertices = np.stack([np.outer(r, np.cos(angles)), np.outer(r, np.sin(angles)),
                         np.repeat(z[:, None], segments, axis=1)], axis=2).reshape(-1, 3)
    i = np.arange(profile)[:, None]
    j = np.arange(segments)[None, :]
    a = i * segments + j
    b = ((i + 1) % profile) * segments + j
    c = ((i + 1) % profile) * segments + (j + 1) % segments
    d = i * segments + (j + 1) % segments
    faces = np.concatenate([np.stack([a, c, b], axis=-1).reshape(-1, 3),
                            np.stack([a, d, c], axis=-1).reshape(-1, 3)])
    return vertices, faces


def mesh_stl(vertices, faces, label=b"PenguinCAM measure_plate hub"):
    """A binary STL of an indexed mesh, with real normals."""
    tris = vertices[faces]
    normals = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
    normals /= np.linalg.norm(normals, axis=1, keepdims=True)
    record = np.dtype([("normal", "<f4", 3), ("v", "<f4", (3, 3)), ("attr", "<u2")])
    body = np.zeros(len(tris), dtype=record)
    body["normal"] = normals
    body["v"] = tris
    return label.ljust(80, b"\0") + np.uint32(len(tris)).tobytes() + body.tobytes()


def hub_stl(triangles):
    return mesh_stl(*hub_mesh(triangles))


def grid_centres(count):
    return [(GRID_ORIGIN_MM + PITCH_MM * (n % GRID_COLUMNS), GRID_ORIGIN_MM + PITCH_MM * (n // GRID_COLUMNS))
            for n in range(count)]


def plate_parts(plate, triangles=None, part_triangles=None):
    """(copies, triangles per copy) for a plate."""
    if plate == "max-copies":
        return MAX_COPIES, (triangles or MAX_JOB_TRIANGLES) // MAX_COPIES
    part = part_triangles or MAX_PART_TRIANGLES
    return math.ceil(MAX_JOB_TRIANGLES / part), part


def plate_layout(stl_path, copies):
    """The plate as print/plate_3mf.py takes it: `copies` hubs, upright, on the grid."""
    stl = Path(stl_path).read_bytes()
    parts = {"hub": {"name": "Hub", "stl_path": Path(stl_path),
                     "footprints": orientation_footprints(read_binary_stl(stl, "Hub"))}}
    placed = [{"ref": "hub", "orientation": "+z", "angle": 0, "x": x, "y": y} for x, y in grid_centres(copies)]
    return placed, parts


def measure(plate, triangles=None, part_triangles=None):
    copies, per_copy = plate_parts(plate, triangles, part_triangles)
    stl = hub_stl(per_copy)
    total = copies * int(np.frombuffer(stl, dtype="<u4", count=1, offset=80)[0])
    code, names_ok = 0, False
    with tempfile.TemporaryDirectory(prefix="measure_plate_") as tmp:
        out = Path(tmp)
        stl_path = out / "hub.stl"
        stl_path.write_bytes(stl)
        placed, parts = plate_layout(stl_path, copies)
        model = out / "plate.3mf"
        names = write_plate_3mf(model, placed, parts)
        started = time.monotonic()
        try:
            result = slice_plate(model, DEFAULT_PROFILES, out / "slice")
            names_ok = sorted(plate_object_names(result.output_path)) == sorted(names)
        except SliceError as exc:
            code = exc.code if exc.code is not None else "error"
        seconds = time.monotonic() - started
        peak_kb = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    if code == "timeout" or peak_kb > MEASURE_MAX_PEAK_KB or seconds > MEASURE_MAX_SECONDS:
        verdict = "over"
    elif code != 0 or not names_ok:
        verdict = "failed"
    else:
        verdict = "ok"
    line = (f"plate={plate} copies={copies} triangles={total} seconds={seconds:.1f} "
            f"peak_kb={peak_kb} exit={code} names={'ok' if names_ok else 'missing'} verdict={verdict}")
    return line, verdict


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plate", choices=PLATES, required=True)
    parser.add_argument("--triangles", type=int, help="max-copies: about N triangles in all")
    parser.add_argument("--part-triangles", type=int, help="max-part: P triangles per part")
    args = parser.parse_args(argv)
    if args.triangles and args.plate != "max-copies":
        parser.error("--triangles applies to the max-copies plate only")
    if args.part_triangles and args.plate != "max-part":
        parser.error("--part-triangles applies to the max-part plate only")
    line, verdict = measure(args.plate, args.triangles, args.part_triangles)
    print(line)
    return 0 if verdict == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())

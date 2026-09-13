#!/usr/bin/env python3
"""Write print/sample_part.stl: an L bracket 40 x 30 x 12 mm as a binary STL.

The polygon (0,0) (40,0) (40,10) (10,10) (10,30) (0,30) is extruded 12 mm in
Z. Twenty triangles, one closed shell, outward normals, counter-clockwise
winding seen from outside. Deterministic: the checked-in file must equal the
output byte for byte (tests/test_make_sample_part.py).
"""
import struct
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUTPUT = REPO / "print" / "sample_part.stl"

FOOTPRINT = [(0.0, 0.0), (40.0, 0.0), (40.0, 10.0), (10.0, 10.0), (10.0, 30.0), (0.0, 30.0)]
HEIGHT = 12.0
# Each cap is the hexagon fanned from its reflex vertex (10,10), as indices
# into FOOTPRINT. The fan uses only real polygon vertices, so every diagonal it
# introduces is shared by two cap triangles running opposite ways and every cap
# boundary edge is exactly one footprint edge, matching one side wall. Splitting
# the L into two rectangles instead would leave T-junctions -- a cap edge
# (0,10)-(40,10) crossing the vertex (10,10), and a vertex (0,10) that the
# left wall does not have -- and the shell would not be closed.
CAP_FAN = [(3, 4, 5), (3, 5, 0), (3, 0, 1), (3, 1, 2)]


def _normal(a, b, c):
    ux, uy, uz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
    vx, vy, vz = c[0] - a[0], c[1] - a[1], c[2] - a[2]
    nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
    length = (nx * nx + ny * ny + nz * nz) ** 0.5 or 1.0
    return (nx / length, ny / length, nz / length)


def l_bracket_triangles():
    tris = []
    for i, j, k in CAP_FAN:
        (xi, yi), (xj, yj), (xk, yk) = FOOTPRINT[i], FOOTPRINT[j], FOOTPRINT[k]
        tris.append(((xi, yi, HEIGHT), (xj, yj, HEIGHT), (xk, yk, HEIGHT)))
        tris.append(((xi, yi, 0.0), (xk, yk, 0.0), (xj, yj, 0.0)))  # reversed: faces -Z
    n = len(FOOTPRINT)
    for i in range(n):
        (xa, ya), (xb, yb) = FOOTPRINT[i], FOOTPRINT[(i + 1) % n]
        a0, b0 = (xa, ya, 0.0), (xb, yb, 0.0)
        a1, b1 = (xa, ya, HEIGHT), (xb, yb, HEIGHT)
        tris.append((a0, b0, b1))
        tris.append((a0, b1, a1))
    return tris


def write_l_bracket(path):
    tris = l_bracket_triangles()
    header = b"PenguinCAM sample part: L bracket 40x30x12 mm".ljust(80, b"\0")
    body = [header, struct.pack("<I", len(tris))]
    for a, b, c in tris:
        body.append(struct.pack("<12fH", *_normal(a, b, c), *a, *b, *c, 0))
    Path(path).write_bytes(b"".join(body))
    return len(tris)


if __name__ == "__main__":
    count = write_l_bracket(OUTPUT)
    print(f"wrote {OUTPUT} ({count} triangles)")
    sys.exit(0)

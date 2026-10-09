"""Writes the SYNTHETIC STL fixtures the mesh checks run against until part-export is recorded.

Each stands in for one export of a test document (spec 5.1), shaped the way Onshape
exports it:

- box_mm.stl     tb-box, units=millimeter&mode=binary: 40 x 30 x 10 mm, binary
- box_inch.stl   tb-box with Onshape's defaults (units=inch, mode=text): the same box in
                 inches, ASCII
- inch_mm.stl    tb-inch, millimetres: the 2 x 1 x 0.5 in block, binary
- slab_mm.stl    tb-oversized, millimetres: 400 x 50 x 10 mm, binary

Run from the repository root: uv run python testbed/tests/fixtures/stl/make_fixtures.py
"""
import struct
from pathlib import Path

HERE = Path(__file__).resolve().parent
INCH_MM = 25.4


def box_triangles(x, y, z):
    """The 12 triangles of an axis-aligned box from the origin to (x, y, z)."""
    v = [(0, 0, 0), (x, 0, 0), (x, y, 0), (0, y, 0), (0, 0, z), (x, 0, z), (x, y, z), (0, y, z)]
    faces = [((0, 0, -1), (0, 2, 1, 0, 3, 2)), ((0, 0, 1), (4, 5, 6, 4, 6, 7)),
             ((0, -1, 0), (0, 1, 5, 0, 5, 4)), ((0, 1, 0), (3, 7, 6, 3, 6, 2)),
             ((-1, 0, 0), (0, 4, 7, 0, 7, 3)), ((1, 0, 0), (1, 2, 6, 1, 6, 5))]
    for normal, idx in faces:
        yield normal, (v[idx[0]], v[idx[1]], v[idx[2]])
        yield normal, (v[idx[3]], v[idx[4]], v[idx[5]])


def binary_stl(triangles, name):
    triangles = list(triangles)
    out = bytearray(name.encode().ljust(80, b' ')[:80])
    out += struct.pack('<I', len(triangles))
    for normal, corners in triangles:
        out += struct.pack('<12fH', *normal, *corners[0], *corners[1], *corners[2], 0)
    return bytes(out)


def ascii_stl(triangles, name):
    lines = [f'solid {name}']
    for normal, corners in triangles:
        lines.append('  facet normal {:e} {:e} {:e}'.format(*normal))
        lines.append('    outer loop')
        for corner in corners:
            lines.append('      vertex {:.6e} {:.6e} {:.6e}'.format(*corner))
        lines.append('    endloop')
        lines.append('  endfacet')
    lines.append(f'endsolid {name}')
    return ('\n'.join(lines) + '\n').encode()


def main():
    (HERE / 'box_mm.stl').write_bytes(binary_stl(box_triangles(40, 30, 10), 'tb-box mm'))
    (HERE / 'box_inch.stl').write_bytes(
        ascii_stl(box_triangles(40 / INCH_MM, 30 / INCH_MM, 10 / INCH_MM), 'tb-box'))
    (HERE / 'inch_mm.stl').write_bytes(
        binary_stl(box_triangles(2 * INCH_MM, 1 * INCH_MM, 0.5 * INCH_MM), 'tb-inch mm'))
    (HERE / 'slab_mm.stl').write_bytes(binary_stl(box_triangles(400, 50, 10), 'tb-oversized mm'))


if __name__ == '__main__':
    main()

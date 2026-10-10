"""The numpy STL reader, its limits and the six orientation footprints."""
import struct
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]        # the repository root
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "testbed" / "tests" / "fixtures"))

from make_print_fixtures import box_stl, cylinder_stl  # noqa: E402
from print import limits  # noqa: E402
from print.mesh import MeshError, orientation_footprints, part_size_mm, read_binary_stl  # noqa: E402
from print.plate import ORIENTATION_MATRICES, ORIENTATIONS  # noqa: E402


def hull_box(hull):
    xs, ys = [p[0] for p in hull], [p[1] for p in hull]
    return min(xs), max(xs), min(ys), max(ys)


def signed_area(hull):
    return sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in zip(hull, hull[1:] + hull[:1])) / 2


class ReadTest(unittest.TestCase):
    def test_reads_binary_box_and_size(self):
        mesh = read_binary_stl(box_stl(), "Box")
        self.assertEqual(mesh.triangles, 12)
        self.assertEqual(mesh.vertices.shape, (8, 3))      # de-duplicated
        self.assertEqual(mesh.faces.shape, (12, 3))
        self.assertEqual(part_size_mm(mesh), {"x": 10.0, "y": 20.0, "z": 30.0})

    def test_ascii_or_truncated_refused(self):
        expected = "Onshape could not export Box. Try again, or Refresh from Onshape."
        for data in (b"solid box\nendsolid box\n", box_stl()[:-1], b"", box_stl()[:84] + b"",
                     b" " * 80 + struct.pack("<I", 0)):
            with self.subTest(size=len(data)), self.assertRaises(MeshError) as caught:
                read_binary_stl(data, "Box")
            self.assertEqual(caught.exception.message, expected)

    def test_non_finite_refused(self):
        data = bytearray(box_stl())
        data[84 + 12:84 + 16] = struct.pack("<f", float("nan"))
        with self.assertRaises(MeshError):
            read_binary_stl(bytes(data), "Box")

    def test_triangle_cap(self):
        count = limits.MAX_PART_TRIANGLES + 1
        data = b" " * 80 + struct.pack("<I", count) + bytes(50 * count)
        with self.assertRaises(MeshError) as caught:
            read_binary_stl(data, "Gear")
        self.assertEqual(caught.exception.message,
                         "Gear is too detailed to print here (over 150,000 triangles).")

    def test_tiny_part_refused(self):
        with self.assertRaises(MeshError) as caught:
            read_binary_stl(box_stl(10, 20, 0.1), "Shim")
        self.assertEqual(caught.exception.message, "Shim is too small to print (under 0.1 mm).")
        read_binary_stl(box_stl(10, 20, 0.11), "Shim")


class FootprintTest(unittest.TestCase):
    def test_matrices_put_the_named_axis_up(self):
        for name in ORIENTATIONS:
            sign = 1 if name[0] == "+" else -1
            axis = "xyz".index(name[1])
            up = [row[axis] * sign for row in ORIENTATION_MATRICES[name]]
            self.assertEqual(up, [0, 0, 1], name)

    def test_footprints_six_orientations(self):
        mesh = read_binary_stl(box_stl(), "Box")
        footprints = orientation_footprints(mesh)
        self.assertEqual(set(footprints), set(ORIENTATIONS))
        spans = {"+z": (10, 20, 30), "-z": (10, 20, 30), "+x": (30, 20, 10), "-x": (30, 20, 10),
                 "+y": (10, 30, 20), "-y": (10, 30, 20)}
        for name, (sx, sy, height) in spans.items():
            with self.subTest(orientation=name):
                footprint = footprints[name]
                x0, x1, y0, y1 = hull_box(footprint["hull"])
                self.assertAlmostEqual(x1 - x0, sx, places=3)
                self.assertAlmostEqual(y1 - y0, sy, places=3)
                self.assertAlmostEqual(x0 + x1, 0, places=3)        # centred on (0, 0)
                self.assertAlmostEqual(y0 + y1, 0, places=3)
                self.assertAlmostEqual(footprint["height"], height, places=3)
                self.assertGreater(signed_area(footprint["hull"]), 0)   # counterclockwise
        # offset: the bounding-box centre and lowest point after the matrix.
        self.assertEqual(footprints["+z"]["offset"], [5.0, 10.0, 0.0])
        self.assertEqual(footprints["-z"]["offset"], [5.0, -10.0, -30.0])
        self.assertEqual(footprints["+x"]["offset"], [-15.0, 10.0, 0.0])
        self.assertEqual(footprints["-x"]["offset"], [15.0, 10.0, -10.0])
        self.assertEqual(footprints["-y"]["offset"], [5.0, 15.0, -20.0])

    def test_opposite_orientations_mirror(self):
        mesh = read_binary_stl(cylinder_stl(), "Cylinder")
        footprints = orientation_footprints(mesh)
        plus, minus = footprints["+z"]["hull"], footprints["-z"]["hull"]
        self.assertEqual(sorted((x, -y) for x, y in plus), sorted(map(tuple, minus)))
        self.assertEqual(footprints["+z"]["height"], footprints["-z"]["height"])
        self.assertGreater(signed_area(minus), 0)
        self.assertGreater(len(plus), 60)                   # a 64-segment cylinder's hull


if __name__ == "__main__":
    unittest.main()

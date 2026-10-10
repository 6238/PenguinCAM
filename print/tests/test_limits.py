"""The print path's numeric limits and the plate-measurement script (no Orca run)."""
import math
import re
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from print import limits
from print.slicer import _binary_triangle_count

REPO = Path(__file__).resolve().parents[2]        # the repository root
sys.path.insert(0, str(REPO / "print" / "scripts"))

from measure_plate import hub_mesh, hub_stl, plate_parts, write_measure_3mf  # noqa: E402


class LimitsTest(unittest.TestCase):
    def test_part_bytes_follow_part_triangles(self):
        self.assertEqual(limits.MAX_PART_BYTES, 7_500_084)
        self.assertEqual(limits.MAX_PART_BYTES, 84 + 50 * limits.MAX_PART_TRIANGLES)

    def test_one_part_always_fits_a_job(self):
        self.assertGreaterEqual(limits.MAX_JOB_TRIANGLES, limits.MAX_PART_TRIANGLES)

    def test_plate_timeout_longer_than_sample_timeout(self):
        from print.slicer import DEFAULT_TIMEOUT_S
        self.assertEqual(DEFAULT_TIMEOUT_S, 120)
        self.assertGreater(limits.PLATE_SLICE_TIMEOUT_S, DEFAULT_TIMEOUT_S)

    def test_tessellation_values_valid(self):
        self.assertTrue(0 < limits.STL_ANGLE_TOLERANCE_RAD < math.pi / 2)
        self.assertTrue(0 < limits.STL_CHORD_TOLERANCE_M < 0.001)


class MeasurePlateTest(unittest.TestCase):
    def test_hub_triangle_count_and_closed_shell(self):
        data = hub_stl(10_000)
        count = _binary_triangle_count(data)
        self.assertIsNotNone(count)                      # the binary length rule holds
        self.assertTrue(9_500 <= count <= 10_000, count)
        _vertices, faces = hub_mesh(10_000)
        edges = {}
        for a, b, c in faces.tolist():
            for edge in ((a, b), (b, c), (c, a)):
                edges[edge] = edges.get(edge, 0) + 1
        # Closed and consistently wound: every directed edge once, its reverse once.
        self.assertTrue(all(n == 1 and edges.get((y, x)) == 1 for (x, y), n in edges.items()))

    def test_plates_follow_the_limits(self):
        self.assertEqual(plate_parts("thirty"), (limits.MAX_COPIES, limits.MAX_JOB_TRIANGLES // limits.MAX_COPIES))
        copies, part = plate_parts("max-part")
        self.assertEqual(part, limits.MAX_PART_TRIANGLES)
        self.assertEqual(copies, math.ceil(limits.MAX_JOB_TRIANGLES / limits.MAX_PART_TRIANGLES))
        self.assertEqual(plate_parts("max-part", part_triangles=50_000), (6, 50_000))

    def test_measure_3mf_one_object_per_copy(self):
        centres = [(40.0, 40.0), (90.0, 40.0), (140.5, 90.25)]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "plate.3mf"
            names = write_measure_3mf(path, hub_stl(200), centres)
            with zipfile.ZipFile(path) as zf:
                entries = set(zf.namelist())
                model = zf.read("3D/3dmodel.model").decode("utf-8")
        self.assertEqual(names, ["Hub #1", "Hub #2", "Hub #3"])
        self.assertTrue({"[Content_Types].xml", "_rels/.rels", "3D/3dmodel.model"} <= entries)
        self.assertIn('unit="millimeter"', model)
        objects = re.findall(r'<object id="(\d+)"[^>]*name="([^"]*)"', model)
        self.assertEqual([n for _id, n in objects], names)
        items = re.findall(r'<item objectid="(\d+)" transform="([^"]*)"', model)
        self.assertEqual([i for i, _t in items], [i for i, _n in objects])
        for (_id, transform), (x, y) in zip(items, centres):
            values = [float(v) for v in transform.split()]
            self.assertEqual(values[:9], [1, 0, 0, 0, 1, 0, 0, 0, 1])
            self.assertEqual(values[9:], [x, y, 0.0])


if __name__ == "__main__":
    unittest.main()

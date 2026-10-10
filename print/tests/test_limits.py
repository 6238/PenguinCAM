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

from measure_plate import cylinder_stl, write_measure_3mf  # noqa: E402


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
    def test_cylinder_triangle_count(self):
        data = cylinder_stl(18.0, 20.0, 2500)
        self.assertEqual(_binary_triangle_count(data), 10_000)
        self.assertEqual(len(data), 84 + 50 * 10_000)

    def test_measure_3mf_one_object_per_copy(self):
        centres = [(40.0, 40.0), (90.0, 40.0), (140.5, 90.25)]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "plate.3mf"
            names = write_measure_3mf(path, cylinder_stl(18.0, 20.0, 16), centres)
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

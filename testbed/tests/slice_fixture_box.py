"""tb-box's export slices with the real Orca and the fixed profile set (spec 5.7).

Slow, and needs Orca: not matched by discover's test_*.py pattern; `make test` runs this
module explicitly. The mesh is the fixture until part-export is recorded, then the recording.
"""
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

from print.slicer import slice_stl
from testbed.tests.test_scenarios import exported_stl


class SliceFixtureBoxTest(unittest.TestCase):
    def test_box_export_slices(self):
        work = Path(tempfile.mkdtemp(prefix='testbed-slice-')).resolve()
        self.addCleanup(shutil.rmtree, work, ignore_errors=True)
        stl = work / 'box_mm.stl'
        stl.write_bytes(exported_stl('tb-box', 'millimeter'))
        result = slice_stl(stl, work / 'out')
        self.assertTrue(zipfile.is_zipfile(result.output_path))
        self.assertGreater(result.layer_count, 0)
        self.assertGreater(result.filament_g, 0)


if __name__ == '__main__':
    unittest.main()

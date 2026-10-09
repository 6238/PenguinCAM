"""Tests for testbed/mesh.py: an exported mesh's size in millimetres."""
import unittest
from pathlib import Path

from testbed.mesh import mesh_size_mm

STL_DIR = Path(__file__).resolve().parent / 'fixtures' / 'stl'


class MeshSizeTest(unittest.TestCase):
    def assertSizes(self, got, expected, places=3):
        for g, e in zip(got, expected, strict=True):
            self.assertAlmostEqual(g, e, places=places)

    def test_mesh_size_binary_and_ascii(self):
        binary = (STL_DIR / 'box_mm.stl').read_bytes()
        self.assertNotIn(b'facet', binary)
        self.assertSizes(mesh_size_mm(binary, 'millimeter'), (10, 30, 40))
        ascii_stl = (STL_DIR / 'box_inch.stl').read_bytes()
        self.assertTrue(ascii_stl.startswith(b'solid'))
        self.assertSizes(mesh_size_mm(ascii_stl, 'millimeter'),
                         (10 / 25.4, 30 / 25.4, 40 / 25.4), places=5)

    def test_inch_scaling(self):
        ascii_stl = (STL_DIR / 'box_inch.stl').read_bytes()
        self.assertSizes(mesh_size_mm(ascii_stl, 'inch'), (10, 30, 40))
        with self.assertRaisesRegex(ValueError, 'furlong'):
            mesh_size_mm(ascii_stl, 'furlong')


if __name__ == '__main__':
    unittest.main()

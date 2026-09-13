"""The fixed sample part is a small, closed, binary STL in millimetres."""
import struct
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

from make_sample_part import write_l_bracket  # noqa: E402


def read_binary_stl(data):
    count = struct.unpack("<I", data[80:84])[0]
    tris = []
    for i in range(count):
        rec = struct.unpack("<12fH", data[84 + i * 50: 84 + (i + 1) * 50])
        tris.append((rec[3:6], rec[6:9], rec[9:12]))
    return tris


class SamplePartTest(unittest.TestCase):
    def test_generator_writes_twenty_triangle_l_bracket(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "part.stl"
            self.assertEqual(write_l_bracket(path), 20)
            data = path.read_bytes()
        self.assertEqual(len(data), 84 + 20 * 50)
        tris = read_binary_stl(data)
        xs = [v[0] for t in tris for v in t]
        ys = [v[1] for t in tris for v in t]
        zs = [v[2] for t in tris for v in t]
        self.assertEqual((min(xs), max(xs)), (0.0, 40.0))
        self.assertEqual((min(ys), max(ys)), (0.0, 30.0))
        self.assertEqual((min(zs), max(zs)), (0.0, 12.0))

    def test_mesh_is_closed(self):
        # Every directed edge must be matched by its reverse exactly once.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "part.stl"
            write_l_bracket(path)
            tris = read_binary_stl(path.read_bytes())
        edges = {}
        for a, b, c in tris:
            for u, v in ((a, b), (b, c), (c, a)):
                edges[(u, v)] = edges.get((u, v), 0) + 1
        for (u, v), n in edges.items():
            self.assertEqual(n, 1, f"edge {u}->{v} used {n} times")
            self.assertEqual(edges.get((v, u), 0), 1, f"edge {u}->{v} has no twin")

    def test_checked_in_stl_matches_generator(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "part.stl"
            write_l_bracket(path)
            fresh = path.read_bytes()
        self.assertEqual((REPO / "print" / "sample_part.stl").read_bytes(), fresh)


if __name__ == "__main__":
    unittest.main()

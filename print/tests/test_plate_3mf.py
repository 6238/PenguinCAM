"""The plate 3MF writer, the delivered name and the name guard (print/plate_3mf.py). No Orca run."""
import json
import re
import shutil
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]        # the repository root
sys.path.insert(0, str(REPO))

from print import printer_relay  # noqa: E402
from print.mesh import orientation_footprints, read_binary_stl  # noqa: E402
from print.plate_3mf import (LEFT_OFF_MESSAGE, LOCAL_TIME_RE, check_plate_names,  # noqa: E402
                             delivered_file_name, plate_object_names, write_plate_3mf)
from print.slicer import SliceError  # noqa: E402

NS = {'m': 'http://schemas.microsoft.com/3dmanufacturing/core/2015/02'}


def box_stl(sx, sy, sz, origin=(0.0, 0.0, 0.0)):
    """A binary STL of an axis-aligned box with one corner at `origin`, real normals."""
    x0, y0, z0 = origin
    corners = np.array([[x0 + dx * sx, y0 + dy * sy, z0 + dz * sz]
                        for dz in (0, 1) for dy in (0, 1) for dx in (0, 1)], dtype=np.float64)
    faces = [(0, 2, 1), (1, 2, 3), (4, 5, 6), (5, 7, 6), (0, 1, 4), (1, 5, 4),
             (2, 6, 3), (3, 6, 7), (0, 4, 2), (2, 4, 6), (1, 3, 5), (3, 7, 5)]
    return mesh_stl(corners, faces)


def mesh_stl(vertices, faces):
    tris = np.asarray(vertices, dtype=np.float64)[np.asarray(faces)]
    normals = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
    normals /= np.linalg.norm(normals, axis=1, keepdims=True)
    record = np.dtype([('normal', '<f4', 3), ('v', '<f4', (3, 3)), ('attr', '<u2')])
    body = np.zeros(len(tris), dtype=record)
    body['normal'] = normals
    body['v'] = tris
    return b'part'.ljust(80, b'\0') + np.uint32(len(tris)).tobytes() + body.tobytes()


def cylinder_stl(radius, height, segments=48):
    """A closed upright cylinder on z = 0, centred on the z axis."""
    angles = np.linspace(0.0, 2 * np.pi, segments, endpoint=False)
    ring = np.stack([radius * np.cos(angles), radius * np.sin(angles)], axis=1)
    vertices = np.vstack([np.hstack([ring, np.zeros((segments, 1))]), np.hstack([ring, np.full((segments, 1), height)]),
                          [[0, 0, 0], [0, 0, height]]])
    bottom, top = 2 * segments, 2 * segments + 1
    faces = []
    for i in range(segments):
        j = (i + 1) % segments
        faces += [(i, j, segments + j), (i, segments + j, segments + i), (bottom, j, i),
                  (top, segments + i, segments + j)]
    return mesh_stl(vertices, faces)


def cylinder_part(directory, name, radius, height):
    return _part(directory, name, cylinder_stl(radius, height))


def _part(directory, name, stl):
    path = Path(directory) / f'{len(list(Path(directory).iterdir()))}.stl'
    path.write_bytes(stl)
    return {'name': name, 'stl_path': path, 'footprints': orientation_footprints(read_binary_stl(stl, name))}


def make_part(directory, name, size, origin=(0.0, 0.0, 0.0)):
    """A parts[ref] entry for write_plate_3mf, its STL written under `directory`."""
    return _part(directory, name, box_stl(*size, origin=origin))


def read_model(path):
    with zipfile.ZipFile(path) as zf:
        entries = set(zf.namelist())
        root = ET.fromstring(zf.read('3D/3dmodel.model'))
    return entries, root


def write_archive(path, names):
    with zipfile.ZipFile(path, 'w') as zf:
        zf.writestr('Metadata/plate_1.json', json.dumps({'bbox_objects': [{'name': n} for n in names]}))


class WriterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='plate-3mf-'))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.stls = self.tmp / 'stls'
        self.stls.mkdir()

    def copy(self, ref, x, y, orientation='+z', angle=0):
        return {'ref': ref, 'orientation': orientation, 'angle': angle, 'x': x, 'y': y}

    def test_one_object_per_copy_named_with_labels(self):
        parts = {'a': make_part(self.stls, 'Bracket', (20, 10, 5)), 'b': make_part(self.stls, 'Spacer', (8, 8, 8))}
        copies = [self.copy('a', 50, 50), self.copy('a', 100, 50), self.copy('b', 150, 50)]
        names = write_plate_3mf(self.tmp / 'plate.3mf', copies, parts)
        self.assertEqual(names, ['Bracket #1', 'Bracket #2', 'Spacer #1'])
        entries, root = read_model(self.tmp / 'plate.3mf')
        self.assertTrue({'[Content_Types].xml', '_rels/.rels', '3D/3dmodel.model'} <= entries)
        self.assertEqual(root.get('unit'), 'millimeter')
        objects = root.findall('m:resources/m:object', NS)
        self.assertEqual([o.get('name') for o in objects], names)
        self.assertTrue(all(o.get('type') == 'model' for o in objects))
        self.assertEqual(len({o.get('id') for o in objects}), 3)
        items = root.findall('m:build/m:item', NS)
        self.assertEqual([i.get('objectid') for i in items], [o.get('id') for o in objects])
        # Each copy carries its part's de-duplicated mesh: a box is 8 vertices, 12 triangles.
        for obj in objects:
            self.assertEqual(len(obj.findall('m:mesh/m:vertices/m:vertex', NS)), 8)
            self.assertEqual(len(obj.findall('m:mesh/m:triangles/m:triangle', NS)), 12)

    def test_same_named_parts_get_distinct_object_names(self):
        parts = {'a': make_part(self.stls, 'Bracket', (20, 10, 5)), 'b': make_part(self.stls, 'Bracket', (8, 8, 8)),
                 'c': make_part(self.stls, 'Brack?t', (8, 8, 8))}
        copies = [self.copy('a', 50, 50), self.copy('b', 100, 50), self.copy('a', 150, 50), self.copy('c', 200, 50)]
        names = write_plate_3mf(self.tmp / 'plate.3mf', copies, parts)
        self.assertEqual(names, ['Bracket #1', 'Bracket #2', 'Bracket #3', 'Brack_t #1'])
        self.assertEqual(len(set(names)), len(names))

    def test_transform_read_back_puts_footprint_centre_at_xy(self):
        # Uncentred, so the footprint offset (R10) matters.
        parts = {'a': make_part(self.stls, 'Bracket', (40, 10, 6), origin=(5.0, -3.0, 2.0))}
        cases = [('+z', 0, 100.0, 120.0), ('+x', 30, 150.5, 80.25), ('-y', 275, 60.0, 200.0), ('-z', 90, 170.0, 160.0)]
        copies = [self.copy('a', x, y, o, a) for o, a, x, y in cases]
        write_plate_3mf(self.tmp / 'plate.3mf', copies, parts)
        _entries, root = read_model(self.tmp / 'plate.3mf')
        objects = {o.get('id'): o for o in root.findall('m:resources/m:object', NS)}
        for item, (_o, _a, x, y) in zip(root.findall('m:build/m:item', NS), cases):
            values = [float(v) for v in item.get('transform').split()]
            matrix = np.array(values).reshape(4, 3)          # 3MF rows: p' = [p 1] . M
            vertices = np.array([[float(v.get(k)) for k in 'xyz']
                                 for v in objects[item.get('objectid')].findall('m:mesh/m:vertices/m:vertex', NS)])
            placed = np.hstack([vertices, np.ones((len(vertices), 1))]) @ matrix
            low, high = placed.min(axis=0), placed.max(axis=0)
            self.assertAlmostEqual((low[0] + high[0]) / 2, x, delta=1e-3)
            self.assertAlmostEqual((low[1] + high[1]) / 2, y, delta=1e-3)
            self.assertAlmostEqual(low[2], 0.0, delta=1e-3)

    def test_names_xml_escaped_and_sanitized(self):
        parts = {'a': make_part(self.stls, 'A&B "q" <x>', (10, 10, 10))}
        names = write_plate_3mf(self.tmp / 'plate.3mf', [self.copy('a', 50, 50)], parts)
        self.assertEqual(names, ['A_B__q___x_ #1'])
        _entries, root = read_model(self.tmp / 'plate.3mf')     # parses: the XML is well formed
        self.assertEqual(root.find('m:resources/m:object', NS).get('name'), 'A_B__q___x_ #1')


class DeliveredNameTest(unittest.TestCase):
    NOW = datetime(2026, 10, 9, 21, 32, tzinfo=timezone.utc)

    def test_delivered_name_patterns(self):
        self.assertEqual(delivered_file_name(['Bracket', 'Spacer', 'Hub'], '20261009-1432'),
                         'Bracket_plus2-20261009-1432.gcode.3mf')
        self.assertEqual(delivered_file_name(['Bracket'], '20261009-1432'), 'Bracket-20261009-1432.gcode.3mf')
        self.assertEqual(delivered_file_name(['A&B'], '20261009-1432'), 'A_B-20261009-1432.gcode.3mf')
        for bad in ('2026-10-09', None, '20261009-1432\n', 'x' * 13, 20261009):
            self.assertEqual(delivered_file_name(['Bracket'], bad, now=self.NOW),
                             'Bracket-20261009-2132.gcode.3mf', bad)
        self.assertTrue(LOCAL_TIME_RE.match('20261009-1432'))
        for parts in (['j1234567'], ['Box', 'j1234567'], ['x-j1234567'], ['j12345678']):
            name = delivered_file_name(parts, '20261009-1432')
            self.assertIsNone(printer_relay._JOB_SUFFIX_RE.search(name), name)
            self.assertEqual(printer_relay.strip_job_suffix(name), name)
        self.assertTrue(re.fullmatch(r'Bracket-\d{8}-\d{4}\.gcode\.3mf', delivered_file_name(['Bracket'], None)))


class NameGuardTest(unittest.TestCase):
    def test_name_guard_catches_missing_and_blank(self):
        check_plate_names(['Box #1', 'Box #2'], ['Box #2', 'Box #1'])
        for found in (['Box #1'], ['Box #1', ''], ['Box #1', 'Box #2', 'Box #3'], []):
            with self.subTest(found=found):
                with self.assertRaises(SliceError) as ctx:
                    check_plate_names(['Box #1', 'Box #2'], found)
                self.assertEqual(ctx.exception.message, LEFT_OFF_MESSAGE)
        with self.assertRaises(SliceError):
            check_plate_names(['', 'Box #1'], ['', 'Box #1'])

    def test_plate_object_names_read_from_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'plate.gcode.3mf'
            write_archive(path, ['Box #1', 'Hub #1'])
            self.assertEqual(plate_object_names(path), ['Box #1', 'Hub #1'])
            with zipfile.ZipFile(path, 'w') as zf:
                zf.writestr('Metadata/plate_1.gcode', '')
            with self.assertRaises(SliceError):
                plate_object_names(path)


if __name__ == '__main__':
    unittest.main()

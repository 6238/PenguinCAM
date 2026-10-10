"""Reading an exported part's binary STL with numpy, and its footprints. Flask-free.

The pure-Python print.slicer.stl_bounds stays for the sample part only; an exported part
can be 150,000 triangles, which numpy reads in a fraction of the time and memory.
"""
from dataclasses import dataclass

import numpy as np
import shapely

from . import limits
from .plate import ORIENTATION_MATRICES
from .slicer import _binary_triangle_count

_TRIANGLE = np.dtype([('normal', '<f4', (3,)), ('v0', '<f4', (3,)), ('v1', '<f4', (3,)),
                      ('v2', '<f4', (3,)), ('attr', '<u2')])
# Each orientation computed directly; its opposite is the same projection mirrored in one
# plate axis (index 0 for x, 1 for y), with the part turned upside down.
_MIRRORS = {'-z': ('+z', 1), '-x': ('+x', 0), '-y': ('+y', 1)}


class MeshError(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.message = message


@dataclass
class Mesh:
    vertices: np.ndarray        # N x 3 float64, unique
    faces: np.ndarray           # M x 3 int, indices into vertices
    triangles: int


def read_binary_stl(data, name):
    """The mesh of a binary STL, refused with a MeshError the student can read."""
    count = _binary_triangle_count(data)
    if not count:
        raise MeshError(limits.EXPORT_FAILED_MESSAGE.format(name=name))
    if count > limits.MAX_PART_TRIANGLES:
        raise MeshError(limits.TOO_DETAILED_MESSAGE.format(name=name))
    records = np.frombuffer(data, dtype=_TRIANGLE, count=count, offset=84)
    corners = np.stack((records['v0'], records['v1'], records['v2']), axis=1).reshape(-1, 3)
    corners = corners.astype(np.float64)
    if not np.isfinite(corners).all():
        raise MeshError(limits.EXPORT_FAILED_MESSAGE.format(name=name))
    vertices, inverse = np.unique(corners, axis=0, return_inverse=True)
    mesh = Mesh(vertices, inverse.reshape(-1, 3), count)
    extent = vertices.max(axis=0) - vertices.min(axis=0)
    # float32 cannot hold 0.1 exactly, so a 0.1 mm side reads as 0.10000000149.
    if (extent <= limits.MIN_PART_EXTENT_MM + 1e-6).any():
        raise MeshError(limits.TOO_SMALL_MESSAGE.format(name=name))
    return mesh


def part_size_mm(mesh):
    x, y, z = (mesh.vertices.max(axis=0) - mesh.vertices.min(axis=0)).tolist()
    return {'x': round(x, 3), 'y': round(y, 3), 'z': round(z, 3)}


def _hull(points):
    """Counterclockwise convex hull of 2D points, without the closing point."""
    geometry = shapely.MultiPoint(points).convex_hull
    if geometry.geom_type != 'Polygon':         # every point on one line: its bounding box
        geometry = shapely.MultiPoint(points).envelope
    if geometry.geom_type != 'Polygon':
        geometry = geometry.buffer(0.0005, cap_style='square')
    ring = shapely.geometry.polygon.orient(geometry, 1.0).exterior.coords
    return np.asarray(ring[:-1], dtype=np.float64)


def _footprint(hull, low, high):
    """The footprint dict for a hull and the rotated part's bounding box."""
    cx, cy = (low[0] + high[0]) / 2, (low[1] + high[1]) / 2
    centred = np.round(hull - (cx, cy), 3) + 0.0      # + 0.0 turns -0.0 into 0.0
    return {'hull': centred.tolist(), 'height': round(float(high[2] - low[2]), 3),
            'offset': [round(float(cx), 3) + 0.0, round(float(cy), 3) + 0.0, round(float(low[2]), 3) + 0.0]}


def orientation_footprints(mesh):
    """{orientation: {'hull', 'height', 'offset'}} for each of plate.ORIENTATIONS.

    The hull is the convex hull of the rotated part seen from above, counterclockwise and
    centred on its bounding-box centre; offset is that centre and the lowest z after the
    rotation (R10), so the browser can place the mesh exactly where the slicer will.
    """
    footprints, computed = {}, {}
    for name in ('+z', '+x', '+y'):
        rotated = mesh.vertices @ np.asarray(ORIENTATION_MATRICES[name], dtype=np.float64).T
        low, high = rotated.min(axis=0), rotated.max(axis=0)
        hull = _hull(rotated[:, :2])
        computed[name] = (hull, low, high)
        footprints[name] = _footprint(hull, low, high)
    for name, (base, axis) in _MIRRORS.items():
        hull, low, high = computed[base]
        flip = np.array([-1.0 if i == axis else 1.0 for i in range(2)] + [-1.0])
        mirrored = (hull * flip[:2])[::-1]               # mirroring reverses the winding
        mirrored_low, mirrored_high = np.minimum(low * flip, high * flip), np.maximum(low * flip, high * flip)
        footprints[name] = _footprint(mirrored, mirrored_low, mirrored_high)
    return {name: footprints[name] for name in ORIENTATION_MATRICES}

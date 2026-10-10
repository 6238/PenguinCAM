"""The plate: orientations, copy placement and the rules a placement must pass. Flask-free,
no app imports.

Each orientation is named by the part axis that ends up pointing up. The matrices are
row-major rotations applied to the part's vertices (v' = M v).

The geometry is written twice on purpose: here, where the server checks a placement before
slicing (the authority), and in print/static/plate_geometry.js, where the browser checks
while the student drags. Both are tested against print/tests/fixtures/plate_cases.json.
"""
import math
import re

from shapely.geometry import Polygon

SPACING_MM = 5.0        # the least distance between two footprints
MARGIN_MM = 3.0         # every footprint stays this far inside the printable area
EPS_MM = 1e-6

ORIENTATIONS = ('+z', '-z', '+x', '-x', '+y', '-y')

ORIENTATION_MATRICES = {
    '+z': ((1, 0, 0), (0, 1, 0), (0, 0, 1)),
    '-z': ((1, 0, 0), (0, -1, 0), (0, 0, -1)),
    '+x': ((0, 0, -1), (0, 1, 0), (1, 0, 0)),
    '-x': ((0, 0, 1), (0, 1, 0), (-1, 0, 0)),
    '+y': ((1, 0, 0), (0, 0, -1), (0, 1, 0)),
    '-y': ((1, 0, 0), (0, 0, 1), (0, -1, 0)),
}

PART_NAME_MAX_CHARS = 40
_NOT_NAME_CHAR = re.compile(r'[^A-Za-z0-9_-]')

OFF_PLATE_MESSAGE = "{label} is not fully on the plate."
TOO_TALL_MESSAGE = "{label} is taller than the printer can print ({height:g} mm)."
TOO_CLOSE_MESSAGE = "{label} and {label2} are closer than {spacing:g} mm."


def _cos_sin(angle):
    """cos and sin of `angle` degrees, exact on multiples of 90 (as plate_geometry.js)."""
    if angle % 90 == 0:
        return {0: (1, 0), 1: (0, 1), 2: (-1, 0), 3: (0, -1)}[int(angle // 90) % 4]
    radians = math.radians(angle)
    return math.cos(radians), math.sin(radians)


def copy_matrix(orientation, angle, offset, x, y):
    """The copy's 3x4 [M | t]: M = Rz(angle) R_o, t = (x, y, 0) - Rz(angle) offset, so that
    p_plate = M p_part + t puts the footprint's centre at (x, y) and its lowest point at z 0."""
    c, s = _cos_sin(angle)
    rz = ((c, -s, 0), (s, c, 0), (0, 0, 1))
    r_o = ORIENTATION_MATRICES[orientation]
    m = [[sum(rz[i][k] * r_o[k][j] for k in range(3)) for j in range(3)] for i in range(3)]
    moved = [sum(rz[i][k] * offset[k] for k in range(3)) for i in range(3)]
    t = (x - moved[0], y - moved[1], -moved[2])
    return [m[i] + [t[i]] for i in range(3)]


def plate_from_printer(printer):
    """The plate the browser and the checks use, from an Orca printer profile."""
    corners = [tuple(float(v) for v in corner.lower().split('x')) for corner in printer['printable_area']]
    xs, ys = [c[0] for c in corners], [c[1] for c in corners]
    return {'min': [min(xs), min(ys)], 'max': [max(xs), max(ys)],
            'height': float(printer['printable_height']), 'margin': MARGIN_MM, 'spacing': SPACING_MM,
            'printer': printer.get('name')}


def copy_hull(copy, footprint):
    """The footprint's hull turned by the copy's angle and moved to its (x, y)."""
    c, s = _cos_sin(copy['angle'])
    return [[c * px - s * py + copy['x'], s * px + c * py + copy['y']] for px, py in footprint['hull']]


def sanitize_part_name(name):
    """The part name in the file-name character set: letters, digits, - and _; every other
    character becomes _, at most 40 characters, `part` when nothing is left."""
    return _NOT_NAME_CHAR.sub('_', name)[:PART_NAME_MAX_CHARS] or 'part'


def copy_labels(names):
    """`<sanitized name> #<n>` per copy, n counting each sanitized name in list order."""
    counts, labels = {}, []
    for name in names:
        clean = sanitize_part_name(name)
        counts[clean] = counts.get(clean, 0) + 1
        labels.append(f'{clean} #{counts[clean]}')
    return labels


def check_placement(copies, parts, plate):
    """The placement's problems: every off_plate, then every too_tall, then every too_close
    pair (i, j), i < j. Each is {'rule', 'copies', 'message'}; an empty list passes."""
    labels = copy_labels([parts[copy['ref']]['name'] for copy in copies])
    footprints = [parts[copy['ref']]['footprints'][copy['orientation']] for copy in copies]
    hulls = [copy_hull(copy, footprint) for copy, footprint in zip(copies, footprints)]
    lo = [plate['min'][i] + plate['margin'] - EPS_MM for i in (0, 1)]
    hi = [plate['max'][i] - plate['margin'] + EPS_MM for i in (0, 1)]
    problems = []
    for i, hull in enumerate(hulls):
        if any(not lo[k] <= point[k] <= hi[k] for point in hull for k in (0, 1)):
            problems.append({'rule': 'off_plate', 'copies': [i],
                             'message': OFF_PLATE_MESSAGE.format(label=labels[i])})
    for i, footprint in enumerate(footprints):
        if footprint['height'] > plate['height'] + EPS_MM:
            problems.append({'rule': 'too_tall', 'copies': [i],
                             'message': TOO_TALL_MESSAGE.format(label=labels[i], height=plate['height'])})
    polygons = [Polygon(hull) for hull in hulls]
    for i in range(len(polygons)):
        for j in range(i + 1, len(polygons)):
            if polygons[i].distance(polygons[j]) < plate['spacing'] - EPS_MM:
                problems.append({'rule': 'too_close', 'copies': [i, j],
                                 'message': TOO_CLOSE_MESSAGE.format(label=labels[i], label2=labels[j],
                                                                     spacing=plate['spacing'])})
    return problems

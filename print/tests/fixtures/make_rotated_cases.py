"""Write the rotated cases of plate_cases.json, whose coordinates are irrational.

    uv run python print/tests/fixtures/make_rotated_cases.py

The axis-aligned cases in plate_cases.json are written by hand and stay exact. The cases
with a copy turned by 45 degrees, and one matrix at 30 degrees, are computed here with
shapely and numpy, independently of print/plate.py, and written back into the file. Each
rotated case keeps at least 0.1 mm between its distance and the spacing or margin, so the
1e-6 rounding cannot flip a verdict; the script refuses to write one that does not.
"""
import json
import math
from pathlib import Path

import numpy as np
from shapely.geometry import Polygon

CASES = Path(__file__).resolve().parent / 'plate_cases.json'
CLEARANCE_MM = 0.1


def placed_hull(footprint, angle, x, y):
    a = math.radians(angle)
    c, s = math.cos(a), math.sin(a)
    return [(c * px - s * py + x, s * px + c * py + y) for px, py in footprint['hull']]


def off_plate_by(hull, plate):
    """How far the hull reaches past the margin (negative: how far inside it stays)."""
    lo = [plate['min'][i] + plate['margin'] for i in (0, 1)]
    hi = [plate['max'][i] - plate['margin'] for i in (0, 1)]
    return max(max(lo[i] - p[i], p[i] - hi[i]) for p in hull for i in (0, 1))


def main():
    data = json.loads(CASES.read_text())
    plate, parts = data['plate'], data['parts']
    square = parts['r-square']['footprints']['+z']
    half_diagonal = 10 * math.sqrt(2)
    cases = {}

    # A 20 mm square that fits squarely, 15 mm from the left edge, but not turned by 45 degrees.
    x, y = 15.0, 100.0
    reach = off_plate_by(placed_hull(square, 45, x, y), plate)
    assert reach >= CLEARANCE_MM, reach
    assert off_plate_by(placed_hull(square, 0, x, y), plate) <= -CLEARANCE_MM
    cases['off_plate_after_45_rotation'] = {
        'copies': [{'ref': 'r-square', 'orientation': '+z', 'angle': 45, 'x': x, 'y': y}],
        'problems': [{'rule': 'off_plate', 'copies': [0], 'message': 'Square #1 is not fully on the plate.'}],
        'note': f'reaches {reach:.6f} mm past the margin'}

    # Two squares turned by 45 degrees: bounding boxes 1 mm apart in x, the facing edges
    # parallel and 5.2 mm apart, so the copies pass the 5 mm spacing.
    ax, ay = 100.0, 100.0
    bx = round(ax + 2 * half_diagonal + 1.0, 6)
    by = round(ay + 5.2 * math.sqrt(2) - 1.0, 6)
    a = Polygon(placed_hull(square, 45, ax, ay))
    b = Polygon(placed_hull(square, 45, bx, by))
    distance = a.distance(b)
    box_gap = b.bounds[0] - a.bounds[2]
    assert abs(distance - plate['spacing']) >= CLEARANCE_MM, distance
    assert distance > plate['spacing'] and abs(box_gap - 1.0) < 1e-5, (distance, box_gap)
    cases['rotated_near_miss'] = {
        'copies': [{'ref': 'r-square', 'orientation': '+z', 'angle': 45, 'x': ax, 'y': ay},
                   {'ref': 'r-square', 'orientation': '+z', 'angle': 45, 'x': bx, 'y': by}],
        'problems': [],
        'note': f'hull distance {distance:.6f} mm, bounding boxes {box_gap:.6f} mm apart'}

    for case in data['cases']:
        if case['name'] in cases:
            case.update(cases[case['name']])

    # One matrix at 30 degrees: M = Rz(30) R_o, t = (x, y, 0) - Rz(30) offset.
    a30 = math.radians(30)
    rz = np.array([[math.cos(a30), -math.sin(a30), 0], [math.sin(a30), math.cos(a30), 0], [0, 0, 1]])
    r_o = np.array([[0, 0, 1], [0, 1, 0], [-1, 0, 0]])           # '-x': the part's -x axis up
    offset = np.array([4.0, -2.0, -12.5])
    t = np.array([60.0, 70.0, 0.0]) - rz @ offset
    matrix = np.hstack([rz @ r_o, t[:, None]]).tolist()
    entry = {'orientation': '-x', 'angle': 30, 'offset': offset.tolist(), 'x': 60.0, 'y': 70.0,
             'matrix': matrix}
    data['matrices'] = [m for m in data['matrices'] if m.get('angle') != 30] + [entry]
    CASES.write_text(json.dumps(data, indent=1) + '\n')
    print(f'wrote {CASES.name}: {", ".join(cases)} and a 30 degree matrix')


if __name__ == '__main__':
    main()

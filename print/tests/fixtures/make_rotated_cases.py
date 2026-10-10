"""Write the rotated cases of plate_cases.json, whose coordinates are irrational.

    uv run python print/tests/fixtures/make_rotated_cases.py

The axis-aligned cases in plate_cases.json are written by hand and stay exact. The cases
with a copy turned by 45 degrees, the wedge cases that pin the rotation direction (an
asymmetric footprint at 30 and 330 degrees), and one matrix at 30 degrees, are computed here with
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


WEDGE = {'name': 'Wedge', 'footprints': {'+z': {'hull': [[-15.0, -10.0], [15.0, -10.0], [-15.0, 10.0]],
                                                'height': 12.0, 'offset': [0.0, 0.0, 0.0]}}}


def placed_hull(footprint, angle, x, y, clockwise=False):
    a = math.radians(-angle if clockwise else angle)
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

    # The rotation direction: a right triangle turned by 30 degrees counterclockwise sits
    # 1 mm inside the top margin; turned clockwise (the wrong direction) it would cross it.
    # Turned by 330 degrees at the same place, it crosses. A twin that turns the wrong way
    # fails both cases.
    parts['r-wedge'] = WEDGE
    wedge = WEDGE['footprints']['+z']
    top = plate['max'][1] - plate['margin']
    wx = 100.0
    wy = round(top - 1.0 - max(p[1] for p in placed_hull(wedge, 30, 0, 0)), 6)
    inside = top - max(p[1] for p in placed_hull(wedge, 30, wx, wy))
    wrong_way = max(p[1] for p in placed_hull(wedge, 30, wx, wy, clockwise=True)) - top
    assert inside >= CLEARANCE_MM and wrong_way >= CLEARANCE_MM, (inside, wrong_way)
    assert abs(off_plate_by(placed_hull(wedge, 330, wx, wy), plate) - wrong_way) < 1e-9
    cases['wedge_turned_30_fits'] = {
        'copies': [{'ref': 'r-wedge', 'orientation': '+z', 'angle': 30, 'x': wx, 'y': wy}],
        'problems': [],
        'note': f'{inside:.6f} mm inside the top margin; turned clockwise it would cross by {wrong_way:.6f} mm'}
    cases['wedge_turned_330_off_plate'] = {
        'copies': [{'ref': 'r-wedge', 'orientation': '+z', 'angle': 330, 'x': wx, 'y': wy}],
        'problems': [{'rule': 'off_plate', 'copies': [0], 'message': 'Wedge #1 is not fully on the plate.'}],
        'note': f'crosses the top margin by {wrong_way:.6f} mm'}

    names = [case['name'] for case in data['cases']]
    for name, values in cases.items():
        if name in names:
            data['cases'][names.index(name)].update(values)
        else:
            data['cases'].append(dict({'name': name}, **values))

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
    print(f'wrote {CASES.name}: {", ".join(cases)}, the wedge part and a 30 degree matrix')


if __name__ == '__main__':
    main()

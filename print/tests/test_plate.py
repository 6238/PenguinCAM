"""Plate geometry in print/plate.py, checked against the cases print/static/plate_geometry.js
also reads (print/tests/fixtures/plate_cases.json): the two cannot drift apart unnoticed."""
import json
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]        # the repository root
sys.path.insert(0, str(REPO))

from print import plate  # noqa: E402
from print.slicer import default_printer_profile  # noqa: E402

CASES = json.loads((REPO / 'print' / 'tests' / 'fixtures' / 'plate_cases.json').read_text())


class SharedCasesTest(unittest.TestCase):
    def test_shared_cases(self):
        names = [case['name'] for case in CASES['cases']]
        for required in ('all_clear', 'off_plate_left', 'off_plate_after_45_rotation', 'exactly_on_margin',
                         'too_tall', 'overlap', 'four_mm_apart', 'exactly_spacing_apart',
                         'rotated_near_miss', 'same_name_two_parts'):
            self.assertIn(required, names)
        for case in CASES['cases']:
            with self.subTest(case=case['name']):
                self.assertTrue(case['copies'], 'make_rotated_cases.py has filled the case')
                self.assertEqual(plate.check_placement(case['copies'], CASES['parts'], CASES['plate']),
                                 case['problems'])

    def test_shared_matrices(self):
        for entry in CASES['matrices']:
            with self.subTest(orientation=entry['orientation'], angle=entry['angle']):
                matrix = plate.copy_matrix(entry['orientation'], entry['angle'], entry['offset'],
                                           entry['x'], entry['y'])
                self.assertEqual(len(matrix), 3)
                for row, expected in zip(matrix, entry['matrix']):
                    self.assertEqual(len(row), 4)
                    for value, want in zip(row, expected):
                        self.assertAlmostEqual(value, want, delta=1e-9)

    def test_shared_labels_and_sanitize(self):
        for entry in CASES['labels']:
            self.assertEqual(plate.copy_labels(entry['names']), entry['labels'])
        for entry in CASES['sanitize']:
            self.assertEqual(plate.sanitize_part_name(entry['in']), entry['out'])


class PlateTest(unittest.TestCase):
    def test_matrices_put_named_axis_up(self):
        for name in plate.ORIENTATIONS:
            axis = [0, 0, 0]
            axis['xyz'.index(name[1])] = 1 if name[0] == '+' else -1
            for angle in (0, 37.5, 90):
                matrix = plate.copy_matrix(name, angle, [0, 0, 0], 0, 0)
                up = [sum(matrix[r][c] * axis[c] for c in range(3)) for r in range(3)]
                for value, want in zip(up, (0, 0, 1)):
                    self.assertAlmostEqual(value, want, delta=1e-12, msg=(name, angle))

    def test_matrix_places_the_footprint_centre_at_x_y(self):
        # The offset (footprint centre, lowest z) lands on (x, y, 0), whatever the angle.
        offset = [7.0, -3.0, -11.0]
        matrix = plate.copy_matrix('+z', 33, offset, 120, 80)
        moved = [sum(matrix[r][c] * offset[c] for c in range(3)) + matrix[r][3] for r in range(3)]
        for value, want in zip(moved, (120, 80, 0)):
            self.assertAlmostEqual(value, want, delta=1e-9)

    def test_plate_from_default_printer(self):
        result = plate.plate_from_printer(default_printer_profile())
        self.assertEqual(result['min'], [0, 0])
        self.assertEqual(result['max'], [340, 320])
        self.assertEqual(result['height'], 340)
        self.assertEqual((result['margin'], result['spacing']), (3.0, 5.0))
        self.assertEqual(result['printer'], 'Bambu Lab H2S 0.6 nozzle')
        self.assertEqual(result, CASES['plate'])

    def test_constants(self):
        self.assertEqual((plate.SPACING_MM, plate.MARGIN_MM, plate.EPS_MM), (5.0, 3.0, 1e-6))

    def test_copy_hull_rotates_then_moves(self):
        footprint = {'hull': [[-2, -1], [2, -1], [2, 1], [-2, 1]], 'height': 1, 'offset': [0, 0, 0]}
        hull = plate.copy_hull({'angle': 90, 'x': 10, 'y': 20}, footprint)
        self.assertEqual([[round(v, 9) + 0.0 for v in p] for p in hull], [[11, 18], [11, 22], [9, 22], [9, 18]])


class PageAnswerTest(unittest.TestCase):
    def test_print_page_answers_plate_and_limits(self):
        from frc_cam_gui_app import app, limiter
        from print.limits import limits_for_browser
        app.config['TESTING'] = True
        client = app.test_client()
        with client.session_transaction() as sess:
            sess['app_verified'] = True
        limiter.reset()
        limiter.enabled = False
        self.addCleanup(setattr, limiter, 'enabled', True)
        resp = client.post('/print/page', json={'source': 'onshape'}, headers={'X-Print-Page': 'abcdef012345'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {'plate': CASES['plate'], 'limits': limits_for_browser()})


if __name__ == '__main__':
    unittest.main()

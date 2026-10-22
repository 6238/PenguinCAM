"""Regressions for #26/#27: contour traversal and tabs after a partial-segment ramp."""
import math
from pathlib import Path
import re
import tempfile
import unittest

import ezdxf
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import orient, substring

from frc_cam_postprocessor import FRCPostProcessor


SQUARE = [(0, 0), (2, 0), (2, 2), (0, 2)]
PLATE = [(0, 0), (3, 0), (3, 3), (0, 3)]
# Exact straight-line geometry from the DXF attached to issue #27.
L_POCKET = [(0.5, 0.5), (2.5, 0.5), (2.5, 2),
            (1.5, 2), (1.5, 2.5), (0.5, 2.5)]


class ContourRampTest(unittest.TestCase):
    def _generate(self, material, tool_mm, pocket=False):
        """Use LINE entities and the normal DXF-to-program pipeline, as in the reports."""
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'part.dxf'
            doc = ezdxf.new()
            for points in ([PLATE, L_POCKET] if pocket else [SQUARE]):
                for start, end in zip(points, points[1:] + points[:1]):
                    doc.modelspace().add_line(start, end)
            doc.saveas(path)
            pp = FRCPostProcessor(0.25, tool_mm / 25.4, units='inch')
            pp.apply_material_preset(material)
            pp.load_dxf(str(path))
            pp.transform_coordinates('bottom-left', 0, enforce_bounds=False)
            pp.identify_perimeter_and_pockets()
            pp.classify_holes()
            result = pp.generate_gcode()
        self.assertTrue(result.success, result.errors)
        if pocket:
            block = result.gcode[result.gcode.index('(Pocket 1'):]
            block = block[:block.index('(===== PERIMETER')]
            self.assertIn('Interior pocket contour', block)
            points, direction = pp.pockets[0], -1
            self.assertNotIn('TAB REMOVAL', block)
        else:
            block = result.gcode[result.gcode.index('(===== PERIMETER'):]
            self.assertIn('TAB REMOVAL', block)
            block = block[:block.index('(===== TAB REMOVAL')]
            points, direction = pp.perimeter, 1
        return pp, block, self._contour(pp, points, direction)

    @staticmethod
    def _contour(pp, points, direction):
        coords = list(orient(Polygon(points).buffer(direction * pp.tool_radius), 1).exterior.coords)[:-1]
        if direction == 1:
            coords.reverse()
        return LineString(coords + coords[:1])

    @staticmethod
    def _passes(block):
        """Track modal XYZ, retaining every move after each ramp up to its retract."""
        passes, moves, position, ramp_end = [], [], None, None
        for line in block.splitlines():
            if '; Move to perimeter start' in line:
                moves, ramp_end = [], None
            if not re.match(r'G[01] ', line):
                continue
            words = {axis: float(value) for axis, value in
                     re.findall(r'([XYZ])(-?[\d.]+)', line.split(';')[0])}
            previous = position
            position = tuple(words.get(axis, position[i] if position else 0)
                             for i, axis in enumerate('XYZ'))
            if '; Ramp segment' in line:
                ramp_end = position
            elif '; Retract' in line and ramp_end is not None:
                passes.append((ramp_end, moves))
            elif ramp_end is not None and line.startswith('G1 '):
                moves.append((previous, position))
        return passes

    def _assert_full_forward_loop(self, block, contour):
        passes = self._passes(block)
        self.assertTrue(passes)
        for ramp_end, moves in passes:
            with self.subTest(ramp_end=ramp_end):
                travel = 0
                for start, end in moves:
                    length = math.dist(start[:2], end[:2])
                    if length < 1e-8:
                        continue
                    segment = LineString([start[:2], end[:2]])
                    self.assertTrue(contour.buffer(0.00015).covers(segment),
                                    f'cut leaves the compensated boundary: {start} -> {end}')
                    advance = contour.project(Point(end[:2])) - contour.project(Point(start[:2]))
                    if advance < -0.0003:
                        advance += contour.length
                    self.assertAlmostEqual(advance, length, delta=0.0003,
                                           msg=f'cut reverses along contour: {start} -> {end}')
                    travel += length
                self.assertAlmostEqual(travel, contour.length, delta=0.001)
                self.assertLess(math.dist(moves[-1][1][:2], ramp_end[:2]), 0.00015)

    def _assert_tabs_survive(self, pp, block, contour):
        passes = self._passes(block)
        depth_per_pass = (pp.material_top - pp.cut_depth) / len(passes)
        keepout = (depth_per_pass + pp.ramp_start_clearance) / math.tan(math.radians(pp.ramp_angle))
        tab_z, zones = pp._plan_tab_zones(contour.length, keepout, [])
        self.assertEqual(len(zones), 3)
        # Test the actual skipped stretches, including both ends of a tab crossing a
        # contour corner. Inset slightly to allow the four-decimal G-code rounding.
        tabs = [substring(contour, a + 0.0003, b - 0.0003) for a, b in zones]
        crossed = set()
        for _, moves in passes:
            for start, end in moves:
                if math.dist(start[:2], end[:2]) < 1e-8:
                    continue
                segment = LineString([start[:2], end[:2]])
                for i, tab in enumerate(tabs):
                    if segment.intersection(tab.buffer(0.00008)).length > 0.0003:
                        crossed.add(i)
                        self.assertGreaterEqual(min(start[2], end[2]), tab_z - 0.0001,
                                                f'tab {i + 1} cut below its top: {start} -> {end}')
        self.assertEqual(crossed, set(range(3)))

    def test_square_perimeter_continues_forward_after_ramp(self):
        _, block, contour = self._generate('plywood', 4)
        self._assert_full_forward_loop(block, contour)

    def test_square_preserves_all_tabs_until_removal(self):
        pp, block, contour = self._generate('plywood', 4)
        self._assert_tabs_survive(pp, block, contour)

    def test_l_pocket_closes_along_boundary_on_both_depth_passes(self):
        _, block, contour = self._generate('aluminum', 3, pocket=True)
        self.assertEqual(len(self._passes(block)), 2)
        self._assert_full_forward_loop(block, contour)

    def test_l_pocket_preserves_tabs_on_both_depth_passes(self):
        pp, block, contour = self._generate('aluminum', 3, pocket=True)
        self._assert_tabs_survive(pp, block, contour)

    def test_ramp_endpoints_with_tabs_disabled(self):
        for direction in (1, -1):
            for endpoint in ('partial', 'vertex', 'loop'):
                with self.subTest(direction=direction, endpoint=endpoint):
                    pp = FRCPostProcessor(0.25, 4 / 25.4, units='inch')
                    pp.apply_material_preset('plywood')
                    pp.tabs_enabled = False
                    contour = self._contour(pp, SQUARE, direction)
                    first_edge = math.dist(*list(contour.coords)[:2])
                    ramp_length = {'partial': first_edge / 2, 'vertex': first_edge,
                                   'loop': contour.length}[endpoint]
                    pp.ramp_start_clearance = 0
                    pp.ramp_angle = math.degrees(math.atan((pp.material_top - pp.cut_depth) / ramp_length))
                    block = '\n'.join(pp._generate_contour_gcode(
                        SQUARE, 'perimeter' if direction == 1 else 'pocket',
                        direction, direction == 1, False))
                    self._assert_full_forward_loop(block, contour)


if __name__ == '__main__':
    unittest.main()

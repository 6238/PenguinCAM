"""Holding-tab behaviour: height, survival across multi-pass cuts, and placement.

All three were reported together from the field: a team using small aluminum tabs
(0.08" high x 0.06" wide) with a shallow max_slotting_depth found the tabs came out a
fraction of the configured height, and that three tabs landed bunched on one side of an
L-shaped interior pocket instead of spread around it."""
import math
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ezdxf
from shapely.geometry import Point, Polygon
from shapely.ops import orient

from frc_cam_postprocessor import FRCPostProcessor
from config_validation import validate_and_sanitize_config
from team_config import TeamConfig


def _plate_with_l_pocket(path, scale=1.0):
    """A plate with one L-shaped through-pocket, the reported geometry."""
    doc = ezdxf.new()
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (12, 0), (12, 10), (0, 10)], close=True)
    s = scale
    msp.add_lwpolyline([(3, 3), (3 + 3 * s, 3), (3 + 3 * s, 3 + s),
                        (3 + s, 3 + s), (3 + s, 3 + 3 * s), (3, 3 + 3 * s)], close=True)
    doc.saveas(path)
    return path


def _config(tab_width=0.25, tab_height=0.08, max_slotting_depth=0.031,
            sacrifice=0.02, spacing=6.0):
    """Build the config the way a team actually does - through YAML validation - so the
    per-material tab overrides resolve exactly as they do in the product."""
    yaml_text = f"""
team:
  number: 9999
  name: "Tab Test"
machining:
  z_reference:
    sacrifice_board_depth: {sacrifice}
  tabs:
    enabled: true
    width: {tab_width}
    height: {tab_height}
    spacing: {spacing}
    remove_tabs: false
materials:
  aluminum:
    name: "Aluminum"
    max_slotting_depth: {max_slotting_depth}
    tab_width: {tab_width}
    tab_height: {tab_height}
"""
    data, _ = validate_and_sanitize_config(yaml_text, strict=False)
    return TeamConfig.from_dict(data)


class _TabCase(unittest.TestCase):
    def _run(self, tmpdir, thickness=0.125, scale=1.0, **cfg_kwargs):
        dxf = _plate_with_l_pocket(os.path.join(tmpdir, 'l.dxf'), scale)
        pp = FRCPostProcessor(material_thickness=thickness, tool_diameter=0.157,
                              units='inch', config=_config(**cfg_kwargs))
        pp.apply_material_preset('aluminum')
        pp.load_dxf(dxf)
        pp.transform_coordinates('bottom-left', 0, enforce_bounds=False)
        pp.identify_perimeter_and_pockets()
        pp.classify_holes()
        result = pp.generate_gcode()
        gcode = result.gcode if getattr(result, 'gcode', None) else ''
        return pp, result, gcode

    @staticmethod
    def _pocket_block(gcode):
        """Just the L-pocket's contour. The perimeter is cut with tabs too, and its lifts
        would otherwise be mixed into every measurement below."""
        body = gcode[gcode.index('(Pocket 1'):]
        marker = '(===== PERIMETER'
        return body[:body.index(marker)] if marker in body else body

    @staticmethod
    def _tab_lift_zs(gcode):
        return [float(m.group(1)) for m in
                re.finditer(r'G1 Z(-?[\d.]+)[^\n]*; Tab \d+ start', gcode)]


class TestTabHeight(_TabCase):
    def test_tab_z_is_the_material_left_not_referenced_to_cut_depth(self):
        """cut_depth overcuts BELOW the stock, so adding tab_height to it under-delivers
        the tab by exactly the overcut."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            pp, _, gcode = self._run(td, tab_height=0.08, sacrifice=0.02)
            zs = set(round(z, 4) for z in self._tab_lift_zs(self._pocket_block(gcode)))
            self.assertEqual(zs, {0.08}, "tab top should sit tab_height above the stock bottom")
            self.assertNotIn(round(pp.cut_depth + 0.08, 4), zs)

    def test_tab_survives_every_pass_of_a_multipass_cut(self):
        """The reported bug: with max_slotting_depth well under tab_height, only the final
        pass lifted, so the intermediate passes had already machined the tab away and the
        real tab was capped at roughly one pass's depth."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            pp, _, gcode = self._run(td, thickness=0.125, tab_height=0.08,
                                     max_slotting_depth=0.031, sacrifice=0.02)
            total = pp.material_top - pp.cut_depth
            num_passes = max(1, int(math.ceil(total / pp.max_slotting_depth)))
            self.assertGreater(num_passes, 1, "test needs a genuinely multi-pass cut")

            # Every pass whose floor dips below the tab must lift over it.
            depth_per_pass = total / num_passes
            expected = sum(1 for n in range(1, num_passes + 1)
                           if (pp.cut_depth if n == num_passes
                               else pp.material_top - n * depth_per_pass) < 0.08 - 1e-9)
            self.assertGreater(expected, 1, "test needs more than one pass below the tab")

            block = self._pocket_block(gcode)
            lifts = self._tab_lift_zs(block)
            num_tabs = len(set(re.findall(r'; Tab (\d+) start', block)))
            self.assertEqual(len(lifts), expected * num_tabs)
            self.assertTrue(all(abs(z - 0.08) < 1e-9 for z in lifts))

    def test_pass_above_the_tab_does_not_lift(self):
        """A pass whose floor is still above the tab's top face never touches it."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            pp, _, gcode = self._run(td, thickness=0.5, tab_height=0.05,
                                     max_slotting_depth=0.1, sacrifice=0.02)
            total = pp.material_top - pp.cut_depth
            num_passes = max(1, int(math.ceil(total / pp.max_slotting_depth)))
            block = self._pocket_block(gcode)
            num_tabs = len(set(re.findall(r'; Tab (\d+) start', block)))
            self.assertLess(len(self._tab_lift_zs(block)), num_passes * num_tabs)


class TestTabWidth(_TabCase):
    def test_skipped_stretch_accounts_for_the_cutter_radius(self):
        """tab_width is the finished tab. The tool clears a radius off each end of the
        skipped stretch, so a stretch of exactly tab_width would leave nothing behind for
        any tab narrower than the cutter."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            pp, _, gcode = self._run(td, tab_width=0.06)
            # Total travel while held at tab height: a zone that straddles a corner is
            # emitted as several moves, so summing is the only correct measurement.
            spans, prev, running = [], None, None
            for line in self._pocket_block(gcode).splitlines():
                stripped = line.strip()
                m = re.match(r'G1 X(-?[\d.]+) Y(-?[\d.]+)', stripped)
                if m:
                    pt = (float(m.group(1)), float(m.group(2)))
                    if running is not None and prev is not None:
                        running += math.dist(prev, pt)
                    prev = pt
                elif stripped.startswith('G1 Z'):
                    if '; Tab' in line and 'start' in line:
                        running = 0.0          # lifted: start accumulating
                    elif running is not None:
                        spans.append(running)  # dropped back to depth: zone complete
                        running = None
            self.assertTrue(spans, "expected tab crossings in the pocket contour")
            for span in spans:
                self.assertAlmostEqual(span, 0.06 + pp.tool_diameter, places=3)

    def test_plan_places_no_tab_inside_the_ramp_keepout(self):
        """Planned directly, in the contour-distance frame the planner works in: the cutter
        is still descending through the ramp, so a tab there would simply be cut away."""
        pp = FRCPostProcessor(material_thickness=0.125, tool_diameter=0.157,
                              units='inch', config=_config(tab_width=0.06, tab_height=0.08))
        pp.apply_material_preset('aluminum')
        contour_length, keepout = 11.338, 1.13
        tab_z, zones = pp._plan_tab_zones(contour_length, keepout, [])
        self.assertAlmostEqual(tab_z, 0.08, places=4)
        self.assertGreaterEqual(len(zones), 3)
        for start, end in zones:
            self.assertGreaterEqual(start, keepout - 1e-9,
                                    f"tab zone {start:.3f}-{end:.3f} starts inside the ramp")
            self.assertLessEqual(end, contour_length + 1e-9)
        # Evenly spaced, including the gap that wraps past the contour start.
        centers = [(a + b) / 2 for a, b in zones]
        gaps = [centers[i + 1] - centers[i] for i in range(len(centers) - 1)]
        gaps.append(contour_length - centers[-1] + centers[0])
        self.assertAlmostEqual(max(gaps), min(gaps), places=6)

    def test_plan_falls_back_when_the_ramp_swamps_a_short_contour(self):
        """A contour too short to fit an even ring clear of the ramp still keeps its tabs
        out of the ramp, and says so rather than silently ramping through one."""
        pp = FRCPostProcessor(material_thickness=0.125, tool_diameter=0.157,
                              units='inch', config=_config(tab_width=0.06, tab_height=0.08))
        pp.apply_material_preset('aluminum')
        notes = []
        _, zones = pp._plan_tab_zones(3.0, 2.5, notes)
        self.assertTrue(zones)
        for start, _end in zones:
            self.assertGreaterEqual(start, 2.5 - 1e-9)
        self.assertTrue(any('short relative to' in n for n in notes), notes)

    def test_tabs_too_wide_for_the_contour_are_rejected_not_merged(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            _, result, _ = self._run(td, tab_width=6.0, scale=1.0)
            self.assertFalse(result.success)
            self.assertTrue(any('run together' in e for e in result.errors), result.errors)


class TestTabPlacement(_TabCase):
    def test_tabs_are_spread_evenly_around_an_l_shaped_pocket(self):
        """The reported bug: tabs were laid out only over the post-ramp stretch, which left
        the wrap-around gap one full ramp-in longer than every other gap. On a short contour
        that reads as all the tabs bunched on one side."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            pp, _, gcode = self._run(td)
            contour = orient(Polygon(pp.pockets[0]).buffer(-pp.tool_radius), 1.0).exterior
            length = contour.length

            starts, prev = [], None
            for line in self._pocket_block(gcode).splitlines():
                m = re.match(r'G1 X(-?[\d.]+) Y(-?[\d.]+)', line.strip())
                if m:
                    prev = (float(m.group(1)), float(m.group(2)))
                if '; Tab' in line and 'start' in line and prev:
                    starts.append(round(contour.project(Point(prev)), 4))
            positions = sorted(set(starts))
            self.assertGreaterEqual(len(positions), 3)

            gaps = [positions[(i + 1) % len(positions)] - positions[i]
                    + (length if i == len(positions) - 1 else 0)
                    for i in range(len(positions))]
            # Every gap, INCLUDING the one that wraps past the start point, within 5%.
            self.assertLess(max(gaps) - min(gaps), 0.05 * length,
                            f"tabs unevenly spaced around the loop: {gaps}")

    def test_tab_positions_are_identical_on_every_pass(self):
        """Tabs must stack into one column of uncut material; a layout that shifted between
        passes would machine each pass's tab away with the next pass."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            _, _, gcode = self._run(td)
            body = self._pocket_block(gcode)
            per_pass, current, prev = [], None, None
            for line in body.splitlines():
                if re.search(r'===== PASS \d+/', line):
                    if current:
                        per_pass.append(current)
                    current = []
                m = re.match(r'G1 X(-?[\d.]+) Y(-?[\d.]+)', line.strip())
                if m:
                    prev = (round(float(m.group(1)), 4), round(float(m.group(2)), 4))
                if '; Tab' in line and 'start' in line and current is not None and prev:
                    current.append(prev)
            if current:
                per_pass.append(current)
            withtabs = [p for p in per_pass if p]
            self.assertGreater(len(withtabs), 1, "need at least two passes with tabs")
            for p in withtabs[1:]:
                self.assertEqual(p, withtabs[0])

class TestRampNoLongerDescendsThroughAir(_TabCase):
    def test_ramp_is_the_same_length_on_every_pass(self):
        """Each pass ramps from just above the previous pass's floor. Ramping from the
        material top made later passes descend through material that was already gone,
        which also inflated the tab keep-out zone."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            _, _, gcode = self._run(td)
            body = self._pocket_block(gcode)
            ramps = [float(m) for m in re.findall(r'\(Ramp-in: ([\d.]+)"', body)]
            self.assertGreater(len(ramps), 1)
            self.assertAlmostEqual(max(ramps), min(ramps), places=4)


if __name__ == '__main__':
    unittest.main()

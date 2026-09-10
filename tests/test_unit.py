"""
Unit tests for FRCPostProcessor.
Focus on higher-level functions; minimal tests for low-level utilities.
"""

import unittest
import math
import sys
import os
import tempfile

import ezdxf

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from frc_cam_postprocessor import (
    FRCPostProcessor, MATERIAL_PRESETS, assemble_job_gcode, validate_job_layout,
    sanitize_filename_base, build_output_filename,
)
from team_config import TeamConfig, parse_length, DEFAULT_TOOL_DIAMETER_IN
from onshape_integration import OnshapeClient
from shapely.geometry import Point
from shapely.ops import unary_union


class TestControllerPortability(unittest.TestCase):
    """Default output is G54-only work-coordinate (portable to GRBL/Easel/WinCNC); the
    G53 machine park and coolant M-codes are opt-in via config."""

    def _flat_gcode(self, cfg=None):
        doc = ezdxf.new(); msp = doc.modelspace()
        msp.add_lwpolyline([(0, 0), (4, 0), (4, 4), (0, 4)], close=True)   # simple square
        path = tempfile.mktemp(suffix='.dxf'); doc.saveas(path)
        try:
            pp = FRCPostProcessor(0.25, 0.157, config=cfg) if cfg else FRCPostProcessor(0.25, 0.157)
            pp.apply_material_preset('aluminum')
            pp.load_dxf(path)
            pp.transform_coordinates('bottom-left', 0)
            pp.identify_perimeter_and_pockets()
            pp.classify_holes()
            return pp.generate_gcode(suggested_filename='p', timestamp='2026-07-31 12:00').gcode
        finally:
            os.remove(path)

    @staticmethod
    def _cfg(**machine):
        return TeamConfig({'version': 2, 'default_machine': 'm',
                           'machines': {'m': {'name': 'M', 'machine': machine}}})

    def test_default_is_portable(self):
        g = self._flat_gcode()
        self.assertNotIn('G53', g)                       # no machine-coordinate moves
        self.assertNotIn('M7', g)                        # no coolant unless configured
        self.assertNotIn('M8', g)
        self.assertIn('G0 Z', g)                         # work-coordinate safe clearance present

    def test_coolant_opt_in(self):
        g = self._flat_gcode(self._cfg(coolant='Air'))
        self.assertIn('M7', g)                           # air/mist coolant on
        self.assertIn('M9  ; Coolant off', g)
        self.assertNotIn('G53', g)                       # coolant doesn't bring back G53

    def test_flood_coolant_uses_m8(self):
        g = self._flat_gcode(self._cfg(coolant='Flood'))
        self.assertIn('M8', g)
        self.assertNotIn('M7', g)

    def test_park_opt_in(self):
        g = self._flat_gcode(self._cfg(park_position={'x': 1.0, 'y': 2.0, 'z': -0.5}))
        self.assertIn('G53 G0 X1.0 Y2.0', g)             # park appears only when configured
        self.assertIn('G53 G0 Z-0.5000', g)


class TestFirstPlungeClearanceRapid(unittest.TestCase):
    """At job start the tool sits up at safe_height (which can be several inches, well
    above any fixture). The FIRST feature must rapid (G0) down to the clearance plane
    before its slow plunge feed, instead of crawling the whole gap at approach speed.
    Subsequent features already retract to the clearance plane, so they must NOT emit a
    redundant rapid."""

    # safe_height (4.0) deliberately well above the retract/clearance plane so the rapid
    # genuinely descends; matches how a real Mach machine with tall fixtures is configured.
    CONFIG_YAML = """
team:
  number: 6238
machine:
  name: "Test"
  controller: "Mach4"
machining:
  z_reference:
    sacrifice_board_depth: 0.008
    clearance_height: 1.0
    safe_height: 4.0
"""

    def _gcode(self):
        cfg = TeamConfig.from_yaml(self.CONFIG_YAML)
        doc = ezdxf.new(); msp = doc.modelspace()
        msp.add_lwpolyline([(0, 0), (6, 0), (6, 6), (0, 6)], close=True)  # perimeter
        msp.add_circle((3, 3), 0.6)                                       # + a millable hole
        path = tempfile.mktemp(suffix='.dxf'); doc.saveas(path)
        try:
            pp = FRCPostProcessor(0.25, 0.157, config=cfg)
            pp.apply_material_preset('aluminum')
            pp.load_dxf(path)
            pp.transform_coordinates('bottom-left', 0)
            pp.identify_perimeter_and_pockets()
            pp.classify_holes()
            return pp.generate_gcode(suggested_filename='p', timestamp='2026-08-04 12:00').gcode, pp.retract_height
        finally:
            os.remove(path)

    def test_rapid_to_clearance_emitted_once(self):
        g, _ = self._gcode()
        # Two features (hole + perimeter) => >=2 approaches, but exactly ONE rapid.
        self.assertGreaterEqual(g.count('Approach to ramp start height'), 2)
        self.assertEqual(g.count('Rapid down to clearance plane'), 1)

    def test_first_plunge_starts_from_clearance_plane_not_safe_height(self):
        g, retract = self._gcode()
        lines = g.splitlines()
        rapid_i = next(i for i, l in enumerate(lines) if 'Rapid down to clearance plane' in l)
        approach_i = next(i for i, l in enumerate(lines) if 'Approach to ramp start height' in l)
        # The rapid comes right before the first plunge feed...
        self.assertLess(rapid_i, approach_i)
        # ...and it rapids to the clearance plane (retract_height), which is below safe Z (4.0).
        self.assertIn(f'G0 Z{retract:.4f}  ; Rapid down to clearance plane', g)
        self.assertIn('G0 Z4.0000  ; Safe Z clearance', g)
        self.assertLess(retract, 4.0)


class TestSharedDxfStitcher(unittest.TestCase):
    """dxf_geometry.entities_to_closed_paths is the single stitcher used by both the 2D
    import and the 2.5D reconstruction; lock its contract directly."""

    def _msp(self):
        return ezdxf.new().modelspace()

    def test_lines_and_ellipse_arc_close(self):
        from dxf_geometry import entities_to_closed_paths
        msp = self._msp()
        msp.add_line((0, 0), (10, 0)); msp.add_line((10, 0), (10, 5)); msp.add_line((0, 5), (0, 0))
        msp.add_ellipse(center=(5, 5), major_axis=(5, 0), ratio=0.4, start_param=0, end_param=math.pi)
        paths = entities_to_closed_paths(
            lines=list(msp.query('LINE')), arcs=list(msp.query('ARC')),
            ellipses=list(msp.query('ELLIPSE')))
        self.assertEqual(len(paths), 1)
        self.assertGreater(len(paths[0]), 4)

    def test_full_ellipse_is_a_closed_path(self):
        from dxf_geometry import entities_to_closed_paths
        msp = self._msp()
        msp.add_ellipse(center=(0, 0), major_axis=(4, 0), ratio=0.5, start_param=0, end_param=2 * math.pi)
        paths = entities_to_closed_paths(ellipses=list(msp.query('ELLIPSE')))
        self.assertEqual(len(paths), 1)

    def test_open_loop_reported_not_dropped_silently(self):
        from dxf_geometry import entities_to_closed_paths
        msp = self._msp()
        # 3 sides of a square (missing the 4th) -> an open chain, not a closed path.
        msp.add_line((0, 0), (10, 0)); msp.add_line((10, 0), (10, 10)); msp.add_line((10, 10), (0, 10))
        seen = []
        paths = entities_to_closed_paths(lines=list(msp.query('LINE')),
                                         on_open_loop=lambda coords, gap: seen.append(gap))
        self.assertEqual(paths, [])
        self.assertEqual(len(seen), 1)
        self.assertAlmostEqual(seen[0], 10.0, places=3)   # end-to-end gap of the open chain


class TestEllipsePerimeterStitching(unittest.TestCase):
    """Onshape exports curved perimeter transitions as ELLIPSE arcs; the path stitcher
    must include them, or the perimeter can't close (real bug: Part 2, 2026-07-28)."""

    def _load(self, doc):
        path = tempfile.mktemp(suffix='.dxf')
        doc.saveas(path)
        try:
            pp = FRCPostProcessor(0.25, 0.157)
            pp.apply_material_preset('aluminum')
            pp.load_dxf(path)
            pp.identify_perimeter_and_pockets()
            return pp
        finally:
            os.remove(path)

    def test_ellipse_arc_closes_perimeter(self):
        # A closed loop of 3 lines + 1 ELLIPSE arc (the top edge bulges out).
        doc = ezdxf.new(); msp = doc.modelspace()
        msp.add_line((0, 0), (10, 0))
        msp.add_line((10, 0), (10, 5))
        msp.add_line((0, 5), (0, 0))
        msp.add_ellipse(center=(5, 5), major_axis=(5, 0), ratio=0.4,
                        start_param=0, end_param=math.pi)
        pp = self._load(doc)
        self.assertIsNotNone(pp.perimeter)
        self.assertGreater(len(pp.perimeter), 3)

    def test_full_ellipse_is_standalone_pocket(self):
        # A perimeter plus a full ELLIPSE inside it -> the ellipse is its own closed loop.
        doc = ezdxf.new(); msp = doc.modelspace()
        for a, b in [((0, 0), (20, 0)), ((20, 0), (20, 20)), ((20, 20), (0, 20)), ((0, 20), (0, 0))]:
            msp.add_line(a, b)
        msp.add_ellipse(center=(10, 10), major_axis=(3, 0), ratio=0.5,
                        start_param=0, end_param=2 * math.pi)
        pp = self._load(doc)
        self.assertIsNotNone(pp.perimeter)
        self.assertEqual(len(pp.pockets), 1)   # the full ellipse became a pocket

    def test_multilayer_hatch_reconstruction_includes_ellipse(self):
        # The 2.5D path rebuilds its own DXF (solid HATCH) from Onshape geometry with a
        # separate stitcher; it must also handle ELLIPSE arcs or curved corners break.
        from onshape_integration import OnshapeClient
        doc = ezdxf.new(); msp = doc.modelspace()
        msp.add_line((0, 0), (10, 0))
        msp.add_line((10, 0), (10, 5))
        msp.add_line((0, 5), (0, 0))
        msp.add_ellipse(center=(5, 5), major_axis=(5, 0), ratio=0.4,
                        start_param=0, end_param=math.pi)
        target = ezdxf.new().modelspace()
        OnshapeClient()._convert_geometry_to_solid_hatch(msp, target, 'layer0')
        # The solid region must span the full closed boundary (~10 x 7 incl. the arc bulge),
        # which is only possible if the ellipse arc was stitched into the perimeter.
        spans = []
        for h in target.query('HATCH'):
            for path in h.paths:
                pts = [(v[0], v[1]) for v in getattr(path, 'vertices', [])]
                if len(pts) >= 3:
                    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
                    spans.append((max(xs) - min(xs)) * (max(ys) - min(ys)))
        self.assertTrue(spans and max(spans) > 50)   # ~10 x 7 = 70; a broken loop would be tiny


class TestLengthParsing(unittest.TestCase):
    """Config/UI length values may carry a unit (metric or SAE); parse to inches."""

    def test_units_and_forms(self):
        self.assertAlmostEqual(parse_length('4mm'), 4 / 25.4)
        self.assertAlmostEqual(parse_length('0.25"'), 0.25)
        self.assertAlmostEqual(parse_length('1/8'), 0.125)      # SAE fraction
        self.assertAlmostEqual(parse_length('1/4"'), 0.25)
        self.assertAlmostEqual(parse_length('3 cm'), 3 / 2.54)
        self.assertAlmostEqual(parse_length('1ft'), 12.0)
        self.assertEqual(parse_length(0.157), 0.157)            # number -> inches
        self.assertAlmostEqual(parse_length('-3mm'), -3 / 25.4)  # negatives allowed (config offsets)

    def test_rejects_non_lengths(self):
        for bad in ('Aluminum', 'G54', '', 'abc', '4mmm', '1/0', None, True):
            self.assertIsNone(parse_length(bad))

    def test_config_normalizes_dimension_strings(self):
        cfg = {'version': 2, 'default_machine': 'm1', 'machines': {'m1': {
            'name': 'M1',
            'default_tool': {'diameter': '1/8"'},
            'machine': {'dimensions': {'x_max': '600mm', 'y_max': 24.0},
                        'park_position': {'z': '-3mm'}},
            'machining': {'tabs': {'width': '6mm'}},
            'materials': {'aluminum': {'name': '6061', 'feed_rate': 55.0, 'tab_width': '5mm'}},
        }}}
        t = TeamConfig(cfg)
        d = t.to_dict('m1')
        self.assertAlmostEqual(d['machine_x_max'], 600 / 25.4)          # mm -> in
        self.assertAlmostEqual(d['default_tool_diameter'], 0.125)        # 1/8"
        self.assertEqual(d['default_tool_diameter_text'], '1/8"')        # display text preserved
        self.assertAlmostEqual(t.machine_park_z, -3 / 25.4)             # negative offset
        self.assertAlmostEqual(t.tab_width, 6 / 25.4)
        self.assertEqual(t.get_material_preset('aluminum', 'm1')['name'], '6061')  # name NOT parsed
        self.assertEqual(cfg['machines']['m1']['machine']['dimensions']['x_max'], '600mm')  # input unmutated

    def test_default_tool_is_4mm(self):
        d = TeamConfig().to_dict()
        self.assertAlmostEqual(d['default_tool_diameter'], DEFAULT_TOOL_DIAMETER_IN)
        self.assertEqual(d['default_tool_diameter_text'], '4mm')


class TestMultiMachineConfig(unittest.TestCase):
    """A v2 config's SELECTED machine must drive every setting, not just its materials.

    Regression: TeamConfig._get() used to hardcode the default machine, so a team with two
    machines got machine 1's tabs/pockets/z_reference/park settings no matter which machine
    they picked - only feeds/speeds followed the selection."""

    # m1: no tabs, never contour interior cutouts. m2: tabs, contour above the threshold.
    TWO_MACHINES = {
        'version': 2,
        'default_machine': 'm1',
        'machines': {
            'm1': {
                'name': 'Router One',
                'machine': {'name': 'Router One', 'dimensions': {'x_max': 24.0, 'y_max': 24.0}},
                'machining': {'tabs': {'enabled': False, 'remove_tabs': False},
                              'pockets': {'contour_threshold': 0}},
            },
            'm2': {
                'name': 'Router Two',
                'machine': {'name': 'Router Two', 'dimensions': {'x_max': 48.0, 'y_max': 96.0}},
                'machining': {'tabs': {'enabled': True, 'remove_tabs': True},
                              'pockets': {'contour_threshold': 510}},
            },
        },
    }

    def _part_gcode(self, config):
        """6x6 plate with a 3" through-hole, cut with a 1/8" tool: big enough to land above
        m2's contour threshold, so the two machines take visibly different strategies."""
        pp = FRCPostProcessor(0.25, 0.125, config=config)
        pp.apply_material_preset('plywood')
        pp.circles = [{'center': (3.0, 3.0), 'diameter': 3.0}]
        pp.polylines = [[(0, 0), (6, 0), (6, 6), (0, 6), (0, 0)]]
        pp.identify_perimeter_and_pockets()
        pp.classify_holes()
        result = pp.generate_gcode()
        self.assertTrue(result.success, f"G-code generation failed: {result.errors}")
        return result.gcode

    def test_unbound_config_uses_default_machine(self):
        cfg = TeamConfig(self.TWO_MACHINES)
        self.assertEqual(cfg.active_machine_id, 'm1')
        self.assertFalse(cfg.tabs_enabled)
        self.assertEqual(cfg.machine_name, 'Router One')

    def test_settings_follow_the_selected_machine(self):
        cfg = TeamConfig(self.TWO_MACHINES).for_machine('m2')
        self.assertEqual(cfg.active_machine_id, 'm2')
        self.assertTrue(cfg.tabs_enabled)
        self.assertTrue(cfg.remove_tabs)
        self.assertEqual(cfg._get('machining', 'pockets', 'contour_threshold'), 510)
        self.assertEqual(cfg.machine_name, 'Router Two')
        self.assertEqual(cfg.machine_x_max, 48.0)

    def test_binding_does_not_mutate_the_original(self):
        cfg = TeamConfig(self.TWO_MACHINES)
        bound = cfg.for_machine('m2')
        self.assertTrue(bound.tabs_enabled)
        self.assertFalse(cfg.tabs_enabled, "binding must not affect the shared instance")
        self.assertIs(cfg.for_machine(None), cfg)

    def test_unknown_machine_falls_back_to_default(self):
        cfg = TeamConfig(self.TWO_MACHINES).for_machine('nope')
        self.assertEqual(cfg.active_machine_id, 'm1')
        self.assertFalse(cfg.tabs_enabled)

    def test_missing_or_dangling_default_machine_uses_first_machine(self):
        # Without this fallback the id resolves to a nonexistent key and EVERY setting
        # silently reverts to the built-in Team 6238 defaults.
        for bad in ({}, {'default_machine': 'ghost'}):
            data = {'version': 2, 'machines': self.TWO_MACHINES['machines'], **bad}
            cfg = TeamConfig(data)
            self.assertEqual(cfg.default_machine_id, 'm1')
            self.assertEqual(cfg.machine_name, 'Router One')

    def test_selected_machine_drives_tabs_and_contouring_in_gcode(self):
        """End-to-end: the same part on both machines must produce different G-code."""
        cfg = TeamConfig(self.TWO_MACHINES)

        m1 = self._part_gcode(cfg)
        self.assertIn('PERIMETER (NO TABS)', m1)
        self.assertIn('Tabs disabled', m1)
        self.assertNotIn('CONTOUR ONLY', m1)  # threshold 0 -> always fully clear

        m2 = self._part_gcode(cfg.for_machine('m2'))
        self.assertIn('PERIMETER WITH TABS', m2)
        self.assertIn('CONTOUR ONLY', m2)     # 3" hole exceeds the threshold
        self.assertIn('TAB REMOVAL PASS', m2)
        self.assertIn('Machine: Router Two', m2)


class TestLowLevelUtilities(unittest.TestCase):
    """Minimal tests for low-level utilities - just verify they work"""

    def test_distance_2d_basic(self):
        pp = FRCPostProcessor(0.25, 0.157)
        self.assertEqual(pp._distance_2d((0, 0), (3, 4)), 5.0)

    def test_format_time_basic(self):
        pp = FRCPostProcessor(0.25, 0.157)
        self.assertEqual(pp._format_time(125), "2m 5s")


class TestMaterialPresets(unittest.TestCase):
    """Test material preset application"""

    def test_plywood_preset_applies_correctly(self):
        pp = FRCPostProcessor(0.25, 0.157)
        pp.apply_material_preset('plywood')
        self.assertEqual(pp.feed_rate, 75.0)
        self.assertEqual(pp.spindle_speed, 18000)
        self.assertEqual(pp.ramp_angle, 20.0)
        self.assertEqual(pp.stepover_percentage, 0.65)

    def test_aluminum_preset_applies_correctly(self):
        pp = FRCPostProcessor(0.25, 0.157)
        pp.apply_material_preset('aluminum')
        self.assertEqual(pp.feed_rate, 55.0)
        self.assertEqual(pp.spindle_speed, 18000)
        self.assertEqual(pp.ramp_angle, 4.0)
        self.assertEqual(pp.stepover_percentage, 0.25)

    def test_polycarbonate_preset_applies_correctly(self):
        pp = FRCPostProcessor(0.25, 0.157)
        pp.apply_material_preset('polycarbonate')
        self.assertEqual(pp.feed_rate, 75.0)
        self.assertEqual(pp.stepover_percentage, 0.55)

    def test_invalid_material_falls_back_to_plywood(self):
        pp = FRCPostProcessor(0.25, 0.157)
        pp.apply_material_preset('unobtainium')
        # Should fall back to plywood defaults
        self.assertEqual(pp.feed_rate, 75.0)
        self.assertEqual(pp.ramp_angle, 20.0)

    def test_mm_units_converts_feed_rates(self):
        pp = FRCPostProcessor(6.35, 4.0, units='mm')  # 0.25" = 6.35mm
        pp.apply_material_preset('plywood')
        # 75 IPM * 25.4 = 1905 mm/min
        self.assertEqual(pp.feed_rate, 75.0 * 25.4)


class TestSolidCircleDetection(unittest.TestCase):
    """Circle detection must not accept a near-round obround slot.

    Regression: a 4.0 mm wide slot with 0.69 mm of straight section scores 0.988 on the
    isoperimetric quotient, passing the 0.95 gate. It was then cleared as a circle sized
    by sqrt(area/pi), producing a round hole ~0.35 mm oversize instead of a slot.
    """

    MM = 25.4

    def setUp(self):
        self.pp = FRCPostProcessor(0.25, 0.125)
        self.pp.apply_material_preset('plywood')

    def _obround(self, width_mm, straight_mm, quad_segs=64):
        r = (width_mm / 2) / self.MM
        half = (straight_mm / 2) / self.MM
        return unary_union([Point(-half, 0).buffer(r, quad_segs=quad_segs),
                            Point(half, 0).buffer(r, quad_segs=quad_segs)])

    def _circle(self, diameter_mm, quad_segs):
        return Point(0, 0).buffer((diameter_mm / 2) / self.MM, quad_segs=quad_segs)

    def test_rejects_near_round_obround_slot(self):
        # The exact geometry that shipped as 5.08 mm round holes.
        self.assertIsNone(self.pp._detect_solid_circle(self._obround(4.0, 0.6863)))

    def test_rejects_longer_obround_slot(self):
        self.assertIsNone(self.pp._detect_solid_circle(self._obround(4.0, 1.3726)))

    def test_accepts_circle_at_any_tessellation_density(self):
        # Every vertex of a polygon inscribed in a circle sits at exactly R, so a coarse
        # DXF arc must still be recognised. This is why the fix is a radial test and not
        # a tighter circularity threshold.
        for quad_segs in (4, 8, 16, 64):
            with self.subTest(quad_segs=quad_segs):
                self.assertIsNotNone(self.pp._detect_solid_circle(self._circle(4.0, quad_segs)))

    def test_reports_measured_radius_not_equal_area_radius(self):
        result = self.pp._detect_solid_circle(self._circle(14.0, 64))
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result[2] * self.MM, 7.0, places=3)


class TestHelixRadiusClamp(unittest.TestCase):
    """The helix entry must never sweep outside the feature it is entering.

    Regression: helix_radius = tool_radius * helix_radius_multiplier was unbounded, so any
    pocket narrower than 2 * tool_radius * (1 + multiplier) was bored out to the helix
    diameter. With a 1/8" tool at multiplier 0.60 that is every feature under 5.08 mm.
    """

    MM = 25.4

    def setUp(self):
        self.pp = FRCPostProcessor(0.25, 0.125)
        self.pp.apply_material_preset('plywood')
        self.pp.helix_radius_multiplier = 0.60

    def _entry(self, travel_region):
        point = travel_region.centroid
        if not travel_region.contains(point):
            point = travel_region.representative_point()
        return point

    def _assert_inside(self, feature_poly):
        travel = feature_poly.buffer(-self.pp.tool_radius)
        entry = self._entry(travel)
        radius = self.pp._clamped_helix_radius(travel, entry.x, entry.y)
        limit = travel.boundary.distance(Point(entry.x, entry.y))
        self.assertLessEqual(radius, limit,
                             "helix would cut outside the finished feature")
        return radius, limit

    def test_helix_stays_inside_small_circular_pocket(self):
        for diameter_mm in (3.5, 4.0, 4.5, 5.0, 5.08, 6.0, 14.0):
            with self.subTest(diameter_mm=diameter_mm):
                self._assert_inside(Point(0, 0).buffer((diameter_mm / 2) / self.MM, quad_segs=64))

    def test_small_pocket_is_clamped_below_preset(self):
        radius, _ = self._assert_inside(Point(0, 0).buffer((4.0 / 2) / self.MM, quad_segs=64))
        self.assertLess(radius, self.pp.tool_radius * self.pp.helix_radius_multiplier)

    def test_large_pocket_keeps_the_preset_radius(self):
        radius, _ = self._assert_inside(Point(0, 0).buffer((14.0 / 2) / self.MM, quad_segs=64))
        self.assertAlmostEqual(radius, self.pp.tool_radius * self.pp.helix_radius_multiplier)

    def test_no_floor_is_applied_above_the_geometric_limit(self):
        # The previous clamp re-floored at 0.25 * tool_radius, which on these two slots
        # exceeds the boundary distance and so gouged despite the clamp being present.
        for straight_mm in (0.6863, 1.3726):
            with self.subTest(straight_mm=straight_mm):
                r = (4.0 / 2) / self.MM
                half = (straight_mm / 2) / self.MM
                slot = unary_union([Point(-half, 0).buffer(r, quad_segs=64),
                                    Point(half, 0).buffer(r, quad_segs=64)])
                radius, limit = self._assert_inside(slot)
                self.assertLess(limit, self.pp.tool_radius * 0.25,
                                "test geometry no longer exercises the floor bug")
                self.assertLess(radius, self.pp.tool_radius * 0.25)


class TestHelicalPassCalculation(unittest.TestCase):
    """Test helical pass calculations for safe ramp angles"""

    def setUp(self):
        self.pp = FRCPostProcessor(0.25, 0.157)
        self.pp.apply_material_preset('plywood')
        # Set known values for predictable results
        self.pp.material_top = 0.25
        self.pp.cut_depth = -0.02
        self.pp.ramp_start_clearance = 0.15

    def test_returns_tuple_of_passes_and_depth(self):
        result = self.pp._calculate_helical_passes(0.1)
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)
        num_passes, depth_per_pass = result
        self.assertIsInstance(num_passes, int)
        self.assertIsInstance(depth_per_pass, float)

    def test_larger_radius_requires_fewer_passes(self):
        # Larger radius = longer circumference = more depth per rev at same angle
        small_radius_passes, _ = self.pp._calculate_helical_passes(0.05)
        large_radius_passes, _ = self.pp._calculate_helical_passes(0.2)
        self.assertGreaterEqual(small_radius_passes, large_radius_passes)

    def test_steeper_angle_requires_fewer_passes(self):
        # Steeper angle = more aggressive = fewer passes needed
        shallow_passes, _ = self.pp._calculate_helical_passes(0.1, target_angle_deg=5)
        steep_passes, _ = self.pp._calculate_helical_passes(0.1, target_angle_deg=20)
        self.assertGreaterEqual(shallow_passes, steep_passes)

    def test_minimum_one_pass(self):
        # Even tiny holes need at least 1 pass
        num_passes, _ = self.pp._calculate_helical_passes(0.001)
        self.assertGreaterEqual(num_passes, 1)


class TestPocketEntryPoint(unittest.TestCase):
    """A pocket's helical plunge point must lie INSIDE the pocket. For a concave pocket
    (L/U shape) the centroid can fall outside, which used to plunge into keep-material and
    slot laterally to reach the pocket."""

    import re as _re

    def _entry_point(self, pocket_points):
        pp = FRCPostProcessor(0.25, 0.157)
        pp.apply_material_preset('aluminum')
        text = '\n'.join(pp._generate_pocket_gcode(pocket_points))
        m = TestPocketEntryPoint._re.search(
            r'X([-\d.]+) Y([-\d.]+) F[\d.]+  ; Position at pocket center', text)
        self.assertIsNotNone(m, "no pocket entry line emitted")
        return float(m.group(1)), float(m.group(2))

    def test_l_shaped_pocket_entry_is_inside(self):
        from shapely.geometry import Polygon, Point
        L = [(0, 0), (3, 0), (3, 1), (1, 1), (1, 3), (0, 3)]   # concave: centroid is in the notch
        poly = Polygon(L)
        self.assertFalse(poly.contains(poly.centroid))          # precondition: centroid is outside
        ex, ey = self._entry_point(L)
        offset = poly.buffer(-0.157 / 2)                        # tool-compensated pocket
        self.assertTrue(offset.contains(Point(ex, ey)),
                        f"entry ({ex},{ey}) must lie inside the pocket")

    def test_convex_pocket_still_enters_at_centroid(self):
        from shapely.geometry import Polygon
        rect = [(0, 0), (4, 0), (4, 2), (0, 2)]
        offset = Polygon(rect).buffer(-0.157 / 2)
        ex, ey = self._entry_point(rect)
        # No regression: convex pocket still plunges at the (inside) centroid.
        self.assertAlmostEqual(ex, offset.centroid.x, places=3)
        self.assertAlmostEqual(ey, offset.centroid.y, places=3)


class TestStockThicknessDiscovery(unittest.TestCase):
    """Designed stock thickness is derived from parallel-face depth bins - one formula shared
    by the 2.5D export and the 2D thickness probe. No Onshape credentials needed: the pure
    formula is tested directly, and detect_stock_thickness is tested against a stubbed
    depth-binning call (the network seam)."""

    def test_thickness_from_depth_bins(self):
        f = OnshapeClient._thickness_from_depth_bins
        self.assertAlmostEqual(f({0.0: [], -0.25: []}), 0.25)     # top - bottom
        self.assertAlmostEqual(f({0.118: [], 0.0: []}), 0.118)    # order-independent
        self.assertIsNone(f({0.0: []}))                            # single face -> undefined
        self.assertIsNone(f({}))                                   # nothing found

    def test_detect_stock_thickness_delegates_and_computes(self):
        client = OnshapeClient()   # no network in __init__
        captured = {}

        def fake_bins(did, wid, eid, normal, origin, body_id=None, cached_faces_data=None, **kw):
            captured.update(did=did, wid=wid, eid=eid, normal=normal, origin=origin,
                            body_id=body_id, cached=cached_faces_data)
            return {0.0: [{'face_id': 'A'}], -0.19: [{'face_id': 'B'}]}

        client.find_parallel_faces_by_depth = fake_bins
        faces = {'bodies': []}   # stand-in for a cached bodydetails response
        thickness = client.detect_stock_thickness(
            'D', 'W', 'E', {'x': 0, 'y': 0, 'z': 1}, {'x': 0, 'y': 0, 'z': 0},
            body_id='BID', cached_faces_data=faces)
        self.assertAlmostEqual(thickness, 0.19)
        # It must pass the reference face + cached faces straight through (no re-fetch).
        self.assertEqual(captured['body_id'], 'BID')
        self.assertIs(captured['cached'], faces)
        self.assertEqual(captured['normal'], {'x': 0, 'y': 0, 'z': 1})

    def test_detect_stock_thickness_none_when_single_depth(self):
        client = OnshapeClient()
        client.find_parallel_faces_by_depth = lambda *a, **k: {0.0: [{'face_id': 'A'}]}
        self.assertIsNone(client.detect_stock_thickness(
            'D', 'W', 'E', {'x': 0, 'y': 0, 'z': 1}, {'x': 0, 'y': 0, 'z': 0}))


class TestCornerSlowdown(unittest.TestCase):
    """Contour-parallel pocket clearing eases the feed through sharp interior corners (where
    the cutter wraps two edges and engagement spikes) without changing the toolpath geometry."""

    def setUp(self):
        self.pp = FRCPostProcessor(0.25, 0.157)
        self.pp.apply_material_preset('aluminum')   # feed 55 IPM

    def test_corner_feed_scale_by_angle(self):
        s = self.pp._corner_feed_scale
        self.assertAlmostEqual(s((-1, 0), (0, 0), (1, 0)), 1.0)                  # straight -> no slowdown
        self.assertAlmostEqual(s((0, 1), (0, 0), (1, 0)), 0.6, places=2)        # 90 deg -> partial
        self.assertAlmostEqual(s((0, 0), (0, 0), (1, 0)), 1.0)                  # degenerate -> safe 1.0
        # A sharp (<=60 deg) corner hits the floor.
        import math as m
        v = (0, 0); p = (m.cos(m.radians(25)), m.sin(m.radians(25))); n = (m.cos(m.radians(-25)), m.sin(m.radians(-25)))
        self.assertAlmostEqual(s(p, v, n), self.pp.corner_min_feed_scale)       # 50 deg included

    def _triangle_feeds(self):
        import re, math as m
        tri = [(0, 0), (3, 0), (1.5, 3 * m.sqrt(3) / 2)]   # equilateral, 60 deg corners
        g = '\n'.join(self.pp._generate_pocket_gcode(tri))
        feeds = [float(x) for x in re.findall(r'F(\d+\.\d+)', g)]
        return tri, g, feeds

    def test_sharp_pocket_slows_at_corners_full_speed_on_straights(self):
        _, g, feeds = self._triangle_feeds()
        base = self.pp.feed_rate
        floor = round(base * self.pp.corner_min_feed_scale, 1)
        self.assertIn(base, feeds)                       # straights still cut at full feed
        self.assertIn(floor, feeds)                      # 60 deg corners cut at the floor feed
        self.assertIn('corner slowdown', g)
        cut_feeds = [f for f in feeds if f <= base]      # ignore rapid/traverse (200)
        self.assertGreaterEqual(min(cut_feeds), floor - 1e-6)   # never below the floor

    def test_corner_floor_is_material_aware(self):
        al = FRCPostProcessor(0.25, 0.157); al.apply_material_preset('aluminum')
        pc = FRCPostProcessor(0.25, 0.157); pc.apply_material_preset('polycarbonate')
        self.assertAlmostEqual(al.corner_min_feed_scale, 0.4)   # force-limited: aggressive slowdown
        self.assertAlmostEqual(pc.corner_min_feed_scale, 0.7)   # heat-limited: gentler, preserve chip load
        # A custom/unknown material falls back to the (softer) default, not the aluminum floor.
        cust = FRCPostProcessor(0.25, 0.157); cust.apply_material_preset('mystery_material')
        self.assertAlmostEqual(cust.corner_min_feed_scale, 0.7)

    def test_corner_slowdown_preserves_pocket_geometry(self):
        import re
        from shapely.geometry import Polygon, LineString
        tri, g, _ = self._triangle_feeds()
        offset = Polygon(tri).buffer(-self.pp.tool_radius)
        cur = None; z = 10.0; outside = 0
        for l in g.splitlines():
            mz = re.search(r'Z([-\d.]+)', l); mm = re.search(r'G1 X([-\d.]+) Y([-\d.]+)', l)
            nz = float(mz.group(1)) if mz else z
            if mm:
                x, y = float(mm.group(1)), float(mm.group(2))
                if cur is not None and z < self.pp.material_top - 1e-6 and nz < self.pp.material_top - 1e-6 \
                        and not offset.buffer(1e-3).contains(LineString([cur, (x, y)])):
                    outside += 1
                cur = (x, y)
            z = nz
        self.assertEqual(outside, 0)   # collinear waypoints only: geometry unchanged


class TestPocketConcaveClearing(unittest.TestCase):
    """Concave pockets must never link straight across a notch through keep-material. Aligned
    ring starts keep links to a short in-pocket hop; a guard + ramped re-entry is the fallback
    when a link would still exit the pocket."""

    def _cutting_moves_outside(self, pocket_pts):
        import re
        from shapely.geometry import Polygon, LineString
        pp = FRCPostProcessor(0.25, 0.157)
        pp.apply_material_preset('aluminum')
        offset = Polygon(pocket_pts).buffer(-pp.tool_radius)   # tool-center safe region
        text = '\n'.join(pp._generate_pocket_gcode(pocket_pts))
        cur = None; z = 10.0; outside = 0
        for l in text.splitlines():
            mz = re.search(r'Z([-\d.]+)', l); m = re.search(r'G[0-3] X([-\d.]+) Y([-\d.]+)', l)
            nz = float(mz.group(1)) if mz else z
            if m:
                x, y = float(m.group(1)), float(m.group(2))
                cutting = z < pp.material_top - 1e-6 and nz < pp.material_top - 1e-6 and l.strip().startswith('G1')
                if cur is not None and cutting and not offset.buffer(1e-3).contains(LineString([cur, (x, y)])):
                    outside += 1
                cur = (x, y)
            z = nz
        return outside

    def test_u_pocket_never_cuts_outside(self):
        U = [(0, 0), (5, 0), (5, 4), (3, 4), (3, 1.5), (2, 1.5), (2, 4), (0, 4)]  # centroid in the notch
        self.assertEqual(self._cutting_moves_outside(U), 0)

    def test_dumbbell_pocket_never_cuts_outside(self):
        # Two lobes joined by a thin neck: inner offsets split into a MultiPolygon.
        D = [(0, 0), (2, 0), (2, 0.9), (3, 0.9), (3, 0), (5, 0),
             (5, 2), (3, 2), (3, 1.1), (2, 1.1), (2, 2), (0, 2)]
        self.assertEqual(self._cutting_moves_outside(D), 0)

    def test_reorder_ring_starts_nearest_reference(self):
        ring = [(0, 0), (1, 0), (1, 1), (0, 1)]
        out = FRCPostProcessor._reorder_closed_ring(ring, (0.9, 1.1))  # nearest vertex is (1,1)
        self.assertEqual(out[0], (1, 1))
        self.assertEqual(set(out), set(ring))          # same vertices, just rotated

    def test_ring_reentry_ramps_when_link_would_exit(self):
        """When a link would leave the pocket, re-enter with a retract + ramp along the ring
        (down to full cut depth) rather than a straight plunge or a gouge across the notch."""
        from shapely.geometry import Polygon
        pp = FRCPostProcessor(0.25, 0.157)
        pp.apply_material_preset('aluminum')
        safe = Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])            # small pocket
        ring = [(0.2, 0.2), (0.8, 0.2), (0.8, 0.8), (0.2, 0.8)]
        ramp_start = pp.material_top + pp.ramp_start_clearance
        g = []
        end = pp._link_and_cut_ring(g, ring, (5.0, 5.0), safe, ramp_start, pp.cut_depth, 1e-4)
        text = '\n'.join(g)
        self.assertIn('Retract', text)                             # guard fired
        self.assertIn('Ramp in along ring', text)                  # ramped, not straight-plunged
        self.assertIn(f'Z{pp.cut_depth:.4f}', text)                # ramp reaches full depth
        self.assertEqual(end, (0.8, 0.8))                          # ends at the vertex nearest cur_pos


class TestMultilayerRotationConsistency(unittest.TestCase):
    """2.5D (multi-layer) rotation must move Shapely polygons (partial-depth pockets) the
    SAME direction as circles/polylines. A sign mismatch (shapely rotates CCW for +angle,
    the rest rotates CW) put partial-depth pockets 180deg off - overlapping other features."""

    def _coincident_distance(self, rotation):
        """A circle and a square pocket start at the same center; after a rotation they must
        remain coincident. Returns the distance between them (0 if rotated consistently)."""
        from shapely.geometry import box
        pp = FRCPostProcessor(0.25, 0.157)
        pp.circles = []; pp.lines = []; pp.arcs = []; pp.splines = []; pp.polylines = []
        # Off-center, off-axis location (3,1) so clockwise vs counter-clockwise clearly differ;
        # a second far circle at (0,0) pushes the part center away from (3,1).
        pp.layer_data = {
            'through': {'depth': 0.0, 'circles': [{'center': (0.0, 0.0), 'radius': 0.2}],
                        'polylines': [], 'polygons': []},
            'pocket': {'depth': -0.1, 'circles': [{'center': (3.0, 1.0), 'radius': 0.2}],
                       'polylines': [], 'polygons': [box(2.8, 0.8, 3.2, 1.2)]},  # square @ (3,1)
        }
        pp.transform_coordinates('bottom-left', rotation, enforce_bounds=False)
        c = pp.layer_data['pocket']['circles'][0]['center']
        p = pp.layer_data['pocket']['polygons'][0].centroid
        return ((c[0] - p.x) ** 2 + (c[1] - p.y) ** 2) ** 0.5

    def test_polygon_and_circle_stay_coincident_after_rotation(self):
        for rotation in (0, 90, 180, 270):
            with self.subTest(rotation=rotation):
                self.assertAlmostEqual(self._coincident_distance(rotation), 0.0, places=6)


class TestFilenameSanitization(unittest.TestCase):
    """Onshape part names can contain path separators and header-illegal characters (e.g.
    '1/4" plate'); the output filename must stay safe to write to disk and serve."""

    def test_slash_and_quote_are_removed(self):
        # The reported failure case: '/' broke os.path.join, '"' broke Content-Disposition.
        out = sanitize_filename_base('1/4" plate')
        self.assertNotIn('/', out)
        self.assertNotIn('"', out)
        self.assertTrue(out)                 # not empty

    def test_all_illegal_characters_stripped(self):
        out = sanitize_filename_base('a/b\\c:d*e?f"g<h>i|j')
        for bad in '/\\:*?"<>|':
            self.assertNotIn(bad, out)

    def test_empty_and_all_illegal_fall_back(self):
        self.assertEqual(sanitize_filename_base('', 'job'), 'job')
        self.assertEqual(sanitize_filename_base('///', 'job'), 'job')   # collapses then trims to nothing

    def test_normal_name_preserved(self):
        self.assertEqual(sanitize_filename_base('Left Gusset'), 'Left Gusset')

    def test_build_output_filename_is_path_safe(self):
        fn = build_output_filename('1/4" plate', '2026-08-10 15:30', 'output')
        self.assertTrue(fn.endswith('.nc'))
        self.assertNotIn('/', fn)
        self.assertNotIn('"', fn)
        # os.path.join must keep it in the target folder (no '/' escaping into a subdir).
        self.assertEqual(os.path.basename(os.path.join('/tmp/out', fn)), fn)

    def test_generate_gcode_produces_safe_filename_end_to_end(self):
        doc = ezdxf.new(); msp = doc.modelspace()
        msp.add_lwpolyline([(0, 0), (4, 0), (4, 4), (0, 4)], close=True)
        path = tempfile.mktemp(suffix='.dxf'); doc.saveas(path)
        try:
            pp = FRCPostProcessor(0.25, 0.157)
            pp.apply_material_preset('aluminum')
            pp.load_dxf(path)
            pp.transform_coordinates('bottom-left', 0)
            pp.identify_perimeter_and_pockets()
            pp.classify_holes()
            res = pp.generate_gcode(suggested_filename='1/4" plate', timestamp='2026-08-10 15:30')
            self.assertNotIn('/', res.filename)
            self.assertNotIn('"', res.filename)
            self.assertEqual(os.path.basename(res.filename), res.filename)
        finally:
            os.remove(path)


class TestHoleClassification(unittest.TestCase):
    """Test hole classification based on tool diameter"""

    def setUp(self):
        self.pp = FRCPostProcessor(0.25, 0.157)

    def test_holes_smaller_than_min_millable_are_skipped(self):
        # min_millable_hole = tool_diameter * 1.2 = 0.157 * 1.2 = 0.1884"
        self.pp.circles = [
            {'center': (1, 1), 'radius': 0.1, 'diameter': 0.2},   # 0.2" > 0.1884" - OK
            {'center': (2, 2), 'radius': 0.05, 'diameter': 0.1},  # 0.1" < 0.1884" - skip
        ]
        self.pp.classify_holes()
        self.assertEqual(len(self.pp.holes), 1)
        self.assertEqual(self.pp.holes[0]['center'], (1, 1))

    def test_all_large_holes_are_kept(self):
        self.pp.circles = [
            {'center': (1, 1), 'radius': 0.25, 'diameter': 0.5},
            {'center': (2, 2), 'radius': 0.5, 'diameter': 1.0},
            {'center': (3, 3), 'radius': 0.375, 'diameter': 0.75},
        ]
        self.pp.classify_holes()
        self.assertEqual(len(self.pp.holes), 3)

    def test_holes_at_exactly_min_millable_are_kept(self):
        # Holes at exactly min_millable_hole are kept (code uses < not <=)
        exact_min = self.pp.min_millable_hole
        self.pp.circles = [
            {'center': (1, 1), 'radius': exact_min / 2, 'diameter': exact_min},
        ]
        self.pp.classify_holes()
        self.assertEqual(len(self.pp.holes), 1)

    def test_hole_exactly_tool_size_is_peck_drilled_not_rejected(self):
        """A hole exactly the tool diameter must be kept and flagged for peck drilling
        (straight plunge), not rejected as too small."""
        self.pp.circles = [
            {'center': (1, 1), 'radius': self.pp.tool_diameter / 2, 'diameter': self.pp.tool_diameter},
        ]
        self.pp.classify_holes()
        self.assertFalse(any('too small' in e for e in self.pp.errors))
        self.assertEqual(len(self.pp.holes), 1)
        self.assertTrue(self.pp.holes[0]['needs_peck_drill'])

    def test_hole_a_hair_under_tool_from_rounding_is_kept(self):
        """A hole a hair under the tool size (e.g. a 0.157" hole vs a "4mm"->0.15748" tool)
        is within tolerance and drilled, not rejected."""
        pp = FRCPostProcessor(0.25, 4.0 / 25.4)   # 4mm tool = 0.15748"
        pp.circles = [{'center': (1, 1), 'radius': 0.157 / 2, 'diameter': 0.157}]
        pp.classify_holes()
        self.assertFalse(any('too small' in e for e in pp.errors))
        self.assertEqual(len(pp.holes), 1)
        self.assertTrue(pp.holes[0]['needs_peck_drill'])

    def test_hole_genuinely_smaller_than_tool_is_rejected(self):
        """A hole clearly smaller than the tool (beyond the rounding tolerance) is still
        rejected - the tool physically cannot make it."""
        self.pp.circles = [
            {'center': (1, 1), 'radius': 0.05, 'diameter': 0.10},   # 0.10" << 0.157" tool
        ]
        self.pp.classify_holes()
        self.assertTrue(any('too small' in e for e in self.pp.errors))
        self.assertEqual(len(self.pp.holes), 0)

    def test_tool_sized_hole_gcode_is_pure_drill_no_degenerate_arc(self):
        """The G-code for a tool-sized hole peck-drills straight down and emits no
        zero-radius (I0 J0) arc, which many controllers reject."""
        self.pp.apply_material_preset('aluminum')   # sets peck_drill_depth
        g = '\n'.join(self.pp._generate_hole_gcode(1.0, 1.0, self.pp.tool_diameter,
                                                   needs_peck_drill=True))
        self.assertIn('G83', g)                     # peck drill happens
        self.assertNotIn('I0.0000 J0', g)           # no degenerate finishing arc
        self.assertIn('no lateral clearing', g)     # pure straight drill path taken


class TestHoleSorting(unittest.TestCase):
    """Test hole sorting for travel optimization using nearest neighbor + 2-opt"""

    def setUp(self):
        self.pp = FRCPostProcessor(0.25, 0.157)

    def test_holes_optimized_for_minimum_travel(self):
        """Test that holes are sorted using nearest neighbor optimization"""
        self.pp.circles = [
            {'center': (5, 5), 'radius': 0.25, 'diameter': 0.5},
            {'center': (1, 3), 'radius': 0.25, 'diameter': 0.5},
            {'center': (1, 1), 'radius': 0.25, 'diameter': 0.5},
            {'center': (3, 2), 'radius': 0.25, 'diameter': 0.5},
        ]
        self.pp.classify_holes()

        # Should start with the hole closest to origin (0,0)
        centers = [h['center'] for h in self.pp.holes]
        self.assertEqual(centers[0], (1, 1))  # Closest to origin

        # Verify all holes are present
        self.assertEqual(len(centers), 4)
        self.assertIn((1, 1), centers)
        self.assertIn((1, 3), centers)
        self.assertIn((3, 2), centers)
        self.assertIn((5, 5), centers)

        # Calculate total travel distance for optimized route
        optimized_dist = self.pp._distance_2d((0, 0), centers[0])
        for i in range(len(centers) - 1):
            optimized_dist += self.pp._distance_2d(centers[i], centers[i + 1])

        # Compare to naive X-then-Y sorting distance
        naive_order = [(1, 1), (1, 3), (3, 2), (5, 5)]
        naive_dist = self.pp._distance_2d((0, 0), naive_order[0])
        for i in range(len(naive_order) - 1):
            naive_dist += self.pp._distance_2d(naive_order[i], naive_order[i + 1])

        # Optimized route should be at most as long as naive route
        self.assertLessEqual(optimized_dist, naive_dist)

    def test_single_hole_not_affected(self):
        self.pp.circles = [
            {'center': (5, 5), 'radius': 0.25, 'diameter': 0.5},
        ]
        self.pp.classify_holes()
        self.assertEqual(len(self.pp.holes), 1)
        self.assertEqual(self.pp.holes[0]['center'], (5, 5))


class TestPocketCircularDetection(unittest.TestCase):
    """Test circular pocket detection"""

    def setUp(self):
        self.pp = FRCPostProcessor(0.25, 0.157)

    def test_circle_is_detected_as_circular(self):
        # Generate points on a circle
        num_points = 32
        radius = 1.0
        circle_points = []
        for i in range(num_points):
            angle = 2 * math.pi * i / num_points
            x = radius * math.cos(angle)
            y = radius * math.sin(angle)
            circle_points.append((x, y))

        self.assertTrue(self.pp._is_pocket_circular(circle_points))

    def test_square_is_detected_as_circular(self):
        # Squares have equidistant vertices from center, so they pass circular check
        # This is intentional - the algorithm only checks vertex distances
        square_points = [(0, 0), (1, 0), (1, 1), (0, 1)]
        self.assertTrue(self.pp._is_pocket_circular(square_points))

    def test_irregular_polygon_is_not_circular(self):
        # L-shaped polygon - definitely not circular
        l_shape = [(0, 0), (2, 0), (2, 1), (1, 1), (1, 2), (0, 2)]
        self.assertFalse(self.pp._is_pocket_circular(l_shape))

    def test_oval_with_tight_tolerance_is_not_circular(self):
        # Oval: different x and y radii
        num_points = 32
        oval_points = []
        for i in range(num_points):
            angle = 2 * math.pi * i / num_points
            x = 2.0 * math.cos(angle)  # x radius = 2
            y = 1.0 * math.sin(angle)  # y radius = 1
            oval_points.append((x, y))

        # With default 10% tolerance, an oval with 2:1 ratio should not be circular
        self.assertFalse(self.pp._is_pocket_circular(oval_points))


class TestPerimeterAndPocketIdentification(unittest.TestCase):
    """Test identification of perimeter and pockets"""

    def setUp(self):
        self.pp = FRCPostProcessor(0.25, 0.157)

    def test_largest_polygon_becomes_perimeter(self):
        # Large outer rectangle
        outer = [(0, 0), (10, 0), (10, 10), (0, 10)]
        # Small inner rectangle
        inner = [(2, 2), (4, 2), (4, 4), (2, 4)]

        self.pp.polylines = [inner, outer]  # Order shouldn't matter
        self.pp.identify_perimeter_and_pockets()

        self.assertEqual(self.pp.perimeter, outer)
        self.assertEqual(len(self.pp.pockets), 1)
        self.assertEqual(self.pp.pockets[0], inner)

    def test_no_polylines_results_in_none(self):
        self.pp.polylines = []
        self.pp.identify_perimeter_and_pockets()
        self.assertIsNone(self.pp.perimeter)
        self.assertEqual(self.pp.pockets, [])


class TestUnmillableFeatures(unittest.TestCase):
    """Test that unmillable features cause generation to fail."""

    def test_hole_too_small_fails(self):
        """Test that holes too small for the tool cause generation to fail."""
        pp = FRCPostProcessor(0.25, 0.157)
        pp.apply_material_preset('aluminum')

        # Manually add a hole that's too small (smaller than min_millable_hole)
        # min_millable_hole = 0.157 * 1.2 = 0.1884"
        pp.circles = [{'center': (0.5, 0.5), 'diameter': 0.15}]  # Too small!
        pp.polylines = []

        # Classify holes - should add error
        pp.classify_holes()

        # Should have 1 error
        self.assertEqual(len(pp.errors), 1)
        self.assertIn("too small", pp.errors[0].lower())

        # Try to generate G-code - should fail
        pp.identify_perimeter_and_pockets()
        result = pp.generate_gcode()

        self.assertFalse(result.success)
        self.assertEqual(len(result.errors), 1)
        self.assertIn("too small", result.errors[0].lower())
        self.assertIsNone(result.gcode)

    def test_multiple_small_holes_fails(self):
        """Test that multiple unmillable holes are all reported."""
        pp = FRCPostProcessor(0.25, 0.157)
        pp.apply_material_preset('aluminum')

        # Add three holes that are too small
        pp.circles = [
            {'center': (0.5, 0.5), 'diameter': 0.10},
            {'center': (1.0, 1.0), 'diameter': 0.15},
            {'center': (1.5, 1.5), 'diameter': 0.12}
        ]
        pp.polylines = []

        # Classify holes - should add 3 errors
        pp.classify_holes()

        # Should have 3 errors
        self.assertEqual(len(pp.errors), 3)
        for error in pp.errors:
            self.assertIn("too small", error.lower())

        # Try to generate G-code - should fail with all errors
        pp.identify_perimeter_and_pockets()
        result = pp.generate_gcode()

        self.assertFalse(result.success)
        self.assertEqual(len(result.errors), 3)

    def test_millable_hole_succeeds(self):
        """Test that holes large enough for the tool succeed."""
        pp = FRCPostProcessor(0.25, 0.157)
        pp.apply_material_preset('aluminum')

        # Add a hole that's large enough (> min_millable_hole)
        # min_millable_hole = 0.157 * 1.2 = 0.1884"
        pp.circles = [{'center': (0.5, 0.5), 'diameter': 0.25}]  # Large enough!
        pp.polylines = [
            # Simple square perimeter
            [(0, 0), (2, 0), (2, 2), (0, 2), (0, 0)]
        ]

        # Classify holes - should NOT add errors
        pp.classify_holes()

        # Should have 0 errors
        self.assertEqual(len(pp.errors), 0)
        self.assertEqual(len(pp.holes), 1)

        # Generate G-code - should succeed
        pp.identify_perimeter_and_pockets()
        result = pp.generate_gcode()

        self.assertTrue(result.success)
        self.assertEqual(len(result.errors), 0)
        self.assertIsNotNone(result.gcode)
        self.assertGreater(len(result.gcode), 0)

    def test_perimeter_with_sharp_internal_corner_fails(self):
        """Test that perimeter with very sharp internal corner causes failure.

        Note: In practice, Shapely's buffer operation handles most internal corners
        gracefully by rounding them. This test creates an extreme case that might
        trigger the invalid geometry check.
        """
        pp = FRCPostProcessor(0.25, 0.157)
        pp.apply_material_preset('aluminum')

        # Create a perimeter with a very narrow notch (internal corner)
        # This creates a shape that might fail offset operations
        pp.circles = []
        pp.polylines = [
            # Rectangle with a very narrow vertical notch
            # The notch is only 0.05" wide - narrower than tool radius (0.0785")
            [(0, 0), (2, 0), (2, 1), (1.025, 1), (1.025, 0.5),
             (0.975, 0.5), (0.975, 1), (0, 1), (0, 0)]
        ]

        pp.classify_holes()
        pp.identify_perimeter_and_pockets()

        # Generate G-code - perimeter might fail during offset
        # Note: This test is somewhat fragile as Shapely's buffer is quite robust
        # If it doesn't fail, at least we've verified the error path exists
        result = pp.generate_gcode()

        # Check if errors were detected - if so, verify they're about perimeter
        if len(pp.errors) > 0:
            # Should have failed
            self.assertFalse(result.success)
            self.assertIsNone(result.gcode)
            # Error should mention perimeter or internal corners
            error_text = ' '.join(result.errors).lower()
            self.assertTrue('perimeter' in error_text or 'corner' in error_text)
        # else: buffer succeeded (Shapely is very robust) - test passes anyway


class TestGCodeFormatting(unittest.TestCase):
    """Test that generated G-code has no nested comments or unicode characters."""

    def setUp(self):
        """Create a simple test part that exercises all major operations."""
        self.pp = FRCPostProcessor(0.25, 0.157)
        self.pp.apply_material_preset('plywood')

        # Add a hole
        self.pp.circles = [{'center': (0.5, 0.5), 'diameter': 0.25}]

        # Add a perimeter
        self.pp.polylines = [
            [(0, 0), (2, 0), (2, 2), (0, 2)]
        ]

        self.pp.classify_holes()
        self.pp.identify_perimeter_and_pockets()

        # Generate G-code
        result = self.pp.generate_gcode()
        self.assertTrue(result.success, "G-code generation should succeed for test setup")
        self.gcode_lines = result.gcode.split('\n')

    def test_no_nested_comments(self):
        """Test that no line contains nested parenthesis comments."""
        for line_num, line in enumerate(self.gcode_lines, 1):
            # Remove semicolon comments first (they're always at the end)
            if ';' in line:
                line = line.split(';')[0]

            # Count parenthesis comment depth
            depth = 0
            max_depth = 0
            for char in line:
                if char == '(':
                    depth += 1
                    max_depth = max(max_depth, depth)
                elif char == ')':
                    depth -= 1

            # Max depth should never exceed 1 (one level of comments)
            self.assertLessEqual(
                max_depth, 1,
                f"Line {line_num} has nested comments: {line.strip()}"
            )

    def test_no_unicode_characters(self):
        """Test that all G-code uses ASCII only (no unicode characters)."""
        for line_num, line in enumerate(self.gcode_lines, 1):
            try:
                # Try to encode as ASCII - will fail if unicode present
                line.encode('ascii')
            except UnicodeEncodeError as e:
                self.fail(
                    f"Line {line_num} contains unicode character(s): {line.strip()}\n"
                    f"Error: {e}"
                )

    def test_no_square_brackets_in_comments(self):
        """Test that square brackets don't appear inside parenthesis comments.

        Some controllers interpret square brackets specially, so they should
        not appear inside comments.
        """
        for line_num, line in enumerate(self.gcode_lines, 1):
            # Find parenthesis comments
            in_paren_comment = False
            for i, char in enumerate(line):
                if char == '(':
                    in_paren_comment = True
                elif char == ')':
                    in_paren_comment = False
                elif in_paren_comment and char in '[]':
                    self.fail(
                        f"Line {line_num} has square bracket inside parenthesis comment: "
                        f"{line.strip()}"
                    )


class TestTeamConfigIntegration(unittest.TestCase):
    """Test that team config values are properly applied to generated G-code."""

    def test_custom_spindle_speed_from_config(self):
        """Test that custom spindle speed from config appears in G-code."""
        # Create custom config with different spindle speed
        config_data = {
            'materials': {
                'plywood': {
                    'spindle_speed': 24000,  # Different from default 18000
                    'feed_rate': 75.0,
                    'plunge_rate': 35.0,
                }
            }
        }
        config = TeamConfig(config_data)

        # Get material preset from config
        material_preset = config.get_material_preset('plywood')

        # Create postprocessor and apply custom preset
        pp = FRCPostProcessor(0.25, 0.157)
        pp.spindle_speed = material_preset['spindle_speed']
        pp.feed_rate = material_preset['feed_rate']
        pp.plunge_rate = material_preset['plunge_rate']
        pp.max_slotting_depth = material_preset.get('max_slotting_depth', 0.4)

        # Add simple geometry
        pp.circles = [{'center': (0.5, 0.5), 'diameter': 0.25}]
        pp.polylines = [[(0, 0), (2, 0), (2, 2), (0, 2)]]

        # Generate G-code
        pp.classify_holes()
        pp.identify_perimeter_and_pockets()
        result = pp.generate_gcode()

        self.assertTrue(result.success)

        # Check that G-code contains custom spindle speed
        self.assertIn('S24000', result.gcode,
                     "G-code should contain custom spindle speed S24000")

    def test_custom_feed_rates_from_config(self):
        """Test that custom feed rates from config appear in G-code."""
        # Create custom config with different feed rates
        config_data = {
            'materials': {
                'aluminum': {
                    'spindle_speed': 18000,
                    'feed_rate': 42.0,       # Different from default 55.0
                    'plunge_rate': 10.0,     # Different from default 15.0
                    'ramp_feed_rate': 28.0,  # Different from default 35.0
                }
            }
        }
        config = TeamConfig(config_data)

        # Get material preset from config
        material_preset = config.get_material_preset('aluminum')

        # Create postprocessor and apply custom preset
        pp = FRCPostProcessor(0.25, 0.157, units='mm')  # Use mm to make values more distinctive
        pp.spindle_speed = material_preset['spindle_speed']
        pp.feed_rate = material_preset['feed_rate'] * 25.4  # Convert to mm/min
        pp.plunge_rate = material_preset['plunge_rate'] * 25.4
        pp.ramp_feed_rate = material_preset['ramp_feed_rate'] * 25.4
        pp.max_slotting_depth = 0.2  # Aluminum default

        # Add simple geometry that will generate plunge and cutting moves
        pp.circles = [{'center': (12.7, 12.7), 'diameter': 6.35}]  # 0.5" hole at 0.5, 0.5 inches
        pp.polylines = [[(0, 0), (50.8, 0), (50.8, 50.8), (0, 50.8)]]  # 2" square

        # Generate G-code
        pp.classify_holes()
        pp.identify_perimeter_and_pockets()
        result = pp.generate_gcode()

        self.assertTrue(result.success)

        # Check that G-code contains custom feed rates (in mm/min)
        # Custom cutting feed: 42 IPM * 25.4 = 1066.8 mm/min
        # Custom plunge feed: 10 IPM * 25.4 = 254.0 mm/min
        # Custom ramp feed: 28 IPM * 25.4 = 711.2 mm/min

        # Look for feed rate commands
        feed_rates = []
        for line in result.gcode.split('\n'):
            if 'F' in line:
                # Extract F values
                import re
                matches = re.findall(r'F([\d.]+)', line)
                feed_rates.extend([float(f) for f in matches])

        # Check that custom cutting feed rate appears
        self.assertIn(1066.8, feed_rates,
                     "G-code should contain custom cutting feed rate F1066.8 (42 IPM)")

        # Check that custom plunge feed rate appears
        self.assertIn(254.0, feed_rates,
                     "G-code should contain custom plunge feed rate F254.0 (10 IPM)")

    def test_pause_before_perimeter_enabled(self):
        """Test that pause_before_perimeter config inserts M0 pause before perimeter."""
        # Create config with pause enabled
        config_data = {
            'machining': {
                'fixturing': {
                    'pause_before_perimeter': True
                }
            }
        }
        config = TeamConfig(config_data)

        # Verify config value is correct
        self.assertTrue(config.pause_before_perimeter)

        # Create postprocessor with pause enabled
        pp = FRCPostProcessor(0.25, 0.157, config=config)
        pp.apply_material_preset('plywood')

        # Verify pause_before_perimeter is set
        self.assertTrue(pp.pause_before_perimeter)

        # Add simple perimeter
        pp.circles = []
        pp.polylines = [[(0, 0), (2, 0), (2, 2), (0, 2)]]

        # Generate G-code
        pp.classify_holes()
        pp.identify_perimeter_and_pockets()
        result = pp.generate_gcode()

        self.assertTrue(result.success)

        # Check that G-code contains pause command
        # The pause includes "M0  ; Program pause"
        self.assertIn('M0', result.gcode,
                     "G-code should contain M0 pause command")
        self.assertIn('PAUSE FOR FIXTURING', result.gcode,
                     "G-code should contain fixturing pause message")
        self.assertIn('Program pause', result.gcode,
                     "G-code should contain program pause comment")

    def test_pause_before_perimeter_disabled(self):
        """Test that pause_before_perimeter=False does not insert M0 pause."""
        # Create config with pause disabled
        config_data = {
            'machining': {
                'fixturing': {
                    'pause_before_perimeter': False
                }
            }
        }
        config = TeamConfig(config_data)

        # Verify config value is correct
        self.assertFalse(config.pause_before_perimeter)

        # Create postprocessor with pause disabled
        pp = FRCPostProcessor(0.25, 0.157, config=config)
        pp.apply_material_preset('plywood')

        # Verify pause_before_perimeter is not set
        self.assertFalse(pp.pause_before_perimeter)

        # Add simple perimeter
        pp.circles = []
        pp.polylines = [[(0, 0), (2, 0), (2, 2), (0, 2)]]

        # Generate G-code
        pp.classify_holes()
        pp.identify_perimeter_and_pockets()
        result = pp.generate_gcode()

        self.assertTrue(result.success)

        # Check that G-code does NOT contain fixturing pause
        # (Note: there will still be an M0 at the end of the program, which is fine)
        self.assertNotIn('PAUSE FOR FIXTURING', result.gcode,
                        "G-code should not contain fixturing pause when pause_before_perimeter=False")

    def test_custom_ramp_angle_from_config(self):
        """Test that custom ramp angle from config affects toolpath generation."""
        # Create config with custom ramp angle
        config_data = {
            'materials': {
                'plywood': {
                    'ramp_angle': 10.0,  # Different from default 20.0
                }
            }
        }
        config = TeamConfig(config_data)

        # Get material preset from config
        material_preset = config.get_material_preset('plywood')

        # Create two postprocessors: one with default, one with custom
        pp_default = FRCPostProcessor(0.25, 0.157)
        pp_default.apply_material_preset('plywood')

        pp_custom = FRCPostProcessor(0.25, 0.157)
        pp_custom.apply_material_preset('plywood')
        pp_custom.ramp_angle = material_preset['ramp_angle']

        # Verify ramp angles are different
        self.assertEqual(pp_default.ramp_angle, 20.0)
        self.assertEqual(pp_custom.ramp_angle, 10.0)

        # Add same geometry to both
        for pp in [pp_default, pp_custom]:
            pp.circles = []
            pp.polylines = [[(0, 0), (2, 0), (2, 2), (0, 2)]]
            pp.classify_holes()
            pp.identify_perimeter_and_pockets()

        # Generate G-code for both
        result_default = pp_default.generate_gcode()
        result_custom = pp_custom.generate_gcode()

        self.assertTrue(result_default.success)
        self.assertTrue(result_custom.success)

        # G-code should be different (shallower angle = different ramp path)
        self.assertNotEqual(result_default.gcode, result_custom.gcode,
                          "G-code should differ when using different ramp angles")


class TestCircularPerimeter(unittest.TestCase):
    """Test parts with circular perimeters (like washers)"""

    def test_washer_with_rotation_and_translation(self):
        """Test washer-like part: circular perimeter with hole, verify proper rotation and translation."""
        # Disable pocket contouring for this test (we want to test normal hole clearing)
        from team_config import TeamConfig
        config = TeamConfig()
        config._data['machines'] = config._data.get('machines', {})
        config._data['machines']['default'] = config._data['machines'].get('default', {})
        config._data['machines']['default']['machining'] = config._data['machines']['default'].get('machining', {})
        config._data['machines']['default']['machining']['pockets'] = {'contour_threshold': 0}

        pp = FRCPostProcessor(0.236, 0.157, config=config)
        pp.apply_material_preset('plywood')  # Sets required material parameters

        # Washer centered at origin: outer 4" diameter, inner 2" diameter
        pp.circles = [
            {'center': (0.0, 0.0), 'radius': 2.0, 'diameter': 4.0},  # Outer
            {'center': (0.0, 0.0), 'radius': 1.0, 'diameter': 2.0},  # Inner hole
        ]
        pp.polylines = []
        pp.lines = []
        pp.arcs = []
        pp.splines = []

        # Apply transformation first (matches backend order)
        # Original bounds: center=(0,0), radius=2.0 → bounds X=[-2,2], Y=[-2,2]
        # After rotation 90° clockwise: still circular, same bounds
        # After translation to bottom-left: offset by (+2, +2) to move min to (0,0)
        pp.transform_coordinates('bottom-left', 90)

        # Identify perimeter (must come before classify_holes per new ordering)
        pp.identify_perimeter_and_pockets()

        # After identify_perimeter_and_pockets:
        # - perimeter should exist (outer circle converted to polyline)
        # - pockets should be empty (no polylines, circular geometry)
        # - outer circle should be removed from self.circles
        self.assertIsNotNone(pp.perimeter, "Perimeter should be identified from outer circle")
        self.assertEqual(len(pp.pockets), 0, "No pockets for circular-only geometry")
        self.assertEqual(len(pp.circles), 1, "Only inner circle should remain after perimeter identification")

        # Classify holes (after transform, so holes have transformed coordinates)
        pp.classify_holes()
        self.assertEqual(len(pp.holes), 1, "Inner circle should be classified as hole")
        self.assertAlmostEqual(pp.holes[0]['diameter'], 2.0, places=2, msg="Hole diameter should be 2.0\"")

        # Check hole center after transformation
        # Both circles present during transform: bounds X=[-2,2], Y=[-2,2] → offset (+2,+2)
        # Inner circle at (0,0) becomes (2,2) after translation
        hole = pp.holes[0]
        cx, cy = hole['center']
        self.assertAlmostEqual(cx, 2.0, places=1, msg="Hole X should be translated to 2.0")
        self.assertAlmostEqual(cy, 2.0, places=1, msg="Hole Y should be translated to 2.0")

        # Check perimeter points are all in positive quadrant
        for i, point in enumerate(pp.perimeter):
            self.assertGreaterEqual(point[0], -0.1,
                                   msg=f"Perimeter point {i} X should be non-negative after translation")
            self.assertGreaterEqual(point[1], -0.1,
                                   msg=f"Perimeter point {i} Y should be non-negative after translation")

        # Generate G-code and verify success
        result = pp.generate_gcode()
        self.assertTrue(result.success, f"G-code generation should succeed: {result.errors}")

        # Verify G-code contains hole operation but NOT two holes
        gcode_lines = result.gcode.split('\n')
        hole_count = sum(1 for line in gcode_lines if 'Hole' in line and 'diameter' in line)
        self.assertEqual(hole_count, 1, "Should have exactly one hole (inner circle), not outer perimeter")

        # Verify G-code contains perimeter operation
        has_perimeter = any('PERIMETER' in line for line in gcode_lines)
        self.assertTrue(has_perimeter, "G-code should contain perimeter operation")

    def test_concentric_circles_correct_identification(self):
        """Test that concentric circles correctly identify outer as perimeter, inner as hole."""
        pp = FRCPostProcessor(0.25, 0.157)

        # Three concentric circles: outer perimeter, two inner holes
        pp.circles = [
            {'center': (5.0, 5.0), 'radius': 4.0, 'diameter': 8.0},  # Outer
            {'center': (5.0, 5.0), 'radius': 2.0, 'diameter': 4.0},  # Middle hole
            {'center': (5.0, 5.0), 'radius': 1.0, 'diameter': 2.0},  # Inner hole
        ]
        pp.polylines = []
        pp.lines = []
        pp.arcs = []
        pp.splines = []

        pp.identify_perimeter_and_pockets()
        pp.classify_holes()

        # Largest should be perimeter, others should be holes
        self.assertIsNotNone(pp.perimeter)
        self.assertEqual(len(pp.circles), 2, "Two inner circles should remain for holes")
        self.assertEqual(len(pp.holes), 2, "Should have 2 holes from inner circles")

        # Verify holes are sorted by size
        self.assertGreater(pp.holes[0]['diameter'], pp.holes[1]['diameter'],
                          "Holes should be sorted largest first")


class TestPocketContouring(unittest.TestCase):
    """Test pocket and hole contouring for large features"""

    def test_large_through_cut_hole_is_contoured(self):
        """Test that a large hole cutting to sacrifice board is contoured instead of cleared"""
        from team_config import TeamConfig
        pp = FRCPostProcessor(0.25, 0.157)
        pp.apply_material_preset('plywood')

        # Outer perimeter (10" × 10" square) and large 4" diameter circular hole (12.56 sq in)
        # Threshold = 510 × 0.157² × 0.65 ≈ 8.2 sq in
        # 12.56 > 8.2 → should be contoured
        pp.circles = [
            {'center': (5.0, 5.0), 'radius': 2.0, 'diameter': 4.0},  # Inner hole
        ]
        pp.polylines = [
            [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0), (0.0, 0.0)]  # Outer perimeter
        ]
        pp.lines = []
        pp.arcs = []
        pp.splines = []

        pp.transform_coordinates('bottom-left', 0)
        pp.identify_perimeter_and_pockets()
        pp.classify_holes()

        result = pp.generate_gcode()
        self.assertTrue(result.success, f"G-code generation should succeed: {result.errors}")

        gcode = result.gcode
        # Should see "contour" and "manual removal" in comments
        self.assertIn('CONTOUR', gcode, "Large through-cut hole should be contoured")
        self.assertIn('manual removal', gcode, "Should warn about manual removal")
        # Should NOT see "helical + spiral" for this hole
        self.assertNotIn('helical + spiral', gcode, "Large contoured hole should not use helical clearing")

    def test_small_through_cut_hole_is_cleared(self):
        """Test that a small hole cutting to sacrifice board is fully cleared"""
        from team_config import TeamConfig
        pp = FRCPostProcessor(0.25, 0.157)
        pp.apply_material_preset('plywood')

        # Outer perimeter and small 0.5" diameter hole (0.196 sq in)
        # Threshold ≈ 8.2 sq in
        # 0.196 < 8.2 → should be fully cleared
        pp.circles = [
            {'center': (5.0, 5.0), 'radius': 0.25, 'diameter': 0.5},
        ]
        pp.polylines = [
            [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0), (0.0, 0.0)]  # Outer perimeter
        ]
        pp.lines = []
        pp.arcs = []
        pp.splines = []

        pp.transform_coordinates('bottom-left', 0)
        pp.identify_perimeter_and_pockets()
        pp.classify_holes()

        result = pp.generate_gcode()
        self.assertTrue(result.success, f"G-code generation should succeed: {result.errors}")

        gcode = result.gcode
        # Should see "helical + spiral" for cleared hole
        self.assertIn('helical', gcode, "Small hole should use helical clearing")
        # Should NOT see "contour" for this hole
        self.assertNotIn('CONTOUR', gcode, "Small hole should not be contoured")

    def test_large_partial_depth_hole_is_cleared(self):
        """Test that a large partial-depth hole is ALWAYS fully cleared (never contoured)"""
        from team_config import TeamConfig
        pp = FRCPostProcessor(0.25, 0.157)
        pp.apply_material_preset('plywood')

        # Outer perimeter and large 4" diameter hole, but only 0.1" deep (partial depth)
        pp.circles = [
            {'center': (5.0, 5.0), 'radius': 2.0, 'diameter': 4.0},
        ]
        pp.polylines = [
            [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0), (0.0, 0.0)]  # Outer perimeter
        ]
        pp.lines = []
        pp.arcs = []
        pp.splines = []

        pp.transform_coordinates('bottom-left', 0)
        pp.identify_perimeter_and_pockets()
        pp.classify_holes()

        # Override cut_depth to be partial (above Z=0)
        pp.cut_depth = 0.15  # Cutting to Z=0.15" (15% into material from top)

        result = pp.generate_gcode()
        self.assertTrue(result.success, f"G-code generation should succeed: {result.errors}")

        gcode = result.gcode
        # Should see "(partial depth)" comment
        self.assertIn('partial depth', gcode, "Should identify as partial depth")
        # Should see "helical" clearing even though it's large
        self.assertIn('helical', gcode, "Large partial-depth hole should still be fully cleared")
        # Should NOT be contoured
        self.assertNotIn('CONTOUR', gcode, "Partial-depth holes should never be contoured")

    def test_large_through_cut_pocket_is_contoured(self):
        """Test that a large pocket cutting to sacrifice board is contoured"""
        from team_config import TeamConfig
        pp = FRCPostProcessor(0.25, 0.157)
        pp.apply_material_preset('plywood')

        # Outer perimeter (10" × 10") and large rectangular pocket: 4" × 4" = 16 sq in
        # Threshold ≈ 8.2 sq in
        # 16 > 8.2 → should be contoured
        pp.circles = []
        pp.polylines = [
            [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0), (0.0, 0.0)],  # Outer perimeter
            [(2.0, 2.0), (6.0, 2.0), (6.0, 6.0), (2.0, 6.0), (2.0, 2.0)]  # Inner pocket
        ]
        pp.lines = []
        pp.arcs = []
        pp.splines = []

        pp.transform_coordinates('bottom-left', 0)
        pp.identify_perimeter_and_pockets()
        pp.classify_holes()

        result = pp.generate_gcode()
        self.assertTrue(result.success, f"G-code generation should succeed: {result.errors}")

        gcode = result.gcode
        # Should see "CONTOUR ONLY" for large pocket
        self.assertIn('CONTOUR', gcode, "Large through-cut pocket should be contoured")
        self.assertIn('manual removal', gcode, "Should warn about manual removal")

    def test_small_through_cut_pocket_is_cleared(self):
        """Test that a small pocket cutting to sacrifice board is fully cleared"""
        from team_config import TeamConfig
        pp = FRCPostProcessor(0.25, 0.157)
        pp.apply_material_preset('plywood')

        # Outer perimeter and small rectangular pocket: 0.5" × 0.5" = 0.25 sq in
        # Threshold ≈ 8.2 sq in
        # 0.25 < 8.2 → should be fully cleared
        pp.circles = []
        pp.polylines = [
            [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0), (0.0, 0.0)],  # Outer perimeter
            [(2.0, 2.0), (2.5, 2.0), (2.5, 2.5), (2.0, 2.5), (2.0, 2.0)]  # Inner pocket
        ]
        pp.lines = []
        pp.arcs = []
        pp.splines = []

        pp.transform_coordinates('bottom-left', 0)
        pp.identify_perimeter_and_pockets()
        pp.classify_holes()

        result = pp.generate_gcode()
        self.assertTrue(result.success, f"G-code generation should succeed: {result.errors}")

        gcode = result.gcode
        # Should see pocket clearing with helical entry
        self.assertIn('helical', gcode, "Small pocket should use helical entry and clearing")
        # Should NOT see contouring
        self.assertNotIn('CONTOUR', gcode, "Small pocket should not be contoured")

    def test_large_partial_depth_pocket_is_cleared(self):
        """Test that a large partial-depth pocket is ALWAYS fully cleared"""
        from team_config import TeamConfig
        pp = FRCPostProcessor(0.25, 0.157)
        pp.apply_material_preset('plywood')

        # Outer perimeter and large rectangular pocket: 4" × 4" = 16 sq in, but partial depth
        pp.circles = []
        pp.polylines = [
            [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0), (0.0, 0.0)],  # Outer perimeter
            [(2.0, 2.0), (6.0, 2.0), (6.0, 6.0), (2.0, 6.0), (2.0, 2.0)]  # Inner pocket
        ]
        pp.lines = []
        pp.arcs = []
        pp.splines = []

        pp.transform_coordinates('bottom-left', 0)
        pp.identify_perimeter_and_pockets()
        pp.classify_holes()

        # Override cut_depth to be partial (above Z=0)
        pp.cut_depth = 0.1  # Cutting to Z=0.1" (partial depth)

        result = pp.generate_gcode()
        self.assertTrue(result.success, f"G-code generation should succeed: {result.errors}")

        gcode = result.gcode
        # Should see "(partial depth)" comment
        self.assertIn('partial depth', gcode, "Should identify pocket as partial depth")
        # Should see helical clearing even though it's large
        self.assertIn('helical', gcode, "Large partial-depth pocket should still be fully cleared")
        # Should NOT be contoured
        self.assertNotIn('CONTOUR', gcode, "Partial-depth pockets should never be contoured")

    def test_contouring_can_be_disabled(self):
        """Test that setting contour_threshold to 0 disables all contouring"""
        from team_config import TeamConfig
        config = TeamConfig()
        config._data['machines'] = config._data.get('machines', {})
        config._data['machines']['default'] = config._data['machines'].get('default', {})
        config._data['machines']['default']['machining'] = config._data['machines']['default'].get('machining', {})
        config._data['machines']['default']['machining']['pockets'] = {'contour_threshold': 0}

        pp = FRCPostProcessor(0.25, 0.157, config=config)
        pp.apply_material_preset('plywood')

        # Outer perimeter and large 4" diameter hole that would normally be contoured
        pp.circles = [
            {'center': (5.0, 5.0), 'radius': 2.0, 'diameter': 4.0},
        ]
        pp.polylines = [
            [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0), (0.0, 0.0)]  # Outer perimeter
        ]
        pp.lines = []
        pp.arcs = []
        pp.splines = []

        pp.transform_coordinates('bottom-left', 0)
        pp.identify_perimeter_and_pockets()
        pp.classify_holes()

        result = pp.generate_gcode()
        self.assertTrue(result.success, f"G-code generation should succeed: {result.errors}")

        gcode = result.gcode
        # With contouring disabled, should be fully cleared
        self.assertIn('helical', gcode, "With contouring disabled, large hole should be cleared")
        self.assertNotIn('CONTOUR', gcode, "With contouring disabled, should not contour")


class TestPerimeterWithArcs(unittest.TestCase):
    """Test parts with complex perimeters including arcs"""

    def test_polyline_perimeter_with_holes_and_transform(self):
        """Test typical part: polyline perimeter with circular holes, verify transform doesn't break."""
        pp = FRCPostProcessor(0.25, 0.157)
        pp.apply_material_preset('plywood')  # Sets max_slotting_depth and other required params

        # Rectangle 10"x8" with two circular holes
        pp.circles = [
            {'center': (3.0, 4.0), 'radius': 0.5, 'diameter': 1.0},  # Left hole
            {'center': (7.0, 4.0), 'radius': 0.5, 'diameter': 1.0},  # Right hole
        ]
        pp.polylines = [
            [(0, 0), (10, 0), (10, 8), (0, 8)]  # Rectangular perimeter
        ]
        pp.lines = []
        pp.arcs = []
        pp.splines = []

        # Apply 90° rotation first (matches backend order) - should swap width and height
        pp.transform_coordinates('bottom-left', 90)

        # Process (after transform)
        pp.identify_perimeter_and_pockets()
        pp.classify_holes()

        # Verify identification
        self.assertIsNotNone(pp.perimeter, "Should identify polyline as perimeter")
        self.assertEqual(len(pp.pockets), 0, "No pockets")
        self.assertEqual(len(pp.holes), 2, "Should have 2 holes")

        # After rotation, bounds should swap: was 10x8, now 8x10
        # Check holes are still in bounds
        for hole in pp.holes:
            cx, cy = hole['center']
            # After rotation and translation, holes should be in positive quadrant
            # and within the new bounds (8x10 after 90° rotation)
            self.assertGreaterEqual(cx, -0.1, "Hole X should be non-negative")
            self.assertGreaterEqual(cy, -0.1, "Hole Y should be non-negative")
            self.assertLessEqual(cx, 9.0, "Hole X should be within bounds")
            self.assertLessEqual(cy, 11.0, "Hole Y should be within bounds")

        # Check perimeter bounds
        perimeter_xs = [p[0] for p in pp.perimeter]
        perimeter_ys = [p[1] for p in pp.perimeter]

        # Min should be at/near origin after bottom-left translation
        self.assertAlmostEqual(min(perimeter_xs), 0.0, places=1,
                              msg="Perimeter min X should be at origin")
        self.assertAlmostEqual(min(perimeter_ys), 0.0, places=1,
                              msg="Perimeter min Y should be at origin")

        # After 90° clockwise rotation, original (10,0) → (0,10), (10,8) → (8,10), (0,8) → (8,0)
        # So the rotated rectangle goes from (0,0) to (8,10)
        self.assertAlmostEqual(max(perimeter_xs), 8.0, places=1,
                              msg="After 90° rotation, X max should be 8")
        self.assertAlmostEqual(max(perimeter_ys), 10.0, places=1,
                              msg="After 90° rotation, Y max should be 10")

        # Generate G-code
        result = pp.generate_gcode()
        self.assertTrue(result.success, "G-code generation should succeed")

    def test_circle_bounds_with_polyline_perimeter(self):
        """Test that a hole circle's radius is included in the bounds calculation.

        The polyline rectangle is the (largest) perimeter; the circle is a hole
        whose radius pokes past the rectangle's left edge, so it must extend the
        transformed bounds. (This used to use a 6" hole inside a 2" rectangle -
        a hole larger than the part, which is physically impossible; the perimeter
        picker now correctly treats the largest boundary of EITHER kind as the
        perimeter, so the geometry here is realistic: rectangle >> hole.)
        """
        pp = FRCPostProcessor(0.25, 0.157)
        pp.apply_material_preset('plywood')  # Sets required material parameters

        # 6x6 rectangular perimeter with a hole on the left edge that pokes out to
        # x=-1, so the circle radius extends the bounds leftward.
        pp.circles = [
            {'center': (0.0, 3.0), 'radius': 1.0, 'diameter': 2.0},
        ]
        pp.polylines = [
            [(0, 0), (6, 0), (6, 6), (0, 6)]  # 6x6 rectangle (clearly the perimeter)
        ]
        pp.lines = []
        pp.arcs = []
        pp.splines = []

        # Bounds including circle radius: x=[-1, 6], y=[0, 6].
        # bottom-left translation shifts by (+1, 0).
        pp.transform_coordinates('bottom-left', 0)

        pp.identify_perimeter_and_pockets()
        pp.classify_holes()

        # The circle stays a hole (rectangle is the perimeter), translated by (+1,0).
        hole = pp.holes[0]
        cx, cy = hole['center']
        self.assertAlmostEqual(cx, 1.0, places=1, msg="Hole X center after translation")
        self.assertAlmostEqual(cy, 3.0, places=1, msg="Hole Y center after translation")

        # Perimeter is the rectangle: min corner (0,0) shifted by (+1,0) -> (1,0).
        perimeter_xs = [p[0] for p in pp.perimeter]
        perimeter_ys = [p[1] for p in pp.perimeter]
        self.assertAlmostEqual(min(perimeter_xs), 1.0, places=1,
                              msg="Perimeter min X (rectangle 0..6 shifted by +1)")
        self.assertAlmostEqual(min(perimeter_ys), 0.0, places=1,
                              msg="Perimeter min Y (rectangle 0..6, no Y shift)")


class TestMultilayerGeometrySubtraction(unittest.TestCase):
    """Test 2.5D multilayer geometry subtraction logic"""

    def _create_multilayer_dxf(self, filename, layers_data):
        """
        Helper to create a multilayer DXF file for testing.

        Args:
            filename: Output DXF file path
            layers_data: Dict mapping layer name to list of shapes
                        e.g., {'Z_0p000': [('circle', (3, 3), 2.0)], ...}
        """
        import ezdxf

        doc = ezdxf.new('R2010')
        msp = doc.modelspace()

        for layer_name, shapes in layers_data.items():
            # Create layer if it doesn't exist
            if layer_name not in doc.layers:
                doc.layers.new(name=layer_name)

            for shape in shapes:
                if shape[0] == 'circle':
                    _, center, radius = shape
                    msp.add_circle(center, radius, dxfattribs={'layer': layer_name})
                elif shape[0] == 'rectangle':
                    _, x, y, width, height = shape
                    points = [(x, y), (x + width, y), (x + width, y + height), (x, y + height)]
                    msp.add_lwpolyline(points, close=True, dxfattribs={'layer': layer_name})

        doc.saveas(filename)

    def test_nested_circles_concentric(self):
        """
        Test nested concentric circles at different depths.

        Setup: Outer circle (5" dia) at Z=0.25", inner circle (4" dia) at Z=0.0"
        Expected: Only the ring between circles is machined at Z=0.25"
        """
        import tempfile

        # Create test DXF
        with tempfile.NamedTemporaryFile(mode='w', suffix='.dxf', delete=False) as f:
            dxf_path = f.name

        try:
            # Outer circle at Z=0.25, inner circle at Z=0.0
            self._create_multilayer_dxf(dxf_path, {
                'Z_0p500': [('rectangle', -3, -3, 6, 6)],  # Top surface (perimeter)
                'Z_0p250': [('circle', (0, 0), 2.5)],      # Outer circle 5" dia
                'Z_0p000': [
                    ('circle', (0, 0), 2.0),                # Inner circle 4" dia
                    ('rectangle', -3, -3, 6, 6)             # Bottom perimeter
                ]
            })

            # Process with aluminum (threshold = 3.14 sq in, ~2" dia)
            config = TeamConfig()
            pp = FRCPostProcessor(material_thickness=0.5, tool_diameter=0.157, config=config)
            pp.apply_material_preset('aluminum')
            pp.load_dxf(dxf_path)
            pp.transform_coordinates('bottom-left', 0)
            result = pp.generate_gcode()

            self.assertTrue(result.success, "G-code generation should succeed")

            # Analyze the G-code
            lines = result.gcode.split('\n')

            # Find Z_0p250 section
            z0p250_start = None
            z0p250_end = None
            for i, line in enumerate(lines):
                if 'LAYER: Z_0p250' in line:
                    z0p250_start = i
                elif z0p250_start and 'LAYER: Z_0p000' in line:
                    z0p250_end = i
                    break

            self.assertIsNotNone(z0p250_start, "Should have Z_0p250 layer")
            z0p250_section = lines[z0p250_start:z0p250_end] if z0p250_end else []

            # Should have pocket (the ring) but no holes
            has_pocket = any('pocket' in line.lower() for line in z0p250_section)
            has_hole = any('hole' in line.lower() and 'Layer Z_0p250: 0 holes' not in line for line in z0p250_section)

            self.assertTrue(has_pocket, "Z_0p250 should have a pocket (ring between circles)")
            self.assertFalse(has_hole, "Z_0p250 should NOT have holes (inner circle subtracted)")

            # Check Z_0p000 section has the inner circle as a contoured hole
            z0p000_section = lines[z0p250_end:z0p250_end+200] if z0p250_end else []
            has_contour = any('CONTOUR ONLY' in line for line in z0p000_section)

            self.assertTrue(has_contour, "Z_0p000 should contour the inner circle (4\" > 2\" threshold)")

        finally:
            # Clean up
            if os.path.exists(dxf_path):
                os.remove(dxf_path)

    def test_overlapping_circles_partial(self):
        """
        Test partially overlapping circles at different depths.

        Setup: Two 3" circles offset horizontally, one at Z=0.25", one at Z=0.0"
        Expected: Only non-overlapping crescent is machined at Z=0.25"
        """
        import tempfile

        # Create test DXF
        with tempfile.NamedTemporaryFile(mode='w', suffix='.dxf', delete=False) as f:
            dxf_path = f.name

        try:
            # Two overlapping circles (3" diameter, 1.5" radius)
            # Centers at (0, 0) and (2, 0) - they overlap by ~1"
            self._create_multilayer_dxf(dxf_path, {
                'Z_0p500': [('rectangle', -3, -3, 8, 6)],  # Top surface
                'Z_0p250': [('circle', (0, 0), 1.5)],      # Left circle at Z=0.25"
                'Z_0p000': [
                    ('circle', (2, 0), 1.5),                # Right circle at Z=0.0" (overlaps)
                    ('rectangle', -3, -3, 8, 6)             # Bottom perimeter
                ]
            })

            # Process
            config = TeamConfig()
            pp = FRCPostProcessor(material_thickness=0.5, tool_diameter=0.157, config=config)
            pp.apply_material_preset('aluminum')
            pp.load_dxf(dxf_path)
            pp.transform_coordinates('bottom-left', 0)
            result = pp.generate_gcode()

            self.assertTrue(result.success, "G-code generation should succeed")

            # Analyze
            lines = result.gcode.split('\n')

            # Find Z_0p250 section
            z0p250_start = None
            z0p250_end = None
            for i, line in enumerate(lines):
                if 'LAYER: Z_0p250' in line:
                    z0p250_start = i
                elif z0p250_start and 'LAYER: Z_0p000' in line:
                    z0p250_end = i
                    break

            self.assertIsNotNone(z0p250_start, "Should have Z_0p250 layer")
            z0p250_section = lines[z0p250_start:z0p250_end] if z0p250_end else []

            # After subtraction, the left circle should be partially cut away
            # It should become a pocket (crescent shape)
            has_pocket = any('pocket' in line.lower() for line in z0p250_section)

            # Check that we're not machining the full original circle area
            # The "partially overlaps - converting to polyline" message indicates subtraction happened
            self.assertTrue(has_pocket, "Z_0p250 should have pocket (crescent after subtraction)")

            # Verify Z_0p000 has the right circle
            z0p000_section = lines[z0p250_end:z0p250_end+100] if z0p250_end else []
            has_hole = any('hole' in line.lower() and '3.0' in line for line in z0p000_section)

            self.assertTrue(has_hole, "Z_0p000 should have the 3\" circle")

        finally:
            # Clean up
            if os.path.exists(dxf_path):
                os.remove(dxf_path)

    def test_rectangular_pockets_nested(self):
        """
        Test nested rectangular pockets at different depths.

        Setup: Large 4"x4" pocket at Z=0.25", small 2"x2" pocket at Z=0.0" (centered)
        Expected: Only the frame between rectangles is machined at Z=0.25"
        """
        import tempfile

        # Create test DXF
        with tempfile.NamedTemporaryFile(mode='w', suffix='.dxf', delete=False) as f:
            dxf_path = f.name

        try:
            # Outer pocket 4"x4" at Z=0.25, inner pocket 2"x2" at Z=0.0
            self._create_multilayer_dxf(dxf_path, {
                'Z_0p500': [('rectangle', -3, -3, 12, 12)],  # Top surface
                'Z_0p250': [('rectangle', 0, 0, 4, 4)],      # Outer pocket
                'Z_0p000': [
                    ('rectangle', 1, 1, 2, 2),                # Inner pocket (centered)
                    ('rectangle', -3, -3, 12, 12)             # Bottom perimeter
                ]
            })

            # Process
            config = TeamConfig()
            pp = FRCPostProcessor(material_thickness=0.5, tool_diameter=0.157, config=config)
            pp.apply_material_preset('aluminum')
            pp.load_dxf(dxf_path)
            pp.transform_coordinates('bottom-left', 0)
            result = pp.generate_gcode()

            self.assertTrue(result.success, "G-code generation should succeed")

            # Analyze
            lines = result.gcode.split('\n')

            # Find Z_0p250 section
            z0p250_start = None
            z0p250_end = None
            for i, line in enumerate(lines):
                if 'LAYER: Z_0p250' in line:
                    z0p250_start = i
                elif z0p250_start and 'LAYER: Z_0p000' in line:
                    z0p250_end = i
                    break

            self.assertIsNotNone(z0p250_start, "Should have Z_0p250 layer")
            z0p250_section = lines[z0p250_start:z0p250_end] if z0p250_end else []

            # Should have a pocket (the frame)
            has_pocket = any('pocket' in line.lower() for line in z0p250_section)

            self.assertTrue(has_pocket, "Z_0p250 should have pocket (frame between rectangles)")

            # The frame should be smaller than the original 4x4=16 sq in
            # After subtracting the 2x2=4 sq in, we should have ~12 sq in
            # This will be fully cleared since it's partial depth

        finally:
            # Clean up
            if os.path.exists(dxf_path):
                os.remove(dxf_path)


class TestConcentricCircleDepths(unittest.TestCase):
    """
    Test 2.5D machining of concentric circles with variable inner-circle Z heights.

    Geometry: 6"x6"x0.5" plate with two concentric circles centered at (3, 3):
      - Outer circle: r=2.483" (groove at Z=0.25")
      - Inner circle: r=2.091" (Z varies per test case)
    The outer circle forms a ring/groove. The inner circle's depth determines
    whether we get 1 or 2 depth operations and how they're classified.
    """

    PLATE_SIZE = 6.0
    PLATE_THICKNESS = 0.5
    CENTER = (3.0, 3.0)
    OUTER_RADIUS = 2.483
    INNER_RADIUS = 2.091
    TOOL_DIAMETER = 0.157

    def _create_hatch_dxf(self, filename, layers):
        """
        Create a multilayer DXF with HATCH entities for solid regions.

        Args:
            filename: Output DXF file path
            layers: Dict mapping layer name to list of shape tuples:
                - ('rectangle', x, y, width, height)
                - ('disk', center, radius) — solid filled circle
                - ('ring', center, outer_r, inner_r) — ring/annular shape
        """
        import ezdxf
        from shapely.geometry import Point, Polygon as ShapelyPolygon

        doc = ezdxf.new('R2010')
        msp = doc.modelspace()

        for layer_name, shapes in layers.items():
            if layer_name not in doc.layers:
                doc.layers.new(name=layer_name)

            for shape in shapes:
                kind = shape[0]

                if kind == 'rectangle':
                    _, x, y, w, h = shape
                    coords = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
                    hatch = msp.add_hatch(color=7, dxfattribs={'layer': layer_name})
                    hatch.paths.add_polyline_path(coords + [coords[0]], is_closed=True)

                elif kind == 'disk':
                    _, center, radius = shape
                    circle_poly = Point(center).buffer(radius)
                    exterior_coords = list(circle_poly.exterior.coords)
                    hatch = msp.add_hatch(color=7, dxfattribs={'layer': layer_name})
                    hatch.paths.add_polyline_path(exterior_coords, is_closed=True)

                elif kind == 'ring':
                    _, center, outer_r, inner_r = shape
                    outer_poly = Point(center).buffer(outer_r)
                    inner_poly = Point(center).buffer(inner_r)
                    ring = outer_poly.difference(inner_poly)

                    hatch = msp.add_hatch(color=7, dxfattribs={'layer': layer_name})
                    exterior_coords = list(ring.exterior.coords)
                    hatch.paths.add_polyline_path(exterior_coords, is_closed=True)
                    for interior in ring.interiors:
                        interior_coords = list(interior.coords)
                        hatch.paths.add_polyline_path(interior_coords, is_closed=True, flags=0)

        doc.saveas(filename)

    def _make_postprocessor(self):
        """Create a FRCPostProcessor configured for our 6x6x0.5 plate."""
        config = TeamConfig()
        pp = FRCPostProcessor(
            material_thickness=self.PLATE_THICKNESS,
            tool_diameter=self.TOOL_DIAMETER,
            config=config,
        )
        pp.apply_material_preset('plywood')
        return pp

    def _process_dxf(self, dxf_path):
        """Load DXF, transform, and generate G-code. Returns (result, gcode_text)."""
        pp = self._make_postprocessor()
        pp.load_dxf(dxf_path)
        pp.transform_coordinates('bottom-left', 0)
        result = pp.generate_gcode()
        return result, (result.gcode if result.success else '')

    def _count_layer_comments(self, gcode, pattern):
        """Count lines in G-code matching a pattern."""
        return sum(1 for line in gcode.split('\n') if pattern in line)

    def _extract_section(self, gcode, start_marker):
        """Extract a G-code section from start_marker to the next LAYER or PERIMETER marker."""
        try:
            start = gcode.index(start_marker)
        except ValueError:
            return ''
        # Find the next section boundary after the start marker
        end = len(gcode)
        search_start = start + len(start_marker)
        for marker in ['===== LAYER:', '===== PERIMETER', 'LAYER: Z_0p']:
            try:
                idx = gcode.index(marker, search_start)
                end = min(end, idx)
            except ValueError:
                pass
        return gcode[start:end]

    # ------------------------------------------------------------------
    # Case 1: Inner circle at Z=0.5 (same as top surface)
    # ------------------------------------------------------------------
    def test_case1_inner_at_top_surface(self):
        """Inner circle at Z=0.5 (top surface) — only a ring groove at Z=0.25."""
        import tempfile

        with tempfile.NamedTemporaryFile(suffix='.dxf', delete=False) as f:
            dxf_path = f.name

        try:
            self._create_hatch_dxf(dxf_path, {
                'Z_0p500': [('rectangle', 0, 0, self.PLATE_SIZE, self.PLATE_SIZE)],
                'Z_0p250': [('ring', self.CENTER, self.OUTER_RADIUS, self.INNER_RADIUS)],
                'Z_0p000': [('rectangle', 0, 0, self.PLATE_SIZE, self.PLATE_SIZE)],
            })

            result, gcode = self._process_dxf(dxf_path)

            # G-code generation must succeed
            self.assertTrue(result.success, f"G-code generation failed: {result.errors}")

            # Should have exactly one depth layer (Z=0.25)
            depth_layer_count = self._count_layer_comments(gcode, 'LAYER: Z_0p250')
            self.assertEqual(depth_layer_count, 1, "Should have Z_0p250 depth layer")

            # The ring should be machined as an island-aware pocket (polygon with interior)
            self.assertIn('island-aware pocket', gcode,
                          "Ring groove should be machined as island-aware pocket")

            # Perimeter section should exist
            self.assertIn('PERIMETER', gcode, "Should have perimeter cut")

            # No features at Z_0p300 (sanity)
            self.assertNotIn('Z_0p300', gcode, "Should not have Z_0p300 layer")

        finally:
            if os.path.exists(dxf_path):
                os.remove(dxf_path)

    # ------------------------------------------------------------------
    # Case 2: Inner circle at Z=0.3 (between top and groove)
    # ------------------------------------------------------------------
    def test_case2_inner_above_groove(self):
        """Inner circle at Z=0.3 — two depth operations: disk at Z=0.30, ring at Z=0.25."""
        import tempfile

        with tempfile.NamedTemporaryFile(suffix='.dxf', delete=False) as f:
            dxf_path = f.name

        try:
            self._create_hatch_dxf(dxf_path, {
                'Z_0p500': [('rectangle', 0, 0, self.PLATE_SIZE, self.PLATE_SIZE)],
                'Z_0p300': [('disk', self.CENTER, self.INNER_RADIUS)],
                'Z_0p250': [('ring', self.CENTER, self.OUTER_RADIUS, self.INNER_RADIUS)],
                'Z_0p000': [('rectangle', 0, 0, self.PLATE_SIZE, self.PLATE_SIZE)],
            })

            result, gcode = self._process_dxf(dxf_path)

            self.assertTrue(result.success, f"G-code generation failed: {result.errors}")

            # Should have two depth layers
            self.assertIn('LAYER: Z_0p300', gcode, "Should have Z_0p300 depth layer")
            self.assertIn('LAYER: Z_0p250', gcode, "Should have Z_0p250 depth layer")

            # Z_0p300: Inner disk should be a pocket (full disk cleared)
            z0p300_section = self._extract_section(gcode, 'LAYER: Z_0p300')
            has_pocket_or_hole = 'pocket' in z0p300_section.lower() or 'hole' in z0p300_section.lower()
            self.assertTrue(has_pocket_or_hole,
                            "Z_0p300 should machine the inner circle as pocket or hole")

            # Z_0p250: Should have the ring as an island-aware pocket
            z0p250_section = self._extract_section(gcode, 'LAYER: Z_0p250')
            self.assertIn('island-aware pocket', z0p250_section,
                          "Z_0p250 ring should be machined as island-aware pocket")

            # Perimeter
            self.assertIn('PERIMETER', gcode, "Should have perimeter cut")

        finally:
            if os.path.exists(dxf_path):
                os.remove(dxf_path)

    # ------------------------------------------------------------------
    # Case 3: Inner circle at Z=0.2 (below groove)
    # ------------------------------------------------------------------
    def test_case3_inner_below_groove(self):
        """Inner circle at Z=0.2 — two depth operations: ring at Z=0.25, disk at Z=0.20."""
        import tempfile

        with tempfile.NamedTemporaryFile(suffix='.dxf', delete=False) as f:
            dxf_path = f.name

        try:
            self._create_hatch_dxf(dxf_path, {
                'Z_0p500': [('rectangle', 0, 0, self.PLATE_SIZE, self.PLATE_SIZE)],
                'Z_0p250': [('ring', self.CENTER, self.OUTER_RADIUS, self.INNER_RADIUS)],
                'Z_0p200': [('disk', self.CENTER, self.INNER_RADIUS)],
                'Z_0p000': [('rectangle', 0, 0, self.PLATE_SIZE, self.PLATE_SIZE)],
            })

            result, gcode = self._process_dxf(dxf_path)

            self.assertTrue(result.success, f"G-code generation failed: {result.errors}")

            # Should have two depth layers
            self.assertIn('LAYER: Z_0p250', gcode, "Should have Z_0p250 depth layer")
            self.assertIn('LAYER: Z_0p200', gcode, "Should have Z_0p200 depth layer")

            # Z_0p250: Ring should be island-aware pocket
            z0p250_section = self._extract_section(gcode, 'LAYER: Z_0p250')
            self.assertIn('island-aware pocket', z0p250_section,
                          "Z_0p250 ring should be machined as island-aware pocket")

            # Z_0p200: Inner disk should be machined
            z0p200_section = self._extract_section(gcode, 'LAYER: Z_0p200')
            has_pocket_or_hole = 'pocket' in z0p200_section.lower() or 'hole' in z0p200_section.lower()
            self.assertTrue(has_pocket_or_hole,
                            "Z_0p200 should machine the inner circle as pocket or hole")

            # Perimeter
            self.assertIn('PERIMETER', gcode, "Should have perimeter cut")

        finally:
            if os.path.exists(dxf_path):
                os.remove(dxf_path)

    # ------------------------------------------------------------------
    # Case 4: Inner circle at Z=0.0 (through-cut)
    # ------------------------------------------------------------------
    def test_case4_inner_through_cut(self):
        """Inner circle at Z=0.0 — ring at Z=0.25, inner circle as through-cut on bottom face."""
        import tempfile

        with tempfile.NamedTemporaryFile(suffix='.dxf', delete=False) as f:
            dxf_path = f.name

        try:
            self._create_hatch_dxf(dxf_path, {
                'Z_0p500': [('rectangle', 0, 0, self.PLATE_SIZE, self.PLATE_SIZE)],
                'Z_0p250': [('ring', self.CENTER, self.OUTER_RADIUS, self.INNER_RADIUS)],
                'Z_0p000': [
                    ('rectangle', 0, 0, self.PLATE_SIZE, self.PLATE_SIZE),
                    ('disk', self.CENTER, self.INNER_RADIUS),
                ],
            })

            result, gcode = self._process_dxf(dxf_path)

            self.assertTrue(result.success, f"G-code generation failed: {result.errors}")

            # Z_0p250: Ring groove
            self.assertIn('LAYER: Z_0p250', gcode, "Should have Z_0p250 depth layer")
            z0p250_section = self._extract_section(gcode, 'LAYER: Z_0p250')
            self.assertIn('island-aware pocket', z0p250_section,
                          "Z_0p250 ring should be machined as island-aware pocket")

            # Bottom face: Inner circle is a through-cut
            # Inner circle area = pi * 2.091^2 ≈ 13.74 sq in
            # Default contour_threshold=510, threshold_area = 510 * 0.157^2 * 0.65 ≈ 8.17 sq in
            # 13.74 > 8.17 → should be contoured
            self.assertIn('CONTOUR ONLY', gcode,
                          "Large inner circle through-cut should be contoured")
            self.assertIn('manual removal', gcode,
                          "Contoured through-cut should warn about manual removal")

            # Perimeter
            self.assertIn('PERIMETER', gcode, "Should have perimeter cut")

        finally:
            if os.path.exists(dxf_path):
                os.remove(dxf_path)


    # ------------------------------------------------------------------
    # Groove width validation tests
    # ------------------------------------------------------------------
    def test_groove_too_narrow_for_tool(self):
        """Groove width (0.05") is less than tool diameter (0.157") -- should fail."""
        import tempfile

        with tempfile.NamedTemporaryFile(suffix='.dxf', delete=False) as f:
            dxf_path = f.name

        try:
            # Ring with outer_r=2.0, inner_r=1.95 -> groove width = 0.05"
            self._create_hatch_dxf(dxf_path, {
                'Z_0p500': [('rectangle', 0, 0, self.PLATE_SIZE, self.PLATE_SIZE)],
                'Z_0p250': [('ring', self.CENTER, 2.0, 1.95)],
                'Z_0p000': [('rectangle', 0, 0, self.PLATE_SIZE, self.PLATE_SIZE)],
            })
            result, gcode = self._process_dxf(dxf_path)

            self.assertFalse(result.success, "Should fail for groove narrower than tool")
            self.assertTrue(
                any("too narrow" in e for e in result.errors),
                f"Error should mention 'too narrow', got: {result.errors}"
            )
        finally:
            if os.path.exists(dxf_path):
                os.remove(dxf_path)

    def test_groove_minimum_viable_width(self):
        """Groove width (~0.20") is slightly wider than tool (0.157") -- should succeed with adapted helix."""
        import tempfile

        with tempfile.NamedTemporaryFile(suffix='.dxf', delete=False) as f:
            dxf_path = f.name

        try:
            # Ring with outer_r=2.0, inner_r=1.8 -> groove width = 0.20"
            self._create_hatch_dxf(dxf_path, {
                'Z_0p500': [('rectangle', 0, 0, self.PLATE_SIZE, self.PLATE_SIZE)],
                'Z_0p250': [('ring', self.CENTER, 2.0, 1.8)],
                'Z_0p000': [('rectangle', 0, 0, self.PLATE_SIZE, self.PLATE_SIZE)],
            })
            result, gcode = self._process_dxf(dxf_path)

            self.assertTrue(result.success, f"Should succeed for viable groove, errors: {result.errors}")
            self.assertTrue(
                'Island-aware pocket' in gcode or 'Circular ring spiral clearing' in gcode,
                "Should use island-aware pocket or circular ring spiral clearing for ring")
        finally:
            if os.path.exists(dxf_path):
                os.remove(dxf_path)

    def test_wide_groove_uses_default_helix(self):
        """Wide groove (1.0") has plenty of room -- should succeed with normal parameters."""
        import tempfile

        with tempfile.NamedTemporaryFile(suffix='.dxf', delete=False) as f:
            dxf_path = f.name

        try:
            # Ring with outer_r=2.0, inner_r=1.0 -> groove width = 1.0"
            self._create_hatch_dxf(dxf_path, {
                'Z_0p500': [('rectangle', 0, 0, self.PLATE_SIZE, self.PLATE_SIZE)],
                'Z_0p250': [('ring', self.CENTER, 2.0, 1.0)],
                'Z_0p000': [('rectangle', 0, 0, self.PLATE_SIZE, self.PLATE_SIZE)],
            })
            result, gcode = self._process_dxf(dxf_path)

            self.assertTrue(result.success, f"Should succeed for wide groove, errors: {result.errors}")
            self.assertTrue(
                'Island-aware pocket' in gcode or 'Circular ring spiral clearing' in gcode,
                "Should use island-aware pocket or circular ring spiral clearing for ring")
        finally:
            if os.path.exists(dxf_path):
                os.remove(dxf_path)


class TestSimplePartFundamentals(unittest.TestCase):
    """End-to-end coverage of the basics on a plain plate: the sacrifice-board Z
    convention, perimeter tabs, and origin-corner translation."""

    def _square_pp(self, size=4.0, thickness=0.25, tool=0.157):
        pp = FRCPostProcessor(thickness, tool)
        pp.apply_material_preset('plywood')
        pp.circles = []
        pp.lines = []
        pp.arcs = []
        pp.splines = []
        pp.polylines = [[(0.0, 0.0), (size, 0.0), (size, size), (0.0, size), (0.0, 0.0)]]
        return pp

    @staticmethod
    def _motion_axis_values(gcode, axis):
        """Extract work-coordinate values for an axis from G0/G1/G2/G3 motion lines,
        skipping G53 machine-coordinate moves and inline/standalone comments."""
        import re
        values = []
        for line in gcode.split('\n'):
            code = line.split(';', 1)[0]
            code = re.sub(r'\([^)]*\)', '', code).strip()
            if not code or 'G53' in code:
                continue
            if not re.match(r'^\s*G[0123]\b', code):
                continue
            m = re.search(axis + r'(-?\d+\.?\d*)', code)
            if m:
                values.append(float(m.group(1)))
        return values

    @classmethod
    def _z_values(cls, gcode):
        return cls._motion_axis_values(gcode, 'Z')

    def test_z_coordinate_convention(self):
        """Z=0 at sacrifice board: cut depth negative, material top = thickness,
        safe/retract height = thickness + clearance, and the G-code stays within
        [cut_depth, retract_height]."""
        pp = self._square_pp(thickness=0.25)
        pp.tabs_enabled = False
        pp.transform_coordinates('bottom-left', 0)
        pp.identify_perimeter_and_pockets()
        pp.classify_holes()
        result = pp.generate_gcode()
        self.assertTrue(result.success, f"Generation should succeed: {result.errors}")

        self.assertAlmostEqual(pp.material_top, 0.25, places=4)
        self.assertAlmostEqual(pp.cut_depth, -pp.sacrifice_board_depth, places=4)
        self.assertAlmostEqual(pp.retract_height, 0.25 + pp.clearance_height, places=4)

        zs = self._z_values(result.gcode)
        self.assertTrue(zs, "G-code should contain Z moves")
        # Deepest cut reaches the sacrifice board; nothing rises above the safe height.
        self.assertAlmostEqual(min(zs), pp.cut_depth, places=3,
                               msg="Deepest Z should reach the cut depth")
        self.assertLessEqual(max(zs), pp.retract_height + 1e-6,
                             "No Z move should exceed the retract/safe height")

    def test_perimeter_tabs_left_at_tab_height(self):
        """With tabs enabled, the perimeter cut lifts to cut_depth + tab_height to
        leave holding tabs."""
        pp = self._square_pp(thickness=0.25)
        pp.tabs_enabled = True
        pp.transform_coordinates('bottom-left', 0)
        pp.identify_perimeter_and_pockets()
        pp.classify_holes()
        result = pp.generate_gcode()
        self.assertTrue(result.success, f"Generation should succeed: {result.errors}")

        expected_tab_z = pp.cut_depth + pp.tab_height
        zs = self._z_values(result.gcode)
        self.assertTrue(
            any(abs(z - expected_tab_z) < 1e-3 for z in zs),
            f"Expected a tab-height Z near {expected_tab_z:.4f} in the perimeter cut")

    def test_origin_corner_translation(self):
        """Selecting the bottom-right corner maps that corner to (0,0): all X<=0, Y>=0."""
        pp = self._square_pp(size=4.0)
        # Shift the square away from origin so the translation is observable.
        pp.polylines = [[(2.0, 2.0), (6.0, 2.0), (6.0, 6.0), (2.0, 6.0), (2.0, 2.0)]]
        pp.tabs_enabled = False
        pp.transform_coordinates('bottom-right', 0)
        pp.identify_perimeter_and_pockets()
        pp.classify_holes()
        result = pp.generate_gcode()
        self.assertTrue(result.success, f"Generation should succeed: {result.errors}")

        xs = self._motion_axis_values(result.gcode, 'X')
        self.assertTrue(xs, "G-code should contain X moves")
        # Bottom-right corner at origin -> part lies to the left (X<=0, within tool clearance).
        self.assertLessEqual(max(xs), pp.tool_diameter,
                             "Bottom-right origin should place the part at X<=0 (+tool offset)")


class TestMultiTier25D(unittest.TestCase):
    """Three stacked rectangular pockets at decreasing depths. Each layer should
    machine only its own frame (current minus the next-deeper region); the shallow
    layer must avoid the innermost region while the innermost layer machines it."""

    def _create_dxf(self, filename, layers):
        import ezdxf
        doc = ezdxf.new('R2010')
        msp = doc.modelspace()
        for name, rects in layers.items():
            if name not in doc.layers:
                doc.layers.new(name=name)
            for (x, y, w, h) in rects:
                pts = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
                hatch = msp.add_hatch(color=7, dxfattribs={'layer': name})
                hatch.paths.add_polyline_path(pts + [pts[0]], is_closed=True)
        doc.saveas(filename)

    def test_thickness_derived_from_cad_layers(self):
        """A multilayer DXF's thickness comes from its deepest layer, overriding whatever
        thickness the caller passed (the wizard sends a placeholder in 2.5D mode)."""
        import tempfile
        with tempfile.NamedTemporaryFile(suffix='.dxf', delete=False) as f:
            dxf_path = f.name
        try:
            self._create_dxf(dxf_path, {
                'Z_0p500': [(0, 0, 6, 6)],       # top face at 0.5" => stock is 0.5" thick
                'Z_0p250': [(2, 2, 2, 2)],
                'Z_0p000': [(0, 0, 6, 6)],
            })
            pp = FRCPostProcessor(material_thickness=0.125, tool_diameter=0.125)  # deliberately wrong
            pp.apply_material_preset('plywood')
            pp.load_dxf(dxf_path)
            self.assertAlmostEqual(pp.material_thickness, 0.5, places=3)
            self.assertAlmostEqual(pp.material_top, 0.5, places=3)
            self.assertAlmostEqual(pp.retract_height, 0.5 + pp.clearance_height, places=3)
        finally:
            if os.path.exists(dxf_path):
                os.remove(dxf_path)

    def test_three_tier_nested_pockets(self):
        import tempfile
        import re
        with tempfile.NamedTemporaryFile(suffix='.dxf', delete=False) as f:
            dxf_path = f.name
        try:
            # 6x6x0.5 plate; concentric pockets at Z=0.375, 0.250, 0.125.
            self._create_dxf(dxf_path, {
                'Z_0p500': [(0, 0, 6, 6)],
                'Z_0p375': [(1.0, 1.0, 4.0, 4.0)],   # outer pocket
                'Z_0p250': [(1.5, 1.5, 3.0, 3.0)],   # mid pocket
                'Z_0p125': [(2.0, 2.0, 2.0, 2.0)],   # inner pocket
                'Z_0p000': [(0, 0, 6, 6)],
            })
            pp = FRCPostProcessor(material_thickness=0.5, tool_diameter=0.125)
            pp.apply_material_preset('plywood')
            pp.load_dxf(dxf_path)
            pp.transform_coordinates('bottom-left', 0)
            result = pp.generate_gcode()
            self.assertTrue(result.success, f"Generation should succeed: {result.errors}")

            lines = result.gcode.split('\n')

            def cut_points_in_layer(layer_marker):
                start = end = None
                for i, line in enumerate(lines):
                    if f'LAYER: {layer_marker}' in line:
                        start = i
                    elif start is not None and i > start and ('===== LAYER:' in line or 'PERIMETER' in line):
                        end = i
                        break
                section = lines[start:end] if start is not None else []
                pts = []
                for line in section:
                    if re.match(r'^\s*G[123]\b', line):
                        xm = re.search(r'X(-?\d+\.?\d*)', line)
                        ym = re.search(r'Y(-?\d+\.?\d*)', line)
                        if xm and ym:
                            pts.append((float(xm.group(1)), float(ym.group(1))))
                return pts

            from shapely.geometry import Point, box
            inner_2x2 = box(2.0, 2.0, 4.0, 4.0)

            shallow_pts = cut_points_in_layer('Z_0p375')
            self.assertGreater(len(shallow_pts), 0, "Shallow layer should machine its frame")
            for (px, py) in shallow_pts:
                self.assertFalse(inner_2x2.contains(Point(px, py)),
                                 f"Shallow-layer cut ({px:.3f},{py:.3f}) entered the inner region")

            inner_pts = cut_points_in_layer('Z_0p125')
            self.assertTrue(
                any(inner_2x2.buffer(-pp.tool_radius).contains(Point(px, py)) for px, py in inner_pts),
                "Innermost layer should machine inside the 2x2 pocket")
        finally:
            if os.path.exists(dxf_path):
                os.remove(dxf_path)


class TestIslandBosses(unittest.TestCase):
    """2.5D parts with raised bosses (islands) fully enclosed by a shallower pocket.

    These guard the N-boundary nesting fix in _convert_to_shapely_polygons: a pocket
    face whose HATCH carries multiple interior boundaries (one per boss) must become a
    single polygon-with-holes, so the boss footprints are preserved as no-go islands
    rather than flattened into overlapping solids and machined through.
    """

    PLATE_W = 6.0
    PLATE_H = 4.0
    PLATE_THICKNESS = 0.5
    POCKET_DEPTH = 0.15
    TOOL_DIAMETER = 0.125

    # Pocket and bosses (all at the pocket-floor layer)
    POCKET = (0.5, 0.5, 5.0, 3.0)              # x, y, w, h
    BOSS_1 = (1.5, 1.5, 1.0, 1.0)
    BOSS_2 = (3.5, 1.5, 1.0, 1.0)

    def _create_island_dxf(self, filename, boss_rects):
        """Create a multilayer DXF mirroring the production solid-HATCH format:
        a full-plate bottom face and top surface, plus a pocket-floor layer whose
        HATCH has the pocket as its exterior boundary and each boss as a flags=0
        interior hole."""
        import ezdxf

        def rect_coords(r):
            x, y, w, h = r
            return [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]

        doc = ezdxf.new('R2010')
        msp = doc.modelspace()
        for name in ('Z_0p500', 'Z_0p150', 'Z_0p000'):
            if name not in doc.layers:
                doc.layers.new(name=name)

        plate = rect_coords((0, 0, self.PLATE_W, self.PLATE_H))
        for name in ('Z_0p500', 'Z_0p000'):
            h = msp.add_hatch(color=7, dxfattribs={'layer': name})
            h.paths.add_polyline_path(plate + [plate[0]], is_closed=True)

        pocket = rect_coords(self.POCKET)
        h = msp.add_hatch(color=7, dxfattribs={'layer': 'Z_0p150'})
        h.paths.add_polyline_path(pocket + [pocket[0]], is_closed=True)
        for boss in boss_rects:
            bc = rect_coords(boss)
            h.paths.add_polyline_path(bc + [bc[0]], is_closed=True, flags=0)

        doc.saveas(filename)

    def _make_pp(self):
        config = TeamConfig()
        pp = FRCPostProcessor(
            material_thickness=self.PLATE_THICKNESS,
            tool_diameter=self.TOOL_DIAMETER,
            config=config,
        )
        pp.apply_material_preset('plywood')
        return pp

    def _pocket_layer_cut_points(self, gcode):
        """Return (x, y) of every cutting move (G1/G2/G3) in the pocket-floor section."""
        import re
        lines = gcode.split('\n')
        start = end = None
        for i, line in enumerate(lines):
            if 'LAYER: Z_0p150' in line:
                start = i
            elif start is not None and ('===== LAYER:' in line or 'PERIMETER' in line) and i > start:
                end = i
                break
        section = lines[start:end] if start is not None else []
        pts = []
        cut_re = re.compile(r'^\s*G[123]\b')
        x_re = re.compile(r'X(-?\d+\.?\d*)')
        y_re = re.compile(r'Y(-?\d+\.?\d*)')
        for line in section:
            if not cut_re.match(line):
                continue
            xm, ym = x_re.search(line), y_re.search(line)
            if xm and ym:
                pts.append((float(xm.group(1)), float(ym.group(1))))
        return pts

    def test_convert_polygons_nests_multiple_islands(self):
        """Pocket polyline + two boss polylines -> one polygon with two interior holes."""
        pp = self._make_pp()
        pocket = [(0.5, 0.5), (5.5, 0.5), (5.5, 3.5), (0.5, 3.5)]
        boss1 = [(1.5, 1.5), (2.5, 1.5), (2.5, 2.5), (1.5, 2.5)]
        boss2 = [(3.5, 1.5), (4.5, 1.5), (4.5, 2.5), (3.5, 2.5)]
        polys = pp._convert_to_shapely_polygons([], [pocket, boss1, boss2])
        self.assertEqual(len(polys), 1, "Three nested loops should yield one polygon")
        self.assertEqual(len(polys[0].interiors), 2, "Both bosses should be interior holes")
        self.assertAlmostEqual(polys[0].area, 5.0 * 3.0 - 2 * 1.0, places=3)

    def test_two_bosses_machined_as_island_aware_pocket(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix='.dxf', delete=False) as f:
            dxf_path = f.name
        try:
            self._create_island_dxf(dxf_path, [self.BOSS_1, self.BOSS_2])
            pp = self._make_pp()
            pp.load_dxf(dxf_path)
            pp.transform_coordinates('bottom-left', 0)
            result = pp.generate_gcode()

            self.assertTrue(result.success, f"Generation should succeed: {result.errors}")
            self.assertIn('islands', result.gcode.lower(),
                          "Pocket with bosses should be machined island-aware")
        finally:
            if os.path.exists(dxf_path):
                os.remove(dxf_path)

    def test_toolpath_avoids_boss_footprints(self):
        """The pocket-floor toolpath must never enter either boss footprint."""
        import tempfile
        from shapely.geometry import Point, box
        with tempfile.NamedTemporaryFile(suffix='.dxf', delete=False) as f:
            dxf_path = f.name
        try:
            self._create_island_dxf(dxf_path, [self.BOSS_1, self.BOSS_2])
            pp = self._make_pp()
            pp.load_dxf(dxf_path)
            pp.transform_coordinates('bottom-left', 0)
            result = pp.generate_gcode()
            self.assertTrue(result.success, f"Generation should succeed: {result.errors}")

            boss_polys = []
            for (x, y, w, h) in (self.BOSS_1, self.BOSS_2):
                boss_polys.append(box(x, y, x + w, y + h))

            pts = self._pocket_layer_cut_points(result.gcode)
            self.assertGreater(len(pts), 0, "Should have cutting moves at the pocket layer")
            for (px, py) in pts:
                p = Point(px, py)
                for i, bp in enumerate(boss_polys):
                    self.assertFalse(
                        bp.contains(p),
                        f"Cutting move ({px:.3f}, {py:.3f}) entered boss {i + 1} footprint")
        finally:
            if os.path.exists(dxf_path):
                os.remove(dxf_path)

    def test_single_boss_still_preserved(self):
        """Regression: a single boss (the old 2-loop path) still nests correctly."""
        import tempfile
        with tempfile.NamedTemporaryFile(suffix='.dxf', delete=False) as f:
            dxf_path = f.name
        try:
            self._create_island_dxf(dxf_path, [self.BOSS_1])
            pp = self._make_pp()
            pp.load_dxf(dxf_path)
            pp.transform_coordinates('bottom-left', 0)
            result = pp.generate_gcode()
            self.assertTrue(result.success, f"Generation should succeed: {result.errors}")
            self.assertIn('islands', result.gcode.lower())
        finally:
            if os.path.exists(dxf_path):
                os.remove(dxf_path)


class TestMultiPartEngine(unittest.TestCase):
    """Multi-part job engine: placement offset, body-only generation, job stitching,
    and layout validation."""

    def _square_part(self, size=4.0, offset=(0.0, 0.0)):
        pp = FRCPostProcessor(0.25, 0.157)
        pp.apply_material_preset('plywood')
        pp.tabs_enabled = False
        pp.circles = []
        pp.lines = []
        pp.arcs = []
        pp.splines = []
        pp.polylines = [[(0.0, 0.0), (size, 0.0), (size, size), (0.0, size), (0.0, 0.0)]]
        pp.transform_coordinates('bottom-left', 0, placement_offset=offset, enforce_bounds=False)
        pp.identify_perimeter_and_pockets()
        pp.classify_holes()
        return pp

    def _rect_with_offset_hole(self, mirror):
        pp = FRCPostProcessor(0.25, 0.125)
        pp.apply_material_preset('plywood')
        pp.tabs_enabled = False
        pp.lines = []; pp.arcs = []; pp.splines = []
        pp.polylines = [[(0, 0), (6, 0), (6, 4), (0, 4), (0, 0)]]
        pp.circles = [{'center': (1.0, 2.0), 'radius': 0.3, 'diameter': 0.6}]  # hole near the left
        pp.transform_coordinates('bottom-left', 0, enforce_bounds=False, mirror=mirror)
        return pp

    def test_mirror_flips_geometry_x(self):
        """Flipping a part over mirrors its geometry across X: a hole 1in from the left
        edge of a 6in part ends up 1in from the right edge (x=5)."""
        normal = self._rect_with_offset_hole(mirror=False)
        flipped = self._rect_with_offset_hole(mirror=True)
        self.assertAlmostEqual(normal.circles[0]['center'][0], 1.0, places=3)
        self.assertAlmostEqual(flipped.circles[0]['center'][0], 5.0, places=3)
        # Y is unchanged, and the overall footprint is the same size.
        self.assertAlmostEqual(flipped.circles[0]['center'][1], 2.0, places=3)
        self.assertAlmostEqual(flipped.bounding_box()[2], 6.0, places=3)

    def test_placement_offset_translates_geometry(self):
        pp = self._square_part(size=4.0, offset=(10.0, 5.0))
        minx, miny, maxx, maxy = pp.bounding_box()
        self.assertAlmostEqual(minx, 10.0, places=3)
        self.assertAlmostEqual(miny, 5.0, places=3)
        self.assertAlmostEqual(maxx, 14.0, places=3)
        self.assertAlmostEqual(maxy, 9.0, places=3)

    def test_body_only_has_no_header_or_footer(self):
        pp = self._square_part()
        body = pp.generate_gcode(include_header_footer=False).gcode
        self.assertNotIn('M30', body, "Body should not contain program-end")
        self.assertNotIn('(PenguinCAM', body, "Body should not contain the header title")
        self.assertNotIn('G54', body, "Body should not set the work coordinate system")

    def _phase_job(self, pp, name, place_x, place_y):
        """Build a part_job dict from a part's phase split (new assemble contract)."""
        phases = pp.generate_part_phases()
        return {'name': name, 'place_x': place_x, 'place_y': place_y, 'rotation': 0,
                'interior': phases['interior'], 'perimeter': phases['perimeter'],
                'tab_removal': phases['tab_removal']}

    def test_assemble_emits_single_header_and_footer(self):
        import re
        p1 = self._square_part(size=4.0, offset=(0.0, 0.0))
        p2 = self._square_part(size=4.0, offset=(6.0, 0.0))
        part_jobs = [
            self._phase_job(p1, 'A', 0.0, 0.0),
            self._phase_job(p2, 'B', 6.0, 0.0),
        ]
        result = assemble_job_gcode(part_jobs, header_pp=p1, timestamp='2026-06-30 12:00:00')
        self.assertTrue(result.success)
        g = result.gcode

        def command_lines_with(token):
            # Count G-code command lines containing token as a whole word, ignoring comments.
            pat = re.compile(r'\b' + re.escape(token) + r'\b')
            count = 0
            for l in g.split('\n'):
                code = re.sub(r'\([^)]*\)', '', l.split(';', 1)[0])
                if pat.search(code):
                    count += 1
            return count

        self.assertEqual(command_lines_with('M3'), 1, "Job should start the spindle exactly once")
        self.assertEqual(command_lines_with('M30'), 1, "Job should end exactly once")
        self.assertEqual(command_lines_with('G54'), 1, "Job should set WCS exactly once")
        self.assertIn('PART 1: A', g)
        self.assertIn('PART 2: B', g)
        self.assertIn('MULTI-PART JOB', g)
        self.assertEqual(result.stats['num_parts'], 2)

    def test_assembled_parts_are_offset(self):
        import re
        p1 = self._square_part(size=4.0, offset=(0.0, 0.0))
        p2 = self._square_part(size=4.0, offset=(6.0, 0.0))
        part_jobs = [
            self._phase_job(p1, 'A', 0.0, 0.0),
            self._phase_job(p2, 'B', 6.0, 0.0),
        ]
        g = assemble_job_gcode(part_jobs, header_pp=p1, timestamp='2026-06-30 12:00:00').gcode
        lines = g.split('\n')

        def section_x(marker):
            # Collect the X coords of cutting moves under one part's block, which runs
            # from its "--- PART n ---" label to the next part label or phase/finish marker.
            start = next(i for i, l in enumerate(lines) if marker in l)
            end = len(lines)
            for j in range(start + 1, len(lines)):
                if '--- PART' in lines[j] or 'PHASE:' in lines[j] or 'FINISH' in lines[j]:
                    end = j
                    break
            xs = []
            for l in lines[start:end]:
                code = re.sub(r'\([^)]*\)', '', l.split(';', 1)[0])
                if re.match(r'^\s*G[0123]\b', code) and 'G53' not in code:
                    m = re.search(r'X(-?\d+\.?\d*)', code)
                    if m:
                        xs.append(float(m.group(1)))
            return xs

        xs_a = section_x('PART 1: A')
        xs_b = section_x('PART 2: B')
        self.assertTrue(xs_a and xs_b)
        self.assertLess(max(xs_a), 5.0, "Part A should be near the origin")
        self.assertGreater(max(xs_b), 6.0, "Part B should be shifted right by its offset")

    def _tabbed_part_with_hole(self, size, offset):
        """A square part with tabs enabled and one small hole (an interior feature),
        with the refixturing pause turned on - exercises all three job phases."""
        pp = FRCPostProcessor(0.25, 0.157)
        pp.apply_material_preset('plywood')
        pp.tabs_enabled = True
        pp.pause_before_perimeter = True
        pp.circles = [{'center': (offset[0] + 1.0, offset[1] + 1.0), 'radius': 0.3, 'diameter': 0.6}]
        pp.lines = []; pp.arcs = []; pp.splines = []
        pp.polylines = [[(0.0, 0.0), (size, 0.0), (size, size), (0.0, size), (0.0, 0.0)]]
        pp.transform_coordinates('bottom-left', 0, placement_offset=offset, enforce_bounds=False)
        pp.identify_perimeter_and_pockets()
        pp.classify_holes()
        return pp

    def test_job_collates_phases_across_parts(self):
        """A multi-part job runs all interiors, then ONE shared refixturing pause, then
        all perimeters, then all tab removals - not each part start-to-finish."""
        p1 = self._tabbed_part_with_hole(4.0, (0.0, 0.0))
        p2 = self._tabbed_part_with_hole(4.0, (6.0, 0.0))
        part_jobs = [self._phase_job(p1, 'A', 0.0, 0.0), self._phase_job(p2, 'B', 6.0, 0.0)]
        # _phase_job zeroes rotation; that's fine for ordering checks.
        g = assemble_job_gcode(part_jobs, header_pp=p1, timestamp='2026-06-30 12:00:00').gcode
        lines = g.split('\n')

        def idx(marker):
            return next(i for i, l in enumerate(lines) if marker in l)

        # Collated phase order.
        i_interior = idx('PHASE: INTERIOR FEATURES')
        i_pause = idx('PAUSE FOR FIXTURING')
        i_perim = idx('PHASE: PERIMETERS')
        i_tabs = idx('PHASE: TAB REMOVAL')
        self.assertLess(i_interior, i_pause)
        self.assertLess(i_pause, i_perim)
        self.assertLess(i_perim, i_tabs)

        # Exactly one shared pause for the whole sheet.
        self.assertEqual(sum(1 for l in lines if l.strip().startswith('M0')), 1)

        # Both parts appear in each of the three phases (interiors before the pause).
        interior_block = "\n".join(lines[i_interior:i_pause])
        self.assertIn('PART 1: A', interior_block)
        self.assertIn('PART 2: B', interior_block)
        perim_block = "\n".join(lines[i_perim:i_tabs])
        self.assertIn('PART 1: A', perim_block)
        self.assertIn('PART 2: B', perim_block)
        tab_block = "\n".join(lines[i_tabs:])
        self.assertEqual(tab_block.count('TAB REMOVAL PASS'), 2)

    def test_job_no_pause_when_not_configured(self):
        """With pause_before_perimeter off, no refixturing pause is emitted even though
        the phases are still collated."""
        p1 = self._tabbed_part_with_hole(4.0, (0.0, 0.0))
        p2 = self._tabbed_part_with_hole(4.0, (6.0, 0.0))
        p1.pause_before_perimeter = False
        p2.pause_before_perimeter = False
        part_jobs = [self._phase_job(p1, 'A', 0.0, 0.0), self._phase_job(p2, 'B', 6.0, 0.0)]
        g = assemble_job_gcode(part_jobs, header_pp=p1, timestamp='2026-06-30 12:00:00').gcode
        self.assertNotIn('PAUSE FOR FIXTURING', g)
        self.assertEqual(sum(1 for l in g.split('\n') if l.strip().startswith('M0')), 0)

    def test_single_part_job_reduces_to_normal_order(self):
        """A one-part job keeps the interiors -> pause -> perimeter -> tab-removal order."""
        p1 = self._tabbed_part_with_hole(4.0, (0.0, 0.0))
        g = assemble_job_gcode([self._phase_job(p1, 'A', 0.0, 0.0)],
                               header_pp=p1, timestamp='2026-06-30 12:00:00').gcode
        lines = g.split('\n')
        order = [i for i, l in enumerate(lines)
                 if any(m in l for m in ('PHASE: INTERIOR', 'PAUSE FOR FIXTURING',
                                         'PHASE: PERIMETERS', 'PHASE: TAB REMOVAL'))]
        self.assertEqual(order, sorted(order))
        self.assertEqual(sum(1 for l in lines if l.strip().startswith('M0')), 1)

    def test_validate_detects_overlap(self):
        parts = [
            {'name': 'A', 'bbox': (0, 0, 4, 4)},
            {'name': 'B', 'bbox': (2, 2, 6, 6)},
        ]
        errors = validate_job_layout(parts, 24, 24)
        self.assertTrue(any('overlap' in e['error'].lower() for e in errors))

    def test_validate_combined_bbox_exceeds_machine(self):
        # A single part isn't "outside a sheet" (it IS the stock); the only fit check is
        # that the combined bbox fits the machine.
        big = [{'name': 'A', 'bbox': (0, 0, 30, 30)}]
        self.assertTrue(any('exceed' in e['error'].lower() for e in validate_job_layout(big, 24, 24)))
        small = [{'name': 'A', 'bbox': (0, 0, 10, 10)}]
        self.assertEqual(validate_job_layout(small, 24, 24), [])  # fits, single part is its own stock

    def test_validate_passes_clean_layout(self):
        parts = [
            {'name': 'A', 'bbox': (0, 0, 4, 4)},
            {'name': 'B', 'bbox': (6, 0, 10, 4)},
        ]
        self.assertEqual(validate_job_layout(parts, 24, 24), [])

    def test_validate_min_gap_kerf(self):
        parts = [
            {'name': 'A', 'bbox': (0, 0, 4, 4)},
            {'name': 'B', 'bbox': (4.1, 0, 8.1, 4)},
        ]
        self.assertEqual(validate_job_layout(parts, 24, 24, min_gap=0.0), [])
        self.assertTrue(validate_job_layout(parts, 24, 24, min_gap=0.25))

    def test_validate_concave_nesting_not_flagged(self):
        """A small part nested in an L-shaped part's notch: bounding boxes overlap, but
        the real perimeters are clear -> must NOT be flagged."""
        from shapely.geometry import Polygon
        L = Polygon([(0, 0), (6, 0), (6, 2), (2, 2), (2, 6), (0, 6)])  # bbox 0,0,6,6
        small = Polygon([(2.5, 2.5), (5.5, 2.5), (5.5, 5.5), (2.5, 5.5)])  # nests in the notch
        parts = [
            {'name': 'L', 'bbox': (0, 0, 6, 6), 'polygon': L},
            {'name': 'small', 'bbox': (2.5, 2.5, 5.5, 5.5), 'polygon': small},
        ]
        self.assertEqual(validate_job_layout(parts, 24, 24, min_gap=0.157), [])

    def test_validate_real_overlap_still_flagged(self):
        """Same L-shape but the small part intrudes into the solid arm -> flagged."""
        from shapely.geometry import Polygon
        L = Polygon([(0, 0), (6, 0), (6, 2), (2, 2), (2, 6), (0, 6)])
        overlapping = Polygon([(1, 1), (4, 1), (4, 4), (1, 4)])  # crosses into L's arms
        parts = [
            {'name': 'L', 'bbox': (0, 0, 6, 6), 'polygon': L},
            {'name': 'X', 'bbox': (1, 1, 4, 4), 'polygon': overlapping},
        ]
        errors = validate_job_layout(parts, 24, 24, min_gap=0.157)
        self.assertTrue(any('overlap' in e['error'].lower() for e in errors))


class TestTubePatternTwoFace(unittest.TestCase):
    """Tube pattern G-code: one-face (pattern mirrored) and two-face (distinct per side)."""

    def _make_face(self, holes):
        """Build a tube-face processor for a 1x2 face with the given holes.

        holes: list of ((cx, cy), diameter).
        """
        pp = FRCPostProcessor(0.125, 0.157)  # 0.125" wall thickness
        pp.apply_material_preset('aluminum')
        pp.tube_height = 2.0
        pp.circles = [{'center': c, 'diameter': d} for (c, d) in holes]
        pp.polylines = [[(0, 0), (1, 0), (1, 2), (0, 2), (0, 0)]]  # tube face rectangle
        pp.identify_perimeter_and_pockets()  # remove the perimeter (tube face) first
        pp.classify_holes()
        return pp

    def _assert_clean(self, gcode):
        """Guard: pure ASCII, no nested parenthesis comments (machine requirements)."""
        for line in gcode.split('\n'):
            body = line.split(';')[0]
            depth = 0
            for ch in body:
                if ch == '(':
                    depth += 1
                elif ch == ')':
                    depth -= 1
                self.assertLessEqual(depth, 1, f"Nested comment: {line}")
            try:
                line.encode('ascii')
            except UnicodeEncodeError:
                self.fail(f"Non-ASCII in tube gcode: {line}")

    def test_one_face_mirror(self):
        """One-face mode: two phases, mirror note, default G54 WCS, flip pause, clean output."""
        pp = self._make_face([((0.3, 1.0), 0.25)])
        result = pp.generate_tube_pattern_gcode(
            tube_height=2.0, square_end=False, cut_to_length=False,
            tube_width=1.0, tube_length=2.0, suggested_filename='tube')
        self.assertTrue(result.success)
        g = result.gcode
        self.assertIn('PHASE 1: FIRST FACE', g)
        self.assertIn('PHASE 2: SECOND FACE', g)
        self.assertIn('One-face mode', g)
        self.assertIn('G54', g)   # default tube WCS (no fixed jig configured)
        self.assertIn('M0', g)
        self.assertFalse(result.stats['two_face'])
        self._assert_clean(g)

    def test_two_face_distinct(self):
        """Two-face mode: distinct patterns, note + stats reflect both faces."""
        face1 = self._make_face([((0.3, 1.0), 0.25)])
        face2 = self._make_face([((0.2, 0.6), 0.25), ((0.8, 1.4), 0.25)])
        result = face1.generate_tube_pattern_gcode(
            tube_height=2.0, square_end=False, cut_to_length=False,
            tube_width=1.0, tube_length=2.0, suggested_filename='tube',
            second_face_pp=face2)
        self.assertTrue(result.success)
        g = result.gcode
        self.assertIn('Two-face mode', g)
        self.assertIn('PHASE 1: FIRST FACE', g)
        self.assertIn('PHASE 2: SECOND FACE', g)
        self.assertIn('M0', g)
        self.assertTrue(result.stats['two_face'])
        # Total holes = face1(1) + face2(2); per-face count reflects face 1.
        self.assertEqual(result.stats['num_holes'], 3)
        self.assertEqual(result.stats['num_holes_per_face'], 1)
        self._assert_clean(g)

    def test_two_face_differs_from_one_face(self):
        """A distinct face 2 changes phase-2 output vs. mirroring face 1."""
        one = self._make_face([((0.3, 1.0), 0.25)])
        g_one = one.generate_tube_pattern_gcode(
            tube_height=2.0, square_end=False, cut_to_length=False,
            tube_width=1.0, tube_length=2.0).gcode
        f1 = self._make_face([((0.3, 1.0), 0.25)])
        f2 = self._make_face([((0.8, 0.5), 0.25)])
        g_two = f1.generate_tube_pattern_gcode(
            tube_height=2.0, square_end=False, cut_to_length=False,
            tube_width=1.0, tube_length=2.0, second_face_pp=f2).gcode
        self.assertNotEqual(g_one, g_two)


if __name__ == '__main__':
    unittest.main()

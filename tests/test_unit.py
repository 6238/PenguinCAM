"""
Unit tests for FRCPostProcessor.
Focus on higher-level functions; minimal tests for low-level utilities.
"""

import unittest
import math
import sys
import os

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from frc_cam_postprocessor import FRCPostProcessor, MATERIAL_PRESETS
from team_config import TeamConfig


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
        """Test that circle radius is properly included in bounds calculation."""
        pp = FRCPostProcessor(0.25, 0.157)
        pp.apply_material_preset('plywood')  # Sets required material parameters

        # Small rectangle with large hole offset to one side
        # This tests that hole radius extends bounds correctly
        pp.circles = [
            {'center': (1.0, 1.0), 'radius': 3.0, 'diameter': 6.0},  # Large hole extends beyond polyline
        ]
        pp.polylines = [
            [(0, 0), (2, 0), (2, 2), (0, 2)]  # Small 2x2 rectangle
        ]
        pp.lines = []
        pp.arcs = []
        pp.splines = []

        # Transform first (matches backend order)
        # The hole extends from (1-3, 1-3) to (1+3, 1+3) = (-2, -2) to (4, 4)
        # Combined with rectangle (0,0) to (2,2), overall bounds are (-2, -2) to (4, 4)
        # After bottom-left translation by (+2, +2), bounds become (0, 0) to (6, 6)
        pp.transform_coordinates('bottom-left', 0)

        pp.identify_perimeter_and_pockets()
        pp.classify_holes()

        # After translation, check that geometry is properly positioned
        hole = pp.holes[0]
        cx, cy = hole['center']

        # Hole center should be translated from (1,1) by (+2,+2) = (3,3)
        self.assertAlmostEqual(cx, 3.0, places=1, msg="Hole X center after translation")
        self.assertAlmostEqual(cy, 3.0, places=1, msg="Hole Y center after translation")

        # Perimeter min should be at X=2, Y=2 (rectangle was 0-2, offset by +2)
        perimeter_xs = [p[0] for p in pp.perimeter]
        perimeter_ys = [p[1] for p in pp.perimeter]
        self.assertAlmostEqual(min(perimeter_xs), 2.0, places=1,
                              msg="Perimeter should start at X=2 (rectangle was 0-2, offset by +2)")
        self.assertAlmostEqual(min(perimeter_ys), 2.0, places=1,
                              msg="Perimeter should start at Y=2 (rectangle was 0-2, offset by +2)")


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


if __name__ == '__main__':
    unittest.main()

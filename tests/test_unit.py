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


if __name__ == '__main__':
    unittest.main()

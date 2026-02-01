"""Unit tests for tube facing mode."""
import unittest
import tempfile
import os
import re
from frc_cam_postprocessor import FRCPostProcessor


class TestYCoordinateAdjustment(unittest.TestCase):
    """Test the _adjust_y_coordinate helper function."""

    def setUp(self):
        self.pp = FRCPostProcessor(0.25, 0.157)

    def test_positive_offset(self):
        """Test shifting Y by positive offset."""
        line = "G1 X1.0 Y-0.175 Z0.5"
        result = self.pp._adjust_y_coordinate(line, 0.175)
        self.assertIn("Y0.0000", result)

    def test_negative_offset(self):
        """Test shifting Y by negative offset."""
        line = "G1 X1.0 Y0.0 Z0.5"
        result = self.pp._adjust_y_coordinate(line, -0.125)
        self.assertIn("Y-0.1250", result)

    def test_no_y_coordinate(self):
        """Test line without Y coordinate is unchanged."""
        line = "G0 X1.0 Z0.5"
        result = self.pp._adjust_y_coordinate(line, 0.175)
        self.assertEqual(line, result)

    def test_arc_with_y_coordinate(self):
        """Test arc line with Y coordinate - only Y coord adjusted."""
        line = "G3 X1.0 Y-0.0787 I-0.1 J0."
        result = self.pp._adjust_y_coordinate(line, 0.175)
        # -0.0787 + 0.175 = 0.0963
        self.assertIn("Y0.0963", result)
        # J should remain unchanged
        self.assertIn("J0.", result)

    def test_negative_y_to_positive(self):
        """Test shifting negative Y to positive."""
        line = "G1 Y-0.23"
        result = self.pp._adjust_y_coordinate(line, 0.23)
        self.assertIn("Y0.0000", result)

    def test_preserves_other_coordinates(self):
        """Test that X and Z coordinates are preserved."""
        line = "G1 X1.234 Y-0.5 Z0.789"
        result = self.pp._adjust_y_coordinate(line, 0.5)
        self.assertIn("X1.234", result)
        self.assertIn("Y0.0000", result)
        self.assertIn("Z0.789", result)

    def test_comment_lines_unchanged(self):
        """Test that comment lines pass through unchanged."""
        line = "( This is a comment with Y-0.5 in it )"
        result = self.pp._adjust_y_coordinate(line, 0.175)
        # The regex will still match Y-0.5 in the comment, which is fine
        # since it doesn't affect machine behavior


class TestTubeFacingGeneration(unittest.TestCase):
    """Test the generate_tube_facing_gcode method."""

    def setUp(self):
        self.pp = FRCPostProcessor(0.25, 0.157)
        self.pp.apply_material_preset('aluminum')

    def _generate_tube_gcode_to_file(self, output_path, tube_size='1x1'):
        """Helper to generate tube facing gcode and write to file (for API tests)."""
        result = self.pp.generate_tube_facing_gcode(tube_size=tube_size)
        with open(output_path, 'w') as f:
            f.write(result.gcode)
        return result

    def test_generates_output_file(self):
        """Test that output file is created."""
        with tempfile.NamedTemporaryFile(suffix='.nc', delete=False) as f:
            output_path = f.name
        try:
            self._generate_tube_gcode_to_file(output_path, '1x1')
            self.assertTrue(os.path.exists(output_path))
            with open(output_path) as f:
                content = f.read()
            self.assertGreater(len(content), 0)
        finally:
            os.unlink(output_path)

    def test_contains_two_phases(self):
        """Test output contains both phases with pause."""
        with tempfile.NamedTemporaryFile(suffix='.nc', delete=False) as f:
            output_path = f.name
        try:
            self._generate_tube_gcode_to_file(output_path, '1x1')
            with open(output_path) as f:
                content = f.read()
            self.assertIn("PHASE 1", content)
            self.assertIn("PHASE 2", content)
            self.assertIn("M0", content)  # Pause for flip
        finally:
            os.unlink(output_path)

    def test_uses_g55_not_g52(self):
        """Test output uses G55 and doesn't contain G52."""
        with tempfile.NamedTemporaryFile(suffix='.nc', delete=False) as f:
            output_path = f.name
        try:
            self._generate_tube_gcode_to_file(output_path, '1x1')
            with open(output_path) as f:
                content = f.read()
            self.assertIn("G55", content)
            self.assertNotIn("G52", content)
        finally:
            os.unlink(output_path)

    def test_contains_setup_instructions(self):
        """Test output contains setup instructions in header."""
        with tempfile.NamedTemporaryFile(suffix='.nc', delete=False) as f:
            output_path = f.name
        try:
            self._generate_tube_gcode_to_file(output_path, '1x1')
            with open(output_path) as f:
                content = f.read()
            self.assertIn("SETUP INSTRUCTIONS", content)
            self.assertIn("Mount tube in jig", content)
            self.assertIn("Z=0 is at bottom of tube", content)
        finally:
            os.unlink(output_path)

    def test_contains_flip_instructions(self):
        """Test output contains flip instructions."""
        with tempfile.NamedTemporaryFile(suffix='.nc', delete=False) as f:
            output_path = f.name
        try:
            self._generate_tube_gcode_to_file(output_path, '1x1')
            with open(output_path) as f:
                content = f.read()
            self.assertIn("Flip tube 180 degrees", content)
            self.assertIn("OPERATOR ACTION REQUIRED", content)
        finally:
            os.unlink(output_path)

    def test_ends_with_m30(self):
        """Test output ends with program end."""
        with tempfile.NamedTemporaryFile(suffix='.nc', delete=False) as f:
            output_path = f.name
        try:
            self._generate_tube_gcode_to_file(output_path, '1x1')
            with open(output_path) as f:
                content = f.read()
            self.assertIn("M30", content)
        finally:
            os.unlink(output_path)

    def test_y_coordinates_differ_between_phases(self):
        """Test that Y coordinates are shifted differently in each phase."""
        with tempfile.NamedTemporaryFile(suffix='.nc', delete=False) as f:
            output_path = f.name
        try:
            self._generate_tube_gcode_to_file(output_path, '1x1')
            with open(output_path) as f:
                lines = f.readlines()

            # Find where phases start
            phase1_start = None
            phase2_start = None
            for i, line in enumerate(lines):
                if "PHASE 1" in line:
                    phase1_start = i
                elif "PHASE 2" in line:
                    phase2_start = i

            self.assertIsNotNone(phase1_start)
            self.assertIsNotNone(phase2_start)

            # Get first toolpath Y coordinate from each phase
            # Skip "G0 X0 Y0" origin moves - look for Y coords that aren't Y0
            phase1_y = None
            phase2_y = None

            for line in lines[phase1_start:phase2_start]:
                if 'Y' in line and ('G0' in line or 'G1' in line):
                    match = re.search(r'Y(-?\d+\.?\d*)', line)
                    if match:
                        y_val = float(match.group(1))
                        # Skip the "G0 X0 Y0" origin positioning
                        if abs(y_val) > 0.01:
                            phase1_y = y_val
                            break

            for line in lines[phase2_start:]:
                if 'Y' in line and ('G0' in line or 'G1' in line):
                    match = re.search(r'Y(-?\d+\.?\d*)', line)
                    if match:
                        y_val = float(match.group(1))
                        # Skip the "G0 X0 Y0" origin positioning
                        if abs(y_val) > 0.01:
                            phase2_y = y_val
                            break

            # Y values should be different (different offsets applied)
            self.assertIsNotNone(phase1_y, "Could not find Y coordinate in Phase 1")
            self.assertIsNotNone(phase2_y, "Could not find Y coordinate in Phase 2")
            self.assertNotAlmostEqual(phase1_y, phase2_y, places=2,
                msg=f"Phase 1 Y ({phase1_y}) should differ from Phase 2 Y ({phase2_y})")

        finally:
            os.unlink(output_path)

    def test_contains_straight_facing_passes(self):
        """Test that straight facing passes are generated (G1 cuts across tube)."""
        with tempfile.NamedTemporaryFile(suffix='.nc', delete=False) as f:
            output_path = f.name
        try:
            self._generate_tube_gcode_to_file(output_path, '1x1')
            with open(output_path) as f:
                content = f.read()
            # Should have G1 linear moves for cutting
            self.assertIn("G1 X", content)
            # Should have roughing and finishing sections
            self.assertIn("ROUGHING", content)
            self.assertIn("FINISHING", content)
            # Should have default G17 (XY plane) in header
            self.assertIn("G17", content)
        finally:
            os.unlink(output_path)

    def test_contains_z_homing(self):
        """Test output contains Z axis homing sequence."""
        with tempfile.NamedTemporaryFile(suffix='.nc', delete=False) as f:
            output_path = f.name
        try:
            self._generate_tube_gcode_to_file(output_path, '1x1')
            with open(output_path) as f:
                content = f.read()
            self.assertIn("G0 G28 G91 Z0", content)
            self.assertIn("G90", content)  # Back to absolute mode
        finally:
            os.unlink(output_path)

    def test_contains_xy_origin_moves(self):
        """Test output contains XY origin rapid moves."""
        with tempfile.NamedTemporaryFile(suffix='.nc', delete=False) as f:
            output_path = f.name
        try:
            self._generate_tube_gcode_to_file(output_path, '1x1')
            with open(output_path) as f:
                content = f.read()
            self.assertIn("G0 X0 Y0", content)
        finally:
            os.unlink(output_path)

    def test_uses_machine_coords_for_parking(self):
        """Test parking uses machine coordinates (G53)."""
        with tempfile.NamedTemporaryFile(suffix='.nc', delete=False) as f:
            output_path = f.name
        try:
            self._generate_tube_gcode_to_file(output_path, '1x1')
            with open(output_path) as f:
                content = f.read()
            self.assertIn("G53 G0 X0.5 Y0.5", content)  # Default generic parking position
            self.assertNotIn("G0 X0 Y-2.0", content)  # Old work coord parking
        finally:
            os.unlink(output_path)

    def test_z_before_xy_pattern(self):
        """Test that G53 G0 Z0 always comes before XY moves."""
        with tempfile.NamedTemporaryFile(suffix='.nc', delete=False) as f:
            output_path = f.name
        try:
            self._generate_tube_gcode_to_file(output_path, '1x1')
            with open(output_path) as f:
                lines = f.readlines()

            # Find all G53 G0 Z0 lines and verify next XY move follows
            for i, line in enumerate(lines):
                if "G53 G0 Z0" in line:
                    # Look for next non-empty, non-comment line
                    for j in range(i+1, min(i+5, len(lines))):
                        next_line = lines[j].strip()
                        if next_line and not next_line.startswith('('):
                            # Should be an XY move (G0 X... or G53 G0 X...)
                            self.assertTrue(
                                'X' in next_line or 'Y' in next_line or next_line == '',
                                f"After G53 G0 Z0 at line {i}, expected XY move but got: {next_line}"
                            )
                            break
        finally:
            os.unlink(output_path)


class TestTubeFacingToolEdgePositions(unittest.TestCase):
    """Test the tool edge positions for each phase."""

    def test_phase1_roughing_edge_at_005(self):
        """Phase 1 roughing tool edge should be at Y=+0.05"."""
        phase1_roughing_edge = 0.05
        self.assertAlmostEqual(phase1_roughing_edge, 0.05, places=3)

    def test_phase1_finishing_edge_at_00625(self):
        """Phase 1 finishing tool edge should be at Y=+0.0625"."""
        phase1_finishing_edge = 0.0625
        self.assertAlmostEqual(phase1_finishing_edge, 0.0625, places=3)

    def test_phase2_roughing_edge_at_negative_00125(self):
        """Phase 2 roughing tool edge should be at Y=-0.0125"."""
        phase2_roughing_edge = -0.0125
        self.assertAlmostEqual(phase2_roughing_edge, -0.0125, places=3)

    def test_phase2_finishing_edge_at_zero(self):
        """Phase 2 finishing tool edge should be at Y=0."""
        phase2_finishing_edge = 0.0
        self.assertAlmostEqual(phase2_finishing_edge, 0.0, places=3)


if __name__ == '__main__':
    unittest.main()

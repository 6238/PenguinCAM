"""
Shared G-code utilities for testing.
These functions are used by both unit tests and system tests.
"""

from pygcode import Line, Machine, GCodeLinearMove, GCodeRapidMove, GCodeArcMoveCW, GCodeArcMoveCCW


def load_gcode_file(gcode_file_path):
    """Load G-code file and return list of Line objects"""
    lines = []
    with open(gcode_file_path, "r") as f:
        for line_num, line_text in enumerate(f.readlines(), 1):
            try:
                line = Line(line_text)
                lines.append(line)
            except Exception as e:
                if line_text.strip() and not line_text.strip().startswith(';') and not line_text.strip().startswith('('):
                    print(f"Warning: Could not parse line {line_num}: {line_text.strip()}")
                    print(f"  Error: {e}")
                continue
    return lines


def get_machine_state(gcode_lines):
    """Process G-code through pygcode Machine to extract final state"""
    machine = Machine()

    for line in gcode_lines:
        if line.block and line.block.gcodes:
            machine.process_gcodes(*line.block.gcodes)

    return machine


def get_feedrates(gcode_lines):
    """Extract all unique feedrates from G-code"""
    feedrates = []

    for line in gcode_lines:
        if line.block and line.block.words:
            for word in line.block.words:
                if word.letter == "F":
                    feedrates.append(word.value)
                    break

    return feedrates


def get_gcode_boundary(gcode_lines):
    """Extract the bounding box (min/max X, Y, Z) from G-code"""
    machine = Machine()

    # Track current position
    current_pos = {'X': 0.0, 'Y': 0.0, 'Z': 0.0}

    # Track min/max for each axis
    bounds = {
        'X': {'min': None, 'max': None},
        'Y': {'min': None, 'max': None},
        'Z': {'min': None, 'max': None}
    }

    for line in gcode_lines:
        if line.block and line.block.gcodes:
            machine.process_gcodes(*line.block.gcodes)

            for gcode in line.block.gcodes:
                # Track linear and arc moves
                if isinstance(gcode, (GCodeLinearMove, GCodeArcMoveCW, GCodeArcMoveCCW)):
                    if line.block.words:
                        # Extract position updates
                        for word in line.block.words:
                            if word.letter in ['X', 'Y', 'Z']:
                                new_val = word.value
                                current_pos[word.letter] = new_val

                                # Update bounds
                                if bounds[word.letter]['min'] is None or new_val < bounds[word.letter]['min']:
                                    bounds[word.letter]['min'] = new_val
                                if bounds[word.letter]['max'] is None or new_val > bounds[word.letter]['max']:
                                    bounds[word.letter]['max'] = new_val

    return bounds


def get_safe_heights(gcode_lines):
    """Extract clearance and retract heights from G-code"""
    machine = Machine()

    current_z = 0.0
    retract_heights = []  # Upward Z moves (retracts)
    clearance_heights = []  # Maximum Z positions reached
    plunge_starts = []  # Z position before plunging

    for line in gcode_lines:
        if line.block and line.block.gcodes:
            machine.process_gcodes(*line.block.gcodes)

            for gcode in line.block.gcodes:
                # Track moves
                if isinstance(gcode, (GCodeLinearMove, GCodeRapidMove)):
                    if line.block.words:
                        has_z = any(w.letter == 'Z' for w in line.block.words)
                        has_xy = any(w.letter in ['X', 'Y'] for w in line.block.words)

                        if has_z:
                            # Get new Z value
                            new_z = current_z
                            for w in line.block.words:
                                if w.letter == 'Z':
                                    new_z = w.value
                                    break

                            # Detect retracts (upward Z moves without XY)
                            if new_z > current_z and not has_xy:
                                retract_heights.append(new_z)
                                clearance_heights.append(new_z)

                            # Detect plunge start positions (Z before downward move)
                            elif new_z < current_z and not has_xy:
                                plunge_starts.append(current_z)

                            current_z = new_z

    return {
        'retract_heights': sorted(set(retract_heights)),
        'max_clearance': max(clearance_heights) if clearance_heights else None,
        'plunge_start_heights': sorted(set(plunge_starts))
    }

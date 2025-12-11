from pygcode import *
from termcolor import colored
from frc_cam_postprocessor import FRCPostProcessor
import sys
import tempfile

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


PASS = colored("PASS", "green")
FAIL = colored("FAIL", "red")


def verify_cam_settings(onshape_lines, fusion_lines):
    print("Test: Verify CAM Settings")

    # Create machine instances and process G-code
    onshape_machine = get_machine_state(onshape_lines)
    fusion_machine = get_machine_state(fusion_lines)

    # Check units (G20 = inches, G21 = millimeters)
    onshape_units = onshape_machine.mode.modal_groups[GCodeUseInches.modal_group]
    fusion_units = fusion_machine.mode.modal_groups[GCodeUseInches.modal_group]
    units_match = onshape_units == fusion_units
    print(f"\tG-code UNITS ---- {PASS if units_match else FAIL}")
    if not units_match:
        print(f"\t\tOnshape: {onshape_units}, Fusion: {fusion_units}")

    # Check spindle speed
    onshape_spindle = onshape_machine.mode.modal_groups[GCodeSpindleSpeed.modal_group]
    fusion_spindle = fusion_machine.mode.modal_groups[GCodeSpindleSpeed.modal_group]
    spindle_match = onshape_spindle == fusion_spindle
    print(f"\tG-code Spindle Speed ---- {PASS if spindle_match else FAIL}")
    if not spindle_match:
        print(f"\t\tOnshape: {onshape_spindle} RPM, Fusion: {fusion_spindle} RPM")

    # Check coordinate plane (G17 = XY, G18 = XZ, G19 = YZ)
    onshape_plane = onshape_machine.mode.modal_groups[GCodePlaneSelect]
    fusion_plane = fusion_machine.mode.modal_groups[GCodePlaneSelect]
    plane_match = onshape_plane == fusion_plane
    print(f"\tG-code Coordinate Plane ---- {PASS if plane_match else FAIL}")
    if not plane_match:
        print(f"\t\tOnshape: {onshape_plane}, Fusion: {fusion_plane}")

    # Check positioning system (G90 = absolute, G91 = incremental)
    onshape_distance = onshape_machine.mode.modal_groups[GCodeDistanceMode]
    fusion_distance = fusion_machine.mode.modal_groups[GCodeDistanceMode]
    distance_match = onshape_distance == fusion_distance
    print(f"\tG-code Distance Mode ---- {PASS if distance_match else FAIL}")
    if not distance_match:
        print(f"\t\tOnshape: {onshape_distance}, Fusion: {fusion_distance}")

    return units_match and spindle_match and plane_match and distance_match


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


def verify_feedrates(onshape_lines, fusion_lines, debug=False):
    print("\nTest: Verify Feedrates")
    
    onshape_feedrates = get_feedrates(onshape_lines)
    fusion_feedrates = get_feedrates(fusion_lines)
    
    all_passed = True
    
    # Get min (plunge) and max (cutting) feedrates
    onshape_plunge = min(onshape_feedrates) if onshape_feedrates else None
    onshape_cutting = max(onshape_feedrates) if onshape_feedrates else None
    
    fusion_plunge = min(fusion_feedrates) if fusion_feedrates else None
    fusion_cutting = max(fusion_feedrates) if fusion_feedrates else None
    
    # Compare plunge feedrates
    plunge_match = onshape_plunge == fusion_plunge
    print(f"\tPlunge Feedrate Match ---- {PASS if plunge_match else FAIL}")
    if not plunge_match:
        print(f"\t\tOnshape plunge: {onshape_plunge/25.4:.2f}")
        print(f"\t\tFusion plunge: {fusion_plunge/25.4:.2f}")
        all_passed = False
    else:
        print(f"\t\tPlunge feedrate: {onshape_plunge/25.4:.2f}")
    
    # Compare cutting feedrates
    cutting_match = onshape_cutting == fusion_cutting
    print(f"\tCutting Feedrate Match ---- {PASS if cutting_match else FAIL}")
    if not cutting_match:
        print(f"\t\tOnshape cutting: {onshape_cutting/25.4:.2f}")
        print(f"\t\tFusion cutting: {fusion_cutting/25.4:.2f}")
        all_passed = False
    else:
        print(f"\t\tCutting feedrate: {onshape_cutting/25.4:.2f}")
    
    return all_passed

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


def verify_boundary(onshape_lines, fusion_lines, tolerance=0.1):
    print("\nTest: Verify Toolpath Boundary")
    
    onshape_bounds = get_gcode_boundary(onshape_lines)
    fusion_bounds = get_gcode_boundary(fusion_lines)
    
    all_passed = True
    
    for axis in ['X', 'Y', 'Z']:
        onshape_min = onshape_bounds[axis]['min']
        onshape_max = onshape_bounds[axis]['max']
        fusion_min = fusion_bounds[axis]['min']
        fusion_max = fusion_bounds[axis]['max']
        
        # Check if bounds exist
        if onshape_min is None or fusion_min is None:
            print(f"\t{axis}-axis Boundary ---- {FAIL} (missing data)")
            all_passed = False
            continue
        
        # Compare with tolerance
        min_match = abs(onshape_min - fusion_min) <= tolerance
        max_match = abs(onshape_max - fusion_max) <= tolerance
        axis_match = min_match and max_match
        
        print(f"\t{axis}-axis Boundary ---- {PASS if axis_match else FAIL}")
        
        if not axis_match:
            print(f"\t\tOnshape: [{onshape_min:.4f}, {onshape_max:.4f}]")
            print(f"\t\tFusion:  [{fusion_min:.4f}, {fusion_max:.4f}]")
            if not min_match:
                print(f"\t\tMin difference: {abs(onshape_min - fusion_min):.4f}")
            if not max_match:
                print(f"\t\tMax difference: {abs(onshape_max - fusion_max):.4f}")
            all_passed = False
        else:
            print(f"\t\tRange: [{onshape_min:.4f}, {onshape_max:.4f}]")
    
    # Calculate and display work envelope size
    onshape_size = {
        'X': onshape_bounds['X']['max'] - onshape_bounds['X']['min'] if onshape_bounds['X']['min'] is not None else 0,
        'Y': onshape_bounds['Y']['max'] - onshape_bounds['Y']['min'] if onshape_bounds['Y']['min'] is not None else 0,
        'Z': onshape_bounds['Z']['max'] - onshape_bounds['Z']['min'] if onshape_bounds['Z']['min'] is not None else 0
    }
    
    print(f"\n\tWork Envelope Size:")
    print(f"\t\tX: {onshape_size['X']:.4f}")
    print(f"\t\tY: {onshape_size['Y']:.4f}")
    print(f"\t\tZ: {onshape_size['Z']:.4f}")
    
    return all_passed


def verify_safe_heights(onshape_lines, fusion_lines, tolerance=0.001):
    print("\nTest: Verify Safe Heights")
    
    onshape_heights = get_safe_heights(onshape_lines)
    fusion_heights = get_safe_heights(fusion_lines)
    
    all_passed = True
    
    # Compare maximum clearance height
    # onshape_max = onshape_heights['max_clearance']
    # fusion_max = fusion_heights['max_clearance']
    
    # if onshape_max is not None and fusion_max is not None:
    #     clearance_match = abs(onshape_max - fusion_max) <= tolerance
    #     print(f"\tMax Clearance Height ---- {PASS if clearance_match else FAIL}")
        
    #     if not clearance_match:
    #         print(f"\t\tOnshape: {onshape_max:.4f}")
    #         print(f"\t\tFusion:  {fusion_max:.4f}")
    #         print(f"\t\tDifference: {abs(onshape_max - fusion_max):.4f}")
    #         all_passed = False
    #     else:
    #         print(f"\t\tClearance: {onshape_max:.4f}")
    
    # Compare retract heights (should use same set)
    onshape_retracts = set(onshape_heights['retract_heights'])
    fusion_retracts = set(fusion_heights['retract_heights'])
    
    # Check if retract height sets match within tolerance
    retracts_match = True
    for oh in onshape_retracts:
        if not any(abs(oh - fh) <= tolerance for fh in fusion_retracts):
            retracts_match = False
            break
    
    print(f"\tRetract Heights Match ---- {PASS if retracts_match else FAIL}")
    
    if not retracts_match:
        print(f"\t\tOnshape retracts: {sorted(onshape_heights['retract_heights'])}")
        print(f"\t\tFusion retracts:  {sorted(fusion_heights['retract_heights'])}")
        all_passed = False
    else:
        print(f"\t\tRetract height(s): {sorted(onshape_heights['retract_heights'])}")
    
    # Compare plunge start heights
    onshape_plunge_starts = set(onshape_heights['plunge_start_heights'])
    fusion_plunge_starts = set(fusion_heights['plunge_start_heights'])
    
    plunge_starts_match = True
    for oh in onshape_plunge_starts:
        if not any(abs(oh - fh) <= tolerance for fh in fusion_plunge_starts):
            plunge_starts_match = False
            break
    
    print(f"\tPlunge Start Heights Match ---- {PASS if plunge_starts_match else FAIL}")
    
    if not plunge_starts_match:
        print(f"\t\tOnshape: {sorted(onshape_heights['plunge_start_heights'])}")
        print(f"\t\tFusion:  {sorted(fusion_heights['plunge_start_heights'])}")
        all_passed = False
    else:
        print(f"\t\tPlunge start(s): {sorted(onshape_heights['plunge_start_heights'])}")
    
    return all_passed


def generate_gcode_from_dxf(dxf_path, material_thickness=0.25, tool_diameter=0.157, 
                            sacrifice_depth=0.02, units='inch', tabs=4, drill_screws=False):
    """Generate G-code from DXF using PenguinCAM"""
    
    # Create post-processor with specified parameters
    pp = FRCPostProcessor(material_thickness=material_thickness, 
                          tool_diameter=tool_diameter,
                          units=units)
    pp.num_tabs = tabs
    pp.drill_screw_holes = drill_screws
    pp.sacrifice_board_depth = sacrifice_depth
    
    # Recalculate Z positions with user-specified sacrifice depth
    pp.cut_depth = -pp.sacrifice_board_depth
    
    # Process DXF file
    pp.load_dxf(dxf_path)
    pp.classify_holes()
    pp.identify_perimeter_and_pockets()
    
    # Generate to temporary file
    temp_gcode = tempfile.NamedTemporaryFile(mode='w', suffix='.gcode', delete=False)
    temp_gcode_path = temp_gcode.name
    temp_gcode.close()
    
    pp.generate_gcode(temp_gcode_path)
    
    return temp_gcode_path

# Update main function
if __name__ == "__main__":
    INPUT_DXF = "./test_part.dxf"
    FUSION_REFERENCE = "./fusion_output.gcode"
    
    # CAM parameters
    MATERIAL_THICKNESS = 6.35
    TOOL_DIAMETER = 4
    SACRIFICE_DEPTH = 0.5
    UNITS = 'mm'
    TABS = 4
    DRILL_SCREWS = True
    
    # Output options
    KEEP_GCODE = False
    OUTPUT_GCODE_PATH = "./penguin_cam_output.gcode"
    
    penguin_gcode_path = generate_gcode_from_dxf(
        INPUT_DXF,
        material_thickness=MATERIAL_THICKNESS,
        tool_diameter=TOOL_DIAMETER,
        sacrifice_depth=SACRIFICE_DEPTH,
        units=UNITS,
        tabs=TABS,
        drill_screws=DRILL_SCREWS
    )
    
    penguin_lines = load_gcode_file(penguin_gcode_path)
    fusion_lines = load_gcode_file(FUSION_REFERENCE)

    settings_passed = verify_cam_settings(penguin_lines, fusion_lines)
    feedrates_passed = verify_feedrates(penguin_lines, fusion_lines, debug=False)
    # safe_heights_passed = verify_safe_heights(penguin_lines, fusion_lines)
    
    all_passed = settings_passed and feedrates_passed # and safe_heights_passed
    print(f"\nOverall: {PASS if all_passed else FAIL}")

    
    # Exit with appropriate code for CI/CD
    if not all_passed:
        sys.exit(1)  # Non-zero exit code indicates failure
    else:
        sys.exit(0)  # Zero exit code indicates success

from pygcode import *
from termcolor import colored
from frc_cam_postprocessor import FRCPostProcessor
import argparse
import io
import sys
import tempfile
from contextlib import redirect_stdout

# Import shared utilities
from tests.gcode_utils import (
    load_gcode_file,
    get_machine_state,
    get_feedrates,
    get_gcode_boundary,
    get_safe_heights,
)

# Global quiet mode flag
QUIET_MODE = False


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


def verify_feedrates(onshape_lines, fusion_lines, debug=False):
    print("\nTest: Verify Feedrates")

    onshape_feedrates = get_feedrates(onshape_lines)
    fusion_feedrates = get_feedrates(fusion_lines)

    all_passed = True

    # Get min (plunge) and max (cutting) feedrates
    # Note: traverse_rate (200 IPM = 5080 mm/min) is for non-cutting moves above material
    # so we exclude it when determining the cutting feedrate
    TRAVERSE_RATE_MM = 5080  # 200 IPM in mm/min
    onshape_cutting_rates = [f for f in onshape_feedrates if f != TRAVERSE_RATE_MM]

    onshape_plunge = min(onshape_feedrates) if onshape_feedrates else None
    onshape_cutting = max(onshape_cutting_rates) if onshape_cutting_rates else None

    fusion_plunge = min(fusion_feedrates) if fusion_feedrates else None
    fusion_cutting = max(fusion_feedrates) if fusion_feedrates else None

    # Verify plunge feedrate is reasonable (we use 35 IPM for plywood, not Fusion's 20 IPM)
    EXPECTED_PLUNGE_MM = 889  # 35 IPM in mm/min for plywood

    if onshape_plunge is None:
        print(f"\tPlunge Feedrate Match ---- {FAIL}")
        print(f"\t\tError: No feedrates found in generated G-code")
        all_passed = False
    else:
        plunge_match = onshape_plunge == EXPECTED_PLUNGE_MM
        print(f"\tPlunge Feedrate Match ---- {PASS if plunge_match else FAIL}")
        if not plunge_match:
            print(f"\t\tOnshape plunge: {onshape_plunge/25.4:.2f}")
            print(f"\t\tExpected plunge: {EXPECTED_PLUNGE_MM/25.4:.2f}")
            all_passed = False
        else:
            print(f"\t\tPlunge feedrate: {onshape_plunge/25.4:.2f}")
    
    # Compare cutting feedrates
    if onshape_cutting is None or fusion_cutting is None:
        print(f"\tCutting Feedrate Match ---- {FAIL}")
        print(f"\t\tError: Missing cutting feedrate data")
        all_passed = False
    else:
        cutting_match = onshape_cutting == fusion_cutting
        print(f"\tCutting Feedrate Match ---- {PASS if cutting_match else FAIL}")
        if not cutting_match:
            print(f"\t\tOnshape cutting: {onshape_cutting/25.4:.2f}")
            print(f"\t\tFusion cutting: {fusion_cutting/25.4:.2f}")
            all_passed = False
        else:
            print(f"\t\tCutting feedrate: {onshape_cutting/25.4:.2f}")
    
    return all_passed


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
                            sacrifice_depth=0.02, units='inch', tabs=4,
                            material='plywood'):
    """Generate G-code from DXF using PenguinCAM"""

    # Suppress stdout in quiet mode
    output = io.StringIO() if QUIET_MODE else sys.stdout

    with redirect_stdout(output):
        # Create post-processor with specified parameters
        pp = FRCPostProcessor(material_thickness=material_thickness,
                              tool_diameter=tool_diameter,
                              units=units)

        # Apply material preset (sets feeds, speeds, ramp angles) - matches GUI behavior
        pp.apply_material_preset(material)

        pp.num_tabs = tabs
        pp.sacrifice_board_depth = sacrifice_depth

        # Recalculate Z positions with user-specified sacrifice depth
        pp.cut_depth = -pp.sacrifice_board_depth

        # Process DXF file
        pp.load_dxf(dxf_path)
        pp.classify_holes()
        pp.identify_perimeter_and_pockets()

        # Generate G-code using new API
        result = pp.generate_gcode()

        if not result.success:
            raise RuntimeError(f"G-code generation failed: {', '.join(result.errors)}")

        # Write to temporary file
        temp_gcode = tempfile.NamedTemporaryFile(mode='w', suffix='.gcode', delete=False)
        temp_gcode_path = temp_gcode.name
        temp_gcode.write(result.gcode)
        temp_gcode.close()

    return temp_gcode_path

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Run G-code comparison tests')
    parser.add_argument('--quiet', '-q', action='store_true',
                        help='Suppress verbose output, show only summary')
    args = parser.parse_args()

    QUIET_MODE = args.quiet

    INPUT_DXF = "./test_part.dxf"
    FUSION_REFERENCE = "./fusion_output.gcode"

    # CAM parameters
    MATERIAL = 'plywood'
    MATERIAL_THICKNESS = 6.35
    TOOL_DIAMETER = 4
    SACRIFICE_DEPTH = 0.5
    UNITS = 'mm'
    TABS = 4

    penguin_gcode_path = generate_gcode_from_dxf(
        INPUT_DXF,
        material_thickness=MATERIAL_THICKNESS,
        tool_diameter=TOOL_DIAMETER,
        sacrifice_depth=SACRIFICE_DEPTH,
        units=UNITS,
        tabs=TABS,
        material=MATERIAL
    )

    penguin_lines = load_gcode_file(penguin_gcode_path)
    fusion_lines = load_gcode_file(FUSION_REFERENCE)

    # Run tests, suppressing detailed output in quiet mode
    if QUIET_MODE:
        output = io.StringIO()
        with redirect_stdout(output):
            settings_passed = verify_cam_settings(penguin_lines, fusion_lines)
            feedrates_passed = verify_feedrates(penguin_lines, fusion_lines, debug=False)
        all_passed = settings_passed and feedrates_passed
        print(f"CAM settings: {PASS if settings_passed else FAIL}")
        print(f"Feedrates: {PASS if feedrates_passed else FAIL}")
    else:
        settings_passed = verify_cam_settings(penguin_lines, fusion_lines)
        feedrates_passed = verify_feedrates(penguin_lines, fusion_lines, debug=False)
        all_passed = settings_passed and feedrates_passed

    print(f"Overall: {PASS if all_passed else FAIL}")

    sys.exit(0 if all_passed else 1)

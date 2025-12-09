#!/usr/bin/env python3
"""
FRC CAM Post-Processor - SAFE TEST VERSION
This version adds extra safety features for testing
"""

import sys
from pathlib import Path

# Import the main post-processor
sys.path.insert(0, str(Path(__file__).parent))
from frc_cam_postprocessor import FRCPostProcessor
import argparse


class SafeTestPostProcessor(FRCPostProcessor):
    """Extended version with safety features for testing"""
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.safety_height_offset = 2.0  # Raise tool 2" above normal
        self.disable_spindle = True      # Force spindle off
    
    def generate_gcode(self, output_file: str):
        """Generate G-code with safety overrides"""
        gcode = []
        
        # SAFETY HEADER
        gcode.append("(===========================================)")
        gcode.append("(       ⚠️  SAFE TEST MODE ACTIVE  ⚠️       )")
        gcode.append("(===========================================)")
        gcode.append("(This is a DRY RUN version for testing)")
        gcode.append(f"(Tool raised {self.safety_height_offset}\" above normal)")
        gcode.append("(Spindle DISABLED)")
        gcode.append("(===========================================)")
        gcode.append("")
        
        # Call parent to generate normal gcode
        super().generate_gcode(output_file)
        
        # Read the generated file
        with open(output_file, 'r') as f:
            original_gcode = f.readlines()
        
        # Modify for safety
        gcode = []
        for line in original_gcode:
            # Disable all spindle commands
            if line.strip().startswith('M3') or line.strip().startswith('M03'):
                gcode.append("M5  ; SPINDLE DISABLED FOR TEST")
                continue
            
            # Raise all Z heights by safety offset
            if 'Z' in line and not line.strip().startswith('('):
                # Parse and modify Z values
                parts = line.split(';')
                code_part = parts[0]
                comment = ';'.join(parts[1:]) if len(parts) > 1 else ''
                
                if 'Z' in code_part:
                    # Simple Z value extraction and modification
                    import re
                    z_match = re.search(r'Z(-?\d+\.?\d*)', code_part)
                    if z_match:
                        original_z = float(z_match.group(1))
                        safe_z = original_z + self.safety_height_offset
                        code_part = re.sub(r'Z-?\d+\.?\d*', f'Z{safe_z:.4f}', code_part)
                        line = code_part
                        if comment:
                            line += ' ; ' + comment
                        line += f' [SAFE: original Z={original_z:.4f}]'
            
            gcode.append(line)
        
        # Write modified gcode
        with open(output_file, 'w') as f:
            f.writelines(gcode)
        
        # Generate report
        report_file = output_file.replace('.gcode', '_SAFETY_REPORT.txt')
        self._generate_safety_report(report_file)
        
        print(f"\n{'='*50}")
        print("⚠️  SAFE TEST MODE - G-CODE MODIFIED")
        print(f"{'='*50}")
        print(f"✓ Spindle disabled")
        print(f"✓ All Z heights raised by {self.safety_height_offset}\"")
        print(f"✓ Safety report: {report_file}")
        print(f"\nG-code written to: {output_file}")
        print(f"\n⚠️  This is for DRY RUN testing only!")
        print(f"⚠️  Do NOT use this G-code for actual cutting!")
        print(f"{'='*50}\n")
    
    def _generate_safety_report(self, report_file: str):
        """Generate a safety checklist report"""
        report = []
        report.append("="*60)
        report.append("FRC CAM POST-PROCESSOR - SAFETY TEST REPORT")
        report.append("="*60)
        report.append("")
        report.append("⚠️  THIS IS A DRY RUN VERSION - FOR TESTING ONLY")
        report.append("")
        report.append("MODIFICATIONS APPLIED:")
        report.append(f"  • All Z heights raised by {self.safety_height_offset}\"")
        report.append("  • Spindle commands disabled (M3 → M5)")
        report.append("  • All other G-code preserved for motion testing")
        report.append("")
        report.append("="*60)
        report.append("PRE-RUN SAFETY CHECKLIST")
        report.append("="*60)
        report.append("")
        report.append("BEFORE RUNNING THIS TEST CODE:")
        report.append("")
        report.append("[ ] 1. Spindle is manually turned OFF and locked out")
        report.append("[ ] 2. Tool is raised well above material surface")
        report.append("[ ] 3. Emergency stop button is within reach")
        report.append("[ ] 4. Machine is in dry run or single-step mode (if available)")
        report.append("[ ] 5. Feed rate override set to 25-50%")
        report.append("[ ] 6. No material is loaded (or use foam for test)")
        report.append("[ ] 7. All clamps and fixtures are clear of tool path")
        report.append("[ ] 8. Machine workspace limits are sufficient for this job")
        report.append("[ ] 9. You have reviewed the G-code in a visualizer")
        report.append("[ ] 10. You understand this is NOT for actual cutting")
        report.append("")
        report.append("="*60)
        report.append("WHAT TO WATCH FOR DURING DRY RUN:")
        report.append("="*60)
        report.append("")
        report.append("✓ Smooth motion without unexpected rapids")
        report.append("✓ Tool stays within machine limits")
        report.append("✓ No collisions with clamps or fixtures")
        report.append("✓ Tab locations look reasonable")
        report.append("✓ Hole locations match your part")
        report.append("✓ Motion sequence makes sense")
        report.append("")
        report.append("❌ STOP IMMEDIATELY IF:")
        report.append("  • Tool moves outside expected area")
        report.append("  • Unexpected rapid motions occur")
        report.append("  • Machine makes unusual sounds")
        report.append("  • Any axis moves unexpectedly")
        report.append("")
        report.append("="*60)
        report.append("PART STATISTICS")
        report.append("="*60)
        report.append("")
        report.append(f"Material thickness: {self.material_thickness}\"")
        report.append(f"Screw holes: {len(self.screw_holes)}")
        report.append(f"Bearing holes: {len(self.bearing_holes)}")
        report.append(f"Pockets: {len(self.pockets)}")
        report.append(f"Perimeter: {'Yes' if self.perimeter else 'No'}")
        report.append(f"Tabs: {self.num_tabs if self.perimeter else 0}")
        report.append("")
        report.append("="*60)
        report.append("NEXT STEPS AFTER SUCCESSFUL DRY RUN")
        report.append("="*60)
        report.append("")
        report.append("1. If dry run looks good, run the NORMAL version:")
        report.append("   python frc_cam_postprocessor.py input.dxf output.gcode")
        report.append("")
        report.append("2. Test on scrap/foam material first")
        report.append("")
        report.append("3. Verify feed rates are appropriate for your material")
        report.append("")
        report.append("4. Start with reduced speed override (50%)")
        report.append("")
        report.append("5. Monitor the first cut closely")
        report.append("")
        report.append("="*60)
        
        with open(report_file, 'w') as f:
            f.write('\n'.join(report))


def main():
    parser = argparse.ArgumentParser(
        description='FRC Robotics CAM Post-Processor - SAFE TEST MODE',
        epilog='This version disables spindle and raises tool for safe testing'
    )
    parser.add_argument('input_dxf', help='Input DXF file from OnShape')
    parser.add_argument('output_gcode', help='Output G-code file (TEST VERSION)')
    parser.add_argument('--thickness', type=float, default=0.25, 
                       help='Material thickness in inches (default: 0.25)')
    parser.add_argument('--tool-diameter', type=float, default=0.157,
                       help='Tool diameter in inches (default: 0.157" = 4mm)')
    parser.add_argument('--units', choices=['inch', 'mm'], default='inch',
                       help='Units (default: inch)')
    parser.add_argument('--tabs', type=int, default=4,
                       help='Number of tabs on perimeter (default: 4)')
    parser.add_argument('--raise-height', type=float, default=2.0,
                       help='How much to raise tool above normal (inches, default: 2.0)')
    
    args = parser.parse_args()
    
    # Create SAFE test post-processor
    pp = SafeTestPostProcessor(material_thickness=args.thickness,
                               tool_diameter=args.tool_diameter,
                               units=args.units)
    pp.num_tabs = args.tabs
    pp.safety_height_offset = args.raise_height
    
    # Process file
    pp.load_dxf(args.input_dxf)
    pp.classify_holes()
    pp.identify_perimeter_and_pockets()
    pp.generate_gcode(args.output_gcode)
    
    print("\n" + "⚠️ "*20)
    print("Remember: This is a DRY RUN version!")
    print("Review the safety report before running.")
    print("⚠️ "*20 + "\n")


if __name__ == '__main__':
    main()

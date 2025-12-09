# FRC Robotics CAM Post-Processor

A simple, no-fuss CAM post-processor designed specifically for FRC robotics teams. Automatically generates G-code from OnShape DXF exports with all your standard operations built-in.

## Features

✅ **Automatic hole detection:**
- #10 screw holes (0.19" diameter)
- 1.125" bearing holes
- Custom operations for each

✅ **Pocket recognition:**
- Automatically identifies inner boundaries as pockets
- Generates toolpaths for pocket clearing

✅ **Perimeter with tabs:**
- Cuts outer perimeter with holding tabs
- **Smart tab placement** - tabs only on straight sections (avoids curves)
- Configurable tab count, width, and height
- Prevents parts from flying away during cutting

✅ **OnShape DXF compatibility:**
- Handles LINE, ARC, and SPLINE entities
- Automatically connects segments into closed paths
- Works with complex curved geometry

✅ **Tool compensation:**
- Perimeter offset OUTWARD for correct part size
- Pockets offset INWARD  
- Holes compensated for exact diameter

✅ **Pre-configured for FRC:**
- Material thickness: 1/8" to 1/2"
- Works with aluminum, polycarbonate, and wood
- Standard feed rates and plunge rates built-in

## Installation

### 1. Install Python (if you don't have it)
Download Python 3.8 or newer from [python.org](https://www.python.org/downloads/)

### 2. Install dependencies
```bash
pip install -r requirements.txt
```

Or install manually:
```bash
pip install ezdxf shapely
```

## Usage

### Step 1: Export from OnShape
1. Open your part in OnShape
2. Right-click on the face you want to cut
3. Select **Export** → **DXF**
4. Save the file (e.g., `robot_plate.dxf`)

### Step 2: Run the post-processor
```bash
python frc_cam_postprocessor.py robot_plate.dxf output.gcode --thickness 0.25 --tool-diameter 0.157
```

**IMPORTANT:** Always specify your tool diameter! Without it, your parts will be the wrong size.

**MACHINE SETUP:**
- **X/Y Zero:** Clamp your material to the sacrifice board, then set X=0 Y=0 at the **lower-left corner** of your material. This matches the origin point in your CAD file.
- **Z Zero:** Set Z=0 at the **sacrifice board surface** (bottom), not the material top. This ensures consistent setup and guaranteed cut-through with the overcut. See [Z_COORDINATE_SYSTEM.md](Z_COORDINATE_SYSTEM.md) for details.

### Command-line options:
```
python frc_cam_postprocessor.py INPUT.dxf OUTPUT.gcode [OPTIONS]

Required:
  INPUT.dxf           Input DXF file from OnShape
  OUTPUT.gcode        Output G-code file

Options:
  --thickness FLOAT        Material thickness in inches (default: 0.25)
  --tool-diameter FLOAT    Tool diameter in inches (default: 0.157" = 4mm)
  --sacrifice-depth FLOAT  Cut depth into sacrifice board (default: 0.02")
  --units inch/mm          Units to use (default: inch)
  --tabs INT               Number of tabs on perimeter (default: 4)
  --drill-screws           Center drill screw holes instead of milling (faster)
```

**Tool diameter is critical!** Common sizes:
- 4mm = 0.157" (default)
- 1/8" = 0.125"
- 6mm = 0.236"
- 1/4" = 0.250"

**Z-axis setup:** Z=0 is at the **sacrifice board (bottom)**, not the material top.
- Always zero to the sacrifice board surface
- Default overcut: 0.02" into sacrifice board
- Increase to 0.03" for flex materials or if cuts aren't complete

### Examples:

**1/4" aluminum plate with 4mm tool:**
```bash
python frc_cam_postprocessor.py plate.dxf plate.gcode --thickness 0.25 --tool-diameter 0.157
```

**1/8" polycarbonate with 6 tabs and 1/8" tool:**
```bash
python frc_cam_postprocessor.py shield.dxf shield.gcode --thickness 0.125 --tool-diameter 0.125 --tabs 6
```

**1/2" plywood with 1/4" tool and faster drilling:**
```bash
python frc_cam_postprocessor.py base.dxf base.gcode --thickness 0.5 --tool-diameter 0.25 --drill-screws
```

## What It Does

The script automatically:

1. **Loads your DXF file** and extracts all geometry
2. **Classifies circles by diameter:**
   - ~0.19" diameter → Screw hole (drill or mill operation)
   - ~1.125" diameter → Bearing hole (helical bore)
3. **Identifies closed polylines:**
   - Largest boundary → Perimeter (cut with tabs)
   - Smaller inner boundaries → Pockets (full depth cut)
4. **Applies tool compensation:**
   - Perimeter: Offset OUTWARD by tool radius (so final part matches CAD)
   - Pockets: Offset INWARD by tool radius
   - Holes: Reduced radius for proper final size
5. **Generates G-code** with proper:
   - Feed rates and plunge rates
   - Safe heights and rapid moves
   - Spindle control
   - Tab placement on perimeter

### Critical: Tool Compensation

**Your parts will be the wrong size without correct tool diameter!**

The script automatically compensates for tool width:
- If CAD says 6" square, final part will be 6" (not 5.843")
- If CAD says 1.125" hole, final hole will be 1.125" (not 1.282")

See [TOOL_COMPENSATION_GUIDE.md](TOOL_COMPENSATION_GUIDE.md) for complete details.

## Customizing Parameters

Open `frc_cam_postprocessor.py` and modify these values in the `__init__` method:

```python
# Tool settings (also via --tool-diameter)
self.tool_diameter = 0.157   # 4mm end mill
self.tool_radius = tool_diameter / 2

# Feed rates (inches per minute)
self.feed_rate = 30.0        # Cutting feed rate
self.plunge_rate = 10.0      # Plunge/retract rate

# Heights
self.safe_height = 0.1       # Clearance height above material

# Tab settings
self.tab_width = 0.25        # Width of each tab (inches)
self.tab_height = 0.03       # Material left in tab (inches)
self.num_tabs = 4            # Number of tabs (also via --tabs)

# Hole detection tolerance
self.tolerance = 0.02        # +/- tolerance for hole matching

# Screw hole strategy
self.drill_screw_holes = True  # True = center drill, False = mill out
```

## Material-Specific Settings

You might want different settings for different materials:

**Aluminum:**
- Feed rate: 30-40 IPM
- Plunge rate: 8-10 IPM

**Polycarbonate:**
- Feed rate: 40-60 IPM
- Plunge rate: 15-20 IPM

**Wood:**
- Feed rate: 50-80 IPM
- Plunge rate: 20-30 IPM

## Output Format

The generated G-code includes:
- Header comments with file info
- Setup commands (units, positioning)
- Spindle control (M3/M5)
- Organized sections for each operation type
- Safety moves between operations
- Return to origin at end

## Troubleshooting

**"No circles found"**
- Make sure your DXF has actual CIRCLE entities, not arcs
- Check that holes aren't grouped or blocked

**"No polylines found" or "Found 0 closed paths"**
- OnShape exports individual LINE/ARC/SPLINE entities instead of polylines
- The post-processor automatically connects them - see [ONSHAPE_DXF_FIX.md](ONSHAPE_DXF_FIX.md)
- If you see "Created convex hull (APPROXIMATE)", your geometry has gaps
- Check that all perimeter segments connect in your CAD

**Wrong holes detected**
- Adjust `self.tolerance` value if your holes are slightly off-size
- Check that circles are the correct diameter in your CAD

**Tabs in wrong places**
- Tabs are evenly spaced around perimeter
- Adjust `self.num_tabs` to change count

**G-code doesn't run**
- Verify your CNC controller accepts these G-code commands
- You may need to adjust M-codes for your specific machine

## Safety Notes

⚠️ **ALWAYS:**
- Review the generated G-code before running
- Do a test run with the spindle off
- Check that tool paths don't collide with fixtures
- Verify material is properly secured
- Use appropriate safety equipment

⚠️ **This is a basic post-processor:**
- No collision detection
- No tool compensation
- Assumes a single tool for all operations
- Tabs may need adjustment for your specific needs

## Advanced Usage

### Creating tool change operations

If you want to use different tools for different operations, you can modify the code to add tool changes:

```python
# In _generate_bearing_hole_gcode():
gcode.append("M6 T2  ; Change to bearing cutter")
```

### Adding adaptive clearing for pockets

For larger pockets, you might want to add a full adaptive clearing routine. Consider using a library like `opencamlib` or `pycam` for more sophisticated toolpath generation.

### Integration with your workflow

You can create batch scripts to process multiple files:

```bash
#!/bin/bash
for file in *.dxf; do
    python frc_cam_postprocessor.py "$file" "${file%.dxf}.gcode" --thickness 0.25
done
```

## Contributing

This is a simple starting point. Feel free to:
- Add more hole sizes
- Implement better pocket clearing strategies
- Add tool libraries
- Create material presets
- Improve tab placement algorithms

## License

Free to use and modify for your FRC team!

## Questions?

If you run into issues, check:
1. Are the hole diameters exactly right in your CAD?
2. Is the DXF export from OnShape clean (no extra layers)?
3. Does the generated G-code match what your CNC expects?

Good luck with your robot build! 🤖

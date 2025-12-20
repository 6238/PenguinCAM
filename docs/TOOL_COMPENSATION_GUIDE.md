# Tool Compensation Guide

## Why Tool Compensation Matters

When you design a part in CAD, you specify the **final dimensions** you want - like a 6" square plate with #10 screw holes. But when cutting with a CNC router, the **tool has width**. If you simply follow the CAD lines with the center of the tool, your part will be the wrong size!

### The Problem Without Compensation

Let's say you have a 4mm (0.157") diameter end mill:

❌ **Without compensation:**
- A 6" square perimeter → Final part is 5.843" (too small by tool diameter)
- A 1.125" bearing hole → Final hole is 1.282" (too large by tool diameter)
- A 2" pocket → Final pocket is 2.157" (too large by tool diameter)

✅ **With compensation:**
- All features come out to the exact CAD dimensions

## How the Tool Compensation Works

### Perimeter (Offset OUTWARD)
```
CAD perimeter: 6.000" square
Tool diameter: 0.157" (4mm)
Tool radius: 0.0785"

Toolpath: Offset outward by 0.0785"
Result: Final part is exactly 6.000" square
```

The tool cuts OUTSIDE the CAD line by the tool radius.

### Pockets (Offset INWARD)
```
CAD pocket: 2.000" square
Tool diameter: 0.157" (4mm)
Tool radius: 0.0785"

Toolpath: Offset inward by 0.0785"
Result: Final pocket is exactly 2.000" square
```

The tool cuts INSIDE the CAD line by the tool radius.

### Holes (Reduced Radius)
```
CAD bearing hole: 1.125" diameter (0.5625" radius)
Tool diameter: 0.157" (4mm)
Tool radius: 0.0785"

Toolpath radius: 0.5625" - 0.0785" = 0.4840"
Result: Final hole is exactly 1.125" diameter
```

The tool follows a circle with radius reduced by the tool radius.

## Using Tool Compensation

### Specify Your Tool Diameter

```bash
# 4mm end mill (default)
python frc_cam_postprocessor.py part.dxf output.gcode --tool-diameter 0.157

# 1/8" end mill
python frc_cam_postprocessor.py part.dxf output.gcode --tool-diameter 0.125

# 6mm end mill
python frc_cam_postprocessor.py part.dxf output.gcode --tool-diameter 0.236
```

**Common tool sizes:**
- 1/8" = 0.125"
- 3mm = 0.118"
- 4mm = 0.157" (default)
- 5mm = 0.197"
- 6mm = 0.236"
- 1/4" = 0.250"

### Verification in G-code

The generated G-code header shows the compensation:

```gcode
(FRC Robotics CAM Post-Processor)
(Material thickness: 0.25")
(Tool diameter: 0.157" = 3.99mm)
(Tool compensation: Perimeter +0.0785", Pockets -0.0785")
```

And the console output confirms:

```
Tool compensation applied:
  Tool diameter: 0.1570"
  Tool radius: 0.0785"
  Perimeter: offset OUTWARD by 0.0785"
  Pockets: offset INWARD by 0.0785"
  Bearing holes: toolpath radius reduced by 0.0785"
  Screw holes: milled with compensation
```

## Screw Holes

Screw holes are milled using helical interpolation with tool compensation applied. This creates precise 0.201" diameter holes (free fit per ASME B18.2.8).

```bash
python frc_cam_postprocessor.py part.dxf output.gcode
```

**What it does:**
- Helical interpolation around hole
- Tool compensation applied
- Creates exact 0.201" diameter hole

**Minimum hole size:**
- Holes must be at least 1.2× the tool diameter for chip evacuation
- Example: 4mm (0.157") tool → minimum hole is 0.188"
- Smaller holes are automatically skipped with a warning

## Tool Size Limits

### Minimum Feature Sizes

Your tool diameter limits what you can cut:

**Pockets:**
- Minimum pocket size ≈ 2× tool diameter
- Example: 4mm tool → minimum ~8mm (0.315") pocket
- Smaller pockets will show warnings

**Holes:**
- Minimum hole diameter is 1.2× tool diameter (for chip evacuation)
- Example: 4mm (0.157") tool → minimum 0.188" hole
- Smaller holes are automatically skipped

**Inside Corners:**
- Corner radius = tool radius
- Example: 4mm tool → 2mm inside radius minimum
- Sharp corners become rounded

### Warning Messages

The post-processor will warn you:

```
(WARNING: Pocket too small for tool diameter 0.157")
(WARNING: Tool diameter 0.157" is too large for 0.100" hole!)
```

If you see these, either:
1. Use a smaller tool
2. Redesign the part with larger features

## Measuring Tool Diameter

### Method 1: Calipers
Measure the cutting diameter of your end mill with digital calipers.

### Method 2: Manufacturer Spec
Check the tool packaging or data sheet.

### Method 3: Test Cut
1. Cut a known feature (like a 2" square)
2. Measure the result
3. Calculate: `actual_tool_diameter = expected_size - measured_size`

Example:
- CAD: 2.000" square
- Measured: 1.843" square
- Tool diameter: 2.000 - 1.843 = 0.157" ✓

## Common Issues

### "My parts are too small!"
→ Tool compensation is working correctly! The TOOLPATH is offset, not the final part.

### "My parts are too large!"
→ Check if tool compensation is disabled or tool diameter is wrong.

### "Corners are rounded!"
→ This is normal - inside corners have radius = tool radius.

### "Some pockets won't cut!"
→ Pocket is too small for your tool. Use smaller tool or bigger pocket.

### "Holes are wrong size!"
→ Verify tool diameter is correct. Try test cut to calibrate.

## Testing Tool Compensation

### Visual Check
1. Generate G-code: `python frc_cam_postprocessor.py test.dxf test.gcode --tool-diameter 0.157`
2. Upload to ncviewer.com
3. Verify:
   - Perimeter cuts OUTSIDE the part boundary
   - Pockets cut INSIDE the pocket boundary
   - Holes are smaller circles than CAD

### Physical Test
1. Cut a test square (e.g., 2.000" × 2.000")
2. Measure with calipers
3. Should be exactly 2.000" ± 0.005"

## Mathematical Details

For those interested in the geometry:

**Offset formula:**
- Perimeter: `toolpath = CAD_boundary.buffer(+tool_radius)`
- Pocket: `toolpath = CAD_boundary.buffer(-tool_radius)`
- Circle: `toolpath_radius = CAD_radius - tool_radius`

**Why buffer() and not just offset?**
- Handles complex shapes correctly
- Manages corners, arcs, and irregular boundaries
- Prevents self-intersecting paths
- Uses robust geometric algorithms (Shapely library)

## Advanced: Changing Tool Mid-Job

If you want to use different tools for different operations, edit the script:

```python
# In generate_gcode() method, before each operation section:

# Small tool for pockets
self.tool_diameter = 0.125  # 1/8" 
self.tool_radius = self.tool_diameter / 2
gcode.append("M6 T1  ; Change to 1/8\" end mill")

# [Generate pocket code]

# Large tool for perimeter
self.tool_diameter = 0.250  # 1/4"
self.tool_radius = self.tool_diameter / 2
gcode.append("M6 T2  ; Change to 1/4\" end mill")

# [Generate perimeter code]
```

## Summary Checklist

Before cutting:
- [ ] Measured actual tool diameter
- [ ] Specified correct `--tool-diameter`
- [ ] Checked G-code header confirms compensation
- [ ] Visualized in NCViewer (paths offset correctly)
- [ ] No "pocket too small" warnings
- [ ] No "hole too small" warnings
- [ ] Test cut on scrap verified dimensions

Tool compensation is **critical** for accurate parts. Don't skip it!

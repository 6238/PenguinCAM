# Quick Start Guide

## Get Running in 5 Minutes

### 1. Install Python and dependencies
```bash
# Install Python from python.org if you don't have it
# Then install the required libraries:
pip install ezdxf shapely
```

### 2. Export from OnShape
- Right-click your part face → Export → DXF
- Save as `my_part.dxf`

### 3. Run the script
```bash
python frc_cam_postprocessor.py my_part.dxf output.gcode --thickness 0.25 --tool-diameter 0.157
```

**⚠️ IMPORTANT:** 
- Always specify `--tool-diameter`! Your parts will be the wrong size without it.
- **Z=0 is at the sacrifice board (bottom)**, not material top. Always zero to sacrifice board!

### 4. Setup and run
- Open `output.gcode` in a text editor to review
- **Zero your Z-axis to the SACRIFICE BOARD surface** (not material top!)
- Load into your CNC controller
- Do a dry run first!

## Common Commands

**1/4" aluminum plate with 4mm tool:**
```bash
python frc_cam_postprocessor.py part.dxf part.gcode --thickness 0.25 --tool-diameter 0.157
```

**1/8" polycarbonate with 1/8" tool and more tabs:**
```bash
python frc_cam_postprocessor.py part.dxf part.gcode --thickness 0.125 --tool-diameter 0.125 --tabs 6
```

**Fast drilling mode for screw holes:**
```bash
python frc_cam_postprocessor.py part.dxf part.gcode --thickness 0.25 --tool-diameter 0.157 --drill-screws
```

## What Gets Detected Automatically

✅ **Screw holes:** 0.19" diameter (#10 clearance)
✅ **Bearing holes:** 1.125" diameter  
✅ **Pockets:** Any closed inner boundary
✅ **Perimeter:** Outer boundary (with tabs)

## Tool Sizes Reference

**Common end mill sizes:**
- **4mm = 0.157"** (default, good all-around)
- 1/8" = 0.125" (smaller features, slower)
- 6mm = 0.236" (faster, larger features only)
- 1/4" = 0.250" (fast perimeter cuts)

**⚠️ Measure your actual tool with calipers!** Nominal sizes may vary.

## Customizing Feed Rates

Edit these lines in `frc_cam_postprocessor.py`:

```python
self.feed_rate = 30.0      # Cutting speed (IPM)
self.plunge_rate = 10.0    # Plunge speed (IPM)
self.tab_width = 0.25      # Tab width (inches)
self.tab_height = 0.03     # Tab thickness (inches)
```

**Material-specific recommendations:**
- **Aluminum:** 30-40 IPM feed, 8-10 IPM plunge
- **Polycarbonate:** 40-60 IPM feed, 15-20 IPM plunge  
- **Wood:** 50-80 IPM feed, 20-30 IPM plunge

## Test File Included

Try it with the included `sample_part.dxf`:
```bash
python frc_cam_postprocessor.py sample_part.dxf test.gcode --thickness 0.25
```

This creates a 6"×6" plate with:
- 4 screw holes at corners
- 1 bearing hole in center
- 1 pocket (2"×2")
- Perimeter with 4 tabs

## Troubleshooting

**Can't find holes?**
- Check hole diameters are exactly 0.19" or 1.125"
- Adjust `self.tolerance` in the script if needed

**Perimeter not detected?**
- Make sure it's a closed polyline in your CAD
- It should be the largest boundary

**Tabs in weird spots?**
- Tabs are evenly spaced around perimeter
- Change with `--tabs` option

## Need Help?

Check the full README.md for detailed information!
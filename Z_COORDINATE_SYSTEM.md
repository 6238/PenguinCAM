# Z-Axis Coordinate System Change

## What Changed

The Z-axis coordinate system has been updated for practical workflow:

### OLD System (Top Surface Reference)
- **Z=0** was at the TOP surface of the material
- Cut depth was negative (e.g., Z=-0.25")
- Required zeroing tool to material top (inconsistent thickness)

### NEW System (Bottom/Sacrifice Board Reference) ✅
- **Z=0** is at the SACRIFICE BOARD surface (bottom)
- Material top is at positive Z (e.g., Z=0.25")
- Cut depth is slightly negative (e.g., Z=-0.02")
- **Always zero to sacrifice board** (consistent every time)

## Why This Is Better

### 1. Consistent Zeroing
- You ALWAYS zero to the sacrifice board
- Don't need to measure material thickness precisely
- Same zero point every time, regardless of material

### 2. Guaranteed Cut-Through
- Can cut slightly into sacrifice board (default: 0.02")
- Ensures complete part separation
- No partial cuts at the bottom

### 3. Material Thickness Doesn't Affect Setup
- Thicker/thinner material? Doesn't matter
- Just set `--thickness` parameter
- Zero point stays the same

## Z Coordinate Examples

**With 0.25" material and 0.02" overcut:**

| Position | Z Coordinate | Description |
|----------|-------------|-------------|
| Safe height | Z=0.35" | 0.1" above material top |
| Material top | Z=0.25" | Top surface |
| Material bottom | Z=0.00" | Sacrifice board surface (ZERO HERE) |
| Cut depth | Z=-0.02" | 0.02" into sacrifice board |

**With 0.125" material and 0.02" overcut:**

| Position | Z Coordinate | Description |
|----------|-------------|-------------|
| Safe height | Z=0.225" | 0.1" above material top |
| Material top | Z=0.125" | Top surface |
| Material bottom | Z=0.00" | Sacrifice board surface (ZERO HERE) |
| Cut depth | Z=-0.02" | 0.02" into sacrifice board |

## New Parameters

### --sacrifice-depth
How far to cut into the sacrifice board (default: 0.02")

**Usage:**
```bash
# Default (0.02" into sacrifice board)
python frc_cam_postprocessor.py part.dxf output.gcode --thickness 0.25

# More aggressive overcut (0.03")
python frc_cam_postprocessor.py part.dxf output.gcode --thickness 0.25 --sacrifice-depth 0.03

# Minimal overcut (0.01")
python frc_cam_postprocessor.py part.dxf output.gcode --thickness 0.25 --sacrifice-depth 0.01
```

**Recommendations:**
- **Aluminum:** 0.02" (default) - adequate
- **Polycarbonate:** 0.02-0.03" - material can flex
- **Wood:** 0.02-0.03" - fibers can cause partial cuts
- **Thin materials (<1/8"):** 0.015" - less overcut needed

## Setup Procedure

### Step 1: Install Material
1. Place material on sacrifice board
2. Clamp or tape down securely
3. Material thickness doesn't need to be exact

### Step 2: Zero Z-Axis
1. **Touch off to SACRIFICE BOARD surface**
2. Set this as Z=0 in your controller
3. DON'T touch off to material top!

### Step 3: Run Program
1. Load G-code
2. Start cycle
3. Tool will:
   - Rapid to safe height (above material)
   - Plunge to material surface
   - Cut through material
   - Cut slightly into sacrifice board
   - Retract to safe height

## G-Code Header

The generated G-code includes clear setup instructions:

```gcode
(Z-AXIS COORDINATE SYSTEM:)
(  Z=0 is at SACRIFICE BOARD (bottom))
(  Material top is at Z=0.2500")
(  Cut depth: Z=-0.0200" (0.0200" into sacrifice board))
(  Safe height: Z=0.3500")
(  ** Zero your Z-axis to the sacrifice board surface **)
```

## Console Output

After generation, the console reminds you:

```
Z-AXIS SETUP:
  ** Zero your Z-axis to the SACRIFICE BOARD surface **
  Material top will be at Z=0.2500"
  Cut depth: Z=-0.0200" (0.0200" into sacrifice board)
  Safe height: Z=0.3500"
```

## Tab Height Calculation

Tabs are calculated correctly in the new system:

```
Cut depth: Z=-0.02" (into sacrifice board)
Tab height: 0.03" (material left in tab)
Tab Z position: -0.02" + 0.03" = 0.01"
```

So tabs are at Z=0.01", which leaves 0.03" of material connecting the part.

## Migration from Old System

If you have old G-code files (before this update):

**DO NOT USE THEM!** The Z coordinates are incompatible.

Regenerate all G-code with the new post-processor.

## Troubleshooting

### "Tool crashes into sacrifice board"
- Check that you zeroed to sacrifice board, not material top
- Verify `--thickness` parameter matches actual material

### "Part not fully cut through"
- Increase `--sacrifice-depth` to 0.03" or 0.04"
- Check that sacrifice board is flat and level
- Verify Z-axis is actually zeroed to board surface

### "Too much material removed from sacrifice board"
- Reduce `--sacrifice-depth` to 0.01"
- Check Z-axis zero procedure
- Consider replacing worn sacrifice board

### "Can't zero to sacrifice board - it's too damaged"
- Replace or flip sacrifice board
- Use a straight edge to find highest point
- Zero to that point instead

## Benefits for FRC Teams

### 1. Faster Setup
- No measuring material thickness with calipers
- No touching off to material surface
- One zero point, every time

### 2. More Reliable
- Guaranteed cut-through every time
- No partial cuts at bottom
- No "almost through" problems

### 3. Easier for New Students
- Simpler zeroing procedure
- Less chance of error
- Clear instructions in G-code header

### 4. Better for Production
- Batch cutting multiple parts
- Don't re-zero between parts (if same thickness)
- Faster turnaround

## Example Workflow

**Cutting 5 identical aluminum plates:**

1. **One-time setup:**
   - Place sacrifice board on CNC
   - Zero Z-axis to sacrifice board
   - DONE - don't touch Z-axis again!

2. **For each plate:**
   - Place 1/4" aluminum on board
   - Tape down
   - Load G-code (same file every time)
   - Run program
   - Remove part
   - Repeat

3. **No re-zeroing needed** between parts!

## Technical Details

### Z Coordinate Calculations

In the code:
```python
self.material_thickness = 0.25          # User input
self.sacrifice_board_depth = 0.02       # User input (default)
self.clearance_height = 0.1             # Fixed

# Calculated values:
self.safe_height = 0.25 + 0.1 = 0.35    # Above material
self.material_top = 0.25                 # Top surface
self.cut_depth = -0.02                   # Into sacrifice board
```

### All Z Moves Updated

Every Z coordinate in the G-code has been updated:
- ✅ Safe height moves (G0 Z...)
- ✅ Plunge moves (G1 Z... for drilling)
- ✅ Helical moves (G2 ... Z... for holes)
- ✅ Tab heights (G1 Z... for tabs)
- ✅ Pocket plunges
- ✅ Perimeter cuts

## Compatibility

This change affects:
- ✅ Main post-processor script
- ⚠️ Safe test mode (needs update)
- ⚠️ Batch processing scripts (use new syntax)

## Summary

✅ **New workflow is better in every way:**
- Easier setup (always zero to sacrifice board)
- More reliable (guaranteed cut-through with overcut)
- Faster (no re-zeroing between parts)
- Clearer (G-code header explains setup)

✅ **Default settings work great:**
- 0.02" overcut is good for most materials
- Adjust if needed with `--sacrifice-depth`

✅ **All Z coordinates automatically calculated:**
- Safe height = material thickness + 0.1"
- Material top = material thickness
- Cut depth = -sacrifice board depth

**Just zero to the sacrifice board and go!** 🎯

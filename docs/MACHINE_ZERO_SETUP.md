# Machine Zero Setup Guide

## Complete Zeroing Procedure

### Overview
Your CNC machine needs three zero points (X=0, Y=0, Z=0) to know where your part is located. Here's where to set each one:

```
                    SACRIFICE BOARD
    ╔═══════════════════════════════════════╗
    ║                                       ║
    ║     YOUR MATERIAL                     ║
    ║   ┌─────────────────────────┐        ║
    ║   │                         │        ║
    ║   │    [Your CAD Design]    │        ║
    ║   │                         │        ║
    ║   │         Part            │        ║
    ║   │                         │        ║
    ║   └─────────────────────────┘        ║
    ║   ↑                                   ║
    ║   X=0, Y=0 (lower-left corner)       ║
    ║                                       ║
    ╚═══════════════════════════════════════╝
    ↑
    Z=0 (sacrifice board surface)
```

## Step-by-Step Setup

### 1. Secure Material
```
Place material on sacrifice board
Clamp or tape at corners
Ensure material is flat
```

### 2. Zero X and Y (Lower-Left Corner)
```
Tool Position: Lower-left corner of material
X-axis: Set to 0
Y-axis: Set to 0

This point matches (0,0) in your CAD file
```

**Visual:**
```
    Material Top View:
    
    (0,0) ← START HERE
      ↓
    ┌─────────────────────┐
    │                     │
    │    Your Part        │
    │                     │
    └─────────────────────┘
                          ↑
                     (max X, max Y)
```

### 3. Zero Z (Sacrifice Board Surface)
```
Tool Position: Any location, touching sacrifice board
Z-axis: Set to 0

NOT the material top!
This ensures consistent depth every time
```

**Side View:**
```
         Tool
          ↓
    ──────█──────  ← Material top (Z = thickness)
    ───────────── 
    ─────────────  ← Material body
    ─────────────
    ═════════════  ← Sacrifice board (Z = 0) ★ ZERO HERE
```

## Why Lower-Left Corner?

**Matches CAD Convention:**
- Most CAD programs use lower-left as origin
- Your DXF file has (0,0) at lower-left
- Parts are drawn in positive X and Y directions

**Practical Benefits:**
- Easy to find corner location
- Can see both edges clearly
- Repeatable positioning
- Standard industry practice

**Coordinate System:**
```
    Y-axis
      ↑
      │
      │    Your Part
      │   ┌─────────┐
      │   │         │
      │   │    ●    │  (part center)
      │   │         │
      └───┴─────────┴─────→ X-axis
    (0,0)
```

## Full Machine Setup Checklist

**Before Starting:**
- [ ] Sacrifice board installed and level
- [ ] Material placed on sacrifice board
- [ ] Material clamped securely
- [ ] Tool installed in spindle
- [ ] Safety glasses on

**Zeroing Procedure:**
- [ ] Jog tool to lower-left corner of material
- [ ] Touch off or use edge finder
- [ ] Set X=0 in controller
- [ ] Set Y=0 in controller
- [ ] Move tool over sacrifice board (any location)
- [ ] Touch off to sacrifice board surface
- [ ] Set Z=0 in controller

**Verification:**
- [ ] Move to X=0 Y=0 - tool should be at lower-left corner
- [ ] Move to Z=0 - tool should touch sacrifice board
- [ ] Move to safe height (e.g., Z=0.5") - tool clears material
- [ ] Coordinates display correctly in controller

**Ready to Cut!**

## Common Mistakes

❌ **Wrong: Zeroing Z to material top**
```
    ──────█──────  ← Z=0 (WRONG!)
    ─────────────  ← Material
    ═════════════  ← Sacrifice board
    
Result: Cut depth depends on material thickness
        Inconsistent between parts
```

✅ **Right: Zeroing Z to sacrifice board**
```
    ─────────────  ← Material top (Z=0.25")
    ─────────────  ← Material
    ══════█══════  ← Z=0 (RIGHT!) ★
    
Result: Cut depth always the same
        Guaranteed cut-through
```

❌ **Wrong: Zeroing X/Y to center**
```
    ┌─────────────┐
    │             │
    │      ●      │  ← X=0, Y=0 (WRONG!)
    │   (center)  │
    └─────────────┘
    
Result: Doesn't match CAD origin
        Part position unpredictable
```

✅ **Right: Zeroing X/Y to lower-left**
```
    ● ← X=0, Y=0 (RIGHT!)
    ┌─────────────┐
    │             │
    │  Your Part  │
    │             │
    └─────────────┘
    
Result: Matches CAD origin
        Predictable positioning
```

## G-Code Coordinates Explained

After zeroing, the G-code uses these coordinates:

**X and Y:**
```
G0 X1.5 Y2.3   → 1.5" right, 2.3" up from lower-left corner
G0 X5.5 Y4.8   → 5.5" right, 4.8" up from lower-left corner
```

**Z:**
```
G0 Z0.35   → 0.35" above sacrifice board (safe height)
G1 Z0.25   → At material top surface
G1 Z0.00   → At sacrifice board surface
G1 Z-0.02  → 0.02" into sacrifice board (cut depth)
```

## Material Size Requirements

Your material must be **larger than the part** because:
1. Tool has diameter (tool compensation adds ~0.16")
2. Clamps need space at edges
3. Lower-left corner must be accessible

**Example:**
```
Part size: 6" × 6"
Tool comp: +0.16" per side = +0.32" total
Clamp space: +0.5" per side = +1.0" total

Minimum material: 7.5" × 7.5"
Recommended: 8" × 8" (gives working room)
```

## Workflow Summary

```
1. PREP
   - Install sacrifice board
   - Place material
   - Clamp securely

2. ZERO X/Y
   - Jog to lower-left corner
   - Set X=0, Y=0

3. ZERO Z
   - Touch sacrifice board
   - Set Z=0

4. VERIFY
   - Check coordinates
   - Test safe height

5. RUN
   - Load G-code
   - Start program
   - Monitor first pass
```

## Pro Tips

**Use Edge Finder:**
- More accurate than eyeballing
- Especially important for X/Y zero
- Worth the investment for production work

**Mark Your Corner:**
- Use layout fluid or marker
- Mark X/Y zero location on sacrifice board
- Speeds up setup for repeat jobs

**Save Work Offsets:**
- Many controllers support multiple work offsets
- Save common positions (G54, G55, etc.)
- Quick switching between jobs

**Test Moves:**
- After zeroing, do a test move
- G0 X0 Y0 Z0.5 should be at lower-left corner, above material
- If not, check your zeroing procedure

## Questions?

**"Why not center of material?"**
- CAD files use lower-left origin by convention
- Easier to measure and verify
- Matches industry standard

**"Can I use a different corner?"**
- Technically yes, but you'd need to modify the CAD file
- Not recommended - causes confusion
- Stick with lower-left for consistency

**"What if my material isn't perfectly square?"**
- Zero to the actual lower-left corner
- Part will cut correctly relative to that corner
- Material edges don't need to be perfect

**"Do I need to re-zero between parts?"**
- X/Y: Yes, if you move the material
- Z: No, if material thickness is the same
- This is why Z-at-sacrifice-board is better!

## Safety Reminders

⚠️ Always:
- Double-check zeros before starting
- Do a dry run first (spindle off, Z raised)
- Keep hand near emergency stop
- Verify safe height clears all clamps
- Watch the first cut closely

Your machine is now properly zeroed and ready to cut! 🎯

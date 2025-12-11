# Machine Zeroing Instructions Added ✅

## What Was Added

Complete X/Y/Z zeroing instructions have been added to the documentation, covering where and how to set all three axes.

## Setup Procedure Summary

### Complete Zeroing Steps:

**1. Clamp Material**
- Place material on sacrifice board
- Secure with clamps or tape

**2. Zero X and Y**
- Position tool at **lower-left corner** of your material
- Set X=0 Y=0 in your controller
- This matches the origin (0,0) in your CAD file

**3. Zero Z**
- Touch off to **sacrifice board surface**
- Set Z=0 in your controller
- NOT the material top!

**4. Run Program**
- Load G-code and start cutting

## Why Lower-Left Corner?

✅ **Matches CAD convention** - Most CAD programs use lower-left as origin
✅ **Standard practice** - Industry standard for CNC work
✅ **Easy to locate** - Can see both edges clearly
✅ **Repeatable** - Same corner every time

## Visual Reference

```
    Material (top view):
    
    X=0, Y=0 ← Zero here!
      ↓
    ┌─────────────────────┐
    │                     │
    │    Your Part        │
    │    from CAD         │
    │                     │
    └─────────────────────┘
    
    Positive X goes right →
    Positive Y goes up ↑
```

## Updated Documentation

**README.md** - Main setup instructions
```
MACHINE SETUP:
- X/Y Zero: Clamp your material to the sacrifice board, then set 
  X=0 Y=0 at the lower-left corner of your material.
- Z Zero: Set Z=0 at the sacrifice board surface (bottom).
```

**QUICK_START.md** - Quick reference
```
- Zero X/Y: Set X=0 Y=0 at the lower-left corner of your material
- Zero Z: Set Z=0 at the sacrifice board surface
```

**Z_COORDINATE_SYSTEM.md** - Detailed procedure
- Added Step 2: Zero X and Y Axes (before Z zeroing)
- Explains why lower-left corner
- Shows coordinate system

**Z_AXIS_UPDATE_SUMMARY.md** - Updated workflow
- Includes X/Y zeroing in setup procedure
- Complete 4-step process

**NEW: MACHINE_ZERO_SETUP.md** - Complete visual guide
- ASCII art diagrams
- Step-by-step checklist
- Common mistakes to avoid
- Pro tips for accuracy
- Troubleshooting guide

## Complete Checklist

Copy this for your team:

```
☐ 1. Place material on sacrifice board
☐ 2. Clamp securely
☐ 3. Jog tool to lower-left corner of material
☐ 4. Set X=0
☐ 5. Set Y=0
☐ 6. Move tool over sacrifice board
☐ 7. Touch off to sacrifice board surface
☐ 8. Set Z=0
☐ 9. Test: Move to X=0 Y=0 (should be at corner)
☐ 10. Test: Move to Z=0 (should touch board)
☐ 11. Load G-code
☐ 12. Run program
```

## Files Reference

Quick links to documentation:

- [README.md](computer:///mnt/user-data/outputs/README.md) - Main documentation with setup
- [QUICK_START.md](computer:///mnt/user-data/outputs/QUICK_START.md) - Fast reference
- [MACHINE_ZERO_SETUP.md](computer:///mnt/user-data/outputs/MACHINE_ZERO_SETUP.md) - Complete visual guide (NEW!)
- [Z_COORDINATE_SYSTEM.md](computer:///mnt/user-data/outputs/Z_COORDINATE_SYSTEM.md) - Z-axis details

## Example Workflow

**Setting up to cut a 6"×6" plate:**

1. Place 8"×8" aluminum on sacrifice board
2. Clamp at corners
3. Jog tool to lower-left corner of aluminum
4. Set X=0 Y=0
5. Touch sacrifice board with tool
6. Set Z=0
7. Load G-code
8. Start cutting!

**No guessing, no measuring - just set the zeros and go!**

## Benefits

✅ **Clear instructions** - Everyone on team knows procedure
✅ **Consistent setup** - Same way every time
✅ **Less confusion** - No more "where do I zero?"
✅ **Faster training** - New students get up to speed quickly
✅ **Industry standard** - Matches professional practice

## Common Questions Answered

**"Why lower-left and not center?"**
- CAD files use lower-left as origin
- Industry standard convention
- Easier to verify and measure

**"Why sacrifice board for Z and not material top?"**
- Consistent reference regardless of material thickness
- Guaranteed cut-through with overcut
- No need to measure material precisely

**"Do I need to re-zero between parts?"**
- X/Y: Yes, if material moves
- Z: No, if same thickness material
- That's why Z-at-board is better!

## Summary

Complete machine zeroing instructions are now documented throughout:

✅ **X/Y axes:** Lower-left corner of material
✅ **Z axis:** Sacrifice board surface (bottom)
✅ **Visual guides:** ASCII art and diagrams
✅ **Checklists:** Step-by-step procedures
✅ **Troubleshooting:** Common mistakes covered

Your team now has everything they need for proper machine setup! 🎯

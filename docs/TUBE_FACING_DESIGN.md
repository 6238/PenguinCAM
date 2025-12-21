# Tube Facing Operation Design Document

## Overview

This document describes the tube facing mode in PenguinCAM that squares the ends of box tubing held in a jig. The operation uses a pre-generated Fusion 360 adaptive toolpath with Y-coordinate offsets applied for a two-pass flip strategy.

**Purpose**: Create a perfectly square, flat end face on 1x1 or 2x1 box tubing from rough bandsaw cuts.

**Key insight**: By facing slightly more than half the tube wall on each pass (with a 180° flip between), we guarantee complete coverage without requiring precise alignment.

---

## Physical Setup

### Coordinate System

```
Looking at tube end from spindle (from -Y toward +Y):

         Z (vertical)
         ↑
         │    ┌──────────────────┐ Z=1.0"
         │    │                  │
         │    │   TUBE END       │
         │    │   (hollow)       │
         │    │                  │
         ●────┴──────────────────┴───→ X (across tube width)
       (0,0)                    X=1.0"
       ORIGIN

Y axis points INTO the tube (away from spindle)
Y=0 is the nominal tube end face
```

### Jig Specifications

| Property | Value | Notes |
|----------|-------|-------|
| Coordinate system | G55 | Dedicated work offset for jig location |
| Origin (X=0, Y=0, Z=0) | Bottom-left corner of tube end | At jig surface |
| X axis | Across tube width | 0 to 1.0" for 1x1 tube |
| Y axis | Along tube length | 0 at tube face, positive into tube |
| Z axis | Vertical | 0 at jig surface, positive up |

### Tube Dimensions

| Tube Size | X Range | Z Range | Wall Thickness |
|-----------|---------|---------|----------------|
| 1x1 | 0 to 1.0" | 0 to 1.0" | 0.125" (1/8") |
| 2x1 standing | 0 to 1.0" | 0 to 2.0" | 0.125" (1/8") |
| 2x1 lying flat | 0 to 2.0" | 0 to 1.0" | 0.125" (1/8") |

---

## Y-Axis Cutting Strategy

### Tool Compensation and Finishing Cut

The toolpath was generated in Fusion 360 for a 4mm (0.157") end mill. The **finishing cut** runs at Y=-0.0787" (one tool radius). After tool compensation:

```
Tool center at Y = -0.0787"
Tool radius    = +0.0787"
                 ─────────
Tube face at Y =  0.0000"
```

This means when the toolpath runs unmodified (offset = 0), the tube face ends up at Y=0.

### Two-Pass Flip Strategy

The tube has 0.125" (1/8") walls. We face slightly more than half the wall on each pass:

| Pass | Y Offset | Finishing Cut Position | Resulting Tube Face |
|------|----------|------------------------|---------------------|
| Pass 1 | +0.125" | Y = -0.0787 + 0.125 = +0.0463" | Y = +0.125" |
| Pass 2 | 0.0" | Y = -0.0787" | Y = 0" |

```
PASS 1 (Original Orientation):
    Toolpath shifted +0.125"
    Finishing cut at Y = +0.0463"
    After tool comp → tube face at Y = +0.125"

    Faces the OUTER half of the tube wall (Y=0 to Y=+0.125)

════════════════════════════════════════════════════════════

FLIP TUBE 180° END-FOR-END

════════════════════════════════════════════════════════════

PASS 2 (After Flip):
    Toolpath at original position (offset = 0)
    Finishing cut at Y = -0.0787"
    After tool comp → tube face at Y = 0"

    Faces what WAS the inner half, now exposed after flip

RESULT: Complete tube end is squared from Y=0 to Y=0.125"
```

### Why This Works

1. **Pass 1** faces the outer portion of each wall (from Y=0 toward +Y)
2. **Flip** rotates the tube 180°, bringing the unfaced inner portion to face the spindle
3. **Pass 2** faces this now-exposed portion, meeting exactly at Y=0

The 0.125" offset equals the wall thickness, ensuring complete coverage with no gaps.

---

## Toolpath Source

### Fusion 360 Adaptive Clearing

The toolpath is extracted from `square_end.gcode` (Fusion 360 CAM output from the mach4 repo) and stored in `tube_facing_toolpath.py` as a Python string constant `TUBE_FACING_TOOLPATH_1X1`.

The toolpath includes:
- **Adaptive clearing passes** at multiple Z levels (Z≈0.45", Z≈0.83", etc.)
- **Ramped/helical entries** into material
- **Helical profile cut** (G19 plane YZ arcs) for the final finishing pass
- **Arc moves** (G2/G3) for smooth tool engagement

### Helical Profile Cut

The final section of the toolpath performs a helical profile cut around the tube end:

```gcode
G19 G3 Y-0.2787 Z0.46 J0.1 K0.    ( Helical entry in YZ plane )
G1 Y-0.1787                        ( Linear approach )
G17 G3 X1. Y-0.0787 I-0.1 J0.      ( Finishing arc - THIS IS THE KEY LINE )
G1 X0.                             ( Cut across tube width )
G3 X-0.1 Y-0.1787 I0. J-0.1        ( Exit arc )
G19 G2 Y-0.3787 Z0.56 J0. K0.1     ( Helical retract )
G17                                 ( Back to XY plane )
```

The finishing arc at `Y-0.0787` (tool radius) places the actual tube face at Y=0 after tool compensation.

---

## G-Code Structure

### Program Flow

```
┌─────────────────────────────────────────────┐
│ INITIALIZATION                              │
│  • G90 G94 G91.1 G40 G49 G17 (modal setup)  │
│  • G20 (inch mode)                          │
│  • G0 G28 G91 Z0. (home Z axis)             │
│  • G90 (back to absolute)                   │
│  • T1 M6 (tool change)                      │
│  • S18000 M3 (spindle on)                   │
│  • G4 P3.0 (dwell for spin-up)              │
│  • G55 (select jig coordinate system)       │
└─────────────────────────────────────────────┘
                    ↓
┌─────────────────────────────────────────────┐
│ PHASE 1: FACE FIRST HALF                    │
│  • G53 G0 Z0. (machine Z0 - safe clearance) │
│  • G0 X0 Y0 (rapid to work origin)          │
│  • [Toolpath with Y offset = +0.125"]       │
└─────────────────────────────────────────────┘
                    ↓
┌─────────────────────────────────────────────┐
│ PAUSE FOR FLIP                              │
│  • G53 G0 Z0. (safe clearance)              │
│  • G53 G0 X0.5 Y23.5 (park - machine coords)│
│  • M5 (spindle off)                         │
│  • G4 P5.0 (dwell)                          │
│  • M0 (mandatory pause)                     │
└─────────────────────────────────────────────┘
                    ↓
┌─────────────────────────────────────────────┐
│ PHASE 2: FACE SECOND HALF                   │
│  • S18000 M3 (spindle on)                   │
│  • G4 P3.0 (dwell for spin-up)              │
│  • G53 G0 Z0. (safe clearance)              │
│  • G0 X0 Y0 (rapid to work origin)          │
│  • [Toolpath with Y offset = 0.0"]          │
└─────────────────────────────────────────────┘
                    ↓
┌─────────────────────────────────────────────┐
│ PROGRAM END                                 │
│  • G53 G0 Z0. (safe clearance)              │
│  • G53 G0 X0.5 Y23.5 (park - machine coords)│
│  • M5 (spindle off)                         │
│  • M30 (program end)                        │
└─────────────────────────────────────────────┘
```

### Critical Safety Pattern: Z Before XY

**Every XY move is preceded by a Z move to machine Z0:**

```gcode
G53 G0 Z0.           ; ALWAYS move Z to safe height first
G0 X0 Y0             ; THEN move XY (to work origin)
```

or

```gcode
G53 G0 Z0.           ; ALWAYS move Z to safe height first
G53 G0 X0.5 Y23.5    ; THEN move XY (to park position)
```

This prevents collisions with the jig or workpiece during XY rapids.

### Machine vs Work Coordinates

| Command | Coordinate System | Purpose |
|---------|-------------------|---------|
| `G53 G0 Z0.` | Machine | Retract spindle fully (safe height) |
| `G53 G0 X0.5 Y23.5` | Machine | Park at back of machine |
| `G0 X0 Y0` | Work (G55) | Rapid to jig origin |
| `G55` | Work | Select jig coordinate system |

---

## CLI Usage

```bash
python frc_cam_postprocessor.py output.nc --mode tube-facing --tube-size 1x1
```

### Arguments

| Argument | Values | Description |
|----------|--------|-------------|
| `--mode` | `standard`, `tube-facing` | Operation mode |
| `--tube-size` | `1x1`, `2x1-standing`, `2x1-flat` | Tube dimensions |
| `output.nc` | Path | Output G-code file |

---

## Implementation Details

### Files

| File | Purpose |
|------|---------|
| `frc_cam_postprocessor.py` | Main code with `generate_tube_facing_gcode()` method |
| `tube_facing_toolpath.py` | Extracted Fusion 360 toolpath as string constant |
| `tests/test_tube_facing.py` | Unit tests for tube facing mode |

### Y Coordinate Adjustment

The `_adjust_y_coordinate()` helper function applies the Y offset to each line of the toolpath:

```python
pass1_y_offset = 0.125  # Shift toolpath +0.125" for Pass 1
pass2_y_offset = 0.0    # No shift for Pass 2

# Regex replaces Y values: Y-0.0787 → Y0.0463 (for pass1_y_offset)
```

### Material Preset

Tube facing always uses the **aluminum** preset:
- Spindle speed: 18,000 RPM
- Feed rate: 55 IPM
- 25% stepover

---

## Sample Generated G-Code

```gcode
( PENGUINCAM TUBE FACING OPERATION )
( Generated: 2025-12-20 22:30 )
( Tube size: 1x1 )
( Tool: 0.157" end mill )
( )
( SETUP INSTRUCTIONS: )
( 1. Mount tube in jig with end facing spindle )
( 2. Verify G55 is set to jig origin )
( 3. Z=0 is at bottom of tube (jig surface) )
( 4. Y=0 is at nominal end face of tube )
( )

( === INITIALIZATION === )
G90 G94 G91.1 G40 G49 G17
G20
G0 G28 G91 Z0.  ; Home Z axis at rapid speed
G90  ; Back to absolute mode

( Tool and spindle )
T1 M6
S18000 M3
G4 P3.0

G55  ; Use jig work coordinate system

( === PHASE 1: FACE FIRST HALF === )
( Face from Y=-0.125 to Y=+0.125 )

G53 G0 Z0.  ; Move to machine Z0 (safe clearance)
G0 X0 Y0  ; Rapid to work origin

G0 X1.1756 Y0.0138
G0 Z1.25
... (adaptive clearing toolpath with Y offset +0.125") ...
G17 G3 X1. Y0.0463 I-0.1 J0.   ; Finishing cut (shifted)
... (rest of toolpath) ...

( === PAUSE FOR TUBE FLIP === )
G53 G0 Z0.  ; Move to machine Z0 (safe clearance)
G53 G0 X0.5 Y23.5  ; Park at back of machine
M5
G4 P5.0

( *** OPERATOR ACTION REQUIRED *** )
( Flip tube 180 degrees end-for-end )
( Re-clamp tube in jig )
( Press CYCLE START to continue )
M0

( === PHASE 2: FACE SECOND HALF === )
( Face from Y=-0.250 to Y=0 )

S18000 M3
G4 P3.0

G53 G0 Z0.  ; Move to machine Z0 (safe clearance)
G0 X0 Y0  ; Rapid to work origin

G0 X1.1756 Y-0.1112
G0 Z1.25
... (adaptive clearing toolpath, no Y offset) ...
G17 G3 X1. Y-0.0787 I-0.1 J0.   ; Finishing cut (original position)
... (rest of toolpath) ...

( === PROGRAM END === )
G53 G0 Z0.  ; Move to machine Z0 (safe clearance)
G53 G0 X0.5 Y23.5  ; Park at back of machine
M5
M30
```

---

## Differences from Standard PenguinCAM Mode

| Aspect | Standard Mode | Tube Facing Mode |
|--------|---------------|------------------|
| Input | DXF file | None (uses stored toolpath) |
| Coordinate system | G54 | G55 |
| Z=0 reference | Sacrifice board | Jig surface (bottom of tube) |
| Z during cut | Constant depth | Multiple Z levels |
| Operation | Holes, pockets, perimeter | Adaptive facing + profile |
| Multi-pass | Single piece | Two-pass with 180° flip |
| Tool compensation | Applied by PenguinCAM | Built into Fusion toolpath |

---

## Setup Verification Checklist

Before running:

1. **G55 origin check**: Jog to G55 X0 Y0 Z0
   - Tool should be at bottom-left corner of tube end, touching jig surface

2. **Tube width check**: Jog to G55 X1.0 (for 1x1 tube)
   - Tool should be at right edge of tube

3. **Tube height check**: Jog to G55 Z1.0 (for 1x1 tube)
   - Tool should be at top of tube

4. **Safe retract check**: Issue G53 G0 Z0
   - Spindle should retract fully to machine home

5. **Clamp check**: Verify tube is secure in jig

---

## Safety Considerations

1. **Z before XY**: All XY rapids preceded by G53 G0 Z0
2. **Spindle stop before flip**: M5 before M0 pause
3. **Dwell after spindle commands**: G4 P3.0 for spin-up, G4 P5.0 for spin-down
4. **Machine coordinate parking**: G53 G0 X0.5 Y23.5 clears the jig completely
5. **Verify G55**: Operator must confirm jig location before running
6. **Clamp verification**: Visual check after flip and before continuing

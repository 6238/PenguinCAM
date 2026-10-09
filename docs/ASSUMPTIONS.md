# Machine-Specific Assumptions for PenguinCAM-Generated G-Code

This document describes **implicit machine, controller, and interpreter assumptions** made by PenguinCAM when generating G-code.

These assumptions are **independent of user-configurable inputs** such as material, thickness, tool diameter, feeds/speeds, tab count, retract heights, or cut depth. If any assumption below is false on a target machine, the generated G-code may behave incorrectly or unsafely.

---

## 1. Controller & Motion Stack

By **default** the output is **G54 work-coordinate only** and uses a common, portable
subset of G-code (`G0 G1 G2 G3 G4 G17 G20/G21 G40 G49 G54 G80 G90 G91.1 G94`, `M0 M3 M5
M30`). It is designed to run on **GRBL, WinCNC, Mach3/Mach4, LinuxCNC** and similar
controllers without machine-coordinate moves.

**One G-word per block.** The opening modal state is emitted as six separate lines
(`G17`, `G94`, `G91.1`, `G40`, `G49`, `G90`) rather than the combined
`G90 G94 G91.1 G40 G49 G17` that most posts use. The combined form is legal - six
distinct modal groups - and stock GRBL accepts it, but **Carbide Motion (Shapeoko HDM)
rejects it** with "Value set multiple times". `G90` is emitted last on purpose: a parser
that truncates `G91.1` to `G91` would otherwise leave the machine in incremental distance
mode. See `FRCPostProcessor._modal_setup_gcode`.

The one exception is `G53`, which is non-modal and applies only to the block it appears
in, so `G53 G0 Z...` must stay on one line. That line is only emitted when
`park_position` is configured (see below) - a controller that rejects multi-G-word blocks
would likely reject it too, so leave `park_position` unset on such machines.

Three features are **opt-in via config** and, if enabled, add codes or assumptions that not
every controller supports:

* **`park_position`** (machine-coordinate park) → emits `G53` machine moves. Leave it out
  for controllers that don't support `G53` (e.g. GRBL behaves unexpectedly). This is
  the ONLY thing that puts `G53` in the output.
* **`machine.coolant`** (`Air`/`Mist`/`Flood`) → emits `M7`/`M8`/`M9`. Leave it out (or set
  `None`) on controllers without coolant M-codes (stock GRBL rejects `M7` unless compiled
  with it).
* **`machine.tube_work_coordinate_system`** (`G54`–`G59`) → the WCS **tube** jobs run in.
  Default `G54` (the operator zeros G54 to each tube, like flat work — fully portable). Set
  an alternate fixed WCS (e.g. `G55`) only if you have a permanently-fixtured jig **and**
  have pre-set that WCS in the controller; an unset WCS defaults to machine zero and cuts in
  the wrong place. Flat/2.5D work is always `G54`.

Still assumed regardless: standard modal behavior and state persistence.

Note: **Easel** (Inventables) is **not a supported target** — its importer rejects arcs
(`G2/G3`) and non-`G54` work offsets, which PenguinCAM relies on. Teams on Easel-based
machines should send PenguinCAM output **directly to the underlying GRBL controller** with a
general G-code sender rather than importing it into Easel.

---

## 2. Work vs Machine Coordinates

* **G54** is used for all flat/2.5D programmed motion, including start/end safe-Z retracts
  (in work coordinates, `G0 Z<safe_height>`).
* **Tube** jobs use the WCS from `machine.tube_work_coordinate_system` — default `G54` (same
  zero-per-tube workflow as flat work), or an opt-in fixed WCS (e.g. `G55`) for a permanent
  jig. When an alternate WCS is used the program switches to it after spindle start and
  resets to `G54` before program end.
* **G53** machine-coordinate moves appear **only** when `park_position` is configured
  (the end-of-program / tube-flip gantry park). With no `park_position`, the program is
  entirely work-coordinate and portable.

---

## 3. Machine Z-Axis Orientation

* Machine Z **increases upward** (standard).
* The safe retract height is a **work-coordinate** value above Z=0 (the sacrifice board):
  `z_reference.safe_height` if set, else `material_thickness + clearance`. No assumption is
  made about where machine Z=0 sits — that was the old `G53`-based behavior and is gone by
  default.
* If you enable `park_position`, then (and only then) the program assumes machine Z=0 is a
  safe high position, since the park raises to `G53 Z<park_z>`.

# Machine-Specific Assumptions for PenguinCAM-Generated G-Code

This document describes **implicit machine, controller, and interpreter assumptions** made by PenguinCAM when generating G-code.

These assumptions are **independent of user-configurable inputs** such as material, thickness, tool diameter, feeds/speeds, tab count, retract heights, or cut depth. If any assumption below is false on a target machine, the generated G-code may behave incorrectly or unsafely.

---

## 1. Controller & Motion Stack

* G-code is interpreted by **Mach4** or a Mach4-compatible controller.
* Motion control is provided by **Ethernet SmoothStepper (ESS)** or an equivalent Mach4-supported motion device.
* The controller supports standard Mach-style modal behavior and state persistence.

This G-code is **not directly portable** to GRBL, LinuxCNC, Fanuc, Haas, or other controllers without review.

---

## 2. Work vs Machine Coordinates

* **G54** is used for all programmed cutting motion.
* **G53** is used explicitly for machine-coordinate safety moves.
* The controller correctly supports `G53` as a non-modal machine-coordinate override.

---

## 3. Machine Z-Axis Orientation (Critical)

The program assumes:

* Machine Z **increases upward**.
* **Machine Z = 0.000** represents a **safe, high-clearance position**.
* Executing `G53 G0 Z0.` retracts the spindle upward, away from the work.

If machine Z=0 is at the bottom of travel or otherwise unsafe, gener

Before running PenguinCAM-generated G-code, confirm all of the following:
☐ Controller is Mach4-compatible (Mach4, ESS, or equivalent)
☐ Supports G53 machine-coordinate moves
☐ Machine Z increases upward
☐ Machine Z = 0 is a safe, high-clearance position
☐ G28 Z reference is configured and safe
☐ Supports G91.1 incremental arc centers
☐ Supports true helical interpolation (XYZ arcs)
☐ S words specify RPM, not percentage or Hz
☐ Spindle spin-up is protected by Mach4 delay or hardware feedback
☐ Rapids (G0) do not alter modal feed state
❌ If any box is unchecked, review or modify the post-processor before running.
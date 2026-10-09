Before running PenguinCAM-generated G-code, confirm the following.

Required for the default (portable, G54-only) output:
☐ Work zero (G54) set at the part origin: X=0/Y=0 at the lower-left, Z=0 at the sacrifice-board surface
☐ Controller supports G91.1 incremental arc centers
☐ Controller supports true helical interpolation (XYZ arcs)
☐ S words specify RPM (not percentage or Hz)
☐ Rapids (G0) do not alter modal feed state

Only if you set `park_position` in your config (this is what adds G53 machine moves):
☐ Controller supports G53 machine-coordinate moves
☐ Machine Z increases upward and machine Z=0 is a safe, high-clearance position
   → If your controller mishandles G53 (e.g. GRBL/Easel), REMOVE `park_position` from your config.
☐ Controller accepts two G-words in one block (`G53 G0 Z...`) - G53 is non-modal and cannot
   be split onto its own line. Carbide Motion rejects multi-G-word blocks, so leave
   `park_position` unset on a Shapeoko.

Only if you set `machine.coolant` (this is what adds M7/M8/M9):
☐ Controller supports the coolant M-codes (stock GRBL needs M7 compiled in)
   → If not, remove `coolant` (or set it to `None`).

Only if you set `machine.tube_work_coordinate_system` to something other than G54 (for a
permanently-fixtured tube jig):
☐ That WCS (e.g. G55) is pre-set in the controller to the jig origin (e.g. via G10 L2 P2)
   → An unset alternate WCS defaults to machine zero and will cut in the wrong place. If in
     doubt, omit the setting and zero G54 to the tube for each job instead.

Notes:
- Easel (Inventables) is not a supported target (rejects arcs and non-G54 offsets). On an
  Easel machine, send PenguinCAM output directly to the underlying GRBL controller instead.
❌ If a required box is unchecked, review or modify the post-processor before running.

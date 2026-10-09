# Tests package for PenguinCAM

# Arm strict G-code hygiene for the whole suite: any generated line that a CNC
# controller could not parse (nested parenthesis comment, non-ASCII, bracketed comment)
# fails the test that produced it, instead of being quietly repaired on the way out as
# it is in production. See gcode_hygiene.py and tests/test_gcode_hygiene.py.
import gcode_hygiene

gcode_hygiene.STRICT = True

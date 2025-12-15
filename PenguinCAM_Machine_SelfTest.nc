(PenguinCAM Machine Compatibility Self-Test)
(VERIFY ALL MOTION VISUALLY — NO CUTTING)

(--- Modal Baseline ---)
G90 G94 G91.1 G40 G49 G17
G20

(--- Test 1: Machine Z Safety ---)
(MSG,TEST 1: Moving to machine Z0 - must retract upward safely)
G53 G0 Z0.
G4 P1.0

(--- Test 2: G28 Z Reference ---)
(MSG,TEST 2: G28 Z move - must be safe retract/home)
G91
G28 Z0.
G90
G4 P1.0

(--- Test 3: Work Offset Independence ---)
(MSG,TEST 3: Activate G54 and confirm no motion)
G54
G4 P0.5

(--- Test 4: G53 XY Motion ---)
(MSG,TEST 4: G53 XY move - machine coordinates)
G53 G0 X1.0 Y1.0
G4 P1.0

(--- Test 5: Arc Semantics ---)
(MSG,TEST 5: Incremental IJK arc - should draw smooth circle)
G54
G0 X0.0 Y0.0 Z1.0
G2 X0.0 Y0.0 I1.0 J0.0 F20.0
G4 P1.0

(--- Test 6: Helical Interpolation ---)
(MSG,TEST 6: Helical arc - Z should move smoothly with arc)
G2 X0.0 Y0.0 I1.0 J0.0 Z0.5 F20.0
G4 P1.0

(--- Test Complete ---)
(MSG,Self-test complete. If all motions were safe and correct, machine is compatible.)
M30

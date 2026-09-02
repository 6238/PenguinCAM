"""Validate the feeds_speeds model.

Two independent checks:

(A) **Field regression.** PenguinCAM shipped hand-tuned feeds for a 4mm 1-flute tool
    in wood, plastic and aluminum. Those numbers came from cutting real parts, so a
    physics-first model that disagrees with them badly is wrong about the physics.
    The model is NOT tuned to reproduce them -- agreement is the evidence.

(B) **Physics invariants.** Properties that must hold for any inputs: the chip
    thinning formula against its closed form, the surface-speed round trip, and the
    monotonic responses (more engagement -> thinner-chip compensation falls, and so on).

Run:  uv run python validate_feeds_speeds.py
"""

import math
import sys

from feeds_speeds import (MATERIALS, calculate_feeds, chip_thinning_factor)

# Field data tolerance. The model derives feeds from published machinability data and
# is never fitted to these presets, so exact agreement is not expected or wanted.
FIELD_TOLERANCE = 0.20

# Hand-tuned presets from team_config.py, all for the 4mm 1-flute tool cutting a
# full-width profile through stock.
FIELD_PRESETS = [
    ('avid_pro2424', 'plywood',       75.0, 0.40),
    ('avid_pro2424', 'aluminum_6061', 55.0, 0.20),
    ('avid_pro2424', 'polycarbonate', 75.0, 0.25),
    ('omio_x8',      'plywood',       70.0, 0.40),
    ('omio_x8',      'aluminum_6061', 50.0, 0.20),
    ('omio_x8',      'polycarbonate', 70.0, 0.25),
]


def _check(failures, label, got, want, tolerance):
    delta = abs(got - want) / want if want else 0.0
    status = 'ok ' if delta <= tolerance else 'FAIL'
    if delta > tolerance:
        failures.append(f"{label}: got {got:.3f}, expected {want:.3f} "
                        f"({delta:.0%} off, tolerance {tolerance:.0%})")
    print(f"  [{status}] {label:<44} {got:>8.3f}  vs {want:>7.3f}  ({delta:>4.0%})")


def check_field_regression(failures):
    print("\n(A) Field regression against hand-tuned presets (4mm 1-flute, profile)")
    for machine, material, feed_expected, depth_expected in FIELD_PRESETS:
        r = calculate_feeds(machine, material, '4mm_1f', 'profile')
        _check(failures, f"{machine}/{material} feed IPM",
               r['feed'], feed_expected, FIELD_TOLERANCE)
        _check(failures, f"{machine}/{material} slot depth in",
               r['ap'], depth_expected, FIELD_TOLERANCE)


def check_chip_thinning(failures):
    print("\n(B1) Radial chip thinning against its closed form")
    diameter = 0.25
    for ratio in (0.05, 0.10, 0.25, 0.40, 0.50, 0.75, 1.00):
        got = chip_thinning_factor(ratio * diameter, diameter)
        if ratio >= 0.5:
            want = 1.0
        else:
            want = 1.0 / (2.0 * math.sqrt(ratio * (1.0 - ratio)))
        _check(failures, f"rctf at ae/D = {ratio:.2f}", got, want, 1e-9)


def check_surface_speed_roundtrip(failures):
    print("\n(B2) Surface speed round trip (RPM back to SFM)")
    for material, tool in (('plywood', '4mm_1f'), ('steel_1045', 'seco_c5131_4mm'),
                           ('aluminum_6061', '250_2f')):
        r = calculate_feeds('avid_pro2424', material, tool, 'pocket_adaptive')
        diameter = {'4mm_1f': 0.157, 'seco_c5131_4mm': 0.1575, '250_2f': 0.250}[tool]
        want = r['rpm'] * math.pi * diameter / 12.0
        _check(failures, f"{material} SFM from reported RPM",
               r['sfm_actual'], want, 0.01)


def check_monotonic(failures):
    print("\n(B3) Monotonic responses")
    base = dict(machine='avid_pro2424', material='steel_1045',
                tool='seco_c5131_4mm', operation='pocket_adaptive')

    # Lighter radial engagement must raise the thinning compensation.
    light = calculate_feeds(**base, ae_override=0.0100)
    heavy = calculate_feeds(**base, ae_override=0.0700)
    ok = light['chip_thinning_factor'] > heavy['chip_thinning_factor']
    print(f"  [{'ok ' if ok else 'FAIL'}] lighter ae raises thinning compensation "
          f"({light['chip_thinning_factor']:.2f} > {heavy['chip_thinning_factor']:.2f})")
    if not ok:
        failures.append("chip thinning compensation did not rise as ae fell")

    # A bigger tool in the same material must turn slower (surface speed is fixed).
    small = calculate_feeds('avid_pro2424', 'steel_1045', 'seco_c5131_4mm', 'slot')
    big = calculate_feeds('avid_pro2424', 'steel_1045', '250_4f_steel', 'slot')
    ok = big['rpm'] <= small['rpm']
    print(f"  [{'ok ' if ok else 'FAIL'}] larger diameter lowers RPM "
          f"({big['rpm']} <= {small['rpm']})")
    if not ok:
        failures.append("larger diameter did not lower RPM")

    # Every material's chipload must scale with diameter, never be a fixed number.
    for key in MATERIALS:
        r_small = calculate_feeds('avid_pro2424', key, '125_1f', 'pocket_conventional')
        r_big = calculate_feeds('avid_pro2424', key, '250_1f', 'pocket_conventional')
        if not r_big['fz_programmed'] > r_small['fz_programmed']:
            failures.append(f"{key}: chipload did not scale with tool diameter")
    print(f"  [ok ] chipload scales with diameter for all "
          f"{len(MATERIALS)} materials")


def check_slot_has_no_thinning(failures):
    print("\n(B4) A full-width slot gets no chip thinning benefit")
    r = calculate_feeds('avid_pro2424', 'steel_1045', 'seco_c5131_4mm', 'slot')
    ok = r['chip_thinning_factor'] == 1.0 and r['is_full_slot']
    print(f"  [{'ok ' if ok else 'FAIL'}] slot rctf = "
          f"{r['chip_thinning_factor']:.2f}, is_full_slot = {r['is_full_slot']}")
    if not ok:
        failures.append("full-width slot reported chip thinning")


def main():
    failures = []
    check_field_regression(failures)
    check_chip_thinning(failures)
    check_surface_speed_roundtrip(failures)
    check_monotonic(failures)
    check_slot_has_no_thinning(failures)

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("All feeds & speeds validations passed.")
    return 0


if __name__ == '__main__':
    sys.exit(main())

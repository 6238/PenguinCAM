# Generate TEST version with safety features
python safe_test_mode.py sample_part.dxf test_safe.gcode --thickness 0.25

# This automatically:
# ✓ Disables spindle (M3 → M5)
# ✓ Raises tool 2" above all operations
# ✓ Creates a safety checklist
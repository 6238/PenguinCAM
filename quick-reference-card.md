# 1. Visualize
Upload to ncviewer.com

# 2. Safe dry run
python safe_test_mode.py part.dxf test.gcode --thickness 0.25
# Run with spindle OFF

# 3. Foam test
python frc_cam_postprocessor.py part.dxf part.gcode --thickness 0.25
# Cut foam

# 4. Production
# Same command, use real material
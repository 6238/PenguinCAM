#!/bin/bash
# Batch process multiple DXF files
# Usage: ./batch_process.sh [thickness] [tabs]
# Example: ./batch_process.sh 0.25 4

THICKNESS=${1:-0.25}
TABS=${2:-4}

echo "========================================="
echo "FRC CAM Batch Processor"
echo "========================================="
echo "Material thickness: ${THICKNESS}\""
echo "Number of tabs: ${TABS}"
echo ""

# Process all DXF files in current directory
count=0
for dxf_file in *.dxf; do
    # Skip if no DXF files found
    [ -e "$dxf_file" ] || continue
    
    # Generate output filename
    gcode_file="${dxf_file%.dxf}.gcode"
    
    echo "Processing: $dxf_file → $gcode_file"
    python frc_cam_postprocessor.py "$dxf_file" "$gcode_file" \
        --thickness "$THICKNESS" \
        --tabs "$TABS"
    
    if [ $? -eq 0 ]; then
        echo "  ✓ Success"
        ((count++))
    else
        echo "  ✗ Failed"
    fi
    echo ""
done

echo "========================================="
echo "Processed $count files"
echo "========================================="

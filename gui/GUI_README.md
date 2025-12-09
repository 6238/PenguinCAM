# FRC CAM Post-Processor - Web GUI

A modern web-based interface for generating G-code from OnShape DXF exports. Works on Windows, Mac, and Linux - just open your browser!

## Features

✨ **Easy to Use**
- Drag & drop DXF files
- Visual parameter controls
- Real-time 3D G-code preview
- One-click download

🎨 **Beautiful Interface**
- Dark mode optimized
- Professional robotics aesthetic
- Step-by-step workflow
- Responsive design

🔧 **Powerful**
- All post-processor features
- Smart tab placement
- Tool compensation
- Sacrifice board zeroing

## Quick Start

### 1. Install Dependencies

```bash
pip install -r requirements_gui.txt
```

This installs:
- Flask (web framework)
- ezdxf (DXF parsing)
- shapely (geometry operations)

### 2. Run the Server

```bash
python frc_cam_gui_app.py
```

You'll see:
```
======================================================================
FRC CAM Post-Processor - Web GUI
======================================================================

Post-processor script: /path/to/frc_cam_postprocessor.py
Temporary directory: /tmp/...

🚀 Starting server...
📂 Open your browser to: http://localhost:5000

⚠️  Press Ctrl+C to stop the server

======================================================================
```

### 3. Open Your Browser

Navigate to: **http://localhost:5000**

The GUI will open automatically!

## Using the GUI

### Step 1: Upload DXF File

**Option A: Drag & Drop**
- Drag your DXF file from OnShape onto the upload area
- File info will appear when loaded

**Option B: Click to Browse**
- Click the upload area
- Select your DXF file from the file picker

### Step 2: Set Parameters

Configure your cutting parameters:

**Material Thickness** (inches)
- Default: 0.25" (1/4" material)
- Examples: 0.125" (1/8"), 0.5" (1/2")

**Tool Diameter** (inches)
- Default: 0.157" (4mm end mill)
- Critical for accurate parts!
- Common: 0.125" (1/8"), 0.25" (1/4")

**Sacrifice Board Depth** (inches)
- Default: 0.02" (how far to cut into board)
- Ensures complete cut-through
- Increase to 0.03" for flex materials

**Number of Tabs**
- Default: 4
- Holds part during cutting
- Placed intelligently on straight sections

**Center Drill Screw Holes** (checkbox)
- Faster: Center drill only
- Unchecked: Mill out holes (more accurate)

### Step 3: Generate G-code

Click **"🚀 Generate G-code"**

The processor will:
1. Parse your DXF file
2. Detect holes and features
3. Apply tool compensation
4. Place tabs smartly
5. Generate optimized G-code

**Results show:**
- Number of screw holes detected
- Number of bearing holes detected
- Total G-code lines
- Console output (expandable)

### Step 4: Preview & Download

**3D Visualization:**
- Orange line shows tool path
- Mouse: Click and drag to rotate
- Scroll: Zoom in/out
- Button: Reset view to default

**Verify:**
- Tool path looks correct
- Holes in right places
- Perimeter complete
- Tabs on straight sections

**Download:**
- Click **"Download G-code File"**
- Save to your computer
- Load into CNC controller

## File Structure

```
your-project/
├── frc_cam_gui_app.py          # Flask web server
├── frc_cam_postprocessor.py    # Post-processor CLI (required!)
├── requirements_gui.txt         # Python dependencies
├── templates/
│   └── index.html              # Web interface
└── static/                      # (auto-created for uploads)
```

**Important:** The GUI requires `frc_cam_postprocessor.py` to be in the same directory!

## Troubleshooting

### "Module not found" errors

Install dependencies:
```bash
pip install -r requirements_gui.txt
```

### "Post-processor not found"

Make sure `frc_cam_postprocessor.py` is in the same directory as `frc_cam_gui_app.py`:
```bash
ls -l frc_cam_*.py
```

Should show both files.

### "Cannot connect to server"

Check that the server started without errors. Look for:
```
* Running on http://0.0.0.0:6238
```

If port 6238 is in use, edit `frc_cam_gui_app.py` and change:
```python
app.run(debug=True, host='0.0.0.0', port=6238)  # Change 6238 to another port
```

### "File upload failed"

Check file size (<50MB) and format (.dxf only).

### "Processing timeout"

Very complex DXF files may timeout. Try:
- Simplifying your design in CAD
- Reducing spline/curve complexity
- Splitting into multiple files

## Advanced Usage

### Running on a Different Port

Edit `frc_cam_gui_app.py`:
```python
app.run(debug=True, host='0.0.0.0', port=8080)  # Use port 8080
```

### Accessing from Other Computers

By default, the server listens on all network interfaces (`0.0.0.0`).

To access from another computer on your network:
1. Find your computer's IP address:
   - Windows: `ipconfig`
   - Mac/Linux: `ifconfig` or `ip addr`
2. On the other computer, browse to: `http://YOUR_IP:6238`

Example: `http://192.168.1.100:6238`

### Production Deployment

For team-wide deployment on a dedicated server:

1. **Use a production WSGI server:**
```bash
pip install gunicorn
gunicorn -w 4 -b 0.0.0.0:6238 frc_cam_gui_app:app
```

2. **Add authentication** (if needed):
Consider adding basic auth or team login.

3. **Set up reverse proxy:**
Use nginx or Apache for HTTPS and better performance.

## Security Notes

⚠️ **This is a LOCAL development server, not for internet deployment!**

- Only run on trusted networks (your team's network)
- Don't expose to the internet without proper security
- File uploads are temporary and cleaned up automatically
- No authentication by default

## Performance

**Upload Limits:**
- Max file size: 50MB
- Processing timeout: 30 seconds

**Typical Performance:**
- Small parts (<100 features): <1 second
- Medium parts (100-500 features): 1-5 seconds
- Large parts (500+ features): 5-30 seconds

## Browser Compatibility

**Recommended:**
- Chrome 90+
- Firefox 88+
- Safari 14+
- Edge 90+

**Required Features:**
- JavaScript enabled
- WebGL support (for 3D visualization)
- File API support
- Fetch API support

## Tips & Tricks

### Batch Processing

Keep the server running and process multiple files:
1. Generate first part
2. Download G-code
3. Upload next DXF immediately
4. Parameters persist between files

### Parameter Presets

Common parameter sets:

**Aluminum 1/4":**
```
Thickness: 0.25"
Tool: 0.157" (4mm)
Sacrifice: 0.02"
Tabs: 4
```

**Polycarbonate 1/8":**
```
Thickness: 0.125"
Tool: 0.157" (4mm)
Sacrifice: 0.03" (more overcut)
Tabs: 6 (more tabs)
```

**Wood 1/2":**
```
Thickness: 0.5"
Tool: 0.25" (1/4")
Sacrifice: 0.03"
Tabs: 4
```

### Visualization Tips

**View Angles:**
- Top view: Good for checking XY layout
- Side view: See Z-heights (tabs, depths)
- Isometric: Overall toolpath overview

**Look For:**
- ✓ Tabs on straight sections (not curves)
- ✓ Tool clears all features
- ✓ No collisions or overlaps
- ✓ Perimeter closed loop
- ✓ Holes at correct locations

## Comparison to CLI

**GUI Advantages:**
- Visual feedback
- Easier parameter adjustment
- 3D preview before cutting
- No command line needed
- Drag & drop convenience

**CLI Advantages:**
- Automation/scripting
- Batch processing (multiple files)
- Integration with other tools
- Lower memory usage

**Both share the same core engine!** Results are identical.

## Keyboard Shortcuts

- `Ctrl+V` / `Cmd+V`: Paste file path (in some browsers)
- `Esc`: Cancel file upload
- Mouse wheel: Zoom visualization
- Click+Drag: Rotate visualization

## What's Next?

After downloading your G-code:

1. **Review the file** in a text editor
2. **Visualize** in NCViewer (optional, already previewed)
3. **Setup machine:**
   - Zero X/Y to lower-left corner of material
   - Zero Z to sacrifice board surface
4. **Dry run** with spindle off, Z raised
5. **Cut** your part!

## Support

Issues? Check:
- Console output in the GUI (expandable section)
- Terminal where you ran `python frc_cam_gui_app.py`
- Browser console (F12 → Console tab)

Common fixes:
- Restart the server
- Clear browser cache
- Check file permissions
- Verify Python dependencies installed

## Updates

To update the post-processor:
1. Stop the server (Ctrl+C)
2. Replace `frc_cam_postprocessor.py` with new version
3. Restart: `python frc_cam_gui_app.py`

No need to update the GUI unless HTML changes!

---

**Happy CNC cutting!** 🤖⚙️🔧

For more info, see the main post-processor documentation:
- README.md
- QUICK_START.md
- MACHINE_ZERO_SETUP.md

# PenguinCAM

**FRC Team 6238 Popcorn Penguins CAM Post-Processor**

A web-based tool for FRC robotics teams to automatically generate CNC G-code from OnShape designs. No CAM software required!

🔗 **Live App:** https://penguincam.popcornpenguins.com

---

## What is PenguinCAM?

PenguinCAM streamlines the workflow from CAD design to CNC machining for FRC teams:

1. **Design in OnShape** → Create flat plates with holes and pockets
2. **Right-click → "Send to PenguinCAM"** → One-click export from OnShape
3. **Orient & Generate** → Rotate part, auto-generate toolpaths
4. **Download or Save to Drive** → Ready to run on your CNC router

**No CAM software, no manual exports, no face selection required!** PenguinCAM knows what FRC teams need.

---

## Features

### 🤖 **Built for FRC**

✅ **Automatic hole detection:**
- #10 screw holes (0.19" diameter)
- 1.125" bearing holes  
- Non-standard holes milled as circular pockets

✅ **Smart perimeter cutting:**
- Holding tabs prevent parts from flying away
- Tabs only on straight sections (avoids curves)
- Configurable tab count

✅ **Pocket recognition:**
- Auto-detects inner boundaries
- Generates clearing toolpaths

✅ **Tool compensation built-in:**
- Parts come out the correct size
- Perimeter offset outward, pockets offset inward

### 🔗 **OnShape Integration** ⭐ Preferred Workflow

**One-Click Export from OnShape:**
- Right-click part in OnShape → "Send to PenguinCAM"
- Auto-detects top face (highest Z position)
- Opens PenguinCAM with part already loaded
- No manual DXF export or face selection needed

**How to Set Up:**
1. Install PenguinCAM extension in your OnShape company
2. Extension appears in right-click menu on parts
3. Click once to send part directly to PenguinCAM
4. OAuth authentication (one-time per team member)

**Alternative:** Manual DXF upload still available for offline work

### 🔐 **Team Access Control**

- Google Workspace authentication
- Restrict to your domain (e.g., @popcornpenguins.com)
- Secure OAuth 2.0 login

### 💾 **Google Drive Integration**

- Upload G-code directly to team Shared Drive
- All team members can access files
- Files persist when students graduate

### 📊 **Visualization & Setup**

**2D Setup View:**
- Orient your part before generating G-code
- Rotate in 90° increments to match stock orientation
- Origin automatically set to bottom-left (X→ Y↑)
- Familiar workflow for 3D printer slicer / laser cutter users

**3D Toolpath Preview:**
- Interactive preview with cutting tool animation
- Scrubber to step through each move
- See completed vs. upcoming cuts
- Verify holes, pockets, and perimeter

---

## Quick Start for Students

### Method 1: From OnShape (Recommended) ⭐

**One-Click Workflow:**
1. **Design your part** in OnShape (flat plate with holes/pockets)
2. **Right-click the part** in the feature tree
3. **Click "Send to PenguinCAM"** from the menu
4. **Orient your part** - Rotate if needed in 2D setup view
5. **Click "Generate G-code"** - Review 3D preview
6. **Download or save to Drive** - Ready for CNC!

**First Time Setup:** You'll be asked to authenticate with Google (one time per team member)

### Method 2: Manual DXF Upload

**For offline work or non-OnShape files:**
1. **Export DXF** from your CAD software
2. **Visit** https://penguincam.popcornpenguins.com
3. **Upload DXF file** via drag-and-drop
4. **Orient & generate** - Same as above
5. **Download or save to Drive**

### Running on the CNC

- Load G-code into your CNC controller
- Set up material and zero axes (see [Quick Reference](docs/quick-reference-card.md))
- Run the program!

---

## For Mentors & Setup

### Deployment

PenguinCAM is deployed on Railway with automatic GitHub integration.

**Setup guides:**
- [Deployment Guide](docs/DEPLOYMENT_GUIDE.md) - Deploy to Railway, environment variables
- [Authentication Guide](docs/AUTHENTICATION_GUIDE.md) - Google OAuth and Workspace setup
- [Integrations Guide](docs/INTEGRATIONS_GUIDE.md) - OnShape and Google Drive configuration
- [OnShape Extension Setup](docs/ONSHAPE_SETUP.md) - Install one-click export in OnShape ⭐

### Documentation

**For daily use:**
- [Quick Reference Card](docs/quick-reference-card.md) - Cheat sheet for students and mentors

**Technical references:**
- [Tool Compensation Guide](docs/TOOL_COMPENSATION_GUIDE.md) - How tool offsets work
- [Z-Coordinate System](docs/Z_COORDINATE_SYSTEM.md) - Sacrifice board zeroing explained

**Planning:**
- [Roadmap](ROADMAP.md) - Future features and improvements

### Requirements

**Runtime dependencies:**
```
Flask>=3.0.0
gunicorn>=21.2.0
requests>=2.31.0
ezdxf>=1.0.0
shapely>=2.0.0
google-auth>=2.23.0
google-auth-oauthlib>=1.1.0
google-api-python-client>=2.100.0
```

See `requirements.txt` for complete list.

---

## How It Works

### The Pipeline

```
OnShape Part Studio
    ↓ (OAuth API)
DXF Export
    ↓ (Automatic face detection)
Geometry Analysis
    ↓ (Hole detection, path generation)
G-code Generation
    ↓ (Tool compensation)
3D Preview + Download
    ↓ (Optional)
Google Drive Upload
```

### Key Components

**Backend (Python):**
- `frc_cam_gui_app.py` - Flask web server
- `frc_cam_postprocessor.py` - G-code generation engine
- `onshape_integration.py` - OnShape API client
- `google_drive_integration.py` - Drive uploads
- `penguincam_auth.py` - Google OAuth authentication

**Frontend:**
- `templates/index.html` - Web interface with Three.js visualization
- `static/popcornlogo.png` - Team branding

**Configuration:**
- `Procfile` - Railway deployment config
- `requirements.txt` - Python dependencies
- Environment variables - Secrets and API keys

---

## Technical Details

### G-code Operations

PenguinCAM generates optimized toolpaths:

1. **Screw holes (#10):**
   - Center drill operation (fast)
   - Or full mill if disabled

2. **Bearing holes (1.125"):**
   - Helical bore from center
   - Compensated for exact final diameter

3. **Non-standard holes:**
   - Milled as circular pockets
   - 32-point circular path

4. **Pockets:**
   - Offset inward by tool radius
   - Full-depth plunge and trace

5. **Perimeter:**
   - Offset outward by tool radius
   - Cut with holding tabs
   - Tabs only on straight sections

### Z-Axis Coordinate System

**Z=0 is at the SACRIFICE BOARD (bottom), not material top.**

This ensures:
- ✅ Consistent setup across jobs
- ✅ Guaranteed cut-through (0.02" overcut)
- ✅ No math required when changing material thickness

See [Z_COORDINATE_SYSTEM.md](docs/Z_COORDINATE_SYSTEM.md) for details.

### Tool Compensation

All toolpaths are automatically compensated:

- **Perimeter:** Offset OUT by tool radius → final part matches CAD size
- **Pockets:** Offset IN by tool radius → pockets match CAD size  
- **Holes:** Reduced radius → final holes match CAD diameter

See [TOOL_COMPENSATION_GUIDE.md](docs/TOOL_COMPENSATION_GUIDE.md) for math and examples.

---

## Default Settings

**Material:**
- Thickness: 0.25" (configurable)
- Sacrifice board overcut: 0.02"

**Tool:**
- Diameter: 0.157" (4mm endmill)
- Common alternatives: 1/8" (0.125"), 1/4" (0.250")

**Feeds & Speeds:**
- Feed rate: 30 IPM
- Plunge rate: 10 IPM
- Safe height: 0.1" above material top

**Tabs:**
- Count: 4 (evenly spaced)
- Width: 0.25"
- Height: 0.03" (material left in tab)

These can be customized in the code for your specific machine and materials.

---

## Repository Structure

```
penguincam/
├── README.md                      # This file
├── ROADMAP.md                     # Future plans
├── requirements.txt               # Python dependencies
├── Procfile                       # Railway deployment
│
├── docs/                          # Documentation
│   ├── DEPLOYMENT_GUIDE.md        # Setup & deployment
│   ├── AUTHENTICATION_GUIDE.md    # Google OAuth
│   ├── INTEGRATIONS_GUIDE.md      # OnShape & Drive
│   ├── quick-reference-card.md    # Quick start
│   ├── TOOL_COMPENSATION_GUIDE.md # Technical reference
│   └── Z_COORDINATE_SYSTEM.md     # Zeroing guide
│
├── static/                        # Static assets
│   └── popcornlogo.png            # Team logo
│
├── templates/                     # HTML templates
│   └── index.html                 # Main web interface
│
├── frc_cam_gui_app.py            # Flask web server
├── frc_cam_postprocessor.py      # G-code generator
├── onshape_integration.py        # OnShape API
├── google_drive_integration.py   # Drive uploads
├── penguincam_auth.py            # OAuth authentication
│
└── [config files...]              # Various JSON configs
```

---

## Development

### Local Testing

1. **Clone repository:**
   ```bash
   git clone https://github.com/your-team/penguincam.git
   cd penguincam
   ```

2. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

3. **Set environment variables:**
   ```bash
   export GOOGLE_CLIENT_ID=your-client-id
   export GOOGLE_CLIENT_SECRET=your-secret
   export ONSHAPE_CLIENT_ID=your-onshape-id
   export ONSHAPE_CLIENT_SECRET=your-onshape-secret
   export BASE_URL=http://localhost:6238
   export AUTH_ENABLED=false  # Skip auth for local testing
   ```

4. **Run locally:**
   ```bash
   python frc_cam_gui_app.py
   ```

5. **Visit:** http://localhost:6238

### Deployment

Push to `main` branch → Railway auto-deploys

See [DEPLOYMENT_GUIDE.md](docs/DEPLOYMENT_GUIDE.md) for complete setup.

---

## Contributing

PenguinCAM was built for FRC Team 6238 but is open for other teams to use and improve!

**Ideas welcome:**
- Additional hole sizes
- More sophisticated pocket clearing
- Multi-tool support
- Material presets
- Better tab algorithms

See [ROADMAP.md](ROADMAP.md) for planned features.

---

## License

[Add your license here]

---

## Credits

**Built by FRC Team 6238 Popcorn Penguins**

For questions or support:
- GitHub Issues: [Your repo URL]
- Team mentor: [Your contact]

---

## For Other FRC Teams

Interested in using PenguinCAM for your team? Great!

**Setup steps:**
1. Fork this repository
2. Follow [DEPLOYMENT_GUIDE.md](docs/DEPLOYMENT_GUIDE.md) to deploy on Railway
3. Configure Google OAuth for your Workspace
4. Set up OnShape API credentials
5. Customize for your team (logo, domain, etc.)

The setup takes about 1-2 hours but then requires minimal maintenance. Your students will love the streamlined workflow!

---

**Go Popcorn Penguins! 🍿🐧**

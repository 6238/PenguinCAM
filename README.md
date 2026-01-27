# PenguinCAM

**Onshape-to-CNC for FRC Teams**

A web-based tool for FRC robotics teams to automatically generate CNC G-code from Onshape designs. No CAM software required! Built by Team 6238, hosted for all FRC teams.

🔗 **Demo video:**  
[![Demo video](https://img.youtube.com/vi/gFReFDz-_LI/0.jpg)](https://youtu.be/zPZCTVh2n2Q)


🔗 **Live app:** https://penguincam.popcornpenguins.com

---

## What is PenguinCAM?

PenguinCAM streamlines the workflow from CAD design to CNC machining for FRC teams:

1. **Design in Onshape** → Create flat plates with holes and pockets
2. **Right-click → "Send to PenguinCAM"** → One-click export from Onshape
3. **Orient & Generate** → Rotate part, auto-generate toolpaths
4. **Download or Save to Drive** → Ready to run on your CNC router

**No CAM software, no manual exports, no face selection required!** PenguinCAM knows what FRC teams need.

Designed to feel like 3D printer slicers or laser cutter software. Get the design, orient it on the machine, and go. Launching directly from Onshape means no export/import steps, lost files or inconsistent naming.

**Multi-team support:** Other teams can use the hosted service at https://penguincam.popcornpenguins.com! Just upload a `PenguinCAM-config.yaml` file to your Onshape documents to customize settings for your CNC machine. See "For Other FRC Teams" below.

---

## Features

### 🤖 **Built for FRC**

✅ **Automatic hole detection:**
- All circular holes (preserves exact CAD dimensions)
- #10 screw holes, bearing holes, or custom sizes
- Helical entry + spiral clearing strategy

✅ **Smart perimeter cutting of plates:**
- Holding tabs prevent parts from flying away
- Configurable tab count

✅ **Pocket recognition:**
- Auto-detects inner boundaries
- Generates clearing toolpaths

✅ **Aluminum tubing support:**
- Tube mounts in tubing jig
- No X/Y zeroing required
- Flip part halfway through
- Automatically squares off near end
- Automatically cuts tube to CAD length
- Pattern mirrored and cut in both top and bottom faces

### 🔗 **Onshape Integration** ⭐ Preferred Workflow

**One-Click Export from Onshape:**
- Right-click part in Onshape → "Send to PenguinCAM"
- Auto-detects top face
- Opens PenguinCAM with part already loaded
- No manual DXF export or face selection needed

**How to Set Up:**
1. Install PenguinCAM extension in your Onshape classroom
2. Extension appears in right-click "Applications" menu on parts
3. Click once to send part directly to PenguinCAM
4. OAuth authentication (one-time per team member)

**Alternative:** Manual DXF upload available for offline work

### 🔐 **Team Access Control**

- Google Workspace authentication
- Restrict to your domain (e.g., @popcornpenguins.com)
- Secure OAuth 2.0 login

### 💾 **Google Drive Integration**

- Upload G-code directly to team Shared Drive
- Easily accessible from CNC computer
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
- Verify toolpath for holes, pockets, and perimeter

---

## Quick Start for Students

### Method 1: From Onshape (Recommended) ⭐

**One-Click Workflow:**
1. **Design your part** in Onshape (flat plate with holes/pockets)
2. **Right-click the part** in the feature tree
3. **Click "Send to PenguinCAM"** from the Applications menu item
4. **Orient your part** - Rotate if needed in 2D setup view
5. **Click "Generate Program"** - Review 3D preview
6. **Download or save to Drive** - Ready for CNC!

**First Time Setup:** You'll be asked to authenticate with Google (one time per team member)

### Method 2: Manual DXF Upload

**For offline work or non-Onshape files:**
1. **Export DXF** from your CAD software
2. **Visit** https://penguincam.popcornpenguins.com
3. **Upload DXF file** via drag-and-drop
4. **Orient & generate** - Same as above
5. **Download or save to Drive**

### Running on the CNC

- Load G-code into your Mach4 on the CNC controller
- Set up material and zero axes (see [Quick Reference](docs/quick-reference-card.md))
- Run the program!

---

## For Mentors & Setup

### Team Configuration (5 minutes)

**For teams using the hosted service:**

Create a `PenguinCAM-config.yaml` file to customize machine settings for your CNC:
- Download template: [`PenguinCAM-config-template.yaml`](https://github.com/6238/PenguinCAM/blob/main/PenguinCAM-config-template.yaml)
- Edit for your team (machine park position, controller type, feeds/speeds, etc.)
- Save as `PenguinCAM-config.yaml` and drag into your Onshape documents folder
- Done! Your settings load automatically when team members use PenguinCAM

See "For Other FRC Teams" section below for complete instructions.

### Deployment (Advanced)

For teams self-hosting PenguinCAM, it's deployed on Railway with automatic GitHub integration.

**Setup guides:**
- [Deployment Guide](docs/DEPLOYMENT_GUIDE.md) - Deploy to Railway, environment variables
- [Authentication Guide](docs/AUTHENTICATION_GUIDE.md) - Google OAuth and Workspace setup
- [Integrations Guide](docs/INTEGRATIONS_GUIDE.md) - Onshape and Google Drive configuration
- [Onshape Extension Setup](docs/ONSHAPE_SETUP.md) - Install one-click export in Onshape ⭐

### Documentation

**For daily use:**
- [Quick Reference Card](docs/quick-reference-card.md) - Cheat sheet for students and mentors

**Technical references:**
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
Onshape Part Studio
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
- `onshape_integration.py` - Onshape API client
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

1. **Holes (all sizes):**
   - Helical entry from center
   - Spiral clearing to final diameter
   - Compensated for exact CAD dimensions
   - Works for #10 screws, bearings, or custom

2. **Pockets:**
   - Offset inward by tool radius
   - Helical entry, then clearing passes
   - Spiral strategy for circular pockets

3. **Perimeter:**
   - Offset outward by tool radius
   - Cut with holding tabs

### Z-Axis Coordinate System

**Z=0 is at the SACRIFICE BOARD (bottom), not material top.**

This ensures:
- ✅ Consistent Z-axis setup across jobs
- ✅ Guaranteed cut-through (0.02" overcut)
- ✅ No math required when changing material thickness

See [Z_COORDINATE_SYSTEM.md](docs/Z_COORDINATE_SYSTEM.md) for details.

---

## Default Settings

PenguinCAM uses Team 6238 defaults optimized for FRC robotics:

**Material:**
- Thickness: 0.25" (configurable per job)
- Sacrifice board overcut: 0.008"

**Tool:**
- Default diameter: 0.157" (4mm endmill)
- Common alternatives: 1/8" (0.125"), 1/4" (0.250")

**Feeds & Speeds:**
- Plywood: 75 IPM cutting, 18,000 RPM
- Aluminum: 55 IPM cutting, 18,000 RPM
- Polycarbonate: 75 IPM cutting, 18,000 RPM

**Tabs:**
- Width: 0.25"
- Height: 0.1" (material left in tab)
- Spacing: ~6" (automatic placement)

**Machine:**
- Park position: X0.5 Y23.5 (machine coordinates)
- Controller: Mach4

**All settings can be customized** per team using a `PenguinCAM-config.yaml` file in your Onshape documents. See "For Other FRC Teams" section below for setup instructions.

No code changes required - just upload your config file!

---

## Repository Structure

```
penguincam/
├── README.md                          # This file
├── ROADMAP.md                         # Future plans
├── requirements.txt                   # Python dependencies
├── Procfile                           # Railway deployment
├── PenguinCAM-config-template.yaml    # Team configuration template
│
├── docs/                              # Documentation
│   ├── DEPLOYMENT_GUIDE.md            # Setup & deployment
│   ├── AUTHENTICATION_GUIDE.md        # Google OAuth
│   ├── INTEGRATIONS_GUIDE.md          # Onshape & Drive
│   ├── quick-reference-card.md        # Quick start
│   ├── TOOL_COMPENSATION_GUIDE.md     # Technical reference
│   └── Z_COORDINATE_SYSTEM.md         # Zeroing guide
│
├── static/                            # Static assets
│   └── popcornlogo.png                # Team logo
│
├── templates/                         # HTML templates
│   └── index.html                     # Main web interface
│
├── frc_cam_gui_app.py                # Flask web server
├── frc_cam_postprocessor.py          # G-code generator
├── team_config.py                     # Team configuration management
├── onshape_integration.py            # Onshape API
├── google_drive_integration.py       # Drive uploads
├── penguincam_auth.py                # OAuth authentication
│
└── [config files...]                  # Various JSON configs
```

---

## Development

### Local Testing

We use [uv](https://docs.astral.sh/uv/) for fast Python dependency management. This works well with git worktrees since packages are cached globally.

1. **Clone repository:**
   ```bash
   git clone https://github.com/your-team/penguincam.git
   cd penguincam
   ```

2. **Install dependencies:**
   ```bash
   make install
   ```
   This installs `uv` if needed, creates a `.venv`, and installs all dependencies.

3. **Run G-code tests:**
   ```bash
   make test
   ```

4. **Set environment variables** (for running the web app):
   ```bash
   export GOOGLE_CLIENT_ID=your-client-id
   export GOOGLE_CLIENT_SECRET=your-secret
   export ONSHAPE_CLIENT_ID=your-onshape-id
   export ONSHAPE_CLIENT_SECRET=your-onshape-secret
   export BASE_URL=http://localhost:6238
   export AUTH_ENABLED=false  # Skip auth for local testing
   ```

5. **Run locally:**
   ```bash
   uv run python frc_cam_gui_app.py
   ```

6. **Visit:** http://localhost:6238

### Deployment

Push to `main` branch → Railway auto-deploys

See [DEPLOYMENT_GUIDE.md](docs/DEPLOYMENT_GUIDE.md) for complete setup.

---

## Contributing

PenguinCAM was built for FRC Team 6238 but is open for other teams to use and improve!

**Ideas welcome:**
- Multiple parts in a single job
- More sophisticated pocket clearing
- Better tab algorithms

See [ROADMAP.md](ROADMAP.md) for planned features.

---

## License

This project is licensed under the [MIT License](LICENSE.txt).

---

## Credits

**Built by FRC Team 6238 Popcorn Penguins**

For questions or support:
- GitHub Issues: https://github.com/6238/PenguinCAM/issues
- Team mentor: Josh Sirota <josh@popcornpenguins.com>

---

## For Other FRC Teams

Interested in using PenguinCAM for your team? Great! **You can use the hosted service at https://penguincam.popcornpenguins.com** - no deployment required!

### Recommended Approach: Use the Hosted Service

PenguinCAM is designed to support multiple teams using the same hosted instance. Each team can customize machine settings, feeds/speeds, and other preferences using a configuration file stored in your Onshape documents.

**Setup steps (5 minutes):**

1. **Download the configuration template:**
   - Get [`PenguinCAM-config-template.yaml`](https://github.com/6238/PenguinCAM/blob/main/PenguinCAM-config-template.yaml) from this repository

2. **Edit for your team:**
   - Update team number and name
   - Configure your CNC machine settings (park position, controller type)
   - Customize feeds/speeds if needed (optional - defaults work for most teams)
   - All values are optional! Only specify what you want to override from Team 6238 defaults

3. **Upload to Onshape:**
   - Save your edited file as `PenguinCAM-config.yaml` (exact name required)
   - Drag the file into your team's Onshape documents folder (at root level, not in a subfolder)
   - The file should appear alongside your parts and assemblies

4. **Authenticate:**
   - Visit https://penguincam.popcornpenguins.com
   - Sign in with Google (one-time setup per team member)
   - Your team's configuration will be automatically loaded!

**What you can customize:**
- Machine park position (important! - set for your specific CNC)
- CNC controller type (Mach3, Mach4, LinuxCNC, etc.)
- Default tool diameter shown in UI
- Feeds, speeds, and ramp angles per material
- Tab sizes and spacing
- Z-axis reference heights
- Google Drive integration settings

**Example:** If you have an Omio X-8 with Mach3, you only need to override:
```yaml
team:
  number: 1234
  name: "Your Team Name"
machine:
  name: "Omio X-8"
  controller: "Mach3"
  park_position:
    x: 1.0
    y: 30.0
```

All other values automatically use proven Team 6238 defaults.

### Advanced: Self-Hosting (Optional)

Want to run your own instance? You can deploy PenguinCAM yourself:

**Setup steps:**
1. Fork this repository
2. Follow [DEPLOYMENT_GUIDE.md](docs/DEPLOYMENT_GUIDE.md) to deploy on Railway
3. Configure Google OAuth for your Workspace
4. Set up Onshape API credentials
5. Customize branding (logo, domain, etc.)

The setup takes about 1-2 hours but then requires minimal maintenance.

**Most teams should use the hosted service above** - it's simpler and includes automatic updates!

---

**Go Popcorn Penguins! 🍿🐧**

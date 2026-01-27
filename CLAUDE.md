# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

PenguinCAM is a web-based CAM post-processor that generates CNC G-code from DXF files exported from Onshape. Built for FRC Team 6238, it automates the CAD-to-CNC workflow for flat plates without requiring CAM software.

**Live deployment:** https://penguincam.popcornpenguins.com

## Development Commands

**Important**: This project uses `uv` for Python environment management. All Python commands should be run with `uv run`:

```bash
# Install dependencies
make install

# Run development server (opens http://localhost:6238)
uv run python frc_cam_gui_app.py

# Run G-code comparison tests
make test

# Run postprocessor directly (CLI)
uv run python frc_cam_postprocessor.py INPUT.dxf OUTPUT.nc \
  --material plywood \
  --thickness 0.25 \
  --tool-diameter 0.157

# Test any Python module import
uv run python -c "from frc_cam_gui_app import app; print('OK')"
```

## Development Rules

**Always run `make test` after making any code changes.** If tests fail, fix the errors before proceeding with other work. Do not commit or consider a change complete until all tests pass.

## Architecture

```
Browser (index.html + Three.js)
    ↓ HTTP POST /process
Flask Server (frc_cam_gui_app.py)
    ↓ subprocess
G-code Generator (frc_cam_postprocessor.py)
    ↓
.nc file → 3D visualization / download / Drive upload
```

**Key files:**
- `frc_cam_gui_app.py` - Flask routes, Onshape OAuth, Drive integration
- `frc_cam_postprocessor.py` - Core G-code generation (`FRCPostProcessor` class)
- `templates/index.html` - Single-page app with Three.js 3D visualization
- `onshape_integration.py` - Onshape API client for one-click export
- `penguincam_auth.py` - Google Workspace OAuth (optional)
- `google_drive_integration.py` - Drive upload (optional)

## Documentation

Detailed documentation lives in the `docs/` directory. **Read these before modifying related code:**

| File | When to Read |
|------|--------------|
| `Z_COORDINATE_SYSTEM.md` | Modifying Z-axis calculations, safe heights, cut depths, or plunge moves |
| `TOOL_COMPENSATION_GUIDE.md` | Changing offset logic for perimeters, pockets, or holes |
| `ASSUMPTIONS.md` | Adding/changing G-code output; lists controller compatibility requirements |
| `MACHINE_CHECKLIST.md` | Updating G-code header comments or safety checks |
| `DEPLOYMENT_GUIDE.md` | Changing environment variables, Railway config, or OAuth redirect URIs |
| `AUTHENTICATION_GUIDE.md` | Modifying Google OAuth flow, session handling, or domain restrictions |
| `INTEGRATIONS_GUIDE.md` | Changing Onshape API calls or Google Drive upload logic |
| `ONSHAPE_SETUP.md` | Updating the Onshape browser extension or import URL format |
| `quick-reference-card.md` | Changing UI workflows or default settings that affect end users |

## Testing

Tests compare PenguinCAM output against Fusion 360 CAM output using the `pygcode` library. Test fixtures are DXF files with known expected G-code.

```bash
make test  # Runs all comparison tests
```

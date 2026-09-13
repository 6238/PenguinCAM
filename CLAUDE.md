# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

PenguinCAM is a web-based CAM post-processor that generates CNC G-code from DXF files exported from Onshape. Built for FRC Team 6238, it automates the CAD-to-CNC workflow for flat plates without requiring CAM software.

**Live deployment:** https://penguincam.popcornpenguins.com

## Development Commands

**Important**: This project uses `uv` for Python environment management locally. All Python commands should be run with `uv run`:

```bash
# Install dependencies, and Orca Slicer into tools/orca/
# (the Orca version is pinned in scripts/install-orca.sh)
make install

# Run development server (opens http://localhost:6238)
uv run python frc_cam_gui_app.py

# Run the fast tests: unit, system and node tests, no slicer
make test-quick

# Run everything, including the real Orca slice (the gate before a change is done)
make test

# Run postprocessor directly (CLI)
uv run python frc_cam_postprocessor.py INPUT.dxf OUTPUT.nc \
  --material plywood \
  --thickness 0.25 \
  --tool-diameter 0.157

# Test any Python module import
uv run python -c "from frc_cam_gui_app import app; print('OK')"
```

## Dependency Management

**CRITICAL**: This project uses `requirements.txt` for dependency specification (NOT `pyproject.toml`).

- **Local development**: Uses `uv` with `requirements.txt` (run `make install` or `uv pip install -r requirements.txt`)
- **Deployment**: Railway builds the `Dockerfile`, and the Docker build installs from `requirements.txt`
- **Adding dependencies**: Edit `requirements.txt` directly, then run `uv pip install -r requirements.txt`
- **DO NOT** create or use `pyproject.toml` - it will cause confusion with Railway deployment

```bash
# Add a new dependency
echo "new-package>=1.0.0" >> requirements.txt
uv pip install -r requirements.txt
```

## Development Rules

**Always run `make test` after making any code changes.** If tests fail, fix the errors before proceeding with other work. Do not commit or consider a change complete until all tests pass.

`make test` is the gate. Use `make test-quick` while iterating: it runs the unit, system and node tests and skips the real Orca slice.

## G-code Generation Rules

**CRITICAL**: The CNC machines that run our G-code have strict requirements. Violating these rules will cause machine failures:

### Nested Comments - FORBIDDEN
- **NEVER use nested parenthesis comments** in G-code output
- ❌ Bad: `(Outer comment (nested comment) more text)`
- ✅ Good: `(Outer comment, nested text, more text)`
- CNC controllers will fail or produce unpredictable behavior with nested comments
- There is a unit test (`test_no_nested_comments`) but it doesn't catch every case since some G-code is conditional

### Unicode Characters - FORBIDDEN
- **All G-code must be pure ASCII** - no unicode characters
- ❌ Bad: `(Cut depth: 0.25″)` (curly quotes), `(Feedrate → 75 IPM)` (arrows)
- ✅ Good: `(Cut depth: 0.25")` (straight quotes), `(Feedrate: 75 IPM)` (colon)
- There is a unit test (`test_no_unicode_characters`) but it doesn't catch every case

### Best Practices
When generating G-code comments:
1. Use commas or semicolons instead of nested parentheses
2. Use straight ASCII quotes and standard punctuation
3. Test conditional code paths manually if they generate comments
4. Be especially careful with f-strings that include measurements or user data

## Git Operations

**DO NOT run any git commands** (git add, git commit, git push, etc.). The user prefers to handle all git operations themselves. Focus on making code changes and let the user manage version control.

## Architecture

```
Browser (wizard.html + Three.js)
    ↓ HTTP POST /process, /process-job
Flask Server (frc_cam_gui_app.py)
    ↓ subprocess
G-code Generator (frc_cam_postprocessor.py)
    ↓
.nc file → 3D visualization / download / Drive upload
```

**Key files:**
- `frc_cam_gui_app.py` - Flask routes, Onshape OAuth, Drive integration
- `frc_cam_postprocessor.py` - Core G-code generation (`FRCPostProcessor` class)
- `templates/wizard.html` - Multi-part wizard UI (the whole app; served at `/`, `/app`, and the Onshape panel), with `static/wizard.js` + `static/gcode_viewer.js` (Three.js 3D visualization)
- `onshape_integration.py` - Onshape API client for one-click export
- `penguincam_auth.py` - Google Workspace OAuth (optional)
- `google_drive_integration.py` - Drive upload (optional)
- `print_routes.py` - Flask blueprint for the print wizard: page, part info, slice jobs, event stream
- `print_jobs.py` - In-process job pool and job table for slices (imports no Flask)
- `print_slicer.py` - Orca Slicer command line wrapper (imports no Flask)
- `templates/print_wizard.html` - Print wizard UI, served at `/print`
- `static/print_wizard.js` - Print wizard logic (fetches the part, submits the job, reads the event stream)
- `static/print_viewer.js` - Three.js mesh-on-bed viewer for the print wizard
- `print/` - Fixed print assets: `sample_part.stl` and the flattened Bambu Lab profiles

## Documentation

Detailed documentation lives in the `docs/` directory. **Read these before modifying related code:**

| File | When to Read |
|------|--------------|
| [Z_COORDINATE_SYSTEM.md](docs/Z_COORDINATE_SYSTEM.md) | Modifying Z-axis calculations, safe heights, cut depths, or plunge moves |
| `TOOL_COMPENSATION_GUIDE.md` (not written yet) | Changing offset logic for perimeters, pockets, or holes |
| [ASSUMPTIONS.md](docs/ASSUMPTIONS.md) | Adding/changing G-code output; lists controller compatibility requirements |
| [MACHINE_CHECKLIST.md](docs/MACHINE_CHECKLIST.md) | Updating G-code header comments or safety checks |
| [DEPLOYMENT_GUIDE.md](docs/DEPLOYMENT_GUIDE.md) | Changing environment variables, Railway config, or OAuth redirect URIs |
| [AUTHENTICATION_GUIDE.md](docs/AUTHENTICATION_GUIDE.md) | Modifying Google OAuth flow, session handling, or domain restrictions |
| [INTEGRATIONS_GUIDE.md](docs/INTEGRATIONS_GUIDE.md) | Changing Onshape API calls or Google Drive upload logic |
| [ONSHAPE_SETUP.md](docs/ONSHAPE_SETUP.md) | Updating the Onshape browser extension or import URL format |
| [quick-reference-card.md](docs/quick-reference-card.md) | Changing UI workflows or default settings that affect end users |
| [3D_PRINTING.md](docs/3D_PRINTING.md) | Changing the print wizard, the slicer wrapper, the job pool, or the Orca install |

## Testing

Tests compare PenguinCAM output against Fusion 360 CAM output using the `pygcode` library. Test fixtures are DXF files with known expected G-code.

```bash
make test-quick  # The fast tests, for iteration: no Orca slice
make test        # Everything, including the real Orca slice
```

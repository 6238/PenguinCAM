# Multi-Team Architecture

## Overview

PenguinCAM now supports hosting for multiple FRC teams with team-specific configuration stored in Onshape. This architecture allows each team to customize their PenguinCAM experience without requiring separate deployments.

## Architecture Changes

### 1. No Mandatory Authentication

**Before:** Google OAuth required to access the webapp
**After:** Public access for basic functionality

- Anyone can use DXF upload and G-code generation
- No authentication required for `/`, `/process`, `/download` routes
- Google OAuth only triggers when saving to Drive
- Onshape OAuth only triggers when importing from Onshape

### 2. Team Configuration (YAML)

Teams store configuration in Onshape documents as `PenguinCAM-config.yaml`:

```yaml
team:
  number: 6238
  name: "Popcorn Penguins"

integrations:
  google_drive:
    enabled: true
    folder_id: "1a2b3c4d5e"

machining:
  # Future: default_material, custom_feed_rates

ui:
  # Future: logo_url, primary_color
```

**Why YAML?**
- Comments allowed (self-documenting)
- FRC teams already familiar with YAML (WPILib configs)
- More human-friendly than JSON
- Excellent Python support via PyYAML

### 3. User Identity from Onshape

When launched from Onshape, PenguinCAM fetches:
- **User info:** Name, email from `/users/sessioninfo`
- **Team info:** Company/org name from document owner
- **Team config:** YAML file from team's documents

This information is:
- Stored in Flask session
- Displayed in UI header
- Used in G-code comments (future)

### 4. Config-Driven Features

Features are enabled/disabled based on team config:

**Google Drive Integration:**
- Only shown if `integrations.google_drive.enabled: true`
- Uses `folder_id` from config
- OAuth triggered on first Drive save

**Future Features:**
- Custom machining presets per team
- UI branding (logo, colors)
- Default materials and feed rates

## File Structure

### New Files

- **`team_config.py`** - TeamConfig class for loading/managing config
- **`PenguinCAM-config-template.yaml`** - Template for teams to customize

### Modified Files

- **`frc_cam_gui_app.py`** - Removed mandatory auth, added config loading
- **`onshape_integration.py`** - Added `fetch_config_file()`, `get_user_session_info()`, `get_document_company()`
- **`pyproject.toml`** - Added PyYAML dependency

## Usage Flow

### Scenario 1: Standalone DXF Upload (Anonymous)

```
User visits penguincam.popcornpenguins.com
  ↓
No authentication required
  ↓
Uploads DXF, generates G-code
  ↓
Downloads .nc file
```

### Scenario 2: Onshape Import with Drive Save

```
User clicks "Send to PenguinCAM" in Onshape
  ↓
Onshape OAuth (one-time per session)
  ↓
Fetches user info, team info, team config
  ↓
Imports DXF, generates G-code
  ↓
User clicks "Save to Drive" (if enabled in config)
  ↓
Google OAuth (one-time per session)
  ↓
Saves to team's configured Drive folder
```

## Team Config Setup

### For Teams Using Onshape:

1. **Create Config Document:**
   - In Onshape, create new document: `PenguinCAM-config`
   - Add a Text/Plain tab
   - Name it: `PenguinCAM-config.yaml`

2. **Copy Template:**
   - Use `PenguinCAM-config-template.yaml` as starting point
   - Update team number, name
   - Add Google Drive folder ID if using Drive

3. **Find Drive Folder ID:**
   - Open your Drive folder in browser
   - Check URL: `https://drive.google.com/drive/folders/FOLDER_ID_HERE`
   - Copy the `FOLDER_ID_HERE` part

4. **Test:**
   - Import any part from Onshape
   - Check logs to verify config is loaded
   - Drive button should appear if enabled

### For Teams NOT Using Onshape:

- No config needed
- Use standalone DXF upload
- No Drive integration (upload manually)

## Security Model

### Public Routes (No Auth)

- `/` - Home page
- `/process` - G-code generation (rate limited: 10/min)
- `/download` - File download
- `/uploads` - Serve uploaded DXFs
- All `/onshape/*` routes (protected by Onshape OAuth)

### Protected Routes (Google OAuth)

- `/drive/upload` - Save to Drive (triggers OAuth)

### Rate Limiting

All routes protected by Flask-Limiter:
- Global default: 200 requests/hour
- `/process`: 10 requests/minute (CPU intensive)
- `/onshape/import`: 20 requests/minute
- `/onshape/save-dxf`: 20 requests/minute
- `/drive/upload`: 30 requests/minute

## Future Enhancements

### Planned Config Properties

```yaml
machining:
  default_material: "plywood"
  custom_feed_rates:
    aluminum: 55
    plywood: 75
  default_tool_diameter: 0.157
  sacrifice_board_depth: 0.008

ui:
  logo_url: "https://team-website.com/logo.png"
  primary_color: "#1976d2"
  team_motto: "Popcorn Power!"
  show_cycle_time: true

safety:
  require_confirmation_for_aluminum: true
  max_feed_rate: 100
```

### Planned Features

- **Per-Team Branding:** Custom logo, colors in UI header
- **Custom Material Presets:** Team-specific feed rates
- **Usage Analytics:** Track G-code generation per team
- **Team Directory:** Public list of teams using PenguinCAM

## Migration from Single-Team

### Team 6238 (Popcorn Penguins)

1. **Create config in Onshape** with current Drive folder ID
2. **No code changes needed** - config system is backwards compatible
3. **Remove Google auth requirement** from Railway environment variables
4. **Test thoroughly** before making public

### Breaking Changes

- **None!** Existing Team 6238 workflows continue to work
- Direct DXF upload always worked, still works
- Onshape import works the same
- Drive save now optional (config-driven)

## Deployment Checklist

- [ ] Add `PenguinCAM-config.yaml` to Team 6238 Onshape
- [ ] Update Railway environment variables (remove mandatory GOOGLE_CLIENT_ID?)
- [ ] Test Onshape import (verify config loading)
- [ ] Test Drive save (verify OAuth flow)
- [ ] Test standalone DXF upload (verify no auth required)
- [ ] Monitor Railway logs for errors
- [ ] Update documentation/README for other teams

## Benefits

### For Teams

- **No barriers to entry:** Try PenguinCAM without any setup
- **Easy customization:** Edit YAML file in Onshape
- **Secure:** Each team's Drive folder is isolated
- **Familiar:** YAML format used in FRC robot code

### For PenguinCAM

- **Single deployment:** One app serves all teams
- **Scalable:** Add teams without code changes
- **Maintainable:** Team-specific logic in config, not code
- **Observable:** Track usage per team via logs

### For FRC Community

- **Lower friction:** Teams can start using immediately
- **Shared improvements:** All teams benefit from updates
- **Best practices:** Template config documents good defaults
- **Community growth:** More teams = more contributors

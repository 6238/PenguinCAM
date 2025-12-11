# Google Drive Integration - Quick Reference

## What It Does

Adds a **"💾 Save to Google Drive"** button to the GUI that uploads G-code files directly to your team's shared drive with a single click!

## Setup (One-Time, ~10 minutes)

### 1. Install Dependencies
```bash
pip install google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client
```

### 2. Get Google Credentials
- Go to [Google Cloud Console](https://console.cloud.google.com/)
- Create project → Enable "Google Drive API"
- Create OAuth "Desktop app" credentials
- Download as `credentials.json`
- Place in GUI directory

### 3. Create Configuration
Create `drive_config.json`:
```json
{
  "shared_drive_name": "Popcorn Penguins",
  "folder_path": "CNC/G-code",
  "folder_id": null
}
```

### 4. First Use
- Generate G-code
- Click "💾 Save to Google Drive"
- Authorize in browser (first time only)
- Done!

## What You'll See

### Before Setup
- Only "📥 Download G-code" button shows
- No Drive functionality

### After Setup (credentials.json + drive_config.json)
- Both buttons show: "📥 Download" and "💾 Save to Google Drive"
- Click Drive button → Uploads to shared drive
- Green message: "✅ Saved to Popcorn Penguins/CNC/G-code/filename.nc"

### If Not Configured
- Warning message: "⚠️ Google Drive not configured - see GOOGLE_DRIVE_SETUP.md"
- Install dependencies and follow setup guide

## Files

```
your-directory/
├── google_drive_integration.py     ← Integration code (included)
├── credentials.json                ← You download from Google
├── drive_config.json               ← You create
└── token.pickle                    ← Auto-created after auth
```

## Usage

1. Start GUI
2. Upload DXF + generate G-code
3. Click **"💾 Save to Google Drive"**
4. File uploads to `Popcorn Penguins/CNC/G-code/`
5. Success message shows
6. All team members can access it!

## Benefits

✅ One-click upload to shared team drive  
✅ Everyone uses same location  
✅ No manual file sharing needed  
✅ Works at competitions (with internet)  
✅ Automatic organization  
✅ Version control through Drive

## Troubleshooting

**Button doesn't appear:**
- Install Google dependencies (see step 1)
- Add credentials.json
- Restart GUI

**"Drive not configured":**
- Follow GOOGLE_DRIVE_SETUP.md
- Check credentials.json exists
- Verify drive_config.json is correct

**Upload fails:**
- Check internet connection
- Verify access to "Popcorn Penguins" shared drive
- Confirm folder exists: CNC/G-code
- Re-authorize if needed (delete token.pickle)

## Complete Guide

See **GOOGLE_DRIVE_SETUP.md** for:
- Detailed step-by-step instructions
- Screenshots and explanations
- OAuth consent screen setup
- Team member configuration
- Security information
- Advanced options

## Optional Feature

Google Drive integration is **optional**:
- GUI works fine without it
- Just won't have the "Save to Drive" button
- Download button always works

To enable: Follow setup steps above!

---

**FRC Team 6238** 🤖🍿🐧
Quick, easy team file sharing!

# Google Drive Integration Setup Guide

## Overview

This guide will help you set up Google Drive integration so your team can save G-code files directly to your shared "Popcorn Penguins" drive with a single click!

## What You'll Need

- Google account with access to the "Popcorn Penguins" shared drive
- 10-15 minutes for one-time setup
- Admin access to create a Google Cloud project (or use existing one)

## Step-by-Step Setup

### Step 1: Install Required Libraries

```bash
pip install google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client
```

Or use the updated requirements file:
```bash
pip install -r requirements_gui.txt
```

### Step 2: Create Google Cloud Project

1. Go to [Google Cloud Console](https://console.cloud.google.com/)

2. Click **"Create Project"** or use an existing project
   - Name it something like "FRC CAM Tool" or "Team 6238 CNC"
   - Click **"Create"**

3. Wait for the project to be created (takes ~30 seconds)

### Step 3: Enable Google Drive API

1. In the Google Cloud Console, make sure your project is selected

2. Go to **"APIs & Services"** → **"Library"**
   - Or visit: https://console.cloud.google.com/apis/library

3. Search for **"Google Drive API"**

4. Click on **"Google Drive API"**

5. Click **"Enable"**

### Step 4: Configure OAuth Consent Screen

1. Go to **"APIs & Services"** → **"OAuth consent screen"**

2. Choose **"Internal"** (if using Google Workspace) or **"External"**
   - Internal: Only your organization can use it
   - External: Anyone with link can authorize (need to add test users)

3. Fill in the app information:
   - **App name**: "FRC CAM GUI" or "Team 6238 CNC Tool"
   - **User support email**: Your email
   - **Developer contact**: Your email

4. Click **"Save and Continue"**

5. **Scopes**: Click **"Add or Remove Scopes"**
   - Search for: `https://www.googleapis.com/auth/drive.file`
   - Check the box
   - Click **"Update"** then **"Save and Continue"**

6. **Test users** (if External):
   - Add email addresses of team members who will use the tool
   - Click **"Add Users"**
   - Click **"Save and Continue"**

7. Click **"Back to Dashboard"**

### Step 5: Create OAuth Credentials

1. Go to **"APIs & Services"** → **"Credentials"**

2. Click **"+ Create Credentials"** → **"OAuth client ID"**

3. Choose **"Desktop app"** as the application type
   - Name it: "FRC CAM GUI Desktop"

4. Click **"Create"**

5. **Download the credentials**:
   - Click the **download icon** (⬇) next to your newly created OAuth client
   - Or click "Download JSON"
   - Save as **`credentials.json`**

6. Move `credentials.json` to your GUI directory:
   ```bash
   # Same directory as frc_cam_gui_app.py
   mv ~/Downloads/credentials.json /path/to/gui/directory/
   ```

### Step 6: Create Drive Configuration

Create a file called `drive_config.json` in the same directory:

```json
{
  "shared_drive_name": "Popcorn Penguins",
  "folder_path": "CNC/G-code",
  "folder_id": null
}
```

**Explanation:**
- `shared_drive_name`: Name of your shared drive (exactly as it appears)
- `folder_path`: Path to folder within the drive (will be created if needed)
- `folder_id`: Leave as `null` (auto-detected on first upload)

**Note:** The folder path uses forward slashes `/` like "CNC/G-code" or "Manufacturing/CNC/Output"

### Step 7: Create the Drive Folder (Optional)

You can create the folder manually in Google Drive, or let the app do it:

**Manual creation (recommended):**
1. Open Google Drive
2. Go to "Shared drives" → "Popcorn Penguins"
3. Create folder structure: `CNC` → `G-code`

**Automatic creation:**
- The app will create folders if they don't exist (requires Drive permissions)

### Step 8: First Time Authorization

1. Start the GUI:
   ```bash
   python frc_cam_gui_app.py
   ```

2. Generate some G-code (upload a DXF, click generate)

3. Click **"💾 Save to Google Drive"**

4. **First time only**: A browser window will open asking you to:
   - Choose your Google account
   - Review permissions (access to files created by this app)
   - Click **"Allow"**

5. The authorization is saved in `token.pickle` for future use

6. Your file uploads to the shared drive!

## File Structure

After setup, you should have:

```
your-gui-directory/
├── frc_cam_gui_app.py
├── frc_cam_postprocessor.py
├── google_drive_integration.py
├── templates/
│   └── index.html
├── credentials.json           ← Downloaded from Google Cloud
├── drive_config.json          ← You create this
└── token.pickle               ← Auto-created after first auth
```

## Troubleshooting

### "Missing credentials.json"

**Problem:** File not found or in wrong location

**Solution:**
- Download credentials.json from Google Cloud Console
- Place in same directory as frc_cam_gui_app.py
- Filename must be exactly `credentials.json`

### "Shared drive 'Popcorn Penguins' not found"

**Problem:** Drive name doesn't match or no access

**Solution:**
- Check exact name in Google Drive (capitalization matters!)
- Verify you have access to the shared drive
- Update `drive_config.json` with exact name

### "Folder 'CNC/G-code' not found"

**Problem:** Folder doesn't exist in the drive

**Solution:**
- Create the folder manually in Google Drive
- Or check folder_path in `drive_config.json`
- Make sure path uses forward slashes: "CNC/G-code"

### "Authentication failed"

**Problem:** OAuth error or expired credentials

**Solution:**
- Delete `token.pickle`
- Try authorizing again
- Check that OAuth consent screen is configured
- Verify your email is in test users list (if External)

### "Permission denied" or "Forbidden"

**Problem:** Google account doesn't have access to shared drive

**Solution:**
- Ask drive owner to add your email
- Make sure you're using the correct Google account
- Verify Drive API is enabled in Cloud Console

### Button doesn't appear

**Problem:** Dependencies not installed or configuration issue

**Solution:**
```bash
# Install dependencies
pip install google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client

# Restart the GUI
python frc_cam_gui_app.py

# Check console for error messages
```

## Testing

To verify everything works:

1. Start the GUI
2. Upload a DXF file
3. Generate G-code
4. You should see: **"💾 Save to Google Drive"** button
5. Click it
6. First time: Authorize in browser
7. Success: "✅ Saved to Popcorn Penguins/CNC/G-code/filename.nc"
8. Check Google Drive - file should be there!

## Team Members Setup

After one person does the full setup:

**Option 1: Share credentials.json (easier)**
- Copy `credentials.json` to each team member's GUI directory
- They authorize with their own Google account (each gets own `token.pickle`)

**Option 2: Each person creates their own (more secure)**
- Each person follows Steps 1-8
- Each has their own OAuth client
- More setup but better security

**Shared drive access:**
- All team members need access to "Popcorn Penguins" shared drive
- Ask drive owner to add everyone's emails

## Security Notes

### What has access:

✅ The app can only:
- Create and upload files
- Access files it created
- Read folder structure

❌ The app CANNOT:
- Read other people's files
- Delete files it didn't create
- Access your entire Google Drive
- Access anything outside the shared drive

### credentials.json

- Contains OAuth client ID and secret
- **Safe to share** within your team
- Don't post publicly online
- Same credentials.json can be used by all team members

### token.pickle

- Contains your personal authorization
- **Do NOT share** - unique to each person
- Deleted when you revoke access
- Auto-refreshes when expired

## Revoking Access

If you need to revoke the app's access:

1. Go to [Google Account Security](https://myaccount.google.com/permissions)
2. Find "FRC CAM GUI" (or your app name)
3. Click "Remove Access"
4. Delete `token.pickle` from GUI directory

## Advanced Configuration

### Custom Folder Location

Edit `drive_config.json`:

```json
{
  "shared_drive_name": "Popcorn Penguins",
  "folder_path": "Robotics/Manufacturing/CNC-Output",
  "folder_id": null
}
```

### Multiple Folders

You can set up different configurations:

```bash
# Development
cp drive_config.json drive_config_dev.json
# Edit: folder_path: "CNC/Development"

# Production
cp drive_config.json drive_config_prod.json
# Edit: folder_path: "CNC/Production"

# Switch by renaming
mv drive_config_prod.json drive_config.json
```

### Network Usage

The app uploads files directly from the server to Google Drive. This means:
- Upload speed depends on server internet connection
- Files don't pass through user's browser
- All team members use same server → same upload location

## Support

### Common Questions

**Q: Do I need a Google Workspace account?**
A: No, free Gmail accounts work fine with External OAuth consent screen.

**Q: Can we use a personal drive instead of shared drive?**
A: Yes! Just change `shared_drive_name` to your email address in drive_config.json.

**Q: How many team members can use this?**
A: Unlimited! Each person authorizes once, then everyone uploads to same location.

**Q: What if someone leaves the team?**
A: They can revoke access. Their token.pickle stops working, but shared credentials.json still works for others.

**Q: Can we use this at competitions?**
A: Yes! Just make sure the computer has internet access to reach Google Drive.

## Success!

You should now see:
- 💾 **Save to Google Drive** button appears after generating G-code
- Clicking uploads file to Popcorn Penguins/CNC/G-code/
- Green success message with file location
- File appears in Google Drive for all team members

Your team can now save G-code files with a single click! 🎉

---

**Need help?** Check the troubleshooting section or create an issue on GitHub.

**FRC Team 6238** - Go Popcorn Penguins! 🤖🍿🐧

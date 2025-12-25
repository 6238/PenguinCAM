# PenguinCAM Integrations Guide

**Complete guide to setting up Onshape API and Google Drive integration**

---

## Table of Contents

1. [Onshape Integration](#onshape-integration)
   - [Creating OAuth Application](#creating-onshape-oauth-application)
   - [Configuring PenguinCAM](#configuring-penguincam-for-onshape)
   - [Browser Extension Setup](#onshape-browser-extension)
   - [Testing Onshape Import](#testing-onshape-import)
2. [Google Drive Integration](#google-drive-integration)
   - [Shared Drive Setup](#shared-drive-setup)
   - [Configuration](#configuring-drive-uploads)
   - [Testing Uploads](#testing-drive-uploads)
3. [Troubleshooting](#troubleshooting)

---

## Onshape Integration

PenguinCAM integrates with Onshape to:
- ✅ Export DXF files directly from Part Studios
- ✅ Auto-detect the top face of parts
- ✅ Streamline workflow (no manual DXF export)

### Creating Onshape OAuth Application

#### Step 1: Access Developer Portal

1. Log in to Onshape: https://cad.onshape.com
2. Click your profile picture (top-right)
3. Select **"Preferences"**
4. Navigate to: **"API & Developer Settings"** tab
5. Scroll to **"OAuth applications"** section

#### Step 2: Create New OAuth App

Click **"Create new OAuth application"**

**Application Name:** `PenguinCAM`  
(This is what users see during authorization)

**Primary Format:** `JSON`

**Redirect URLs:** Add these two:
```
https://penguincam.popcornpenguins.com/onshape/oauth/callback
http://localhost:6238/onshape/oauth/callback
```

**Important:**
- First URL: Your production domain
- Second URL: For local testing (optional)
- Replace `penguincam.popcornpenguins.com` with your actual domain

**Permissions:**
- ☑️ **Read documents** (required)
- ☑️ **Read user info** (required)

Click **"Create application"**

#### Step 3: Save Credentials

After creation, you'll see:
- **Client ID:** Long alphanumeric string
- **Client Secret:** Another long string

**Critical:** Copy both immediately! The secret won't be shown again.

Click **"Show"** next to Client Secret to reveal it, then copy.

---

### Configuring PenguinCAM for Onshape

Add these environment variables in Railway:

```bash
ONSHAPE_CLIENT_ID=your-onshape-client-id
ONSHAPE_CLIENT_SECRET=your-onshape-client-secret
BASE_URL=https://penguincam.popcornpenguins.com
```

**Replace:**
- `your-onshape-client-id` with actual Client ID from Onshape
- `your-onshape-client-secret` with actual Client Secret
- Domain with your actual domain

Railway will automatically redeploy with new variables.

---

### Onshape Browser Extension

**Status:** Currently blocked pending Onshape support resolution

The browser extension would allow right-clicking parts in Onshape and selecting "Export to PenguinCAM". Configuration exists but visibility issues are being resolved with Onshape support.

#### Current Configuration (for reference)

In Onshape Developer Portal → OAuth app → **Extensions** tab:

**Extension Name:** `Export to PenguinCAM`

**Location:** `Element tab` (recommended)
- Shows in the ☰ menu when viewing a Part Studio

**Alternatives if Element tab doesn't work:**
- Element context menu (right-click Part Studio in tree)
- Document list context menu (right-click document)

**Context:** `Inside document`

**Action URL:**
```
https://penguincam.popcornpenguins.com/onshape/import
```

**Action Type:** `GET`

**Icon:** Upload your team logo (SVG format, max 100KB)

#### Workaround (While Extension is Unavailable)

**Users can still process Onshape parts via direct URL:**

1. Open Part Studio in Onshape
2. Copy the URL from browser address bar
   - Example: `https://cad.onshape.com/documents/abc123.../w/xyz789.../e/def456...`
3. In PenguinCAM, there will be an "Import from Onshape" option
4. Paste the URL
5. PenguinCAM extracts document/workspace/element IDs automatically

---

### Testing Onshape Import

#### Step 1: Authenticate with Onshape

1. Visit PenguinCAM: `https://penguincam.popcornpenguins.com`
2. You should see "Connect to Onshape" button or link
3. Click it
4. Onshape authorization page appears
5. Review permissions:
   - Read your documents
   - Read user information
6. Click **"Allow"**
7. Redirected back to PenguinCAM

#### Step 2: Import a Test Part

**Using Direct API (Workaround):**

1. Create a simple test part in Onshape
   - Rectangle with a couple holes
   - Make sure it's a flat plate
2. Copy the Part Studio URL
3. In PenguinCAM, use import feature with URL
4. PenguinCAM should:
   - Auto-detect the top face
   - Export DXF
   - Generate G-code preview

**Expected Result:**
- DXF exported successfully
- G-code generated
- 3D preview shows toolpaths

---

## Google Drive Integration

PenguinCAM uploads generated G-code to your team's **Shared Drive** so everyone can access files.

### Shared Drive Setup

#### Prerequisites

You need a **Google Workspace Shared Drive** (formerly Team Drive):
- All team members have access
- Files are owned by the organization (not individuals)
- Persists when students graduate

#### Step 1: Verify Shared Drive Exists

1. Go to: https://drive.google.com
2. Left sidebar: Look for **"Shared drives"**
3. Expand it to see your team's shared drive
   - Example: "Popcorn Penguins"

**If you don't have a shared drive:**
1. Contact your Google Workspace admin
2. Or create one: Click **"+ New"** next to Shared drives
3. Name it after your team

#### Step 2: Create Folder Structure

Inside your shared drive:

```
Popcorn Penguins/
└── CNC/
    └── G-code/
        └── (generated files will go here)
```

Create these folders:
1. Click into your shared drive
2. Right-click → New Folder → "CNC"
3. Enter CNC folder → New Folder → "G-code"

**Note:** You can use any folder structure you want, just remember the path!

---

### Configuring Drive Uploads

#### Environment Variables (Optional)

These have sensible defaults but can be customized:

```bash
DRIVE_NAME=Popcorn Penguins
DRIVE_FOLDER=CNC/G-code
```

**Default behavior if not set:**
- Shared drive: "Popcorn Penguins"
- Folder path: "CNC/G-code"

Only set these if you want different names/paths.

#### How It Works

1. User logs in with Google (OAuth from [AUTHENTICATION_GUIDE.md](AUTHENTICATION_GUIDE.md))
2. User processes a part and generates G-code
3. User clicks **"Save to Google Drive"**
4. File uploads to: `Shared Drive > CNC > G-code > filename.nc`
5. All team members can access the file

**Important:** The user's OAuth tokens are used for upload, but the file goes to the **shared drive**, not their personal Drive.

---

### Testing Drive Uploads

#### Step 1: Process a Test File

1. Log in to PenguinCAM
2. Upload a sample DXF or import from Onshape
3. Generate G-code
4. Preview looks correct

#### Step 2: Upload to Drive

1. Click **"Save to Google Drive"** button
2. Status message appears: "Uploading..."
3. Success: "✓ Uploaded to Google Drive"
4. Failure: Error message displayed

#### Step 3: Verify in Google Drive

1. Go to: https://drive.google.com
2. Navigate to: **Shared drives** → Your team → CNC → G-code
3. Your file should appear (e.g., `my_part.nc`)
4. Click it to verify contents

#### Step 4: Test Access

Have another team member:
1. Log in to Google Drive
2. Navigate to same folder
3. They should see the file
4. They can download/preview it

**This confirms shared access works!**

---

## Troubleshooting

### Onshape Issues

#### OAuth Authorization Fails

**Problem:** Can't authorize Onshape or get error during OAuth

**Solutions:**
1. Verify redirect URLs in Onshape app settings match exactly:
   ```
   https://penguincam.yourdomain.com/onshape/oauth/callback
   ```
2. Check `ONSHAPE_CLIENT_ID` and `ONSHAPE_CLIENT_SECRET` in Railway
3. Ensure `BASE_URL` matches your domain exactly
4. Try logging out of Onshape, clearing cookies, and trying again

---

#### "Failed to Export DXF"

**Problem:** Onshape connection works but DXF export fails

**Possible Causes:**
1. Part Studio has no faces to export
2. User lacks permissions to document
3. Document is in a shared workspace without access
4. Onshape API endpoint changed

**Solutions:**
1. Verify Part Studio has actual geometry
2. Check user has "View" or "Edit" access to document
3. Try with a document you own
4. Check PenguinCAM logs in Railway for specific error

---

#### Can't Find Top Face

**Problem:** "No horizontal faces found"

**Causes:**
- Part is not a flat plate
- Face normal isn't pointing up/down
- Part geometry is complex

**Solutions:**
1. Verify part has a flat top face
2. Check face is perpendicular to Z-axis
3. For complex parts, may need manual face selection (future feature)

---

### Google Drive Issues

#### "Google Drive not configured"

**Problem:** Drive button shows error message

**Causes:**
1. User not logged in
2. Drive API not enabled
3. OAuth scope missing

**Solutions:**
1. Log out and log back in (refresh Drive tokens)
2. Verify Drive API enabled in Google Cloud Console
3. Check OAuth consent screen includes `drive.file` scope
4. See [AUTHENTICATION_GUIDE.md](AUTHENTICATION_GUIDE.md)

---

#### "Upload Failed"

**Problem:** Upload button clicked but error occurs

**Possible Causes:**
1. Network connection issue
2. File too large (unlikely for G-code)
3. Drive quota exceeded
4. Shared drive doesn't exist
5. User lacks access to shared drive

**Solutions:**
1. Check internet connection
2. Verify shared drive exists and name matches `DRIVE_NAME`
3. Verify folder path exists: `CNC/G-code`
4. Check user is a member of the shared drive
5. Try uploading a file directly to Drive to test access

---

#### Files Go to Wrong Location

**Problem:** Files upload but appear in wrong folder

**Solutions:**
1. Check `DRIVE_FOLDER` environment variable
2. Verify path matches actual folder structure
3. Path should be relative to shared drive root: `CNC/G-code` not `/CNC/G-code`
4. Case-sensitive: `G-code` ≠ `g-code`

---

#### Can't Find Uploaded Files

**Problem:** Upload succeeds but can't find files in Drive

**Check:**
1. Looking in correct shared drive (not "My Drive")
2. Looking in correct folder path
3. File name might be different than expected
   - Format: `{original_name}_{timestamp}.nc`
4. Use Drive search: filename or `.nc` extension

---

### Permission Issues

#### "Access Denied" to Shared Drive

**Problem:** User can log in but can't access shared drive

**Solutions:**
1. Add user to shared drive members:
   - Drive → Shared drives → Your drive → ⚙️ Settings
   - Add member with appropriate role (Contributor or Content Manager)
2. Verify user is in your Google Workspace
3. Check they're using correct account

---

#### Onshape Document Access

**Problem:** Can't import from Onshape document

**Causes:**
- Document is private
- User not in share list
- Wrong Onshape account

**Solutions:**
1. Share document with user's Onshape account
2. Verify user logged into correct Onshape account
3. Try with a public document first (test)

---

## Integration Workflow

### Complete User Flow

Here's how everything works together:

1. **Student logs in to PenguinCAM**
   - Google Workspace authentication
   - Gets Drive access tokens

2. **Student authorizes Onshape (first time)**
   - Click "Connect to Onshape"
   - Authorize read access

3. **Student designs part in Onshape**
   - Creates flat plate in Part Studio
   - Adds holes, pockets, perimeter

4. **Student exports to PenguinCAM**
   - Copies Part Studio URL
   - Pastes into PenguinCAM import
   - OR uses browser extension (when available)

5. **PenguinCAM processes part**
   - Auto-detects top face
   - Exports DXF from Onshape
   - Generates G-code
   - Shows 3D preview

6. **Student reviews and downloads**
   - Checks toolpaths in 3D view
   - Downloads G-code locally
   - OR uploads to team shared drive

7. **Manufacturing**
   - Retrieves G-code from Drive
   - Loads into CNC machine
   - Manufactures part!

---

## Best Practices

### Onshape Organization

**Folder structure:**
```
Team Documents/
└── FRC 2025 Season/
    └── Robot Parts/
        ├── Chassis Plates/
        ├── Gearbox Mounts/
        └── Intake Components/
```

**Naming convention:**
- `SUBSYSTEM_PartName_v1`
- Example: `CHASSIS_BaseFrame_v2`

---

### Drive Organization

**Folder structure:**
```
Popcorn Penguins/
├── CNC/
│   ├── G-code/           ← PenguinCAM uploads here
│   ├── Setup Sheets/
│   └── CAM Projects/
├── Designs/
└── Documentation/
```

**File naming:**
- Auto-generated by PenguinCAM
- Format: `{dxf_name}_{timestamp}.nc`
- Keeps versions separate

---

### Access Control

**Shared Drive Permissions:**
- **Content Manager:** Mentors, lead students
- **Contributor:** All team members
- **Commenter:** Alumni, supporters

**Onshape Sharing:**
- Share design folders with whole team
- Use Onshape Teams for organization

---

## Testing Checklist

Before going live with students:

**Onshape:**
- [ ] OAuth app created
- [ ] Client ID/Secret in Railway
- [ ] Test authorization with mentor account
- [ ] Test import with sample part
- [ ] Verify top face detection works
- [ ] Check DXF export quality

**Google Drive:**
- [ ] Shared drive exists
- [ ] Folder structure created
- [ ] All team members added
- [ ] Test upload with mentor account
- [ ] Verify file appears for others
- [ ] Check permissions are correct

**Integration:**
- [ ] End-to-end: Onshape → PenguinCAM → Drive
- [ ] Test with student account
- [ ] Verify on mobile (responsive)
- [ ] Check error messages are helpful

---

## Next Steps

Once integrations are working:

1. ✅ Train students → [quick-reference-card.md](quick-reference-card.md)
2. ✅ Monitor usage during build season
3. ✅ Plan improvements → [../ROADMAP.md](../ROADMAP.md)

---

## Quick Reference

**Onshape OAuth App:**
- Developer Portal: Profile → Preferences → API & Developer Settings
- Redirect URL: `https://your-domain.com/onshape/oauth/callback`
- Permissions: Read documents, Read user info

**Environment Variables:**
```bash
ONSHAPE_CLIENT_ID=xxxxx
ONSHAPE_CLIENT_SECRET=xxxxx
DRIVE_NAME=Your Team Shared Drive
DRIVE_FOLDER=CNC/G-code
```

**Drive Path:**
```
Shared drives → [Team Name] → CNC → G-code
```

---

**Last Updated:** December 2025  
**Maintained by:** FRC Team 6238 Popcorn Penguins

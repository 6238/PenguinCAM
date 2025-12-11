# PenguinCAM Authentication Guide

**Complete guide to setting up Google Workspace authentication with OAuth 2.0 and Drive API access**

---

## Table of Contents

1. [Overview](#overview)
2. [Google Cloud Console Setup](#google-cloud-console-setup)
3. [OAuth Consent Screen](#oauth-consent-screen)
4. [Creating OAuth Credentials](#creating-oauth-credentials)
5. [Configuring PenguinCAM](#configuring-penguincam)
6. [Testing Authentication](#testing-authentication)
7. [Troubleshooting](#troubleshooting)

---

## Overview

PenguinCAM uses **Google OAuth 2.0** to:
- ✅ Authenticate users (only your Workspace domain)
- ✅ Access Google Drive API (for uploading G-code)
- ✅ Maintain user sessions securely

**Why OAuth instead of SAML?**
- OAuth provides both authentication AND API access tokens
- Allows PenguinCAM to upload files to Google Drive on behalf of users
- Simpler integration for web apps

**Important:** Even though you use Google Workspace, OAuth apps are configured in **Google Cloud Console**, not the Workspace Admin panel.

---

## Google Cloud Console Setup

### Step 1: Create a Google Cloud Project

1. Go to: https://console.cloud.google.com/
2. Sign in with your **Workspace admin account**
3. Click **"Select a project"** dropdown (top bar)
4. Click **"New Project"**

**Project Configuration:**
- **Project name:** `PenguinCAM` (or your choice)
- **Organization:** Select your Workspace organization (critical!)
  - If you don't see your organization, you may need to enable Google Cloud for your Workspace
  - Must be set to organization to use "Internal" OAuth consent screen

5. Click **"Create"**
6. Wait for project creation (takes ~30 seconds)

---

### Step 2: Enable Required APIs

1. In Google Cloud Console, select your new project
2. Navigate to: **APIs & Services** → **Library**

**Enable these APIs:**

#### **Google Drive API**
- Search: "Google Drive API"
- Click the result
- Click **"Enable"**
- Purpose: Allows file uploads to shared drive

#### **Google People API**
- Search: "People API"  
- Click the result
- Click **"Enable"**
- Purpose: Retrieves user profile information

**Both APIs must be enabled or authentication will fail!**

---

## OAuth Consent Screen

The consent screen is what users see when they log in. Setting it to "Internal" restricts access to your Workspace domain only.

### Step 1: Configure Consent Screen

1. Navigate to: **APIs & Services** → **OAuth consent screen**
2. Choose **User Type:**

#### **Select "Internal"** (Required!)
- Only users in your Workspace can access
- No Google verification required
- Perfect for team-only tools

**Note:** If you don't see "Internal" option:
- Your project isn't in your Workspace organization
- Go back and ensure Organization is selected when creating project

### Step 2: App Information

**App name:** `PenguinCAM`

**User support email:** Your team email address

**App logo:** (Optional)
- Can upload team logo
- 120x120 pixels recommended
- PNG or JPG format

**Application home page:** (Optional)
- Your team website or leave blank

**Authorized domains:**
```
popcornpenguins.com
```
(Replace with your actual domain)

**Developer contact email:** Your team email

Click **"Save and Continue"**

---

### Step 3: Add Scopes

Scopes define what permissions PenguinCAM requests.

Click **"Add or Remove Scopes"**

**Required scopes:**

1. **Email and Profile:**
   - `openid`
   - `.../auth/userinfo.email`
   - `.../auth/userinfo.profile`
   
   Type in filter: `userinfo`
   Check both boxes

2. **Google Drive:**
   - `.../auth/drive.file`
   
   Type in filter: `drive.file`
   Check the box
   
   **Important:** Use `drive.file` (limited access) not `drive` (full access)
   - `drive.file` only accesses files created by the app
   - More secure and appropriate for this use case

**Manual entry if needed:**
```
openid
https://www.googleapis.com/auth/userinfo.email
https://www.googleapis.com/auth/userinfo.profile
https://www.googleapis.com/auth/drive.file
```

Click **"Update"** → **"Save and Continue"**

---

### Step 4: Test Users

For "Internal" apps, **this step can be skipped** - all Workspace users are automatically allowed.

Click **"Save and Continue"**

---

### Step 5: Review

Review your configuration:
- User type: Internal
- App name: PenguinCAM
- Scopes: 4 scopes (openid, email, profile, drive.file)

Click **"Back to Dashboard"**

**Your OAuth consent screen is now configured!** ✅

---

## Creating OAuth Credentials

Now create the actual credentials (Client ID and Secret) that PenguinCAM will use.

### Step 1: Create Credentials

1. Navigate to: **APIs & Services** → **Credentials**
2. Click **"+ Create Credentials"** (top of page)
3. Select **"OAuth client ID"**

### Step 2: Configure OAuth Client

**Application type:** `Web application`

**Name:** `PenguinCAM Web`
(This name is for your reference only, users don't see it)

### Step 3: Authorized JavaScript Origins

Add your app's base URL:

```
https://penguincam.popcornpenguins.com
```

**Important:**
- Must be HTTPS (not HTTP)
- No trailing slash
- Replace with your actual domain
- If testing locally first, also add: `http://localhost:6238`

### Step 4: Authorized Redirect URIs

Add the OAuth callback endpoint:

```
https://penguincam.popcornpenguins.com/auth/callback
```

**Important:**
- Must exactly match your `BASE_URL` + `/auth/callback`
- HTTPS required in production
- For local testing, also add: `http://localhost:6238/auth/callback`

### Step 5: Create

Click **"Create"**

### Step 6: Save Credentials

A dialog appears with your credentials:

- **Client ID:** `xxxxx.apps.googleusercontent.com`
- **Client secret:** `xxxxx`

**Critical:** Copy both values immediately!

Click **"Download JSON"** to save a backup (optional but recommended)

---

## Configuring PenguinCAM

### Environment Variables in Railway

Go to Railway → Your service → **Variables** tab

Add these variables:

```bash
GOOGLE_CLIENT_ID=your-client-id.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=your-client-secret
BASE_URL=https://penguincam.popcornpenguins.com
AUTH_ENABLED=true
ALLOWED_DOMAINS=popcornpenguins.com
```

**Replace:**
- `your-client-id` with your actual Client ID
- `your-client-secret` with your actual Client Secret
- `penguincam.popcornpenguins.com` with your domain
- `popcornpenguins.com` with your Workspace domain

### Optional: Restrict to Specific Emails

If you want only certain people to access (e.g., just mentors):

```bash
ALLOWED_EMAILS=mentor@popcornpenguins.com,admin@popcornpenguins.com
```

Comma-separated, no spaces.

**Leave blank to allow all Workspace users.**

### Save and Deploy

Railway will automatically redeploy when you add variables.

---

## Testing Authentication

### Step 1: Visit Your App

Go to: `https://penguincam.popcornpenguins.com`

You should be redirected to `/auth/login`

### Step 2: Sign In

1. Click **"Sign in with Google"** (or similar)
2. Google consent screen appears
3. Select your Workspace account
4. Review permissions:
   - View email and profile
   - Access Google Drive files created by this app
5. Click **"Allow"**

### Step 3: Verify Success

- You should be redirected back to PenguinCAM
- You should see the main interface
- Top-right should show your profile picture/name

### Step 4: Test Drive Permission

1. Process a sample DXF file
2. Generate G-code
3. Click **"Save to Google Drive"**
4. Should upload successfully to your shared drive

---

## Troubleshooting

### "App isn't verified" Warning

**Cause:** Google shows this for external apps, but you selected "Internal"

**Solution:**
- Verify OAuth consent screen shows "Internal" user type
- Ensure Google Cloud project is in your Workspace organization
- Only Workspace users should see login, never external users

---

### "Access Denied" After Login

**Problem:** User is authenticated but blocked from PenguinCAM

**Causes:**
1. **Wrong domain:** User email domain doesn't match `ALLOWED_DOMAINS`
2. **Email restriction:** User not in `ALLOWED_EMAILS` list

**Solutions:**
- Check user's email domain matches workspace domain
- Verify `ALLOWED_DOMAINS` environment variable
- If using `ALLOWED_EMAILS`, add user to list or remove variable

---

### "redirect_uri_mismatch" Error

**Problem:** OAuth error about redirect URI

**Cause:** Mismatch between:
- Redirect URI in Google Cloud Console
- `BASE_URL` in Railway
- Actual domain you're visiting

**Solution:**
1. Verify all three match exactly:
   ```
   Google Cloud: https://penguincam.example.com/auth/callback
   BASE_URL:     https://penguincam.example.com
   Visiting:     https://penguincam.example.com
   ```
2. No trailing slashes
3. HTTPS (not HTTP) in production
4. Case-sensitive match

---

### Login Loop (Redirects Back to Login)

**Problem:** After Google login, redirects back to login page

**Causes:**
1. Session cookies not being set
2. BASE_URL mismatch
3. Browser blocking third-party cookies

**Solutions:**
- Clear browser cookies and try again
- Verify `BASE_URL` matches actual domain
- Try in incognito/private browsing
- Check browser allows cookies from your domain

---

### "insecure_transport" Error

**Problem:** `OAuth 2 MUST utilize https`

**Cause:** Flask seeing HTTP instead of HTTPS behind Railway's proxy

**Solution:** Already fixed in code via ProxyFix middleware. If still occurring:
1. Verify `BASE_URL` uses `https://`
2. Ensure Railway deployment is using latest code
3. Redeploy if necessary

---

### Drive Upload Fails

**Problem:** "Not authenticated with Google Drive"

**Causes:**
1. Drive API not enabled
2. Drive scope not in OAuth consent screen
3. User needs to re-authenticate

**Solutions:**
1. Verify Drive API enabled in Google Cloud Console
2. Check OAuth consent screen has `drive.file` scope
3. Have user log out and log back in
4. Check Drive integration settings

---

### Can't Find "Internal" Option

**Problem:** Only see "External" user type

**Cause:** Google Cloud project not in Workspace organization

**Solutions:**
1. Create new project
2. When creating, select your organization in "Organization" dropdown
3. If no organization appears:
   - You may need Workspace admin to enable Google Cloud
   - Contact workspace administrator

---

## Security Best Practices

### Protecting Credentials

**Never commit to Git:**
```bash
# In .gitignore
auth_config.json
*.json
.env
```

**Use environment variables:**
- All credentials in Railway variables
- Never hardcoded in source files

### Limiting Access

**Domain restriction:**
```bash
ALLOWED_DOMAINS=yourdomain.com
```
Only users with @yourdomain.com emails can access

**Email whitelist (optional):**
```bash
ALLOWED_EMAILS=mentor@team.com,admin@team.com
```
Further restrict to specific users

### Monitoring

**Check logs regularly:**
- Railway → Deployments → Logs
- Look for repeated failed logins
- Check for unusual access patterns

### Token Security

**OAuth tokens are stored in:**
- User's session (server-side)
- Encrypted cookies
- Automatically expire after 24 hours

**Users should log out:**
- On shared computers
- At end of session

---

## Next Steps

Once authentication is working:

1. ✅ Configure OnShape integration → [INTEGRATIONS_GUIDE.md](INTEGRATIONS_GUIDE.md)
2. ✅ Set up Google Drive → Already done if Drive upload works!
3. ✅ Test with students → [quick-reference-card.md](quick-reference-card.md)

---

## Quick Reference

**Google Cloud Console URLs:**
- Project dashboard: https://console.cloud.google.com
- OAuth consent: https://console.cloud.google.com/apis/credentials/consent
- Credentials: https://console.cloud.google.com/apis/credentials

**Environment Variables:**
```bash
GOOGLE_CLIENT_ID=xxxxx.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=xxxxx
BASE_URL=https://your-domain.com
AUTH_ENABLED=true
ALLOWED_DOMAINS=your-workspace-domain.com
```

**Scopes Required:**
- `openid`
- `https://www.googleapis.com/auth/userinfo.email`
- `https://www.googleapis.com/auth/userinfo.profile`
- `https://www.googleapis.com/auth/drive.file`

---

**Last Updated:** December 2025  
**Maintained by:** FRC Team 6238 Popcorn Penguins

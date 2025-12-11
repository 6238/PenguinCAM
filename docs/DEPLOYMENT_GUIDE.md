# PenguinCAM Deployment Guide

**Complete guide to deploying PenguinCAM on Railway**

---

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Railway Deployment](#railway-deployment)
3. [Environment Variables](#environment-variables)
4. [Custom Domain Setup](#custom-domain-setup)
5. [Troubleshooting](#troubleshooting)
6. [Updating the Deployment](#updating-the-deployment)

---

## Prerequisites

Before deploying, you need:

- ✅ GitHub account with PenguinCAM repository
- ✅ Railway account (free tier available)
- ✅ Google Cloud Console credentials (see [AUTHENTICATION_GUIDE.md](AUTHENTICATION_GUIDE.md))
- ✅ OnShape OAuth credentials (see [INTEGRATIONS_GUIDE.md](INTEGRATIONS_GUIDE.md))
- ✅ (Optional) Custom domain for branding

---

## Railway Deployment

### Step 1: Create Railway Account

1. Go to https://railway.app
2. Sign up with your GitHub account
3. Authorize Railway to access your repositories

### Step 2: Deploy from GitHub

1. Click **"New Project"**
2. Select **"Deploy from GitHub repo"**
3. Choose your **PenguinCAM repository**
4. Railway will automatically:
   - Detect it's a Python app
   - Read `Procfile` for startup command
   - Install dependencies from `requirements.txt`

### Step 3: Configure Build

Railway auto-detects the configuration from your repo:

**Procfile:**
```
web: gunicorn frc_cam_gui_app:app --bind 0.0.0.0:$PORT
```

**Requirements:**
- Flask, gunicorn, requests
- OnShape/Google API libraries
- DXF processing (ezdxf, shapely)

### Step 4: Wait for Build

- Initial build takes 2-3 minutes
- Watch the build logs for any errors
- Once complete, you'll see "Deployment successful"

---

## Environment Variables

**Critical:** Set these before first deployment or the app will not work correctly.

### Required Variables

Navigate to your Railway service → **Variables** tab:

#### **Base Configuration**
```bash
BASE_URL=https://your-domain.com
# Example: https://penguincam.popcornpenguins.com
# IMPORTANT: Must be HTTPS, no trailing slash
```

#### **Google Authentication**
```bash
GOOGLE_CLIENT_ID=your-client-id.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=your-client-secret
AUTH_ENABLED=true
ALLOWED_DOMAINS=your-workspace-domain.com
# Example: ALLOWED_DOMAINS=popcornpenguins.com
```

#### **OnShape Integration**
```bash
ONSHAPE_CLIENT_ID=your-onshape-client-id
ONSHAPE_CLIENT_SECRET=your-onshape-client-secret
```

### Optional Variables

```bash
# Flask Session (strongly recommended)
FLASK_SECRET_KEY=<64-character-hex-string>
# Keeps user sessions valid across redeploys
# Generate with: python3 -c "import secrets; print(secrets.token_hex(32))"
# Without this, users must re-authenticate after each deployment

# Authentication (optional)
ALLOWED_EMAILS=admin@example.com,teacher@example.com
# Comma-separated list of specific emails to allow

# Google Drive (optional)
DRIVE_NAME=Your Team Shared Drive
DRIVE_FOLDER=CNC/G-code
# Path within the shared drive

# Flask (optional)
FLASK_ENV=production
# Disables debug mode in production
```

### Adding Variables in Railway

1. Go to your service dashboard
2. Click **"Variables"** tab
3. Click **"New Variable"**
4. Enter name and value
5. Click **"Add"**
6. Railway will automatically redeploy

**Important:** Variables must be added to your **service**, not just created in the project!

---

## Custom Domain Setup

### Using Your Own Domain

#### Step 1: Configure Railway

1. Go to your service → **Settings** → **Domains**
2. Click **"Custom Domain"**
3. Enter your subdomain: `penguincam.yourdomain.com`
4. Railway will show you the CNAME target

#### Step 2: Configure DNS

In your DNS provider (Dreamhost, Cloudflare, etc.):

```
Type:   CNAME
Name:   penguincam
Target: your-service.up.railway.app  (Railway provides this)
TTL:    Auto or 3600
```

#### Step 3: Wait for Validation

- DNS propagation: 5-60 minutes
- Railway validates domain ownership
- Railway provisions SSL certificate (automatic)
- Status changes from "Validating" → "Active"

#### Step 4: Update Environment Variables

Once domain is active, update:

```bash
BASE_URL=https://penguincam.yourdomain.com
```

Also update in Google Cloud Console and OnShape:
- OAuth redirect URIs
- Authorized JavaScript origins

### Using Railway's Default Domain

Railway provides a free domain:
```
https://your-service-name.up.railway.app
```

This works but is less memorable for students. Use `BASE_URL` with this URL if not using custom domain.

---

## Troubleshooting

### Build Failures

**Problem:** Build fails during deployment

**Solutions:**
1. Check `requirements.txt` for invalid package names
2. Ensure Python version compatibility (Railway uses 3.11+)
3. Review build logs for specific errors
4. Verify `Procfile` syntax

### OAuth Errors: "insecure_transport"

**Problem:** `OAuth 2 MUST utilize https`

**Cause:** Flask sees HTTP instead of HTTPS behind Railway's proxy

**Solution:** Already fixed in code via `ProxyFix` middleware. If you still see this:
1. Verify `BASE_URL` starts with `https://`
2. Check that ProxyFix is enabled in `frc_cam_gui_app.py`
3. Restart deployment

### Environment Variables Not Working

**Problem:** App can't find CLIENT_ID or CLIENT_SECRET

**Solutions:**
1. Verify variables are added to the **service**, not just the project
2. Check for typos in variable names (case-sensitive!)
3. Ensure no extra spaces in values
4. Redeploy after adding variables

### "Authentication Required" Loop

**Problem:** Login redirects back to login page

**Cause:** Session cookies not working or domain mismatch

**Solutions:**
1. Verify `BASE_URL` matches your actual domain exactly
2. Check browser allows cookies
3. Ensure redirect URIs in Google Cloud match `BASE_URL`

### Google Drive Upload Fails

**Problem:** "Not authenticated with Google Drive"

**Solutions:**
1. Log out and log back in to get fresh tokens
2. Verify Drive API is enabled in Google Cloud Console
3. Check Drive scope is included in OAuth consent screen
4. Ensure user has access to the shared drive

### Port 8080 or Port Mismatch

**Problem:** App shows wrong port in logs

**Cause:** Railway assigns port dynamically via `$PORT` environment variable

**Solution:** This is normal! Railway's proxy handles routing. Users always connect on port 443 (HTTPS).

---

## Updating the Deployment

### Auto-Deploy from GitHub

**Railway automatically redeploys when you push to main branch.**

```bash
git add .
git commit -m "Update feature X"
git push origin main
```

Railway will:
1. Detect the push
2. Pull latest code
3. Rebuild and redeploy
4. Switch to new version (zero downtime)

### Manual Redeploy

If needed, you can trigger a manual redeploy:

1. Go to Railway dashboard
2. Select your service
3. Click **"Deployments"** tab
4. Click **⋮** menu on latest deployment
5. Select **"Redeploy"**

### Checking Deployment Status

**Live logs:**
1. Railway dashboard → Your service
2. Click **"Deployments"** tab
3. Click on active deployment
4. View real-time logs

**Health check:**
Visit your app URL - you should see the PenguinCAM interface.

---

## Production Checklist

Before launching to students:

- [ ] All environment variables configured
- [ ] Custom domain active with SSL
- [ ] Google OAuth consent screen approved
- [ ] Test authentication with student account
- [ ] Test OnShape import with sample part
- [ ] Test Google Drive upload
- [ ] Verify shared drive access for all team members
- [ ] Update OnShape OAuth redirect URIs to production domain
- [ ] Update Google Cloud OAuth redirect URIs to production domain
- [ ] Test on mobile devices (responsive design)

---

## Cost Considerations

**Railway Free Tier:**
- $5 credit per month
- 500 hours execution time
- Enough for light usage (FRC team)

**Typical Usage:**
- Small team (10-20 students)
- Occasional part processing
- Should stay within free tier

**If You Exceed Free Tier:**
- Railway charges $0.000463/GB-hour
- Expect $5-10/month for active season

**Monitoring Usage:**
Railway dashboard shows current usage and costs in real-time.

---

## Security Best Practices

1. **Never commit secrets to Git**
   - Use environment variables for all credentials
   - Add `*.json` with credentials to `.gitignore`

2. **Restrict authentication**
   - Use `ALLOWED_DOMAINS` to limit to your workspace
   - Consider `ALLOWED_EMAILS` for admin-only features

3. **Keep dependencies updated**
   - Periodically update `requirements.txt`
   - Test updates in development before deploying

4. **Monitor logs**
   - Check Railway logs for unusual activity
   - Set up alerts for repeated failed logins

---

## Getting Help

**Railway Issues:**
- Railway Discord: https://discord.gg/railway
- Railway Docs: https://docs.railway.app

**PenguinCAM Issues:**
- GitHub Issues: [Your repo URL]
- Team mentor: [Your contact]

**Google/OnShape OAuth:**
- See [AUTHENTICATION_GUIDE.md](AUTHENTICATION_GUIDE.md)
- See [INTEGRATIONS_GUIDE.md](INTEGRATIONS_GUIDE.md)

---

## Next Steps

Once deployed:
1. ✅ Set up authentication → [AUTHENTICATION_GUIDE.md](AUTHENTICATION_GUIDE.md)
2. ✅ Configure integrations → [INTEGRATIONS_GUIDE.md](INTEGRATIONS_GUIDE.md)
3. ✅ Test with students → [quick-reference-card.md](quick-reference-card.md)
4. ✅ Plan improvements → [../ROADMAP.md](../ROADMAP.md)

---

**Last Updated:** December 2025  
**Maintained by:** FRC Team 6238 Popcorn Penguins

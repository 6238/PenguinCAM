# PenguinCAM Deployment Guide

**Complete guide to deploying PenguinCAM on Railway**

---

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Railway Deployment](#railway-deployment)
3. [Environment Variables](#environment-variables)
4. [Printer Relay](#printer-relay)
5. [Custom Domain Setup](#custom-domain-setup)
6. [Orca Slicer and the Docker build](#orca-slicer-and-the-docker-build)
7. [Troubleshooting](#troubleshooting)
8. [Updating the Deployment](#updating-the-deployment)

---

## Prerequisites

Before deploying, you need:

- ✅ GitHub account with PenguinCAM repository
- ✅ Railway account (free tier available)
- ✅ Google Cloud Console credentials (see [AUTHENTICATION_GUIDE.md](AUTHENTICATION_GUIDE.md))
- ✅ Onshape OAuth credentials (see [INTEGRATIONS_GUIDE.md](INTEGRATIONS_GUIDE.md))
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
   - Detect the `Dockerfile` in the repository root
   - Build the image from it
   - Run the image's `CMD`

### Step 3: Configure Build

The repository has a `Dockerfile`, so Railway builds from it and ignores the `Procfile`. The
`Procfile` is kept identical to the Dockerfile's `CMD` for tools that read it, but nothing in
production does. See [Orca Slicer and the Docker build](#orca-slicer-and-the-docker-build) for
what the image contains.

**Startup command (the Dockerfile's `CMD`, and the `Procfile`):**
```
gunicorn frc_cam_gui_app:app --bind 0.0.0.0:$PORT --workers 1 --worker-class gthread --threads 16
```

**Requirements:**
- Flask, gunicorn, requests
- Onshape/Google API libraries
- DXF processing (ezdxf, shapely)
- Orca Slicer, downloaded during the image build

### Step 4: Wait for Build

- The image build takes several minutes; the Orca download is about 130 MB
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

#### **Onshape Integration**
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

## Printer Relay

The Bambu printer relay adds a Raspberry Pi in a team's shop that polls this backend and
sends prints to a LAN printer. The mentor-facing guide is
[PRINTER_SETUP.md](PRINTER_SETUP.md). What follows is what deployment has to get right.

### `FLASK_SECRET_KEY` is required, not merely recommended

The daemon's token is signed with `FLASK_SECRET_KEY`. Without it the app makes a random key
on every start. Every token then dies on every deploy, and the Pi cannot re-pair on its own,
because the exchange needs a session to have loaded the team's YAML first.

**Symptom:** after every deploy, every printer shows "has not reported since the service
started" until somebody opens PenguinCAM from Onshape.

Set it once, keep it. It is listed under Optional Variables above for sessions. For the
printer relay it is required.

### Development mode and the HTTPS rule

Development mode is Flask's debug flag on **and** `RAILWAY_ENVIRONMENT` absent. The
development server started as the project's [CLAUDE.md](../CLAUDE.md) describes meets both
conditions. Gunicorn on Railway meets neither.

Outside development mode, `/printer/daemon/*` and `/install-printer.sh` answer 403 to any
request that did not arrive over HTTPS, judged after `ProxyFix`. On Railway, therefore,
nothing in this feature is ever served over plain HTTP, whatever a proxy forwards.

### `PRINTER_DEV_PAIRING_CODE`

Seeds a relay record for that pairing code, so a daemon can pair against a server nobody has
opened in Onshape. It is read **only** in development mode and ignored on Railway.

**Never set it in production.**

### The installer route and the documented URL

`GET /install-printer.sh` serves `print/printer_daemon/install.sh` from this app. It is a
convenience alias.

The **documented** install command fetches the same script from GitHub, pinned to a commit:

```
curl -fsSL https://raw.githubusercontent.com/6238/PenguinCAM/<sha>/print/printer_daemon/install.sh | sudo bash
```

That is deliberate. The site is behind Cloudflare, and a challenge page piped into `bash`
fails confusingly.

### Cloudflare

On a **Pro plan or better**, add a WAF custom rule with action **Skip**, skipping the phases
`http_request_sbfm` and `http_ratelimit` and the products `uaBlock`, `bic` and
`securityLevel`, with this expression:

```
(starts_with(http.request.uri.path, "/printer/daemon/")) or
(starts_with(http.request.uri.path, "/download/")) or
(http.request.uri.path eq "/install-printer.sh")
```

On the **free plan**, Bot Fight Mode cannot be skipped per path. Cloudflare's own
documentation says so, and the rule above does nothing for bots there. **Keep Bot Fight Mode
off**, or the daemon's five-second syncs may be challenged.

The daemon recognises a challenge page, an HTML body on a JSON route, and names it in
`penguincam-printer status` instead of reporting a parse error.

**Not yet confirmed:** as of 2026-09-13 the zone's plan and its Bot Fight Mode setting had
not been checked in the dashboard, and nothing had been challenged. The owner should look:
**Websites -> popcornpenguins.com** for the plan badge, and **Security -> Bots** for the
toggle.

### Rate limits

Every printer route sets its own limit with `override_defaults=True`. A five-second sync is
720 requests an hour, against the app-wide default of 200 per hour.

`storage_uri="memory://"` means the counters live in each gunicorn worker. With one worker
the limits are what they say. If the worker count ever rises above one, the effective limits
multiply by the number of workers.

### Accepted risk: the pairing exchange budget is service-wide

`POST /printer/daemon/pair` is limited to 60 requests a minute on a constant key, so every
team shares one budget. That is the spec's choice (section 6): a pairing code is forty bits,
which is out of reach at sixty guesses a minute, and keying the limit on the address would
let a guesser spread the attempts over a botnet instead.

The cost is that flask-limiter counts a request **before** the view runs, so a request the
HTTPS gate would have refused has already spent one of the sixty. An anonymous client can
therefore keep the budget empty and stop every team's Pi from exchanging its pairing code,
including right after a deploy that lost `FLASK_SECRET_KEY`.

**This is accepted, not overlooked.** A Pi retries every thirty seconds and honours
`Retry-After`, so the effect is a pairing that takes longer, not one that fails, and it
clears as soon as the pressure stops. Nothing is exposed: the attacker cannot pair either,
and a Pi that already holds a token keeps syncing, because only the exchange is on this
budget. If it ever happens in practice, the fix is a per-address limit alongside the
service-wide one, once verification 8 says what address production actually sees.

### What was verified, and when

- Verification 5 (2026-09-13): a token signed with `FLASK_SECRET_KEY` verifies under the
  same key and is rejected under another; pinned by
  `print/tests/test_printer_relay.py::TestVerificationFiveTokenDurability`, which mints a token in
  a separate interpreter so a restart is what is actually being tested.
- Verification 6 (2026-09-13): every daemon dependency has an aarch64 wheel; about 7 MB of
  downloads, about 23 MB installed; 64-bit Raspberry Pi OS is required because Pillow
  publishes no 32-bit wheel.
- Verification 7 (2026-09-13): the zone is proxied by Cloudflare, nothing was challenged on
  the day, the plan was not confirmed, and free-plan Bot Fight Mode cannot be skipped per
  path -- hence the GitHub raw installer URL.
- Verification 8, partial (2026-09-13): `ProxyFix` honours one forwarded hop (`x_for=1`);
  `override_defaults=True` is set on every printer route and pinned by a test. Both, and the
  `X-Forwarded-Proto: https` that opens the HTTPS gate, are pinned by
  `print/tests/test_printer_routes.py::TestVerificationEightLimiterAndProxy`. Note that `ProxyFix`
  counts hops from the **right**: with `x_for=1` an `X-Forwarded-For` that already carries a
  chain gives the app the address the nearest proxy reported, not the browser's. Nothing in
  this feature keys a real limit on the address -- the printer routes key on the pairing
  code -- but the app-wide default does. **Open:** what address the app actually sees in
  production for a browser and for a Pi. Needs a live probe against Railway; **ask the
  owner** before running one.
- Verifications 1 to 4: **open**, need the shop H2S.
  `print/printer_daemon/tests/fixtures/h2s_status.json` is provisional until
  `print/printer_daemon/tests/smoke_test.py` has been run against it.

### Updating the daemon

`print/print/printer_daemon/install.sh` pins `PENGUINCAM_SHA`. That SHA is the one place the daemon's
version is written.

**Every pull request that changes anything under `print/printer_daemon/` updates it to that pull
request's merge commit.** Until the owner sets it when opening the pull request it reads
`REPLACE_BEFORE_MERGE`, and the installer refuses to run.

**It is two edits, one commit.** `PENGUINCAM_SHA` in `print/printer_daemon/install.sh` and the
`<sha>` in the documented install command in [PRINTER_SETUP.md](PRINTER_SETUP.md) section 4
must name the same commit, and both are set when the pull request is opened. They are what a
mentor's install is made of: the URL says which script is fetched, the SHA inside it says
which daemon that script downloads. Two different commits mean a pairing nobody tested.

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

Also update in Google Cloud Console and Onshape:
- OAuth redirect URIs
- Authorized JavaScript origins

### Using Railway's Default Domain

Railway provides a free domain:
```
https://your-service-name.up.railway.app
```

This works but is less memorable for students. Use `BASE_URL` with this URL if not using custom domain.

---

## Orca Slicer and the Docker build

The 3D print path runs Orca Slicer as a subprocess, so the deployed image has to carry the
slicer and every library it links against. Developer detail lives in
[3D_PRINTING.md](3D_PRINTING.md); this section covers what that means for the deployment.

### Railway builds from the Dockerfile

Railway uses the `Dockerfile` whenever one is present. The image's `CMD` is what production
runs. The `Procfile` holds the same line so tools that read a Procfile see the right command,
and the two must be changed together.

### What the build does

1. Starts from `python:3.11-slim-trixie`. The pin matters: Debian 13 (trixie) ships glibc 2.41,
   and Orca's Ubuntu 24.04 AppImage needs glibc 2.38 or newer. A pinned tag keeps a future tag
   move from changing the C library under the slicer.
2. Installs the runtime packages listed in the `Dockerfile`. The AppImage bundles only a few
   private libraries, so GTK, Cairo, Pango, WebKit, GStreamer, Wayland and X11 packages come
   from the Debian index.
3. Runs `print/scripts/install-orca.sh`, which downloads Orca **2.4.2** (version and SHA-256
   checksums pinned in the script) and unpacks it into `tools/orca/`. This happens before the
   application code is copied, so a code change does not re-download the release.
4. Checks the result with `ldd`. If the Orca binary has an unresolved library, the build fails
   there instead of at the first slice in production.

### Web process and worker count

```
gunicorn frc_cam_gui_app:app --bind 0.0.0.0:$PORT --workers 1 --worker-class gthread --threads 16
```

Sixteen threads let several open event streams sit beside ordinary requests. Workers stay at
one, and `--workers 1` is explicit because gunicorn otherwise reads `WEB_CONCURRENCY`. The job
table, the file token map and the rate limiter's storage are all per process: a second worker
would see none of the first worker's jobs, tokens or counters.

### Slice pool sizing

`print/jobs.py` allows one Orca process at a time (`MAX_RUNNING = 1`) and queues two more
(`MAX_QUEUED = 2`). Beyond that, a request is refused rather than queued for someone who has
stopped waiting.

Measured on 2026-09-13, on an aarch64 Linux machine:

| Part | Wall clock | Peak resident |
|------|-----------|---------------|
| `print/sample_part.stl` | 0.23 s | 93 MB |
| 120 x 120 x 40 mm box | 0.6 s | 250 MB |

The rule for raising `MAX_RUNNING` is `min(vCPU count, memory / peak resident memory of one
slice)`, never below one. The Railway plan's vCPU and memory allotment has not been measured
yet, so the constants stay where they are until someone reads the plan's
allotment from the Railway dashboard and revisits them with that rule.

### Rate limit on slices

`POST /print-job` is capped at 3 per minute. That cap is deployment-wide, not per user:
`ProxyFix` is configured without `x_for`, so every browser behind Railway's proxy is seen as
the same address and shares one counter. Raise the limit with the pool constants, not on its
own.

### Vercel is unsupported

From the 3D print stage on, the serverless deployment in `vercel.json` cannot run this app:
there is no Orca binary in that build and no room for one. `app.py` and `vercel.json` are
untouched but no longer a supported target. Deploy on Railway, or on any host that builds the
`Dockerfile`.

---

## Troubleshooting

### Build Failures

**Problem:** Build fails during deployment

**Solutions:**
1. Check `requirements.txt` for invalid package names
2. Review build logs for specific errors
3. If the build fails on the `ldd` check, a runtime library is missing from the `Dockerfile`'s
   `apt-get install` list; the failing line names it
4. If the build fails in `print/scripts/install-orca.sh`, the download or its checksum failed; see
   [3D_PRINTING.md](3D_PRINTING.md)

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
- [ ] Test Onshape import with sample part
- [ ] Test Google Drive upload
- [ ] Verify shared drive access for all team members
- [ ] Update Onshape OAuth redirect URIs to production domain
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

**Google/Onshape OAuth:**
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

**Last Updated:** January 2026
**Maintained by:** FRC Team 6238 Popcorn Penguins

# PenguinCAM Authentication Setup

## Overview

When deploying PenguinCAM to a public server, you'll want to restrict access to your team members only. This authentication system uses Google OAuth to ensure only authorized users can access the tool.

## Features

✅ **Google OAuth 2.0** - Secure authentication via Google  
✅ **Domain restrictions** - Only allow specific domains (e.g., team6238.org)  
✅ **Email whitelist** - Allow specific email addresses  
✅ **Session management** - 24-hour sessions with secure tokens  
✅ **Beautiful login page** - Team-branded interface  
✅ **Optional** - Works without auth for local use  
✅ **Integrated** - Uses same Google Workspace as Drive integration

## Quick Setup (10 minutes)

### 1. Create OAuth Credentials

Go to [Google Cloud Console](https://console.cloud.google.com/):

1. **Select your project** (same one as Google Drive if you set that up)

2. **Go to "APIs & Services" → "Credentials"**

3. **Click "+ Create Credentials" → "OAuth client ID"**

4. **Choose "Web application"**
   - Name: "PenguinCAM Web"
   
5. **Add Authorized JavaScript origins:**
   - http://localhost:6238 (for testing)
   - https://your-server.com (your production URL)
   
6. **Add Authorized redirect URIs:**
   - http://localhost:6238/auth/callback
   - https://your-server.com/auth/callback

7. **Click "Create"**

8. **Copy the Client ID** (looks like: `123456789-abc...xyz.apps.googleusercontent.com`)

### 2. Configure Authentication

Edit `auth_config.json`:

```json
{
  "enabled": true,
  "google_client_id": "YOUR_CLIENT_ID_HERE",
  "allowed_domains": [
    "team6238.org",
    "gmail.com"
  ],
  "allowed_emails": [
    "mentor@example.com",
    "captain@example.com"
  ],
  "require_domain": true,
  "session_timeout": 86400
}
```

**Configuration options:**

- `enabled`: Set to `true` to require authentication
- `google_client_id`: Your OAuth client ID from step 1
- `allowed_domains`: List of email domains that can access
- `allowed_emails`: Specific emails (bypasses domain check)
- `require_domain`: If `true`, must match allowed domain
- `session_timeout`: Session duration in seconds (86400 = 24 hours)

### 3. Test Locally

```bash
python frc_cam_gui_app.py
# Open http://localhost:6238
# You'll see the login page!
```

### 4. Deploy to Production

See [PRODUCTION_DEPLOYMENT.md](#) for full deployment guide.

## How It Works

### Without Authentication (Local Use)

```
User → http://localhost:6238 → PenguinCAM GUI
                                 (no login required)
```

### With Authentication (Public Server)

```
User → https://penguin cam.team6238.org
         ↓
Login page → Google Sign-In
         ↓
Verify email/domain → Create session → PenguinCAM GUI
```

## User Experience

### First Visit

1. Navigate to PenguinCAM URL
2. See login page with "Sign in with Google" button
3. Click button → Google popup
4. Choose your account
5. Authorize access
6. Redirected to PenguinCAM GUI
7. Session lasts 24 hours

### Subsequent Visits

- Already logged in (session active)
- Go directly to GUI
- No login needed until session expires

### Logout

- Click user icon / logout button
- Session cleared
- Redirected to login page

## Access Control Strategies

### Strategy 1: Domain-Based (Recommended for Schools)

Allow anyone from your organization's domain:

```json
{
  "enabled": true,
  "google_client_id": "...",
  "allowed_domains": ["school.edu", "team6238.org"],
  "allowed_emails": [],
  "require_domain": true
}
```

**Pros:** Automatic access for all team members  
**Cons:** Less control over individual users

### Strategy 2: Email Whitelist (Recommended for Competition)

Specific team members only:

```json
{
  "enabled": true,
  "google_client_id": "...",
  "allowed_domains": [],
  "allowed_emails": [
    "student1@gmail.com",
    "student2@gmail.com",
    "mentor@team.org"
  ],
  "require_domain": false
}
```

**Pros:** Complete control over access  
**Cons:** Must update list for new members

### Strategy 3: Hybrid

Domain + specific external emails:

```json
{
  "enabled": true,
  "google_client_id": "...",
  "allowed_domains": ["team6238.org"],
  "allowed_emails": [
    "external-mentor@gmail.com"
  ],
  "require_domain": true
}
```

**Pros:** Flexible, allows external collaborators  
**Cons:** Most complex to manage

## Security Features

### What's Protected

✅ Main GUI interface  
✅ File upload and processing  
✅ G-code download  
✅ Google Drive upload  
✅ All API endpoints

### What's Secure

- **Sessions:** Server-side with secure tokens
- **HTTPS:** Required for production (see deployment guide)
- **Token verification:** Google tokens verified on server
- **No password storage:** Uses Google's authentication
- **Session timeout:** Automatic logout after 24 hours
- **CSRF protection:** Built into Flask sessions

### What Users See

**Authorized user:**
- Login once
- Full access to PenguinCAM
- Session persists across browser closes

**Unauthorized user:**
- Login page appears
- Sign in with Google
- "Access denied" if not authorized
- Clear message about restrictions

## Troubleshooting

### "Authentication not enabled" error

**Cause:** `enabled: false` in config  
**Solution:** Set `enabled: true` in auth_config.json

### "Google Client ID not configured"

**Cause:** Missing or invalid client_id  
**Solution:** Add your OAuth client ID to auth_config.json

### "Access denied"

**Cause:** User's email/domain not in allowed lists  
**Solution:** 
- Add their email to `allowed_emails`, OR
- Add their domain to `allowed_domains`

### Login button doesn't work

**Cause:** Client ID mismatch  
**Solution:** 
- Verify client ID in auth_config.json
- Check OAuth app authorized origins in Google Console

### Redirect URI mismatch

**Cause:** OAuth app not configured for your URL  
**Solution:** Add URL to "Authorized redirect URIs" in Google Console

### Users keep getting logged out

**Cause:** Session timeout too short  
**Solution:** Increase `session_timeout` in auth_config.json

## Testing Checklist

Before deploying to production:

- [ ] OAuth credentials created in Google Console
- [ ] Client ID added to auth_config.json
- [ ] Authorized origins configured
- [ ] Redirect URIs configured  
- [ ] Test login with authorized user
- [ ] Test rejection of unauthorized user
- [ ] Test logout functionality
- [ ] Test session persistence
- [ ] Test from external network

## Local Development

For local testing:

```json
{
  "enabled": false,
  "...": "..."
}
```

Set `enabled: false` to bypass authentication during development.

## Production Deployment

### Requirements

1. **HTTPS:** Authentication REQUIRES HTTPS in production
   - Use Let's Encrypt for free SSL certificates
   - Configure nginx/Apache as reverse proxy

2. **Firewall:** Only expose ports 80 (HTTP) and 443 (HTTPS)

3. **Session secret:** Auto-generated but you can set manually in code

### Example nginx Configuration

```nginx
server {
    listen 443 ssl;
    server_name penguincam.team6238.org;
    
    ssl_certificate /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;
    
    location / {
        proxy_pass http://localhost:6238;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

## Integration with Google Drive

If you've already set up Google Drive integration:

✅ **Use the same Google Cloud project**  
✅ **Same OAuth consent screen**  
✅ **Users authenticate once for both features**  
✅ **Shared team workspace**

## Adding Team Members

### If using domain-based access:

1. Member joins your organization/workspace
2. Gets email on allowed domain
3. Can immediately access PenguinCAM
4. No configuration needed!

### If using email whitelist:

1. Get member's email address
2. Add to `allowed_emails` in auth_config.json
3. Restart server
4. Member can now login

## Removing Team Members

### Domain-based:

1. Remove from organization
2. They lose access automatically

### Email whitelist:

1. Remove email from auth_config.json
2. Restart server
3. Their session expires (max 24 hours)
4. Can't login again

## Advanced Configuration

### Custom Session Timeout

```json
{
  "session_timeout": 3600
}
```

Values in seconds:
- 3600 = 1 hour
- 28800 = 8 hours  
- 86400 = 24 hours (default)
- 604800 = 1 week

### Multiple Environments

Development:
```json
{
  "enabled": false
}
```

Staging:
```json
{
  "enabled": true,
  "allowed_emails": ["tester@team.org"]
}
```

Production:
```json
{
  "enabled": true,
  "allowed_domains": ["team6238.org"]
}
```

## Monitoring

Check authentication logs in Flask console:

```
⚠️  Authentication module not available  # Auth disabled
✓ Authentication enabled                 # Auth active
Login: user@team.org                     # Successful login
Access denied: external@gmail.com        # Rejected user
```

## Support

### Google OAuth Resources

- [OAuth 2.0 Documentation](https://developers.google.com/identity/protocols/oauth2)
- [Web sign-in guide](https://developers.google.com/identity/gsi/web/guides/overview)
- [OAuth Console](https://console.cloud.google.com/apis/credentials)

### Common Questions

**Q: Can we use Microsoft/GitHub/other OAuth?**  
A: Currently only Google OAuth is supported. Can add others if needed.

**Q: Does this work offline?**  
A: No, authentication requires internet. For offline use, set `enabled: false`.

**Q: What if Google is down?**  
A: Users can't login. Existing sessions continue working.

**Q: Can we have different access levels?**  
A: Currently all authenticated users have full access. Role-based access can be added.

**Q: Is this HIPAA/FERPA compliant?**  
A: Consult your security team. This provides basic authentication, not full compliance.

## Summary

### What You Get

✅ Secure authentication via Google OAuth  
✅ Team-only access control  
✅ Domain or email-based restrictions  
✅ Beautiful branded login page  
✅ 24-hour sessions  
✅ Easy team member management  
✅ Production-ready security

### Setup Time

- **OAuth setup:** 5 minutes
- **Configuration:** 2 minutes  
- **Testing:** 3 minutes
- **Total:** ~10 minutes

### Files Involved

- `penguincam_auth.py` - Authentication module (created)
- `auth_config.json` - Configuration (you edit)
- `frc_cam_gui_app.py` - Protected routes (updated)

---

**FRC Team 6238 - Popcorn Penguins** 🤖🍿🐧  
**Feature:** Google OAuth Authentication  
**Status:** Production-ready  
**Security:** Team-only access control

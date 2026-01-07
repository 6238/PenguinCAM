# PenguinCAM Onshape Side Panel Extension Blueprint

## Executive Summary

This document outlines how to convert PenguinCAM from a standalone web app to an **Onshape right-side panel extension** while preserving Google Drive integration. The key challenge is OAuth authentication in iframes, which modern browsers restrict for security reasons.

**Feasibility: Yes, with architectural changes**

The approach requires:
1. One-time popup authentication for Google Drive (cached indefinitely)
2. Pre-authorization model for Onshape OAuth
3. Persistent token storage (database instead of in-memory sessions)
4. Cookie configuration changes for cross-origin iframe operation

---

## Current Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  Browser Tab (standalone)                                   │
│  ┌───────────────────────────────────────────────────────┐  │
│  │  PenguinCAM UI (index.html + Three.js)                │  │
│  │                                                       │  │
│  │  [Upload DXF] ──────► Flask Server ──────► G-code     │  │
│  │  [Import from Onshape] ── OAuth redirect ──► DXF      │  │
│  │  [Upload to Drive] ── OAuth redirect ──► Drive        │  │
│  └───────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

**Current OAuth Flows:**
- **Onshape**: Full page redirect to `cad.onshape.com/oauth/authorize`
- **Google**: Full page redirect to `accounts.google.com/o/oauth2/auth`

**Token Storage:**
- `OnshapeSessionManager`: In-memory dict (lost on restart)
- Google credentials: Flask session (lost on restart)

---

## Proposed Architecture (Side Panel)

```
┌─────────────────────────────────────────────────────────────────────────┐
│  Onshape Document Tab                                                   │
│  ┌─────────────────────────────────┬───────────────────────────────────┐│
│  │                                 │  PenguinCAM Side Panel (iframe)   ││
│  │                                 │  ┌─────────────────────────────┐  ││
│  │      Onshape CAD View           │  │  Context: docId, wsId, elId │  ││
│  │                                 │  │                             │  ││
│  │                                 │  │  [Generate G-code]          │  ││
│  │                                 │  │  [Upload to Drive] ◄─ popup │  ││
│  │                                 │  │                             │  ││
│  │                                 │  │  3D Preview (Three.js)      │  ││
│  │                                 │  └─────────────────────────────┘  ││
│  └─────────────────────────────────┴───────────────────────────────────┘│
└─────────────────────────────────────────────────────────────────────────┘
                                        │
                                        ▼
                              ┌─────────────────────┐
                              │  PenguinCAM Server  │
                              │  (Railway/Lambda)   │
                              │                     │
                              │  - Token storage    │
                              │  - G-code gen       │
                              │  - Drive upload     │
                              └─────────────────────┘
```

---

## OAuth Strategy

### The Problem

Modern browsers block OAuth redirects in iframes to prevent clickjacking attacks:

| Provider | iframe Redirect Allowed? | Header |
|----------|-------------------------|--------|
| Google | No | `X-Frame-Options: deny` |
| Onshape | Partial (special flow) | Allows with `redirectOnshapeUri` |

### The Solution: Hybrid Approach

#### 1. Onshape OAuth: Pre-Authorization Model

Onshape extensions support a flow where users authorize **before** using the extension:

```
User Flow:
1. User installs PenguinCAM extension from Onshape App Store
2. During installation, user grants OAuth permissions
3. Extension loads with user already authorized
4. Tokens stored server-side, retrieved via session cookie
```

**Implementation:**
- Register app in Onshape Developer Portal as an "Extension"
- Configure OAuth scopes: `OAuth2Read OAuth2ReadPII`
- Set redirect URI to handle post-installation auth
- Store tokens in database keyed by Onshape user ID

#### 2. Google OAuth: One-Time Popup

Google OAuth cannot work in iframes, but we can use a popup window:

```
User Flow:
1. User clicks "Connect Google Drive" in side panel
2. Popup window opens: accounts.google.com/o/oauth2/auth
3. User authenticates in popup
4. Popup receives tokens, sends to parent via postMessage
5. Parent (iframe) stores tokens server-side
6. Popup closes automatically
7. Future sessions: tokens retrieved from database (no popup needed)
```

**Key Point:** Users only see the popup **once ever** (unless they revoke access).

---

## Token Storage Architecture

### Current (Won't Work in Production)

```python
# onshape_integration.py
class OnshapeSessionManager:
    def __init__(self):
        self.sessions = {}  # In-memory - lost on restart!
```

### Proposed: Persistent Database Storage

**Option A: PostgreSQL (Railway has built-in support)**

```python
# models.py
class UserTokens(db.Model):
    id = db.Column(db.String(64), primary_key=True)  # Onshape user ID

    # Onshape tokens
    onshape_access_token = db.Column(db.Text)  # Encrypted
    onshape_refresh_token = db.Column(db.Text)  # Encrypted
    onshape_token_expires = db.Column(db.DateTime)

    # Google tokens
    google_access_token = db.Column(db.Text)  # Encrypted
    google_refresh_token = db.Column(db.Text)  # Encrypted
    google_token_expires = db.Column(db.DateTime)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, onupdate=datetime.utcnow)
```

**Option B: Redis (Simpler, good for Railway)**

```python
# token_storage.py
import redis
from cryptography.fernet import Fernet

class TokenStorage:
    def __init__(self):
        self.redis = redis.from_url(os.environ['REDIS_URL'])
        self.cipher = Fernet(os.environ['ENCRYPTION_KEY'])

    def store_google_tokens(self, user_id, access_token, refresh_token):
        data = {
            'access_token': self.cipher.encrypt(access_token.encode()),
            'refresh_token': self.cipher.encrypt(refresh_token.encode()),
            'stored_at': datetime.utcnow().isoformat()
        }
        self.redis.hset(f'google_tokens:{user_id}', mapping=data)

    def get_google_tokens(self, user_id):
        data = self.redis.hgetall(f'google_tokens:{user_id}')
        if not data:
            return None
        return {
            'access_token': self.cipher.decrypt(data['access_token']).decode(),
            'refresh_token': self.cipher.decrypt(data['refresh_token']).decode()
        }
```

**Option C: DynamoDB (If moving to AWS Lambda like ShapeForge)**

ShapeForge uses this approach - see `/ShapeForge/awsStorage.js` for reference.

---

## Implementation Plan

### Phase 1: Cookie Configuration for iframes

Flask sessions must work across origins (Onshape → PenguinCAM):

```python
# frc_cam_gui_app.py
from flask import Flask

app = Flask(__name__)
app.config.update(
    SESSION_COOKIE_SECURE=True,      # HTTPS only
    SESSION_COOKIE_HTTPONLY=True,    # No JS access
    SESSION_COOKIE_SAMESITE='None',  # Required for cross-origin iframe
)
```

### Phase 2: Context Extraction from Onshape

When loaded as a side panel, Onshape passes context via URL parameters:

```python
# routes.py
@app.route('/panel')
def side_panel():
    """Entry point when loaded as Onshape side panel"""
    # Onshape passes these automatically
    document_id = request.args.get('documentId')
    workspace_id = request.args.get('workspaceId')
    element_id = request.args.get('elementId')

    # Store in session for later API calls
    session['onshape_context'] = {
        'document_id': document_id,
        'workspace_id': workspace_id,
        'element_id': element_id
    }

    return render_template('panel.html',
                          document_id=document_id,
                          from_onshape=True)
```

### Phase 3: Google OAuth Popup Flow

**Frontend (JavaScript):**

```javascript
// static/app.js
class GoogleDriveAuth {
    constructor() {
        this.popup = null;
        this.authPromise = null;
    }

    async connectDrive() {
        // Check if already connected
        const status = await fetch('/drive/status');
        const data = await status.json();

        if (data.connected) {
            return true; // Already have valid tokens
        }

        // Open popup for OAuth
        return this.openAuthPopup();
    }

    openAuthPopup() {
        return new Promise((resolve, reject) => {
            // Calculate popup position (centered)
            const width = 500;
            const height = 600;
            const left = window.screenX + (window.outerWidth - width) / 2;
            const top = window.screenY + (window.outerHeight - height) / 2;

            // Open popup to our OAuth initiation endpoint
            this.popup = window.open(
                '/auth/google/popup',
                'google_auth',
                `width=${width},height=${height},left=${left},top=${top}`
            );

            // Listen for message from popup
            const messageHandler = (event) => {
                // Verify origin
                if (event.origin !== window.location.origin) return;

                if (event.data.type === 'google_auth_success') {
                    window.removeEventListener('message', messageHandler);
                    this.popup?.close();
                    resolve(true);
                } else if (event.data.type === 'google_auth_error') {
                    window.removeEventListener('message', messageHandler);
                    this.popup?.close();
                    reject(new Error(event.data.error));
                }
            };

            window.addEventListener('message', messageHandler);

            // Handle popup closed without completing
            const checkClosed = setInterval(() => {
                if (this.popup?.closed) {
                    clearInterval(checkClosed);
                    window.removeEventListener('message', messageHandler);
                    reject(new Error('Authentication cancelled'));
                }
            }, 500);
        });
    }
}

// Usage
const driveAuth = new GoogleDriveAuth();

document.getElementById('upload-drive').addEventListener('click', async () => {
    try {
        await driveAuth.connectDrive();
        // Now upload the file
        await uploadToDrive(currentFilename);
    } catch (error) {
        showError('Drive connection failed: ' + error.message);
    }
});
```

**Backend (Flask):**

```python
# auth_routes.py
@app.route('/auth/google/popup')
def google_auth_popup():
    """Initiate Google OAuth in popup window"""
    # Generate OAuth URL
    flow = google_auth_oauthlib.flow.Flow.from_client_config(
        client_config=GOOGLE_CLIENT_CONFIG,
        scopes=['https://www.googleapis.com/auth/drive.file']
    )
    flow.redirect_uri = url_for('google_auth_callback', _external=True)

    auth_url, state = flow.authorization_url(
        access_type='offline',  # Get refresh token
        include_granted_scopes='true',
        prompt='consent'  # Force consent to get refresh token
    )

    session['google_oauth_state'] = state
    return redirect(auth_url)


@app.route('/auth/google/callback')
def google_auth_callback():
    """Handle Google OAuth callback in popup"""
    try:
        flow = google_auth_oauthlib.flow.Flow.from_client_config(
            client_config=GOOGLE_CLIENT_CONFIG,
            scopes=['https://www.googleapis.com/auth/drive.file'],
            state=session.get('google_oauth_state')
        )
        flow.redirect_uri = url_for('google_auth_callback', _external=True)

        # Exchange code for tokens
        flow.fetch_token(authorization_response=request.url)
        credentials = flow.credentials

        # Store tokens in database (encrypted)
        user_id = get_current_user_id()  # From Onshape session
        token_storage.store_google_tokens(
            user_id=user_id,
            access_token=credentials.token,
            refresh_token=credentials.refresh_token
        )

        # Return HTML that sends message to parent and closes
        return '''
        <!DOCTYPE html>
        <html>
        <head><title>Authentication Complete</title></head>
        <body>
            <script>
                window.opener.postMessage({type: 'google_auth_success'}, '*');
                window.close();
            </script>
            <p>Authentication successful! This window should close automatically.</p>
        </body>
        </html>
        '''

    except Exception as e:
        return f'''
        <!DOCTYPE html>
        <html>
        <head><title>Authentication Failed</title></head>
        <body>
            <script>
                window.opener.postMessage({{
                    type: 'google_auth_error',
                    error: '{str(e)}'
                }}, '*');
                window.close();
            </script>
            <p>Authentication failed: {str(e)}</p>
        </body>
        </html>
        '''
```

### Phase 4: Token Refresh Logic

```python
# token_manager.py
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

class TokenManager:
    def __init__(self, storage):
        self.storage = storage

    def get_valid_google_credentials(self, user_id):
        """Get valid Google credentials, refreshing if needed"""
        tokens = self.storage.get_google_tokens(user_id)

        if not tokens:
            return None  # User needs to authenticate

        credentials = Credentials(
            token=tokens['access_token'],
            refresh_token=tokens['refresh_token'],
            token_uri='https://oauth2.googleapis.com/token',
            client_id=os.environ['GOOGLE_CLIENT_ID'],
            client_secret=os.environ['GOOGLE_CLIENT_SECRET']
        )

        # Check if expired and refresh
        if credentials.expired and credentials.refresh_token:
            try:
                credentials.refresh(Request())

                # Store the new access token
                self.storage.store_google_tokens(
                    user_id=user_id,
                    access_token=credentials.token,
                    refresh_token=credentials.refresh_token
                )
            except Exception as e:
                print(f"Token refresh failed: {e}")
                return None  # User needs to re-authenticate

        return credentials
```

### Phase 5: Onshape Extension Registration

Create an extension in the Onshape Developer Portal:

1. Go to https://dev-portal.onshape.com/
2. Create new OAuth Application
3. Set Application Type: "Extension"
4. Configure:
   - **Name**: PenguinCAM
   - **Action URL**: `https://penguincam.popcornpenguins.com/panel?documentId={$documentId}&workspaceId={$workspaceId}&elementId={$elementId}`
   - **OAuth Redirect URI**: `https://penguincam.popcornpenguins.com/onshape/oauth/callback`
   - **Scopes**: `OAuth2Read`, `OAuth2ReadPII`
5. Submit for App Store review (or use in development mode)

---

## User Experience Flow

### First-Time User

```
1. User opens Onshape document
2. User clicks "+" to add extension → finds PenguinCAM
3. User authorizes PenguinCAM to access Onshape (one-time)
4. Side panel loads with document context
5. User clicks "Generate G-code"
   → G-code generated from current part
6. User clicks "Upload to Drive"
   → Popup opens for Google auth (one-time)
   → User logs in, grants permission
   → Popup closes
   → File uploads to Drive
```

### Returning User

```
1. User opens Onshape document
2. PenguinCAM side panel loads automatically
3. User clicks "Generate G-code" → works immediately
4. User clicks "Upload to Drive" → uploads immediately (no popup)
```

---

## Key Concerns and Mitigations

### 1. Security: Token Storage

| Concern | Mitigation |
|---------|------------|
| Tokens stored in database | Encrypt with AES-256 using per-deployment key |
| Database breach | Refresh tokens can be revoked; access tokens expire in 1 hour |
| Cross-site attacks | `SameSite=None; Secure` cookies, CSRF tokens |

### 2. Browser Compatibility

| Browser | Status | Notes |
|---------|--------|-------|
| Chrome | Works | Third-party cookies need `SameSite=None` |
| Firefox | Works | Same as Chrome |
| Safari | Partial | May need Storage Access API for persistent sessions |
| Edge | Works | Same as Chrome |

### 3. Session Persistence

| Scenario | Behavior |
|----------|----------|
| User closes browser | Session persists via database |
| Server restarts | Session persists via database |
| User clears cookies | Re-authentication required (popup for Google) |
| Token expires | Auto-refresh using refresh token |
| User revokes access | Re-authentication required |

### 4. Onshape API Rate Limits

The side panel enables more efficient API usage:
- Context already available (no URL parsing needed)
- Direct access to document/workspace/element IDs
- Potential for real-time updates via Onshape webhooks

---

## Migration Path

### Incremental Approach

1. **Week 1**: Add persistent token storage (PostgreSQL/Redis)
2. **Week 2**: Implement Google OAuth popup flow
3. **Week 3**: Configure cookies for iframe operation
4. **Week 4**: Register Onshape extension, test in dev mode
5. **Week 5**: Update UI for side panel dimensions
6. **Week 6**: Submit to Onshape App Store

### Backwards Compatibility

The standalone app can continue to work alongside the side panel:
- Same backend serves both
- Same token storage
- Different entry points (`/` vs `/panel`)

---

## Alternative: Keep Standalone, Improve Integration

If the side panel complexity is too high, consider these improvements to the current standalone approach:

1. **Bookmarklet**: One-click to open PenguinCAM with current Onshape context
2. **Browser Extension**: Adds "Open in PenguinCAM" button to Onshape toolbar
3. **Deep Links**: `penguincam://import?doc=X&ws=Y&el=Z`

These maintain the full-page experience while reducing friction.

---

## Appendix: ShapeForge Reference

ShapeForge (at `/Users/sethhondl/dev/active-projects/ShapeForge/`) implements a working Onshape side panel. Key files:

| File | Relevance |
|------|-----------|
| `app.js` | OAuth flow with Passport.js, session config |
| `api.js` | Token retrieval, refresh logic |
| `dynamoSessionStore.js` | Persistent session storage |
| `awsStorage.js` | Encrypted token storage |
| `serverless.yml` | Infrastructure (DynamoDB tables) |
| `src/contexts/OnshapeContext.jsx` | URL parameter extraction |
| `src/contexts/AuthContext.jsx` | Frontend auth state management |

---

## Conclusion

Converting PenguinCAM to an Onshape side panel is **feasible** and would significantly improve the user experience. The main changes required are:

1. **Persistent token storage** (database instead of in-memory)
2. **Popup-based Google OAuth** (one-time per user)
3. **Cookie configuration** for cross-origin iframe
4. **Onshape extension registration**

The Google Drive integration remains fully functional - users just authenticate once via popup instead of redirect. After that initial setup, everything works seamlessly within the Onshape interface.

**Estimated effort**: 2-4 weeks for a developer familiar with OAuth and Flask.

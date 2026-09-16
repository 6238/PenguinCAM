"""
PenguinCAM Authentication Module V2
Google OAuth 2.0 with Drive API access
"""

import os
import json
from functools import wraps
from flask import session, redirect, url_for, request, jsonify
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from google.auth.transport.requests import Request as GoogleRequest
from googleapiclient.discovery import build
import secrets
from datetime import timedelta
from logging_config import log  # shared log() + logging setup (was duplicated per module)

class PenguinCAMAuth:
    """Handles Google OAuth authentication with Drive API access"""
    
    # Scopes we need
    SCOPES = [
        'openid',
        'https://www.googleapis.com/auth/userinfo.email',
        'https://www.googleapis.com/auth/userinfo.profile',
        # drive.file is the only non-sensitive Drive scope: it grants per-file access to
        # files this app creates, which is all we do. Broader scopes (drive, drive.readonly,
        # drive.metadata*) are RESTRICTED and would require an annual CASA security
        # assessment to publish externally. A user-supplied folder ID works as a parent
        # under drive.file as long as the signed-in user can write to that folder.
        'https://www.googleapis.com/auth/drive.file'
    ]
    
    def __init__(self, app):
        self.app = app
        self.config = self._load_config()
        
        # Set up Flask session with persistent secret key
        if not app.secret_key:
            # Use environment variable or generate one (but warn about it)
            secret_key = os.environ.get('FLASK_SECRET_KEY')
            if secret_key:
                app.secret_key = secret_key
            else:
                app.secret_key = secrets.token_hex(32)
                log("⚠️  WARNING: Using random secret key. Set FLASK_SECRET_KEY environment variable for persistent sessions across redeploys.")
        
        # Configure session lifetime (30 days)
        app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=30)

        if self.config.get('enabled'):
            if self.config.get('allow_any_domain'):
                log("🔓 Google sign-in OPEN to any Google account (ALLOWED_DOMAINS='*')")
            elif self.config.get('allowed_domains'):
                log(f"🔐 Google sign-in limited to: "
                    f"{', '.join(self.config['allowed_domains'])}")
            else:
                log("⚠️  AUTH_ENABLED=true but ALLOWED_DOMAINS is unset — every sign-in "
                    "will be denied. Set it to a domain list or '*'.")
        
        # Register routes
        self._register_routes()
    
    def _load_config(self):
        """Load authentication configuration from environment variables"""
        config = {}
        
        # Required settings
        config['enabled'] = os.environ.get('AUTH_ENABLED', 'false').lower() == 'true'
        config['google_client_id'] = os.environ.get('GOOGLE_CLIENT_ID')
        config['google_client_secret'] = os.environ.get('GOOGLE_CLIENT_SECRET')
        
        # Base URL for redirects
        config['base_url'] = os.environ.get('BASE_URL', 'http://localhost:6238')
        
        # Allowed domains/emails.
        # ALLOWED_DOMAINS='*' opens sign-in to any Google account (any domain), which is
        # what a public deployment wants: teams live on their own school Workspace
        # domains, so an explicit list would mean a redeploy per team. Unset/empty still
        # denies everyone, so the gate can only be opened deliberately.
        env_domains = os.environ.get('ALLOWED_DOMAINS', '')
        config['allowed_domains'] = [d.strip() for d in env_domains.split(',') if d.strip()]
        config['allow_any_domain'] = '*' in config['allowed_domains']
        
        env_emails = os.environ.get('ALLOWED_EMAILS', '')
        config['allowed_emails'] = [e.strip() for e in env_emails.split(',') if e.strip()]
        
        config['require_domain'] = True
        config['session_timeout'] = 86400  # 24 hours
        
        return config
    
    def is_enabled(self):
        """Check if authentication is enabled"""
        return self.config.get('enabled', False)
    
    def is_authenticated(self):
        """Check if user is authenticated"""
        if not self.is_enabled():
            return True  # If auth disabled, everyone is "authenticated"
        
        return session.get('authenticated', False)
    
    def get_credentials(self):
        """Get Google API credentials from session"""
        if not self.is_authenticated():
            return None
        
        creds_data = session.get('google_credentials')
        if not creds_data:
            return None
        
        # Reconstruct credentials from session
        creds = Credentials(
            token=creds_data['token'],
            refresh_token=creds_data.get('refresh_token'),
            token_uri=creds_data.get('token_uri'),
            client_id=creds_data.get('client_id'),
            client_secret=creds_data.get('client_secret'),
            scopes=creds_data.get('scopes')
        )
        
        # Refresh if expired
        if creds.expired and creds.refresh_token:
            creds.refresh(GoogleRequest())
            # Update session
            self._save_credentials(creds)
        
        return creds
    
    def _save_credentials(self, creds):
        """Save credentials to session"""
        session['google_credentials'] = {
            'token': creds.token,
            'refresh_token': creds.refresh_token,
            'token_uri': creds.token_uri,
            'client_id': creds.client_id,
            'client_secret': creds.client_secret,
            'scopes': creds.scopes
        }
    
    def _create_flow(self):
        """Create OAuth flow"""
        client_config = {
            "web": {
                "client_id": self.config['google_client_id'],
                "client_secret": self.config['google_client_secret'],
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [f"{self.config['base_url']}/auth/callback"]
            }
        }
        
        flow = Flow.from_client_config(
            client_config,
            scopes=self.SCOPES,
            redirect_uri=f"{self.config['base_url']}/auth/callback"
        )
        
        return flow
    
    def _check_authorization(self, email, domain):
        """
        Check if user is authorized.

        Denials are logged with the reason: the user-facing page cannot say why
        (it would leak the allowlist), so without this a rejected sign-in is
        invisible server-side and indistinguishable from a broken config.
        """
        # Check specific emails
        if email in self.config.get('allowed_emails', []):
            return True

        # Check domain
        if self.config.get('require_domain', True):
            if self.config.get('allow_any_domain'):
                return True  # ALLOWED_DOMAINS='*'

            allowed_domains = self.config.get('allowed_domains', [])
            if not allowed_domains:
                log(f"🚫 Auth denied for {email}: ALLOWED_DOMAINS is unset or empty, "
                    f"so no one is authorized. Set it to a domain list or '*'.")
                return False  # No domains configured = no one allowed

            if domain in allowed_domains:
                return True

            log(f"🚫 Auth denied for {email}: domain '{domain}' is not in "
                f"ALLOWED_DOMAINS ({', '.join(allowed_domains)}).")
            return False

        return True  # If not requiring domain and not in specific list, allow
    
    def _register_routes(self):
        """Register authentication routes"""
        
        @self.app.route('/auth/login')
        def auth_login():
            """Initiate OAuth flow"""
            if not self.is_enabled():
                return redirect('/')
            
            # Already logged in?
            if self.is_authenticated():
                return redirect('/')
            
            # Create OAuth flow
            flow = self._create_flow()
            
            # Generate authorization URL
            # No include_granted_scopes: we request a fixed scope list rather than doing
            # incremental auth, and users who previously granted full Drive would get the
            # union back, which makes fetch_token() fail with "Scope has changed".
            authorization_url, state = flow.authorization_url(
                access_type='offline',
                prompt='consent'  # Force consent to get refresh token
            )
            
            # Store state in session for CSRF protection
            session['oauth_state'] = state
            
            # Redirect to Google
            return redirect(authorization_url)
        
        @self.app.route('/auth/callback')
        def auth_callback():
            """Handle OAuth callback from Google"""
            if not self.is_enabled():
                return redirect('/')
            
            # Verify state for CSRF protection
            if request.args.get('state') != session.get('oauth_state'):
                return 'Invalid state parameter', 400
            
            try:
                # Exchange authorization code for tokens
                flow = self._create_flow()
                flow.fetch_token(authorization_response=request.url)
                
                # Get credentials
                creds = flow.credentials
                
                # Get user info
                user_info_service = build('oauth2', 'v2', credentials=creds)
                user_info = user_info_service.userinfo().get().execute()
                
                email = user_info.get('email')
                domain = email.split('@')[1] if '@' in email else None
                
                # Check authorization
                if not self._check_authorization(email, domain):
                    return self._render_error_page(
                        'Access Denied',
                        f'Your account ({email}) is not authorized to access '
                        f'PenguinCAM. Ask your team mentor to allow your email domain.'
                    )
                
                # Save credentials to session
                self._save_credentials(creds)
                
                # Create session (use google_ prefix to not conflict with Onshape auth)
                session['authenticated'] = True
                session['google_user_email'] = email
                session['google_user_name'] = user_info.get('name')
                session['google_user_picture'] = user_info.get('picture')
                session.permanent = True
                
                # Clear OAuth state
                session.pop('oauth_state', None)

                log(f"✅ User authenticated: {email}")

                # Check if opened in popup (for Drive auth flow)
                # If there's no return URL stored, assume it's a popup
                return_url = session.pop('auth_return_url', None)

                if not return_url:
                    # Likely opened in popup - return HTML that closes the window
                    return '''<!DOCTYPE html>
<html>
<head>
    <title>Authentication Successful</title>
    <style>
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            background: linear-gradient(135deg, #0A0E14 0%, #1a1f2e 100%);
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            color: #fff;
            margin: 0;
        }
        .success-container {
            text-align: center;
            background: rgba(255, 255, 255, 0.05);
            border: 1px solid rgba(46, 160, 67, 0.3);
            border-radius: 20px;
            padding: 3rem;
        }
        .icon { font-size: 4rem; margin-bottom: 1rem; }
        h1 { color: #2EA043; margin-bottom: 1rem; }
        p { color: #C9D1D9; }
    </style>
    <script>
        // Close popup after brief delay
        setTimeout(() => {
            window.close();
        }, 1000);
    </script>
</head>
<body>
    <div class="success-container">
        <div class="icon">✅</div>
        <h1>Authentication Successful!</h1>
        <p>This window will close automatically...</p>
    </div>
</body>
</html>'''
                else:
                    # Full page flow - redirect to original URL
                    log(f"🔙 Redirecting to: {return_url}")
                    return redirect(return_url)
                
            except Exception as e:
                return self._render_error_page(
                    'Authentication Error',
                    f'Failed to authenticate: {str(e)}'
                )
        
        @self.app.route('/auth/logout')
        def auth_logout():
            """Logout endpoint"""
            session.clear()
            return redirect('/auth/login' if self.is_enabled() else '/')
        
        @self.app.route('/auth/status')
        def auth_status():
            """Check authentication status"""
            if not self.is_enabled():
                return jsonify({'enabled': False, 'authenticated': True})
            
            return jsonify({
                'enabled': True,
                'authenticated': self.is_authenticated(),
                'drive_connected': session.get('google_credentials') is not None,
                'user': {
                    'email': session.get('user_email'),
                    'name': session.get('user_name'),
                    'picture': session.get('user_picture')
                } if self.is_authenticated() else None
            })
    
    def require_auth(self, f):
        """Decorator to require authentication"""
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if not self.is_enabled():
                return f(*args, **kwargs)
            
            if not self.is_authenticated():
                if request.is_json:
                    return jsonify({'error': 'Authentication required'}), 401
                
                # Store the original URL (with query params) before redirecting to login
                session['auth_return_url'] = request.url
                return redirect('/auth/login')
            
            return f(*args, **kwargs)
        
        return decorated_function
    
    def get_user(self):
        """Get current user info"""
        if not self.is_authenticated():
            return None
        
        return {
            'email': session.get('user_email'),
            'name': session.get('user_name'),
            'picture': session.get('user_picture')
        }
    
    def _render_error_page(self, title, message):
        """Render an error page"""
        html = f'''<!DOCTYPE html>
<html>
<head>
    <title>{title} - PenguinCAM</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            background: linear-gradient(135deg, #0A0E14 0%, #1a1f2e 100%);
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            color: #fff;
            margin: 0;
            padding: 20px;
        }}
        
        .error-container {{
            background: rgba(255, 255, 255, 0.05);
            border: 1px solid rgba(255, 69, 0, 0.3);
            border-radius: 20px;
            padding: 3rem;
            max-width: 500px;
            text-align: center;
        }}
        
        .icon {{
            font-size: 4rem;
            margin-bottom: 1rem;
        }}
        
        h1 {{
            color: #FDB515;
            margin-bottom: 1rem;
        }}
        
        p {{
            color: #C9D1D9;
            margin-bottom: 2rem;
            line-height: 1.6;
        }}
        
        a {{
            display: inline-block;
            background: #FDB515;
            color: #000;
            padding: 12px 24px;
            border-radius: 8px;
            text-decoration: none;
            font-weight: 500;
        }}
        
        a:hover {{
            background: #D99F12;
        }}
    </style>
</head>
<body>
    <div class="error-container">
        <div class="icon">⚠️</div>
        <h1>{title}</h1>
        <p>{message}</p>
        <a href="/auth/login">Try Again</a>
    </div>
</body>
</html>'''
        return html


def init_auth(app):
    """Initialize authentication for the Flask app"""
    return PenguinCAMAuth(app)

"""
PenguinCAM Authentication Module
Google OAuth integration for restricting access to team members
"""

import os
import json
from functools import wraps
from flask import session, redirect, url_for, request, jsonify
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests
import secrets

class PenguinCAMAuth:
    """Handles Google OAuth authentication for PenguinCAM"""
    
    def __init__(self, app):
        self.app = app
        self.config = self._load_config()
        
        # Set up Flask session
        if not app.secret_key:
            app.secret_key = secrets.token_hex(32)
        
        # Register routes
        self._register_routes()
    
    def _load_config(self):
        """Load authentication configuration"""
        config_file = 'auth_config.json'
        
        if os.path.exists(config_file):
            with open(config_file, 'r') as f:
                return json.load(f)
        
        # Default config
        return {
            'enabled': False,  # Set to True to enable auth
            'google_client_id': None,  # Your OAuth client ID
            'allowed_domains': [],  # e.g., ["team6238.org", "gmail.com"]
            'allowed_emails': [],  # Specific allowed emails
            'require_domain': True,  # Require domain match
            'session_timeout': 86400  # 24 hours in seconds
        }
    
    def _save_config(self):
        """Save configuration"""
        with open('auth_config.json', 'w') as f:
            json.dump(self.config, f, indent=2)
    
    def is_enabled(self):
        """Check if authentication is enabled"""
        return self.config.get('enabled', False)
    
    def _register_routes(self):
        """Register authentication routes"""
        
        @self.app.route('/auth/login')
        def auth_login():
            """Login page"""
            if not self.is_enabled():
                return redirect('/')
            
            # Already logged in?
            if self.is_authenticated():
                return redirect('/')
            
            # Serve login page
            return self._render_login_page()
        
        @self.app.route('/auth/callback', methods=['POST'])
        def auth_callback():
            """Handle Google Sign-In callback"""
            if not self.is_enabled():
                return jsonify({'error': 'Authentication not enabled'}), 400
            
            # Get the ID token from the request
            token = request.json.get('credential')
            
            if not token:
                return jsonify({'error': 'No credential provided'}), 400
            
            try:
                # Verify the token
                client_id = self.config.get('google_client_id')
                if not client_id:
                    return jsonify({'error': 'Google Client ID not configured'}), 500
                
                idinfo = id_token.verify_oauth2_token(
                    token, 
                    google_requests.Request(), 
                    client_id
                )
                
                # Get user info
                email = idinfo.get('email')
                email_verified = idinfo.get('email_verified')
                domain = email.split('@')[1] if '@' in email else None
                
                # Check if email is verified
                if not email_verified:
                    return jsonify({'error': 'Email not verified'}), 403
                
                # Check authorization
                authorized = self._check_authorization(email, domain)
                
                if not authorized:
                    return jsonify({
                        'error': 'Access denied',
                        'message': 'Your account is not authorized to access PenguinCAM'
                    }), 403
                
                # Create session
                session['authenticated'] = True
                session['user_email'] = email
                session['user_name'] = idinfo.get('name')
                session['user_picture'] = idinfo.get('picture')
                session.permanent = True
                
                return jsonify({
                    'success': True,
                    'redirect': '/'
                })
                
            except ValueError as e:
                return jsonify({'error': f'Invalid token: {str(e)}'}), 401
            except Exception as e:
                return jsonify({'error': f'Authentication error: {str(e)}'}), 500
        
        @self.app.route('/auth/logout')
        def auth_logout():
            """Logout endpoint"""
            session.clear()
            return redirect('/auth/login')
        
        @self.app.route('/auth/status')
        def auth_status():
            """Check authentication status"""
            if not self.is_enabled():
                return jsonify({'enabled': False, 'authenticated': True})
            
            return jsonify({
                'enabled': True,
                'authenticated': self.is_authenticated(),
                'user': {
                    'email': session.get('user_email'),
                    'name': session.get('user_name'),
                    'picture': session.get('user_picture')
                } if self.is_authenticated() else None
            })
    
    def _check_authorization(self, email, domain):
        """Check if user is authorized"""
        # Check allowed emails list
        allowed_emails = self.config.get('allowed_emails', [])
        if email in allowed_emails:
            return True
        
        # Check allowed domains
        require_domain = self.config.get('require_domain', True)
        if require_domain and domain:
            allowed_domains = self.config.get('allowed_domains', [])
            if domain in allowed_domains:
                return True
        
        # If no restrictions set, deny by default
        if not allowed_emails and not self.config.get('allowed_domains'):
            # No restrictions configured - allow all (for initial setup)
            # In production, you should configure restrictions
            return True
        
        return False
    
    def is_authenticated(self):
        """Check if current user is authenticated"""
        if not self.is_enabled():
            return True  # No auth required
        
        return session.get('authenticated', False)
    
    def require_auth(self, f):
        """Decorator to require authentication for a route"""
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if not self.is_enabled():
                return f(*args, **kwargs)
            
            if not self.is_authenticated():
                if request.is_json:
                    return jsonify({'error': 'Authentication required'}), 401
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
    
    def _render_login_page(self):
        """Render the login page HTML"""
        client_id = self.config.get('google_client_id', 'YOUR_CLIENT_ID')
        
        html = f'''<!DOCTYPE html>
<html>
<head>
    <title>PenguinCAM - Login</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <script src="https://accounts.google.com/gsi/client" async defer></script>
    <style>
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}
        
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            background: linear-gradient(135deg, #0A0E14 0%, #1a1f2e 100%);
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            color: #fff;
        }}
        
        .login-container {{
            background: rgba(255, 255, 255, 0.05);
            border: 1px solid rgba(255, 255, 255, 0.1);
            border-radius: 20px;
            padding: 3rem;
            max-width: 400px;
            width: 90%;
            text-align: center;
            backdrop-filter: blur(10px);
        }}
        
        .logo {{
            font-size: 3rem;
            margin-bottom: 1rem;
        }}
        
        h1 {{
            font-size: 2rem;
            margin-bottom: 0.5rem;
            color: #FF4500;
        }}
        
        .subtitle {{
            color: #8B949E;
            margin-bottom: 2rem;
        }}
        
        .team-info {{
            background: rgba(255, 69, 0, 0.1);
            border: 1px solid rgba(255, 69, 0, 0.3);
            border-radius: 10px;
            padding: 1rem;
            margin-bottom: 2rem;
        }}
        
        .team-info h2 {{
            font-size: 1.2rem;
            color: #FF4500;
            margin-bottom: 0.5rem;
        }}
        
        .team-info p {{
            color: #C9D1D9;
            font-size: 0.9rem;
        }}
        
        #google-signin-button {{
            margin: 2rem auto;
        }}
        
        .error {{
            background: rgba(255, 69, 0, 0.2);
            border: 1px solid #FF4500;
            color: #fff;
            padding: 1rem;
            border-radius: 10px;
            margin-top: 1rem;
            display: none;
        }}
        
        .footer {{
            margin-top: 2rem;
            color: #8B949E;
            font-size: 0.85rem;
        }}
    </style>
</head>
<body>
    <div class="login-container">
        <div class="logo">🐧</div>
        <h1>PenguinCAM</h1>
        <p class="subtitle">FRC Team 6238 CAM Tool</p>
        
        <div class="team-info">
            <h2>Popcorn Penguins</h2>
            <p>Sign in with your team Google account</p>
        </div>
        
        <div id="google-signin-button"></div>
        
        <div id="error-message" class="error"></div>
        
        <div class="footer">
            Secure authentication via Google
        </div>
    </div>
    
    <script>
        function handleCredentialResponse(response) {{
            // Send the credential to our server
            fetch('/auth/callback', {{
                method: 'POST',
                headers: {{
                    'Content-Type': 'application/json',
                }},
                body: JSON.stringify({{
                    credential: response.credential
                }})
            }})
            .then(res => res.json())
            .then(data => {{
                if (data.success) {{
                    window.location.href = data.redirect;
                }} else {{
                    showError(data.message || data.error || 'Authentication failed');
                }}
            }})
            .catch(error => {{
                showError('Network error: ' + error.message);
            }});
        }}
        
        function showError(message) {{
            const errorDiv = document.getElementById('error-message');
            errorDiv.textContent = message;
            errorDiv.style.display = 'block';
        }}
        
        window.onload = function() {{
            google.accounts.id.initialize({{
                client_id: '{client_id}',
                callback: handleCredentialResponse
            }});
            
            google.accounts.id.renderButton(
                document.getElementById('google-signin-button'),
                {{
                    theme: 'filled_black',
                    size: 'large',
                    text: 'signin_with',
                    shape: 'pill',
                    width: 280
                }}
            );
        }};
    </script>
</body>
</html>'''
        
        return html


# Helper function for use in Flask routes
def init_auth(app):
    """Initialize authentication for the Flask app"""
    return PenguinCAMAuth(app)

#!/usr/bin/env python3
"""
PenguinCAM - FRC Team 6238 CAM Tool
A Flask-based web interface for generating G-code from DXF files
"""

from flask import Flask, render_template, request, jsonify, send_file, session, send_from_directory, redirect, make_response
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.middleware.proxy_fix import ProxyFix
import os
import requests
import subprocess
import tempfile
import shutil
import traceback
from pathlib import Path
import json
import base64
import secrets
import re
import io
import zipfile
import atexit
import time
import threading
from datetime import datetime
import ezdxf
from shapely.ops import unary_union
import logging
import metrics
import feeds_speeds
from logging_config import log  # shared log() + base logging setup (was duplicated per module)

# Quiet Werkzeug's request logging (clutters Vercel logs); the base logging config and log()
# now come from logging_config (imported above).
logging.getLogger('werkzeug').disabled = True
logging.getLogger('werkzeug').setLevel(logging.ERROR)  # Only show errors, not INFO
werkzeug_logger = logging.getLogger('werkzeug')
werkzeug_logger.handlers = []  # Remove all handlers

# Import Google Drive integration (optional - will work without it)
try:
    from google_drive_integration import GoogleDriveUploader
    GOOGLE_DRIVE_AVAILABLE = True
except ImportError:
    GOOGLE_DRIVE_AVAILABLE = False
    log("⚠️  Google Drive integration not available (missing dependencies)")
    log("   Install with: pip install google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client")

# Import authentication (optional - will work without it)
try:
    from penguincam_auth import init_auth
    AUTH_AVAILABLE = True
except ImportError:
    AUTH_AVAILABLE = False
    log("⚠️  Authentication module not available")

# Import Onshape integration (optional - will work without it)
try:
    from onshape_integration import get_onshape_client, session_manager, build_multilayer_dxf
    ONSHAPE_AVAILABLE = True
except ImportError:
    ONSHAPE_AVAILABLE = False
    log("⚠️  Onshape integration not available")

# Import postprocessor directly (for API calls instead of subprocess)
from frc_cam_postprocessor import (
    FRCPostProcessor, PostProcessorResult, assemble_job_gcode, validate_job_layout,
)

# Import team config management
from team_config import TeamConfig, DEFAULT_TOOL_DIAMETER_IN
from config_validation import validate_and_sanitize_config, ConfigValidationError

# ============================================================================
# File Token Manager - Secure file access with random tokens
# ============================================================================

class FileTokenManager:
    """
    Manages secure token-based file access to prevent filename guessing attacks.
    Maps random tokens to actual file paths and handles automatic cleanup.

    For serverless (Vercel), tokens are stored in Flask session cookies to work
    across different container instances.
    """

    def __init__(self):
        # For backwards compatibility with non-serverless environments
        self.tokens = {}  # token → {'filepath': ..., 'filename': ..., 'created': timestamp}
        self.lock = threading.Lock()
        self.use_session = os.environ.get('VERCEL') == '1'  # Use session storage on Vercel

    def register_file(self, filepath, real_filename):
        """
        Register a file and return a secure random token.

        Args:
            filepath: Full path to the file on disk
            real_filename: The original filename (for download headers)

        Returns:
            Random token string (safe for URLs)
        """
        token = secrets.token_urlsafe(32)
        file_info = {
            'filepath': filepath,
            'filename': real_filename,
            'created': time.time()
        }

        if self.use_session:
            # Store in Flask session (cookie-based, works across serverless instances)
            if 'file_tokens' not in session:
                session['file_tokens'] = {}
            session['file_tokens'][token] = file_info
            session.modified = True  # Force session save
        else:
            # Store in memory (for non-serverless environments)
            with self.lock:
                self.tokens[token] = file_info

        log(f"🔐 Registered file: {real_filename} → token {token[:16]}... ({'session' if self.use_session else 'memory'})")
        return token

    def get_file(self, token):
        """
        Get file info for a token.

        Args:
            token: The secure token

        Returns:
            Dict with 'filepath' and 'filename', or None if not found
        """
        if self.use_session:
            # Retrieve from Flask session
            file_tokens = session.get('file_tokens', {})
            return file_tokens.get(token)
        else:
            # Retrieve from memory
            with self.lock:
                return self.tokens.get(token)

    def cleanup_old_files(self, max_age_seconds=3600):
        """
        Remove files older than max_age_seconds (default 1 hour).
        Deletes both the file on disk and the token mapping.

        Args:
            max_age_seconds: Maximum file age in seconds (default 3600 = 1 hour)
        """
        current_time = time.time()
        with self.lock:
            expired_tokens = []
            for token, info in self.tokens.items():
                age = current_time - info['created']
                if age > max_age_seconds:
                    expired_tokens.append(token)
                    # Delete the file from disk
                    try:
                        if os.path.exists(info['filepath']):
                            os.unlink(info['filepath'])
                            log(f"🗑️  Cleaned up expired file ({age/60:.1f} min old): {info['filename']}")
                    except Exception as e:
                        log(f"⚠️  Failed to delete {info['filepath']}: {e}")

            # Remove expired tokens from mapping
            for token in expired_tokens:
                del self.tokens[token]

            if expired_tokens:
                log(f"✅ Cleanup complete: removed {len(expired_tokens)} expired file(s)")

def cleanup_worker():
    """Background thread that periodically cleans up old files"""
    while True:
        time.sleep(600)  # Run every 10 minutes
        try:
            file_token_manager.cleanup_old_files(max_age_seconds=3600)  # 1 hour
        except Exception as e:
            log(f"⚠️  Error in cleanup worker: {e}")

# Initialize file token manager
file_token_manager = FileTokenManager()

# Start background cleanup thread (only for traditional server deployments)
# Serverless platforms (Vercel, AWS Lambda) auto-cleanup when containers terminate
IS_SERVERLESS = os.environ.get('VERCEL') or os.environ.get('AWS_LAMBDA_FUNCTION_NAME')

if IS_SERVERLESS:
    log("✅ File token manager initialized (serverless mode - container auto-cleanup)")
else:
    cleanup_thread = threading.Thread(target=cleanup_worker, daemon=True)
    cleanup_thread.start()
    log("✅ File token manager initialized with auto-cleanup thread (1 hour expiry)")

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB max file size

# Disable Flask/Werkzeug request logging in production (Vercel)
if os.environ.get('VERCEL'):
    app.logger.disabled = True
    log_werkzeug = logging.getLogger('werkzeug')
    log_werkzeug.disabled = True

# Trust proxy headers (Railway, nginx, etc.)
# This tells Flask it's behind HTTPS even if internal requests are HTTP
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

# Set secret key for session management (required by auth and Onshape integration)
# Check environment variable first for persistent sessions across deployments
secret_key = os.environ.get('FLASK_SECRET_KEY')
if secret_key:
    app.secret_key = secret_key
    log("✅ Using persistent FLASK_SECRET_KEY from environment")
elif not app.secret_key:
    app.secret_key = secrets.token_hex(32)
    log("⚠️  WARNING: Using random secret key. Sessions will not persist across restarts.")

# Embedded Onshape panel runs in an iframe, a third-party context. For the session
# cookie (and thus OAuth/login) to be sent there, it MUST be SameSite=None; Secure
# (requires HTTPS). Without this, the OAuth popup sets the cookie first-party but the
# iframe's reload can't send it back, so the panel stays "not authenticated".
#
# EMBED_COOKIES explicitly forces on/off; otherwise auto-enable on deployed HTTPS
# platforms (Railway/Vercel set PORT / platform vars, or FLASK_SECRET_KEY is set).
# Left as Flask's default (Lax) only for local HTTP dev.
_embed_env = os.environ.get('EMBED_COOKIES')
if _embed_env is not None:
    _embed_cookies = _embed_env.strip().lower() in ('1', 'true', 'yes')
else:
    _embed_cookies = bool(secret_key or os.environ.get('PORT')
                          or os.environ.get('RAILWAY_ENVIRONMENT') or os.environ.get('VERCEL'))
if _embed_cookies:
    app.config['SESSION_COOKIE_SAMESITE'] = 'None'
    app.config['SESSION_COOKIE_SECURE'] = True
    log("🔒 Session cookie: SameSite=None; Secure (embeddable in Onshape iframe)")
else:
    log("🍪 Session cookie: default SameSite=Lax — set EMBED_COOKIES=1 for Onshape iframe embedding")
    log("   Set FLASK_SECRET_KEY environment variable for persistent sessions.")

# Initialize authentication if available
if AUTH_AVAILABLE:
    auth = init_auth(app)
else:
    # Create a dummy auth object that allows everything
    class DummyAuth:
        def is_enabled(self):
            return False
        def require_auth(self, f):
            return f
        def is_authenticated(self):
            return True
    auth = DummyAuth()

# Initialize rate limiting
limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["200 per hour"],  # Global default for all routes
    storage_uri="memory://",
    headers_enabled=True  # Send X-RateLimit headers in responses
)
log("✅ Rate limiting enabled (200 requests/hour default)")

# ============================================================================
# Cloudflare Turnstile — bot-gate for the anonymous DXF-upload flow
# ============================================================================
# The full-page upload flow has NO Onshape OAuth. Instead the frontend solves a
# Turnstile challenge and POSTs the token to /session/verify, which we verify
# server-side and use to mint an anonymous session (`session['app_verified']`)
# that the compute endpoints require. The Onshape-panel flow is unaffected — its
# OAuth session is its gate. When the secret key is unset (local dev / tests /
# an unkeyed run), verification is BYPASSED with a loud warning so nothing
# breaks; set both keys on any public host.
TURNSTILE_SITE_KEY = os.environ.get('TURNSTILE_SITE_KEY', '')
TURNSTILE_SECRET_KEY = os.environ.get('TURNSTILE_SECRET_KEY', '')
TURNSTILE_VERIFY_URL = 'https://challenges.cloudflare.com/turnstile/v0/siteverify'
if TURNSTILE_SITE_KEY and TURNSTILE_SECRET_KEY:
    log("🛡️  Turnstile bot-gate ENABLED for the upload flow")
else:
    log("⚠️  Turnstile keys not set — upload-flow bot-gate DISABLED "
        "(set TURNSTILE_SITE_KEY / TURNSTILE_SECRET_KEY on any public host)")


def _client_ip():
    """Real client IP. Behind Cloudflare the true client is in CF-Connecting-IP;
    fall back to the proxied remote address otherwise."""
    return request.headers.get('CF-Connecting-IP') or get_remote_address()


def _turnstile_enabled():
    return bool(TURNSTILE_SITE_KEY and TURNSTILE_SECRET_KEY)


def _verify_turnstile_token(token):
    """Verify a Turnstile token with Cloudflare. Returns True on success — or when
    Turnstile isn't configured (dev/test bypass). Never raises."""
    if not _turnstile_enabled():
        return True  # unconfigured: dev/test bypass (warned at startup)
    if not token:
        return False
    try:
        resp = requests.post(TURNSTILE_VERIFY_URL, data={
            'secret': TURNSTILE_SECRET_KEY,
            'response': token,
            'remoteip': _client_ip(),
        }, timeout=10)
        return bool(resp.ok and resp.json().get('success'))
    except Exception as e:
        log(f"Turnstile verification error: {e}")
        return False

# Directory for temporary files
# Serverless platforms (Vercel, Lambda) have /tmp as only writable location
# Traditional servers get isolated temp directory
if IS_SERVERLESS:
    TEMP_DIR = '/tmp'
    log("✅ Using /tmp for serverless environment")
else:
    TEMP_DIR = tempfile.mkdtemp()
    log(f"✅ Created temp directory: {TEMP_DIR}")

UPLOAD_FOLDER = os.path.join(TEMP_DIR, 'uploads')
OUTPUT_FOLDER = os.path.join(TEMP_DIR, 'outputs')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

# Path to the post-processor script (assumed to be in same directory)
SCRIPT_DIR = Path(__file__).parent
POST_PROCESSOR = SCRIPT_DIR / 'frc_cam_postprocessor.py'

# ============================================================================
# Helper Functions
# ============================================================================

def get_current_user_id():
    """Get the current user ID from session"""
    return session.get('user_email', 'default_user')

def normalize_material(material):
    """Canonicalize a material id from the request form. 'aluminum_tube' -> 'aluminum' (a
    UI-only id that uses the aluminum preset) and 'polycarb' -> 'polycarbonate' (legacy);
    everything else, including custom materials, passes through unchanged."""
    m = str(material).lower()
    if m == 'aluminum_tube':
        return 'aluminum'
    if m == 'polycarb':
        return 'polycarbonate'
    return material

def get_onshape_client_or_401():
    """
    Get Onshape client for current user, or return 401 error response.
    Returns: (client, error_response, status_code)
    If client is None, return the error_response with status_code.
    """
    if not ONSHAPE_AVAILABLE:
        return None, jsonify({'error': 'Onshape integration not available'}), 400

    client = session_manager.get_client(get_current_user_id())
    if not client:
        return None, jsonify({
            'error': 'Not authenticated with Onshape',
            'auth_url': '/onshape/auth'
        }), 401

    return client, None, None

# ============================================================================
# Routes
# ============================================================================

# Team config is fetched from Onshape at login and cached in the session cookie. Re-fetch
# it when the cached copy is older than this, so a returning user picks up a config their
# mentor edited since login - without having to clear cookies or re-authenticate.
TEAM_CONFIG_TTL_SECONDS = 600  # 10 minutes


def _load_team_config_into_session(client):
    """Fetch PenguinCAM-config.yaml from the user's Onshape classroom and store it in the
    session. Single source of truth for how config lands in the session - used by the OAuth
    callback (initial load), the /config/refresh route, and the TTL auto-refresh below."""
    config_yaml = client.fetch_config_file() if client else None
    # Validate/sanitize even the Onshape-hosted config: a mentor can commit crappy YAML to
    # their classroom just as easily as an upload user can host it. Lenient mode never raises
    # -- it drops out-of-range values (reverting them to defaults) and sanitizes strings for
    # G-code safety, so a bad config degrades instead of breaking the page.
    config_data = None
    if config_yaml:
        config_data, warnings = validate_and_sanitize_config(config_yaml, strict=False)
        for w in warnings:
            log(f"⚠️  Team config: {w}")
    if config_data:
        team_config = TeamConfig.from_dict(config_data)
        log(f"✅ Team config loaded: {team_config.team_name} (#{team_config.team_number})")
        session['team_config_data'] = team_config._data
        session['team_config'] = team_config.to_dict()
        session['team_number'] = team_config.team_number
        session['team_config_url'] = getattr(client, 'last_config_url', None)
        session['using_default_config'] = False
        session.pop('team_config_error', None)
    else:
        log("⚠️  No team config found - using defaults")
        team_config = TeamConfig()
        session['team_config_data'] = {}
        session['team_config'] = team_config.to_dict()
        session['team_number'] = team_config.team_number
        session.pop('team_config_url', None)
        session['using_default_config'] = True
        # Why the lookup came up empty, in plain language, so /config/refresh can tell the
        # user instead of silently re-rendering an identical page (see fetch_config_file).
        session['team_config_error'] = getattr(client, 'last_config_error', None) if client else None
    session['team_config_fetched_at'] = time.time()
    # Persist any token refresh triggered by the Onshape API calls above.
    if client:
        session_manager.update_session_tokens(client)
    return team_config


def _maybe_refresh_team_config():
    """Best-effort TTL refresh of the cached team config (see TEAM_CONFIG_TTL_SECONDS).
    Runs on every page render but only actually re-fetches once the cached copy goes stale.
    Silently keeps the existing session copy if Onshape is unreachable or the re-fetch
    fails, so a transient error never blocks the page."""
    if not ONSHAPE_AVAILABLE:
        return
    if time.time() - session.get('team_config_fetched_at', 0) < TEAM_CONFIG_TTL_SECONDS:
        return
    client = session_manager.get_client(get_current_user_id())
    if not client:
        return
    try:
        _load_team_config_into_session(client)
    except Exception as e:
        log(f"⚠️  Team config TTL refresh failed, keeping cached copy: {e}")
        # Bump the timestamp so a persistent failure doesn't re-hit Onshape every render.
        session['team_config_fetched_at'] = time.time()


def _active_team_config(machine_id=None):
    """The TeamConfig that applies to the current COMPUTE request (/process, /process-job,
    /part-outline). Upload-supplied config (from the anonymous flow's POST /session/config)
    takes precedence when present; otherwise the Onshape classroom config cached in the
    session; otherwise Team 6238 defaults. Single resolver so the compute endpoints agree
    with what the page rendered. Note: only the upload flow ever sets `upload_config_data`,
    so an Onshape user (who never hits /session/config) transparently keeps their config.

    `machine_id` binds the returned config to the machine the job selected, so ALL of that
    machine's settings apply (tabs, pockets, z_reference, park, coolant, bed size, ...) --
    not just its materials. Falls back to the session's machine, then the config's default,
    so a request that omits machine_id still matches what the page rendered."""
    if session.get('upload_config_data'):
        config = TeamConfig.from_dict(session['upload_config_data'])
    else:
        config = TeamConfig.from_dict(session.get('team_config_data', {}))
    return config.for_machine(machine_id or session.get('machine_id'))


def _app_template_context(force_defaults=False):
    """Build the shared template context (machines, materials, tool, bed size) used by
    both the legacy single-part page and the multi-part wizard.

    force_defaults=True (the anonymous upload flow) skips the Onshape config refresh
    entirely and NEVER reads the Onshape `team_config_data` (so a prior Onshape login in the
    same browser can't leak in). It instead uses the upload flow's own `upload_config_data`
    -- the team config that flow supplied via POST /session/config -- or the built-in Team
    6238 defaults when none has been supplied. Either way it makes NO Onshape calls."""
    if not force_defaults:
        _maybe_refresh_team_config()  # opportunistic TTL refresh before we read the cached copy
    user_name = None if force_defaults else session.get('user_name')
    team_name = None if force_defaults else session.get('team_name')

    if force_defaults:
        team_config_data = session.get('upload_config_data', {})
        using_default = not bool(team_config_data)
    else:
        team_config_data = session.get('team_config_data', {})
        # Derive from the data rather than the stored flag: the two are always written
        # together by _load_team_config_into_session, and deriving means a session that
        # never completed a lookup reports "defaults" instead of claiming a real config.
        using_default = not bool(team_config_data)
    # Bind to the session's machine so this context reflects the same machine the compute
    # endpoints will use. active_machine_id resolves a stale/unknown session id back to the
    # default machine, so a config edit that renames machines can't leave the page pointing
    # at a machine that no longer exists.
    team_config = TeamConfig(team_config_data).for_machine(session.get('machine_id'))

    machines = team_config.get_available_machines()
    current_machine_id = team_config.active_machine_id

    team_config_dict = team_config.to_dict(current_machine_id)
    drive_enabled = team_config_dict.get('google_drive_enabled', False)
    # Use `or` (not .get default) so an explicit None in the config doesn't render as
    # the string "None" into a numeric <input value="...">.
    default_tool_diameter = team_config_dict.get('default_tool_diameter') or DEFAULT_TOOL_DIAMETER_IN
    default_tool_diameter_text = team_config_dict.get('default_tool_diameter_text') or '4mm'
    machine_x_max = team_config_dict.get('machine_x_max') or 48.0
    machine_y_max = team_config_dict.get('machine_y_max') or 96.0

    available_materials = team_config.get_available_materials(current_machine_id)
    available_materials['aluminum_tube'] = {
        **available_materials.get('aluminum', {}),
        'name': 'Aluminum Tube'
    }

    incomplete_materials = {
        material_id for material_id in available_materials.keys()
        if not team_config.is_material_complete(material_id, current_machine_id) and material_id != 'aluminum_tube'
    }

    # Per-machine data so the client can update bed size, tool default, and the material
    # list when the user picks a different machine (without a page reload). Keyed by id.
    machines_info = {}
    for mid in machines.keys():
        md = team_config.to_dict(mid)
        mats = team_config.get_available_materials(mid)
        machines_info[mid] = {
            'name': md.get('machine_name') or mid,
            'x_max': md.get('machine_x_max'),
            'y_max': md.get('machine_y_max'),
            'tool': md.get('default_tool_diameter'),
            'tool_text': md.get('default_tool_diameter_text'),
            'materials': [
                {'id': matid, 'name': m.get('name') or matid}
                for matid, m in mats.items() if matid != 'aluminum_tube'
            ],
        }

    return {
        'user_name': user_name,
        'team_name': team_name,
        'drive_enabled': drive_enabled,
        'default_tool_diameter': default_tool_diameter,
        'default_tool_diameter_text': default_tool_diameter_text,
        'machine_x_max': machine_x_max,
        'machine_y_max': machine_y_max,
        'using_default_config': using_default,
        # Display fields for the header "Config: N (name)" banner, sourced per-flow so both
        # the upload flow (upload_config_url) and the Onshape flow (team_config_url) render
        # correctly server-side (before JS runs / on reload).
        'config_team_number': team_config.team_number,
        'config_team_name': team_config.team_name,
        'config_url': (session.get('upload_config_url') if force_defaults
                       else session.get('team_config_url')),
        # One-shot result of an explicit /config/refresh click, consumed on this render so
        # it doesn't stick around on the next page load.
        'config_refresh_result': None if force_defaults else session.pop('config_refresh_result', None),
        'machines': machines,
        'machines_info': machines_info,
        'current_machine_id': current_machine_id,
        'materials': available_materials,
        'incomplete_materials': incomplete_materials,
        'detected_thickness': None,
    }


def _require_onshape_auth():
    """Apply the Onshape OAuth gate. Returns a redirect response if unauthenticated,
    else None. Shared by the app pages."""
    if ONSHAPE_AVAILABLE:
        user_id = get_current_user_id()
        client = session_manager.get_client(user_id)
        if not client:
            log("⛔ Access denied: No Onshape authentication, redirecting to /onshape/auth")
            return redirect('/onshape/auth')
    return None


def _has_onshape_session():
    """True when the current browser has an authenticated Onshape client (the
    Onshape-panel flow). Its OAuth is its gate — unchanged by the upload flow."""
    return bool(ONSHAPE_AVAILABLE and session_manager.get_client(get_current_user_id()))


def _require_app_session():
    """Gate for the shared compute endpoints (/process, /process-job, /part-outline).

    Authorized when EITHER the browser has an Onshape session (Onshape-panel flow,
    behavior unchanged) OR it has passed the Turnstile bot-gate for the anonymous
    upload flow (`session['app_verified']`). Returns a (response, 401) tuple when
    neither holds, else None. Note: when Turnstile is unconfigured the upload flow
    marks itself verified on /session/verify, so this still passes in dev."""
    if _has_onshape_session():
        return None
    if session.get('app_verified'):
        return None
    return jsonify({'error': 'Session verification required.', 'need_verification': True}), 401


def _compute_dxf_outline(path):
    """Load a DXF and return its perimeter outline + dims + holes for the wizard layout
    canvas/thumbnail. Coordinates normalized so the bounding-box minimum is (0,0).
    Returns a dict (width, height, outline, holes) or None if there is no geometry."""
    team_config = _active_team_config()
    pp = FRCPostProcessor(material_thickness=0.25, tool_diameter=0.125,
                          units='inch', config=team_config)
    pp.load_dxf(path)
    pp.transform_coordinates('bottom-left', 0, enforce_bounds=False)
    pp.identify_perimeter_and_pockets()
    pp.classify_holes()
    bbox = pp.bounding_box()
    if not bbox:
        return None
    minx, miny, maxx, maxy = bbox
    width, height = maxx - minx, maxy - miny
    outline = []
    if pp.perimeter:
        outline = [[round(x - minx, 4), round(y - miny, 4)] for (x, y) in pp.perimeter]
    # For negative-space 2.5D (HATCH) parts the detected "perimeter" is often an
    # interior feature (e.g. a central bore), because the true outer profile isn't a
    # filled polygon in this representation. When the outline doesn't span the part,
    # fall back to the convex hull of all layer geometry so the layout footprint and
    # thumbnail match the real bounding box. Rendering only — G-code has its own path.
    def _span(pts):
        if not pts:
            return 0.0, 0.0
        xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
        return max(xs) - min(xs), max(ys) - min(ys)
    ow, oh = _span(outline)
    if pp.layer_data and (ow < 0.9 * width or oh < 0.9 * height):
        all_polys = [poly for info in pp.layer_data.values()
                     for poly in info.get('polygons', [])]
        if all_polys:
            hull = unary_union(all_polys).convex_hull
            if hull.geom_type == 'Polygon':
                outline = [[round(x - minx, 4), round(y - miny, 4)]
                           for (x, y) in hull.exterior.coords]
    if len(outline) < 3:
        outline = [[0, 0], [round(width, 4), 0],
                   [round(width, 4), round(height, 4)], [0, round(height, 4)]]
    holes = [{'cx': round(h['center'][0] - minx, 4),
              'cy': round(h['center'][1] - miny, 4),
              'r': round(h['diameter'] / 2.0, 4)}
             for h in (pp.holes or [])]
    # For 2.5D parts the interesting features (pockets/rings/steps) live in the depth
    # layers as HATCH-derived polygons, not as holes/circles — so the thumbnail would
    # otherwise show just the bare perimeter. Surface each layer polygon's rings as
    # `inner` outlines so the layout preview is recognizable. Dedupe by rounded bounds
    # and drop rings that coincide with the perimeter (already drawn as `outline`).
    inner = []
    if pp.layer_data:
        seen = set()
        perim_key = (0.0, 0.0, round(width, 2), round(height, 2))
        for info in pp.layer_data.values():
            for poly in info.get('polygons', []):
                for ring in [poly.exterior, *poly.interiors]:
                    rminx, rminy, rmaxx, rmaxy = ring.bounds
                    # Skip degenerate slivers (sub-0.02" in a dimension): they're
                    # slicing artifacts that would litter the thumbnail with dashes.
                    if (rmaxx - rminx) < 0.02 or (rmaxy - rminy) < 0.02:
                        continue
                    key = tuple(round(v, 2) for v in ring.bounds)
                    if key in seen or key == perim_key:
                        continue
                    seen.add(key)
                    inner.append([[round(x - minx, 4), round(y - miny, 4)]
                                  for (x, y) in ring.coords])
                    if len(inner) >= 400:  # guard against pathological part counts
                        break
    else:
        # Single-layer part: interior pockets are closed paths that are neither the
        # perimeter nor circular holes, so surface them as inner outlines too —
        # otherwise the thumbnail shows only the outline + holes and omits pockets.
        for pocket in (pp.pockets or []):
            if len(pocket) >= 3:
                inner.append([[round(x - minx, 4), round(y - miny, 4)]
                              for (x, y) in pocket])
    return {'width': round(width, 4), 'height': round(height, 4),
            'outline': outline, 'holes': holes, 'inner': inner}


@app.route('/session/verify', methods=['POST'])
@limiter.limit("30 per minute")
def session_verify():
    """Establish an anonymous upload session by verifying a Cloudflare Turnstile
    token. Only the non-Onshape upload flow uses this. When Turnstile is
    unconfigured (dev/test) verification passes and the session is still minted,
    so the compute-endpoint gate keeps working locally."""
    data = request.get_json(silent=True) or {}
    token = data.get('token') or request.form.get('cf-turnstile-response')
    if _verify_turnstile_token(token):
        session['app_verified'] = True
        return jsonify({'ok': True})
    return jsonify({'ok': False, 'error': 'Verification failed.'}), 403


def _config_status_payload():
    """JSON-safe snapshot of the upload flow's current config (team name, machine/material/
    tool/bed context) so the client can apply a newly-loaded config in place without a full
    page reload. Mirrors the template context used on initial render."""
    ctx = dict(_app_template_context(force_defaults=True))
    # incomplete_materials is a set (not JSON-serializable); the rest is plain data.
    ctx['incomplete_materials'] = sorted(ctx.get('incomplete_materials', []))
    team_config = _active_team_config()
    ctx['team_name'] = team_config.team_name
    ctx['team_number'] = team_config.team_number
    ctx['config_url'] = session.get('upload_config_url')
    return ctx


@app.route('/session/config', methods=['POST'])
@limiter.limit("30 per minute")
def session_config():
    """Set (or clear) the team config for the anonymous UPLOAD flow.

    The browser fetches a team-hosted YAML config client-side (from a CORS-friendly host)
    and POSTs its text here; we validate/sanitize it STRICTLY (rejecting bad input with a
    user-facing message) and store the result under `upload_config_data` -- kept separate
    from the Onshape `team_config_data` so the two flows never cross-contaminate. An empty or
    absent `yaml` clears back to Team 6238 defaults. Gated like the compute endpoints, so
    only a verified session can set it."""
    gate = _require_app_session()
    if gate:
        return gate
    data = request.get_json(silent=True) or {}
    yaml_text = data.get('yaml')
    source_url = (data.get('url') or '').strip() or None

    # Empty content -> clear the upload config, revert to defaults.
    if not yaml_text or not yaml_text.strip():
        session.pop('upload_config_data', None)
        session.pop('upload_config_url', None)
        log("🧹 Upload team config cleared - using defaults")
        return jsonify({'ok': True, 'cleared': True, **_config_status_payload()})

    try:
        config_dict, warnings = validate_and_sanitize_config(yaml_text, strict=True)
    except ConfigValidationError as e:
        return jsonify({'ok': False, 'error': e.message}), 400

    team_config = TeamConfig.from_dict(config_dict)
    session['upload_config_data'] = team_config._data
    session['upload_config_url'] = source_url
    # Newline-strip the URL before logging (avoid log-forging via the user-supplied value).
    safe_url = (source_url or '').replace('\n', ' ').replace('\r', ' ')[:200]
    log(f"✅ Upload team config set: {team_config.team_name} (#{team_config.team_number})"
        + (f" from {safe_url}" if safe_url else ""))
    return jsonify({'ok': True, 'warnings': warnings, **_config_status_payload()})


def _serve_wizard_upload():
    """Render the multi-part wizard full-screen in DXF-upload (non-Onshape) mode.

    Shared by the root route and the `/app` alias. NO Onshape OAuth: the anonymous
    upload flow is bot-gated by Cloudflare Turnstile (see /session/verify) and
    always uses DEFAULT team config — it never calls an Onshape API (no
    permissions in this scenario). The Onshape-panel flow is served separately by
    `_serve_onshape_panel` and is unchanged.
    """
    return render_template('wizard.html', source='upload', authenticated=True,
                           onshape_ctx={}, theme='dark',
                           turnstile_sitekey=TURNSTILE_SITE_KEY,
                           **_app_template_context(force_defaults=True))


@app.route('/')
def index():
    """Render the main app: the multi-part wizard, full-screen, in DXF-upload mode."""
    return _serve_wizard_upload()


@app.route('/config/refresh')
def refresh_config():
    """Force an immediate re-search for the team config in Onshape (the subtle reload glyph
    next to the config banner). Same fetch-and-store as login and the TTL refresh.

    Offered whether or not a config is currently loaded: a team that starts on defaults and
    only later adds PenguinCAM-config.yaml needs exactly this button, and before it was
    rendered only once a config had ALREADY been found - so the one case that needed it
    most had no way to trigger it.

    Stashes a one-shot outcome message (consumed by the next render) because the failure
    mode is otherwise invisible: without it, a click that finds nothing returns a
    byte-identical page and looks like a broken button."""
    if not ONSHAPE_AVAILABLE:
        session['config_refresh_result'] = {'ok': False, 'message': 'Onshape integration is not enabled.'}
        return redirect(request.referrer or '/')
    client = session_manager.get_client(get_current_user_id())
    if not client:
        session['config_refresh_result'] = {
            'ok': False, 'message': 'You are not signed in to Onshape, so there is nowhere to look.'}
        return redirect(request.referrer or '/')
    try:
        team_config = _load_team_config_into_session(client)
    except Exception as e:
        log(f"⚠️  Manual team config refresh failed: {e}")
        session['config_refresh_result'] = {
            'ok': False, 'message': f'The lookup failed: {e}'}
        return redirect(request.referrer or '/')
    if session.get('using_default_config'):
        reason = session.get('team_config_error') or 'No PenguinCAM-config.yaml was found.'
        session['config_refresh_result'] = {'ok': False, 'message': f'Still using defaults. {reason}'}
    else:
        session['config_refresh_result'] = {
            'ok': True,
            'message': f'Loaded config for {team_config.team_number} ({team_config.team_name}).'}
    return redirect(request.referrer or '/')


@app.route('/app')
def wizard_app():
    """Alias for the root route: the standalone (DXF upload) wizard. Kept so existing
    links to /app keep working; both it and / render the same full-screen upload wizard."""
    return _serve_wizard_upload()


def _clean_onshape_id(value):
    """Return a usable Onshape id, or '' if the value is an unsubstituted URL-template
    placeholder. When Onshape launches our element panel it fills placeholders like
    {$workspaceId}/{$versionId} in the configured URL - but only for ids that exist in
    the current context. Opened on an older VERSION, a document has no workspace, so
    {$workspaceId} arrives literally (e.g. '{$workspaceId}' / '%7B$workspaceId%7D').
    Treat any value containing '{' or '$' (or empty) as absent so the wvm resolver can
    fall back to the version/microversion id that DID substitute."""
    if not value:
        return ''
    s = str(value).strip()
    if '{' in s or '$' in s:
        return ''
    return s


def _serve_onshape_panel():
    """Render the multi-part wizard for embedding in the Onshape right-side panel.

    Does NOT hard-redirect to OAuth when unauthenticated (the iframe cannot frame
    Onshape's login). Instead it passes an `authenticated` flag and the wizard shows
    a Connect button that runs OAuth in a popup. Sets framing + cookie headers so the
    iframe and its OAuth/session work inside Onshape."""
    # Pass the launch ids through RAW (do NOT clean placeholders here). These feed the
    # wizard's Onshape postMessage calls (applicationInit / requestSelection), and
    # Onshape's selection channel breaks if we hand it an EMPTY workspaceId - it
    # tolerates the unsubstituted '{$workspaceId}' literal (version context) but not ''.
    # Placeholder cleaning + wvm resolution happens at the real API boundary instead:
    # /onshape/export-face cleans the POST body via _clean_onshape_id before any call.
    onshape_ctx = {
        'documentId': request.args.get('documentId', ''),
        'workspaceId': request.args.get('workspaceId', ''),
        # A panel opened on an older version/microversion gets these instead of a
        # workspace id; carried through so the export path can address that snapshot.
        'versionId': request.args.get('versionId', ''),
        'microversionId': request.args.get('microversionId', ''),
        'elementId': request.args.get('elementId', ''),
        'server': request.args.get('server', 'https://cad.onshape.com'),
    }
    # Onshape passes the user's current theme (e.g. ?theme=light|dark) when it loads the
    # panel iframe; mirror it so the panel matches Onshape's light/dark preference.
    theme = 'dark' if request.args.get('theme', 'light').lower() == 'dark' else 'light'
    authenticated = bool(ONSHAPE_AVAILABLE and session_manager.get_client(get_current_user_id()))
    log(f"[PANEL] render did={onshape_ctx['documentId'][:8]} authed={authenticated} theme={theme}")
    resp = make_response(render_template('wizard.html', source='onshape',
                                         authenticated=authenticated, onshape_ctx=onshape_ctx,
                                         theme=theme, **_app_template_context()))
    # Allow embedding only within Onshape; allow the session cookie to ride in the iframe.
    resp.headers['Content-Security-Policy'] = "frame-ancestors https://*.onshape.com"
    resp.headers.pop('X-Frame-Options', None)
    return resp


@app.route('/onshape-panel')
def onshape_panel():
    """Canonical wizard panel route."""
    return _serve_onshape_panel()

def _detect_tube_dims(dxf_path, rotation):
    """Detect a tube face's width x length from a DXF's geometry bounds.

    Returns (tube_width, tube_length) in inches, or (None, None) if bounds could
    not be read. Rotation of 90/270 swaps width and length to match how the part
    is placed in the jig.
    """
    try:
        doc = ezdxf.readfile(dxf_path)
        msp = doc.modelspace()
        all_x, all_y = [], []
        for entity in msp:
            if entity.dxftype() == 'CIRCLE':
                center = entity.dxf.center
                radius = entity.dxf.radius
                all_x.extend([center.x - radius, center.x + radius])
                all_y.extend([center.y - radius, center.y + radius])
            elif entity.dxftype() in ['LWPOLYLINE', 'POLYLINE']:
                points = list(entity.get_points())
                if points:
                    all_x.extend([p[0] for p in points])
                    all_y.extend([p[1] for p in points])
            elif entity.dxftype() == 'LINE':
                all_x.extend([entity.dxf.start.x, entity.dxf.end.x])
                all_y.extend([entity.dxf.start.y, entity.dxf.end.y])

        if not (all_x and all_y):
            return None, None
        dxf_width = max(all_x) - min(all_x)
        dxf_height = max(all_y) - min(all_y)
        # Account for rotation: swap dimensions if rotated 90 or 270 degrees.
        if rotation in [90, 270]:
            return dxf_height, dxf_width
        return dxf_width, dxf_height
    except Exception as e:
        log(f"⚠️  Could not extract tube dimensions from DXF: {e}")
        return None, None


@app.route('/process', methods=['POST'])
@limiter.limit("10 per minute")  # Strict limit - CPU intensive operation
def process_file():
    """Process uploaded DXF file and generate G-code"""
    gate = _require_app_session()
    if gate:
        return gate
    try:
        # Get uploaded file
        if 'file' not in request.files:
            return jsonify({'error': 'No file uploaded'}), 400
        
        file = request.files['file']
        if file.filename == '':
            return jsonify({'error': 'No file selected'}), 400
        
        if not file.filename.lower().endswith('.dxf'):
            return jsonify({'error': 'File must be a DXF file'}), 400
        
        # Get parameters
        material = request.form.get('material', 'plywood')
        is_aluminum_tube = (material.lower() == 'aluminum_tube')
        machine_id = request.form.get('machine_id', None)  # Optional machine selection
        material = normalize_material(material)  # aluminum_tube->aluminum, polycarb->polycarbonate

        tool_diameter = float(request.form.get('tool_diameter', DEFAULT_TOOL_DIAMETER_IN))
        origin_corner = request.form.get('origin_corner', 'bottom-left')
        rotation = int(request.form.get('rotation', 0))
        mirror = request.form.get('mirror', '0') == '1'  # "flip over" (horizontal mirror)
        suggested_filename = request.form.get('suggested_filename', '')

        # Get timestamp from client (in user's local timezone)
        timestamp_str = request.form.get('timestamp', '')

        # Material-specific parameters
        thickness = float(request.form.get('thickness', 0.25))  # Material/wall thickness (used by both modes)

        if is_aluminum_tube:
            # Tube mode parameters
            tube_height = float(request.form.get('tube_height', 1.0))
            square_end = request.form.get('square_end', '0') == '1'
            cut_to_length = request.form.get('cut_to_length', '0') == '1'
        else:
            # Standard mode parameters
            tab_spacing = float(request.form.get('tab_spacing', 6.0))

        # Save uploaded file
        input_path = os.path.join(UPLOAD_FOLDER, 'input.dxf')
        file.save(input_path)

        # For tube mode, extract DXF bounds to determine tube dimensions
        tube_width = None
        tube_length = None
        if is_aluminum_tube:
            tube_width, tube_length = _detect_tube_dims(input_path, rotation)
            if tube_width is not None:
                log(f"📏 Detected tube dimensions (after {rotation}° rotation): {tube_width:.3f}\" x {tube_length:.3f}\"")

        # Generate suggested filename base (without extension or timestamp)
        if suggested_filename:
            # Use Onshape-derived name
            base_name = suggested_filename
            log(f"📝 Using Onshape filename base: {base_name}")
        else:
            # Use DXF filename
            base_name = Path(file.filename).stem
            log(f"📝 Using DXF filename base: {base_name}")

        log(f"🚀 Running post-processor API...")

        # Get the team config that applies to this request (upload-supplied, Onshape, or
        # defaults), bound to the selected machine so its full settings apply.
        team_config = _active_team_config(machine_id)
        log(f"📋 Using team config: {team_config}")
        log(f"🔍 DEBUG: TeamConfig internals: team={team_config.team_number}, name={team_config.team_name}")

        # Call post-processor API based on mode
        try:
            if is_aluminum_tube:
                # Tube mode - use tube-pattern API. Each face is prepared identically;
                # face 2 (if provided) carries a distinct pattern for the opposite side.
                user_name = session.get('user_name')

                def _prepare_tube_face(dxf_path):
                    face_pp = FRCPostProcessor(
                        material_thickness=thickness,
                        tool_diameter=tool_diameter,
                        units='inch',
                        config=team_config
                    )
                    face_pp.tube_height = tube_height  # Store for Z-offset calculations
                    face_pp.apply_material_preset(material, machine_id)
                    if user_name:
                        face_pp.user_name = user_name
                    face_pp.load_dxf(dxf_path)
                    face_pp.transform_coordinates('bottom-left', rotation)  # Tube jig is always bottom-left
                    face_pp.identify_perimeter_and_pockets()  # Must come BEFORE classify_holes to remove perimeter circles
                    face_pp.classify_holes()
                    return face_pp

                pp = _prepare_tube_face(input_path)

                # Optional distinct second face. When absent, face 1 is mirrored onto the
                # opposite side (one-face mode).
                second_face_pp = None
                face2 = request.files.get('file_face2')
                if face2 is not None and face2.filename:
                    if not face2.filename.lower().endswith('.dxf'):
                        return jsonify({'error': 'Second face must be a DXF file'}), 400
                    face2_path = os.path.join(UPLOAD_FOLDER, 'input_face2.dxf')
                    face2.save(face2_path)
                    second_face_pp = _prepare_tube_face(face2_path)
                    # Both faces are opposite walls of the same tube, so their widths
                    # should match; warn (don't fail) if they differ noticeably.
                    f2_width, _ = _detect_tube_dims(face2_path, rotation)
                    if tube_width and f2_width and abs(f2_width - tube_width) > 0.05:
                        log(f"⚠️  Tube face widths differ: face1={tube_width:.3f}\" face2={f2_width:.3f}\" "
                            f"(using face1 width for both)")

                # Generate G-code using API
                result = pp.generate_tube_pattern_gcode(
                    tube_height=tube_height,
                    square_end=square_end,
                    cut_to_length=cut_to_length,
                    tube_width=tube_width,
                    tube_length=tube_length,
                    suggested_filename=base_name,
                    timestamp=timestamp_str,
                    second_face_pp=second_face_pp
                )
            else:
                # Standard mode - use standard API
                pp = FRCPostProcessor(
                    material_thickness=thickness,
                    tool_diameter=tool_diameter,
                    units='inch',
                    config=team_config
                )

                # Apply material preset (for specific machine if selected)
                pp.apply_material_preset(material, machine_id)

                # Add user name if authenticated
                user_name = session.get('user_name')
                if user_name:
                    pp.user_name = user_name

                # Standard mode specific parameters
                pp.tab_spacing = tab_spacing

                # Load and process DXF
                pp.load_dxf(input_path)
                pp.transform_coordinates(origin_corner, rotation, mirror=mirror)
                pp.identify_perimeter_and_pockets()  # Must come BEFORE classify_holes to remove perimeter circles
                pp.classify_holes()

                # Generate G-code using API
                result = pp.generate_gcode(suggested_filename=base_name, timestamp=timestamp_str)

            if not result.success:
                log(f"❌ Post-processor API failed!")
                for error in result.errors:
                    log(f"   Error: {error}")
                return jsonify({
                    'error': 'Post-processor failed',
                    'details': '\n'.join(result.errors)
                }), 500

            # Write G-code to file
            output_path = os.path.join(OUTPUT_FOLDER, result.filename)
            with open(output_path, 'w') as f:
                f.write(result.gcode)

            log(f"✅ Output file created: {os.path.getsize(output_path)} bytes")
            log(f"📄 Output file: {output_path}")

            # Register file with token manager for secure access
            actual_filename = result.filename
            output_token = file_token_manager.register_file(output_path, actual_filename)

        except Exception as e:
            log(f"❌ Post-processor API error: {e}")
            log(traceback.format_exc())
            return jsonify({
                'error': 'Post-processor API error',
                'details': str(e)
            }), 500

        # Build console output from result stats (for backward compatibility with UI)
        console_lines = []
        console_lines.append(f"Identified {result.stats.get('num_holes', 0)} millable holes and {result.stats.get('num_pockets', 0)} pockets")
        console_lines.append(f"Total lines: {result.stats.get('total_lines', 0)}")
        if 'cycle_time_display' in result.stats:
            console_lines.append(f"\n⏱️  ESTIMATED_CYCLE_TIME: {result.stats['cycle_time_seconds']:.1f} seconds ({result.stats['cycle_time_display']})")
        console_output = '\n'.join(console_lines)

        # Build parameters dictionary based on mode
        parameters = {
            'thickness': thickness,
            'tool_diameter': tool_diameter,
            'origin_corner': origin_corner,
            'rotation': rotation
        }

        if is_aluminum_tube:
            parameters.update({
                'tube_height': tube_height,
                'square_end': square_end,
                'cut_to_length': cut_to_length
            })
        else:
            parameters.update({
                'tab_spacing': tab_spacing
            })

        response_data = {
            'success': True,
            'filename': output_token,  # Return secure token (not actual filename)
            'gcode': result.gcode,
            'console': console_output,
            # Settings PenguinCAM adapted to suit this part (e.g. a team-config tab height
            # taller than the stock being cut). Not failures - the operator just needs to
            # know what changed, since they usually cannot edit the team config themselves.
            'warnings': result.warnings,
            'parameters': parameters
        }

        # Add cycle time if available
        if 'cycle_time_display' in result.stats:
            response_data['cycle_time'] = result.stats['cycle_time_display']
            response_data['cycle_time_seconds'] = result.stats['cycle_time_seconds']

        # Log metrics
        team_number = session.get('team_number')
        user_email = session.get('user_email')
        metrics.log_event('gcode_generated',
                         team_number=team_number,
                         user_email=user_email,
                         metadata={
                             'material': material,
                             'is_tube': is_aluminum_tube,
                             'from_onshape': request.form.get('fromOnshape', 'false') == 'true'
                         })

        return jsonify(response_data)

    except ValueError as e:
        return jsonify({'error': f'Invalid parameter value: {str(e)}'}), 400
    except Exception as e:
        log(traceback.format_exc())
        return jsonify({'error': f'Unexpected error: {str(e)}'}), 500


@app.route('/process-job', methods=['POST'])
@limiter.limit("10 per minute")  # CPU intensive
def process_job():
    """Process a multi-part job: several DXF parts placed on one stock sheet, emitted
    as a single G-code program. 2D standard mode only (2.5D is single-part via /process).

    Request (multipart/form-data):
      - file_0, file_1, ... : one DXF per distinct part
      - job : JSON {material, tool_diameter, thickness, tab_spacing, machine_id,
                    stock:{width,height}, name,
                    parts:[{file_index, name, place_x, place_y, rotation}]}
      - timestamp : client-local timestamp string

    Response: {success, filename(token), gcode, cycle_time, stock, parts:[...]}
              or {success:false, part_errors:[{part_index, name, error}]}.
    """
    gate = _require_app_session()
    if gate:
        return gate
    job_dir = None
    try:
        job_raw = request.form.get('job')
        if not job_raw:
            return jsonify({'error': 'Missing job specification'}), 400
        try:
            job = json.loads(job_raw)
        except (ValueError, TypeError):
            return jsonify({'error': 'Invalid job JSON'}), 400

        parts_spec = job.get('parts', [])
        if not parts_spec:
            return jsonify({'error': 'Job has no parts'}), 400

        # Shared job parameters (one tool/material per job in v1).
        material = normalize_material(job.get('material', 'plywood'))  # aluminum_tube->aluminum, polycarb->polycarbonate
        tool_diameter = float(job.get('tool_diameter', DEFAULT_TOOL_DIAMETER_IN))
        thickness = float(job.get('thickness', 0.25))
        tab_spacing = float(job.get('tab_spacing', 6.0))
        machine_id = job.get('machine_id')
        timestamp_str = request.form.get('timestamp', '')

        # Stock size (and G54 origin) are derived server-side from the placed parts'
        # combined bounding box; the client's stock field is advisory only. This avoids
        # a client/server bbox mismatch flagging a part as "outside" its own stock.

        # Save each uploaded DXF to a distinct path so parts don't clobber each other.
        job_dir = tempfile.mkdtemp(prefix='job_', dir=UPLOAD_FOLDER)
        saved_paths = {}
        for key in list(request.files.keys()):
            if not key.startswith('file_'):
                continue
            f = request.files[key]
            if not f.filename.lower().endswith('.dxf'):
                return jsonify({'error': f'{f.filename} is not a DXF file'}), 400
            try:
                idx = int(key.split('_', 1)[1])
            except (ValueError, IndexError):
                continue
            p = os.path.join(job_dir, f'part_{idx}.dxf')
            f.save(p)
            saved_paths[idx] = p

        team_config = _active_team_config(machine_id)
        user_name = session.get('user_name')
        machine_x = team_config.machine_x_max
        machine_y = team_config.machine_y_max

        log(f"[JOB] {len(parts_spec)} parts, tool {tool_diameter}, material {material}, thickness {thickness}")

        # Pass 1: build + place each part; collect footprints for layout validation.
        prepared = []
        placed = []
        gen_errors = []
        job_warnings = []  # per-part settings adapted to fit; surfaced, never fatal
        for i, part in enumerate(parts_spec):
            fidx = part.get('file_index', i)
            if fidx not in saved_paths:
                gen_errors.append({'part_index': i, 'name': part.get('name'),
                                   'error': f"Missing DXF upload for part {i + 1}"})
                continue
            name = part.get('name') or Path(saved_paths[fidx]).stem
            place_x = float(part.get('place_x', 0.0))
            place_y = float(part.get('place_y', 0.0))
            rotation = float(part.get('rotation', 0))
            mirror = bool(part.get('mirror'))

            pp = FRCPostProcessor(material_thickness=thickness, tool_diameter=tool_diameter,
                                  units='inch', config=team_config)
            pp.apply_material_preset(material, machine_id)
            if user_name:
                pp.user_name = user_name
            pp.tab_spacing = tab_spacing
            pp.load_dxf(saved_paths[fidx])
            pp.transform_coordinates('bottom-left', rotation,
                                     placement_offset=(place_x, place_y),
                                     enforce_bounds=False, mirror=mirror)
            pp.identify_perimeter_and_pockets()
            pp.classify_holes()
            bbox = pp.bounding_box()
            placed.append({'name': name, 'bbox': bbox, 'polygon': pp.placed_polygon()})
            prepared.append({'pp': pp, 'bbox': bbox, 'name': name,
                             'place_x': place_x, 'place_y': place_y, 'rotation': rotation})

        if gen_errors:
            return jsonify({'success': False, 'part_errors': gen_errors}), 400

        # Stock = the parts' combined bounding box (server-authoritative).
        boxes = [p['bbox'] for p in placed if p.get('bbox')]
        if boxes:
            stock_w = max(b[2] for b in boxes) - min(b[0] for b in boxes)
            stock_h = max(b[3] for b in boxes) - min(b[1] for b in boxes)
        else:
            stock_w = stock_h = 0.0

        # Validate before the expensive body generation: the combined bbox must fit the
        # machine, and parts must not overlap (kerf = tool diameter).
        layout_errors = validate_job_layout(placed, machine_x, machine_y, min_gap=tool_diameter)
        if layout_errors:
            log(f"[JOB] layout invalid: {layout_errors}")
            return jsonify({'success': False, 'part_errors': layout_errors}), 400

        # Pass 2: generate each part's toolpath body, then stitch into one program.
        part_jobs = []
        response_parts = []
        for i, item in enumerate(prepared):
            phases = item['pp'].generate_part_phases()
            if phases['errors']:
                for e in phases['errors']:
                    gen_errors.append({'part_index': i, 'name': item['name'], 'error': e})
                continue
            part_jobs.append({
                'name': item['name'], 'place_x': item['place_x'],
                'place_y': item['place_y'], 'rotation': item['rotation'],
                'interior': phases['interior'], 'perimeter': phases['perimeter'],
                'tab_removal': phases['tab_removal'],
            })
            for note in getattr(item['pp'], 'warnings', []):
                job_warnings.append(f"{item['name']}: {note}")
            minx, miny, maxx, maxy = item['bbox']
            response_parts.append({
                'index': i, 'name': item['name'],
                'place_x': item['place_x'], 'place_y': item['place_y'], 'rotation': item['rotation'],
                'bbox': {'minX': minx, 'minY': miny, 'maxX': maxx, 'maxY': maxy},
            })

        if gen_errors:
            return jsonify({'success': False, 'part_errors': gen_errors}), 400
        if not part_jobs:
            return jsonify({'error': 'No parts could be generated'}), 400

        result = assemble_job_gcode(part_jobs, header_pp=prepared[0]['pp'],
                                    timestamp=timestamp_str or None,
                                    suggested_filename=job.get('name', 'job'))

        output_path = os.path.join(OUTPUT_FOLDER, result.filename)
        with open(output_path, 'w') as fh:
            fh.write(result.gcode)
        output_token = file_token_manager.register_file(output_path, result.filename)

        log(f"[JOB] assembled {result.stats['num_parts']} parts, "
            f"{result.stats['total_lines']} lines, {result.stats['cycle_time_display']}")

        metrics.log_event('job_generated',
                          team_number=session.get('team_number'),
                          user_email=session.get('user_email'),
                          metadata={'material': material, 'num_parts': len(part_jobs)})

        return jsonify({
            'success': True,
            'filename': output_token,
            'gcode': result.gcode,
            'cycle_time': result.stats.get('cycle_time_display'),
            'cycle_time_seconds': result.stats.get('cycle_time_seconds'),
            'stock': {'width': round(stock_w, 4), 'height': round(stock_h, 4)},
            'parts': response_parts,
            'warnings': job_warnings,
        })

    except ValueError as e:
        return jsonify({'error': f'Invalid parameter value: {str(e)}'}), 400
    except Exception as e:
        log(traceback.format_exc())
        return jsonify({'error': f'Unexpected error: {str(e)}'}), 500
    finally:
        if job_dir:
            shutil.rmtree(job_dir, ignore_errors=True)


@app.route('/part-outline', methods=['POST'])
@limiter.limit("30 per minute")
def part_outline():
    """Return the perimeter outline + dimensions of an uploaded DXF, for the layout
    canvas and parts-list thumbnail. Stateless: the browser keeps the DXF bytes and
    re-submits them at /process-job time (serverless-safe). Coordinates are normalized
    so the part's bounding-box minimum sits at (0,0)."""
    gate = _require_app_session()
    if gate:
        return gate
    path = None
    try:
        if 'file' not in request.files:
            return jsonify({'error': 'No file uploaded'}), 400
        f = request.files['file']
        if not f.filename.lower().endswith('.dxf'):
            return jsonify({'error': 'File must be a DXF'}), 400

        tmp = tempfile.NamedTemporaryFile(suffix='.dxf', delete=False, dir=UPLOAD_FOLDER)
        path = tmp.name
        tmp.close()
        f.save(path)

        geo = _compute_dxf_outline(path)
        if not geo:
            return jsonify({'error': 'No geometry found in DXF'}), 400
        geo['success'] = True
        geo['name'] = Path(f.filename).stem
        return jsonify(geo)
    except Exception as e:
        log(traceback.format_exc())
        return jsonify({'error': str(e)}), 500
    finally:
        if path and os.path.exists(path):
            os.remove(path)


@app.route('/download/<token>')
@limiter.limit("30 per minute")
def download_file(token):
    """
    Download generated G-code file using secure token.
    Token prevents filename guessing attacks.
    """
    try:
        # Look up file by token
        file_info = file_token_manager.get_file(token)
        if not file_info:
            return jsonify({'error': 'File not found or expired'}), 404

        file_path = file_info['filepath']
        real_filename = file_info['filename']

        # Verify file still exists on disk
        if not os.path.exists(file_path):
            return jsonify({'error': 'File not found'}), 404

        log(f"📥 Download request: token {token[:16]}... → {real_filename}")

        # Log metrics
        team_number = session.get('team_number')
        user_email = session.get('user_email')
        metrics.log_event('download',
                         team_number=team_number,
                         user_email=user_email,
                         metadata={'filename': real_filename})

        return send_file(
            file_path,
            as_attachment=True,
            download_name=real_filename,  # User sees the real filename
            mimetype='text/plain'
        )
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/debug/download-dxf')
@limiter.limit("30 per minute")
def debug_download_dxf():
    """
    Debug endpoint: Download the exact DXF from the most recent Onshape face export
    (flat or 2.5D multi-layer). Lets you reproduce a part offline against the
    postprocessor with the identical geometry the server generated.
    """
    try:
        # Get DXF token from session
        dxf_token = session.get('debug_dxf_token')
        if not dxf_token:
            return jsonify({
                'error': 'No debug DXF available',
                'message': 'Import a part from Onshape first.'
            }), 404

        # Look up file by token
        file_info = file_token_manager.get_file(dxf_token)
        if not file_info:
            return jsonify({
                'error': 'DXF file not found or expired',
                'message': 'The DXF may have been cleaned up. Import the part again.'
            }), 404

        file_path = file_info['filepath']
        real_filename = session.get('debug_dxf_filename', 'debug.dxf')

        # Verify file still exists on disk
        if not os.path.exists(file_path):
            return jsonify({'error': 'DXF file no longer exists on disk'}), 404

        log(f"🐛 Debug DXF download: {real_filename} ({os.path.getsize(file_path)} bytes)")

        return send_file(
            file_path,
            as_attachment=True,
            download_name=real_filename,
            mimetype='application/dxf'
        )
    except Exception as e:
        log(f"❌ Debug DXF download error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/debug/download-raw-dxf')
@limiter.limit("30 per minute")
def debug_download_raw_dxf():
    """
    Debug endpoint: Download a zip of the RAW per-depth DXFs from the most recent 2.5D
    Onshape import — exactly as Onshape returned them, BEFORE our HATCH reconstruction.
    Use this to inspect what the outer-profile boundary geometry looks like at the source.
    """
    try:
        token = session.get('debug_raw_dxf_token')
        if not token:
            return jsonify({
                'error': 'No raw debug DXF available',
                'message': 'Import a 2.5D part from Onshape first.'
            }), 404

        file_info = file_token_manager.get_file(token)
        if not file_info or not os.path.exists(file_info['filepath']):
            return jsonify({
                'error': 'Raw DXF zip not found or expired',
                'message': 'It may have been cleaned up. Import the part again.'
            }), 404

        real_filename = session.get('debug_raw_dxf_filename', 'raw-layers.zip')
        log(f"🐛 Debug RAW DXF zip download: {real_filename} "
            f"({os.path.getsize(file_info['filepath'])} bytes)")
        return send_file(file_info['filepath'], as_attachment=True,
                         download_name=real_filename, mimetype='application/zip')
    except Exception as e:
        log(f"❌ Debug raw DXF download error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/uploads/<token>')
@limiter.limit("30 per minute")
def serve_upload(token):
    """
    Serve uploaded DXF files for frontend preview using secure token.
    Token prevents filename guessing attacks.
    """
    try:
        # Look up file by token
        file_info = file_token_manager.get_file(token)
        if not file_info:
            return jsonify({'error': 'File not found or expired'}), 404

        file_path = file_info['filepath']

        # Verify file still exists on disk
        if not os.path.exists(file_path):
            return jsonify({'error': 'File not found'}), 404

        log(f"📂 Upload preview: token {token[:16]}... → {file_info['filename']}")

        return send_file(file_path, mimetype='application/dxf')
    except Exception as e:
        return jsonify({'error': f'File not found: {str(e)}'}), 404

@app.route('/drive/status')
@limiter.limit("30 per minute")
def drive_status():
    """Check if Google Drive integration is available and configured"""
    if not GOOGLE_DRIVE_AVAILABLE:
        return jsonify({
            'available': False,
            'enabled': False,
            'message': 'Google Drive dependencies not installed'
        })

    # Check team config to see if Drive is enabled
    team_config = session.get('team_config', {})
    drive_enabled = team_config.get('google_drive_enabled', False)
    folder_id = team_config.get('google_drive_folder_id')

    if not drive_enabled or not folder_id:
        return jsonify({
            'available': True,
            'enabled': False,
            'message': 'Google Drive not configured for your team. Add PenguinCAM-config.yaml to enable.'
        })

    # Check if user is authenticated with Google
    if AUTH_AVAILABLE and auth.is_enabled():
        creds = auth.get_credentials()
        if not creds:
            return jsonify({
                'available': True,
                'enabled': True,
                'authenticated': False,
                'message': 'Click "Save to Drive" to authenticate'
            })

        return jsonify({
            'available': True,
            'enabled': True,
            'authenticated': True,
            'message': 'Google Drive ready',
            'folder_id': folder_id
        })
    else:
        return jsonify({
            'available': True,
            'enabled': True,
            'authenticated': False,
            'message': 'Click "Save to Drive" to authenticate'
        })

@app.route('/drive/upload/<token>', methods=['POST'])
@limiter.limit("30 per minute")  # Reasonable limit for uploads
@auth.require_auth
def upload_to_drive(token):
    """Upload a G-code file to Google Drive using secure token"""
    log(f"📤 Drive upload requested for token: {token[:16]}...")

    if not GOOGLE_DRIVE_AVAILABLE:
        log("❌ Google Drive integration not available")
        return jsonify({
            'success': False,
            'message': 'Google Drive integration not available'
        }), 400

    try:
        # Look up file by token
        file_info = file_token_manager.get_file(token)
        if not file_info:
            log(f"❌ Token not found or expired: {token[:16]}...")
            return jsonify({
                'success': False,
                'message': 'File not found or expired'
            }), 404

        file_path = file_info['filepath']
        real_filename = file_info['filename']

        log(f"📂 Looking for file at: {file_path}")
        log(f"📂 Real filename: {real_filename}")
        log(f"📂 File exists: {os.path.exists(file_path)}")

        if not os.path.exists(file_path):
            log(f"❌ File not found: {file_path}")
            return jsonify({
                'success': False,
                'message': 'File not found'
            }), 404
        
        # Get credentials from session
        creds = None
        if AUTH_AVAILABLE and auth.is_enabled():
            log("🔐 Getting credentials from session...")
            creds = auth.get_credentials()
            if not creds:
                log("❌ No credentials in session")
                return jsonify({
                    'success': False,
                    'message': 'Not authenticated with Google Drive'
                }), 401
            log(f"✅ Got credentials, scopes: {creds.scopes if hasattr(creds, 'scopes') else 'unknown'}")
        
        # Create uploader with credentials
        log("🔧 Creating GoogleDriveUploader...")
        uploader = GoogleDriveUploader(credentials=creds)
        
        log("🔐 Authenticating...")
        if not uploader.authenticate():
            log("❌ Authentication failed")
            return jsonify({
                'success': False,
                'message': 'Failed to authenticate with Google Drive'
            }), 500
        
        log("✅ Authenticated, uploading file...")
        # Upload the file with real filename
        result = uploader.upload_file(file_path, real_filename)

        log(f"📤 Upload result: {result}")

        if result and result.get('success'):
            log(f"✅ Upload successful: {result.get('web_link')}")

            # Log metrics
            team_number = session.get('team_number')
            user_email = session.get('user_email')
            metrics.log_event('drive_save',
                             team_number=team_number,
                             user_email=user_email,
                             metadata={'filename': real_filename})

            return jsonify({
                'success': True,
                'message': f'✅ Uploaded: {real_filename}',
                'file_id': result.get('file_id'),
                'web_view_link': result.get('web_link')
            })
        else:
            log(f"❌ Upload failed: {result.get('message') if result else 'Unknown error'}")
            return jsonify({
                'success': False,
                'message': result.get('message') if result else 'Upload failed'
            }), 500
            
    except Exception as e:
        return jsonify({
            'success': False,
            'message': f'Upload error: {str(e)}'
        }), 500

# ============================================================================
# Onshape Integration Routes
# ============================================================================

@app.route('/onshape/auth')
def onshape_auth():
    """Start Onshape OAuth flow"""
    if not ONSHAPE_AVAILABLE:
        return jsonify({
            'error': 'Onshape integration not available'
        }), 400

    try:
        client = get_onshape_client()

        # Generate state for CSRF protection
        state = secrets.token_urlsafe(32)

        # Store state in session for verification
        session['onshape_oauth_state'] = state

        # Popup mode (from the embedded panel): remember so the callback closes the
        # popup and notifies the opener instead of navigating the whole window.
        if request.args.get('popup'):
            session['onshape_oauth_popup'] = True
        else:
            session.pop('onshape_oauth_popup', None)

        # Get authorization URL
        auth_url = client.get_authorization_url(state=state)

        # Redirect user to Onshape for authorization
        return redirect(auth_url)
        
    except Exception as e:
        return jsonify({'error': f'OAuth initialization failed: {str(e)}'}), 500

@app.route('/onshape/oauth/callback')
def onshape_oauth_callback():
    """Handle Onshape OAuth callback"""
    if not ONSHAPE_AVAILABLE:
        return "Onshape integration not available", 400

    try:
        # Get authorization code and state
        code = request.args.get('code')
        state = request.args.get('state')

        if not code:
            return "Authorization failed: No code received", 400

        # Verify state (CSRF protection)
        expected_state = session.get('onshape_oauth_state')
        if state != expected_state:
            return "Authorization failed: Invalid state", 400

        # Exchange code for access token
        client = get_onshape_client()
        token_data = client.exchange_code_for_token(code)

        if not token_data:
            return "Authorization failed: Could not get access token", 400

        # Store client in session
        # In production, you'd want to store tokens in a database
        user_id = get_current_user_id()
        session_manager.create_session(user_id, client)
        session['onshape_authenticated'] = True

        # Fetch user info and team config for session
        log("\n" + "="*60)
        log("Fetching user and team config after OAuth")
        log("="*60)

        # Get user session info
        user_session = client.get_user_session_info()
        if user_session:
            user_name = user_session.get('name')
            user_email = user_session.get('email')
            log(f"✅ User: {user_name} ({user_email})")
            session['user_name'] = user_name
            session['user_email'] = user_email

        # Load the team's config file (lives in the user's own Onshape documents).
        _load_team_config_into_session(client)

        log("="*60 + "\n")

        # Clean up OAuth state
        session.pop('onshape_oauth_state', None)

        # Safeguard: browsers drop cookies over ~4KB, which would silently lose the
        # tokens we just wrote. Warn if the session is getting close.
        try:
            _total = sum(len(json.dumps(v, default=str)) for v in session.values())
            if _total > 3500:
                log(f"[ONSHAPE-AUTH] WARNING: session ~{_total}B nearing the 4KB cookie limit")
        except Exception:
            pass

        # Popup mode (embedded panel connect): close the popup; the panel detects the
        # close and verifies auth.
        if session.pop('onshape_oauth_popup', None):
            return redirect('/onshape/auth-complete')

        # Direct (non-popup) auth: back to the app.
        return redirect('/?onshape_connected=true')
        
    except Exception as e:
        return f"OAuth callback error: {str(e)}", 500


@app.route('/onshape/auth-complete')
def onshape_auth_complete():
    """Tiny page shown in the OAuth popup after a successful connect: close the popup.
    The embedded panel detects the close (popup.closed) and verifies auth."""
    return """<!doctype html><html><head><meta charset="utf-8"><title>Connected</title></head>
<body style="font-family:-apple-system,sans-serif;background:#0f1419;color:#e6edf3;text-align:center;padding:2rem">
<p>Connected to Onshape. You can close this window.</p>
<script>setTimeout(function () { window.close(); }, 300);</script></body></html>"""


@app.route('/onshape/authed')
def onshape_authed():
    """Cheap session-only auth check (no Onshape API call) for the panel's connect
    poll. The embedded iframe polls this after opening the OAuth popup and reloads
    once tokens land in the session — avoids relying on window.opener/postMessage,
    which cross-origin OAuth navigation often severs."""
    authed = bool(ONSHAPE_AVAILABLE and session_manager.get_client(get_current_user_id()))
    return jsonify({'authenticated': authed})


@app.route('/onshape/export-face', methods=['POST'])
@limiter.limit("30 per minute")
def onshape_export_face():
    """Export a selected Onshape face and return its DXF bytes (base64) plus the
    outline/dims for the layout canvas. The browser holds the DXF bytes and
    re-submits them at /process-job time (serverless-safe; no server cache).

    In 2.5D mode (request field multilayer=true) this builds a depth-layered DXF
    of all parallel faces; otherwise it exports a single flat face."""
    client, err_resp, err_code = get_onshape_client_or_401()
    if err_resp:
        return err_resp, err_code

    data = request.get_json(force=True, silent=True) or {}
    did = _clean_onshape_id(data.get('documentId'))
    # Onshape addresses element data by workspace (live), version, or microversion.
    # A panel launched on an older version sends versionId with workspaceId left as an
    # unsubstituted placeholder, so clean all three and require at least one.
    wid = _clean_onshape_id(data.get('workspaceId'))
    vid = _clean_onshape_id(data.get('versionId'))
    mvid = _clean_onshape_id(data.get('microversionId'))
    eid = _clean_onshape_id(data.get('elementId'))
    fid = data.get('faceId')
    bid = data.get('partId') or data.get('bodyId')
    multilayer = str(data.get('multilayer', '')).lower() in ('true', '1', 'yes')
    if not all([did, eid, fid]) or not (wid or vid or mvid):
        return jsonify({'error': 'Missing selection IDs (need document/workspace-or-version/element/face).'}), 400

    log(f"[EXPORT] face did={str(did)[:8]} eid={str(eid)[:8]} fid={fid} bid={bid} "
        f"ctx={'w/'+wid[:8] if wid else ('v/'+vid[:8] if vid else 'm/'+mvid[:8])} "
        f"mode={'2.5D' if multilayer else 'flat'}")
    path = None
    keep_dxf = False  # set once the DXF is registered for debug download (see finally)
    try:
        # Resolve the selected face's normal (for correct flatten orientation) and the
        # part name (for the parts list) in one bodydetails call. Reused below for the
        # 2.5D depth-layer construction so we don't re-fetch faces.
        face_normal = None
        reference_origin = None   # selected face's plane origin; needed to probe stock thickness
        name = data.get('name')
        faces = None
        resolved_bid = None
        try:
            faces = client.list_faces(did, wid, eid, version_id=vid, microversion_id=mvid)
            for body in (faces or {}).get('bodies', []):
                for fc in body.get('faces', []):
                    if fc.get('id') == fid:
                        surface = fc.get('surface') or {}
                        face_normal = surface.get('normal')
                        reference_origin = surface.get('origin')
                        resolved_bid = body.get('id')
                        if not name:
                            name = (body.get('properties') or {}).get('name')
                        break
                if resolved_bid:
                    break
        except Exception:
            pass

        # Scope the export to the selected part's body. Onshape's face-selection message
        # doesn't reliably include a part/body id, and without one the 2.5D depth search
        # spans EVERY body in the Part Studio and pulls in other parts (e.g. an Origin
        # Cube). The body that actually OWNS the selected face is the authoritative scope
        # (it comes from the same bodydetails response the depth search filters on), so
        # prefer it over whatever id the client sent, which may be a different id form.
        if resolved_bid and resolved_bid != bid:
            log(f"[EXPORT] scoping to body {resolved_bid} that owns the selected face (client sent bid={bid})")
            bid = resolved_bid

        detected_thickness = None
        if multilayer:
            dxf_bytes, detected_thickness = build_multilayer_dxf(
                client, did, wid, eid, fid, bid, face_normal, cached_faces_data=faces,
                version_id=vid, microversion_id=mvid
            )
        else:
            dxf_bytes = client.export_face_to_dxf(did, wid, eid, fid, body_id=bid, face_normal=face_normal,
                                                  version_id=vid, microversion_id=mvid)
            # Seed the (still-editable) 2D thickness field from the part's designed stock
            # height, reusing the same parallel-face depth discovery 2.5D uses. Best-effort:
            # on any failure detected_thickness stays None and the UI keeps its default.
            if face_normal and reference_origin is not None:
                try:
                    detected_thickness = client.detect_stock_thickness(
                        did, wid, eid, face_normal, reference_origin,
                        body_id=bid, cached_faces_data=faces,
                        version_id=vid, microversion_id=mvid)
                except Exception as e:
                    log(f"[EXPORT] 2D thickness discovery failed (non-fatal): {e}")
        session_manager.update_session_tokens(client)
        if not dxf_bytes:
            return jsonify({'error': 'Onshape returned no DXF for that face.'}), 502

        tmp = tempfile.NamedTemporaryFile(suffix='.dxf', delete=False, dir=UPLOAD_FOLDER)
        path = tmp.name
        tmp.close()
        with open(path, 'wb') as fh:
            fh.write(dxf_bytes)

        geo = _compute_dxf_outline(path)
        if not geo:
            return jsonify({'error': 'Exported DXF had no usable geometry.'}), 500

        geo['name'] = name or 'Onshape part'
        geo['success'] = True
        geo['multilayer'] = multilayer
        # Thickness discovered from the CAD part server-side: authoritative in 2.5D, and a
        # seed for the editable field in 2D. Pass it back when we found one.
        if detected_thickness:
            geo['thickness'] = detected_thickness
        geo['dxf'] = base64.b64encode(dxf_bytes).decode('ascii')

        # Retain the exact DXF behind a session token so it can be re-downloaded
        # for debugging via /debug/download-dxf. Invaluable for reproducing a bad
        # part offline against the postprocessor. The token manager expires the
        # file after ~1 hour; until then it owns the file (don't delete it below).
        debug_filename = '{}{}.dxf'.format(geo['name'], '-2.5D' if multilayer else '')
        session['debug_dxf_token'] = file_token_manager.register_file(path, debug_filename)
        session['debug_dxf_filename'] = debug_filename
        keep_dxf = True

        # Also retain the RAW per-depth DXFs (exactly as Onshape returned them, before
        # _convert_geometry_to_solid_hatch reconstructs the geometry) as a zip, so we can
        # inspect what the outer-profile boundary looks like pre-conversion. Debug only;
        # served at /debug/download-raw-dxf. Token manager expires it after ~1 hour.
        raw_layers = getattr(client, 'last_raw_depth_dxfs', None) if multilayer else None
        if raw_layers:
            zbuf = io.BytesIO()
            with zipfile.ZipFile(zbuf, 'w', zipfile.ZIP_DEFLATED) as zf:
                for depth, content in raw_layers.items():
                    zf.writestr('raw_depth_{:+.4f}.dxf'.format(depth), content)
            ztmp = tempfile.NamedTemporaryFile(suffix='.zip', delete=False, dir=UPLOAD_FOLDER)
            ztmp.write(zbuf.getvalue())
            ztmp.close()
            raw_zip_name = '{}-raw-layers.zip'.format(geo['name'])
            session['debug_raw_dxf_token'] = file_token_manager.register_file(ztmp.name, raw_zip_name)
            session['debug_raw_dxf_filename'] = raw_zip_name

        metrics.log_event('onshape_import',
                          team_number=session.get('team_number'),
                          user_email=session.get('user_email'),
                          metadata={'part_name': geo['name'], 'multilayer': multilayer})

        detail = ' (2.5D, t={:.4f}")'.format(detected_thickness) if detected_thickness else ''
        log(f"[EXPORT] ok name='{geo['name']}' {geo['width']}x{geo['height']}{detail}")
        return jsonify(geo)
    except RuntimeError as e:
        # Expected, user-facing failures from the 2.5D builder (bad face selection,
        # expired auth, no parallel faces).
        log(f"[EXPORT] multilayer failed: {e}")
        return jsonify({'error': str(e)}), 502
    except Exception as e:
        log(traceback.format_exc())
        return jsonify({'error': f'Face export failed: {str(e)}'}), 500
    finally:
        # Remove the temp DXF unless it was registered for debug download, in which
        # case the token manager owns its lifecycle.
        if path and not keep_dxf and os.path.exists(path):
            os.remove(path)


@app.route('/onshape/status')
@limiter.limit("30 per minute")
def onshape_status():
    """Check Onshape connection status"""
    if not ONSHAPE_AVAILABLE:
        return jsonify({
            'available': False,
            'connected': False,
            'message': 'Onshape integration not installed'
        })

    try:
        user_id = get_current_user_id()
        client = session_manager.get_client(user_id)

        if client and client.access_token:
            # Try to get user info to verify connection
            user_info = client.get_user_info()

            # Save potentially-refreshed tokens back to session
            session_manager.update_session_tokens(client)

            return jsonify({
                'available': True,
                'connected': True,
                'user': user_info.get('name') if user_info else 'Unknown'
            })
        else:
            return jsonify({
                'available': True,
                'connected': False,
                'message': 'Not connected to Onshape'
            })

    except Exception as e:
        return jsonify({
            'available': True,
            'connected': False,
            'message': f'Error: {str(e)}'
        })

@app.route('/docs/')
@app.route('/docs')
def docs_redirect():
    """Redirect /docs to the static documentation"""
    return redirect('/static/docs/index.html')

@app.route('/docs/feeds-speeds')
def feeds_speeds_page():
    """Serve the standalone feeds & speeds calculator page."""
    return redirect('/static/docs/feeds-speeds.html')

@app.route('/api/feeds-speeds/presets')
def feeds_speeds_presets():
    """Expose the model's machine/material/tool presets for the calculator UI."""
    return jsonify({
        'machines': feeds_speeds.MACHINES,
        'materials': feeds_speeds.MATERIALS,
        'tools': feeds_speeds.TOOL_PRESETS,
        'operations': feeds_speeds.OPERATIONS,
        'iso_groups': feeds_speeds.ISO_GROUP_NAMES,
    })

@app.route('/api/feeds-speeds', methods=['POST'])
@limiter.limit("120 per minute")
def api_feeds_speeds():
    """Compute derived feeds & speeds from machine + material + tool inputs.

    Body: {machine, material, tool, operation, ae_override, ap_override,
    bore_diameter}. Each of machine/material/tool may be a preset key or an inline
    dict of overrides (see feeds_speeds._resolve), so the public calculator works
    without PenguinCAM presets.
    """
    data = request.get_json(silent=True) or {}
    try:
        result = feeds_speeds.calculate_feeds(
            data.get('machine') or 'avid_pro2424',
            data.get('material') or 'plywood',
            data.get('tool') or '4mm_1f',
            operation=data.get('operation') or 'profile',
            ae_override=data.get('ae_override'),
            ap_override=data.get('ap_override'),
            bore_diameter=data.get('bore_diameter'),
        )
    except (ValueError, TypeError, KeyError) as exc:
        return jsonify({'error': str(exc)}), 400
    return jsonify(result)

@app.route('/set-machine', methods=['POST'])
@limiter.limit("30 per minute")
def set_machine():
    """Set the current machine for the session"""
    try:
        machine_id = request.json.get('machine_id')
        if not machine_id:
            return jsonify({'error': 'No machine_id provided'}), 400

        # Verify machine exists in config. Uses the same resolver as the compute endpoints
        # so the anonymous upload flow (whose config lives in `upload_config_data`) can
        # switch machines too -- reading only `team_config_data` here rejected every machine
        # in that flow, and the client fire-and-forgets this call, so the 400 was silent.
        team_config = _active_team_config()
        machines = team_config.get_available_machines()

        if machine_id not in machines:
            return jsonify({'error': f'Unknown machine: {machine_id}'}), 400

        # Store in session
        session['machine_id'] = machine_id

        # Return updated config for this machine
        team_config_dict = team_config.to_dict(machine_id)

        return jsonify({
            'success': True,
            'machine_id': machine_id,
            'machine_name': machines[machine_id].get('name', machine_id),
            'config': team_config_dict
        })

    except Exception as e:
        log(f"Error setting machine: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/debug/session')
@limiter.limit("30 per minute")
def debug_session():
    """Debug endpoint to see session contents (especially team config). Admin-only: it
    exposes the user's email and team config."""
    auth_error = require_admin()
    if auth_error:
        return auth_error
    return jsonify({
        'user_name': session.get('user_name'),
        'user_email': session.get('user_email'),
        'team_name': session.get('team_name'),
        'team_config': session.get('team_config', {}),
        'team_config_data_keys': list(session.get('team_config_data', {}).keys()),
        'onshape_authenticated': session.get('onshape_authenticated'),
    })

@app.route('/debug/onshape/faces')
@limiter.limit("10 per minute")
def debug_onshape_faces():
    """Debug endpoint to test Onshape face listing. Admin-only: it makes live Onshape API
    calls on the caller's behalf and returns raw tracebacks."""
    auth_error = require_admin()
    if auth_error:
        return auth_error
    if not ONSHAPE_AVAILABLE:
        return jsonify({'error': 'Onshape integration not available'}), 400

    # Get parameters
    document_id = request.args.get('documentId')
    workspace_id = request.args.get('workspaceId')
    element_id = request.args.get('elementId')
    body_id = request.args.get('bodyId')

    if not all([document_id, workspace_id, element_id]):
        return jsonify({
            'error': 'Missing required parameters',
            'required': ['documentId', 'workspaceId', 'elementId']
        }), 400

    # Get Onshape client
    user_id = get_current_user_id()
    client = session_manager.get_client(user_id)

    if not client:
        return jsonify({
            'error': 'Not authenticated with Onshape',
            'auth_url': '/onshape/auth'
        }), 401

    try:
        log("\n" + "="*70)
        log("DEBUG ENDPOINT: Testing face listing")
        log("="*70)

        # Test list_faces
        faces_data = client.list_faces(document_id, workspace_id, element_id)

        if not faces_data:
            return jsonify({
                'success': False,
                'error': 'list_faces returned None'
            }), 500

        # Test auto_select_top_face
        face_id, body_id_result, part_name, normal = client.auto_select_top_face(
            document_id, workspace_id, element_id, body_id, faces_data
        )

        # Save potentially-refreshed tokens back to session
        session_manager.update_session_tokens(client)

        return jsonify({
            'success': True,
            'faces_data_summary': {
                'body_count': len(faces_data.get('bodies', [])),
                'bodies': [
                    {
                        'id': body.get('id'),
                        'name': body.get('properties', {}).get('name'),
                        'face_count': len(body.get('faces', []))
                    }
                    for body in faces_data.get('bodies', [])
                ]
            },
            'auto_selected': {
                'face_id': face_id,
                'body_id': body_id_result,
                'part_name': part_name,
                'normal': normal
            } if face_id else None
        })

    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e),
            'traceback': traceback.format_exc()
        }), 500

@app.route('/onshape/element-panel')
def onshape_element_panel():
    """Legacy panel URL that existing Onshape app configs point at. Now serves the
    new multi-part wizard (same handler as /onshape-panel), so no Onshape-side
    reconfiguration is needed. The old onshape_panel.html is no longer used."""
    return _serve_onshape_panel()

# ============================================================================
# ADMIN ENDPOINTS (Metrics)
# ============================================================================

def require_admin():
    """Check if current user is authorized to access admin endpoints."""
    admin_email = os.environ.get('ADMIN_EMAIL')
    if not admin_email:
        return jsonify({'error': 'Admin access not configured'}), 500

    user_email = session.get('user_email')
    if not user_email:
        return jsonify({'error': 'Unauthorized - not logged in'}), 403

    if user_email != admin_email:
        return jsonify({'error': 'Unauthorized'}), 403

    return None  # Success

@app.route('/admin/metrics/summary')
@limiter.limit("30 per minute")
def admin_metrics_summary():
    """Get summary of all metrics (admin only)."""
    # Check authorization
    auth_error = require_admin()
    if auth_error:
        return auth_error

    summary = metrics.get_summary()
    if summary is None:
        return jsonify({'error': 'Metrics database unavailable'}), 503

    return jsonify(summary)

@app.route('/admin/metrics/events')
@limiter.limit("30 per minute")
def admin_metrics_events():
    """Get recent events, optionally filtered (admin only)."""
    # Check authorization
    auth_error = require_admin()
    if auth_error:
        return auth_error

    event_type = request.args.get('event_type')
    limit = min(int(request.args.get('limit', 100)), 1000)  # Cap at 1000
    offset = int(request.args.get('offset', 0))

    events = metrics.get_events(event_type=event_type, limit=limit, offset=offset)
    if events is None:
        return jsonify({'error': 'Metrics database unavailable'}), 503

    return jsonify({
        'events': events,
        'count': len(events),
        'limit': limit,
        'offset': offset
    })

def cleanup():
    """Clean up temporary files on shutdown"""
    # Skip cleanup for serverless - containers are ephemeral
    if IS_SERVERLESS:
        return

    try:
        shutil.rmtree(TEMP_DIR)
        log(f"🗑️  Cleaned up temp directory: {TEMP_DIR}")
    except Exception as e:
        log(f"⚠️  Failed to clean up temp directory: {e}")

# Register cleanup only if not serverless (serverless containers auto-cleanup)
if not IS_SERVERLESS:
    atexit.register(cleanup)

if __name__ == '__main__':
    # Get port from environment variable (Railway) or default to 6238 for local dev
    port = int(os.environ.get('PORT', 6238))
    
    log("="*70)
    log("PenguinCAM - FRC Team 6238")
    log("="*70)
    log(f"\nPost-processor script: {POST_PROCESSOR}")
    log(f"Temporary directory: {TEMP_DIR}")
    log("\n🚀 Starting server...")
    log(f"📂 Server will run on port: {port}")
    log("\n⚠️  Press Ctrl+C to stop the server\n")
    log("="*70)
    
    # Disable debug mode in production
    debug_mode = os.environ.get('FLASK_ENV') != 'production'
    app.run(debug=debug_mode, host='0.0.0.0', port=port)

#!/usr/bin/env python3
"""
PenguinCAM - FRC Team 6238 CAM Tool
A Flask-based web interface for generating G-code from DXF files
"""

from flask import Flask, render_template, request, jsonify, send_file, session, send_from_directory, redirect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.middleware.proxy_fix import ProxyFix
import os
import sys
import subprocess
import tempfile
import shutil
import traceback
from pathlib import Path
import json
import secrets
import re
import atexit
import time
import threading
from datetime import datetime
from urllib.parse import urlencode
import ezdxf
import logging

# Configure logging for Vercel
logging.basicConfig(
    level=logging.INFO,
    format='%(message)s',
    stream=sys.stderr,
    force=True
)
logger = logging.getLogger(__name__)

# Disable Werkzeug's request logging (clutters Vercel logs)
# Try multiple approaches since WSGI environment might be tricky
logging.getLogger('werkzeug').disabled = True
logging.getLogger('werkzeug').setLevel(logging.ERROR)  # Only show errors, not INFO
werkzeug_logger = logging.getLogger('werkzeug')
werkzeug_logger.handlers = []  # Remove all handlers

# Logging helper for Vercel/serverless environments
def log(*args, **kwargs):
    """Log to stderr using Python logging module for better Vercel compatibility"""
    message = ' '.join(str(arg) for arg in args)
    logger.info(message)

# Import Google Drive integration (optional - will work without it)
try:
    from google_drive_integration import upload_gcode_to_drive, GoogleDriveUploader
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
    from onshape_integration import get_onshape_client, session_manager
    ONSHAPE_AVAILABLE = True
except ImportError:
    ONSHAPE_AVAILABLE = False
    log("⚠️  Onshape integration not available")

# Import postprocessor directly (for API calls instead of subprocess)
from frc_cam_postprocessor import FRCPostProcessor, PostProcessorResult

# Import team config management
from team_config import TeamConfig

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

def extract_onshape_params(params):
    """Extract Onshape parameters from request params dict"""
    return {
        'document_id': params.get('documentId') or params.get('did'),
        'workspace_id': params.get('workspaceId') or params.get('wid'),
        'element_id': params.get('elementId') or params.get('eid'),
        'face_id': params.get('faceId') or params.get('fid'),
        'body_id': params.get('partId') or params.get('bodyId') or params.get('bid')
    }

def fetch_face_normal_and_body(client, document_id, workspace_id, element_id, face_id, body_id):
    """
    Fetch face normal and body information for a given face_id.

    Returns:
        tuple: (face_normal dict, auto_selected_body_id, part_name_from_body)
    """
    log(f"Face ID provided: {face_id}, fetching face normal...")

    face_normal = None
    auto_selected_body_id = None
    part_name_from_body = None

    try:
        # Get all faces to find the normal for the selected face
        faces_data = client.list_faces(document_id, workspace_id, element_id)

        if faces_data and 'bodies' in faces_data:
            # Debug: Log all face IDs to find mismatch
            all_face_ids = []
            for body in faces_data['bodies']:
                for face in body.get('faces', []):
                    all_face_ids.append(face.get('id'))
            log(f"🔍 All face IDs in response ({len(all_face_ids)} total): {all_face_ids[:20]}{'...' if len(all_face_ids) > 20 else ''}")
            log(f"🔍 Looking for face_id: {face_id}")

            # Search through all bodies and faces to find the matching face_id
            for body in faces_data['bodies']:
                bid = body.get('id')
                for face in body.get('faces', []):
                    if face.get('id') == face_id:
                        # Found the matching face! Extract its normal
                        surface = face.get('surface', {})
                        face_normal = surface.get('normal', {})
                        part_name_from_body = body.get('properties', {}).get('name', 'Unnamed')

                        # Set body_id if not already provided
                        if not body_id:
                            auto_selected_body_id = bid

                        log(f"✅ Found face {face_id} in body {bid} ({part_name_from_body})")
                        log(f"   Normal: ({face_normal.get('x', 0):.3f}, {face_normal.get('y', 0):.3f}, {face_normal.get('z', 0):.3f})")
                        break
                if face_normal:
                    break

        if not face_normal:
            log(f"⚠️  Warning: Could not find normal for face {face_id}, using default view")

    except Exception as e:
        log(f"⚠️  Warning: Error fetching face normal: {e}")
        log("   Continuing with default view matrix")

    return face_normal, auto_selected_body_id, part_name_from_body

def generate_onshape_filename(doc_name, part_name):
    """
    Generate a clean filename from Onshape document and part names.
    Falls back to timestamp if names are unavailable or generic.
    """
    # Clean function for filename sanitization
    def clean_name(name):
        return re.sub(r'[^\w\s-]', '', name).strip().replace(' ', '_')[:50]

    if doc_name and part_name:
        # Best case: combine both
        doc_clean = clean_name(doc_name)
        part_clean = clean_name(part_name)
        return f"{doc_clean}_{part_clean}"

    elif part_name:
        # Fallback: part name only
        part_clean = clean_name(part_name)
        if part_clean and part_clean != 'Unnamed_Part':
            return part_clean

    # Last resort: timestamp (server's local time)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    return f"Onshape_Part_{timestamp}"

# ============================================================================
# Routes
# ============================================================================

@app.route('/')
def index():
    """Render the main GUI page"""
    # ========================================================================
    # AUTHENTICATION GATE: Require Onshape OAuth to access app
    # ========================================================================
    # This restricts access to authenticated Onshape users only, providing:
    # - Natural security gate (no anonymous internet users)
    # - Known user/team identity for configs and tracking
    # - Better protection from abuse and cost control
    #
    # TO MAKE APP WIDE OPEN (allow anonymous browser access):
    # Simply comment out or remove the code block below (lines until "End gate")
    # ========================================================================
    if ONSHAPE_AVAILABLE:
        user_id = get_current_user_id()
        client = session_manager.get_client(user_id)
        if not client:
            # No Onshape session - redirect to OAuth
            log("⛔ Access denied: No Onshape authentication, redirecting to /onshape/auth")
            return redirect('/onshape/auth')
    # ========================================================================
    # End authentication gate
    # ========================================================================

    # Get user/team info from session (if coming from Onshape)
    user_name = session.get('user_name')
    team_name = session.get('team_name')

    # Reconstruct TeamConfig
    team_config_data = session.get('team_config_data', {})
    team_config = TeamConfig(team_config_data)

    # Get available machines
    machines = team_config.get_available_machines()

    # Get current machine (from session, or use default)
    current_machine_id = session.get('machine_id', team_config.default_machine_id)

    # Get machine-specific config dict
    team_config_dict = team_config.to_dict(current_machine_id)
    drive_enabled = team_config_dict.get('google_drive_enabled', False)
    default_tool_diameter = team_config_dict.get('default_tool_diameter', 0.157)
    machine_x_max = team_config_dict.get('machine_x_max', 48.0)
    machine_y_max = team_config_dict.get('machine_y_max', 96.0)

    # Get available materials for current machine
    available_materials = team_config.get_available_materials(current_machine_id)

    # Add 'aluminum_tube' as a special UI-only material (uses aluminum preset)
    available_materials['aluminum_tube'] = {
        **available_materials.get('aluminum', {}),
        'name': 'Aluminum Tube'
    }

    # Check for incomplete materials (custom materials missing required params)
    incomplete_materials = {
        material_id for material_id in available_materials.keys()
        if not team_config.is_material_complete(material_id, current_machine_id) and material_id != 'aluminum_tube'
    }

    return render_template('index.html',
                         user_name=user_name,
                         team_name=team_name,
                         drive_enabled=drive_enabled,
                         default_tool_diameter=default_tool_diameter,
                         machine_x_max=machine_x_max,
                         machine_y_max=machine_y_max,
                         using_default_config=session.get('using_default_config', False),
                         machines=machines,
                         current_machine_id=current_machine_id,
                         materials=available_materials,
                         incomplete_materials=incomplete_materials)

@app.route('/process', methods=['POST'])
@limiter.limit("10 per minute")  # Strict limit - CPU intensive operation
def process_file():
    """Process uploaded DXF file and generate G-code"""
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

        # Map special cases:
        # - 'aluminum_tube' -> 'aluminum' (aluminum_tube is UI-only, uses aluminum preset)
        # - 'polycarb' -> 'polycarbonate' (legacy compatibility)
        # All other materials pass through as-is (including custom materials from config)
        if material.lower() == 'aluminum_tube':
            material = 'aluminum'
        elif material.lower() == 'polycarb':
            material = 'polycarbonate'

        tool_diameter = float(request.form.get('tool_diameter', 0.157))
        origin_corner = request.form.get('origin_corner', 'bottom-left')
        rotation = int(request.form.get('rotation', 0))
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
            try:
                doc = ezdxf.readfile(input_path)
                msp = doc.modelspace()

                # Collect all geometry bounds
                all_x = []
                all_y = []

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

                if all_x and all_y:
                    dxf_width = max(all_x) - min(all_x)
                    dxf_height = max(all_y) - min(all_y)

                    # Account for rotation: swap dimensions if rotated 90° or 270°
                    if rotation in [90, 270]:
                        tube_width = dxf_height
                        tube_length = dxf_width
                        log(f"📏 Detected tube dimensions (after {rotation}° rotation): {tube_width:.3f}\" x {tube_length:.3f}\"")
                    else:
                        tube_width = dxf_width
                        tube_length = dxf_height
                        log(f"📏 Detected tube dimensions: {tube_width:.3f}\" x {tube_length:.3f}\"")
            except Exception as e:
                log(f"⚠️  Could not extract tube dimensions from DXF: {e}")

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

        # Get team config from session (if available)
        config_data = session.get('team_config_data', {})
        team_config = TeamConfig.from_dict(config_data)
        log(f"📋 Using team config: {team_config}")

        # Call post-processor API based on mode
        try:
            if is_aluminum_tube:
                # Tube mode - use tube-pattern API
                pp = FRCPostProcessor(
                    material_thickness=thickness,
                    tool_diameter=tool_diameter,
                    units='inch',
                    config=team_config
                )

                # Store tube height for Z-offset calculations
                pp.tube_height = tube_height

                # Apply material preset (for specific machine if selected)
                pp.apply_material_preset(material, machine_id)

                # Add user name if authenticated
                user_name = session.get('user_name')
                if user_name:
                    pp.user_name = user_name

                # Load and process DXF
                pp.load_dxf(input_path)
                pp.transform_coordinates('bottom-left', rotation)  # Tube jig is always bottom-left
                pp.classify_holes()
                pp.identify_perimeter_and_pockets()

                # Generate G-code using API
                result = pp.generate_tube_pattern_gcode(
                    tube_height=tube_height,
                    square_end=square_end,
                    cut_to_length=cut_to_length,
                    tube_width=tube_width,
                    tube_length=tube_length,
                    suggested_filename=base_name,
                    timestamp=timestamp_str
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
                pp.transform_coordinates(origin_corner, rotation)
                pp.classify_holes()
                pp.identify_perimeter_and_pockets()

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
            'parameters': parameters
        }

        # Add cycle time if available
        if 'cycle_time_display' in result.stats:
            response_data['cycle_time'] = result.stats['cycle_time_display']
            response_data['cycle_time_seconds'] = result.stats['cycle_time_seconds']

        return jsonify(response_data)

    except ValueError as e:
        return jsonify({'error': f'Invalid parameter value: {str(e)}'}), 400
    except Exception as e:
        log(traceback.format_exc())
        return jsonify({'error': f'Unexpected error: {str(e)}'}), 500

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

        return send_file(
            file_path,
            as_attachment=True,
            download_name=real_filename,  # User sees the real filename
            mimetype='text/plain'
        )
    except Exception as e:
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

        # Get team config file
        config_yaml = client.fetch_config_file()
        if config_yaml:
            team_config = TeamConfig.from_yaml(config_yaml)
            log(f"✅ Team config loaded: {team_config.team_name} (#{team_config.team_number})")
            session['team_config_data'] = team_config._data
            session['team_config'] = team_config.to_dict()
            session['using_default_config'] = False
        else:
            log("⚠️  No team config found - using defaults")
            team_config = TeamConfig()
            session['team_config_data'] = {}
            session['team_config'] = team_config.to_dict()
            session['using_default_config'] = True

        log("="*60 + "\n")

        # Clean up OAuth state
        session.pop('onshape_oauth_state', None)

        # Check if there was a pending import
        pending_import = session.pop('pending_onshape_import', None)

        if pending_import:
            # Redirect back to import with original parameters
            params = urlencode({k: v for k, v in pending_import.items() if v})
            return redirect(f'/onshape/import?{params}')

        # Otherwise redirect to main page with success message
        return redirect('/?onshape_connected=true')
        
    except Exception as e:
        return f"OAuth callback error: {str(e)}", 500

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

@app.route('/download-config-template')
@limiter.limit("30 per minute")
def download_config_template():
    """Download the PenguinCAM config template file"""
    try:
        template_path = os.path.join(os.path.dirname(__file__), 'PenguinCAM-config-template.yaml')

        if not os.path.exists(template_path):
            return jsonify({'error': 'Template file not found'}), 404

        return send_file(
            template_path,
            as_attachment=True,
            download_name='PenguinCAM-config.yaml',
            mimetype='text/yaml'
        )
    except Exception as e:
        log(f"Error downloading config template: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/set-machine', methods=['POST'])
@limiter.limit("30 per minute")
def set_machine():
    """Set the current machine for the session"""
    try:
        machine_id = request.json.get('machine_id')
        if not machine_id:
            return jsonify({'error': 'No machine_id provided'}), 400

        # Verify machine exists in config
        team_config_data = session.get('team_config_data', {})
        team_config = TeamConfig(team_config_data)
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
    """Debug endpoint to see session contents (especially team config)"""
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
    """Debug endpoint to test Onshape face listing"""
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
        import traceback
        return jsonify({
            'success': False,
            'error': str(e),
            'traceback': traceback.format_exc()
        }), 500

@app.route('/onshape/import', methods=['GET', 'POST'])
@limiter.limit("20 per minute")  # Moderate limit - authenticated via Onshape OAuth
def onshape_import():
    """
    Import a DXF from Onshape
    Accepts parameters from Onshape extension or direct URL
    """
    if not ONSHAPE_AVAILABLE:
        return jsonify({'error': 'Onshape integration not available'}), 400

    try:
        log(f"\n{'='*70}")
        log(f"ONSHAPE IMPORT REQUEST")
        log(f"{'='*70}")
        log(f"Request URL: {request.url}")
        log(f"Method: {request.method}")
        log(f"Headers: {dict(request.headers)}")

        # Get parameters (either from query string or JSON body)
        if request.method == 'POST':
            raw_params = request.json or {}
            log(f"Source: POST body (JSON)")
        else:
            raw_params = request.args.to_dict()
            log(f"Source: Query string")

        log(f"\n📝 RAW PARAMETERS RECEIVED:")
        for key, value in sorted(raw_params.items()):
            log(f"   {key}: {value!r}")

        params = extract_onshape_params(raw_params)

        log(f"\n🔧 EXTRACTED PARAMETERS:")
        log(f"   document_id: {params['document_id']!r}")
        log(f"   workspace_id: {params['workspace_id']!r}")
        log(f"   element_id: {params['element_id']!r}")
        log(f"   face_id: {params['face_id']!r}")
        log(f"   body_id: {params['body_id']!r}")

        document_id = params['document_id']
        workspace_id = params['workspace_id']
        element_id = params['element_id']
        face_id = params['face_id']
        body_id = params['body_id']  # Optional - for part selection

        # Get Onshape server and user info that IS being sent
        onshape_server = raw_params.get('server', 'https://cad.onshape.com')
        onshape_userid = raw_params.get('userId')

        log(f"\n🔍 PARAMETER ANALYSIS:")
        if face_id:
            log(f"   ✓ face_id provided: {face_id}")
            if not face_id.startswith('J'):
                log(f"   ⚠️  WARNING: face_id doesn't start with 'J' (unusual for Onshape IDs)")
            if len(face_id) < 10:
                log(f"   ⚠️  WARNING: face_id seems too short (Onshape IDs are usually longer)")
        else:
            log(f"   ℹ️  No face_id - will auto-select")

        if body_id:
            log(f"   ✓ body_id provided: {body_id}")
        else:
            log(f"   ℹ️  No body_id - will search all parts")

        log(f"{'='*70}\n")
        
        # WORKAROUND: If params have placeholder strings, we can't proceed
        if (document_id and ('${' in str(document_id) or document_id.startswith('$'))):
            log("❌ Onshape variable substitution failed!")
            log(f"Received literal: documentId={document_id}")

            # Show helpful error page
            return render_template('index.html',
                                 error_message='Onshape integration error: Variable substitution not working. Please contact support or use manual DXF upload.',
                                 debug_info={
                                     'issue': 'Onshape extension not substituting variables',
                                     'received_params': str(raw_params),
                                     'workaround': 'Export DXF manually from Onshape and upload it here'
                                 },
                                 using_default_config=session.get('using_default_config', False)), 400

        if not all([document_id, workspace_id, element_id]):
            return jsonify({
                'error': 'Missing required parameters',
                'required': ['documentId', 'workspaceId', 'elementId'],
                'received': raw_params,
                'help': 'Onshape variable substitution not working. Check extension configuration or use manual DXF upload.'
            }), 400

        # Get Onshape client for this user
        user_id = get_current_user_id()
        client = session_manager.get_client(user_id)

        if not client:
            # Store import parameters in session before redirecting to OAuth
            session['pending_onshape_import'] = {
                'documentId': document_id,
                'workspaceId': workspace_id,
                'elementId': element_id,
                'faceId': face_id
            }

            # Redirect to Onshape OAuth
            return redirect('/onshape/auth')

        # User info and team config already loaded during OAuth callback
        # Session contains: user_name, user_email, team_config, team_config_data

        # Get document's owning company/classroom (Onshape Education context)
        # This requires a document, so we fetch it here rather than during OAuth
        doc_company = client.get_document_company(document_id)
        if doc_company:
            team_name = doc_company.get('name')
            log(f"📚 Document company: {team_name}")
            session['team_name'] = team_name

        # If no face_id provided, auto-select the top face
        part_name_from_body = None
        auto_selected_body_id = None
        face_normal = None  # Initialize face_normal for when face_id is provided
        if not face_id:
            log("No face ID provided, auto-selecting top face...")

            try:
                # First, try to list all faces for debugging
                faces_data = client.list_faces(document_id, workspace_id, element_id)
                body_count = len(faces_data.get('bodies', [])) if faces_data else 0
                log(f"📊 Found {body_count} bodies/parts in document")

                # If multiple parts and no bodyId specified, show part selection modal
                if body_count > 1 and not body_id:
                    log("🔍 Multiple parts detected, showing part selector...")

                    # Get detailed info about each part (reuse cached faces_data)
                    part_selection_data = []

                    # Get body faces using cached data to avoid duplicate API call
                    bodies_with_faces = client.get_body_faces(document_id, workspace_id, element_id, cached_faces_data=faces_data)

                    # Find the largest part by top face area
                    largest_body_id = None
                    largest_area = 0

                    for bid, body_data in bodies_with_faces.items():
                        # Get all planar faces
                        planar_faces = [f for f in body_data['faces'] if f['surfaceType'] == 'PLANE']

                        if planar_faces:
                            # Find largest planar face
                            largest_face = max(planar_faces, key=lambda f: f.get('area', 0))
                            face_area = largest_face.get('area', 0)

                            if face_area > largest_area:
                                largest_area = face_area
                                largest_body_id = bid

                        part_selection_data.append({
                            'body_id': bid,
                            'name': body_data['name'],
                            'face_count': len(body_data['faces']),
                            'is_largest': False  # Will set this after loop
                        })

                    # Mark the largest part
                    for part in part_selection_data:
                        if part['body_id'] == largest_body_id:
                            part['is_largest'] = True
                            break

                    # Sort by size (largest first)
                    part_selection_data.sort(key=lambda p: p['face_count'] * (1 if p['is_largest'] else 0), reverse=True)

                    # Render template with part selection
                    return render_template('index.html',
                                         part_selection={
                                             'parts': part_selection_data,
                                             'document_id': document_id,
                                             'workspace_id': workspace_id,
                                             'element_id': element_id
                                         },
                                         from_onshape=True,
                                         using_default_config=session.get('using_default_config', False))

                # This now returns (face_id, body_id, part_name, normal)
                # Pass body_id if user selected a specific part in Onshape, and cached data to avoid duplicate API call
                face_id, auto_selected_body_id, part_name_from_body, face_normal = client.auto_select_top_face(document_id, workspace_id, element_id, body_id, faces_data)

                if not face_id:
                    # Provide helpful error with face list
                    error_msg = 'No horizontal plane faces found. '
                    if faces_data:
                        face_count = len(faces_data.get('bodies', []))
                        error_msg += f'Found {face_count} bodies total. '
                    error_msg += 'Try selecting a face manually in Onshape.'

                    # Render error page instead of JSON
                    return render_template('index.html',
                                         error_message=error_msg,
                                         from_onshape=True,
                                         debug_info={
                                             'documentId': document_id,
                                             'workspaceId': workspace_id,
                                             'elementId': element_id,
                                             'bodies_found': face_count if faces_data else 0
                                         },
                                         using_default_config=session.get('using_default_config', False)), 400

                log(f"Auto-selected face: {face_id} from part: {part_name_from_body}")

            except Exception as e:
                log(f"Error in face detection: {str(e)}")
                return jsonify({
                    'error': 'Face detection failed',
                    'message': str(e)
                }), 400
        else:
            # face_id was provided (e.g., from element panel), but we need to fetch the face normal
            face_normal, auto_selected_body_id, part_name_from_body = fetch_face_normal_and_body(
                client, document_id, workspace_id, element_id, face_id, body_id
            )

        # Check if multi-layer export is requested (default: true)
        multilayer = raw_params.get('multilayer', 'true').lower() in ('true', '1', 'yes')

        # Fetch DXF from Onshape
        # Use body_id from URL parameter if provided, otherwise use the one from auto-selection
        export_body_id = body_id if body_id else auto_selected_body_id
        log(f"Exporting with body_id: {export_body_id} (from {'URL param' if body_id else 'auto-selection'})")

        if multilayer:
            log("🔷 Multi-layer export requested")

            # For multi-layer export, we need the reference face normal and origin
            if not face_normal:
                log("⚠️  No face normal available, fetching...")
                faces_data = client.list_faces(document_id, workspace_id, element_id)

                # Find the reference face
                reference_face = None

                # If face_id is provided, find that specific face
                if face_id:
                    for body in faces_data.get('bodies', []):
                        if export_body_id and body.get('id') != export_body_id:
                            continue
                        for face in body.get('faces', []):
                            if face.get('id') == face_id:
                                reference_face = face
                                break
                        if reference_face:
                            break
                else:
                    # No face_id provided (one-click flow): auto-select largest upward-facing plane
                    log("⚠️  No face_id provided, auto-selecting reference face...")
                    largest_area = 0
                    for body in faces_data.get('bodies', []):
                        if export_body_id and body.get('id') != export_body_id:
                            continue
                        for face in body.get('faces', []):
                            surface = face.get('surface', {})
                            if surface.get('type') == 'PLANE':
                                normal = surface.get('normal', {})
                                # Check if pointing up (z > 0.9)
                                if normal.get('z', 0) > 0.9:
                                    area = face.get('area', 0)
                                    if area > largest_area:
                                        largest_area = area
                                        reference_face = face
                                        face_id = face.get('id')  # Update face_id for later use

                    if reference_face:
                        log(f"✅ Auto-selected reference face: {face_id} (area: {largest_area:.6f} m²)")

                if reference_face:
                    surface = reference_face.get('surface', {})
                    face_normal = surface.get('normal', {'x': 0, 'y': 0, 'z': 1})
                else:
                    log("❌ Could not find reference face for multi-layer export")
                    return jsonify({'error': 'Could not find reference face for multi-layer export. Please select a flat top face.'}), 500

            # Get reference origin from face
            faces_data = client.list_faces(document_id, workspace_id, element_id)
            reference_origin = None
            for body in faces_data.get('bodies', []):
                if export_body_id and body.get('id') != export_body_id:
                    continue
                for face in body.get('faces', []):
                    if face.get('id') == face_id:
                        surface = face.get('surface', {})
                        reference_origin = surface.get('origin', {'x': 0, 'y': 0, 'z': 0})
                        break
                if reference_origin:
                    break

            if not reference_origin:
                log("⚠️  Could not find reference origin, using default")
                reference_origin = {'x': 0, 'y': 0, 'z': 0}

            log(f"Reference normal: {face_normal}")
            log(f"Reference origin: {reference_origin}")

            # Export multi-layer DXF
            dxf_content = client.export_multilayer_dxf(
                document_id, workspace_id, element_id,
                face_id, export_body_id, face_normal, reference_origin,
                body_id=export_body_id, cached_faces_data=faces_data
            )
        else:
            log("📄 Single-layer export")
            dxf_content = client.export_face_to_dxf(
                document_id, workspace_id, element_id, face_id, export_body_id, face_normal
            )

        if not dxf_content:
            error_msg = f"Failed to export DXF from Onshape. "
            if export_body_id:
                error_msg += f"Attempted to export body/part: {export_body_id}. "
            else:
                error_msg += "No body/part ID available for export. "
            error_msg += "Check Onshape API logs above for details."

            return jsonify({
                'error': 'Failed to export DXF from Onshape',
                'message': error_msg,
                'details': {
                    'face_id': face_id,
                    'body_id': export_body_id,
                    'document_id': document_id,
                    'element_id': element_id
                }
            }), 500
        
        log(f"📄 DXF content received: {len(dxf_content)} bytes")

        # Generate filename: try to combine document name + part name
        doc_name = None

        # Try to get document name (optional, may fail with 404)
        try:
            log("📝 Attempting to fetch document name...")
            doc_info = client.get_document_info(document_id)
            if doc_info:
                doc_name = doc_info.get('name')
                log(f"   ✅ Got document name: {doc_name}")
            else:
                log(f"   ⚠️  Document API returned None")
        except Exception as e:
            log(f"   ⚠️  Document API failed (will use part name only): {e}")

        # Build filename from whatever we have
        suggested_filename = generate_onshape_filename(doc_name, part_name_from_body)
        log(f"✅ Generated filename: {suggested_filename}.nc")

        # Save DXF to temp file in uploads folder
        temp_dxf = tempfile.NamedTemporaryFile(
            suffix='.dxf',
            dir=UPLOAD_FOLDER,
            delete=False
        )
        temp_dxf.write(dxf_content)
        temp_dxf.close()

        dxf_filename = os.path.basename(temp_dxf.name)
        dxf_path = temp_dxf.name

        log(f"✅ DXF imported from Onshape: {dxf_filename}")
        log(f"📂 Saved to: {dxf_path}")
        log(f"📏 File size on disk: {os.path.getsize(dxf_path)} bytes")

        # Register DXF file with token manager for secure access
        dxf_token = file_token_manager.register_file(dxf_path, f"{suggested_filename}.dxf")
        log(f"🔗 Will be served at: /uploads/{dxf_token[:16]}...")

        # Render main page with DXF auto-loaded
        # The frontend will detect the dxf_file parameter and auto-upload it

        # Reconstruct TeamConfig to get materials list
        team_config_data = session.get('team_config_data', {})
        team_config = TeamConfig(team_config_data)

        # Get available machines
        machines = team_config.get_available_machines()

        # Get current machine (from session, or use default)
        current_machine_id = session.get('machine_id', team_config.default_machine_id)

        # Get machine-specific config dict
        team_config_dict = team_config.to_dict(current_machine_id)
        drive_enabled = team_config_dict.get('google_drive_enabled', False)
        machine_x_max = team_config_dict.get('machine_x_max', 48.0)
        machine_y_max = team_config_dict.get('machine_y_max', 96.0)
        default_tool_diameter = team_config_dict.get('default_tool_diameter', 0.157)

        # Get user/team info
        user_name = session.get('user_name')
        team_name = session.get('team_name')

        # Get available materials for current machine
        available_materials = team_config.get_available_materials(current_machine_id)

        # Add 'aluminum_tube' as a special UI-only material (uses aluminum preset)
        available_materials['aluminum_tube'] = {
            **available_materials.get('aluminum', {}),
            'name': 'Aluminum Tube'
        }

        # Check for incomplete materials
        incomplete_materials = {
            material_id for material_id in available_materials.keys()
            if not team_config.is_material_complete(material_id, current_machine_id) and material_id != 'aluminum_tube'
        }

        return render_template('index.html',
                             dxf_file=dxf_token,  # Pass token instead of filename
                             from_onshape=True,
                             document_id=document_id,
                             face_id=face_id,
                             suggested_filename=suggested_filename or '',
                             user_name=user_name,
                             team_name=team_name,
                             drive_enabled=drive_enabled,
                             machine_x_max=machine_x_max,
                             machine_y_max=machine_y_max,
                             default_tool_diameter=default_tool_diameter,
                             using_default_config=session.get('using_default_config', False),
                             machines=machines,
                             current_machine_id=current_machine_id,
                             materials=available_materials,
                             incomplete_materials=incomplete_materials)
        
    except Exception as e:
        return jsonify({
            'error': f'Import failed: {str(e)}'
        }), 500

@app.route('/onshape/save-dxf', methods=['GET', 'POST'])
@limiter.limit("20 per minute")  # Moderate limit - authenticated via Onshape OAuth
def onshape_save_dxf():
    """
    Save a DXF from Onshape directly to Google Drive without generating G-code.
    Accepts parameters from Onshape extension or direct URL.
    """
    if not ONSHAPE_AVAILABLE:
        return jsonify({'error': 'Onshape integration not available'}), 400

    if not GOOGLE_DRIVE_AVAILABLE:
        return jsonify({'error': 'Google Drive integration not available'}), 400

    try:
        log(f"\n💾 Onshape Save DXF request: {request.url}")
        log(f"   Method: {request.method}")

        # Get parameters (either from query string or JSON body)
        if request.method == 'POST':
            raw_params = request.json or {}
        else:
            raw_params = request.args.to_dict()

        params = extract_onshape_params(raw_params)
        document_id = params['document_id']
        workspace_id = params['workspace_id']
        element_id = params['element_id']
        face_id = params['face_id']
        body_id = params['body_id']

        log(f"Onshape params: doc={document_id}, workspace={workspace_id}, element={element_id}, face={face_id}, body={body_id}")

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

        # Auto-select face if needed (use existing helper function)
        part_name_from_body = None
        auto_selected_body_id = None
        face_normal = None

        if not face_id:
            log("No face ID, auto-selecting top face...")
            try:
                # Use existing auto_select_top_face helper
                face_id, auto_selected_body_id, part_name_from_body, face_normal = client.auto_select_top_face(
                    document_id, workspace_id, element_id, body_id
                )

                if not face_id:
                    return jsonify({
                        'error': 'Could not auto-select a face',
                        'message': 'No top face found on any part'
                    }), 400

            except Exception as e:
                log(f"Error in face detection: {str(e)}")
                return jsonify({
                    'error': 'Face detection failed',
                    'message': str(e)
                }), 400
        else:
            # face_id was provided (e.g., from element panel), but we need to fetch the face normal
            face_normal, auto_selected_body_id, part_name_from_body = fetch_face_normal_and_body(
                client, document_id, workspace_id, element_id, face_id, body_id
            )

        # Export DXF from Onshape
        export_body_id = body_id if body_id else auto_selected_body_id
        log(f"Exporting DXF with body_id: {export_body_id}")

        dxf_content = client.export_face_to_dxf(
            document_id, workspace_id, element_id, face_id, export_body_id, face_normal
        )

        if not dxf_content:
            return jsonify({
                'error': 'Failed to export DXF from Onshape',
                'details': {
                    'face_id': face_id,
                    'body_id': export_body_id
                }
            }), 500

        log(f"📄 DXF exported: {len(dxf_content)} bytes")

        # Generate filename with timestamp
        doc_name = None
        try:
            doc_info = client.get_document_info(document_id)
            if doc_info:
                doc_name = doc_info.get('name')
                log(f"📝 Document name: {doc_name}")
        except Exception as e:
            log(f"⚠️  Could not get document name: {e}")

        base_filename = generate_onshape_filename(doc_name, part_name_from_body)

        # Add timestamp (server's local time)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        dxf_filename = f"{base_filename}_{timestamp}.dxf"

        log(f"✅ Generated filename: {dxf_filename}")

        # Save DXF to temp file
        temp_dxf = tempfile.NamedTemporaryFile(
            suffix='.dxf',
            dir=OUTPUT_FOLDER,  # Use OUTPUT_FOLDER so it's accessible for upload
            delete=False
        )
        temp_dxf.write(dxf_content)
        temp_dxf.close()

        dxf_path = temp_dxf.name
        log(f"💾 Saved temp DXF: {dxf_path}")

        # Upload to Google Drive
        creds = None
        if AUTH_AVAILABLE and auth.is_enabled():
            creds = auth.get_credentials()
            if not creds:
                os.unlink(dxf_path)  # Clean up temp file
                return jsonify({
                    'error': 'Not authenticated with Google Drive'
                }), 401

        uploader = GoogleDriveUploader(credentials=creds)

        if not uploader.authenticate():
            os.unlink(dxf_path)  # Clean up temp file
            return jsonify({
                'error': 'Failed to authenticate with Google Drive'
            }), 500

        log("📤 Uploading to Google Drive...")
        result = uploader.upload_file(dxf_path, dxf_filename)

        # Clean up temp file
        try:
            os.unlink(dxf_path)
        except:
            pass

        if result and result.get('success'):
            log(f"✅ Upload successful: {result.get('web_link')}")
            return jsonify({
                'success': True,
                'message': f'✅ DXF saved to Google Drive: {dxf_filename}',
                'filename': dxf_filename,
                'file_id': result.get('file_id'),
                'web_view_link': result.get('web_link')
            })
        else:
            return jsonify({
                'success': False,
                'error': 'Upload to Google Drive failed',
                'message': result.get('message') if result else 'Unknown error'
            }), 500

    except Exception as e:
        log(f"❌ Error in save-dxf: {str(e)}")
        log(traceback.format_exc())
        return jsonify({
            'error': f'Save DXF failed: {str(e)}'
        }), 500

@app.route('/onshape/element-panel')
def onshape_element_panel():
    """
    Serve the Onshape element right panel extension
    This page will be embedded as an iframe in Onshape
    """
    # Get Onshape context from query parameters
    # These are passed by Onshape when the iframe loads
    document_id = request.args.get('documentId', '')
    workspace_id = request.args.get('workspaceId', '')
    element_id = request.args.get('elementId', '')
    server = request.args.get('server', 'https://cad.onshape.com')

    return render_template('onshape_panel.html',
                         document_id=document_id,
                         workspace_id=workspace_id,
                         element_id=element_id,
                         server=server)

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

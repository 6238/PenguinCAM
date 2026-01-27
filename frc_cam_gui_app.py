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
from zoneinfo import ZoneInfo
from urllib.parse import urlencode
import ezdxf

# Import Google Drive integration (optional - will work without it)
try:
    from google_drive_integration import upload_gcode_to_drive, GoogleDriveUploader
    GOOGLE_DRIVE_AVAILABLE = True
except ImportError:
    GOOGLE_DRIVE_AVAILABLE = False
    print("⚠️  Google Drive integration not available (missing dependencies)")
    print("   Install with: pip install google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client")

# Import authentication (optional - will work without it)
try:
    from penguincam_auth import init_auth
    AUTH_AVAILABLE = True
except ImportError:
    AUTH_AVAILABLE = False
    print("⚠️  Authentication module not available")

# Import Onshape integration (optional - will work without it)
try:
    from onshape_integration import get_onshape_client, session_manager
    ONSHAPE_AVAILABLE = True
except ImportError:
    ONSHAPE_AVAILABLE = False
    print("⚠️  Onshape integration not available")

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
    """

    def __init__(self):
        self.tokens = {}  # token → {'filepath': ..., 'filename': ..., 'created': timestamp}
        self.lock = threading.Lock()

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
        with self.lock:
            self.tokens[token] = {
                'filepath': filepath,
                'filename': real_filename,
                'created': time.time()
            }
        print(f"🔐 Registered file: {real_filename} → token {token[:16]}...")
        return token

    def get_file(self, token):
        """
        Get file info for a token.

        Args:
            token: The secure token

        Returns:
            Dict with 'filepath' and 'filename', or None if not found
        """
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
                            print(f"🗑️  Cleaned up expired file ({age/60:.1f} min old): {info['filename']}")
                    except Exception as e:
                        print(f"⚠️  Failed to delete {info['filepath']}: {e}")

            # Remove expired tokens from mapping
            for token in expired_tokens:
                del self.tokens[token]

            if expired_tokens:
                print(f"✅ Cleanup complete: removed {len(expired_tokens)} expired file(s)")

def cleanup_worker():
    """Background thread that periodically cleans up old files"""
    while True:
        time.sleep(600)  # Run every 10 minutes
        try:
            file_token_manager.cleanup_old_files(max_age_seconds=3600)  # 1 hour
        except Exception as e:
            print(f"⚠️  Error in cleanup worker: {e}")

# Initialize file token manager
file_token_manager = FileTokenManager()

# Start background cleanup thread
cleanup_thread = threading.Thread(target=cleanup_worker, daemon=True)
cleanup_thread.start()
print("✅ File token manager initialized with auto-cleanup (1 hour expiry)")

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB max file size

# Trust proxy headers (Railway, nginx, etc.)
# This tells Flask it's behind HTTPS even if internal requests are HTTP
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

# Set secret key for session management (required by auth and Onshape integration)
# Check environment variable first for persistent sessions across deployments
secret_key = os.environ.get('FLASK_SECRET_KEY')
if secret_key:
    app.secret_key = secret_key
    print("✅ Using persistent FLASK_SECRET_KEY from environment")
elif not app.secret_key:
    app.secret_key = secrets.token_hex(32)
    print("⚠️  WARNING: Using random secret key. Sessions will not persist across restarts.")
    print("   Set FLASK_SECRET_KEY environment variable for persistent sessions.")

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
print("✅ Rate limiting enabled (200 requests/hour default)")

# Directory for temporary files
TEMP_DIR = tempfile.mkdtemp()
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
    print(f"Face ID provided: {face_id}, fetching face normal...")

    face_normal = None
    auto_selected_body_id = None
    part_name_from_body = None

    try:
        # Get all faces to find the normal for the selected face
        faces_data = client.list_faces(document_id, workspace_id, element_id)

        if faces_data and 'bodies' in faces_data:
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

                        print(f"✅ Found face {face_id} in body {bid} ({part_name_from_body})")
                        print(f"   Normal: ({face_normal.get('x', 0):.3f}, {face_normal.get('y', 0):.3f}, {face_normal.get('z', 0):.3f})")
                        break
                if face_normal:
                    break

        if not face_normal:
            print(f"⚠️  Warning: Could not find normal for face {face_id}, using default view")

    except Exception as e:
        print(f"⚠️  Warning: Error fetching face normal: {e}")
        print("   Continuing with default view matrix")

    return face_normal, auto_selected_body_id, part_name_from_body

def generate_pacific_timestamp():
    """Generate timestamp string in Pacific timezone"""
    pacific_time = datetime.now(ZoneInfo("America/Los_Angeles"))
    return pacific_time.strftime("%Y-%m-%d_%H-%M-%S")

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

    # Last resort: timestamp (Pacific time)
    return f"Onshape_Part_{generate_pacific_timestamp()}"

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
            print("⛔ Access denied: No Onshape authentication, redirecting to /onshape/auth")
            return redirect('/onshape/auth')
    # ========================================================================
    # End authentication gate
    # ========================================================================

    # Get user/team info from session (if coming from Onshape)
    user_name = session.get('user_name')
    team_name = session.get('team_name')
    team_config = session.get('team_config', {})
    drive_enabled = team_config.get('google_drive_enabled', False)

    return render_template('index.html',
                         user_name=user_name,
                         team_name=team_name,
                         drive_enabled=drive_enabled)

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

        # Map UI material names to post-processor material names
        material_mapping = {
            'polycarb': 'polycarbonate',
            'polycarbonate': 'polycarbonate',
            'plywood': 'plywood',
            'aluminum': 'aluminum',
            'aluminum_tube': 'aluminum'  # Use aluminum presets for tube
        }
        material = material_mapping.get(material.lower(), 'plywood')

        tool_diameter = float(request.form.get('tool_diameter', 0.157))
        origin_corner = request.form.get('origin_corner', 'bottom-left')
        rotation = int(request.form.get('rotation', 0))
        suggested_filename = request.form.get('suggested_filename', '')

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
                        print(f"📏 Detected tube dimensions (after {rotation}° rotation): {tube_width:.3f}\" x {tube_length:.3f}\"")
                    else:
                        tube_width = dxf_width
                        tube_length = dxf_height
                        print(f"📏 Detected tube dimensions: {tube_width:.3f}\" x {tube_length:.3f}\"")
            except Exception as e:
                print(f"⚠️  Could not extract tube dimensions from DXF: {e}")

        # Generate suggested filename base (without extension or timestamp)
        if suggested_filename:
            # Use Onshape-derived name
            base_name = suggested_filename
            print(f"📝 Using Onshape filename base: {base_name}")
        else:
            # Use DXF filename
            base_name = Path(file.filename).stem
            print(f"📝 Using DXF filename base: {base_name}")

        print(f"🚀 Running post-processor API...")

        # Call post-processor API based on mode
        try:
            if is_aluminum_tube:
                # Tube mode - use tube-pattern API
                pp = FRCPostProcessor(
                    material_thickness=thickness,
                    tool_diameter=tool_diameter,
                    units='inch'
                )

                # Store tube height for Z-offset calculations
                pp.tube_height = tube_height

                # Apply material preset
                pp.apply_material_preset(material)

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
                    suggested_filename=base_name
                )
            else:
                # Standard mode - use standard API
                pp = FRCPostProcessor(
                    material_thickness=thickness,
                    tool_diameter=tool_diameter,
                    units='inch'
                )

                # Apply material preset
                pp.apply_material_preset(material)

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
                result = pp.generate_gcode(suggested_filename=base_name)

            if not result.success:
                print(f"❌ Post-processor API failed!")
                for error in result.errors:
                    print(f"   Error: {error}")
                return jsonify({
                    'error': 'Post-processor failed',
                    'details': '\n'.join(result.errors)
                }), 500

            # Write G-code to file
            output_path = os.path.join(OUTPUT_FOLDER, result.filename)
            with open(output_path, 'w') as f:
                f.write(result.gcode)

            print(f"✅ Output file created: {os.path.getsize(output_path)} bytes")
            print(f"📄 Output file: {output_path}")

            # Register file with token manager for secure access
            actual_filename = result.filename
            output_token = file_token_manager.register_file(output_path, actual_filename)

        except Exception as e:
            print(f"❌ Post-processor API error: {e}")
            traceback.print_exc()
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
        traceback.print_exc()
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

        print(f"📥 Download request: token {token[:16]}... → {real_filename}")

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

        print(f"📂 Upload preview: token {token[:16]}... → {file_info['filename']}")

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
    print(f"📤 Drive upload requested for token: {token[:16]}...")

    if not GOOGLE_DRIVE_AVAILABLE:
        print("❌ Google Drive integration not available")
        return jsonify({
            'success': False,
            'message': 'Google Drive integration not available'
        }), 400

    try:
        # Look up file by token
        file_info = file_token_manager.get_file(token)
        if not file_info:
            print(f"❌ Token not found or expired: {token[:16]}...")
            return jsonify({
                'success': False,
                'message': 'File not found or expired'
            }), 404

        file_path = file_info['filepath']
        real_filename = file_info['filename']

        print(f"📂 Looking for file at: {file_path}")
        print(f"📂 Real filename: {real_filename}")
        print(f"📂 File exists: {os.path.exists(file_path)}")

        if not os.path.exists(file_path):
            print(f"❌ File not found: {file_path}")
            return jsonify({
                'success': False,
                'message': 'File not found'
            }), 404
        
        # Get credentials from session
        creds = None
        if AUTH_AVAILABLE and auth.is_enabled():
            print("🔐 Getting credentials from session...")
            creds = auth.get_credentials()
            if not creds:
                print("❌ No credentials in session")
                return jsonify({
                    'success': False,
                    'message': 'Not authenticated with Google Drive'
                }), 401
            print(f"✅ Got credentials, scopes: {creds.scopes if hasattr(creds, 'scopes') else 'unknown'}")
        
        # Create uploader with credentials
        print("🔧 Creating GoogleDriveUploader...")
        uploader = GoogleDriveUploader(credentials=creds)
        
        print("🔐 Authenticating...")
        if not uploader.authenticate():
            print("❌ Authentication failed")
            return jsonify({
                'success': False,
                'message': 'Failed to authenticate with Google Drive'
            }), 500
        
        print("✅ Authenticated, uploading file...")
        # Upload the file with real filename
        result = uploader.upload_file(file_path, real_filename)

        print(f"📤 Upload result: {result}")

        if result and result.get('success'):
            print(f"✅ Upload successful: {result.get('web_link')}")
            return jsonify({
                'success': True,
                'message': f'✅ Uploaded: {real_filename}',
                'file_id': result.get('file_id'),
                'web_view_link': result.get('web_link')
            })
        else:
            print(f"❌ Upload failed: {result.get('message') if result else 'Unknown error'}")
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
        # Log the complete incoming URL for debugging
        print(f"\n🔗 Complete request URL: {request.url}")
        print(f"   Method: {request.method}")

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
        body_id = params['body_id']  # Optional - for part selection

        # Get Onshape server and user info that IS being sent
        onshape_server = raw_params.get('server', 'https://cad.onshape.com')
        onshape_userid = raw_params.get('userId')

        print(f"Onshape params received: {raw_params}")
        print(f"  Extracted body_id/partId: {body_id!r}")
        if body_id:
            print(f"  ✅ User selected body/part: {body_id}")
        else:
            print(f"  ⚠️  No partId received - will search all parts in document")
        
        # WORKAROUND: If params have placeholder strings, we can't proceed
        if (document_id and ('${' in str(document_id) or document_id.startswith('$'))):
            print("❌ Onshape variable substitution failed!")
            print(f"Received literal: documentId={document_id}")

            # Show helpful error page
            return render_template('index.html',
                                 error_message='Onshape integration error: Variable substitution not working. Please contact support or use manual DXF upload.',
                                 debug_info={
                                     'issue': 'Onshape extension not substituting variables',
                                     'received_params': str(raw_params),
                                     'workaround': 'Export DXF manually from Onshape and upload it here'
                                 }), 400

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

        # ========================================================================
        # Fetch user info, team info, and team config
        # ========================================================================
        print("\n" + "="*60)
        print("Fetching user, company, and config info from Onshape")
        print("="*60)

        # 1. Get user session info
        print("\n1️⃣  User Session Info:")
        user_session = client.get_user_session_info()
        user_name = None
        if user_session:
            user_name = user_session.get('name')
            user_email = user_session.get('email')
            print(f"   Name: {user_name}")
            print(f"   Email: {user_email}")
            # Store in session for UI
            session['user_name'] = user_name
            session['user_email'] = user_email

        # 2. Get document's owning company
        print("\n2️⃣  Document Company:")
        doc_company = client.get_document_company(document_id)
        team_name = None
        if doc_company:
            team_name = doc_company.get('name')
            print(f"   Company Name: {team_name}")
            print(f"   Company ID: {doc_company.get('id')}")
            # Store in session for UI
            session['team_name'] = team_name
        else:
            print("   No company found (document may be owned by user)")

        # 3. Get team config file
        print("\n3️⃣  Team Configuration File:")
        team_config = None
        config_yaml = client.fetch_config_file()
        if config_yaml:
            team_config = TeamConfig.from_yaml(config_yaml)
            print("   ✅ Successfully parsed team configuration:")
            print(f"      Team Number: {team_config.team_number}")
            print(f"      Team Name: {team_config.team_name}")
            print(f"      Drive Enabled: {team_config.google_drive_enabled}")
            if team_config.google_drive_folder_id:
                print(f"      Drive Folder ID: {team_config.google_drive_folder_id}")
            # Store config dict in session for UI
            session['team_config'] = team_config.to_dict()
        else:
            print("   ⚠️  No team configuration found - using defaults")
            team_config = TeamConfig.default()
            session['team_config'] = team_config.to_dict()

        print("\n" + "="*60 + "\n")
        # ========================================================================

        # If no face_id provided, auto-select the top face
        part_name_from_body = None
        auto_selected_body_id = None
        face_normal = None  # Initialize face_normal for when face_id is provided
        if not face_id:
            print("No face ID provided, auto-selecting top face...")

            try:
                # First, try to list all faces for debugging
                faces_data = client.list_faces(document_id, workspace_id, element_id)
                body_count = len(faces_data.get('bodies', [])) if faces_data else 0
                print(f"📊 Found {body_count} bodies/parts in document")

                # If multiple parts and no bodyId specified, show part selection modal
                if body_count > 1 and not body_id:
                    print("🔍 Multiple parts detected, showing part selector...")

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
                                         from_onshape=True)

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
                                         }), 400

                print(f"Auto-selected face: {face_id} from part: {part_name_from_body}")

            except Exception as e:
                print(f"Error in face detection: {str(e)}")
                return jsonify({
                    'error': 'Face detection failed',
                    'message': str(e)
                }), 400
        else:
            # face_id was provided (e.g., from element panel), but we need to fetch the face normal
            face_normal, auto_selected_body_id, part_name_from_body = fetch_face_normal_and_body(
                client, document_id, workspace_id, element_id, face_id, body_id
            )

        # Fetch DXF from Onshape
        # Use body_id from URL parameter if provided, otherwise use the one from auto-selection
        export_body_id = body_id if body_id else auto_selected_body_id
        print(f"Exporting with body_id: {export_body_id} (from {'URL param' if body_id else 'auto-selection'})")

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
        
        print(f"📄 DXF content received: {len(dxf_content)} bytes")

        # Generate filename: try to combine document name + part name
        doc_name = None

        # Try to get document name (optional, may fail with 404)
        try:
            print("📝 Attempting to fetch document name...")
            doc_info = client.get_document_info(document_id)
            if doc_info:
                doc_name = doc_info.get('name')
                print(f"   ✅ Got document name: {doc_name}")
            else:
                print(f"   ⚠️  Document API returned None")
        except Exception as e:
            print(f"   ⚠️  Document API failed (will use part name only): {e}")

        # Build filename from whatever we have
        suggested_filename = generate_onshape_filename(doc_name, part_name_from_body)
        print(f"✅ Generated filename: {suggested_filename}.nc")

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

        print(f"✅ DXF imported from Onshape: {dxf_filename}")
        print(f"📂 Saved to: {dxf_path}")
        print(f"📏 File size on disk: {os.path.getsize(dxf_path)} bytes")

        # Register DXF file with token manager for secure access
        dxf_token = file_token_manager.register_file(dxf_path, f"{suggested_filename}.dxf")
        print(f"🔗 Will be served at: /uploads/{dxf_token[:16]}...")

        # Render main page with DXF auto-loaded
        # The frontend will detect the dxf_file parameter and auto-upload it
        return render_template('index.html',
                             dxf_file=dxf_token,  # Pass token instead of filename
                             from_onshape=True,
                             document_id=document_id,
                             face_id=face_id,
                             suggested_filename=suggested_filename or '')
        
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
        print(f"\n💾 Onshape Save DXF request: {request.url}")
        print(f"   Method: {request.method}")

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

        print(f"Onshape params: doc={document_id}, workspace={workspace_id}, element={element_id}, face={face_id}, body={body_id}")

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
            print("No face ID, auto-selecting top face...")
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
                print(f"Error in face detection: {str(e)}")
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
        print(f"Exporting DXF with body_id: {export_body_id}")

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

        print(f"📄 DXF exported: {len(dxf_content)} bytes")

        # Generate filename with timestamp
        doc_name = None
        try:
            doc_info = client.get_document_info(document_id)
            if doc_info:
                doc_name = doc_info.get('name')
                print(f"📝 Document name: {doc_name}")
        except Exception as e:
            print(f"⚠️  Could not get document name: {e}")

        base_filename = generate_onshape_filename(doc_name, part_name_from_body)

        # Add timestamp
        pacific_time = datetime.now(ZoneInfo("America/Los_Angeles"))
        timestamp = pacific_time.strftime("%Y%m%d_%H%M%S")
        dxf_filename = f"{base_filename}_{timestamp}.dxf"

        print(f"✅ Generated filename: {dxf_filename}")

        # Save DXF to temp file
        temp_dxf = tempfile.NamedTemporaryFile(
            suffix='.dxf',
            dir=OUTPUT_FOLDER,  # Use OUTPUT_FOLDER so it's accessible for upload
            delete=False
        )
        temp_dxf.write(dxf_content)
        temp_dxf.close()

        dxf_path = temp_dxf.name
        print(f"💾 Saved temp DXF: {dxf_path}")

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

        print("📤 Uploading to Google Drive...")
        result = uploader.upload_file(dxf_path, dxf_filename)

        # Clean up temp file
        try:
            os.unlink(dxf_path)
        except:
            pass

        if result and result.get('success'):
            print(f"✅ Upload successful: {result.get('web_link')}")
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
        print(f"❌ Error in save-dxf: {str(e)}")
        traceback.print_exc()
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
    try:
        shutil.rmtree(TEMP_DIR)
    except:
        pass

atexit.register(cleanup)

if __name__ == '__main__':
    # Get port from environment variable (Railway) or default to 6238 for local dev
    port = int(os.environ.get('PORT', 6238))
    
    print("="*70)
    print("PenguinCAM - FRC Team 6238")
    print("="*70)
    print(f"\nPost-processor script: {POST_PROCESSOR}")
    print(f"Temporary directory: {TEMP_DIR}")
    print("\n🚀 Starting server...")
    print(f"📂 Server will run on port: {port}")
    print("\n⚠️  Press Ctrl+C to stop the server\n")
    print("="*70)
    
    # Disable debug mode in production
    debug_mode = os.environ.get('FLASK_ENV') != 'production'
    app.run(debug=debug_mode, host='0.0.0.0', port=port)

#!/usr/bin/env python3
"""
PenguinCAM - FRC Team 6238 CAM Tool
A Flask-based web interface for generating G-code from DXF files
"""

from flask import Flask, render_template, request, jsonify, send_file
from werkzeug.middleware.proxy_fix import ProxyFix
import os
import sys
import subprocess
import tempfile
import shutil
from pathlib import Path
import json
import secrets

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

# Import OnShape integration (optional - will work without it)
try:
    from onshape_integration import get_onshape_client, session_manager
    ONSHAPE_AVAILABLE = True
except ImportError:
    ONSHAPE_AVAILABLE = False
    print("⚠️  OnShape integration not available")

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB max file size

# Trust proxy headers (Railway, nginx, etc.)
# This tells Flask it's behind HTTPS even if internal requests are HTTP
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

# Set secret key for session management (required by auth and OnShape integration)
if not app.secret_key:
    app.secret_key = secrets.token_hex(32)

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

# Directory for temporary files
TEMP_DIR = tempfile.mkdtemp()
UPLOAD_FOLDER = os.path.join(TEMP_DIR, 'uploads')
OUTPUT_FOLDER = os.path.join(TEMP_DIR, 'outputs')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

# Path to the post-processor script (assumed to be in same directory)
SCRIPT_DIR = Path(__file__).parent
POST_PROCESSOR = SCRIPT_DIR / 'frc_cam_postprocessor.py'

@app.route('/')
@auth.require_auth
def index():
    """Render the main GUI page"""
    return render_template('index.html')

@app.route('/process', methods=['POST'])
@auth.require_auth
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
        material = request.form.get('material', "polycarb")
        if material == 'aluminum':
            spindle_speed = 24000
            feedrate = 45
            plungerate = 15
        elif material == 'plywood':
            spindle_speed = 24000
            feedrate = 75 
            plungerate = 25
        else:  # polycarb
            spindle_speed = 24000
            feedrate = 75
            plungerate = 25


        thickness = float(request.form.get('thickness', 0.25))
        tool_diameter = float(request.form.get('tool_diameter', 0.157))
        sacrifice_depth = float(request.form.get('sacrifice_depth', 0.02))
        tabs = int(request.form.get('tabs', 4))
        drill_screws = request.form.get('drill_screws', 'false') == 'true'
        origin_corner = request.form.get('origin_corner', 'bottom-left')
        rotation = int(request.form.get('rotation', 0))
        
        # Save uploaded file
        input_path = os.path.join(UPLOAD_FOLDER, 'input.dxf')
        file.save(input_path)
        
        # Generate output path
        output_filename = Path(file.filename).stem + '.gcode'
        output_path = os.path.join(OUTPUT_FOLDER, output_filename)
        
        # Build command
        cmd = [
            sys.executable,
            str(POST_PROCESSOR),
            input_path,
            output_path,
            '--thickness', str(thickness),
            '--tool-diameter', str(tool_diameter),
            '--sacrifice-depth', str(sacrifice_depth),
            '--tabs', str(tabs),
            '--origin-corner', origin_corner,
            '--rotation', str(rotation),
            '--spindle-speed', str(spindle_speed),
            '--feed-rate', str(feedrate),
            '--plunge-rate', str(plungerate),
        ]
        
        if drill_screws:
            cmd.append('--drill-screws')
        
        # Run post-processor
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30
        )

        print(result.stderr)
        
        if result.returncode != 0:
            return jsonify({
                'error': 'Post-processor failed',
                'details': result.stderr
            }), 500
        
        # Read generated G-code
        with open(output_path, 'r') as f:
            gcode_content = f.read()
        
        # Parse console output for statistics
        console_output = result.stdout
        
        return jsonify({
            'success': True,
            'filename': output_filename,
            'gcode': gcode_content,
            'console': console_output,
            'parameters': {
                'thickness': thickness,
                'tool_diameter': tool_diameter,
                'sacrifice_depth': sacrifice_depth,
                'tabs': tabs,
                'drill_screws': drill_screws,
                'origin_corner': origin_corner,
                'rotation': rotation
            }
        })
        
    except ValueError as e:
        return jsonify({'error': f'Invalid parameter value: {str(e)}'}), 400
    except subprocess.TimeoutExpired:
        return jsonify({'error': 'Processing timeout - file too complex'}), 500
    except Exception as e:
        return jsonify({'error': f'Unexpected error: {str(e)}'}), 500

@app.route('/download/<filename>')
@auth.require_auth
def download_file(filename):
    """Download generated G-code file"""
    try:
        file_path = os.path.join(OUTPUT_FOLDER, filename)
        if not os.path.exists(file_path):
            return jsonify({'error': 'File not found'}), 404
        
        return send_file(
            file_path,
            as_attachment=True,
            download_name=filename,
            mimetype='text/plain'
        )
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/drive/status')
@auth.require_auth
def drive_status():
    """Check if Google Drive integration is available and configured"""
    if not GOOGLE_DRIVE_AVAILABLE:
        return jsonify({
            'available': False,
            'message': 'Google Drive dependencies not installed'
        })
    
    # Check if user is authenticated and has Drive access
    if AUTH_AVAILABLE and auth.is_enabled():
        creds = auth.get_credentials()
        if not creds:
            return jsonify({
                'available': True,
                'configured': False,
                'message': 'Please log in to connect Google Drive'
            })
        
        return jsonify({
            'available': True,
            'configured': True,
            'message': 'Google Drive connected'
        })
    else:
        # Auth disabled, Drive not available
        return jsonify({
            'available': True,
            'configured': False,
            'message': 'Google Drive not configured - see GOOGLE_DRIVE_SETUP.md'
        })

@app.route('/drive/upload/<filename>', methods=['POST'])
@auth.require_auth
def upload_to_drive(filename):
    """Upload a G-code file to Google Drive"""
    if not GOOGLE_DRIVE_AVAILABLE:
        return jsonify({
            'success': False,
            'message': 'Google Drive integration not available'
        }), 400
    
    try:
        file_path = os.path.join(OUTPUT_FOLDER, filename)
        if not os.path.exists(file_path):
            return jsonify({
                'success': False,
                'message': 'File not found'
            }), 404
        
        # Get credentials from session
        creds = None
        if AUTH_AVAILABLE and auth.is_enabled():
            creds = auth.get_credentials()
            if not creds:
                return jsonify({
                    'success': False,
                    'message': 'Not authenticated with Google Drive'
                }), 401
        
        # Create uploader with credentials
        uploader = GoogleDriveUploader(credentials=creds)
        
        if not uploader.authenticate():
            return jsonify({
                'success': False,
                'message': 'Failed to authenticate with Google Drive'
            }), 500
        
        # Upload the file
        result = uploader.upload_file(file_path, filename)
        
        if result:
            return jsonify({
                'success': True,
                'message': 'File uploaded to Google Drive',
                'file_id': result.get('id'),
                'web_view_link': result.get('webViewLink')
            })
        else:
            return jsonify({
                'success': False,
                'message': 'Upload failed'
            }), 500
            
    except Exception as e:
        return jsonify({
            'success': False,
            'message': f'Upload error: {str(e)}'
        }), 500

# ============================================================================
# OnShape Integration Routes
# ============================================================================

@app.route('/onshape/auth')
@auth.require_auth
def onshape_auth():
    """Start OnShape OAuth flow"""
    if not ONSHAPE_AVAILABLE:
        return jsonify({
            'error': 'OnShape integration not available'
        }), 400
    
    try:
        client = get_onshape_client()
        
        # Generate state for CSRF protection
        import secrets
        state = secrets.token_urlsafe(32)
        
        # Store state in session for verification
        from flask import session
        session['onshape_oauth_state'] = state
        
        # Get authorization URL
        auth_url = client.get_authorization_url(state=state)
        
        # Redirect user to OnShape for authorization
        from flask import redirect
        return redirect(auth_url)
        
    except Exception as e:
        return jsonify({'error': f'OAuth initialization failed: {str(e)}'}), 500

@app.route('/onshape/oauth/callback')
def onshape_oauth_callback():
    """Handle OnShape OAuth callback"""
    if not ONSHAPE_AVAILABLE:
        return "OnShape integration not available", 400
    
    try:
        from flask import session, redirect
        
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
        user_id = session.get('user_email', 'default_user')
        session_manager.create_session(user_id, client)
        session['onshape_authenticated'] = True
        
        # Clean up OAuth state
        session.pop('onshape_oauth_state', None)
        
        # Redirect to main page with success message
        return redirect('/?onshape_connected=true')
        
    except Exception as e:
        return f"OAuth callback error: {str(e)}", 500

@app.route('/onshape/status')
@auth.require_auth
def onshape_status():
    """Check OnShape connection status"""
    if not ONSHAPE_AVAILABLE:
        return jsonify({
            'available': False,
            'connected': False,
            'message': 'OnShape integration not installed'
        })
    
    try:
        from flask import session
        
        user_id = session.get('user_email', 'default_user')
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
                'message': 'Not connected to OnShape'
            })
            
    except Exception as e:
        return jsonify({
            'available': True,
            'connected': False,
            'message': f'Error: {str(e)}'
        })

@app.route('/onshape/list-faces', methods=['GET'])
@auth.require_auth
def onshape_list_faces():
    """
    List all faces in a Part Studio element
    For debugging and exploring the OnShape API
    """
    if not ONSHAPE_AVAILABLE:
        return jsonify({'error': 'OnShape integration not available'}), 400
    
    try:
        from flask import session
        
        # Get parameters
        document_id = request.args.get('documentId') or request.args.get('did')
        workspace_id = request.args.get('workspaceId') or request.args.get('wid')
        element_id = request.args.get('elementId') or request.args.get('eid')
        
        if not all([document_id, workspace_id, element_id]):
            return jsonify({
                'error': 'Missing required parameters',
                'required': ['documentId', 'workspaceId', 'elementId']
            }), 400
        
        # Get OnShape client for this user
        user_id = session.get('user_email', 'default_user')
        client = session_manager.get_client(user_id)
        
        if not client:
            return jsonify({
                'error': 'Not authenticated with OnShape',
                'auth_url': '/onshape/auth'
            }), 401
        
        # List faces
        faces_data = client.list_faces(document_id, workspace_id, element_id)
        
        if faces_data:
            return jsonify({
                'success': True,
                'data': faces_data
            })
        else:
            return jsonify({
                'error': 'Failed to list faces',
                'message': 'Check console for details'
            }), 500
            
    except Exception as e:
        return jsonify({
            'error': f'Failed: {str(e)}'
        }), 500

@app.route('/onshape/body-faces', methods=['GET'])
@auth.require_auth
def onshape_body_faces():
    """
    Get all faces for all bodies (or a specific body) in an element
    """
    if not ONSHAPE_AVAILABLE:
        return jsonify({'error': 'OnShape integration not available'}), 400
    
    try:
        from flask import session
        
        # Get parameters
        document_id = request.args.get('documentId') or request.args.get('did')
        workspace_id = request.args.get('workspaceId') or request.args.get('wid')
        element_id = request.args.get('elementId') or request.args.get('eid')
        body_id = request.args.get('bodyId') or request.args.get('bid')  # Optional
        
        if not all([document_id, workspace_id, element_id]):
            return jsonify({
                'error': 'Missing required parameters',
                'required': ['documentId', 'workspaceId', 'elementId'],
                'optional': ['bodyId']
            }), 400
        
        # Get OnShape client for this user
        user_id = session.get('user_email', 'default_user')
        client = session_manager.get_client(user_id)
        
        if not client:
            return jsonify({
                'error': 'Not authenticated with OnShape',
                'auth_url': '/onshape/auth'
            }), 401
        
        # Get faces for bodies
        faces_by_body = client.get_body_faces(document_id, workspace_id, element_id, body_id)
        
        if faces_by_body:
            return jsonify({
                'success': True,
                'bodies': faces_by_body
            })
        else:
            return jsonify({
                'error': 'Failed to get faces',
                'message': 'Check console for details'
            }), 500
            
    except Exception as e:
        return jsonify({
            'error': f'Failed: {str(e)}'
        }), 500

@app.route('/onshape/import', methods=['GET', 'POST'])
@auth.require_auth
def onshape_import():
    """
    Import a DXF from OnShape
    Accepts parameters from OnShape extension or direct URL
    """
    if not ONSHAPE_AVAILABLE:
        return jsonify({'error': 'OnShape integration not available'}), 400
    
    try:
        from flask import session
        
        # Get parameters (either from query string or JSON body)
        if request.method == 'POST':
            params = request.json or {}
        else:
            params = request.args.to_dict()
        
        document_id = params.get('documentId') or params.get('did')
        workspace_id = params.get('workspaceId') or params.get('wid')
        element_id = params.get('elementId') or params.get('eid')
        face_id = params.get('faceId') or params.get('fid')
        
        if not all([document_id, workspace_id, element_id]):
            return jsonify({
                'error': 'Missing required parameters',
                'required': ['documentId', 'workspaceId', 'elementId']
            }), 400
        
        # Get OnShape client for this user
        user_id = session.get('user_email', 'default_user')
        client = session_manager.get_client(user_id)
        
        if not client:
            # User needs to authenticate with OnShape first
            return jsonify({
                'error': 'Not authenticated with OnShape',
                'auth_url': '/onshape/auth'
            }), 401
        
        # If no face_id provided, auto-select the top face
        if not face_id:
            print("No face ID provided, auto-selecting top face...")
            face_id = client.auto_select_top_face(document_id, workspace_id, element_id)
            
            if not face_id:
                return jsonify({
                    'error': 'Could not auto-select top face',
                    'message': 'No horizontal plane faces found. Please specify a faceId manually.'
                }), 400
            
            print(f"Auto-selected face: {face_id}")
        
        # Fetch DXF from OnShape
        dxf_content = client.export_face_to_dxf(
            document_id, workspace_id, element_id, face_id
        )
        
        if not dxf_content:
            return jsonify({
                'error': 'Failed to export DXF from OnShape',
                'message': 'Check that the face ID is valid and you have access to the document'
            }), 500
        
        # Save DXF to temp file in uploads folder
        import tempfile
        temp_dxf = tempfile.NamedTemporaryFile(
            suffix='.dxf',
            dir=UPLOAD_FOLDER,
            delete=False
        )
        temp_dxf.write(dxf_content)
        temp_dxf.close()
        
        dxf_filename = os.path.basename(temp_dxf.name)
        dxf_path = temp_dxf.name
        
        # Automatically process through PenguinCAM
        try:
            print(f"\nProcessing {dxf_filename} through PenguinCAM...")
            
            # Generate output filename
            base_name = os.path.splitext(dxf_filename)[0]
            output_filename = f"{base_name}_onshape.nc"
            output_path = os.path.join(OUTPUT_FOLDER, output_filename)
            
            # Run post-processor (uses positional args: input_dxf output_gcode)
            result = subprocess.run(
                [POST_PROCESSOR, dxf_path, output_path],
                capture_output=True,
                text=True,
                timeout=30
            )
            
            if result.returncode == 0:
                print(f"✅ G-code generated: {output_filename}")
                print(f"📂 Full path: {output_path}")
                
                # Return success with both DXF and G-code info
                return jsonify({
                    'success': True,
                    'dxf_filename': dxf_filename,
                    'gcode_filename': output_filename,
                    'output_path': output_path,
                    'selected_face_id': face_id,
                    'message': 'DXF imported from OnShape and processed to G-code',
                    'document_id': document_id,
                    'download_url': f'/download/{output_filename}'
                })
            else:
                # Processing failed, but DXF is saved
                print(f"⚠️  Processing failed: {result.stderr}")
                return jsonify({
                    'success': True,
                    'dxf_filename': dxf_filename,
                    'selected_face_id': face_id,
                    'message': 'DXF imported but processing failed',
                    'document_id': document_id,
                    'error_details': result.stderr
                })
                
        except subprocess.TimeoutExpired:
            return jsonify({
                'success': True,
                'dxf_filename': dxf_filename,
                'selected_face_id': face_id,
                'message': 'DXF imported but processing timed out',
                'document_id': document_id
            })
        except Exception as e:
            return jsonify({
                'success': True,
                'dxf_filename': dxf_filename,
                'selected_face_id': face_id,
                'message': f'DXF imported but processing error: {str(e)}',
                'document_id': document_id
            })
        
    except Exception as e:
        return jsonify({
            'error': f'Import failed: {str(e)}'
        }), 500

def cleanup():
    """Clean up temporary files on shutdown"""
    try:
        shutil.rmtree(TEMP_DIR)
    except:
        pass

import atexit
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

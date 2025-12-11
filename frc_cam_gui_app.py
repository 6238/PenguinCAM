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
        suggested_filename = request.form.get('suggested_filename', '')
        
        # Save uploaded file
        input_path = os.path.join(UPLOAD_FOLDER, 'input.dxf')
        file.save(input_path)
        
        # Generate output filename
        if suggested_filename:
            # Use OnShape-derived name
            output_filename = suggested_filename + '.nc'
            print(f"📝 Using OnShape filename: {output_filename}")
        else:
            # Use DXF filename
            output_filename = Path(file.filename).stem + '.nc'
            print(f"📝 Using DXF filename: {output_filename}")
        
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
        
        print(f"🚀 Running post-processor...")
        print(f"   Command: {' '.join(cmd)}")
        print(f"   Output will be: {output_path}")
        
        # Run post-processor
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30
        )

        print(result.stderr)

        print(f"📊 Post-processor finished:")
        print(f"   Return code: {result.returncode}")
        print(f"   Stdout length: {len(result.stdout)} bytes")
        print(f"   Stderr length: {len(result.stderr)} bytes")
        
        if result.returncode != 0:
            print(f"❌ Post-processor failed!")
            print(f"   Stderr: {result.stderr}")
            return jsonify({
                'error': 'Post-processor failed',
                'details': result.stderr
            }), 500
        
        # Check if output file was created
        if not os.path.exists(output_path):
            print(f"❌ Output file not created: {output_path}")
            return jsonify({
                'error': 'Post-processor did not create output file',
                'details': result.stdout + '\n\n' + result.stderr
            }), 500
        
        print(f"✅ Output file created: {os.path.getsize(output_path)} bytes")
        
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

@app.route('/uploads/<filename>')
def serve_upload(filename):
    """Serve uploaded DXF files for frontend preview"""
    from flask import send_from_directory
    try:
        return send_from_directory(UPLOAD_FOLDER, filename)
    except Exception as e:
        return jsonify({'error': f'File not found: {str(e)}'}), 404

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
    print(f"📤 Drive upload requested for: {filename}")
    
    if not GOOGLE_DRIVE_AVAILABLE:
        print("❌ Google Drive integration not available")
        return jsonify({
            'success': False,
            'message': 'Google Drive integration not available'
        }), 400
    
    try:
        file_path = os.path.join(OUTPUT_FOLDER, filename)
        print(f"📂 Looking for file at: {file_path}")
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
        # Upload the file
        result = uploader.upload_file(file_path, filename)
        
        print(f"📤 Upload result: {result}")
        
        if result and result.get('success'):
            print(f"✅ Upload successful: {result.get('web_link')}")
            return jsonify({
                'success': True,
                'message': 'File uploaded to Google Drive',
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
        
        # Check if there was a pending import
        pending_import = session.pop('pending_onshape_import', None)
        
        if pending_import:
            # Redirect back to import with original parameters
            from urllib.parse import urlencode
            params = urlencode({k: v for k, v in pending_import.items() if v})
            return redirect(f'/onshape/import?{params}')
        
        # Otherwise redirect to main page with success message
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
        
        # Get OnShape server and user info that IS being sent
        onshape_server = params.get('server', 'https://cad.onshape.com')
        onshape_userid = params.get('userId')
        
        print(f"OnShape params received: {params}")
        
        # WORKAROUND: If params have placeholder strings, we can't proceed
        if (document_id and ('${' in str(document_id) or document_id.startswith('$'))):
            print("❌ OnShape variable substitution failed!")
            print(f"Received literal: documentId={document_id}")
            
            # Show helpful error page
            from flask import render_template
            return render_template('index.html',
                                 error_message='OnShape integration error: Variable substitution not working. Please contact support or use manual DXF upload.',
                                 debug_info={
                                     'issue': 'OnShape extension not substituting variables',
                                     'received_params': str(params),
                                     'workaround': 'Export DXF manually from OnShape and upload it here'
                                 }), 400
        
        if not all([document_id, workspace_id, element_id]):
            return jsonify({
                'error': 'Missing required parameters',
                'required': ['documentId', 'workspaceId', 'elementId'],
                'received': params,
                'help': 'OnShape variable substitution not working. Check extension configuration or use manual DXF upload.'
            }), 400
        
        # Get OnShape client for this user
        user_id = session.get('user_email', 'default_user')
        client = session_manager.get_client(user_id)
        
        if not client:
            # Store import parameters in session before redirecting to OAuth
            session['pending_onshape_import'] = {
                'documentId': document_id,
                'workspaceId': workspace_id,
                'elementId': element_id,
                'faceId': face_id
            }
            
            # Redirect to OnShape OAuth
            from flask import redirect
            return redirect('/onshape/auth')
        
        # If no face_id provided, auto-select the top face
        if not face_id:
            print("No face ID provided, auto-selecting top face...")
            
            try:
                # First, try to list all faces for debugging
                faces_data = client.list_faces(document_id, workspace_id, element_id)
                print(f"Available faces: {faces_data}")
                
                face_id = client.auto_select_top_face(document_id, workspace_id, element_id)
                
                if not face_id:
                    # Provide helpful error with face list
                    error_msg = 'No horizontal plane faces found. '
                    if faces_data:
                        face_count = len(faces_data.get('faces', []))
                        error_msg += f'Found {face_count} faces total. '
                    error_msg += 'Try selecting a face manually in OnShape.'
                    
                    # Render error page instead of JSON
                    from flask import render_template
                    return render_template('index.html',
                                         error_message=error_msg,
                                         from_onshape=True,
                                         debug_info={
                                             'documentId': document_id,
                                             'workspaceId': workspace_id,
                                             'elementId': element_id,
                                             'faces_found': face_count if faces_data else 0
                                         }), 400
                
                print(f"Auto-selected face: {face_id}")
                
            except Exception as e:
                print(f"Error in face detection: {str(e)}")
                return jsonify({
                    'error': 'Face detection failed',
                    'message': str(e),
                    'debug_url': f'/onshape/list-faces?documentId={document_id}&workspaceId={workspace_id}&elementId={element_id}'
                }), 400
        
        # Fetch DXF from OnShape
        dxf_content = client.export_face_to_dxf(
            document_id, workspace_id, element_id, face_id
        )
        
        if not dxf_content:
            return jsonify({
                'error': 'Failed to export DXF from OnShape',
                'message': 'Check that the face ID is valid and you have access to the document'
            }), 500
        
        print(f"📄 DXF content received: {len(dxf_content)} bytes")
        
        # Fetch document and element names for better filename
        suggested_filename = None
        try:
            print("📝 Fetching document and element names...")
            print(f"   Document ID: {document_id}")
            print(f"   Workspace ID: {workspace_id}")
            print(f"   Element ID: {element_id}")
            
            doc_info = client.get_document_info(document_id)
            print(f"   Doc info result: {doc_info is not None}")
            if doc_info:
                print(f"   Document: {doc_info.get('name', 'N/A')}")
            
            element_info = client.get_element_info(document_id, workspace_id, element_id)
            print(f"   Element info result: {element_info is not None}")
            if element_info:
                print(f"   Element: {element_info.get('name', 'N/A')}")
            
            if doc_info or element_info:
                # Use whatever names we got (prefer both, fallback to one)
                doc_name = doc_info.get('name', 'OnShape_Doc') if doc_info else 'OnShape_Doc'
                element_name = element_info.get('name', 'Part_Studio') if element_info else 'Part_Studio'

                # Clean names for filename (remove spaces, special chars)
                import re
                doc_clean = re.sub(r'[^\w\s-]', '', doc_name).strip().replace(' ', '_')
                elem_clean = re.sub(r'[^\w\s-]', '', element_name).strip().replace(' ', '_')

                # Limit length
                doc_clean = doc_clean[:50]
                elem_clean = elem_clean[:50]

                # If both are default/fallback names, use timestamp instead
                if doc_clean == 'OnShape_Doc' and elem_clean == 'Part_Studio':
                    from datetime import datetime
                    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
                    suggested_filename = f"OnShape_Part_{timestamp}"
                    print(f"⚠️  Using timestamp (no names available): {suggested_filename}.nc")
                else:
                    # Use element name only if doc name is fallback
                    if doc_clean == 'OnShape_Doc':
                        suggested_filename = elem_clean
                        print(f"✅ Suggested filename (element only): {suggested_filename}.nc")
                    else:
                        suggested_filename = f"{doc_clean}_{elem_clean}"
                        print(f"✅ Suggested filename: {suggested_filename}.nc")
            else:
                # Generate timestamp fallback (both API calls failed)
                from datetime import datetime
                timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
                suggested_filename = f"OnShape_Part_{timestamp}"
                print(f"⚠️  Could not fetch document/element names - using timestamp: {suggested_filename}.nc")
        except Exception as e:
            # Generate timestamp fallback on error
            from datetime import datetime
            timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            suggested_filename = f"OnShape_Part_{timestamp}"
            print(f"⚠️  Error fetching names: {type(e).__name__}: {e}")
            print(f"   Using timestamp fallback: {suggested_filename}.nc")
            import traceback
            traceback.print_exc()
        
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
        
        print(f"✅ DXF imported from OnShape: {dxf_filename}")
        print(f"📂 Saved to: {dxf_path}")
        print(f"📏 File size on disk: {os.path.getsize(dxf_path)} bytes")
        print(f"🔗 Will be served at: /uploads/{dxf_filename}")
        
        # Render main page with DXF auto-loaded
        # The frontend will detect the dxf_file parameter and auto-upload it
        from flask import render_template
        return render_template('index.html', 
                             dxf_file=dxf_filename,
                             from_onshape=True,
                             document_id=document_id,
                             face_id=face_id,
                             suggested_filename=suggested_filename or '')
        
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

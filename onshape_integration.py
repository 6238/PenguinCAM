"""
Onshape Integration for PenguinCAM
Handles OAuth authentication and DXF export from Onshape
"""

import os
import sys
import json
import requests
import base64
from urllib.parse import urlencode, parse_qs
from datetime import datetime, timedelta
from flask import session
import logging

# Configure logging for Vercel
logging.basicConfig(
    level=logging.INFO,
    format='%(message)s',
    stream=sys.stderr,
    force=True
)
logger = logging.getLogger(__name__)

# Logging helper for Vercel/serverless environments
def log(*args, **kwargs):
    """Log to stderr using Python logging module for better Vercel compatibility"""
    message = ' '.join(str(arg) for arg in args)
    logger.info(message)

class OnshapeClient:
    """Client for interacting with Onshape API"""
    
    BASE_URL = "https://cad.onshape.com"
    API_BASE = "https://cad.onshape.com/api/v13"
    
    def __init__(self):
        self.config = self._load_config()
        self.access_token = None
        self.refresh_token = None
        self.token_expires = None
    
    def _load_config(self):
        """Load Onshape OAuth configuration, prioritizing environment variables"""
        # Try to load from file first
        config_file = 'onshape_config.json'
        config = {}
        
        if os.path.exists(config_file):
            with open(config_file, 'r') as f:
                config = json.load(f)
        
        # Override with environment variables (these take precedence)
        config['client_id'] = os.environ.get('ONSHAPE_CLIENT_ID', config.get('client_id', 'VKDKRMPYLAC3PE6YNHRWFGRTW37ZFWTG2IDE5UI='))
        config['client_secret'] = os.environ.get('ONSHAPE_CLIENT_SECRET', config.get('client_secret'))
        
        # Set defaults for other fields if not present
        if 'redirect_uri' not in config:
            # Determine base URL from environment or default to localhost
            base_url = os.environ.get('BASE_URL', 'http://localhost:6238')
            config['redirect_uri'] = f"{base_url}/onshape/oauth/callback"
        
        if 'scopes' not in config:
            config['scopes'] = 'OAuth2Read OAuth2ReadPII'
        
        return config
    
    def _save_config(self):
        """Save configuration"""
        with open('onshape_config.json', 'w') as f:
            json.dump(self.config, f, indent=2)
    
    def get_authorization_url(self, state=None):
        """
        Get the OAuth authorization URL to redirect user to
        
        Args:
            state: Optional state parameter for CSRF protection
            
        Returns:
            Authorization URL string
        """
        params = {
            'response_type': 'code',
            'client_id': self.config['client_id'],
            'redirect_uri': self.config['redirect_uri'],
            'scope': self.config['scopes'],
        }
        
        if state:
            params['state'] = state
        
        auth_url = f"{self.BASE_URL}/oauth/authorize"
        return f"{auth_url}?{urlencode(params)}"
    
    def exchange_code_for_token(self, code):
        """
        Exchange authorization code for access token
        
        Args:
            code: Authorization code from OAuth callback
            
        Returns:
            dict with token info or None if failed
        """
        if not self.config.get('client_secret'):
            raise ValueError("Onshape client_secret not configured")
        
        # Create Basic Auth header
        credentials = f"{self.config['client_id']}:{self.config['client_secret']}"
        b64_credentials = base64.b64encode(credentials.encode()).decode()
        
        headers = {
            'Authorization': f'Basic {b64_credentials}',
            'Content-Type': 'application/x-www-form-urlencoded'
        }
        
        data = {
            'grant_type': 'authorization_code',
            'code': code,
            'redirect_uri': self.config['redirect_uri']
        }
        
        try:
            response = requests.post(
                f"{self.BASE_URL}/oauth/token",
                headers=headers,
                data=data
            )
            
            if response.status_code == 200:
                token_data = response.json()
                
                # Store tokens
                self.access_token = token_data.get('access_token')
                self.refresh_token = token_data.get('refresh_token')
                
                # Calculate expiration
                expires_in = token_data.get('expires_in', 3600)
                self.token_expires = datetime.now() + timedelta(seconds=expires_in)
                
                return token_data
            else:
                log(f"Token exchange failed: {response.status_code} - {response.text}")
                return None
                
        except Exception as e:
            log(f"Error exchanging code for token: {e}")
            return None
    
    def refresh_access_token(self):
        """Refresh the access token using refresh token"""
        if not self.refresh_token:
            return False
        
        credentials = f"{self.config['client_id']}:{self.config['client_secret']}"
        b64_credentials = base64.b64encode(credentials.encode()).decode()
        
        headers = {
            'Authorization': f'Basic {b64_credentials}',
            'Content-Type': 'application/x-www-form-urlencoded'
        }
        
        data = {
            'grant_type': 'refresh_token',
            'refresh_token': self.refresh_token
        }
        
        try:
            response = requests.post(
                f"{self.BASE_URL}/oauth/token",
                headers=headers,
                data=data
            )
            
            if response.status_code == 200:
                token_data = response.json()
                self.access_token = token_data.get('access_token')
                expires_in = token_data.get('expires_in', 3600)
                self.token_expires = datetime.now() + timedelta(seconds=expires_in)
                return True
            else:
                return False
                
        except Exception as e:
            log(f"Error refreshing token: {e}")
            return False
    
    def _ensure_valid_token(self):
        """Ensure we have a valid access token"""
        if not self.access_token:
            raise ValueError("No access token. User must authenticate first.")
        
        # Refresh if expired or about to expire (within 5 minutes)
        if self.token_expires and datetime.now() >= self.token_expires - timedelta(minutes=5):
            if not self.refresh_access_token():
                raise ValueError("Token expired and refresh failed")
    
    def _make_api_request(self, method, endpoint, **kwargs):
        """
        Make an authenticated API request to Onshape
        
        Args:
            method: HTTP method (GET, POST, etc.)
            endpoint: API endpoint (e.g., '/documents/d/...')
            **kwargs: Additional arguments for requests
            
        Returns:
            Response object
        """
        self._ensure_valid_token()
        
        url = f"{self.API_BASE}{endpoint}"
        
        headers = kwargs.pop('headers', {})
        headers['Authorization'] = f'Bearer {self.access_token}'
        
        return requests.request(method, url, headers=headers, **kwargs)
    
    def get_user_info(self):
        """Get information about the authenticated user"""
        try:
            response = self._make_api_request('GET', '/users/sessioninfo')
            if response.status_code == 200:
                return response.json()
            return None
        except Exception as e:
            log(f"Error getting user info: {e}")
            return None

    def get_user_session_info(self):
        """
        Get detailed session info for the authenticated user

        Returns:
            dict with user session info including name, email, etc.
        """
        try:
            log("   Fetching user session info...")
            response = self._make_api_request('GET', '/users/sessioninfo')
            if response.status_code == 200:
                user_info = response.json()
                log(f"   ✅ User: {user_info.get('name', 'Unknown')}")
                return user_info
            else:
                log(f"   ❌ Failed to get session info: HTTP {response.status_code}")
                return None
        except Exception as e:
            log(f"   ❌ Error getting session info: {e}")
            import traceback
            log(traceback.format_exc())
            return None

    def get_companies(self):
        """
        Get list of companies/teams the user belongs to

        Returns:
            list of company dicts
        """
        try:
            log("   Fetching companies...")
            response = self._make_api_request('GET', '/companies?activeOnly=true&includeAll=false')
            if response.status_code == 200:
                companies = response.json().get('items', [])
                log(f"   ✅ Found {len(companies)} companies: {[c.get('name') for c in companies]}")
                return companies
            else:
                log(f"   ❌ Failed to get companies: HTTP {response.status_code}")
                return None
        except Exception as e:
            log(f"   ❌ Error getting companies: {e}")
            import traceback
            log(traceback.format_exc())
            return None

    def get_document_company(self, document_id):
        """
        Get the company/team that owns a specific document

        Args:
            document_id: Onshape document ID

        Returns:
            dict with company info, or None if not found
        """
        try:
            log("   Determining document owner company...")

            # Get document info to find owner
            doc_info = self.get_document_info(document_id)
            if not doc_info:
                log("   ❌ Could not get document info")
                return None

            # Documents have an 'owner' field with type and id
            # type: 0 = user, 1 = company, 2 = team (I think - need to verify)
            owner_info = doc_info.get('owner', {})
            owner_type = owner_info.get('type')
            owner_id = owner_info.get('id')
            owner_name = owner_info.get('name', 'Unknown')

            log(f"   Document owner: {owner_name} (type={owner_type}, id={owner_id[:8]}...)")

            # If owner is a company/team (type 1 or 2), find it in the companies list
            if owner_type in [1, 2]:
                companies = self.get_companies()
                if companies:
                    for company in companies:
                        if company.get('id') == owner_id:
                            log(f"   ✅ Document belongs to company: {company.get('name')}")
                            return company
                    log(f"   ⚠️  Document owner company not found in user's companies")
                    return None
            else:
                log(f"   ℹ️  Document is owned by user (not a company/team)")
                return None

        except Exception as e:
            log(f"   ❌ Error getting document company: {e}")
            import traceback
            log(traceback.format_exc())
            return None
    
    def _calculate_view_matrix(self, normal):
        """
        Calculate a view matrix that looks at a face straight-on based on its normal.

        Args:
            normal: Dict with 'x', 'y', 'z' keys for the face normal vector

        Returns:
            String representing a 4x4 view matrix in Onshape format
        """
        nx = normal.get('x', 0)
        ny = normal.get('y', 0)
        nz = normal.get('z', 1)

        # Determine which axis the normal is closest to
        abs_nx, abs_ny, abs_nz = abs(nx), abs(ny), abs(nz)

        # View matrices for 6 cardinal directions (4x4 in row-major order)
        # Format: a,b,c,d,e,f,g,h,i,j,k,l,m,n,o,p representing:
        # a b c d
        # e f g h
        # i j k l
        # m n o p

        if abs_nz > abs_nx and abs_nz > abs_ny:
            # Face pointing ±Z (horizontal)
            if nz > 0:
                # Top view (looking down -Z)
                return "1,0,0,0,0,1,0,0,0,0,1,0,0,0,0,1"
            else:
                # Bottom view (looking up +Z, flip X)
                return "-1,0,0,0,0,1,0,0,0,0,-1,0,0,0,0,1"
        elif abs_ny > abs_nx:
            # Face pointing ±Y
            if ny > 0:
                # Back view (looking along -Y, rotate -90° around X)
                return "1,0,0,0,0,0,-1,0,0,1,0,0,0,0,0,1"
            else:
                # Front view (looking along +Y, rotate 90° around X)
                return "1,0,0,0,0,0,1,0,0,-1,0,0,0,0,0,1"
        else:
            # Face pointing ±X
            if nx > 0:
                # Right side view (looking along -X, rotate 90° around Y)
                return "0,0,-1,0,0,1,0,0,1,0,0,0,0,0,0,1"
            else:
                # Left side view (looking along +X, rotate -90° around Y)
                return "0,0,1,0,0,1,0,0,-1,0,0,0,0,0,0,1"

    def export_face_to_dxf(self, document_id, workspace_id, element_id, face_id, body_id=None, face_normal=None):
        """
        Export a face from a Part Studio as DXF

        Args:
            document_id: Onshape document ID (from URL: /documents/d/{did})
            workspace_id: Workspace ID (from URL: /w/{wid})
            element_id: Element ID (from URL: /e/{eid})
            face_id: The face ID (used for logging/backwards compatibility)
            body_id: The body/part ID to export (if None, uses face_id for backwards compatibility)
            face_normal: Optional dict with face normal vector {'x': ..., 'y': ..., 'z': ...}

        Returns:
            DXF file content as bytes, or None if failed
        """
        log(f"\n=== Attempting DXF export ===")
        log(f"Document: {document_id}")
        log(f"Workspace: {workspace_id}")
        log(f"Element: {element_id}")
        log(f"Face: {face_id}")
        log(f"Body: {body_id}")
        if face_normal:
            log(f"Normal: ({face_normal.get('x', 0):.3f}, {face_normal.get('y', 0):.3f}, {face_normal.get('z', 0):.3f})")
        
        # Try the internal export endpoint that Onshape's web UI uses
        log("\n[Method 1] Trying exportinternal endpoint (web UI method)...")
        endpoint = f"/documents/d/{document_id}/w/{workspace_id}/e/{element_id}/exportinternal"
        
        try:
            # For Part Studios, Onshape's "partIds" parameter actually expects face IDs, not body IDs
            # (Confusing naming by Onshape!)
            export_id = face_id  # Always use face_id for Part Studio exports
            log(f"Using face_id for export: {export_id}")

            # Calculate view matrix based on face normal (if provided)
            if face_normal:
                view_matrix = self._calculate_view_matrix(face_normal)
                log(f"Using calculated view matrix for normal ({face_normal.get('x', 0):.3f}, {face_normal.get('y', 0):.3f}, {face_normal.get('z', 0):.3f})")
            else:
                # Default to top-down view
                view_matrix = "1,0,0,0,0,1,0,0,0,0,1,0,0,0,0,1"
                log("Using default top-down view matrix")

            body = {
                "format": "DXF",
                "view": view_matrix,
                "version": "2013",
                "units": "inch",
                "flatten": "true",  # Critical for 2D export
                "includeBendCenterlines": "true",
                "includeSketches": "true",
                "splinesAsPolylines": "true",
                "triggerAutoDownload": "true",
                "storeInDocument": "false",
                "partIds": export_id  # Must be a string, not an array!
            }
            
            log(f"API endpoint: {self.API_BASE}{endpoint}")
            log(f"Request body: {json.dumps(body, indent=2)}")
            
            response = self._make_api_request('POST', endpoint, json=body)
            
            log(f"Response status: {response.status_code}")
            log(f"Response headers: {dict(response.headers)}")
            
            if response.status_code == 200:
                log(f"Success! DXF content length: {len(response.content)} bytes")
                # Check if it's actually DXF content
                content_preview = response.content[:100].decode('utf-8', errors='ignore')
                log(f"Content preview: {content_preview[:50]}...")
                return response.content
            else:
                log(f"exportinternal failed: {response.status_code}")
                log(f"Response: {response.text}")
                
        except Exception as e:
            log(f"Error with exportinternal: {e}")
            import traceback
            log(traceback.format_exc())
        
        # Fallback: Try async translations API
        log("\n[Method 2] Trying async translations API...")
        result = self.export_dxf_async(document_id, workspace_id, element_id)
        if result:
            return result
        
        # Fallback: Try POST /export endpoint
        log("\n[Method 3] Trying POST /export endpoint...")
        endpoint = f"/partstudios/d/{document_id}/w/{workspace_id}/e/{element_id}/export"
        
        try:
            body = {
                "format": "DXF",
                "version": "2013",
                "flattenAssemblies": True
            }
            
            response = self._make_api_request('POST', endpoint, json=body)
            
            if response.status_code == 200:
                log(f"Success! DXF content length: {len(response.content)} bytes")
                return response.content
            else:
                log(f"POST export failed: {response.status_code}")
                
        except Exception as e:
            log(f"Error with POST export: {e}")
        
        log("\n=== All export methods failed ===")
        return None
    
    def _export_element_to_dxf(self, document_id, workspace_id, element_id):
        """Try to export entire element as DXF"""
        endpoint = f"/partstudios/d/{document_id}/w/{workspace_id}/e/{element_id}/dxf"
        
        try:
            log(f"Exporting entire element as DXF...")
            response = self._make_api_request('GET', endpoint)
            
            log(f"Response status: {response.status_code}")
            
            if response.status_code == 200:
                log(f"Success! DXF content length: {len(response.content)} bytes")
                return response.content
            else:
                log(f"Failed: {response.status_code} - {response.text}")
                return None
                
        except Exception as e:
            log(f"Error: {e}")
            return None
    
    def start_dxf_translation(self, document_id, workspace_id, element_id):
        """
        Start an async DXF export translation
        
        Returns:
            Translation ID if successful, None otherwise
        """
        endpoint = f"/partstudios/d/{document_id}/w/{workspace_id}/e/{element_id}/translations"
        
        try:
            log(f"\nStarting DXF translation for element {element_id}")
            log(f"API endpoint: {self.API_BASE}{endpoint}")
            
            body = {
                "formatName": "DXF",
                "storeInDocument": False,  # Don't store in Onshape, just export
                "flattenAssemblies": True
            }
            
            log(f"Request body: {json.dumps(body, indent=2)}")
            
            response = self._make_api_request('POST', endpoint, json=body)
            
            log(f"Response status: {response.status_code}")
            
            if response.status_code == 200:
                data = response.json()
                translation_id = data.get('id')
                log(f"Translation started! ID: {translation_id}")
                return translation_id
            else:
                log(f"Failed to start translation: {response.status_code}")
                log(f"Response: {response.text}")
                return None
                
        except Exception as e:
            log(f"Error starting translation: {e}")
            import traceback
            log(traceback.format_exc())
            return None
    
    def check_translation_status(self, translation_id):
        """
        Check the status of a translation
        
        Returns:
            dict with 'state' and other info, or None if failed
        """
        endpoint = f"/translations/{translation_id}"
        
        try:
            response = self._make_api_request('GET', endpoint)
            
            if response.status_code == 200:
                data = response.json()
                state = data.get('requestState', 'UNKNOWN')
                log(f"Translation {translation_id}: {state}")
                return data
            else:
                log(f"Failed to check translation: {response.status_code}")
                return None
                
        except Exception as e:
            log(f"Error checking translation: {e}")
            return None
    
    def download_translation_result(self, document_id, translation_id, external_data_id):
        """
        Download the result of a completed translation
        
        Args:
            external_data_id: The ID from translation result
            
        Returns:
            File content as bytes, or None
        """
        endpoint = f"/documents/d/{document_id}/externaldata/{external_data_id}"
        
        try:
            log(f"Downloading translation result...")
            response = self._make_api_request('GET', endpoint)
            
            if response.status_code == 200:
                log(f"Downloaded {len(response.content)} bytes")
                return response.content
            else:
                log(f"Failed to download: {response.status_code}")
                log(f"Response: {response.text}")
                return None
                
        except Exception as e:
            log(f"Error downloading result: {e}")
            return None
    
    def export_dxf_async(self, document_id, workspace_id, element_id, timeout=60):
        """
        Export DXF using async translations API
        Polls until complete or timeout
        
        Returns:
            DXF content as bytes, or None
        """
        import time
        
        # Start translation
        translation_id = self.start_dxf_translation(document_id, workspace_id, element_id)
        if not translation_id:
            return None
        
        # Poll for completion
        start_time = time.time()
        while time.time() - start_time < timeout:
            status = self.check_translation_status(translation_id)
            
            if not status:
                return None
            
            state = status.get('requestState', '')
            
            if state == 'DONE':
                # Get the result URL
                result_external_data_id = status.get('resultExternalDataIds', [])
                if result_external_data_id:
                    return self.download_translation_result(
                        document_id, 
                        translation_id, 
                        result_external_data_id[0]
                    )
                else:
                    log("Translation done but no result data ID found")
                    return None
                    
            elif state in ['FAILED', 'ACTIVE']:
                log(f"Translation failed with state: {state}")
                failure_reason = status.get('failureReason', 'Unknown')
                log(f"Failure reason: {failure_reason}")
                return None
            
            # Still processing, wait a bit
            time.sleep(2)
        
        log(f"Translation timed out after {timeout} seconds")
        return None
    
    def list_faces(self, document_id, workspace_id, element_id):
        """
        List all faces in a Part Studio element using bodydetails endpoint

        Returns:
            Dict with bodies and their faces, or None if failed
        """
        endpoint = f"/partstudios/d/{document_id}/w/{workspace_id}/e/{element_id}/bodydetails"

        try:
            log(f"\n{'='*70}")
            log(f"ONSHAPE API: Getting body details")
            log(f"{'='*70}")
            log(f"Document ID: {document_id}")
            log(f"Workspace ID: {workspace_id}")
            log(f"Element ID: {element_id}")
            log(f"Full endpoint: {self.API_BASE}{endpoint}")

            response = self._make_api_request('GET', endpoint)

            log(f"\n📡 Response status: {response.status_code}")
            log(f"📡 Response headers: {dict(response.headers)}")

            if response.status_code == 200:
                data = response.json()

                log(f"\n✅ API call succeeded")
                log(f"Raw response keys: {list(data.keys())}")

                # Parse bodies and faces
                if 'bodies' in data:
                    body_count = len(data['bodies'])
                    log(f"\n📦 Found {body_count} bodies in element:")

                    if body_count == 0:
                        log("⚠️  WARNING: Element has ZERO bodies - this is unusual!")
                        log("   This means the Part Studio is either empty or the API isn't returning body data")

                    for body in data['bodies']:
                        body_id = body.get('id', 'unknown')
                        body_name = body.get('properties', {}).get('name', 'Unnamed')
                        faces = body.get('faces', [])
                        face_count = len(faces)

                        log(f"\n  🔷 Body: {body_id}")
                        log(f"     Name: {body_name}")
                        log(f"     Faces: {face_count}")

                        if face_count == 0:
                            log(f"     ⚠️  WARNING: Body has ZERO faces!")
                        else:
                            # Count face types
                            face_types = {}
                            for face in faces:
                                surface_type = face.get('surface', {}).get('type', 'UNKNOWN')
                                face_types[surface_type] = face_types.get(surface_type, 0) + 1

                            log(f"     Face types: {face_types}")

                            # Show first 5 faces with details
                            for i, face in enumerate(faces[:5]):
                                face_id = face.get('id', 'unknown')
                                surface = face.get('surface', {})
                                surface_type = surface.get('type', 'UNKNOWN')
                                normal = surface.get('normal', {})
                                area = face.get('area', 0)

                                log(f"     Face {i+1}/{face_count}: ID={face_id}")
                                log(f"       Type: {surface_type}")
                                log(f"       Area: {area:.6f}")
                                log(f"       Normal: ({normal.get('x', 0):.3f}, {normal.get('y', 0):.3f}, {normal.get('z', 0):.3f})")

                            if len(faces) > 5:
                                log(f"     ... and {len(faces) - 5} more faces")
                else:
                    log(f"⚠️  WARNING: Response has no 'bodies' key!")
                    log(f"   Available keys: {list(data.keys())}")

                log(f"{'='*70}\n")
                return data
            else:
                log(f"\n❌ API call failed: HTTP {response.status_code}")
                log(f"Response body: {response.text[:500]}")
                log(f"{'='*70}\n")
                return None

        except Exception as e:
            log(f"\n❌ Exception during list_faces:")
            log(f"Error: {e}")
            import traceback
            log(traceback.format_exc())
            log(f"{'='*70}\n")
            return None
    
    def get_body_faces(self, document_id, workspace_id, element_id, body_id=None, cached_faces_data=None):
        """
        Get face information for bodies in an element

        Args:
            body_id: Optional body ID filter (e.g., 'JHD')
            cached_faces_data: Optional pre-fetched faces data to avoid duplicate API calls

        Returns:
            Dict mapping body IDs to lists of face info dicts with id, area, surface type, position
        """
        data = cached_faces_data if cached_faces_data else self.list_faces(document_id, workspace_id, element_id)
        
        if not data or 'bodies' not in data:
            return None
        
        result = {}
        
        for body in data['bodies']:
            bid = body.get('id')
            if not bid:
                continue

            # If body_id specified, only include that body
            if body_id and bid != body_id:
                continue

            # Extract part name from properties
            body_name = body.get('properties', {}).get('name', 'Unnamed_Part')

            # Extract face information including area and surface details
            face_info = []
            for face in body.get('faces', []):
                fid = face.get('id')
                if fid:
                    surface = face.get('surface', {})
                    origin = surface.get('origin', {})
                    normal = surface.get('normal', {})

                    info = {
                        'id': fid,
                        'area': face.get('area', 0),
                        'surfaceType': surface.get('type', 'UNKNOWN'),
                        'origin': origin,
                        'normal': normal
                    }
                    face_info.append(info)

            # Sort by area (largest first)
            face_info.sort(key=lambda f: f['area'], reverse=True)

            result[bid] = {
                'name': body_name,
                'faces': face_info
            }
            log(f"Body {bid} ({body_name}): {len(face_info)} faces, largest area: {face_info[0]['area'] if face_info else 0}")
        
        return result
    
    def auto_select_top_face(self, document_id, workspace_id, element_id, body_id=None, cached_faces_data=None):
        """
        Automatically select the largest planar face

        Args:
            document_id: Onshape document ID
            workspace_id: Onshape workspace ID
            element_id: Onshape element ID
            body_id: Optional body/part ID to filter to a specific part
            cached_faces_data: Optional pre-fetched faces data to avoid duplicate API calls

        Returns:
            Tuple of (face_id, body_id, part_name, normal) or (None, None, None, None) if not found
        """
        log(f"\n{'='*70}")
        log(f"AUTO-SELECTING TOP FACE")
        log(f"{'='*70}")
        log(f"Document: {document_id}")
        log(f"Workspace: {workspace_id}")
        log(f"Element: {element_id}")
        log(f"Requested body_id: {body_id if body_id else '(auto-detect)'}")
        log(f"Using cached data: {cached_faces_data is not None}")

        faces_by_body = self.get_body_faces(document_id, workspace_id, element_id, body_id, cached_faces_data)

        if not faces_by_body:
            log("❌ get_body_faces returned None - no bodies found")
            log(f"{'='*70}\n")
            return None, None, None, None

        # Show available body IDs for debugging
        available_body_ids = list(faces_by_body.keys())
        log(f"\n📋 Available body IDs in document: {available_body_ids}")
        log(f"   Total bodies: {len(available_body_ids)}")

        # If body_id was specified, check if it matches
        if body_id:
            if body_id in faces_by_body:
                log(f"✅ Filtering to selected body: {body_id} ({faces_by_body[body_id]['name']})")
            else:
                log(f"⚠️  Requested body_id '{body_id}' not found in available bodies!")
                log(f"   Available: {available_body_ids}")
                log(f"   Will search all parts instead")

        # Get all faces from all bodies (or just the selected body), tracking which body they belong to
        all_faces = []
        for bid, body_data in faces_by_body.items():
            part_name = body_data['name']
            face_list = body_data['faces']
            log(f"\n   Processing body {bid} ({part_name}): {len(face_list)} faces")

            for face in face_list:
                face['body_id'] = bid  # The actual body ID from the loop
                face['part_name'] = part_name
                all_faces.append(face)

        log(f"\n📊 Total faces across all bodies: {len(all_faces)}")

        # Count face types
        face_type_counts = {}
        for face in all_faces:
            surface_type = face.get('surfaceType', 'UNKNOWN')
            face_type_counts[surface_type] = face_type_counts.get(surface_type, 0) + 1

        log(f"📊 Face type distribution: {face_type_counts}")

        # Filter for PLANE faces (any orientation)
        log(f"\n🔍 Filtering for PLANE faces...")
        plane_faces = []
        for face in all_faces:
            surface_type = face.get('surfaceType', 'UNKNOWN')

            if surface_type != 'PLANE':
                continue

            normal = face.get('normal', {})
            plane_faces.append({
                'face_id': face['id'],
                'area': face['area'],
                'part_name': face['part_name'],
                'body_id': face['body_id'],
                'normal': normal
            })

            log(f"   ✓ Found planar face: {face['id'][:8]}... ({face['part_name']})")
            log(f"      Area: {face['area']:.6f}")
            log(f"      Normal: ({normal.get('x', 0):.3f}, {normal.get('y', 0):.3f}, {normal.get('z', 0):.3f})")

        log(f"\n📊 Total planar faces found: {len(plane_faces)}")

        if not plane_faces:
            log("❌ No planar faces found in any body")
            log(f"{'='*70}\n")
            return None, None, None, None

        # Select the face with the largest area
        selected_face = max(plane_faces, key=lambda f: f['area'])

        # Store the normal for view matrix calculation
        normal = selected_face['normal']
        nx = normal.get('x', 0)
        ny = normal.get('y', 0)
        nz = normal.get('z', 1)

        log(f"\n✅ AUTO-SELECTED FACE:")
        log(f"   Face ID: {selected_face['face_id']}")
        log(f"   Part: {selected_face['part_name']}")
        log(f"   Body: {selected_face['body_id']}")
        log(f"   Area: {selected_face['area']:.6f}")
        log(f"   Normal: ({nx:.3f}, {ny:.3f}, {nz:.3f})")
        log(f"{'='*70}\n")

        return selected_face['face_id'], selected_face['body_id'], selected_face['part_name'], selected_face['normal']

    def find_parallel_faces_by_depth(self, document_id, workspace_id, element_id,
                                      reference_normal, reference_origin,
                                      body_id=None, cached_faces_data=None,
                                      angle_tolerance=0.1, depth_tolerance=0.01):
        """
        Find all planar faces parallel to a reference plane, binned by depth

        Args:
            reference_normal: Dict with x, y, z of reference plane normal
            reference_origin: Dict with x, y, z of reference plane origin
            body_id: Optional body ID to limit search
            cached_faces_data: Optional pre-fetched faces data
            angle_tolerance: Tolerance for checking if normals are parallel (0.1 = ~5.7 degrees)
            depth_tolerance: Tolerance for binning faces at similar depths (inches)

        Returns:
            Dict mapping depth values to lists of face_ids and metadata
            e.g., {0.0: [{'face_id': 'ABC', 'area': 10.5, ...}], -0.25: [...]}
        """
        log(f"\n{'='*70}")
        log(f"FINDING PARALLEL FACES BY DEPTH")
        log(f"{'='*70}")

        # Get all faces
        faces_by_body = self.get_body_faces(document_id, workspace_id, element_id, body_id, cached_faces_data)
        if not faces_by_body:
            log("No faces found")
            return {}

        # Reference normal vector (unitless, no conversion needed)
        ref_nx = reference_normal.get('x', 0)
        ref_ny = reference_normal.get('y', 0)
        ref_nz = reference_normal.get('z', 1)
        ref_mag = (ref_nx**2 + ref_ny**2 + ref_nz**2)**0.5

        # Reference origin point
        # NOTE: Units depend on document units - treat as-is for now
        ref_ox = reference_origin.get('x', 0)
        ref_oy = reference_origin.get('y', 0)
        ref_oz = reference_origin.get('z', 0)

        log(f"Reference normal: ({ref_nx:.3f}, {ref_ny:.3f}, {ref_nz:.3f})")
        log(f"Reference origin: ({ref_ox:.3f}, {ref_oy:.3f}, {ref_oz:.3f})")

        # Collect all parallel faces with their depths
        parallel_faces = []

        for bid, body_data in faces_by_body.items():
            for face in body_data['faces']:
                # Only consider planar faces
                if face.get('surfaceType') != 'PLANE':
                    continue

                normal = face.get('normal', {})
                origin = face.get('origin', {})

                nx = normal.get('x', 0)
                ny = normal.get('y', 0)
                nz = normal.get('z', 1)
                n_mag = (nx**2 + ny**2 + nz**2)**0.5

                # Check if normals are parallel (dot product ≈ ±1)
                if n_mag > 0 and ref_mag > 0:
                    dot_product = (nx * ref_nx + ny * ref_ny + nz * ref_nz) / (n_mag * ref_mag)

                    # Parallel if dot product is close to 1 or -1
                    if abs(abs(dot_product) - 1.0) < angle_tolerance:
                        # Calculate signed distance from reference plane
                        # Distance = (point - ref_origin) · ref_normal / |ref_normal|
                        ox = origin.get('x', 0)
                        oy = origin.get('y', 0)
                        oz = origin.get('z', 0)

                        dx = ox - ref_ox
                        dy = oy - ref_oy
                        dz = oz - ref_oz

                        signed_distance = (dx * ref_nx + dy * ref_ny + dz * ref_nz) / ref_mag

                        parallel_faces.append({
                            'face_id': face['id'],
                            'body_id': bid,
                            'part_name': body_data['name'],
                            'area': face['area'],
                            'depth': signed_distance,
                            'normal': normal,
                            'origin': origin
                        })

                        log(f"  Found parallel face {face['id'][:8]}... at depth {signed_distance:.4f}\" (area={face['area']:.4f})")

        log(f"\nTotal parallel faces found: {len(parallel_faces)}")

        # Bin faces by depth
        depth_bins = {}
        for face in parallel_faces:
            depth = face['depth']

            # Find existing bin within tolerance
            matched_bin = None
            for existing_depth in depth_bins.keys():
                if abs(depth - existing_depth) < depth_tolerance:
                    matched_bin = existing_depth
                    break

            if matched_bin is not None:
                depth_bins[matched_bin].append(face)
            else:
                depth_bins[depth] = [face]

        # Sort bins by depth (shallowest first)
        sorted_bins = dict(sorted(depth_bins.items(), key=lambda x: x[0], reverse=True))

        log(f"\nDepth bins (shallowest to deepest):")
        for depth, faces in sorted_bins.items():
            log(f"  Z={depth:+.4f}\": {len(faces)} faces")

        return sorted_bins

    def merge_dxfs_with_layers(self, dxf_contents_by_depth, depth_metadata=None):
        """
        Merge multiple DXF contents into one with depth-encoded layer names

        Args:
            dxf_contents_by_depth: Dict {depth: dxf_bytes}
            depth_metadata: Dict {depth: {'offset_x': float, 'offset_y': float}} for coordinate alignment

        Returns:
            Merged DXF content as bytes
        """
        import ezdxf
        import tempfile
        import os

        log(f"\n{'='*70}")
        log(f"MERGING DXFs WITH LAYER NAMES")
        log(f"{'='*70}")

        # Create new DXF document
        merged_doc = ezdxf.new('R2010', setup=True)
        merged_msp = merged_doc.modelspace()

        for depth, dxf_content in dxf_contents_by_depth.items():
            # Generate layer name: Z_0p000, Z_-0p250, etc.
            # Format: Z_{integer}p{fractional_digits}
            abs_depth = abs(depth)
            int_part = int(abs_depth)
            frac_part = int(round((abs_depth - int_part) * 1000))  # 3 decimal places

            if depth >= 0:
                layer_name = f"Z_{int_part}p{frac_part:03d}"
            else:
                layer_name = f"Z_-{int_part}p{frac_part:03d}"

            log(f"Processing depth {depth:.4f}\" -> layer {layer_name}")

            # Write DXF to temp file and read it back (ezdxf.read() from StringIO doesn't work properly)
            with tempfile.NamedTemporaryFile(mode='wb', suffix='.dxf', delete=False) as tmp_file:
                tmp_file.write(dxf_content)
                tmp_filename = tmp_file.name

            try:
                source_doc = ezdxf.readfile(tmp_filename)
                source_msp = source_doc.modelspace()

                log(f"  Source has {len(source_msp)} entities in modelspace")

                # Get translation offset for this layer
                offset_x = 0
                offset_y = 0
                if depth_metadata and depth in depth_metadata:
                    offset_x = depth_metadata[depth].get('offset_x', 0)
                    offset_y = depth_metadata[depth].get('offset_y', 0)
                    if offset_x != 0 or offset_y != 0:
                        log(f"  Applying translation: ({offset_x:.4f}, {offset_y:.4f})")

                # Create layer in merged doc if it doesn't exist
                if layer_name not in merged_doc.layers:
                    merged_doc.layers.add(layer_name)

                # Copy all entities to merged doc with new layer and translation
                entity_count = 0
                for entity in source_msp:
                    # Clone entity and change its layer
                    try:
                        new_entity = entity.copy()
                        new_entity.dxf.layer = layer_name

                        # Apply translation offset to align coordinate systems
                        if offset_x != 0 or offset_y != 0:
                            new_entity.translate(offset_x, offset_y, 0)

                        merged_msp.add_entity(new_entity)
                        entity_count += 1
                    except Exception as e:
                        log(f"  Warning: Could not copy entity {entity.dxftype()}: {e}")

                log(f"  Copied {entity_count} entities to layer {layer_name}")

            finally:
                # Clean up temp file
                os.unlink(tmp_filename)

        # Write merged document to bytes
        with tempfile.NamedTemporaryFile(mode='wb', suffix='.dxf', delete=False) as tmp_file:
            tmp_filename = tmp_file.name

        try:
            merged_doc.saveas(tmp_filename)
            with open(tmp_filename, 'rb') as f:
                merged_bytes = f.read()
        finally:
            os.unlink(tmp_filename)

        log(f"\nMerged DXF size: {len(merged_bytes)} bytes")

        return merged_bytes

    def export_multilayer_dxf(self, document_id, workspace_id, element_id,
                             reference_face_id, reference_body_id, reference_normal, reference_origin,
                             body_id=None, cached_faces_data=None):
        """
        Export multiple parallel faces as a single multi-layer DXF for 2.5D machining

        Args:
            reference_face_id: Face ID of the reference plane (typically the top face)
            reference_body_id: Body ID of the reference face
            reference_normal: Dict with reference plane normal
            reference_origin: Dict with reference plane origin
            body_id: Optional body ID to limit search
            cached_faces_data: Optional pre-fetched faces data

        Returns:
            Multi-layer DXF content as bytes, or None if failed
        """
        log(f"\n{'='*70}")
        log(f"MULTI-LAYER DXF EXPORT")
        log(f"{'='*70}")

        # Find all parallel faces grouped by depth
        # Use tight tolerance (1 mil) to avoid grouping distinct layers
        depth_bins = self.find_parallel_faces_by_depth(
            document_id, workspace_id, element_id,
            reference_normal, reference_origin,
            body_id, cached_faces_data,
            depth_tolerance=0.001  # 0.001" = 1 mil tolerance
        )

        if not depth_bins:
            log("No parallel faces found")
            return None

        # Export each depth group
        dxf_contents = {}

        from concurrent.futures import ThreadPoolExecutor, as_completed

        def export_depth_group(depth, faces):
            """Export a single depth group"""
            face_ids = [f['face_id'] for f in faces]
            face_ids_str = ','.join(face_ids)

            log(f"\nExporting depth {depth:.4f}\" ({len(faces)} faces): {face_ids_str}")

            # Use the existing export method with comma-separated face IDs
            # We'll modify export_face_to_dxf to accept multiple IDs
            return depth, self._export_faces_group_to_dxf(
                document_id, workspace_id, element_id,
                face_ids_str, reference_normal
            )

        # Export depth groups in parallel
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {
                executor.submit(export_depth_group, depth, faces): depth
                for depth, faces in depth_bins.items()
            }

            for future in as_completed(futures):
                depth = futures[future]
                try:
                    result_depth, dxf_content = future.result()
                    if dxf_content:
                        dxf_contents[result_depth] = dxf_content
                        log(f"✓ Depth {result_depth:.4f}\" exported ({len(dxf_content)} bytes)")
                    else:
                        log(f"✗ Depth {result_depth:.4f}\" export failed")
                except Exception as e:
                    log(f"✗ Depth {depth:.4f}\" export error: {e}")

        if not dxf_contents:
            log("No DXF content exported")
            return None

        # Calculate XY offsets for each depth layer to align them
        # Onshape exports each face group centered at the group's centroid
        # We know the face origins from bodydetails API, so we can calculate where to position each layer
        depth_metadata = {}

        log(f"\nCalculating layer positions from face origins:")

        for depth, faces in depth_bins.items():
            # Calculate the centroid of this depth group's faces
            # Onshape will export this group centered at (0,0), but it represents this centroid position
            centroid_x = sum(f['origin'].get('x', 0) for f in faces) / len(faces)
            centroid_y = sum(f['origin'].get('y', 0) for f in faces) / len(faces)

            # The DXF will be centered at (0,0), so we translate by the centroid to position it correctly
            depth_metadata[depth] = {
                'offset_x': centroid_x,
                'offset_y': centroid_y
            }

            log(f"  Depth {depth:.4f}\": {len(faces)} faces, centroid at ({centroid_x:.4f}, {centroid_y:.4f})")

        # Merge DXFs with layer names and coordinate alignment
        merged_dxf = self.merge_dxfs_with_layers(dxf_contents, depth_metadata)

        return merged_dxf

    def _export_faces_group_to_dxf(self, document_id, workspace_id, element_id, face_ids_str, face_normal=None):
        """
        Export multiple faces as a single DXF (helper for multi-layer export)

        Args:
            face_ids_str: Comma-separated face IDs (e.g., "JHD,JHE,JHF")
            face_normal: Optional dict with face normal vector

        Returns:
            DXF file content as bytes, or None if failed
        """
        endpoint = f"/documents/d/{document_id}/w/{workspace_id}/e/{element_id}/exportinternal"

        try:
            # Calculate view matrix based on face normal
            if face_normal:
                view_matrix = self._calculate_view_matrix(face_normal)
            else:
                view_matrix = "1,0,0,0,0,1,0,0,0,0,1,0,0,0,0,1"

            body = {
                "format": "DXF",
                "view": view_matrix,
                "version": "2013",
                "units": "inch",
                "flatten": "true",
                "includeBendCenterlines": "true",
                "includeSketches": "true",
                "splinesAsPolylines": "true",
                "triggerAutoDownload": "true",
                "storeInDocument": "false",
                "partIds": face_ids_str  # Comma-separated string
            }

            response = self._make_api_request('POST', endpoint, json=body)

            if response.status_code == 200:
                return response.content
            else:
                log(f"Export failed: {response.status_code} - {response.text}")
                return None

        except Exception as e:
            log(f"Error exporting faces: {e}")
            return None

    def get_document_info(self, document_id):
        """Get information about a document"""
        try:
            endpoint = f'/documents/{document_id}'
            log(f"   Calling: {self.API_BASE}{endpoint}")
            response = self._make_api_request('GET', endpoint)
            if response.status_code == 200:
                return response.json()
            else:
                log(f"Failed to get document info: HTTP {response.status_code}")
                log(f"Response: {response.text[:200]}")
                return None
        except Exception as e:
            log(f"Error getting document info: {e}")
            import traceback
            log(traceback.format_exc())
            return None
    
    def get_element_info(self, document_id, workspace_id, element_id):
        """Get information about an element (Part Studio, Assembly, etc.)"""
        try:
            # Get all elements in the document
            response = self._make_api_request(
                'GET',
                f'/documents/d/{document_id}/w/{workspace_id}/elements'
            )
            if response.status_code == 200:
                elements = response.json()
                log(f"   Found {len(elements)} elements in document")
                # Find the matching element
                for element in elements:
                    if element.get('id') == element_id:
                        return element
                log(f"   Element {element_id} not found in {len(elements)} elements")
                return None
            else:
                log(f"Failed to get elements: HTTP {response.status_code}")
                log(f"Response: {response.text[:200]}")
                return None
        except Exception as e:
            log(f"Error getting element info: {e}")
            import traceback
            log(traceback.format_exc())
            return None

    def get_user_session_info(self):
        """
        Get detailed session info for the authenticated user

        Returns:
            dict with user session info including name, email, etc.
        """
        try:
            log("   Fetching user session info...")
            response = self._make_api_request('GET', '/users/sessioninfo')
            if response.status_code == 200:
                user_info = response.json()
                log(f"   ✅ User: {user_info.get('name', 'Unknown')}")
                return user_info
            else:
                log(f"   ❌ Failed to get session info: HTTP {response.status_code}")
                return None
        except Exception as e:
            log(f"   ❌ Error getting session info: {e}")
            import traceback
            log(traceback.format_exc())
            return None

    def get_companies(self):
        """
        Get list of companies/teams the user belongs to

        Returns:
            list of company dicts
        """
        try:
            log("   Fetching companies...")
            response = self._make_api_request('GET', '/companies?activeOnly=true&includeAll=false')
            if response.status_code == 200:
                companies = response.json().get('items', [])
                log(f"   ✅ Found {len(companies)} companies: {[c.get('name') for c in companies]}")
                return companies
            else:
                log(f"   ❌ Failed to get companies: HTTP {response.status_code}")
                return None
        except Exception as e:
            log(f"   ❌ Error getting companies: {e}")
            import traceback
            log(traceback.format_exc())
            return None

    def get_document_company(self, document_id):
        """
        Get the company/team that owns a specific document

        Args:
            document_id: Onshape document ID

        Returns:
            dict with company info, or None if not found
        """
        try:
            log("   Determining document owner company...")

            # Get document info to find owner
            doc_info = self.get_document_info(document_id)
            if not doc_info:
                log("   ❌ Could not get document info")
                return None

            # Documents have an 'owner' field with type and id
            # type: 0 = user, 1 = company, 2 = team
            owner_info = doc_info.get('owner', {})
            owner_type = owner_info.get('type')
            owner_id = owner_info.get('id')
            owner_name = owner_info.get('name', 'Unknown')

            log(f"   Document owner: {owner_name} (type={owner_type}, id={owner_id[:8]}...)")

            # If owner is a company/team (type 1 or 2), find it in the companies list
            if owner_type in [1, 2]:
                companies = self.get_companies()
                if companies:
                    for company in companies:
                        if company.get('id') == owner_id:
                            log(f"   ✅ Document belongs to company: {company.get('name')}")
                            return company
                    log(f"   ⚠️  Document owner company not found in user's companies")
                    return None
            else:
                log(f"   ℹ️  Document is owned by user (not a company/team)")
                return None

        except Exception as e:
            log(f"   ❌ Error getting document company: {e}")
            import traceback
            log(traceback.format_exc())
            return None

    def fetch_config_file(self):
        """
        Search for and fetch PenguinCAM-config.yaml from the user's documents.

        Returns:
            str with raw YAML content, or None if not found or on error
        """
        try:
            log("\n🔍 Searching for PenguinCAM-config.yaml...")

            # Search for documents with the config filename (v13 API)
            search_body = {
                'rawQuery': 'PenguinCAM-config.yaml'
            }
            response = self._make_api_request('POST', '/documents/search', json=search_body)

            if response.status_code != 200:
                log(f"   ❌ Document search failed: HTTP {response.status_code}")
                log(f"   Response: {response.text[:200]}")
                return None

            search_results = response.json()
            items = search_results.get('items', [])

            log(f"   Found {len(items)} matching document(s)")

            if not items:
                log("   ℹ️  No PenguinCAM-config.yaml found in documents")
                return None

            # Use the first matching document
            config_doc = items[0]
            doc_id = config_doc.get('id')
            doc_name = config_doc.get('name', 'unknown')

            log(f"   ✅ Found config document: {doc_name} (ID: {doc_id[:8]}...)")

            # Get workspace ID from search results (v13 includes defaultWorkspace)
            workspace_id = config_doc.get('defaultWorkspace', {}).get('id')
            if not workspace_id:
                log("   ⚠️  No defaultWorkspace in search results, fetching document info...")
                # Fallback: fetch document info separately
                doc_info = self.get_document_info(doc_id)
                if not doc_info:
                    log("   ❌ Could not get document info")
                    return None
                workspace_id = doc_info.get('defaultWorkspace', {}).get('id')
                if not workspace_id:
                    log("   ❌ No default workspace found")
                    return None

            log(f"   Using workspace: {workspace_id[:8]}...")

            # List elements to find the YAML file tab
            response = self._make_api_request(
                'GET',
                f'/documents/d/{doc_id}/w/{workspace_id}/elements'
            )

            if response.status_code != 200:
                log(f"   ❌ Could not list elements: HTTP {response.status_code}")
                return None

            elements = response.json()

            # Look for a Blob element with exact filename match
            config_element = None
            for elem in elements:
                elem_name = elem.get('name', '')
                # Match exact filename (case-insensitive)
                if (elem.get('type') == 'Blob' and
                    elem_name.lower() in ['penguincam-config.yaml', 'penguincam-config.yml']):
                    config_element = elem
                    break

            if not config_element:
                log("   ❌ No YAML element found in document")
                log(f"   Available elements: {[e.get('name') for e in elements]}")
                return None

            element_id = config_element.get('id')
            element_name = config_element.get('name')

            log(f"   ✅ Found YAML element: {element_name} (ID: {element_id[:8]}...)")

            # Download the blob content as text
            response = self._make_api_request(
                'GET',
                f'/blobelements/d/{doc_id}/w/{workspace_id}/e/{element_id}'
            )

            if response.status_code != 200:
                log(f"   ❌ Could not download blob: HTTP {response.status_code}")
                return None

            # Return raw text content
            config_yaml = response.text
            log(f"   ✅ Successfully fetched config file ({len(config_yaml)} bytes)")

            return config_yaml

        except Exception as e:
            log(f"   ❌ Error fetching config file: {e}")
            import traceback
            log(traceback.format_exc())
            return None

    def parse_onshape_url(self, url):
        """
        Parse an Onshape URL to extract document/workspace/element IDs
        
        Args:
            url: Onshape URL (e.g., https://cad.onshape.com/documents/d/abc.../w/def.../e/ghi...)
            
        Returns:
            dict with 'document_id', 'workspace_id', 'element_id' or None if invalid
        """
        try:
            parts = url.split('/')
            
            result = {}
            
            # Find document ID
            if '/d/' in url:
                d_idx = parts.index('d')
                result['document_id'] = parts[d_idx + 1]
            
            # Find workspace ID
            if '/w/' in url:
                w_idx = parts.index('w')
                result['workspace_id'] = parts[w_idx + 1]
            
            # Find element ID
            if '/e/' in url:
                e_idx = parts.index('e')
                result['element_id'] = parts[e_idx + 1]
            
            return result if len(result) == 3 else None
            
        except Exception as e:
            log(f"Error parsing Onshape URL: {e}")
            return None


class OnshapeSessionManager:
    """
    Manages Onshape OAuth sessions using Flask session (encrypted cookies).

    Serverless-compatible: Tokens are stored in encrypted session cookies,
    not server memory. Works across multiple container instances.
    """

    def create_session(self, user_id, client):
        """
        Store Onshape tokens in Flask session (not the entire client object).

        Args:
            user_id: User identifier (for logging/debugging)
            client: OnshapeClient with valid tokens
        """
        # Store only the serializable token data in Flask session
        session['onshape_tokens'] = {
            'access_token': client.access_token,
            'refresh_token': client.refresh_token,
            'expires_at': client.token_expires.isoformat() if client.token_expires else None,
            'created': datetime.now().isoformat()
        }

    def get_client(self, user_id):
        """
        Reconstruct OnshapeClient from Flask session tokens.

        Args:
            user_id: User identifier (unused - tokens come from session cookie)

        Returns:
            OnshapeClient with tokens restored, or None if not authenticated
        """
        tokens = session.get('onshape_tokens')
        if not tokens:
            return None

        # Reconstruct client from stored tokens
        client = OnshapeClient()
        client.access_token = tokens.get('access_token')
        client.refresh_token = tokens.get('refresh_token')

        # Parse expiration timestamp
        expires_str = tokens.get('expires_at')
        if expires_str:
            client.token_expires = datetime.fromisoformat(expires_str)

        return client

    def clear_session(self, user_id):
        """
        Remove Onshape tokens from Flask session.

        Args:
            user_id: User identifier (unused - operates on session cookie)
        """
        if 'onshape_tokens' in session:
            del session['onshape_tokens']


# Global session manager (stateless - all state in Flask session cookies)
session_manager = OnshapeSessionManager()


def get_onshape_client():
    """Get a new Onshape client instance"""
    return OnshapeClient()

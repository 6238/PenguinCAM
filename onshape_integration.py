"""
OnShape Integration for PenguinCAM
Handles OAuth authentication and DXF export from OnShape
"""

import os
import json
import requests
import base64
from urllib.parse import urlencode, parse_qs
from datetime import datetime, timedelta

class OnShapeClient:
    """Client for interacting with OnShape API"""
    
    BASE_URL = "https://cad.onshape.com"
    API_BASE = "https://cad.onshape.com/api/v12"
    
    def __init__(self):
        self.config = self._load_config()
        self.access_token = None
        self.refresh_token = None
        self.token_expires = None
    
    def _load_config(self):
        """Load OnShape OAuth configuration, prioritizing environment variables"""
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
            raise ValueError("OnShape client_secret not configured")
        
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
                print(f"Token exchange failed: {response.status_code} - {response.text}")
                return None
                
        except Exception as e:
            print(f"Error exchanging code for token: {e}")
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
            print(f"Error refreshing token: {e}")
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
        Make an authenticated API request to OnShape
        
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
            print(f"Error getting user info: {e}")
            return None
    
    def export_face_to_dxf(self, document_id, workspace_id, element_id, face_id, body_id=None):
        """
        Export a face from a Part Studio as DXF

        Args:
            document_id: OnShape document ID (from URL: /documents/d/{did})
            workspace_id: Workspace ID (from URL: /w/{wid})
            element_id: Element ID (from URL: /e/{eid})
            face_id: The face ID (used for logging/backwards compatibility)
            body_id: The body/part ID to export (if None, uses face_id for backwards compatibility)

        Returns:
            DXF file content as bytes, or None if failed
        """
        print(f"\n=== Attempting DXF export ===")
        print(f"Document: {document_id}")
        print(f"Workspace: {workspace_id}")
        print(f"Element: {element_id}")
        print(f"Face: {face_id}")
        print(f"Body: {body_id}")
        
        # Try the internal export endpoint that OnShape's web UI uses
        print("\n[Method 1] Trying exportinternal endpoint (web UI method)...")
        endpoint = f"/documents/d/{document_id}/w/{workspace_id}/e/{element_id}/exportinternal"
        
        try:
            # Use a top-down view matrix (looking down at XY plane)
            # This is what you'd see when looking at a flat plate from above
            # Use body_id if provided (to export full part), otherwise fall back to face_id
            export_id = body_id if body_id else face_id
            body = {
                "format": "DXF",
                "view": "1,0,0,0,0,1,0,0,0,0,1,0,0,0,0,1",  # Identity matrix (top view)
                "version": "2013",
                "units": "inch",
                "flatten": "true",  # Critical for 2D export
                "includeBendCenterlines": "true",
                "includeSketches": "true",
                "splinesAsPolylines": "true",
                "triggerAutoDownload": "true",
                "storeInDocument": "false",
                "partIds": export_id  # The body/part ID (or face ID for backwards compatibility)
            }
            
            print(f"API endpoint: {self.API_BASE}{endpoint}")
            print(f"Request body: {json.dumps(body, indent=2)}")
            
            response = self._make_api_request('POST', endpoint, json=body)
            
            print(f"Response status: {response.status_code}")
            print(f"Response headers: {dict(response.headers)}")
            
            if response.status_code == 200:
                print(f"Success! DXF content length: {len(response.content)} bytes")
                # Check if it's actually DXF content
                content_preview = response.content[:100].decode('utf-8', errors='ignore')
                print(f"Content preview: {content_preview[:50]}...")
                return response.content
            else:
                print(f"exportinternal failed: {response.status_code}")
                print(f"Response: {response.text}")
                
        except Exception as e:
            print(f"Error with exportinternal: {e}")
            import traceback
            traceback.print_exc()
        
        # Fallback: Try async translations API
        print("\n[Method 2] Trying async translations API...")
        result = self.export_dxf_async(document_id, workspace_id, element_id)
        if result:
            return result
        
        # Fallback: Try POST /export endpoint
        print("\n[Method 3] Trying POST /export endpoint...")
        endpoint = f"/partstudios/d/{document_id}/w/{workspace_id}/e/{element_id}/export"
        
        try:
            body = {
                "format": "DXF",
                "version": "2013",
                "flattenAssemblies": True
            }
            
            response = self._make_api_request('POST', endpoint, json=body)
            
            if response.status_code == 200:
                print(f"Success! DXF content length: {len(response.content)} bytes")
                return response.content
            else:
                print(f"POST export failed: {response.status_code}")
                
        except Exception as e:
            print(f"Error with POST export: {e}")
        
        print("\n=== All export methods failed ===")
        return None
    
    def _export_element_to_dxf(self, document_id, workspace_id, element_id):
        """Try to export entire element as DXF"""
        endpoint = f"/partstudios/d/{document_id}/w/{workspace_id}/e/{element_id}/dxf"
        
        try:
            print(f"Exporting entire element as DXF...")
            response = self._make_api_request('GET', endpoint)
            
            print(f"Response status: {response.status_code}")
            
            if response.status_code == 200:
                print(f"Success! DXF content length: {len(response.content)} bytes")
                return response.content
            else:
                print(f"Failed: {response.status_code} - {response.text}")
                return None
                
        except Exception as e:
            print(f"Error: {e}")
            return None
    
    def start_dxf_translation(self, document_id, workspace_id, element_id):
        """
        Start an async DXF export translation
        
        Returns:
            Translation ID if successful, None otherwise
        """
        endpoint = f"/partstudios/d/{document_id}/w/{workspace_id}/e/{element_id}/translations"
        
        try:
            print(f"\nStarting DXF translation for element {element_id}")
            print(f"API endpoint: {self.API_BASE}{endpoint}")
            
            body = {
                "formatName": "DXF",
                "storeInDocument": False,  # Don't store in OnShape, just export
                "flattenAssemblies": True
            }
            
            print(f"Request body: {json.dumps(body, indent=2)}")
            
            response = self._make_api_request('POST', endpoint, json=body)
            
            print(f"Response status: {response.status_code}")
            
            if response.status_code == 200:
                data = response.json()
                translation_id = data.get('id')
                print(f"Translation started! ID: {translation_id}")
                return translation_id
            else:
                print(f"Failed to start translation: {response.status_code}")
                print(f"Response: {response.text}")
                return None
                
        except Exception as e:
            print(f"Error starting translation: {e}")
            import traceback
            traceback.print_exc()
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
                print(f"Translation {translation_id}: {state}")
                return data
            else:
                print(f"Failed to check translation: {response.status_code}")
                return None
                
        except Exception as e:
            print(f"Error checking translation: {e}")
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
            print(f"Downloading translation result...")
            response = self._make_api_request('GET', endpoint)
            
            if response.status_code == 200:
                print(f"Downloaded {len(response.content)} bytes")
                return response.content
            else:
                print(f"Failed to download: {response.status_code}")
                print(f"Response: {response.text}")
                return None
                
        except Exception as e:
            print(f"Error downloading result: {e}")
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
                    print("Translation done but no result data ID found")
                    return None
                    
            elif state in ['FAILED', 'ACTIVE']:
                print(f"Translation failed with state: {state}")
                failure_reason = status.get('failureReason', 'Unknown')
                print(f"Failure reason: {failure_reason}")
                return None
            
            # Still processing, wait a bit
            time.sleep(2)
        
        print(f"Translation timed out after {timeout} seconds")
        return None
    
    def list_faces(self, document_id, workspace_id, element_id):
        """
        List all faces in a Part Studio element using bodydetails endpoint
        
        Returns:
            Dict with bodies and their faces, or None if failed
        """
        endpoint = f"/partstudios/d/{document_id}/w/{workspace_id}/e/{element_id}/bodydetails"
        
        try:
            print(f"\nGetting body details for element {element_id}...")
            print(f"API endpoint: {self.API_BASE}{endpoint}")
            
            response = self._make_api_request('GET', endpoint)
            
            print(f"Response status: {response.status_code}")
            
            if response.status_code == 200:
                data = response.json()
                
                # Parse bodies and faces
                if 'bodies' in data:
                    print(f"\nFound {len(data['bodies'])} bodies:")

                    for body in data['bodies']:
                        body_id = body.get('id', 'unknown')
                        body_name = body.get('properties', {}).get('name', 'Unnamed')
                        faces = body.get('faces', [])
                        print(f"  Body {body_id} ({body_name}): {len(faces)} faces")

                        for i, face in enumerate(faces[:5]):  # Show first 5
                            face_id = face.get('id', 'unknown')
                            surface_type = face.get('surface', {}).get('type', 'unknown')
                            print(f"    Face {face_id}: {surface_type}")

                        if len(faces) > 5:
                            print(f"    ... and {len(faces) - 5} more faces")
                
                return data
            else:
                print(f"Failed: {response.status_code} - {response.text}")
                return None
                
        except Exception as e:
            print(f"Error listing faces: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    def get_body_faces(self, document_id, workspace_id, element_id, body_id=None):
        """
        Get face information for bodies in an element
        
        Args:
            body_id: Optional body ID filter (e.g., 'JHD')
            
        Returns:
            Dict mapping body IDs to lists of face info dicts with id, area, surface type, position
        """
        data = self.list_faces(document_id, workspace_id, element_id)
        
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
            print(f"Body {bid} ({body_name}): {len(face_info)} faces, largest area: {face_info[0]['area'] if face_info else 0}")
        
        return result
    
    def auto_select_top_face(self, document_id, workspace_id, element_id, body_id=None):
        """
        Automatically select the top face of a part (highest Z plane face)

        Args:
            document_id: OnShape document ID
            workspace_id: OnShape workspace ID
            element_id: OnShape element ID
            body_id: Optional body/part ID to filter to a specific part

        Returns:
            Tuple of (face_id, body_id, part_name) or (None, None, None) if not found
        """
        faces_by_body = self.get_body_faces(document_id, workspace_id, element_id, body_id)

        if not faces_by_body:
            return None, None, None

        # Show available body IDs for debugging
        available_body_ids = list(faces_by_body.keys())
        print(f"\n📋 Available body IDs in document: {available_body_ids}")

        # If body_id was specified, check if it matches
        if body_id:
            if body_id in faces_by_body:
                print(f"✅ Filtering to selected body: {body_id} ({faces_by_body[body_id]['name']})")
            else:
                print(f"⚠️  Requested body_id '{body_id}' not found in available bodies!")
                print(f"   Available: {available_body_ids}")
                print(f"   Will search all parts instead")

        # Get all faces from all bodies (or just the selected body), tracking which body they belong to
        all_faces = []
        for bid, body_data in faces_by_body.items():
            part_name = body_data['name']
            for face in body_data['faces']:
                face['body_id'] = bid  # The actual body ID from the loop
                face['part_name'] = part_name
                all_faces.append(face)

        # Filter for PLANE faces that are horizontal (normal pointing up/down in Z)
        horizontal_planes = []
        for face in all_faces:
            if face['surfaceType'] != 'PLANE':
                continue

            normal = face.get('normal', {})
            origin = face.get('origin', {})

            nz = normal.get('z', None)

            if nz is None:
                continue

            # Check if normal is vertical (pointing up or down)
            # Normal should be close to (0, 0, ±1)
            if abs(abs(nz) - 1.0) < 0.1:  # Allow small tolerance
                z_pos = origin.get('z', 0)

                horizontal_planes.append({
                    'face_id': face['id'],
                    'z_position': z_pos,
                    'normal_z': nz,
                    'area': face['area'],
                    'part_name': face['part_name'],
                    'body_id': face['body_id']
                })

                print(f"  Found horizontal plane: {face['id']} ({face['part_name']}), Z={z_pos:.6f}, normal_z={nz:.3f}, area={face['area']:.6f}")

        if not horizontal_planes:
            print("No horizontal plane faces found")
            return None, None, None

        # Find the highest Z position
        max_z = max(f['z_position'] for f in horizontal_planes)

        # Among faces near the top (within 0.01" of max Z), select the one with largest area
        # This avoids selecting tiny faces on top of teeth/features
        tolerance = 0.01
        top_faces = [f for f in horizontal_planes if abs(f['z_position'] - max_z) <= tolerance]

        if not top_faces:
            top_faces = horizontal_planes  # Fallback

        # Select the face with the largest area
        top_face = max(top_faces, key=lambda f: f['area'])

        print(f"\n✅ Auto-selected top face: {top_face['face_id']} from part '{top_face['part_name']}' (body: {top_face['body_id']}) at Z={top_face['z_position']:.6f}, area={top_face['area']:.6f}")

        return top_face['face_id'], top_face['body_id'], top_face['part_name']
    
    def get_document_info(self, document_id):
        """Get information about a document"""
        try:
            endpoint = f'/documents/{document_id}'
            print(f"   Calling: {self.API_BASE}{endpoint}")
            response = self._make_api_request('GET', endpoint)
            if response.status_code == 200:
                return response.json()
            else:
                print(f"Failed to get document info: HTTP {response.status_code}")
                print(f"Response: {response.text[:200]}")
                return None
        except Exception as e:
            print(f"Error getting document info: {e}")
            import traceback
            traceback.print_exc()
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
                print(f"   Found {len(elements)} elements in document")
                # Find the matching element
                for element in elements:
                    if element.get('id') == element_id:
                        return element
                print(f"   Element {element_id} not found in {len(elements)} elements")
                return None
            else:
                print(f"Failed to get elements: HTTP {response.status_code}")
                print(f"Response: {response.text[:200]}")
                return None
        except Exception as e:
            print(f"Error getting element info: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    def parse_onshape_url(self, url):
        """
        Parse an OnShape URL to extract document/workspace/element IDs
        
        Args:
            url: OnShape URL (e.g., https://cad.onshape.com/documents/d/abc.../w/def.../e/ghi...)
            
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
            print(f"Error parsing OnShape URL: {e}")
            return None


class OnShapeSessionManager:
    """Manages OnShape OAuth sessions for users"""
    
    def __init__(self):
        self.sessions = {}  # In-memory storage (use Redis/DB in production)
    
    def create_session(self, user_id, client):
        """Store OnShape client for a user session"""
        self.sessions[user_id] = {
            'client': client,
            'created': datetime.now()
        }
    
    def get_client(self, user_id):
        """Get OnShape client for a user"""
        session = self.sessions.get(user_id)
        if session:
            return session['client']
        return None
    
    def clear_session(self, user_id):
        """Remove user's OnShape session"""
        if user_id in self.sessions:
            del self.sessions[user_id]


# Global session manager
session_manager = OnShapeSessionManager()


def get_onshape_client():
    """Get a new OnShape client instance"""
    return OnShapeClient()

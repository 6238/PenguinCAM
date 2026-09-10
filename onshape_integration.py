"""
Onshape Integration for PenguinCAM
Handles OAuth authentication and DXF export from Onshape
"""

import math
import os
import json
import tempfile
import time
import traceback

import ezdxf
import requests
import base64
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from urllib.parse import urlencode, parse_qs

from flask import session
from shapely.geometry import Point, Polygon
from shapely.ops import unary_union

from dxf_geometry import entities_to_closed_paths, polygon_from_path
from logging_config import log  # shared log() + logging setup (was duplicated per module)


def mask(secret):
    """Redact a secret for logging.

    Returns a fingerprint that is safe to write to logs / stack traces without
    revealing the value. Never returns enough characters to reconstruct it.
    Used anywhere an API key/secret (or a derived auth header) might otherwise
    be printed.
    """
    if not secret:
        return '<unset>'
    s = str(secret)
    if len(s) <= 8:
        return '****'
    return f'****{s[-4:]}'


class OnshapeClient:
    """Client for interacting with Onshape API"""
    
    BASE_URL = "https://cad.onshape.com"
    API_BASE = "https://cad.onshape.com/api/v13"
    
    def __init__(self):
        self.config = self._load_config()
        self.access_token = None
        self.refresh_token = None
        self.token_expires = None
        # Auth mode: 'oauth' (interactive, session-driven) or 'apikey' (headless).
        self.auth_mode = 'oauth'
        self.api_access_key = None
        self.api_secret_key = None
        # Shared connection pool with retry/backoff for transient failures
        # (connection resets, 429/5xx). Applies to every API call in both auth
        # modes; important for the headless harness that fires many requests.
        self.session = requests.Session()
        retry = Retry(
            total=4, connect=4, read=4, status=4,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(['GET', 'POST', 'PUT', 'DELETE']),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount('https://', adapter)
        self.session.mount('http://', adapter)

    @classmethod
    def from_api_keys(cls, access_key=None, secret_key=None):
        """Build a headless client authenticated with an Onshape API key pair.

        No OAuth / Flask session involved. When args are omitted, reads
        ONSHAPE_ACCESS_KEY / ONSHAPE_SECRET_KEY from the environment. The secret
        stays inside this process and is never logged; only a masked fingerprint
        of the access key is emitted so runs are traceable.
        """
        client = cls()
        client.auth_mode = 'apikey'
        client.api_access_key = access_key or os.environ.get('ONSHAPE_ACCESS_KEY')
        client.api_secret_key = secret_key or os.environ.get('ONSHAPE_SECRET_KEY')
        if not client.api_access_key or not client.api_secret_key:
            raise ValueError(
                "API-key auth requires ONSHAPE_ACCESS_KEY and ONSHAPE_SECRET_KEY "
                "environment variables to be set (their values are never printed)."
            )
        log(f"   Onshape API-key mode: access key {mask(client.api_access_key)}")
        return client

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
        if self.auth_mode == 'apikey':
            # API keys don't expire and carry no refresh flow; nothing to do.
            if not (self.api_access_key and self.api_secret_key):
                raise ValueError("API-key mode selected but keys are not set")
            return
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

        if self.auth_mode == 'apikey':
            # HTTP Basic (accessKey:secretKey). requests builds the header
            # internally; the credential is never placed in a variable we log.
            return self.session.request(
                method, url, headers=headers,
                auth=(self.api_access_key, self.api_secret_key), **kwargs
            )

        headers['Authorization'] = f'Bearer {self.access_token}'
        return self.session.request(method, url, headers=headers, **kwargs)
    
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

        # Normalize the normal vector
        mag = math.sqrt(nx*nx + ny*ny + nz*nz)
        if mag < 1e-6:
            # Degenerate normal, use default top view
            return "1,0,0,0,0,1,0,0,0,0,1,0,0,0,0,1"

        nx /= mag
        ny /= mag
        nz /= mag

        # Create an orthonormal basis for the view
        # The normal becomes the Z-axis (viewing direction)
        # We need to find perpendicular X and Y axes

        # Choose a reference "up" vector that isn't parallel to the normal
        # Prefer world Z-axis, but use world Y if normal is close to Z
        if abs(nz) < 0.9:
            # Normal is not close to Z-axis, use world Z as up reference
            up_x, up_y, up_z = 0, 0, 1
        else:
            # Normal is close to Z-axis, use world Y as up reference
            up_x, up_y, up_z = 0, 1, 0

        # Compute right vector = up × normal (cross product)
        right_x = up_y * nz - up_z * ny
        right_y = up_z * nx - up_x * nz
        right_z = up_x * ny - up_y * nx

        # Normalize right vector
        right_mag = math.sqrt(right_x*right_x + right_y*right_y + right_z*right_z)
        if right_mag < 1e-6:
            # Degenerate case, fall back to default
            return "1,0,0,0,0,1,0,0,0,0,1,0,0,0,0,1"

        right_x /= right_mag
        right_y /= right_mag
        right_z /= right_mag

        # Compute actual up vector = normal × right
        actual_up_x = ny * right_z - nz * right_y
        actual_up_y = nz * right_x - nx * right_z
        actual_up_z = nx * right_y - ny * right_x

        # Build 4x4 view matrix in row-major order
        # First row is right vector
        # Second row is up vector
        # Third row is normal vector
        # Fourth row is translation (0,0,0,1)
        matrix = [
            right_x, right_y, right_z, 0,
            actual_up_x, actual_up_y, actual_up_z, 0,
            nx, ny, nz, 0,
            0, 0, 0, 1
        ]

        # Convert to comma-separated string
        return ','.join(str(v) for v in matrix)

    @staticmethod
    def _wvm_path(document_id, workspace_id=None, version_id=None, microversion_id=None):
        """Build the Onshape document-context path segment 'd/{did}/{w|v|m}/{id}'.

        Onshape addresses element data by workspace (w, live/editable), version (v),
        or microversion (m) - the latter two are read-only snapshots. When our element
        panel is launched on an OLDER VERSION of a document, Onshape substitutes the
        {$versionId} placeholder in the panel URL but leaves {$workspaceId} unfilled
        (there is no workspace in that context). Callers pass whichever id they were
        handed and this picks the matching segment, preferring workspace when present.
        Read is valid against all three, so DXF export works from a version too.
        """
        if workspace_id:
            wvm = f"w/{workspace_id}"
        elif version_id:
            wvm = f"v/{version_id}"
        elif microversion_id:
            wvm = f"m/{microversion_id}"
        else:
            raise ValueError("Onshape document context needs a workspace, version, or microversion id")
        return f"d/{document_id}/{wvm}"

    def export_face_to_dxf(self, document_id, workspace_id, element_id, face_id, body_id=None, face_normal=None,
                           version_id=None, microversion_id=None):
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
        endpoint = f"/documents/{self._wvm_path(document_id, workspace_id, version_id, microversion_id)}/e/{element_id}/exportinternal"
        
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
            
            if response.status_code == 200:
                log(f"Success! DXF content length: {len(response.content)} bytes")
                # Check if it's actually DXF content
                content_preview = response.content[:100].decode('utf-8', errors='ignore')
                return response.content
            else:
                log(f"exportinternal failed: {response.status_code}")
                log(f"Response: {response.text}")
                
        except Exception as e:
            log(f"Error with exportinternal: {e}")
            log(traceback.format_exc())
        
        # Fallback: Try async translations API
        log("\n[Method 2] Trying async translations API...")
        result = self.export_dxf_async(document_id, workspace_id, element_id,
                                       version_id=version_id, microversion_id=microversion_id)
        if result:
            return result

        # Fallback: Try POST /export endpoint
        log("\n[Method 3] Trying POST /export endpoint...")
        endpoint = f"/partstudios/{self._wvm_path(document_id, workspace_id, version_id, microversion_id)}/e/{element_id}/export"
        
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
    
    def start_dxf_translation(self, document_id, workspace_id, element_id,
                              version_id=None, microversion_id=None):
        """
        Start an async DXF export translation

        Returns:
            Translation ID if successful, None otherwise
        """
        endpoint = f"/partstudios/{self._wvm_path(document_id, workspace_id, version_id, microversion_id)}/e/{element_id}/translations"
        
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
    
    def export_dxf_async(self, document_id, workspace_id, element_id, timeout=60,
                         version_id=None, microversion_id=None):
        """
        Export DXF using async translations API
        Polls until complete or timeout

        Returns:
            DXF content as bytes, or None
        """

        # Start translation
        translation_id = self.start_dxf_translation(document_id, workspace_id, element_id,
                                                    version_id=version_id, microversion_id=microversion_id)
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
    
    def list_faces(self, document_id, workspace_id, element_id,
                   version_id=None, microversion_id=None):
        """
        List all faces in a Part Studio element using bodydetails endpoint

        Returns:
            Dict with bodies and their faces, or None if failed
        """
        endpoint = f"/partstudios/{self._wvm_path(document_id, workspace_id, version_id, microversion_id)}/e/{element_id}/bodydetails"

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

            if response.status_code == 200:
                data = response.json()

                log(f"\n✅ API call succeeded")

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
            log(traceback.format_exc())
            log(f"{'='*70}\n")
            return None
    
    def get_body_faces(self, document_id, workspace_id, element_id, body_id=None, cached_faces_data=None,
                       version_id=None, microversion_id=None):
        """
        Get face information for bodies in an element

        Args:
            body_id: Optional body ID filter (e.g., 'JHD')
            cached_faces_data: Optional pre-fetched faces data to avoid duplicate API calls

        Returns:
            Dict mapping body IDs to lists of face info dicts with id, area, surface type, position
        """
        data = cached_faces_data if cached_faces_data else self.list_faces(
            document_id, workspace_id, element_id, version_id=version_id, microversion_id=microversion_id)
        
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
    
    def auto_select_top_face(self, document_id, workspace_id, element_id, body_id=None, cached_faces_data=None,
                             version_id=None, microversion_id=None):
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

        faces_by_body = self.get_body_faces(document_id, workspace_id, element_id, body_id, cached_faces_data,
                                            version_id=version_id, microversion_id=microversion_id)

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
                                      angle_tolerance=0.1, depth_tolerance=0.01,
                                      version_id=None, microversion_id=None):
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
        faces_by_body = self.get_body_faces(document_id, workspace_id, element_id, body_id, cached_faces_data,
                                            version_id=version_id, microversion_id=microversion_id)
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

                # Check if normals are parallel (same or opposite direction)
                # Accept both upward-facing (grooves/pockets) and downward-facing (bottom face)
                if n_mag > 0 and ref_mag > 0:
                    dot_product = (nx * ref_nx + ny * ref_ny + nz * ref_nz) / (n_mag * ref_mag)

                    # Accept faces with normals parallel in EITHER direction
                    # dot product ≈ +1: same direction (grooves/pockets at partial depth)
                    # dot product ≈ -1: opposite direction (bottom face of part)
                    if abs(dot_product) > (1.0 - angle_tolerance):
                        # Calculate signed distance from reference plane
                        # Distance = (point - ref_origin) · ref_normal / |ref_normal|
                        ox = origin.get('x', 0)
                        oy = origin.get('y', 0)
                        oz = origin.get('z', 0)

                        dx = ox - ref_ox
                        dy = oy - ref_oy
                        dz = oz - ref_oz

                        # Calculate signed distance in meters
                        signed_distance_m = (dx * ref_nx + dy * ref_ny + dz * ref_nz) / ref_mag

                        # Convert from meters to inches
                        METERS_TO_INCHES = 39.3701
                        signed_distance = signed_distance_m * METERS_TO_INCHES

                        # Convert area from square meters to square inches
                        area_sq_in = face['area'] * (METERS_TO_INCHES ** 2)

                        parallel_faces.append({
                            'face_id': face['id'],
                            'body_id': bid,
                            'part_name': body_data['name'],
                            'area': area_sq_in,
                            'depth': signed_distance,
                            'normal': normal,
                            'origin': origin
                        })

                        log(f"  Found parallel face {face['id'][:8]}... at depth {signed_distance:.4f}\" (area={area_sq_in:.4f} sq in)")

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

    @staticmethod
    def _thickness_from_depth_bins(depth_bins):
        """Designed stock thickness = the span of parallel-face depths (top minus bottom),
        in inches - or None if fewer than two distinct depths were found (a single flat face
        has no defined thickness). Single definition used by both the 2.5D multilayer export
        and the 2D thickness probe."""
        depths = list(depth_bins.keys())
        if len(depths) < 2:
            return None
        return max(depths) - min(depths)

    def detect_stock_thickness(self, document_id, workspace_id, element_id,
                               reference_normal, reference_origin,
                               body_id=None, cached_faces_data=None,
                               version_id=None, microversion_id=None):
        """Discover a part's designed stock thickness (height along the reference-face normal)
        by binning its parallel faces by depth - the same derivation 2.5D uses, exposed so the
        2D path can seed its (still-editable) thickness field. Returns inches, or None if it
        can't be determined. Best-effort: callers should tolerate None."""
        depth_bins = self.find_parallel_faces_by_depth(
            document_id, workspace_id, element_id,
            reference_normal, reference_origin,
            body_id=body_id, cached_faces_data=cached_faces_data,
            version_id=version_id, microversion_id=microversion_id)
        return self._thickness_from_depth_bins(depth_bins)

    # Typical tube wall thickness ceiling (inches). Real extrusion walls are
    # 1/16"-1/8"; a "wall" thicker than this is more likely a real machining
    # level than a hollow-section wall.
    _TUBE_WALL_MAX_IN = 0.2

    @staticmethod
    def _classify_from_depths(depths, name_hint=''):
        """Decide a machining setup from a part's parallel-face depths (pure logic).

        This is the heuristic the harness uses to replicate what a user picks in
        the wizard, factored out of any API calls so it is unit-testable.

        Args:
            depths: iterable of parallel-face depths in inches (0 = reference/top
                face, negative going down). Order doesn't matter; sorted here.
            name_hint: part/document name, used only as a weak tube signal.

        Returns:
            dict with:
              part_type: '2d' | '2.5d' | 'tube' | 'unknown'
              export_strategy: 'onshape_standard_dxf' | 'constructed_multilayer'
                               | 'tube' | None
              thickness_in: material/wall thickness (inches) or None
              tube_height_in: full tube height (inches) for tubes, else None
              confidence: 'high' | 'low'
              needs_review: bool
              notes: list[str] explaining the decision
        """
        name_l = (name_hint or '').lower()
        ds = sorted(set(round(d, 4) for d in depths), reverse=True)  # shallow->deep
        n = len(ds)
        notes = []

        if n <= 1:
            return {
                'part_type': 'unknown', 'export_strategy': None,
                'thickness_in': None, 'tube_height_in': None,
                'confidence': 'low', 'needs_review': True,
                'notes': [f'Only {n} distinct parallel-face depth(s); '
                          'cannot determine thickness. Needs a human.'],
            }

        gaps = [ds[i] - ds[i + 1] for i in range(n - 1)]  # all > 0
        span = ds[0] - ds[-1]

        # Tube signature: a thin wall at BOTH the top and bottom of the section
        # with a large hollow void between them -> gap pattern small, big, small.
        tube_by_geometry = (
            n >= 4
            and gaps[0] < OnshapeClient._TUBE_WALL_MAX_IN
            and gaps[-1] < OnshapeClient._TUBE_WALL_MAX_IN
            and max(gaps[1:-1]) > 3 * max(gaps[0], gaps[-1])
        )
        tube_by_name = 'tube' in name_l
        if tube_by_geometry or tube_by_name:
            if tube_by_geometry:
                notes.append('Thin-wall/hollow depth signature (small-big-small gaps).')
            if tube_by_name:
                notes.append('Name mentions "tube".')
            # Tube wizard fields: material thickness = wall, tube_height = section.
            wall = gaps[0] if tube_by_geometry else None
            return {
                'part_type': 'tube', 'export_strategy': 'tube',
                'thickness_in': wall, 'tube_height_in': span if tube_by_geometry else None,
                # Tube is the riskiest call and the tube path differs the most,
                # so always route it past a human even when both signals agree.
                'confidence': 'high' if (tube_by_geometry and tube_by_name) else 'low',
                'needs_review': True, 'notes': notes,
            }

        if n == 2:
            return {
                'part_type': '2d', 'export_strategy': 'onshape_standard_dxf',
                'thickness_in': span, 'tube_height_in': None,
                'confidence': 'high', 'needs_review': False,
                'notes': ['Two parallel faces (top + bottom): simple through-cut '
                          '-> devolve to a standard Onshape DXF export.'],
            }

        return {
            'part_type': '2.5d', 'export_strategy': 'constructed_multilayer',
            'thickness_in': span, 'tube_height_in': None,
            'confidence': 'high', 'needs_review': False,
            'notes': [f'{n} distinct depths: pocketed/multi-level part '
                      '-> construct a multilayer DXF ourselves.'],
        }

    def classify_part(self, document_id, workspace_id, element_id,
                      version_id=None, microversion_id=None):
        """Infer how a user would set up a part in the wizard, from its geometry.

        Mirrors the live 2.5D path's analyze-first approach but with no user in
        the loop: auto-selects the largest planar face (instead of a click) and
        decides 2D vs 2.5D vs tube from the parallel-face depth bins (instead of
        a toggle). The result names the export_strategy Phase 3 dispatches on and
        carries the face_id/body_id/normal those export calls need.

        Returns a dict (see _classify_from_depths) augmented with source and
        selection fields, or a low-confidence 'unknown' entry on any failure.
        """
        def _unknown(reason):
            return {
                'part_type': 'unknown', 'export_strategy': None,
                'thickness_in': None, 'tube_height_in': None,
                'confidence': 'low', 'needs_review': True, 'notes': [reason],
                'face_id': None, 'body_id': None, 'face_normal': None,
                'part_name': None, 'depth_bins_in': [],
            }

        faces = self.list_faces(document_id, workspace_id, element_id,
                                version_id=version_id, microversion_id=microversion_id)
        if not faces:
            return _unknown('Onshape returned no face data for this element.')

        face_id, body_id, part_name, normal = self.auto_select_top_face(
            document_id, workspace_id, element_id, cached_faces_data=faces,
            version_id=version_id, microversion_id=microversion_id)
        if not face_id:
            return _unknown('No planar face found to use as the reference/top face.')

        # The selected face's plane origin is needed to measure depths from it.
        reference_origin = None
        for body in faces.get('bodies', []):
            for fc in body.get('faces', []):
                if fc.get('id') == face_id:
                    reference_origin = (fc.get('surface') or {}).get('origin')
                    break
            if reference_origin is not None:
                break
        if reference_origin is None:
            return _unknown('Could not resolve the reference face origin.')

        depth_bins = self.find_parallel_faces_by_depth(
            document_id, workspace_id, element_id, normal, reference_origin,
            body_id=body_id, cached_faces_data=faces,
            version_id=version_id, microversion_id=microversion_id)

        result = self._classify_from_depths(list(depth_bins.keys()), name_hint=part_name)
        result.update({
            'face_id': face_id, 'body_id': body_id, 'face_normal': normal,
            'part_name': part_name,
            'depth_bins_in': sorted((round(d, 4) for d in depth_bins.keys()), reverse=True),
        })
        return result

    def _convert_geometry_to_solid_hatch(self, source_msp, target_msp, layer_name):
        """
        Convert circles and polylines from source to solid HATCH entities in target.

        This represents each face as a solid filled region (negative space to remove)
        rather than stroked outlines. This makes slicing logic much simpler.

        Args:
            source_msp: Source modelspace with circles/lines/polylines
            target_msp: Target modelspace to add HATCH entities to
            layer_name: Layer name for the HATCH entities

        Returns:
            Number of HATCH entities created
        """

        # Extract all geometry
        circles = []
        polylines = []

        # Get circles
        for entity in source_msp.query('CIRCLE'):
            center = (entity.dxf.center.x, entity.dxf.center.y)
            radius = entity.dxf.radius
            circles.append({'center': center, 'radius': radius})

        # Get closed polylines
        for entity in source_msp.query('LWPOLYLINE'):
            if entity.closed:
                points = [(p[0], p[1]) for p in entity.get_points('xy')]
                if len(points) >= 3:
                    polylines.append(points)

        for entity in source_msp.query('POLYLINE'):
            if entity.is_2d_polyline and entity.is_closed:
                points = [(v.dxf.location.x, v.dxf.location.y) for v in entity.vertices]
                if len(points) >= 3:
                    polylines.append(points)

        # Stitch open LINE/ARC/ELLIPSE/SPLINE + unclosed-LWPOLYLINE entities into closed
        # boundary loops via the shared dxf_geometry stitcher (same code the 2D importer
        # uses). It snaps sub-micron-apart CAD endpoints together so linemerge can close
        # a boundary; full ellipses come back as their own closed loop.
        def _warn_open_loop(coords, gap):
            # A large loop that failed to close is exactly how a missing outer profile
            # hides - surface it rather than silently dropping the boundary.
            span_x = max(c[0] for c in coords) - min(c[0] for c in coords)
            span_y = max(c[1] for c in coords) - min(c[1] for c in coords)
            if max(span_x, span_y) > 1.0:
                log(f"    WARNING: dropped an UNCLOSED boundary loop "
                    f"({len(coords)} pts, {span_x:.2f}x{span_y:.2f}\", "
                    f"end-gap {gap:.4f}\") - geometry may be incomplete")

        stitched = entities_to_closed_paths(
            lines=list(source_msp.query('LINE')),
            arcs=list(source_msp.query('ARC')),
            ellipses=list(source_msp.query('ELLIPSE')),
            splines=list(source_msp.query('SPLINE')),
            polylines=[e for e in source_msp.query('LWPOLYLINE') if not e.closed],
            on_open_loop=_warn_open_loop,
        )
        for coords in stitched:
            polylines.append(coords)
            log(f"    Stitched segments into closed path ({len(coords)} points)")

        log(f"    Converting to solid regions: {len(circles)} circles, {len(polylines)} polylines")

        if not circles and not polylines:
            log(f"    No geometry to convert")
            return 0

        # Detect concentric circles and convert to rings BEFORE unioning
        # This is critical for circular grooves/rings
        geoms = []
        used_circles = set()

        for i, circle1 in enumerate(circles):
            if i in used_circles:
                continue

            center1 = circle1['center']
            radius1 = circle1['radius']

            # Look for concentric circles (same center, different radius)
            concentric_group = [circle1]
            for j, circle2 in enumerate(circles):
                if i == j or j in used_circles:
                    continue

                center2 = circle2['center']
                radius2 = circle2['radius']

                # Check if centers are the same (within tolerance)
                dx = abs(center1[0] - center2[0])
                dy = abs(center1[1] - center2[1])
                if dx < 0.001 and dy < 0.001 and abs(radius1 - radius2) > 0.001:
                    # Concentric!
                    concentric_group.append(circle2)
                    used_circles.add(j)

            used_circles.add(i)

            # Create geometry from this group
            if len(concentric_group) == 1:
                # Single circle - filled disk
                geom = Point(center1).buffer(radius1)
                geoms.append(geom)
            else:
                # Multiple concentric circles - create ring(s)
                # Sort by radius (largest first)
                concentric_group.sort(key=lambda c: c['radius'], reverse=True)

                # Outer boundary is the largest circle
                outer_geom = Point(concentric_group[0]['center']).buffer(concentric_group[0]['radius'])

                # Subtract all inner circles
                for inner_circle in concentric_group[1:]:
                    inner_geom = Point(inner_circle['center']).buffer(inner_circle['radius'])
                    outer_geom = outer_geom.difference(inner_geom)

                if not outer_geom.is_empty:
                    geoms.append(outer_geom)
                    log(f"      Detected concentric circles: outer r={concentric_group[0]['radius']:.3f}\", "
                        f"{len(concentric_group)-1} inner hole(s) - created ring")

        # Add polylines as filled polygons. Repair rather than discard: these are raw
        # CAD boundary loops, and in this negative-space representation a lost loop is a
        # window that never gets cut (see polygon_from_path).
        for polyline in polylines:
            poly, _ = polygon_from_path(polyline)
            if poly is None:
                log(f"    WARNING: boundary loop with {len(polyline)} points encloses "
                    f"no area - a feature may be missing from this part")
                continue
            geoms.append(poly)

        # Containment-aware union: if a smaller polygon is fully inside a larger one,
        # it represents a hole boundary (e.g., a circle inside a rectangle), not a
        # filled region. Naive unary_union would lose the hole since union(A, B) = A
        # when B is contained in A.
        if geoms:
            geoms.sort(key=lambda g: g.area, reverse=True)

            result_geoms = []
            used_as_hole = set()

            for i, outer in enumerate(geoms):
                if i in used_as_hole:
                    continue

                current = outer
                for j in range(i + 1, len(geoms)):
                    if j in used_as_hole:
                        continue
                    if current.contains(geoms[j]):
                        current = current.difference(geoms[j])
                        used_as_hole.add(j)
                        log(f"      Detected hole: subtracted contained geometry (area={geoms[j].area:.4f})")

                result_geoms.append(current)

            union = unary_union(result_geoms)

            # Convert union back to HATCH entities
            hatch_count = 0

            if union.geom_type == 'Polygon':
                hatch_count += self._create_hatch_from_polygon(union, target_msp, layer_name)
            elif union.geom_type == 'MultiPolygon':
                for poly in union.geoms:
                    hatch_count += self._create_hatch_from_polygon(poly, target_msp, layer_name)

            log(f"    Created {hatch_count} solid HATCH entities")
            return hatch_count

        return 0

    def _create_hatch_from_polygon(self, polygon, msp, layer_name):
        """
        Create a HATCH entity from a Shapely polygon.
        Polygon may have holes (interior rings).

        Returns:
            Number of HATCHes created (1 if successful, 0 otherwise)
        """
        try:
            # Create HATCH entity
            hatch = msp.add_hatch(color=7, dxfattribs={'layer': layer_name})

            # Add exterior boundary (EXTERNAL flag set by default)
            exterior_coords = list(polygon.exterior.coords)
            hatch.paths.add_polyline_path(exterior_coords, is_closed=True)

            # Add interior holes (NO EXTERNAL flag - marks them as holes, not outer boundaries)
            # DXF path_type_flags: bit 0 (1) = EXTERNAL, bit 1 (2) = POLYLINE
            # For holes: flags=0 (no EXTERNAL bit)
            for interior in polygon.interiors:
                interior_coords = list(interior.coords)
                hatch.paths.add_polyline_path(interior_coords, is_closed=True, flags=0)

            return 1
        except Exception as e:
            log(f"      Warning: Could not create HATCH: {e}")
            return 0

    def merge_dxfs_with_layers(self, dxf_contents_by_depth, depth_metadata=None):
        """
        Merge multiple DXF contents into one with depth-encoded layer names.
        Converts geometry to solid HATCH entities representing negative space.

        Args:
            dxf_contents_by_depth: Dict {depth: dxf_bytes}
            depth_metadata: Dict {depth: {'offset_x': float, 'offset_y': float}} for coordinate alignment

        Returns:
            Merged DXF content as bytes
        """

        log(f"\n{'='*70}")
        log(f"MERGING DXFs WITH LAYER NAMES (AS SOLID REGIONS)")
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

                # Apply translation to source geometry if needed
                if offset_x != 0 or offset_y != 0:
                    log(f"  Translating source geometry by ({offset_x:.4f}, {offset_y:.4f})")
                    for entity in source_msp:
                        try:
                            entity.translate(offset_x, offset_y, 0)
                        except Exception:
                            pass

                # Convert geometry to solid HATCH entities
                hatch_count = self._convert_geometry_to_solid_hatch(source_msp, merged_msp, layer_name)

                if hatch_count > 0:
                    log(f"  Added {hatch_count} solid regions to layer {layer_name}")
                else:
                    log(f"  Warning: No solid regions created for layer {layer_name}")

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

        # Optionally dump the merged DXF for inspection. Off by default so production 2.5D
        # imports don't write to a world-readable /tmp path on every request; set
        # PENGUINCAM_DEBUG_DXF=1 to enable locally.
        if os.environ.get('PENGUINCAM_DEBUG_DXF'):
            debug_path = "/tmp/debug_merged.dxf"
            with open(debug_path, "wb") as f:
                f.write(merged_bytes)
            log(f"DEBUG: Saved merged DXF to {debug_path}")

        return merged_bytes

    def export_multilayer_dxf(self, document_id, workspace_id, element_id,
                             reference_face_id, reference_body_id, reference_normal, reference_origin,
                             body_id=None, cached_faces_data=None,
                             version_id=None, microversion_id=None):
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
        log(f"Reference normal: {reference_normal}")
        log(f"Reference origin: {reference_origin}")
        log(f"Reference face ID: {reference_face_id}")

        # Find all parallel faces grouped by depth
        # Use tight tolerance (1 mil) to avoid grouping distinct layers
        depth_bins = self.find_parallel_faces_by_depth(
            document_id, workspace_id, element_id,
            reference_normal, reference_origin,
            body_id, cached_faces_data,
            depth_tolerance=0.001,  # 0.001" = 1 mil tolerance
            version_id=version_id, microversion_id=microversion_id
        )

        if not depth_bins:
            log("No parallel faces found")
            return None

        # CRITICAL: Find the depth of the selected reference face
        # The selected face must ALWAYS end up at the TOP (maximum Z)
        reference_depth = None
        for depth, faces in depth_bins.items():
            for face in faces:
                if face['face_id'] == reference_face_id:
                    reference_depth = depth
                    log(f"\n📍 Found reference face {reference_face_id} at depth {depth:+.4f}\"")
                    break
            if reference_depth is not None:
                break

        if reference_depth is None:
            log(f"⚠️  Reference face {reference_face_id} not found in depth bins!")
            log(f"   Available faces: {[f['face_id'] for faces in depth_bins.values() for f in faces]}")
            # Continue anyway, fall back to old logic
        else:
            # Check if reference face has the maximum depth
            # If not, we need to flip all depths so it becomes the maximum
            max_depth = max(depth_bins.keys())
            min_depth = min(depth_bins.keys())

            log(f"   Current depth range: {min_depth:+.4f}\" to {max_depth:+.4f}\"")
            log(f"   Reference face is at: {reference_depth:+.4f}\"")

            # If reference is closer to min than max, flip all depths
            # This ensures the selected face ends up at the TOP
            if abs(reference_depth - min_depth) < abs(reference_depth - max_depth):
                log(f"⚠️  Reference face is closer to MIN depth (should be at TOP)")
                log(f"   Negating all depths to put reference face at maximum")
                corrected_bins = {}
                for depth, faces in depth_bins.items():
                    corrected_depth = -depth
                    log(f"   {depth:+.4f}\" → {corrected_depth:+.4f}\"")
                    corrected_bins[corrected_depth] = faces
                depth_bins = corrected_bins
                log(f"   Reference face now at: {-reference_depth:+.4f}\"")
            else:
                log(f"✅ Reference face is already at or near maximum depth (will be at TOP)")

        # Export each depth group
        dxf_contents = {}


        def export_depth_group(depth, faces):
            """Export a single depth group"""
            face_ids = [f['face_id'] for f in faces]
            face_ids_str = ','.join(face_ids)

            log(f"\nExporting depth {depth:.4f}\" ({len(faces)} faces): {face_ids_str}")

            # Use the existing export method with comma-separated face IDs
            # We'll modify export_face_to_dxf to accept multiple IDs
            return depth, self._export_faces_group_to_dxf(
                document_id, workspace_id, element_id,
                face_ids_str, reference_normal,
                version_id=version_id, microversion_id=microversion_id
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

        # Stash the raw per-depth DXFs (exactly as Onshape returned them, before
        # _convert_geometry_to_solid_hatch reconstructs geometry) so the Flask layer
        # can expose them for debugging the geometry pipeline.
        self.last_raw_depth_dxfs = dict(dxf_contents)

        # No coordinate translation needed
        # Onshape exports each depth group with faces at their correct relative positions
        # The face 'origin' field is a plane equation reference point, not a geometric centroid
        # So we can't use it for positioning. Fortunately, Onshape preserves the relative
        # geometry within each exported DXF, so we just use zero offsets for all layers.
        depth_metadata = {}

        log(f"\nUsing zero offsets for all layers (geometry already correctly positioned in each DXF):")

        for depth, faces in depth_bins.items():
            # Use zero offset - the DXF geometry is already correctly positioned
            depth_metadata[depth] = {
                'offset_x': 0.0,
                'offset_y': 0.0
            }

            log(f"  Depth {depth:.4f}\": {len(faces)} faces, offset (0.0000, 0.0000)")

        # Calculate part thickness from depth bins (shared formula with the 2D probe).
        # Depths are signed distances from reference (top) face:
        # reference face ≈ 0, bottom face ≈ -thickness (or +thickness if flipped).
        depths = list(depth_bins.keys())
        detected_thickness = self._thickness_from_depth_bins(depth_bins)
        if depths:
            min_depth = min(depths)  # Deepest (bottom face, typically negative)
            thickness_str = f"{detected_thickness:.4f}" if detected_thickness is not None else "unknown"
            log(f"\n📏 Detected part thickness: {thickness_str}\" (from Z={max(depths):+.4f}\" to Z={min_depth:+.4f}\")")

            # COORDINATE SYSTEM TRANSFORMATION
            # Transform from "distance from reference face" to "height above sacrifice board"
            # Old: reference face = 0, bottom face = -thickness (or vice versa)
            # New: bottom (on sacrifice board) = 0, top = thickness
            log(f"\n🔄 Transforming coordinate system to Z=0 at sacrifice board")
            transformed_bins = {}
            transformed_metadata = {}
            transformed_contents = {}

            for depth, faces in depth_bins.items():
                # Transform: new_z = old_z - min_depth
                # This makes bottom (min_depth) -> 0 and top (max_depth) -> thickness
                new_depth = depth - min_depth
                log(f"   Z={depth:+.4f}\" → Z={new_depth:.4f}\"")
                transformed_bins[new_depth] = faces
                transformed_contents[new_depth] = dxf_contents[depth]
                if depth in depth_metadata:
                    transformed_metadata[new_depth] = depth_metadata[depth]

            # Replace with transformed values
            depth_bins = transformed_bins
            dxf_contents = transformed_contents
            depth_metadata = transformed_metadata
        else:
            log("\n⚠️  Could not detect part thickness (no depth bins)")

        # Merge DXFs with layer names and coordinate alignment
        merged_dxf = self.merge_dxfs_with_layers(dxf_contents, depth_metadata)

        # Store thickness as metadata in the DXF object (we'll pass it back separately)
        # For now, just return both values
        return merged_dxf, detected_thickness

    def _export_faces_group_to_dxf(self, document_id, workspace_id, element_id, face_ids_str, face_normal=None,
                                   version_id=None, microversion_id=None):
        """
        Export multiple faces as a single DXF (helper for multi-layer export)

        Args:
            face_ids_str: Comma-separated face IDs (e.g., "JHD,JHE,JHF")
            face_normal: Optional dict with face normal vector

        Returns:
            DXF file content as bytes, or None if failed
        """
        endpoint = f"/documents/{self._wvm_path(document_id, workspace_id, version_id, microversion_id)}/e/{element_id}/exportinternal"

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
            log(traceback.format_exc())
            return None
    
    def list_folder_documents(self, folder_id):
        """List documents directly contained in an Onshape folder (non-recursive).

        Uses the global tree nodes API with offset pagination. Subfolders are
        ignored here (recursion can be layered on later if the corpus grows a
        nested structure).

        Returns:
            list of {'id', 'name'} dicts, one per document node.
        """
        documents = []
        offset = 0
        limit = 50
        while True:
            endpoint = (
                f'/globaltreenodes/folder/{folder_id}'
                f'?getPathToRoot=false&offset={offset}&limit={limit}'
            )
            response = self._make_api_request('GET', endpoint)
            if response.status_code != 200:
                log(f"Failed to list folder {folder_id}: "
                    f"HTTP {response.status_code} {response.text[:200]}")
                break
            items = response.json().get('items', [])
            for item in items:
                # Document nodes carry resourceType 'document' (subfolders are
                # 'folder'); jsonType is the belt-and-suspenders fallback.
                if (item.get('resourceType') == 'document'
                        or item.get('jsonType') == 'document-summary'):
                    documents.append({
                        'id': item.get('id'),
                        'name': item.get('name', 'unnamed'),
                    })
            if len(items) < limit:
                break
            offset += len(items)
        return documents

    def list_part_studios(self, document_id, workspace_id=None):
        """List Part Studio elements in a document's default (or given) workspace.

        Returns:
            list of {'id', 'name', 'workspace_id'} dicts, one per Part Studio.
        """
        if workspace_id is None:
            doc_info = self.get_document_info(document_id)
            if not doc_info:
                return []
            workspace_id = doc_info.get('defaultWorkspace', {}).get('id')
            if not workspace_id:
                log(f"No default workspace for document {document_id}")
                return []

        response = self._make_api_request(
            'GET', f'/documents/d/{document_id}/w/{workspace_id}/elements'
        )
        if response.status_code != 200:
            log(f"Failed to list elements for {document_id}: "
                f"HTTP {response.status_code}")
            return []

        studios = []
        for elem in response.json():
            if (elem.get('type') == 'Part Studio'
                    or elem.get('elementType') == 'PARTSTUDIO'):
                studios.append({
                    'id': elem.get('id'),
                    'name': elem.get('name', 'unnamed'),
                    'workspace_id': workspace_id,
                })
        return studios

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
            log(traceback.format_exc())
            return None

    def fetch_config_file(self, document_id=None):
        """
        Search for and fetch PenguinCAM-config.yaml from the classrooms
        (companies/teams) the authenticated user belongs to.

        The user is the source of truth for which classroom's config to
        use — the mentor of the user's classroom configures the machines
        the user has access to, regardless of where the active CAD part
        was designed. When the user belongs to multiple classrooms and
        has a config in more than one, the active document's classroom
        is used as a tie-breaker so that students working inside a
        particular team's documents land on that team's config; we
        never fall back to a foreign classroom's config just because a
        part was shared from there.

        Args:
            document_id: Optional Onshape document ID for the active
                export. Only used as a tie-breaker among configs in the
                user's own classrooms.

        Returns:
            str with raw YAML content, or None if not found or on error
        """
        try:
            log("\n🔍 Searching for PenguinCAM-config.yaml...")
            self.last_config_url = None

            user_companies = self.get_companies() or []
            user_classroom_ids = {c.get('id') for c in user_companies if c.get('id')}

            if not user_classroom_ids:
                log("   ❌ User belongs to no classrooms — PenguinCAM expects the config to live in a company/team-owned document")
                return None

            log(f"   User belongs to {len(user_classroom_ids)} classroom(s)")

            # Tie-breaker only: use the active document's classroom to disambiguate
            # when the user has configs in multiple of their own classrooms.
            preferred_owner_id = None
            if document_id:
                doc_info = self.get_document_info(document_id)
                if doc_info:
                    owner = doc_info.get('owner', {})
                    # owner type: 0 = user, 1 = company, 2 = team
                    if owner.get('type') in (1, 2) and owner.get('id') in user_classroom_ids:
                        preferred_owner_id = owner['id']
                        log(f"   Active document classroom: {owner.get('name', 'Unknown')}")

            # Onshape's /documents/search accepts a single ownerId per request,
            # so issue one scoped search per classroom and dedupe by doc ID.
            # Mirrors the Onshape UI's own search payload (ownerId, foundIn=w,
            # documentFilter=7) to avoid picking up publicly-shared configs
            # from unrelated teams.
            candidates = {}
            for owner_id in user_classroom_ids:
                search_body = {
                    'ownerId': owner_id,
                    'foundIn': 'w',
                    'when': 'latest',
                    'documentFilter': 7,
                    'rawQuery': '_all:PenguinCAM-config.yaml',
                    'limit': 50,
                }
                response = self._make_api_request('POST', '/documents/search', json=search_body)
                if response.status_code != 200:
                    log(f"   ⚠️  Search failed for owner {owner_id[:8]}: HTTP {response.status_code}")
                    continue
                items = response.json().get('items', [])
                log(f"   Owner {owner_id[:8]}: {len(items)} match(es)")
                for item in items:
                    item_id = item.get('id')
                    if item_id and item_id not in candidates:
                        candidates[item_id] = item

            if not candidates:
                log("   ℹ️  No PenguinCAM-config.yaml found in your classrooms")
                return None

            # Belt-and-suspenders: re-verify each candidate's owner via document
            # metadata, in case anything other than a user-classroom doc slipped
            # into a scoped search result.
            verified = []
            for item_id, item in candidates.items():
                doc_name = item.get('name', 'unknown')
                doc_info = self.get_document_info(item_id)
                if not doc_info:
                    continue
                owner = doc_info.get('owner', {})
                owner_type = owner.get('type')
                owner_id = owner.get('id')
                owner_name = owner.get('name', 'Unknown')
                log(f"   - Found: {doc_name} (ID: {item_id[:8]}, owner: {owner_name})")
                if owner_type in (1, 2) and owner_id in user_classroom_ids:
                    verified.append((item, owner_id))
                else:
                    log(f"     ✗ Owner not in user's classrooms (ignoring)")

            if not verified:
                log("   ❌ No PenguinCAM-config.yaml found in your classrooms after verification")
                return None

            def sort_key(entry):
                item, owner_id = entry
                is_preferred = preferred_owner_id is not None and owner_id == preferred_owner_id
                return (1 if is_preferred else 0, item.get('modifiedAt', ''))

            verified.sort(key=sort_key, reverse=True)
            config_doc, chosen_owner_id = verified[0]
            doc_id = config_doc.get('id')
            doc_name = config_doc.get('name', 'unknown')

            if len(verified) > 1:
                log(f"   ⚠️  {len(verified)} matching configs in your classrooms — picked one")

            log(f"   ✅ Using config: {doc_name} (ID: {doc_id[:8]}, owner: {chosen_owner_id[:8]})")

            # Get workspace ID from search results (v13 includes defaultWorkspace)
            log(f"   🔍 DEBUG: config_doc keys: {list(config_doc.keys())}")
            workspace_id = config_doc.get('defaultWorkspace', {}).get('id')
            log(f"   🔍 DEBUG: workspace_id from defaultWorkspace: {workspace_id}")
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

            log(f"   ✅ Using workspace: {workspace_id[:8]}...")

            # List elements to find the YAML file tab
            log(f"   🔍 DEBUG: Listing elements for doc {doc_id[:8]}, workspace {workspace_id[:8]}")
            response = self._make_api_request(
                'GET',
                f'/documents/d/{doc_id}/w/{workspace_id}/elements'
            )

            if response.status_code != 200:
                log(f"   ❌ Could not list elements: HTTP {response.status_code}")
                log(f"   Response: {response.text[:500]}")
                return None

            elements = response.json()
            log(f"   🔍 DEBUG: Got {len(elements)} elements")

            # Look for a Blob element with exact filename match
            config_element = None
            for elem in elements:
                elem_name = elem.get('name', '')
                elem_type = elem.get('type', '')
                log(f"   🔍 DEBUG: Element: {elem_name} (type: {elem_type})")
                # Match exact filename (case-insensitive)
                if (elem.get('type') == 'Blob' and
                    elem_name.lower() in ['penguincam-config.yaml', 'penguincam-config.yml']):
                    config_element = elem
                    log(f"   🔍 DEBUG: MATCH! Found config element")
                    break

            if not config_element:
                log("   ❌ No YAML element found in document")
                log(f"   Available elements: {[e.get('name') for e in elements]}")
                return None

            element_id = config_element.get('id')
            element_name = config_element.get('name')

            log(f"   ✅ Found YAML element: {element_name} (ID: {element_id[:8]}...)")

            # Download the blob content as text
            log(f"   🔍 DEBUG: Downloading blob element {element_id[:8]}...")
            response = self._make_api_request(
                'GET',
                f'/blobelements/d/{doc_id}/w/{workspace_id}/e/{element_id}'
            )

            log(f"   🔍 DEBUG: Blob download response status: {response.status_code}")
            if response.status_code != 200:
                log(f"   ❌ Could not download blob: HTTP {response.status_code}")
                log(f"   Response: {response.text[:500]}")
                return None

            # Return raw text content
            config_yaml = response.text
            self.last_config_url = f"{self.BASE_URL}/documents/{doc_id}/w/{workspace_id}/e/{element_id}"
            log(f"   ✅ Successfully fetched config file ({len(config_yaml)} bytes)")
            log(f"   🔍 DEBUG: Returning config_yaml (is None? {config_yaml is None})")

            return config_yaml

        except Exception as e:
            log(f"   ❌ EXCEPTION in fetch_config_file: {e}")
            log(f"   Full traceback:\n{traceback.format_exc()}")
            return None


def build_multilayer_dxf(client, did, wid, eid, face_id, body_id, face_normal,
                         cached_faces_data=None, version_id=None, microversion_id=None):
    """Build a single multi-layer (2.5D) DXF for the selected reference face.

    Resolves the reference face's normal (if not already known) and origin, then
    asks the Onshape client to bin all parallel faces by depth and merge them into
    one depth-layered DXF. Returns (dxf_bytes, detected_thickness).

    This is the sole home of the 2.5D DXF construction. The G-code path derives
    the actual stock thickness from the DXF layers themselves (see the
    postprocessor's load_dxf), so detected_thickness here is informational.
    Raises RuntimeError with a user-facing message on failure.

    Lives here (not in the Flask app) so both the web export route and the
    headless test harness construct 2.5D DXFs the exact same way.
    """
    faces_data = cached_faces_data or client.list_faces(
        did, wid, eid, version_id=version_id, microversion_id=microversion_id)
    if not faces_data:
        raise RuntimeError("Failed to retrieve face data from Onshape. Your "
                           "authentication token may have expired. Please "
                           "re-authenticate with Onshape.")

    # Locate the selected reference face (for the normal fallback + origin).
    reference_face = None
    for body in faces_data.get('bodies', []):
        if body_id and body.get('id') != body_id:
            continue
        for face in body.get('faces', []):
            if face.get('id') == face_id:
                reference_face = face
                break
        if reference_face:
            break

    if reference_face is None:
        raise RuntimeError("Could not find the selected face for multi-layer "
                           "export. Please select a flat top face.")

    surface = reference_face.get('surface', {}) or {}
    if not face_normal:
        face_normal = surface.get('normal', {'x': 0, 'y': 0, 'z': 1})
    reference_origin = surface.get('origin', {'x': 0, 'y': 0, 'z': 0})

    result = client.export_multilayer_dxf(
        did, wid, eid,
        face_id, body_id, face_normal, reference_origin,
        body_id=body_id, cached_faces_data=faces_data,
        version_id=version_id, microversion_id=microversion_id
    )
    # Backwards-compatible unpack if the client doesn't return thickness.
    if isinstance(result, tuple):
        dxf_bytes, detected_thickness = result
    else:
        dxf_bytes, detected_thickness = result, None

    if not dxf_bytes:
        raise RuntimeError("Onshape returned no multi-layer DXF for that face. "
                           "Make sure the selected face is a flat top face of a "
                           "2.5D part.")
    return dxf_bytes, detected_thickness


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

    def update_session_tokens(self, client):
        """
        Update session tokens after potential refresh.

        Call this after making API calls with a client to ensure
        refreshed tokens are saved back to the session.

        Args:
            client: OnshapeClient that may have refreshed tokens
        """
        if not client:
            return

        # Update session with potentially-refreshed tokens
        session['onshape_tokens'] = {
            'access_token': client.access_token,
            'refresh_token': client.refresh_token,
            'expires_at': client.token_expires.isoformat() if client.token_expires else None,
            'created': session.get('onshape_tokens', {}).get('created', datetime.now().isoformat())
        }

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

"""
Google Drive Integration for FRC CAM GUI
Saves G-code files directly to team's shared Google Drive
"""

import os
import json
import pickle
from pathlib import Path
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError

# Scopes needed - only Drive file access
SCOPES = ['https://www.googleapis.com/auth/drive.file']

# Where to store credentials and tokens
CREDENTIALS_FILE = 'credentials.json'  # Download from Google Cloud Console
TOKEN_FILE = 'token.pickle'  # Auto-generated after first auth

class GoogleDriveUploader:
    """Handles uploading files to Google Drive"""
    
    def __init__(self):
        self.service = None
        self.config = self._load_config()
        
    def _load_config(self):
        """Load drive configuration (folder IDs, etc.)"""
        config_file = 'drive_config.json'
        if os.path.exists(config_file):
            with open(config_file, 'r') as f:
                return json.load(f)
        return {
            'shared_drive_name': 'Popcorn Penguins',
            'folder_path': 'CNC/G-code',  # Path within the shared drive
            'folder_id': None  # Will be found automatically
        }
    
    def _save_config(self):
        """Save configuration"""
        with open('drive_config.json', 'w') as f:
            json.dump(self.config, f, indent=2)
    
    def authenticate(self):
        """Authenticate with Google Drive"""
        creds = None
        
        # Token file stores user's access and refresh tokens
        if os.path.exists(TOKEN_FILE):
            with open(TOKEN_FILE, 'rb') as token:
                creds = pickle.load(token)
        
        # If no valid credentials, let user log in
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not os.path.exists(CREDENTIALS_FILE):
                    raise FileNotFoundError(
                        f"Missing {CREDENTIALS_FILE}. "
                        "Please download from Google Cloud Console. "
                        "See GOOGLE_DRIVE_SETUP.md for instructions."
                    )
                
                flow = InstalledAppFlow.from_client_secrets_file(
                    CREDENTIALS_FILE, SCOPES)
                creds = flow.run_local_server(port=0)
            
            # Save credentials for next time
            with open(TOKEN_FILE, 'wb') as token:
                pickle.dump(creds, token)
        
        self.service = build('drive', 'v3', credentials=creds)
        return True
    
    def find_shared_drive(self, drive_name):
        """Find a shared drive by name"""
        try:
            results = self.service.drives().list(
                pageSize=100
            ).execute()
            
            drives = results.get('drives', [])
            for drive in drives:
                if drive['name'] == drive_name:
                    return drive['id']
            
            return None
        except HttpError as error:
            print(f"Error finding shared drive: {error}")
            return None
    
    def find_folder_in_drive(self, drive_id, folder_path):
        """
        Find a folder by path within a shared drive
        folder_path example: "CNC/G-code" 
        """
        folder_names = folder_path.split('/')
        current_folder_id = None
        
        for folder_name in folder_names:
            query_parts = [
                f"name='{folder_name}'",
                "mimeType='application/vnd.google-apps.folder'",
                "trashed=false"
            ]
            
            if current_folder_id:
                query_parts.append(f"'{current_folder_id}' in parents")
            
            query = ' and '.join(query_parts)
            
            try:
                results = self.service.files().list(
                    q=query,
                    spaces='drive',
                    corpora='drive',
                    driveId=drive_id,
                    includeItemsFromAllDrives=True,
                    supportsAllDrives=True,
                    fields='files(id, name)'
                ).execute()
                
                folders = results.get('files', [])
                if not folders:
                    # Folder doesn't exist
                    return None
                
                current_folder_id = folders[0]['id']
                
            except HttpError as error:
                print(f"Error finding folder '{folder_name}': {error}")
                return None
        
        return current_folder_id
    
    def create_folder(self, drive_id, parent_folder_id, folder_name):
        """Create a folder in the shared drive"""
        try:
            file_metadata = {
                'name': folder_name,
                'mimeType': 'application/vnd.google-apps.folder',
                'driveId': drive_id,
                'parents': [parent_folder_id] if parent_folder_id else []
            }
            
            folder = self.service.files().create(
                body=file_metadata,
                supportsAllDrives=True,
                fields='id'
            ).execute()
            
            return folder['id']
            
        except HttpError as error:
            print(f"Error creating folder: {error}")
            return None
    
    def upload_file(self, file_path, filename=None):
        """
        Upload a file to the configured Google Drive folder
        
        Args:
            file_path: Path to the file to upload
            filename: Optional custom filename (uses file_path name if not provided)
        
        Returns:
            dict with 'success', 'file_id', 'web_link', and 'message'
        """
        if not self.service:
            if not self.authenticate():
                return {
                    'success': False,
                    'message': 'Authentication failed'
                }
        
        # Find or set up the shared drive and folder
        drive_name = self.config.get('shared_drive_name', 'Popcorn Penguins')
        drive_id = self.find_shared_drive(drive_name)
        
        if not drive_id:
            return {
                'success': False,
                'message': f"Shared drive '{drive_name}' not found. "
                          f"Make sure you have access to it."
            }
        
        # Find the target folder
        folder_path = self.config.get('folder_path', 'CNC/G-code')
        folder_id = self.config.get('folder_id')
        
        if not folder_id:
            folder_id = self.find_folder_in_drive(drive_id, folder_path)
            if folder_id:
                self.config['folder_id'] = folder_id
                self._save_config()
        
        if not folder_id:
            return {
                'success': False,
                'message': f"Folder '{folder_path}' not found in '{drive_name}'. "
                          f"Please create it manually or update drive_config.json"
            }
        
        # Upload the file
        try:
            if not filename:
                filename = os.path.basename(file_path)
            
            file_metadata = {
                'name': filename,
                'parents': [folder_id],
                'driveId': drive_id
            }
            
            media = MediaFileUpload(file_path, resumable=True)
            
            file = self.service.files().create(
                body=file_metadata,
                media_body=media,
                supportsAllDrives=True,
                fields='id, name, webViewLink'
            ).execute()
            
            return {
                'success': True,
                'file_id': file['id'],
                'web_link': file.get('webViewLink', ''),
                'message': f"✅ Saved to {drive_name}/{folder_path}/{filename}"
            }
            
        except HttpError as error:
            return {
                'success': False,
                'message': f"Upload failed: {str(error)}"
            }
    
    def is_configured(self):
        """Check if Google Drive is set up and ready"""
        if not os.path.exists(CREDENTIALS_FILE):
            return False, "Missing credentials.json - see GOOGLE_DRIVE_SETUP.md"
        
        try:
            if not self.service:
                self.authenticate()
            return True, "Google Drive ready"
        except Exception as e:
            return False, f"Authentication error: {str(e)}"


# Convenience function
def upload_gcode_to_drive(file_path, filename=None):
    """
    Upload a G-code file to the team's Google Drive
    
    Args:
        file_path: Path to the G-code file
        filename: Optional custom filename
    
    Returns:
        dict with upload result
    """
    uploader = GoogleDriveUploader()
    return uploader.upload_file(file_path, filename)

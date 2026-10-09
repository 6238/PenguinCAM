"""
Google Drive Integration for FRC CAM GUI
Saves G-code files directly to a team's Google Drive folder

Scope note: this module operates under `drive.file` only (see PenguinCAMAuth.SCOPES).
That grants access to files this app creates -- it cannot browse, search, or resolve
folders by name or path. The destination is therefore always an explicit folder ID
supplied by the team's config, never discovered at runtime.
"""

import os
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError
from logging_config import log
from team_config import extract_drive_folder_id  # one shared URL-or-ID normalizer  # shared log() + logging setup (was duplicated per module)

# Scope needed - per-file access to files we create (non-sensitive; no CASA audit)
SCOPES = ['https://www.googleapis.com/auth/drive.file']


def default_folder_id():
    """
    Server-wide fallback destination folder, if one is configured.

    Teams normally supply their own folder via PenguinCAM-config.yaml; this only
    covers deployments running without team config.
    """
    # Check both GOOGLE_DRIVE_FOLDER_ID (preferred) and DRIVE_FOLDER_ID (legacy).
    # Normalized so a pasted Drive URL works here too, same as in team config.
    return extract_drive_folder_id(
        os.environ.get('GOOGLE_DRIVE_FOLDER_ID') or os.environ.get('DRIVE_FOLDER_ID'))


class GoogleDriveUploader:
    """Handles uploading files to Google Drive"""

    def __init__(self, credentials=None, folder_id=None):
        """
        Initialize with credentials from session

        Args:
            credentials: Google OAuth2 credentials object
            folder_id: Destination Drive folder ID (from the team's config). Falls back
                       to the server-wide default only if not supplied. Must be passed
                       per request -- it is per-team state and must never be cached on
                       the server, which is shared by every team.
        """
        self.service = None
        self.credentials = credentials
        self.folder_id = folder_id or default_folder_id()

    def authenticate(self):
        """
        Use provided credentials to build Drive service
        Returns True if successful
        """
        if not self.credentials:
            return False

        try:
            self.service = build('drive', 'v3', credentials=self.credentials)
            return True
        except Exception as e:
            log(f"Drive authentication error: {e}")
            return False

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

        if not self.folder_id:
            return {
                'success': False,
                'message': 'No Google Drive folder configured. '
                           'Set google_drive_folder_id in PenguinCAM-config.yaml.'
            }

        try:
            if not filename:
                filename = os.path.basename(file_path)

            # parents + supportsAllDrives is enough for shared drives; driveId is not
            # needed and cannot be resolved under drive.file anyway.
            file_metadata = {
                'name': filename,
                'parents': [self.folder_id]
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
                'message': f"✅ Saved {filename} to Google Drive"
            }

        except HttpError as error:
            # 404 on create almost always means the folder ID is wrong or this user
            # lacks access to it -- say so, rather than leaking the raw API error.
            if error.resp.status in (403, 404):
                return {
                    'success': False,
                    'message': f"Could not write to the configured Drive folder. "
                               f"Check that google_drive_folder_id is correct and that "
                               f"you have edit access to that folder."
                }
            return {
                'success': False,
                'message': f"Upload failed: {str(error)}"
            }

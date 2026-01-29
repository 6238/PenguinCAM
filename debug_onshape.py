#!/usr/bin/env python3
"""
Debug script to test Onshape API calls
Run this to see what the Onshape API is actually returning
"""

import sys
import os

# Add current directory to path so we can import our modules
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from onshape_integration import session_manager, get_onshape_client
from flask import Flask, session

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'debug-key')

def test_face_selection(document_id, workspace_id, element_id, body_id=None):
    """Test the face selection logic"""

    print("\n" + "="*70)
    print("ONSHAPE FACE SELECTION DEBUG")
    print("="*70)

    with app.test_request_context():
        # Get client from session
        user_id = 'default_user'
        client = session_manager.get_client(user_id)

        if not client:
            print("❌ No Onshape session found!")
            print("   Please authenticate first by using the web app")
            return False

        print(f"\n✅ Found Onshape session")
        print(f"   Access token: {client.access_token[:20]}...")

        # Test list_faces
        print(f"\n{'='*70}")
        print(f"TEST 1: List all faces in element")
        print(f"{'='*70}")

        faces_data = client.list_faces(document_id, workspace_id, element_id)

        if not faces_data:
            print("❌ list_faces failed - returned None")
            return False

        # Test auto_select_top_face
        print(f"\n{'='*70}")
        print(f"TEST 2: Auto-select top face")
        print(f"{'='*70}")

        face_id, body_id_result, part_name, normal = client.auto_select_top_face(
            document_id, workspace_id, element_id, body_id
        )

        if not face_id:
            print("❌ auto_select_top_face failed - returned None")
            return False

        print(f"\n✅ All tests passed!")
        return True

if __name__ == '__main__':
    if len(sys.argv) < 4:
        print("Usage: python debug_onshape.py <document_id> <workspace_id> <element_id> [body_id]")
        print("\nExample:")
        print("  python debug_onshape.py d6894428a9514fd8ba2081e2 abc123 def456")
        sys.exit(1)

    document_id = sys.argv[1]
    workspace_id = sys.argv[2]
    element_id = sys.argv[3]
    body_id = sys.argv[4] if len(sys.argv) > 4 else None

    success = test_face_selection(document_id, workspace_id, element_id, body_id)

    sys.exit(0 if success else 1)

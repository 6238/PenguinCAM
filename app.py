"""
Vercel entrypoint for PenguinCAM Flask application.

Vercel expects a file named 'app.py' that exports the Flask app instance
as 'app'. This file imports from the main application module.
"""

from frc_cam_gui_app import app

# Vercel will use this 'app' variable as the WSGI application
# No need to call app.run() - Vercel handles that

if __name__ == '__main__':
    # For local testing with: python app.py
    import os
    port = int(os.environ.get('PORT', 6238))
    app.run(host='0.0.0.0', port=port, debug=True)

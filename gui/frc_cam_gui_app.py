#!/usr/bin/env python3
"""
FRC CAM Post-Processor - Web GUI
A Flask-based web interface for generating G-code from DXF files
"""

from flask import Flask, render_template, request, jsonify, send_file
import os
import sys
import subprocess
import tempfile
import shutil
from pathlib import Path
import json

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB max file size

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
def index():
    """Render the main GUI page"""
    return render_template('index.html')

@app.route('/process', methods=['POST'])
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
        thickness = float(request.form.get('thickness', 0.25))
        tool_diameter = float(request.form.get('tool_diameter', 0.157))
        sacrifice_depth = float(request.form.get('sacrifice_depth', 0.02))
        tabs = int(request.form.get('tabs', 4))
        drill_screws = request.form.get('drill_screws', 'false') == 'true'
        
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
                'drill_screws': drill_screws
            }
        })
        
    except ValueError as e:
        return jsonify({'error': f'Invalid parameter value: {str(e)}'}), 400
    except subprocess.TimeoutExpired:
        return jsonify({'error': 'Processing timeout - file too complex'}), 500
    except Exception as e:
        return jsonify({'error': f'Unexpected error: {str(e)}'}), 500

@app.route('/download/<filename>')
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

def cleanup():
    """Clean up temporary files on shutdown"""
    try:
        shutil.rmtree(TEMP_DIR)
    except:
        pass

import atexit
atexit.register(cleanup)

if __name__ == '__main__':
    print("="*70)
    print("FRC CAM Post-Processor - Web GUI")
    print("="*70)
    print(f"\nPost-processor script: {POST_PROCESSOR}")
    print(f"Temporary directory: {TEMP_DIR}")
    print("\n🚀 Starting server...")
    print("📂 Open your browser to: http://localhost:6238")
    print("\n⚠️  Press Ctrl+C to stop the server\n")
    print("="*70)
    
    app.run(debug=True, host='0.0.0.0', port=6238)

// @ts-check

// ============================================================================
// Part Selection Modal
// ============================================================================

function selectPart() {
    const selected = document.querySelector('input[name="partSelection"]:checked');
    if (selected) {
        const bodyId = selected.value;
        const url = new URL(window.location.href);
        url.searchParams.set('bodyId', bodyId);
        window.location.href = url.toString();
    }
}

// Handle part option selection (visual feedback)
document.addEventListener('DOMContentLoaded', () => {
    const partOptions = document.querySelectorAll('.part-option');
    partOptions.forEach(option => {
        option.addEventListener('click', () => {
            partOptions.forEach(opt => opt.classList.remove('selected'));
            option.classList.add('selected');
        });
    });
});

// Global state
let uploadedFile = null;
let suggestedFilename = null; // For OnShape imports
let gcodeContent = null;
let outputFilename = null;
let scene, camera, renderer, controls;
let optimalCameraPosition = { x: 10, y: 10, z: 10 };
let optimalLookAtPosition = { x: 0, y: 0, z: 0 };

// DOM elements
const dropZone = document.getElementById('dropZone');
const fileInput = document.getElementById('fileInput');
const fileInfo = document.getElementById('fileInfo');
const fileName = document.getElementById('fileName');
const fileSize = document.getElementById('fileSize');
const generateBtn = document.getElementById('generateBtn');
const downloadBtn = document.getElementById('downloadBtn');
const driveBtn = document.getElementById('driveBtn');
const driveStatus = document.getElementById('driveStatus');
const loading = document.getElementById('loading');
const results = document.getElementById('results');
const errorAlert = document.getElementById('errorAlert');
const errorMessage = document.getElementById('errorMessage');
const stats = document.getElementById('stats');
const consoleOutput = document.getElementById('consoleOutput');
const materialSelect = document.getElementById('material');
const tubeParams = document.getElementById('tubeParams');

// Handle material type selection - show/hide tube parameters
materialSelect.addEventListener('change', (e) => {
    const isAluminumTube = e.target.value === 'aluminum_tube';
    if (tubeParams) {
        tubeParams.style.display = isAluminumTube ? 'block' : 'none';
    }

    // Update thickness label, default value, and hide tabs for aluminum tube
    const thicknessGroup = document.getElementById('thickness')?.closest('.param-group');
    const thicknessLabel = thicknessGroup?.querySelector('label');
    const thicknessInput = document.getElementById('thickness');
    const tabsGroup = document.getElementById('tabs')?.closest('.param-group');

    if (thicknessLabel && thicknessInput) {
        if (isAluminumTube) {
            // Change label and default for tube mode
            thicknessLabel.innerHTML = `
                Tube Wall Thickness (inches)
                <span class="label-hint">1/8" = 0.125"</span>
            `;
            thicknessInput.value = '0.125';
        } else {
            // Standard label and default
            thicknessLabel.innerHTML = `
                Material Thickness (inches)
                <span class="label-hint">1/4" = 0.25</span>
            `;
            thicknessInput.value = '0.25';
        }
    }

    // Hide tabs for aluminum tube (not used)
    if (tabsGroup) tabsGroup.style.display = isAluminumTube ? 'none' : 'block';
});

// Check Google Drive availability
let driveAvailable = false;
async function checkDriveStatus() {
    try {
        const response = await fetch('/drive/status');
        const data = await response.json();
        
        if (data.available && data.configured) {
            driveAvailable = true;
            driveBtn.style.display = 'inline-block';
        } else if (data.available && !data.configured) {
            driveStatus.textContent = '⚠️ Google Drive not configured - see GOOGLE_DRIVE_SETUP.md';
            driveStatus.style.display = 'block';
            driveStatus.style.color = '#FFA500';
        }
    } catch (error) {
        // Drive integration not available - that's okay
        console.log('Google Drive integration not available');
    }
}
checkDriveStatus();


function formatFileSize(bytes) {
    if (bytes < 1024) return bytes + ' bytes';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}

// Generate G-code
generateBtn.addEventListener('click', async () => {
    if (!uploadedFile) return;

    const formData = new FormData();
    formData.append('file', uploadedFile);
    const material = document.getElementById('material').value;
    formData.append('material', material);
    formData.append('tool_diameter', document.getElementById('toolDiameter').value);
    formData.append('origin_corner', 'bottom-left'); // Always bottom-left

    // Add material-specific parameters
    if (material === 'aluminum_tube') {
        // Tube-specific parameters
        formData.append('thickness', document.getElementById('thickness').value); // Tube wall thickness
        formData.append('tube_height', document.getElementById('tubeHeight').value);
        formData.append('square_end', document.getElementById('squareEnd').checked ? '1' : '0');
        formData.append('cut_to_length', document.getElementById('cutToLength').checked ? '1' : '0');
    } else {
        // Standard parameters
        formData.append('thickness', document.getElementById('thickness').value);
        formData.append('tabs', document.getElementById('tabs').value);
    }
    formData.append('rotation', rotationAngle); // Add rotation angle
    if (suggestedFilename) {
        formData.append('suggested_filename', suggestedFilename); // OnShape filename
    }

    showLoading();
    hideError();
    hideResults();

    try {
        const response = await fetch('/process', {
            method: 'POST',
            body: formData
        });

        const data = await response.json();

        if (!response.ok) {
            // Include details if available
            const errorMsg = data.error || 'Unknown error';
            const details = data.details ? `\n\n${data.details}` : '';
            throw new Error(errorMsg + details);
        }

        gcodeContent = data.gcode;
        outputFilename = data.filename;

        // Show results
        showResults(data);

        // Switch to preview mode and visualize G-code
        switchMode('preview');
        visualizeGcode(data.gcode, toolpathMoves);

        // Enable download and drive buttons
        downloadBtn.disabled = false;
        if (driveAvailable) {
            driveBtn.disabled = false;
        }

    } catch (error) {
        if (Object.hasOwn(error, "details")) {
            console.error(error.details);
        }
        showError('Generation Failed', error.message);
    } finally {
        hideLoading();
    }
});

// Upload to Google Drive
driveBtn.addEventListener('click', async () => {
    if (!outputFilename) return;
    
    driveBtn.disabled = true;
    driveBtn.textContent = '⏳ Uploading...';
    driveStatus.style.display = 'none';
    
    try {
        const response = await fetch(`/drive/upload/${outputFilename}`, {
            method: 'POST'
        });
        
        const data = await response.json();
        
        if (data.success) {
            driveStatus.textContent = data.message;
            driveStatus.style.color = '#00D26A';
            driveStatus.style.display = 'block';
            driveBtn.textContent = '✅ Saved!';
            setTimeout(() => {
                driveBtn.textContent = '💾 Save to Google Drive';
                driveBtn.disabled = false;
            }, 3000);
        } else {
            driveStatus.textContent = '❌ ' + data.message;
            driveStatus.style.color = 'var(--error)';
            driveStatus.style.display = 'block';
            driveBtn.textContent = '💾 Save to Google Drive';
            driveBtn.disabled = false;
        }
    } catch (error) {
        driveStatus.textContent = '❌ Upload failed: ' + error.message;
        driveStatus.style.color = 'var(--error)';
        driveStatus.style.display = 'block';
        driveBtn.textContent = '💾 Save to Google Drive';
        driveBtn.disabled = false;
    }
});

// UI helpers
function showLoading() {
    loading.classList.add('show');
    generateBtn.disabled = true;
}

function hideLoading() {
    loading.classList.remove('show');
    generateBtn.disabled = false;
}

function showError(title, message) {
    errorAlert.classList.add('show');
    // Escape HTML but preserve newlines
    const escapedMessage = message
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/\n/g, '<br>');
    errorMessage.innerHTML = `<strong>${title}:</strong><br>${escapedMessage}`;
}

function hideError() {
    errorAlert.classList.remove('show');
}

function showResults(data) {
    results.classList.add('show');
    consoleOutput.textContent = data.console;

    // Parse statistics from console
    const lines = data.console.split('\n');
    const statsHtml = [];

    // Add cycle time if available
    if (data.cycle_time) {
        statsHtml.push(`<div class="stat"><div class="stat-label">⏱️ Estimated Time</div><div class="stat-value">${data.cycle_time}</div></div>`);
    }

    // Extract key info
    const holesMatch = data.console.match(/(\d+) millable holes/);
    const pocketsMatch = data.console.match(/and (\d+) pockets/);
    const linesMatch = data.console.match(/Total lines: (\d+)/);

    if (holesMatch) {
        statsHtml.push(`<div class="stat"><div class="stat-label">Holes</div><div class="stat-value">${holesMatch[1]}</div></div>`);
    }
    if (pocketsMatch) {
        statsHtml.push(`<div class="stat"><div class="stat-label">Pockets</div><div class="stat-value">${pocketsMatch[1]}</div></div>`);
    }
    if (linesMatch) {
        statsHtml.push(`<div class="stat"><div class="stat-label">G-code Lines</div><div class="stat-value">${linesMatch[1]}</div></div>`);
    }

    stats.innerHTML = statsHtml.join('');
}

function hideResults() {
    results.classList.remove('show');
}

// DXF Setup State
let currentMode = 'setup'; // 'setup' or 'preview'
let dxfGeometry = null; // Parsed DXF geometry
let rotationAngle = 0; // 0, 90, 180, 270 degrees
let dxfCanvas2D = null;
let dxfCtx2D = null;
let dxfBounds = null;

// Mode Switching
function switchMode(mode) {
    currentMode = mode;
    
    // Update mode buttons
    document.querySelectorAll('.mode-button').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.mode === mode);
    });
    
    // Show/hide appropriate views
    const setupContainer = document.getElementById('dxf-setup-container');
    const previewContainer = document.getElementById('canvas-container');
    const scrubberContainer = document.getElementById('scrubberContainer');
    const previewControls = document.getElementById('previewControls');
    const gcodeButtons = document.getElementById('gcodeButtons');
    const playbackControl = document.getElementById('playbackControls')
    
    if (mode === 'setup') {
        setupContainer.style.display = 'block';
        previewContainer.style.display = 'none';
        scrubberContainer.style.display = 'none';
        previewControls.style.display = 'none';
        gcodeButtons.style.display = 'none';
        playbackControl.style.display = 'none';
        
        // Resize canvas now that it's visible
        if (dxfCanvas2D && dxfGeometry) {
            setTimeout(() => {
                const rect = dxfCanvas2D.getBoundingClientRect();
                if (rect.width > 0 && rect.height > 0) {
                    dxfCanvas2D.width = rect.width;
                    dxfCanvas2D.height = rect.height;
                }
                renderDxfSetup();
            }, 0);
        } else if (dxfGeometry) {
            renderDxfSetup();
        }
    } else {
        playbackControl.style.display = 'flex';
        setupContainer.style.display = 'none';
        previewContainer.style.display = 'block';
        previewControls.style.display = 'flex';
        gcodeButtons.style.display = 'flex';
        // Scrubber visibility handled by visualizeGcode
    }
}

// G-code visualization
let toolpathMoves = []; // Array of moves for scrubber
let toolMesh = null; // 3D representation of cutting tool
let completedLine = null; // Line showing completed moves
let upcomingLine = null; // Line showing upcoming moves



// Initialize on load
// Initialize on load
window.addEventListener('load', () => {
    initVisualization();
    initDxfSetup();
    
    // DEBUG: Check if OnShape provides context via JavaScript
    console.log('=== OnShape Context Debug ===');
    console.log('window.opener:', window.opener);
    console.log('window.parent:', window.parent);
    console.log('URL params:', new URLSearchParams(window.location.search));
    console.log('OnShape globals:', {
        onshape: typeof window.onshape !== 'undefined' ? window.onshape : 'undefined',
        OnshapeClient: typeof window.OnshapeClient !== 'undefined' ? window.OnshapeClient : 'undefined'
    });
    
    // Check for error message from OnShape import
    const errorMessage = window.ONSHAPE_DATA?.errorMessage || '';
    if (errorMessage) {
        const statusDiv = document.getElementById('statusMessage');
        if (statusDiv) {
            statusDiv.textContent = '❌ ' + errorMessage;
            statusDiv.style.display = 'block';
            statusDiv.className = 'error';
        }
        return; // Don't try to load DXF
    }

    // Auto-load DXF if coming from OnShape
    const dxfFile = window.ONSHAPE_DATA?.dxfFile || '';
    const fromOnshape = window.ONSHAPE_DATA?.fromOnshape || false;
    const onshapeSuggestedFilename = window.ONSHAPE_DATA?.suggestedFilename || '';
    
    if (dxfFile && fromOnshape) {
        console.log('Auto-loading DXF from OnShape:', dxfFile);
        console.log('Fetching from:', `/uploads/${dxfFile}`);
        
        // Fetch the DXF and load it
        fetch(`/uploads/${dxfFile}`)
            .then(response => {
                console.log('Fetch response:', response.status, response.statusText);
                if (!response.ok) {
                    throw new Error(`HTTP ${response.status}: ${response.statusText}`);
                }
                return response.text();
            })
            .then(dxfContent => {
                console.log('DXF content received:', dxfContent.length, 'bytes');
                console.log('First 200 chars:', dxfContent.substring(0, 200));
                
                // Create a File object from the DXF content
                const blob = new Blob([dxfContent], { type: 'application/dxf' });
                const file = new File([blob], dxfFile, { type: 'application/dxf' });
                
                // Set file state and enable generate button
                uploadedFile = file;
                suggestedFilename = onshapeSuggestedFilename || null; // Store suggested name
                fileName.textContent = dxfFile;
                fileSize.textContent = formatFileSize(dxfContent.length);
                fileInfo.style.display = 'flex';
                generateBtn.disabled = false;
                generateBtn.textContent = '🚀 Generate G-code';
                hideError();
                hideResults();
                
                // Parse for 2D setup view
                parseDxfForSetup(dxfContent);
                
                // Show success message
                const statusDiv = document.getElementById('statusMessage');
                if (statusDiv) {
                    statusDiv.textContent = '✅ Imported from OnShape! Orient your part and click Generate G-code.';
                    statusDiv.style.display = 'block';
                    statusDiv.className = 'success';
                }
            })
            .catch(error => {
                console.error('Error loading DXF:', error);
                const statusDiv = document.getElementById('statusMessage');
                if (statusDiv) {
                    statusDiv.textContent = `❌ Failed to load DXF: ${error.message}`;
                    statusDiv.style.display = 'block';
                    statusDiv.className = 'error';
                }
            });
    }
});

// Handle window resize
window.addEventListener('resize', () => {
    const container = document.getElementById('canvas-container');
    camera.aspect = container.clientWidth / container.clientHeight;
    camera.updateProjectionMatrix();
    renderer.setSize(container.clientWidth, container.clientHeight);
    
    // Also resize DXF canvas to maintain correct aspect ratio
    if (dxfCanvas2D && dxfGeometry) {
        const rect = dxfCanvas2D.getBoundingClientRect();
        dxfCanvas2D.width = rect.width;
        dxfCanvas2D.height = rect.height;
        renderDxfSetup(); // Re-render with new size
    }
});

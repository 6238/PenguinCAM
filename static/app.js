// ============================================================================
// Application State
// ============================================================================

const appState = {
    // File upload
    uploadedFile: null,
    suggestedFilename: null,
    gcodeContent: null,
    outputFilename: null,

    // 3D Visualization
    scene: null,
    camera: null,
    renderer: null,
    controls: null,
    optimalCameraPosition: { x: 10, y: 10, z: 10 },
    optimalLookAtPosition: { x: 0, y: 0, z: 0 },

    // DXF Setup
    currentMode: 'setup',
    dxfGeometry: null,
    rotationAngle: 0,
    dxfCanvas2D: null,
    dxfCtx2D: null,
    dxfBounds: null,

    // Drive integration
    driveAvailable: false
};

// ============================================================================
// DXF Geometry Utilities
// ============================================================================

/**
 * Check if an angle is within an arc's angular range
 * Handles arcs that cross the 0° boundary
 */
function angleInArcRange(angle, startAngle, endAngle) {
    // Normalize angles to 0-360
    angle = ((angle % 360) + 360) % 360;
    startAngle = ((startAngle % 360) + 360) % 360;
    endAngle = ((endAngle % 360) + 360) % 360;

    if (startAngle <= endAngle) {
        return angle >= startAngle && angle <= endAngle;
    } else {
        // Arc crosses 0°
        return angle >= startAngle || angle <= endAngle;
    }
}

/**
 * Calculate tight bounding box for an arc (not full circle)
 * Returns {minX, maxX, minY, maxY}
 */
function calculateArcBounds(centerX, centerY, radius, startAngle, endAngle) {
    // Start with arc endpoints
    const startRad = startAngle * Math.PI / 180;
    const endRad = endAngle * Math.PI / 180;

    const points = [
        { x: centerX + radius * Math.cos(startRad), y: centerY + radius * Math.sin(startRad) },
        { x: centerX + radius * Math.cos(endRad), y: centerY + radius * Math.sin(endRad) }
    ];

    // Check if arc crosses any cardinal directions (extrema)
    if (angleInArcRange(0, startAngle, endAngle)) {
        points.push({ x: centerX + radius, y: centerY });  // Right (0°)
    }
    if (angleInArcRange(90, startAngle, endAngle)) {
        points.push({ x: centerX, y: centerY + radius });  // Top (90°)
    }
    if (angleInArcRange(180, startAngle, endAngle)) {
        points.push({ x: centerX - radius, y: centerY });  // Left (180°)
    }
    if (angleInArcRange(270, startAngle, endAngle)) {
        points.push({ x: centerX, y: centerY - radius });  // Bottom (270°)
    }

    // Calculate bounds from all critical points
    const xs = points.map(p => p.x);
    const ys = points.map(p => p.y);

    return {
        minX: Math.min(...xs),
        maxX: Math.max(...xs),
        minY: Math.min(...ys),
        maxY: Math.max(...ys)
    };
}

// ============================================================================
// Settings Persistence (localStorage)
// ============================================================================

/**
 * Default settings for the application
 */
const DEFAULT_SETTINGS = {
    material: 'plywood',
    thickness: '0.25',
    tabSpacing: '6.0',
    tubeHeight: '2.0',
    squareEnd: true,
    cutToLength: true,
    toolDiameter: '0.157',
    rotationAngle: 0
};

/**
 * Save current form settings to localStorage
 */
function saveSettings() {
    const settings = {
        material: document.getElementById('material').value,
        thickness: document.getElementById('thickness').value,
        tabSpacing: document.getElementById('tabSpacing').value,
        tubeHeight: document.getElementById('tubeHeight').value,
        squareEnd: document.getElementById('squareEnd').checked,
        cutToLength: document.getElementById('cutToLength').checked,
        toolDiameter: document.getElementById('toolDiameter').value,
        rotationAngle: appState.rotationAngle
    };

    try {
        localStorage.setItem('penguinCAM_settings', JSON.stringify(settings));
    } catch (e) {
        console.warn('Failed to save settings to localStorage:', e);
    }
}

/**
 * Load settings from localStorage and apply to form
 */
function loadSettings() {
    try {
        const saved = localStorage.getItem('penguinCAM_settings');
        const settings = saved ? JSON.parse(saved) : DEFAULT_SETTINGS;

        // Get server-provided default tool diameter from HTML (set by team config)
        const serverDefaultToolDiameter = document.getElementById('toolDiameter').value;

        // Apply settings to form elements
        document.getElementById('material').value = settings.material || DEFAULT_SETTINGS.material;
        document.getElementById('thickness').value = settings.thickness || DEFAULT_SETTINGS.thickness;
        document.getElementById('tabSpacing').value = settings.tabSpacing || DEFAULT_SETTINGS.tabSpacing;
        document.getElementById('tubeHeight').value = settings.tubeHeight || DEFAULT_SETTINGS.tubeHeight;
        document.getElementById('squareEnd').checked = settings.squareEnd !== undefined ? settings.squareEnd : DEFAULT_SETTINGS.squareEnd;
        document.getElementById('cutToLength').checked = settings.cutToLength !== undefined ? settings.cutToLength : DEFAULT_SETTINGS.cutToLength;
        // Use saved value if exists, otherwise keep server-provided default
        document.getElementById('toolDiameter').value = settings.toolDiameter || serverDefaultToolDiameter;
        appState.rotationAngle = settings.rotationAngle || DEFAULT_SETTINGS.rotationAngle;

        // Trigger material change to show/hide tube params
        const materialSelect = document.getElementById('material');
        if (materialSelect.value === 'aluminum_tube') {
            document.getElementById('tubeParams').style.display = 'block';
        }

        console.log('Settings loaded from localStorage');
    } catch (e) {
        console.warn('Failed to load settings from localStorage:', e);
        // Use defaults if localStorage fails
        Object.keys(DEFAULT_SETTINGS).forEach(key => {
            const element = document.getElementById(key);
            if (element) {
                if (element.type === 'checkbox') {
                    element.checked = DEFAULT_SETTINGS[key];
                } else {
                    element.value = DEFAULT_SETTINGS[key];
                }
            }
        });
    }
}

/**
 * Attach event listeners to form elements to auto-save on change
 */
function setupSettingsAutoSave() {
    const fields = ['material', 'thickness', 'tabSpacing', 'tubeHeight', 'squareEnd', 'cutToLength', 'toolDiameter'];

    fields.forEach(fieldId => {
        const element = document.getElementById(fieldId);
        if (element) {
            const eventType = element.type === 'checkbox' ? 'change' : 'input';
            element.addEventListener(eventType, saveSettings);
        }
    });
}

// ============================================================================
// Helper Functions
// ============================================================================

/**
 * Create a bounds tracker for calculating min/max coordinates
 */
function createBounds() {
    return {
        minX: Infinity,
        maxX: -Infinity,
        minY: Infinity,
        maxY: -Infinity,
        minZ: Infinity,
        maxZ: -Infinity,

        update(x, y, z) {
            if (x !== undefined) {
                this.minX = Math.min(this.minX, x);
                this.maxX = Math.max(this.maxX, x);
            }
            if (y !== undefined) {
                this.minY = Math.min(this.minY, y);
                this.maxY = Math.max(this.maxY, y);
            }
            if (z !== undefined) {
                this.minZ = Math.min(this.minZ, z);
                this.maxZ = Math.max(this.maxZ, z);
            }
        },

        isValid() {
            return this.minX !== Infinity;
        },

        reset() {
            this.minX = this.minY = this.minZ = Infinity;
            this.maxX = this.maxY = this.maxZ = -Infinity;
        }
    };
}

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

// Main application initialization
document.addEventListener('DOMContentLoaded', () => {
    // Handle part option selection (visual feedback)
    const partOptions = document.querySelectorAll('.part-option');
    partOptions.forEach(option => {
        option.addEventListener('click', () => {
            partOptions.forEach(opt => opt.classList.remove('selected'));
            option.classList.add('selected');
        });
    });

    // Load saved settings from localStorage
        loadSettings();

    // Global state (using appState object for cross-scope access)
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
            const tabsGroup = document.getElementById('tabSpacing')?.closest('.param-group');

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

                if (data.available && data.enabled) {
                    driveAvailable = true;
                    driveBtn.style.display = 'inline-block';
                }
                // Don't show Drive warnings during DXF setup - only relevant after G-code generation
            } catch (error) {
                // Drive integration not available - that's okay
                console.log('Google Drive integration not available');
            }
        }
        checkDriveStatus();

        // Setup auto-save for settings
        setupSettingsAutoSave();

        // File upload handling
        dropZone.addEventListener('click', () => fileInput.click());

        dropZone.addEventListener('dragover', (e) => {
            e.preventDefault();
            dropZone.classList.add('dragover');
        });

        dropZone.addEventListener('dragleave', () => {
            dropZone.classList.remove('dragover');
        });

        dropZone.addEventListener('drop', (e) => {
            e.preventDefault();
            dropZone.classList.remove('dragover');
            const files = e.dataTransfer.files;
            if (files.length > 0) {
                handleFile(files[0]);
            }
        });

        fileInput.addEventListener('change', (e) => {
            if (e.target.files.length > 0) {
                handleFile(e.target.files[0]);
            }
        });

        function handleFile(file) {
            if (!file.name.toLowerCase().endsWith('.dxf')) {
                showError('Invalid file type', 'Please upload a DXF file.');
                return;
            }

            // Store in appState for access across scopes
            appState.uploadedFile = file;
            fileName.textContent = file.name;
            fileSize.textContent = formatFileSize(file.size);
            fileInfo.style.display = 'flex';
            generateBtn.disabled = false;
            generateBtn.textContent = '🚀 Generate Program';
            hideError();
            hideResults();

            // Read DXF file for setup mode
            const reader = new FileReader();
            reader.onload = (e) => {
                parseDxfForSetup(e.target.result);
            };
            reader.readAsText(file);
        }

        function formatFileSize(bytes) {
            if (bytes < 1024) return bytes + ' bytes';
            if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
            return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
        }

        // Generate G-code
        generateBtn.addEventListener('click', async () => {
            console.log('🔍 Generate button clicked');
            console.log('📂 appState.uploadedFile:', appState.uploadedFile);

            if (!appState.uploadedFile) {
                console.error('❌ No file in appState.uploadedFile');
                return;
            }

            const formData = new FormData();
            formData.append('file', appState.uploadedFile);
            console.log('✅ FormData created with file:', appState.uploadedFile.name);

            // Generate timestamp in user's local timezone
            const now = new Date();
            const year = now.getFullYear();
            const month = String(now.getMonth() + 1).padStart(2, '0');
            const day = String(now.getDate()).padStart(2, '0');
            const hour = String(now.getHours()).padStart(2, '0');
            const minute = String(now.getMinutes()).padStart(2, '0');
            const second = String(now.getSeconds()).padStart(2, '0');
            const timestamp = `${year}-${month}-${day} ${hour}:${minute}:${second}`;
            formData.append('timestamp', timestamp);

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
                formData.append('tab_spacing', document.getElementById('tabSpacing').value);
            }
            formData.append('rotation', rotationAngle); // Add rotation angle
            if (appState.suggestedFilename) {
                formData.append('suggested_filename', appState.suggestedFilename); // Onshape filename
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

                appState.gcodeContent = data.gcode;
                appState.outputFilename = data.filename;

                // Show results
                showResults(data);

                // Switch to preview mode and visualize G-code
                switchMode('preview');
                visualizeGcode(data.gcode);

                // Enable download button
                downloadBtn.disabled = false;

                // Re-check Drive status (config may have been loaded during Onshape import)
                checkDriveStatus().then(() => {
                    if (driveAvailable) {
                        driveBtn.disabled = false;
                    }
                });

            } catch (error) {
                if (Object.hasOwn(error, "details")) {
                    console.error(error.details);
                }
                showError('Generation Failed', error.message);
            } finally {
                hideLoading();
            }
        });

        // Download G-code
        downloadBtn.addEventListener('click', () => {
            if (!appState.outputFilename) return;
            window.location.href = `/download/${appState.outputFilename}`;
        });

        // Upload to Google Drive
        driveBtn.addEventListener('click', async () => {
            if (!appState.outputFilename) return;

            driveBtn.disabled = true;
            driveBtn.textContent = '⏳ Checking auth...';
            driveStatus.style.display = 'none';

            try {
                // First, check if we're authenticated
                const statusResponse = await fetch('/drive/status');
                const statusData = await statusResponse.json();

                if (!statusData.authenticated) {
                    // Not authenticated - open OAuth in popup
                    driveBtn.textContent = '🔐 Authenticating...';
                    driveStatus.textContent = 'Opening Google sign-in...';
                    driveStatus.style.color = '#FDB515';
                    driveStatus.style.display = 'block';

                    // Open OAuth in popup window
                    const popup = window.open(
                        '/auth/login',
                        'GoogleAuth',
                        'width=600,height=700,left=100,top=100'
                    );

                    if (!popup || popup.closed) {
                        // Popup blocked - show instructions instead of auto-redirecting
                        driveBtn.textContent = '💾 Save to Google Drive';
                        driveBtn.disabled = false;
                        driveStatus.innerHTML = '⚠️ Popup blocked! Please allow popups for this site and try again.<br>' +
                                               'Or <a href="/auth/login" target="_blank" style="color: #FDB515; text-decoration: underline;">click here</a> to authenticate in a new tab.';
                        driveStatus.style.color = 'var(--warning)';
                        driveStatus.style.display = 'block';
                        return;
                    }

                    // Wait for popup to close (OAuth complete)
                    const pollTimer = setInterval(() => {
                        if (popup.closed) {
                            clearInterval(pollTimer);
                            // Popup closed, retry the upload
                            console.log('Auth popup closed, retrying upload...');
                            setTimeout(() => {
                                driveBtn.click(); // Retry the upload
                            }, 500);
                        }
                    }, 500);

                    return;
                }

                // We're authenticated, proceed with upload
                driveBtn.textContent = '⏳ Uploading...';

                const response = await fetch(`/drive/upload/${appState.outputFilename}`, {
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
            const stockSizeDisplay = document.getElementById('stockSizeDisplay');

            if (mode === 'setup') {
                setupContainer.style.display = 'block';
                previewContainer.style.display = 'none';
                scrubberContainer.style.display = 'none';
                previewControls.style.display = 'none';
                gcodeButtons.style.display = 'none';
                if (stockSizeDisplay) stockSizeDisplay.style.display = 'none';
                
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
                setupContainer.style.display = 'none';
                previewContainer.style.display = 'block';
                previewControls.style.display = 'flex';
                gcodeButtons.style.display = 'flex';
                // Stock size display shown if G-code has been generated
                if (stockSizeDisplay && toolpathMoves.length > 0) {
                    stockSizeDisplay.style.display = 'flex';
                }
                // Scrubber visibility handled by visualizeGcode
            }
        }

        // Initialize 2D canvas for DXF setup
        function initDxfSetup() {
            dxfCanvas2D = document.getElementById('dxfSetupCanvas');
            dxfCtx2D = dxfCanvas2D.getContext('2d');
            
            // CRITICAL: Set canvas internal size to match CSS display size
            // to avoid stretching/distortion
            const rect = dxfCanvas2D.getBoundingClientRect();
            if (rect.width > 0 && rect.height > 0) {
                dxfCanvas2D.width = rect.width;
                dxfCanvas2D.height = rect.height;
            } else {
                // Fallback if element not yet sized
                console.warn('Canvas not yet sized, using defaults');
                dxfCanvas2D.width = 800;
                dxfCanvas2D.height = 500;
            }
            
            // Setup event listeners
            document.getElementById('rotateBtn').addEventListener('click', () => {
                rotationAngle = (rotationAngle + 90) % 360;
                appState.rotationAngle = rotationAngle; // Keep appState in sync
                document.getElementById('rotationDisplay').textContent = rotationAngle + '°';
                renderDxfSetup();
                saveSettings(); // Persist rotation angle
            });
            
            // Mode toggle listeners
            document.querySelectorAll('.mode-button').forEach(btn => {
                btn.addEventListener('click', () => switchMode(btn.dataset.mode));
            });
        }

        // Parse DXF geometry from file using dxf-parser library
        function parseDxfForSetup(dxfContent) {
            try {
                // Check if library loaded
                if (typeof window.DxfParser === 'undefined') {
                    console.error('DxfParser library not loaded');
                    // Fall back to simple manual parsing
                    parseDxfManually(dxfContent);
                    return;
                }
                
                // Use dxf-parser library to parse DXF
                const parser = new window.DxfParser();
                const dxf = parser.parseSync(dxfContent);
                
                console.log('Parsed DXF:', dxf);
                
                // Extract bounds from all entities
                let minX = Infinity, maxX = -Infinity;
                let minY = Infinity, maxY = -Infinity;
                
                // Helper to update bounds
                function updateBounds(x, y) {
                    minX = Math.min(minX, x);
                    maxX = Math.max(maxX, x);
                    minY = Math.min(minY, y);
                    maxY = Math.max(maxY, y);
                }
                
                // Process entities to get bounds
                if (dxf.entities) {
                    dxf.entities.forEach(entity => {
                        switch(entity.type) {
                            case 'CIRCLE':
                                updateBounds(entity.center.x - entity.radius, entity.center.y - entity.radius);
                                updateBounds(entity.center.x + entity.radius, entity.center.y + entity.radius);
                                break;
                            case 'ARC':
                                // Calculate proper arc bounds (not full circle)
                                {
                                    const bounds = calculateArcBounds(
                                        entity.center.x,
                                        entity.center.y,
                                        entity.radius,
                                        entity.startAngle || 0,
                                        entity.endAngle || 360
                                    );
                                    updateBounds(bounds.minX, bounds.minY);
                                    updateBounds(bounds.maxX, bounds.maxY);
                                }
                                break;
                            case 'LINE':
                                updateBounds(entity.vertices[0].x, entity.vertices[0].y);
                                updateBounds(entity.vertices[1].x, entity.vertices[1].y);
                                break;
                            case 'LWPOLYLINE':
                            case 'POLYLINE':
                                entity.vertices.forEach(v => updateBounds(v.x, v.y));
                                break;
                            case 'SPLINE':
                                if (entity.controlPoints) {
                                    entity.controlPoints.forEach(p => updateBounds(p.x, p.y));
                                }
                                break;
                            case 'ELLIPSE':
                                // Approximate with bounding box
                                const majorRadius = Math.sqrt(entity.majorAxisEndPoint.x ** 2 + entity.majorAxisEndPoint.y ** 2);
                                const minorRadius = majorRadius * entity.axisRatio;
                                updateBounds(entity.center.x - majorRadius, entity.center.y - minorRadius);
                                updateBounds(entity.center.x + majorRadius, entity.center.y + minorRadius);
                                break;
                        }
                    });
                }
                
                if (minX === Infinity) {
                    minX = 0; maxX = 10;
                    minY = 0; maxY = 10;
                }
                
                console.log(`DXF bounds: X=[${minX.toFixed(3)}, ${maxX.toFixed(3)}], Y=[${minY.toFixed(3)}, ${maxY.toFixed(3)}]`);
                console.log(`Entity count: ${dxf.entities ? dxf.entities.length : 0}`);
                
                // Store parsed DXF data
                dxfGeometry = { 
                    minX, maxX, minY, maxY,
                    entities: dxf.entities || []
                };
                dxfBounds = {
                    width: maxX - minX,
                    height: maxY - minY,
                    centerX: (minX + maxX) / 2,
                    centerY: (minY + maxY) / 2
                };

                // Debug: Log bounds calculation
                console.log('DXF Bounds Debug:');
                console.log(`  minX: ${minX.toFixed(4)}, maxX: ${maxX.toFixed(4)}`);
                console.log(`  minY: ${minY.toFixed(4)}, maxY: ${maxY.toFixed(4)}`);
                console.log(`  Width: ${dxfBounds.width.toFixed(4)}", Height: ${dxfBounds.height.toFixed(4)}"`);
                console.log(`  Total entities processed: ${dxf.entities ? dxf.entities.length : 0}`);

                // Show mode toggle and switch to setup mode
                document.getElementById('modeToggle').style.display = 'flex';
                switchMode('setup');
                
            } catch (error) {
                console.error('DXF parsing error:', error);
                // Try manual fallback
                console.log('Attempting manual DXF parsing...');
                parseDxfManually(dxfContent);
            }
        }
        
        // Fallback manual DXF parser (simple but works for basic shapes)
        function parseDxfManually(dxfContent) {
            const lines = dxfContent.split('\n');

            const entities = [];
            let inEntitiesSection = false;
            let currentEntity = null;
            let entityData = {};

            for (let i = 0; i < lines.length; i++) {
                const line = lines[i].trim();

                if (line === 'ENTITIES') {
                    inEntitiesSection = true;
                    continue;
                }
                if (line === 'ENDSEC' && inEntitiesSection) break;
                if (!inEntitiesSection) continue;

                // Detect entity type
                if (line === 'CIRCLE' || line === 'ARC' || line === 'LINE' || line === 'LWPOLYLINE' || line === 'SPLINE') {
                    if (currentEntity) {
                        entities.push(createEntity(currentEntity, entityData));
                    }
                    currentEntity = line;
                    entityData = { type: line };
                    if (line === 'LWPOLYLINE') {
                        entityData.vertices = [];
                    }
                    if (line === 'SPLINE') {
                        entityData.controlPoints = [];
                    }
                }

                // Parse coordinates (store in entity data, don't update bounds yet)
                if (line === '10' && i + 1 < lines.length) {
                    const val = parseFloat(lines[i + 1]);
                    if (!isNaN(val) && Math.abs(val) < 1e10) {
                        if (currentEntity === 'CIRCLE' || currentEntity === 'ARC') {
                            entityData.centerX = val;
                        } else if (currentEntity === 'LINE') {
                            entityData.x1 = val;
                        } else if (currentEntity === 'LWPOLYLINE') {
                            entityData.tempX = val;
                        } else if (currentEntity === 'SPLINE') {
                            entityData.tempX = val;
                        }
                    }
                } else if (line === '20' && i + 1 < lines.length) {
                    const val = parseFloat(lines[i + 1]);
                    if (!isNaN(val) && Math.abs(val) < 1e10) {
                        if (currentEntity === 'CIRCLE' || currentEntity === 'ARC') {
                            entityData.centerY = val;
                        } else if (currentEntity === 'LINE') {
                            entityData.y1 = val;
                        } else if (currentEntity === 'LWPOLYLINE' && entityData.tempX !== undefined) {
                            entityData.vertices.push({ x: entityData.tempX, y: val });
                            delete entityData.tempX;
                        } else if (currentEntity === 'SPLINE' && entityData.tempX !== undefined) {
                            entityData.controlPoints.push({ x: entityData.tempX, y: val });
                            delete entityData.tempX;
                        }
                    }
                } else if (line === '40' && i + 1 < lines.length) {
                    const val = parseFloat(lines[i + 1]);
                    if (!isNaN(val) && val < 1e10) {
                        entityData.radius = val;
                    }
                } else if (line.trim() === '50' && i + 1 < lines.length && currentEntity === 'ARC') {
                    entityData.startAngle = parseFloat(lines[i + 1].trim());
                } else if (line.trim() === '51' && i + 1 < lines.length && currentEntity === 'ARC') {
                    entityData.endAngle = parseFloat(lines[i + 1].trim());
                } else if (line === '11' && i + 1 < lines.length) {
                    const val = parseFloat(lines[i + 1]);
                    if (!isNaN(val) && Math.abs(val) < 1e10) {
                        entityData.x2 = val;
                    }
                } else if (line === '21' && i + 1 < lines.length) {
                    const val = parseFloat(lines[i + 1]);
                    if (!isNaN(val) && Math.abs(val) < 1e10) {
                        entityData.y2 = val;
                    }
                } else if (line === '70' && i + 1 < lines.length && currentEntity === 'LWPOLYLINE') {
                    // Group code 70 contains polyline flags; bit 0 (value & 1) indicates closed
                    const flags = parseInt(lines[i + 1].trim());
                    if (!isNaN(flags)) {
                        entityData.closed = (flags & 1) !== 0;
                    }
                }
            }

            if (currentEntity) {
                entities.push(createEntity(currentEntity, entityData));
            }

            // Calculate bounds from rendered entities only (not raw DXF coordinates)
            let minX = Infinity, maxX = -Infinity;
            let minY = Infinity, maxY = -Infinity;

            function updateBounds(x, y) {
                minX = Math.min(minX, x);
                maxX = Math.max(maxX, x);
                minY = Math.min(minY, y);
                maxY = Math.max(maxY, y);
            }

            // Calculate bounds only from closed contours + circles (match backend behavior)
            // But still render all entities for preview
            console.log(`Calculating bounds from entities (filtering construction geometry)...`);
            entities.forEach((entity, idx) => {
                // Skip bounds calculation for isolated LINE/ARC entities
                // These are construction lines that won't be processed by backend
                let skipForBounds = false;

                if (entity.type === 'LINE' || entity.type === 'ARC') {
                    // Check if this is an isolated construction entity (very large)
                    let isConstruction = false;

                    if (entity.type === 'LINE' && entity.vertices.length === 2) {
                        const dx = entity.vertices[1].x - entity.vertices[0].x;
                        const dy = entity.vertices[1].y - entity.vertices[0].y;
                        const length = Math.sqrt(dx * dx + dy * dy);
                        if (length > 12.0) {  // Suspiciously long isolated line
                            isConstruction = true;
                            console.log(`  Skipping LINE ${idx} for bounds (${length.toFixed(1)}" long, likely construction)`);
                        }
                    } else if (entity.type === 'ARC' && entity.radius > 3.0) {
                        isConstruction = true;
                        console.log(`  Skipping ARC ${idx} for bounds (${entity.radius.toFixed(1)}" radius, likely construction)`);
                    }

                    skipForBounds = isConstruction;
                }

                if (skipForBounds) {
                    return;  // Skip this entity for bounds calculation
                }
                let entityMinX = Infinity, entityMaxX = -Infinity;
                let entityMinY = Infinity, entityMaxY = -Infinity;

                if (entity.type === 'CIRCLE') {
                    entityMinX = entity.center.x - entity.radius;
                    entityMaxX = entity.center.x + entity.radius;
                    entityMinY = entity.center.y - entity.radius;
                    entityMaxY = entity.center.y + entity.radius;
                    updateBounds(entityMinX, entityMinY);
                    updateBounds(entityMaxX, entityMaxY);
                } else if (entity.type === 'ARC') {
                    // Calculate proper arc bounds (not full circle)
                    const bounds = calculateArcBounds(
                        entity.center.x,
                        entity.center.y,
                        entity.radius,
                        entity.startAngle || 0,
                        entity.endAngle || 360
                    );
                    updateBounds(bounds.minX, bounds.minY);
                    updateBounds(bounds.maxX, bounds.maxY);
                } else if (entity.type === 'LINE') {
                    entity.vertices.forEach(v => {
                        entityMinX = Math.min(entityMinX, v.x);
                        entityMaxX = Math.max(entityMaxX, v.x);
                        entityMinY = Math.min(entityMinY, v.y);
                        entityMaxY = Math.max(entityMaxY, v.y);
                        updateBounds(v.x, v.y);
                    });
                } else if (entity.type === 'LWPOLYLINE' || entity.type === 'POLYLINE') {
                    entity.vertices.forEach(v => {
                        entityMinX = Math.min(entityMinX, v.x);
                        entityMaxX = Math.max(entityMaxX, v.x);
                        entityMinY = Math.min(entityMinY, v.y);
                        entityMaxY = Math.max(entityMaxY, v.y);
                        updateBounds(v.x, v.y);
                    });
                } else if (entity.type === 'SPLINE' && entity.controlPoints) {
                    entity.controlPoints.forEach(p => {
                        entityMinX = Math.min(entityMinX, p.x);
                        entityMaxX = Math.max(entityMaxX, p.x);
                        entityMinY = Math.min(entityMinY, p.y);
                        entityMaxY = Math.max(entityMaxY, p.y);
                        updateBounds(p.x, p.y);
                    });
                }

                // Log entities that extend beyond expected bounds
                if (entityMinX < -27 || entityMaxX > -9 || entityMinY < -1 || entityMaxY > 8) {
                    console.log(`  ⚠️ Entity ${idx} (${entity.type}) extends bounds significantly:`);
                    console.log(`     X=[${entityMinX.toFixed(3)}, ${entityMaxX.toFixed(3)}], Y=[${entityMinY.toFixed(3)}, ${entityMaxY.toFixed(3)}]`);
                    if (entity.type === 'CIRCLE' || entity.type === 'ARC') {
                        console.log(`     Center: (${entity.center.x.toFixed(3)}, ${entity.center.y.toFixed(3)}), Radius: ${entity.radius.toFixed(3)}`);
                    }
                }
            });
            console.log(`After bounds calculation: X=[${minX.toFixed(3)}, ${maxX.toFixed(3)}], Y=[${minY.toFixed(3)}, ${maxY.toFixed(3)}]`);

            if (minX === Infinity) {
                console.warn('⚠️ No valid geometry found, using fallback 10×10 bounds');
                minX = 0; maxX = 10;
                minY = 0; maxY = 10;
            }

            console.log(`Manual parse: ${entities.length} entities`);
            console.log(`Bounds: X=[${minX.toFixed(3)}, ${maxX.toFixed(3)}], Y=[${minY.toFixed(3)}, ${maxY.toFixed(3)}]`);

            dxfGeometry = {
                minX, maxX, minY, maxY,
                entities: entities  // Use all entities for rendering
            };
            dxfBounds = { 
                width: maxX - minX, 
                height: maxY - minY,
                centerX: (minX + maxX) / 2,
                centerY: (minY + maxY) / 2
            };
            
            document.getElementById('modeToggle').style.display = 'flex';
            switchMode('setup');
        }
        
        function createEntity(type, data) {
            if (type === 'CIRCLE') {
                return {
                    type: 'CIRCLE',
                    center: { x: data.centerX, y: data.centerY },
                    radius: data.radius
                };
            } else if (type === 'ARC') {
                return {
                    type: 'ARC',
                    center: { x: data.centerX, y: data.centerY },
                    radius: data.radius,
                    startAngle: data.startAngle || 0,
                    endAngle: data.endAngle || 360
                };
            } else if (type === 'LINE') {
                return {
                    type: 'LINE',
                    vertices: [
                        { x: data.x1, y: data.y1 },
                        { x: data.x2, y: data.y2 }
                    ]
                };
            } else if (type === 'LWPOLYLINE') {
                return {
                    type: 'LWPOLYLINE',
                    vertices: data.vertices || [],
                    closed: data.closed || false,  // Used to filter construction geometry
                    shape: data.closed || false  // Used by renderer to close path
                };
            } else if (type === 'SPLINE') {
                return {
                    type: 'SPLINE',
                    controlPoints: data.controlPoints || []
                };
            }
            return null;
        }

        // Render 2D DXF setup view
        function renderDxfSetup() {
            if (!dxfGeometry || !dxfCtx2D) return;
            
            const ctx = dxfCtx2D;
            const canvas = dxfCanvas2D;
            const width = canvas.width;
            const height = canvas.height;
            
            // Check if canvas has valid size
            if (width === 0 || height === 0) {
                console.warn('Canvas has zero size, skipping render');
                return;
            }
            
            // Clear
            ctx.fillStyle = '#0A0E14';
            ctx.fillRect(0, 0, width, height);
            
            // Calculate transform to fit DXF in canvas with padding
            const padding = 80;
            const availWidth = width - 2 * padding;
            const availHeight = height - 2 * padding;
            
            // Apply rotation to bounds for calculating display size
            let displayWidth = dxfBounds.width;
            let displayHeight = dxfBounds.height;
            if (rotationAngle === 90 || rotationAngle === 270) {
                [displayWidth, displayHeight] = [displayHeight, displayWidth];
            }
            
            const scale = Math.min(availWidth / displayWidth, availHeight / displayHeight);
            
            // Center position (no rotation of entire canvas)
            const centerX = width / 2;
            const centerY = height / 2;
            
            // Helper functions to transform coordinates
            function rotatePoint(x, y, angle) {
                const rad = -angle * Math.PI / 180; // Negative for clockwise
                const cos = Math.cos(rad);
                const sin = Math.sin(rad);
                return {
                    x: x * cos - y * sin,
                    y: x * sin + y * cos
                };
            }
            
            function toCanvasCoords(x, y) {
                // Translate to center origin
                let dx = x - dxfBounds.centerX;
                let dy = y - dxfBounds.centerY;
                
                // Apply rotation
                const rotated = rotatePoint(dx, dy, rotationAngle);
                
                // Scale and flip Y, then translate to canvas center
                return {
                    x: centerX + rotated.x * scale,
                    y: centerY - rotated.y * scale
                };
            }
            
            // Draw all entities (rotated)
            ctx.strokeStyle = '#6B7280';
            ctx.lineWidth = 1.5;
            
            if (dxfGeometry.entities) {
                dxfGeometry.entities.forEach(entity => {
                    ctx.beginPath();
                    
                    switch(entity.type) {
                        case 'CIRCLE':
                            const cPos = toCanvasCoords(entity.center.x, entity.center.y);
                            ctx.arc(cPos.x, cPos.y, entity.radius * scale, 0, Math.PI * 2);
                            ctx.stroke();
                            break;
                            
                        case 'ARC':
                            const aPos = toCanvasCoords(entity.center.x, entity.center.y);
                            // Y-flip means angles are negated, rotation subtracts from angle
                            // Canvas angle = -(DXF angle - rotation) = -DXF angle + rotation
                            const startRad = (-entity.startAngle + rotationAngle) * Math.PI / 180;
                            const endRad = (-entity.endAngle + rotationAngle) * Math.PI / 180;
                            const arcRadius = entity.radius * scale;
                            
                            // Validate arc parameters
                            if (isNaN(startRad) || isNaN(endRad) || arcRadius <= 0 || !isFinite(arcRadius)) {
                                console.warn('Invalid arc parameters:', { startRad, endRad, arcRadius });
                                break;
                            }
                            
                            // Y-flip also reverses direction: counter-clockwise becomes clockwise
                            // So we swap start and end to maintain the arc direction
                            ctx.arc(aPos.x, aPos.y, arcRadius, endRad, startRad, false);
                            ctx.stroke();
                            break;
                            
                        case 'LINE':
                            const p1 = toCanvasCoords(entity.vertices[0].x, entity.vertices[0].y);
                            const p2 = toCanvasCoords(entity.vertices[1].x, entity.vertices[1].y);
                            ctx.moveTo(p1.x, p1.y);
                            ctx.lineTo(p2.x, p2.y);
                            ctx.stroke();
                            break;
                            
                        case 'LWPOLYLINE':
                        case 'POLYLINE':
                            if (entity.vertices && entity.vertices.length > 0) {
                                const v0 = toCanvasCoords(entity.vertices[0].x, entity.vertices[0].y);
                                ctx.moveTo(v0.x, v0.y);
                                for (let i = 1; i < entity.vertices.length; i++) {
                                    const v = toCanvasCoords(entity.vertices[i].x, entity.vertices[i].y);
                                    ctx.lineTo(v.x, v.y);
                                }
                                if (entity.shape) {
                                    ctx.closePath();
                                }
                                ctx.stroke();
                            }
                            break;
                            
                        case 'SPLINE':
                            if (entity.controlPoints && entity.controlPoints.length > 1) {
                                const sp0 = toCanvasCoords(entity.controlPoints[0].x, entity.controlPoints[0].y);
                                ctx.moveTo(sp0.x, sp0.y);
                                for (let i = 1; i < entity.controlPoints.length; i++) {
                                    const sp = toCanvasCoords(entity.controlPoints[i].x, entity.controlPoints[i].y);
                                    ctx.lineTo(sp.x, sp.y);
                                }
                                ctx.stroke();
                            }
                            break;
                            
                        case 'ELLIPSE':
                            const ePos = toCanvasCoords(entity.center.x, entity.center.y);
                            const majorRadius = Math.sqrt(entity.majorAxisEndPoint.x ** 2 + entity.majorAxisEndPoint.y ** 2);
                            const minorRadius = majorRadius * entity.axisRatio;
                            ctx.ellipse(ePos.x, ePos.y, majorRadius * scale, minorRadius * scale, 0, 0, Math.PI * 2);
                            ctx.stroke();
                            break;
                    }
                });
            }
            
            // Calculate bounding box corners in SCREEN coordinates (NOT rotated)
            const boxLeft = centerX - (displayWidth * scale) / 2;
            const boxRight = centerX + (displayWidth * scale) / 2;
            const boxTop = centerY - (displayHeight * scale) / 2;
            const boxBottom = centerY + (displayHeight * scale) / 2;
            
            // Draw bounding box (dashed, NOT rotated)
            ctx.strokeStyle = '#8B949E';
            ctx.lineWidth = 2;
            ctx.setLineDash([5, 5]);
            ctx.strokeRect(boxLeft, boxTop, displayWidth * scale, displayHeight * scale);
            ctx.setLineDash([]);
            
            // Draw origin marker at bottom-left (ALWAYS)
            const originX = boxLeft;
            const originY = boxBottom;
            
            ctx.beginPath();
            ctx.arc(originX, originY, 12, 0, Math.PI * 2);
            ctx.fillStyle = '#FDB515';
            ctx.fill();
            ctx.strokeStyle = '#FDB515';
            ctx.lineWidth = 3;
            ctx.stroke();
            
            // Draw origin label
            ctx.fillStyle = '#FDB515';
            ctx.font = 'bold 14px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif';
            ctx.textAlign = 'center';
            ctx.textBaseline = 'middle';
            ctx.fillText('Origin (0,0)', originX, originY - 25);
            
            // Draw axes from bottom-left origin
            // X axis (red) - points right
            ctx.beginPath();
            ctx.moveTo(originX, originY);
            ctx.lineTo(originX + 60, originY);
            ctx.strokeStyle = '#FF0000';
            ctx.lineWidth = 2;
            ctx.stroke();
            
            ctx.fillStyle = '#FF0000';
            ctx.font = 'bold 12px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif';
            ctx.fillText('X', originX + 70, originY);
            
            // Y axis (green) - points up
            ctx.beginPath();
            ctx.moveTo(originX, originY);
            ctx.lineTo(originX, originY - 60);
            ctx.strokeStyle = '#00FF00';
            ctx.lineWidth = 2;
            ctx.stroke();
            
            ctx.fillStyle = '#00FF00';
            ctx.fillText('Y', originX, originY - 70);
            
            // Draw dimensions at top
            ctx.font = '14px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif';
            ctx.textAlign = 'center';
            ctx.textBaseline = 'top';

            // Check if part fits within machine bounds
            const machineXMax = window.MACHINE_CONFIG?.xMax || 48.0;
            const machineYMax = window.MACHINE_CONFIG?.yMax || 96.0;
            const fitsInMachine = displayWidth <= machineXMax && displayHeight <= machineYMax;

            if (fitsInMachine) {
                ctx.fillStyle = '#8B949E';
                ctx.fillText(
                    `${displayWidth.toFixed(2)}" × ${displayHeight.toFixed(2)}" (${rotationAngle}°)`,
                    width / 2,
                    20
                );
            } else {
                // Part exceeds machine bounds - show error
                ctx.fillStyle = '#FF4444';
                ctx.fillText(
                    `⚠️ ${displayWidth.toFixed(2)}" × ${displayHeight.toFixed(2)}" (${rotationAngle}°) - TOO LARGE`,
                    width / 2,
                    20
                );
                ctx.fillStyle = '#FF4444';
                ctx.font = '12px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif';
                ctx.fillText(
                    `Machine max: ${machineXMax.toFixed(0)}" × ${machineYMax.toFixed(0)}" - Rotate or reduce size`,
                    width / 2,
                    40
                );
            }
        }

        // G-code visualization
        let toolpathMoves = []; // Array of moves for scrubber
        let toolMesh = null; // 3D representation of cutting tool
        let completedLine = null; // Line showing completed moves
        let upcomingLine = null; // Line showing upcoming moves

        function initVisualization() {
            const container = document.getElementById('canvas-container');
            const canvas = document.getElementById('gcodeCanvas');

            // Scene
            scene = new THREE.Scene();
            scene.background = new THREE.Color(0x0A0E14);

            // Camera
            camera = new THREE.PerspectiveCamera(45, container.clientWidth / container.clientHeight, 0.1, 1000);
            camera.position.set(10, 10, 10);
            camera.lookAt(0, 0, 0);

            // Renderer
            renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
            renderer.setSize(container.clientWidth, container.clientHeight);
            renderer.setPixelRatio(window.devicePixelRatio);

            // Lights
            const ambientLight = new THREE.AmbientLight(0xffffff, 0.6);
            scene.add(ambientLight);

            const directionalLight = new THREE.DirectionalLight(0xffffff, 0.8);
            directionalLight.position.set(5, 10, 7.5);
            scene.add(directionalLight);

            // Grid, axes, and origin marker will be added when G-code is loaded
            // (sized appropriately for the part)

            // Mouse controls
            addMouseControls();

            // Animate
            animate();
        }

        function addAxisLabels() {
            // Not needed - origin marker added in visualizeGcode with proper sizing
        }

        function addMouseControls() {
            const canvas = document.getElementById('gcodeCanvas');
            let isDragging = false;
            let isPanning = false;
            let previousMousePosition = { x: 0, y: 0 };

            canvas.addEventListener('mousedown', (e) => {
                // Middle mouse button (button 1) or Shift + left button for panning
                if (e.button === 1 || (e.button === 0 && e.shiftKey)) {
                    e.preventDefault(); // Prevent default middle-click behavior
                    isPanning = true;
                    isDragging = false;
                } else if (e.button === 0) {
                    // Left button for rotation
                    isDragging = true;
                    isPanning = false;
                }
                previousMousePosition = { x: e.clientX, y: e.clientY };
            });

            // Prevent context menu on canvas (for middle mouse button)
            canvas.addEventListener('contextmenu', (e) => {
                e.preventDefault();
            });

            canvas.addEventListener('mousemove', (e) => {
                if (!isDragging && !isPanning) return;

                const deltaX = e.clientX - previousMousePosition.x;
                const deltaY = e.clientY - previousMousePosition.y;

                if (isPanning) {
                    // Pan camera (Onshape-style: middle mouse or Shift+left)
                    const panSpeed = 0.01;

                    // Get camera right and up vectors for proper panning
                    const cameraDirection = new THREE.Vector3();
                    camera.getWorldDirection(cameraDirection);

                    const cameraRight = new THREE.Vector3();
                    cameraRight.crossVectors(camera.up, cameraDirection).normalize();

                    const cameraUp = new THREE.Vector3();
                    cameraUp.crossVectors(cameraDirection, cameraRight).normalize();

                    // Calculate pan offset
                    const distance = camera.position.length();
                    const panX = cameraRight.multiplyScalar(-deltaX * panSpeed * distance * 0.01);
                    const panY = cameraUp.multiplyScalar(deltaY * panSpeed * distance * 0.01);

                    // Apply pan to both camera and look-at target
                    camera.position.add(panX).add(panY);

                    // Update look-at position
                    optimalLookAtPosition.x += panX.x + panY.x;
                    optimalLookAtPosition.y += panX.y + panY.y;
                    optimalLookAtPosition.z += panX.z + panY.z;

                    camera.lookAt(optimalLookAtPosition.x, optimalLookAtPosition.y, optimalLookAtPosition.z);
                } else if (isDragging) {
                    // Rotate camera (Onshape-style: left mouse)
                    const rotationSpeed = 0.005;
                    camera.position.x = camera.position.x * Math.cos(deltaX * rotationSpeed) - camera.position.z * Math.sin(deltaX * rotationSpeed);
                    camera.position.z = camera.position.x * Math.sin(deltaX * rotationSpeed) + camera.position.z * Math.cos(deltaX * rotationSpeed);
                    camera.position.y += deltaY * rotationSpeed * 5;

                    camera.lookAt(optimalLookAtPosition.x, optimalLookAtPosition.y, optimalLookAtPosition.z);
                }

                previousMousePosition = { x: e.clientX, y: e.clientY };
            });

            canvas.addEventListener('mouseup', () => {
                isDragging = false;
                isPanning = false;
            });

            canvas.addEventListener('wheel', (e) => {
                e.preventDefault();
                const zoomSpeed = 0.1;
                const distance = camera.position.length();
                const newDistance = distance * (1 + e.deltaY * zoomSpeed * 0.01);
                camera.position.multiplyScalar(newDistance / distance);
            });

            // Reset view button
            document.getElementById('resetView').addEventListener('click', () => {
                camera.position.set(
                    optimalCameraPosition.x,
                    optimalCameraPosition.y,
                    optimalCameraPosition.z
                );
                camera.lookAt(
                    optimalLookAtPosition.x,
                    optimalLookAtPosition.y,
                    optimalLookAtPosition.z
                );
            });
        }

        function animate() {
            requestAnimationFrame(animate);
            renderer.render(scene, camera);
        }

        /**
         * Render DXF geometry entities as white lines on the stock top surface
         * This shows the "cutting geometry" - the original design shapes
         */
        function renderDxfGeometry(scene, entities, zHeight) {
            if (!dxfBounds) return;

            const dxfMaterial = new THREE.LineBasicMaterial({
                color: 0xFFFFFF, // White for visibility
                linewidth: 2,
                opacity: 0.8,
                transparent: true
            });

            // Helper to transform a point: center, rotate, then position
            function transformPoint(x, y) {
                // Step 1: Center the DXF around origin
                let tx = x - dxfBounds.centerX;
                let ty = y - dxfBounds.centerY;

                // Step 2: Apply rotation
                const radians = rotationAngle * Math.PI / 180;
                const cos = Math.cos(radians);
                const sin = Math.sin(radians);
                const rx = tx * cos - ty * sin;
                const ry = tx * sin + ty * cos;

                // Step 3: Return transformed coordinates
                // Three.js uses X (right), Y (up), Z (back)
                // Our G-code uses X (right), Y (forward), Z (up)
                // So we map: DXF X -> THREE X, DXF Y -> THREE -Z
                return new THREE.Vector3(rx, zHeight, -ry);
            }

            entities.forEach(entity => {
                let points = [];

                switch(entity.type) {
                    case 'LINE':
                        // Straight line from start to end
                        points = [
                            transformPoint(entity.vertices[0].x, entity.vertices[0].y),
                            transformPoint(entity.vertices[1].x, entity.vertices[1].y)
                        ];
                        break;

                    case 'CIRCLE':
                        // Full circle - tessellate into line segments
                        {
                            const numPoints = 50;
                            for (let i = 0; i <= numPoints; i++) {
                                const angle = (i / numPoints) * 2 * Math.PI;
                                const x = entity.center.x + entity.radius * Math.cos(angle);
                                const y = entity.center.y + entity.radius * Math.sin(angle);
                                points.push(transformPoint(x, y));
                            }
                        }
                        break;

                    case 'ARC':
                        // Partial arc - tessellate into line segments
                        {
                            const startAngle = (entity.startAngle || 0) * Math.PI / 180;
                            const endAngle = (entity.endAngle || 360) * Math.PI / 180;
                            const numPoints = 50;

                            for (let i = 0; i <= numPoints; i++) {
                                const t = i / numPoints;
                                const angle = startAngle + (endAngle - startAngle) * t;
                                const x = entity.center.x + entity.radius * Math.cos(angle);
                                const y = entity.center.y + entity.radius * Math.sin(angle);
                                points.push(transformPoint(x, y));
                            }
                        }
                        break;

                    case 'LWPOLYLINE':
                    case 'POLYLINE':
                        // Connected line segments through vertices
                        points = entity.vertices.map(v => transformPoint(v.x, v.y));
                        // Close the polyline if it's marked as closed
                        if (entity.closed && points.length > 0) {
                            points.push(points[0].clone());
                        }
                        break;

                    case 'SPLINE':
                        // Approximate spline with control points
                        if (entity.controlPoints && entity.controlPoints.length > 1) {
                            points = entity.controlPoints.map(p => transformPoint(p.x, p.y));
                        }
                        break;

                    default:
                        // Skip unsupported entity types
                        return;
                }

                // Create and add the line to the scene
                if (points.length >= 2) {
                    const geometry = new THREE.BufferGeometry().setFromPoints(points);
                    const line = new THREE.Line(geometry, dxfMaterial);
                    scene.add(line);
                }
            });
        }

        function visualizeGcode(gcode) {
            // Parse G-code into moves
            const lines = gcode.split('\n');
            toolpathMoves = [];
            let currentX = 0, currentY = 0, currentZ = 0;
            let minX = Infinity, maxX = -Infinity;
            let minY = Infinity, maxY = -Infinity;
            let minZ = Infinity, maxZ = -Infinity;

            for (const line of lines) {
                const trimmed = line.trim();
                if (trimmed.startsWith('(') || trimmed.startsWith(';') || !trimmed) continue;

                const gMatch = trimmed.match(/^(G[0-3])/);
                if (!gMatch) continue;

                const moveType = gMatch[1];
                const xMatch = trimmed.match(/X([-\d.]+)/);
                const yMatch = trimmed.match(/Y([-\d.]+)/);
                const zMatch = trimmed.match(/Z([-\d.]+)/);

                const newX = xMatch ? parseFloat(xMatch[1]) : currentX;
                const newY = yMatch ? parseFloat(yMatch[1]) : currentY;
                const newZ = zMatch ? parseFloat(zMatch[1]) : currentZ;

                // Handle arcs (G2 = CW, G3 = CCW)
                if (moveType === 'G2' || moveType === 'G3') {
                    const iMatch = trimmed.match(/I([-\d.]+)/);
                    const jMatch = trimmed.match(/J([-\d.]+)/);

                    if (iMatch && jMatch) {
                        const arcI = parseFloat(iMatch[1]);
                        const arcJ = parseFloat(jMatch[1]);

                        // Arc center (incremental from start point - G91.1 mode)
                        const centerX = currentX + arcI;
                        const centerY = currentY + arcJ;

                        // Calculate arc parameters
                        const startAngle = Math.atan2(currentY - centerY, currentX - centerX);
                        const endAngle = Math.atan2(newY - centerY, newX - centerX);
                        const radius = Math.sqrt(arcI * arcI + arcJ * arcJ);

                        // Determine sweep direction and angle
                        let sweepAngle = endAngle - startAngle;

                        // Handle G2 (clockwise) vs G3 (counterclockwise)
                        const isClockwise = moveType === 'G2';

                        // Normalize sweep angle
                        if (isClockwise) {
                            // For CW, sweep should be negative
                            if (sweepAngle > 0) sweepAngle -= 2 * Math.PI;
                            // Handle full circles (start == end)
                            if (Math.abs(sweepAngle) < 0.001) sweepAngle = -2 * Math.PI;
                        } else {
                            // For CCW, sweep should be positive
                            if (sweepAngle < 0) sweepAngle += 2 * Math.PI;
                            // Handle full circles (start == end)
                            if (Math.abs(sweepAngle) < 0.001) sweepAngle = 2 * Math.PI;
                        }

                        // Validate arc parameters
                        if (isNaN(radius) || radius <= 0 || isNaN(sweepAngle)) {
                            console.warn('Invalid arc parameters:', { radius, sweepAngle, centerX, centerY });
                            continue;
                        }

                        // Save start position before tessellation
                        const startX = currentX;
                        const startY = currentY;
                        const startZ = currentZ;

                        // Tessellate arc into line segments
                        const numSegments = Math.max(8, Math.ceil(Math.abs(sweepAngle) * radius * 10));
                        const zStep = (newZ - startZ) / numSegments;

                        for (let i = 0; i < numSegments; i++) {
                            const t = (i + 1) / numSegments;
                            const angle = startAngle + sweepAngle * t;
                            const arcX = centerX + radius * Math.cos(angle);
                            const arcY = centerY + radius * Math.sin(angle);
                            const arcZ = startZ + zStep * (i + 1);

                            // Validate segment
                            if (isNaN(arcX) || isNaN(arcY) || isNaN(arcZ)) {
                                console.warn('Invalid arc segment:', { arcX, arcY, arcZ });
                                continue;
                            }

                            toolpathMoves.push({
                                type: moveType,
                                from: { x: currentX, y: currentY, z: currentZ },
                                to: { x: arcX, y: arcY, z: arcZ },
                                line: trimmed
                            });

                            currentX = arcX;
                            currentY = arcY;
                            currentZ = arcZ;

                            minX = Math.min(minX, currentX);
                            maxX = Math.max(maxX, currentX);
                            minY = Math.min(minY, currentY);
                            maxY = Math.max(maxY, currentY);
                            minZ = Math.min(minZ, currentZ);
                            maxZ = Math.max(maxZ, currentZ);
                        }

                        continue; // Skip the linear move handling below
                    }
                }

                // Linear moves (G0, G1) or arcs without I/J
                if (newX !== currentX || newY !== currentY || newZ !== currentZ) {
                    toolpathMoves.push({
                        type: moveType,
                        from: { x: currentX, y: currentY, z: currentZ },
                        to: { x: newX, y: newY, z: newZ },
                        line: trimmed
                    });

                    currentX = newX;
                    currentY = newY;
                    currentZ = newZ;

                    minX = Math.min(minX, currentX);
                    maxX = Math.max(maxX, currentX);
                    minY = Math.min(minY, currentY);
                    maxY = Math.max(maxY, currentY);
                    minZ = Math.min(minZ, currentZ);
                    maxZ = Math.max(maxZ, currentZ);
                }
            }

            console.log('Arc parsing complete. Total moves:', toolpathMoves.length);
            console.log('Bounds:', { minX, maxX, minY, maxY, minZ, maxZ });
            console.log('First 5 moves:', toolpathMoves.slice(0, 5));

            if (toolpathMoves.length === 0) return;

            // Clear old visualization
            const toRemove = [];
            scene.children.forEach(child => {
                if (!(child instanceof THREE.AmbientLight) && !(child instanceof THREE.DirectionalLight)) {
                    toRemove.push(child);
                }
            });
            toRemove.forEach(child => scene.remove(child));
            completedLine = null;
            upcomingLine = null;
            toolMesh = null;

            // Add grid and axes
            const maxDimension = Math.max(maxX, maxY, maxZ);
            const gridSize = Math.max(maxX * 1.3, maxY * 1.3, 15);
            const gridHelper = new THREE.GridHelper(gridSize, Math.ceil(gridSize), 0x30363D, 0x1E2632);
            gridHelper.position.set(gridSize / 3, 0, -gridSize / 3);
            scene.add(gridHelper);

            const axisLength = Math.max(maxDimension, 5) * 1.2;
            const axesHelper = new THREE.AxesHelper(axisLength);
            scene.add(axesHelper);

            const markerSize = Math.max(0.15, maxDimension * 0.02);
            const originMarker = new THREE.Mesh(
                new THREE.SphereGeometry(markerSize, 16, 16),
                new THREE.MeshBasicMaterial({ color: 0xFFFFFF })
            );
            scene.add(originMarker);

            // Get actual material thickness for visualization
            const material = document.getElementById('material').value;
            const isAluminumTube = (material === 'aluminum_tube');
            const materialThickness = parseFloat(document.getElementById('thickness').value);

            // For tube mode, use tube height as stock height instead of wall thickness
            const stockHeightValue = isAluminumTube ?
                parseFloat(document.getElementById('tubeHeight').value) :
                materialThickness;

            // Material boundaries (at material top surface)
            const materialOutline = new THREE.Line(
                new THREE.BufferGeometry().setFromPoints([
                    new THREE.Vector3(minX, materialThickness, -minY),
                    new THREE.Vector3(maxX, materialThickness, -minY),
                    new THREE.Vector3(maxX, materialThickness, -maxY),
                    new THREE.Vector3(minX, materialThickness, -maxY),
                    new THREE.Vector3(minX, materialThickness, -minY)
                ]),
                new THREE.LineBasicMaterial({ color: 0x8B949E, linewidth: 1, opacity: 0.5, transparent: true })
            );
            scene.add(materialOutline);

            const sacrificeOutline = new THREE.Line(
                new THREE.BufferGeometry().setFromPoints([
                    new THREE.Vector3(minX, 0, -minY),
                    new THREE.Vector3(maxX, 0, -minY),
                    new THREE.Vector3(maxX, 0, -maxY),
                    new THREE.Vector3(minX, 0, -maxY),
                    new THREE.Vector3(minX, 0, -minY)
                ]),
                new THREE.LineBasicMaterial({ color: 0x8B949E, linewidth: 1, opacity: 0.3, transparent: true })
            );
            scene.add(sacrificeOutline);

            // Add stock material as semi-transparent solid
            const stockWidth = maxX - minX;
            const stockDepth = maxY - minY;
            const stockHeight = stockHeightValue; // Use tube height for tubes, thickness for plates

            // Calculate and display stock size
            const toolDiameter = parseFloat(document.getElementById('toolDiameter').value) || 0.157;
            const stockSizeDisplay = document.getElementById('stockSizeDisplay');
            const stockSizeValue = document.getElementById('stockSizeValue');

            if (isAluminumTube) {
                // For tube: show profile dimensions × length (no margin)
                // Tube height from form input, tube width from DXF short dimension
                const tubeHeightInput = parseFloat(document.getElementById('tubeHeight').value) || 1.0;
                const dxfShort = dxfBounds ? Math.min(dxfBounds.width, dxfBounds.height) : Math.min(stockWidth, stockDepth);
                const tubeLength = dxfBounds ? Math.max(dxfBounds.width, dxfBounds.height) : Math.max(stockWidth, stockDepth);

                if (stockSizeDisplay && stockSizeValue) {
                    // Display as: width × height × length
                    stockSizeValue.textContent = `${dxfShort.toFixed(0)}" × ${tubeHeightInput.toFixed(0)}" × ${tubeLength.toFixed(3)}"`;
                    stockSizeDisplay.style.display = 'flex';
                }
            } else {
                // For plates: DXF bounding box + tool margin only if cutting perimeter
                // Account for rotation - swap DXF dimensions if rotated 90 or 270 degrees
                let dxfWidth = dxfBounds ? dxfBounds.width : stockWidth;
                let dxfHeight = dxfBounds ? dxfBounds.height : stockDepth;
                if (rotationAngle === 90 || rotationAngle === 270) {
                    [dxfWidth, dxfHeight] = [dxfHeight, dxfWidth];
                }

                // Check if toolpath extends beyond DXF bounds (indicating perimeter cutting)
                const tolerance = 0.01;
                const toolpathWidth = maxX - minX;
                const toolpathHeight = maxY - minY;

                // If toolpath is larger than DXF bounds, tool is cutting outside the part on that axis
                const cutsOutsideX = toolpathWidth > dxfWidth + tolerance;
                const cutsOutsideY = toolpathHeight > dxfHeight + tolerance;

                // Only add margin on axes where tool cuts outside the part
                const fullStockWidth = dxfWidth + (cutsOutsideX ? 2 * toolDiameter : 0);
                const fullStockDepth = dxfHeight + (cutsOutsideY ? 2 * toolDiameter : 0);

                if (stockSizeDisplay && stockSizeValue) {
                    stockSizeValue.textContent = `${fullStockWidth.toFixed(3)}" × ${fullStockDepth.toFixed(3)}"`;
                    stockSizeDisplay.style.display = 'flex';
                }
            }

            const stockGeometry = new THREE.BoxGeometry(stockWidth, stockHeight, stockDepth);
            const stockMaterial = new THREE.MeshStandardMaterial({
                color: 0xE8F0FF, // Light blue-white (aluminum-ish)
                transparent: true,
                opacity: 0.15, // More transparent so toolpaths show through
                metalness: 0.3,
                roughness: 0.7,
                side: THREE.DoubleSide,
                depthWrite: false // Critical! Allows lines to render through transparent material
            });
            
            const stockMesh = new THREE.Mesh(stockGeometry, stockMaterial);
            // Position at center of stock, halfway up from sacrifice board
            stockMesh.position.set(
                (minX + maxX) / 2,
                stockHeight / 2,
                -(minY + maxY) / 2
            );
            stockMesh.renderOrder = -1; // Render stock before toolpaths
            scene.add(stockMesh);

            // Render DXF geometry overlay (white lines on stock top surface)
            if (dxfGeometry && dxfGeometry.entities) {
                renderDxfGeometry(scene, dxfGeometry.entities, materialThickness);
            }

            // Create tool representation (endmill)
            const toolLength = Math.max(maxZ * 1.5, 1.0);
            const toolGeometry = new THREE.CylinderGeometry(
                toolDiameter / 2, 
                toolDiameter / 2, 
                toolLength, 
                16
            );
            const toolMaterial = new THREE.MeshStandardMaterial({
                color: 0xC0C0C0, // Silver
                metalness: 0.8,
                roughness: 0.2,
                emissive: 0x404040
            });
            toolMesh = new THREE.Mesh(toolGeometry, toolMaterial);
            toolMesh.userData.toolLength = toolLength; // Store for positioning
            scene.add(toolMesh);

            // Initialize toolpath lines
            updateToolpathDisplay(0);

            // Setup scrubber
            const scrubber = document.getElementById('toolpathScrubber');
            const scrubberContainer = document.getElementById('scrubberContainer');
            scrubberContainer.style.display = 'block';
            
            scrubber.max = toolpathMoves.length - 1;
            scrubber.value = 0;
            
            scrubber.oninput = (e) => {
                const moveIndex = parseInt(e.target.value);
                updateToolpathDisplay(moveIndex);
            };

            // Show playback controls
            document.getElementById('playbackControls').style.display = 'flex';

            let isPlaying = false;
            let playbackInterval = null;
            let playbackSpeed = 40; // moves per second (default 1x speed)

            // Get playback controls
            const playButton = document.getElementById('playButton');
            const restartButton = document.getElementById('restartButton');
            const playbackSpeedSelect = document.getElementById('playbackSpeed');
            const playIcon = playButton.querySelector('.play-icon');
            const pauseIcon = playButton.querySelector('.pause-icon');

            // Play/Pause button handler
            playButton.addEventListener('click', () => {
                if (isPlaying) {
                    stopPlayback();
                } else {
                    startPlayback();
                }
            });

            // Restart button handler
            restartButton.addEventListener('click', () => {
                scrubber.value = 0;
                updateToolpathDisplay(0);
                if (isPlaying) {
                    stopPlayback();
                    setTimeout(startPlayback, 100); // Brief pause before restart
                }
            });

            // Speed selector handler
            playbackSpeedSelect.addEventListener('change', (e) => {
                playbackSpeed = parseInt(e.target.value);
                if (isPlaying) {
                    // Restart playback with new speed
                    stopPlayback();
                    startPlayback();
                }
            });

            function startPlayback() {
                isPlaying = true;
                playButton.classList.add('playing');
                playIcon.style.display = 'none';
                pauseIcon.style.display = 'block';

                // Calculate interval based on speed (moves per second)
                const intervalMs = 1000 / playbackSpeed;

                playbackInterval = setInterval(() => {
                    const currentValue = parseInt(scrubber.value);
                    const maxValue = parseInt(scrubber.max);

                    if (currentValue >= maxValue) {
                        stopPlayback();
                        return;
                    }

                    scrubber.value = currentValue + 1;
                    updateToolpathDisplay(currentValue + 1);
                }, intervalMs);
            }

            function stopPlayback() {
                isPlaying = false;
                playButton.classList.remove('playing');
                playIcon.style.display = 'block';
                pauseIcon.style.display = 'none';

                if (playbackInterval) {
                    clearInterval(playbackInterval);
                    playbackInterval = null;
                }
            }

            // Camera positioning
            const viewDist = Math.max(maxX, maxY, maxZ) * 2;
            camera.position.set(viewDist * 0.7, viewDist * 0.7, viewDist * 0.7);
            camera.lookAt(maxX / 3, maxZ / 3, -maxY / 3);

            optimalCameraPosition = { x: camera.position.x, y: camera.position.y, z: camera.position.z };
            optimalLookAtPosition = { x: maxX / 3, y: maxZ / 3, z: -maxY / 3 };

            document.querySelector('.empty-state').style.display = 'none';
        }

        function updateToolpathDisplay(moveIndex) {
            if (toolpathMoves.length === 0) return;

            // Update scrubber labels
            document.getElementById('scrubberLabel').textContent = 
                `Move ${moveIndex + 1} of ${toolpathMoves.length}`;
            
            const currentMove = toolpathMoves[moveIndex];
            const moveType = currentMove.type === 'G0' ? 'Rapid' : 'Cut';
            document.getElementById('scrubberOperation').textContent =
                `${moveType}: ${currentMove.line}`;

            // Update tool position
            if (toolMesh) {
                const pos = currentMove.to;
                // Position tool so BOTTOM is at Z coordinate, not center
                // Cylinder center needs to be offset up by half its length
                const toolLength = toolMesh.userData.toolLength;
                toolMesh.position.set(pos.x, pos.z + toolLength / 2, -pos.y);
            }

            // Remove old toolpath lines
            if (completedLine) scene.remove(completedLine);
            if (upcomingLine) scene.remove(upcomingLine);

            // Build upcoming path first (gold) - draw this first so completed renders on top
            if (moveIndex < toolpathMoves.length - 1) {
                const upcomingPoints = [];
                for (let i = moveIndex; i < toolpathMoves.length; i++) {
                    const move = toolpathMoves[i];
                    if (i === moveIndex) {
                        upcomingPoints.push(new THREE.Vector3(move.from.x, move.from.z, -move.from.y));
                    }
                    upcomingPoints.push(new THREE.Vector3(move.to.x, move.to.z, -move.to.y));
                }
                const upcomingGeometry = new THREE.BufferGeometry().setFromPoints(upcomingPoints);
                upcomingLine = new THREE.Line(
                    upcomingGeometry,
                    new THREE.LineBasicMaterial({ 
                        color: 0xFDB515, 
                        linewidth: 3,
                        opacity: 0.8, 
                        transparent: true 
                    })
                );
                scene.add(upcomingLine);
            }

            // Build completed path (green) - draw this last so it's on top
            if (moveIndex > 0) {
                const completedPoints = [];
                for (let i = 0; i <= moveIndex; i++) {
                    const move = toolpathMoves[i];
                    if (i === 0) {
                        completedPoints.push(new THREE.Vector3(move.from.x, move.from.z, -move.from.y));
                    }
                    completedPoints.push(new THREE.Vector3(move.to.x, move.to.z, -move.to.y));
                }
                const completedGeometry = new THREE.BufferGeometry().setFromPoints(completedPoints);
                completedLine = new THREE.Line(
                    completedGeometry,
                    new THREE.LineBasicMaterial({ color: 0x2EA043, linewidth: 3 })
                );
                scene.add(completedLine);
            }
        }

        // Initialize on load
        // Initialize on load
        window.addEventListener('load', () => {
            initVisualization();
            initDxfSetup();

            // DEBUG: Check if Onshape provides context via JavaScript
            console.log('=== Onshape Context Debug ===');
            console.log('window.opener:', window.opener);
            console.log('window.parent:', window.parent);
            console.log('URL params:', new URLSearchParams(window.location.search));
            console.log('Onshape globals:', {
                onshape: typeof window.onshape !== 'undefined' ? window.onshape : 'undefined',
                OnshapeClient: typeof window.OnshapeClient !== 'undefined' ? window.OnshapeClient : 'undefined'
            });

            // Check for error message from Onshape import
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

            // Show info alert if using default config
            const usingDefaultConfig = window.ONSHAPE_DATA?.usingDefaultConfig || false;
            if (usingDefaultConfig) {
                const configInfoAlert = document.getElementById('configInfoAlert');
                if (configInfoAlert) {
                    configInfoAlert.style.display = 'block';
                }
            }

            // Auto-load DXF if coming from Onshape
            const dxfFile = window.ONSHAPE_DATA?.dxfFile || '';
            const fromOnshape = window.ONSHAPE_DATA?.fromOnshape || false;
            const onshapeSuggestedFilename = window.ONSHAPE_DATA?.suggestedFilename || '';
            
            if (dxfFile && fromOnshape) {
                console.log('Auto-loading DXF from Onshape:', dxfFile);
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
                        // Use suggested filename (not token) for the File object name
                        const filename = onshapeSuggestedFilename ?
                            `${onshapeSuggestedFilename}.dxf` :
                            (dxfFile.endsWith('.dxf') ? dxfFile : `${dxfFile}.dxf`);
                        const blob = new Blob([dxfContent], { type: 'application/dxf' });
                        const file = new File([blob], filename, { type: 'application/dxf' });

                        // Use appState to store file (accessible across scopes)
                        appState.uploadedFile = file;
                        appState.suggestedFilename = onshapeSuggestedFilename || null;

                        // Update UI elements
                        const fileNameEl = document.getElementById('fileName');
                        const fileSizeEl = document.getElementById('fileSize');
                        const fileInfoEl = document.getElementById('fileInfo');
                        const generateBtnEl = document.getElementById('generateBtn');

                        if (fileNameEl) fileNameEl.textContent = filename;
                        if (fileSizeEl) fileSizeEl.textContent = formatFileSize(dxfContent.length);
                        if (fileInfoEl) fileInfoEl.style.display = 'flex';
                        if (generateBtnEl) {
                            generateBtnEl.disabled = false;
                            generateBtnEl.textContent = '🚀 Generate Program';
                        }

                        // Parse for 2D setup view
                        parseDxfForSetup(dxfContent);

                        // Show success message
                        const statusDiv = document.getElementById('statusMessage');
                        if (statusDiv) {
                            statusDiv.textContent = '✅ Imported from Onshape! Orient your part and click Generate G-code.';
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
});

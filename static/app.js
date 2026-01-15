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

    // Global state
        let uploadedFile = null;
        let suggestedFilename = null; // For Onshape imports
        let gcodeContent = null;
        let outputFilename = null;
        let perimeterPoints = null; // Perimeter outline from postprocessor for 3D visualization
        let tubeDimensions = null; // Tube width/length from backend for 3D visualization
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

            uploadedFile = file;
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
                formData.append('suggested_filename', suggestedFilename); // Onshape filename
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
                perimeterPoints = data.perimeter_points || null; // Store perimeter for 3D visualization

                // Store tube dimensions from backend for accurate 3D visualization
                if (data.parameters && data.parameters.tube_width !== undefined) {
                    tubeDimensions = {
                        width: data.parameters.tube_width,
                        length: data.parameters.tube_length,
                        height: data.parameters.tube_height
                    };
                    console.log('Tube dimensions from backend:', tubeDimensions);
                } else {
                    tubeDimensions = null;
                }

                // Show results
                showResults(data);

                // Switch to preview mode and visualize G-code
                switchMode('preview');
                visualizeGcode(data.gcode);

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

        // Download G-code
        downloadBtn.addEventListener('click', () => {
            if (!outputFilename) return;
            window.location.href = `/download/${outputFilename}`;
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
        let rotationAngle = 0; // Rotation in degrees (0-359)
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
            const rotationInput = document.getElementById('rotationInput');

            rotationInput.addEventListener('input', () => {
                let value = parseInt(rotationInput.value) || 0;
                // Normalize to 0-359 range
                value = ((value % 360) + 360) % 360;
                rotationAngle = value;
                renderDxfSetup();
            });

            rotationInput.addEventListener('blur', () => {
                // Normalize displayed value on blur
                rotationInput.value = rotationAngle;
            });

            document.getElementById('optimizeRotationBtn').addEventListener('click', () => {
                if (!dxfGeometry || !dxfBounds) return;

                const optimalAngle = findMinAreaRotation();
                rotationAngle = optimalAngle;
                rotationInput.value = rotationAngle;
                renderDxfSetup();
            });

            // Mode toggle listeners
            document.querySelectorAll('.mode-button').forEach(btn => {
                btn.addEventListener('click', () => switchMode(btn.dataset.mode));
            });
        }

        // Find the rotation angle that minimizes bounding box area
        function findMinAreaRotation() {
            if (!dxfGeometry || !dxfBounds) return 0;

            // Helper to rotate a point
            function rotatePoint(x, y, angle) {
                const rad = -angle * Math.PI / 180;
                const cos = Math.cos(rad);
                const sin = Math.sin(rad);
                return {
                    x: x * cos - y * sin,
                    y: x * sin + y * cos
                };
            }

            // Calculate bounding box area for a given angle
            function getBoundsArea(angle) {
                let minX = Infinity, maxX = -Infinity;
                let minY = Infinity, maxY = -Infinity;

                function updateBounds(x, y) {
                    const dx = x - dxfBounds.centerX;
                    const dy = y - dxfBounds.centerY;
                    const rotated = rotatePoint(dx, dy, angle);
                    minX = Math.min(minX, rotated.x);
                    maxX = Math.max(maxX, rotated.x);
                    minY = Math.min(minY, rotated.y);
                    maxY = Math.max(maxY, rotated.y);
                }

                function addCircleBounds(cx, cy, radius) {
                    const dx = cx - dxfBounds.centerX;
                    const dy = cy - dxfBounds.centerY;
                    const rotatedCenter = rotatePoint(dx, dy, angle);
                    minX = Math.min(minX, rotatedCenter.x - radius);
                    maxX = Math.max(maxX, rotatedCenter.x + radius);
                    minY = Math.min(minY, rotatedCenter.y - radius);
                    maxY = Math.max(maxY, rotatedCenter.y + radius);
                }

                if (dxfGeometry.entities) {
                    dxfGeometry.entities.forEach(entity => {
                        switch(entity.type) {
                            case 'CIRCLE':
                                addCircleBounds(entity.center.x, entity.center.y, entity.radius);
                                break;
                            case 'ARC':
                                addCircleBounds(entity.center.x, entity.center.y, entity.radius);
                                break;
                            case 'LINE':
                                entity.vertices.forEach(v => updateBounds(v.x, v.y));
                                break;
                            case 'LWPOLYLINE':
                            case 'POLYLINE':
                                if (entity.vertices) {
                                    entity.vertices.forEach(v => updateBounds(v.x, v.y));
                                }
                                break;
                            case 'SPLINE':
                                if (entity.controlPoints) {
                                    entity.controlPoints.forEach(p => updateBounds(p.x, p.y));
                                }
                                break;
                            case 'ELLIPSE':
                                const majorRadius = Math.sqrt(entity.majorAxisEndPoint.x ** 2 + entity.majorAxisEndPoint.y ** 2);
                                addCircleBounds(entity.center.x, entity.center.y, majorRadius);
                                break;
                        }
                    });
                }

                const width = maxX - minX;
                const height = maxY - minY;
                return width * height;
            }

            // Search angles 0-179 (180° gives same bounding box dimensions)
            let minArea = Infinity;
            let bestAngle = 0;

            for (let angle = 0; angle < 180; angle++) {
                const area = getBoundsArea(angle);
                if (area < minArea) {
                    minArea = area;
                    bestAngle = angle;
                }
            }

            return bestAngle;
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
                                updateBounds(entity.center.x - entity.radius, entity.center.y - entity.radius);
                                updateBounds(entity.center.x + entity.radius, entity.center.y + entity.radius);
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
            let minX = Infinity, maxX = -Infinity;
            let minY = Infinity, maxY = -Infinity;
            
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
                
                // Parse coordinates
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
                        minX = Math.min(minX, val);
                        maxX = Math.max(maxX, val);
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
                        minY = Math.min(minY, val);
                        maxY = Math.max(maxY, val);
                    }
                } else if (line === '40' && i + 1 < lines.length) {
                    const val = parseFloat(lines[i + 1]);
                    if (!isNaN(val) && val < 1e10) {
                        entityData.radius = val;
                        if (entityData.centerX !== undefined) {
                            minX = Math.min(minX, entityData.centerX - val);
                            maxX = Math.max(maxX, entityData.centerX + val);
                            minY = Math.min(minY, entityData.centerY - val);
                            maxY = Math.max(maxY, entityData.centerY + val);
                        }
                    }
                } else if (line.trim() === '50' && i + 1 < lines.length && currentEntity === 'ARC') {
                    entityData.startAngle = parseFloat(lines[i + 1].trim());
                } else if (line.trim() === '51' && i + 1 < lines.length && currentEntity === 'ARC') {
                    entityData.endAngle = parseFloat(lines[i + 1].trim());
                } else if (line === '11' && i + 1 < lines.length) {
                    const val = parseFloat(lines[i + 1]);
                    if (!isNaN(val) && Math.abs(val) < 1e10) {
                        entityData.x2 = val;
                        minX = Math.min(minX, val);
                        maxX = Math.max(maxX, val);
                    }
                } else if (line === '21' && i + 1 < lines.length) {
                    const val = parseFloat(lines[i + 1]);
                    if (!isNaN(val) && Math.abs(val) < 1e10) {
                        entityData.y2 = val;
                        minY = Math.min(minY, val);
                        maxY = Math.max(maxY, val);
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
            
            if (minX === Infinity) {
                console.warn('⚠️ No valid geometry found, using fallback 10×10 bounds');
                minX = 0; maxX = 10;
                minY = 0; maxY = 10;
            }
            
            console.log(`Manual parse: ${entities.length} entities`);
            console.log(`Bounds: X=[${minX.toFixed(3)}, ${maxX.toFixed(3)}], Y=[${minY.toFixed(3)}, ${maxY.toFixed(3)}]`);
            
            dxfGeometry = { 
                minX, maxX, minY, maxY,
                entities: entities
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
                    shape: data.closed || false  // 'shape' property is used by renderer to close path
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
            
            // Helper function to rotate a point around the part center
            function rotatePoint(x, y, angle) {
                const rad = -angle * Math.PI / 180; // Negative for clockwise
                const cos = Math.cos(rad);
                const sin = Math.sin(rad);
                return {
                    x: x * cos - y * sin,
                    y: x * sin + y * cos
                };
            }

            // Calculate actual bounding box of rotated geometry
            // by iterating through all points and finding min/max
            let minX = Infinity, maxX = -Infinity;
            let minY = Infinity, maxY = -Infinity;

            function updateBounds(x, y) {
                // Translate to center, rotate, then track bounds
                const dx = x - dxfBounds.centerX;
                const dy = y - dxfBounds.centerY;
                const rotated = rotatePoint(dx, dy, rotationAngle);
                minX = Math.min(minX, rotated.x);
                maxX = Math.max(maxX, rotated.x);
                minY = Math.min(minY, rotated.y);
                maxY = Math.max(maxY, rotated.y);
            }

            // Process all geometry to find rotated bounds
            // For circles/arcs: rotate center first, then add ±radius to rotated center
            // For lines/polylines: rotate each vertex
            function addCircleBounds(cx, cy, radius) {
                // Rotate the center point
                const dx = cx - dxfBounds.centerX;
                const dy = cy - dxfBounds.centerY;
                const rotatedCenter = rotatePoint(dx, dy, rotationAngle);
                // Circle bounds are at rotated center ± radius
                minX = Math.min(minX, rotatedCenter.x - radius);
                maxX = Math.max(maxX, rotatedCenter.x + radius);
                minY = Math.min(minY, rotatedCenter.y - radius);
                maxY = Math.max(maxY, rotatedCenter.y + radius);
            }

            if (dxfGeometry.entities) {
                dxfGeometry.entities.forEach(entity => {
                    switch(entity.type) {
                        case 'CIRCLE':
                            addCircleBounds(entity.center.x, entity.center.y, entity.radius);
                            break;
                        case 'ARC':
                            // Use full circle bounds as conservative estimate
                            addCircleBounds(entity.center.x, entity.center.y, entity.radius);
                            break;
                        case 'LINE':
                            entity.vertices.forEach(v => updateBounds(v.x, v.y));
                            break;
                        case 'LWPOLYLINE':
                        case 'POLYLINE':
                            if (entity.vertices) {
                                entity.vertices.forEach(v => updateBounds(v.x, v.y));
                            }
                            break;
                        case 'SPLINE':
                            if (entity.controlPoints) {
                                entity.controlPoints.forEach(p => updateBounds(p.x, p.y));
                            }
                            break;
                        case 'ELLIPSE':
                            const majorRadius = Math.sqrt(entity.majorAxisEndPoint.x ** 2 + entity.majorAxisEndPoint.y ** 2);
                            addCircleBounds(entity.center.x, entity.center.y, majorRadius);
                            break;
                    }
                });
            }

            // Calculate display dimensions from rotated bounds
            let displayWidth = maxX - minX;
            let displayHeight = maxY - minY;

            // Fallback if no geometry found
            if (!isFinite(displayWidth) || displayWidth <= 0) {
                displayWidth = dxfBounds.width;
                displayHeight = dxfBounds.height;
            }

            const scale = Math.min(availWidth / displayWidth, availHeight / displayHeight);

            // Canvas center
            const centerX = width / 2;
            const centerY = height / 2;

            // Center of the rotated bounding box (used to keep origin fixed)
            const boundsCenterX = (minX + maxX) / 2;
            const boundsCenterY = (minY + maxY) / 2;

            function toCanvasCoords(x, y) {
                // Translate to part center
                let dx = x - dxfBounds.centerX;
                let dy = y - dxfBounds.centerY;

                // Apply rotation
                const rotated = rotatePoint(dx, dy, rotationAngle);

                // Offset by bounds center so the BOUNDING BOX is centered on canvas
                // This keeps the origin (bottom-left of box) at a fixed position
                return {
                    x: centerX + (rotated.x - boundsCenterX) * scale,
                    y: centerY - (rotated.y - boundsCenterY) * scale
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
            
            // Calculate bounding box corners - box is centered on canvas
            const boxLeft = centerX - displayWidth * scale / 2;
            const boxRight = centerX + displayWidth * scale / 2;
            const boxTop = centerY - displayHeight * scale / 2;
            const boxBottom = centerY + displayHeight * scale / 2;
            
            // Draw bounding box (dashed, NOT rotated)
            ctx.strokeStyle = '#8B949E';
            ctx.lineWidth = 2;
            ctx.setLineDash([5, 5]);
            ctx.strokeRect(boxLeft, boxTop, boxRight - boxLeft, boxBottom - boxTop);
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
            ctx.fillStyle = '#8B949E';
            ctx.font = '14px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif';
            ctx.textAlign = 'center';
            ctx.textBaseline = 'top';
            ctx.fillText(
                `${displayWidth.toFixed(2)}" × ${displayHeight.toFixed(2)}" (${rotationAngle}°)`,
                width / 2,
                20
            );
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
            let previousMousePosition = { x: 0, y: 0 };

            canvas.addEventListener('mousedown', (e) => {
                isDragging = true;
                previousMousePosition = { x: e.clientX, y: e.clientY };
            });

            canvas.addEventListener('mousemove', (e) => {
                if (!isDragging) return;

                const deltaX = e.clientX - previousMousePosition.x;
                const deltaY = e.clientY - previousMousePosition.y;

                // Rotate camera
                const rotationSpeed = 0.005;
                camera.position.x = camera.position.x * Math.cos(deltaX * rotationSpeed) - camera.position.z * Math.sin(deltaX * rotationSpeed);
                camera.position.z = camera.position.x * Math.sin(deltaX * rotationSpeed) + camera.position.z * Math.cos(deltaX * rotationSpeed);
                camera.position.y += deltaY * rotationSpeed * 5;

                camera.lookAt(0, 0, 0);

                previousMousePosition = { x: e.clientX, y: e.clientY };
            });

            canvas.addEventListener('mouseup', () => {
                isDragging = false;
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

        // Create 3D part geometry from DXF entities (for plates)
        // Uses actual part outline from DXF polylines, with holes from circles
        function createPartFromDxf(dxfData, thickness, gcodeMinX, gcodeMinY, gcodeMaxX, gcodeMaxY) {
            console.log('createPartFromDxf: G-code bounds', { gcodeMinX, gcodeMinY, gcodeMaxX, gcodeMaxY });

            // Calculate transformation from DXF coordinates to G-code coordinates
            const dxfCenterX = dxfData ? (dxfData.minX + dxfData.maxX) / 2 : 0;
            const dxfCenterY = dxfData ? (dxfData.minY + dxfData.maxY) / 2 : 0;
            const gcodeCenterX = (gcodeMinX + gcodeMaxX) / 2;
            const gcodeCenterY = (gcodeMinY + gcodeMaxY) / 2;
            const rad = -rotationAngle * Math.PI / 180;

            function transformDxfToGcode(x, y) {
                let dx = x - dxfCenterX;
                let dy = y - dxfCenterY;
                const rotatedX = dx * Math.cos(rad) - dy * Math.sin(rad);
                const rotatedY = dx * Math.sin(rad) + dy * Math.cos(rad);
                return {
                    x: rotatedX + gcodeCenterX,
                    y: rotatedY + gcodeCenterY
                };
            }

            // Find the outer perimeter from closed polylines
            let outerShape = null;
            let largestArea = 0;

            if (dxfData && dxfData.entities) {
                // Helper to check if a polyline is closed (by flag or by vertices)
                function isPolylineClosed(poly) {
                    if (!poly.vertices || poly.vertices.length < 3) return false;
                    // Check closed flag
                    if (poly.shape) return true;
                    // Check if first and last vertices are the same (within tolerance)
                    const first = poly.vertices[0];
                    const last = poly.vertices[poly.vertices.length - 1];
                    const tolerance = 0.001;
                    return Math.abs(first.x - last.x) < tolerance && Math.abs(first.y - last.y) < tolerance;
                }

                // Look for LWPOLYLINE entities that are closed
                const allPolylines = dxfData.entities.filter(e => e.type === 'LWPOLYLINE');
                console.log('createPartFromDxf: Total polylines:', allPolylines.length);

                const closedPolylines = allPolylines.filter(isPolylineClosed);
                console.log('createPartFromDxf: Closed polylines:', closedPolylines.length);

                // Find the largest closed polyline (outer perimeter)
                for (const poly of closedPolylines) {
                    // Calculate approximate area using bounding box
                    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
                    for (const v of poly.vertices) {
                        minX = Math.min(minX, v.x);
                        maxX = Math.max(maxX, v.x);
                        minY = Math.min(minY, v.y);
                        maxY = Math.max(maxY, v.y);
                    }
                    const area = (maxX - minX) * (maxY - minY);
                    console.log('createPartFromDxf: Polyline with', poly.vertices.length, 'vertices, area:', area.toFixed(2));

                    if (area > largestArea) {
                        largestArea = area;
                        // Create shape from this polyline
                        outerShape = new THREE.Shape();
                        const firstPt = transformDxfToGcode(poly.vertices[0].x, poly.vertices[0].y);
                        outerShape.moveTo(firstPt.x, firstPt.y);
                        for (let i = 1; i < poly.vertices.length; i++) {
                            const pt = transformDxfToGcode(poly.vertices[i].x, poly.vertices[i].y);
                            outerShape.lineTo(pt.x, pt.y);
                        }
                        outerShape.closePath();
                    }
                }
            }

            // Fallback: use perimeter points from postprocessor, or rectangular bounds
            if (!outerShape) {
                if (perimeterPoints && perimeterPoints.length > 2) {
                    // Use perimeter points from postprocessor (already in G-code coordinates)
                    console.log('createPartFromDxf: Using perimeter from postprocessor with', perimeterPoints.length, 'points');
                    outerShape = new THREE.Shape();
                    outerShape.moveTo(perimeterPoints[0][0], perimeterPoints[0][1]);
                    for (let i = 1; i < perimeterPoints.length; i++) {
                        outerShape.lineTo(perimeterPoints[i][0], perimeterPoints[i][1]);
                    }
                    outerShape.closePath();
                } else {
                    console.log('createPartFromDxf: No closed polyline or perimeter found, using rectangular bounds');
                    console.log('createPartFromDxf: DXF entities:', dxfData?.entities?.map(e => e.type));
                    outerShape = new THREE.Shape();
                    outerShape.moveTo(gcodeMinX, gcodeMinY);
                    outerShape.lineTo(gcodeMaxX, gcodeMinY);
                    outerShape.lineTo(gcodeMaxX, gcodeMaxY);
                    outerShape.lineTo(gcodeMinX, gcodeMaxY);
                    outerShape.closePath();
                }
            } else {
                console.log('createPartFromDxf: Using polyline outline with area', largestArea.toFixed(2));
            }

            // Add circles as holes from DXF, transformed to G-code coordinates
            if (dxfData && dxfData.entities) {
                const circles = dxfData.entities.filter(e => e.type === 'CIRCLE');
                for (const circle of circles) {
                    if (circle.center && circle.radius) {
                        const transformed = transformDxfToGcode(circle.center.x, circle.center.y);
                        const holePath = new THREE.Path();
                        // true = clockwise (required for holes - opposite of outer shape)
                        holePath.absarc(transformed.x, transformed.y, circle.radius, 0, Math.PI * 2, true);
                        outerShape.holes.push(holePath);
                    }
                }
                console.log('createPartFromDxf: Added', outerShape.holes.length, 'holes');
            }

            const geometry = new THREE.ExtrudeGeometry(outerShape, {
                depth: thickness,
                bevelEnabled: false
            });

            // Rotate so extrusion goes up (Y axis in Three.js)
            // rotateX(-90°): (x, y, z) -> (x, z, -y)
            // So shape at (gX, gY) with Z extrusion becomes (gX, 0..thickness, -gY)
            geometry.rotateX(-Math.PI / 2);

            const material = new THREE.MeshStandardMaterial({
                color: 0xC0C8D0,
                metalness: 0.6,
                roughness: 0.4,
                side: THREE.DoubleSide
            });

            const mesh = new THREE.Mesh(geometry, material);
            // No position offset needed - geometry is already at correct coordinates

            return mesh;
        }

        // Create 3D tube geometry (hollow box tube with holes in top and bottom faces)
        // Uses G-code bounds to ensure alignment with toolpath
        // dxfData contains circles that represent holes to cut through walls
        function createTubeGeometry(tubeWidth, tubeHeight, tubeLength, wallThickness, gcodeMinX, gcodeMinY, gcodeMaxX, gcodeMaxY, dxfData) {
            if (!tubeWidth || !tubeHeight || !tubeLength || tubeWidth <= 0 || tubeHeight <= 0 || tubeLength <= 0) {
                console.warn('Invalid tube dimensions:', { tubeWidth, tubeHeight, tubeLength });
                return null;
            }

            console.log('createTubeGeometry:', { tubeWidth, tubeHeight, tubeLength, wallThickness });
            console.log('createTubeGeometry G-code bounds:', { gcodeMinX, gcodeMinY, gcodeMaxX, gcodeMaxY });

            const material = new THREE.MeshStandardMaterial({
                color: 0xC0C8D0,
                metalness: 0.6,
                roughness: 0.4,
                side: THREE.DoubleSide
            });

            const group = new THREE.Group();

            // Build tube from 4 wall panels:
            // - Left and right walls: solid vertical panels
            // - Top and bottom walls: horizontal panels with holes from DXF
            // - No end caps: open ends show hollow interior
            //
            // Tube coordinate system (in Three.js space):
            // - X: 0 to tubeWidth (matches G-code X)
            // - Y: 0 to tubeHeight (tube height, vertical)
            // - Z: 0 to -tubeLength (tube extends backward from front face)
            //
            // The G-code toolpath is at the front face (Z ≈ 0 in Three.js, Y ≈ 0 in G-code)
            // G-code Y is small (facing depth) but tube extends full length backward

            // Use actual tube dimensions, positioned at origin
            // The tube should be at X=0 to tubeWidth, regardless of G-code toolpath bounds
            // (G-code may have lead-in/out moves that extend beyond the tube)
            const actualTubeMinX = 0;  // Tube starts at origin
            const actualTubeMaxX = tubeWidth;  // Tube ends at tube width
            const actualTubeMinY = 0;  // Front face at Y=0
            const actualTubeMaxY = tubeLength;  // Tube extends to full length

            console.log('createTubeGeometry: Actual tube bounds:', {
                x: [actualTubeMinX, actualTubeMaxX],
                y: [actualTubeMinY, actualTubeMaxY]
            });

            // Calculate transformation from DXF coordinates to tube coordinates
            // DXF may be rotated and/or translated
            const dxfCenterX = dxfData ? (dxfData.minX + dxfData.maxX) / 2 : 0;
            const dxfCenterY = dxfData ? (dxfData.minY + dxfData.maxY) / 2 : 0;
            // Transform to tube center (using actual tube bounds)
            const tubeCenterX = (actualTubeMinX + actualTubeMaxX) / 2;
            const tubeCenterY = (actualTubeMinY + actualTubeMaxY) / 2;
            const rad = -rotationAngle * Math.PI / 180; // Negative for clockwise rotation

            console.log('createTubeGeometry: DXF center', { dxfCenterX, dxfCenterY });
            console.log('createTubeGeometry: Tube center', { tubeCenterX, tubeCenterY });
            console.log('createTubeGeometry: Rotation angle', rotationAngle);

            // Transform a point from DXF space to tube space
            function transformDxfToTube(x, y) {
                // Translate to DXF center
                let dx = x - dxfCenterX;
                let dy = y - dxfCenterY;
                // Rotate
                const rotatedX = dx * Math.cos(rad) - dy * Math.sin(rad);
                const rotatedY = dx * Math.sin(rad) + dy * Math.cos(rad);
                // Translate to tube center
                return {
                    x: rotatedX + tubeCenterX,
                    y: rotatedY + tubeCenterY
                };
            }

            // Helper: create a horizontal wall shape with holes from DXF circles
            // Uses actual tube dimensions for the outer boundary
            function createWallWithHoles() {
                const shape = new THREE.Shape();
                // Counter-clockwise outer boundary using actual tube dimensions
                shape.moveTo(actualTubeMinX, actualTubeMinY);
                shape.lineTo(actualTubeMaxX, actualTubeMinY);
                shape.lineTo(actualTubeMaxX, actualTubeMaxY);
                shape.lineTo(actualTubeMinX, actualTubeMaxY);
                shape.closePath();

                // Add circles from DXF as holes, transformed to tube coordinates
                if (dxfData && dxfData.entities) {
                    const circles = dxfData.entities.filter(e => e.type === 'CIRCLE');
                    for (const circle of circles) {
                        if (circle.center && circle.radius) {
                            // Transform circle center from DXF to tube coordinates
                            const transformed = transformDxfToTube(circle.center.x, circle.center.y);
                            const holePath = new THREE.Path();
                            // true = clockwise (required for holes - opposite of outer shape)
                            holePath.absarc(transformed.x, transformed.y, circle.radius,
                                           0, Math.PI * 2, true);
                            shape.holes.push(holePath);
                            console.log('Hole at DXF', circle.center, '-> tube', transformed);
                        }
                    }
                }
                return shape;
            }

            // LEFT WALL (solid) - vertical panel at tube left edge, full height
            const leftGeom = new THREE.BoxGeometry(wallThickness, tubeHeight, tubeLength);
            const leftMesh = new THREE.Mesh(leftGeom, material);
            leftMesh.position.set(
                actualTubeMinX + wallThickness / 2,
                tubeHeight / 2,
                -(actualTubeMinY + tubeLength / 2)
            );
            group.add(leftMesh);

            // RIGHT WALL (solid) - vertical panel at tube right edge, full height
            const rightGeom = new THREE.BoxGeometry(wallThickness, tubeHeight, tubeLength);
            const rightMesh = new THREE.Mesh(rightGeom, material);
            rightMesh.position.set(
                actualTubeMaxX - wallThickness / 2,
                tubeHeight / 2,
                -(actualTubeMinY + tubeLength / 2)
            );
            group.add(rightMesh);

            // TOP WALL (with holes) - horizontal panel at top of tube
            const topShape = createWallWithHoles();
            const topGeom = new THREE.ExtrudeGeometry(topShape, {
                depth: wallThickness,
                bevelEnabled: false
            });
            // Rotate so extrusion goes up (Y axis): (x, y, z) -> (x, z, -y)
            topGeom.rotateX(-Math.PI / 2);
            const topMesh = new THREE.Mesh(topGeom, material);
            topMesh.position.set(0, tubeHeight - wallThickness, 0);
            group.add(topMesh);
            console.log('createTubeGeometry: Top wall has', topShape.holes.length, 'holes');

            // BOTTOM WALL (with holes) - horizontal panel at Y = 0
            const bottomShape = createWallWithHoles();
            const bottomGeom = new THREE.ExtrudeGeometry(bottomShape, {
                depth: wallThickness,
                bevelEnabled: false
            });
            // Rotate so extrusion goes up (Y axis)
            bottomGeom.rotateX(-Math.PI / 2);
            const bottomMesh = new THREE.Mesh(bottomGeom, material);
            bottomMesh.position.set(0, 0, 0);
            group.add(bottomMesh);
            console.log('createTubeGeometry: Bottom wall has', bottomShape.holes.length, 'holes');

            return group;
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

            // Material boundaries - only show for tubes (plates show actual part shape)
            if (isAluminumTube) {
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
            }

            // Add 3D part geometry
            const stockWidth = maxX - minX;
            const stockDepth = maxY - minY;
            const stockHeight = stockHeightValue; // Use tube height for tubes, thickness for plates

            // Calculate and display stock size
            const toolDiameter = parseFloat(document.getElementById('toolDiameter').value) || 0.157;
            const stockSizeDisplay = document.getElementById('stockSizeDisplay');
            const stockSizeValue = document.getElementById('stockSizeValue');

            if (isAluminumTube) {
                // For tube: use dimensions from backend if available, otherwise fallback to DXF bounds
                let tubeWidthDisplay, tubeLengthDisplay, tubeHeightDisplay;

                if (tubeDimensions) {
                    // Use accurate dimensions from backend
                    tubeWidthDisplay = tubeDimensions.width;
                    tubeLengthDisplay = tubeDimensions.length;
                    tubeHeightDisplay = tubeDimensions.height;
                    console.log('Using tube dimensions from backend:', tubeDimensions);
                } else {
                    // Fallback to DXF bounds estimation
                    const tubeHeightInput = parseFloat(document.getElementById('tubeHeight').value) || 1.0;
                    tubeWidthDisplay = dxfBounds ? Math.min(dxfBounds.width, dxfBounds.height) : Math.min(stockWidth, stockDepth);
                    tubeLengthDisplay = dxfBounds ? Math.max(dxfBounds.width, dxfBounds.height) : Math.max(stockWidth, stockDepth);
                    tubeHeightDisplay = tubeHeightInput;
                }

                if (stockSizeDisplay && stockSizeValue) {
                    // Display as: width × height × length
                    stockSizeValue.textContent = `${tubeWidthDisplay.toFixed(3)}" × ${tubeHeightDisplay.toFixed(3)}" × ${tubeLengthDisplay.toFixed(3)}"`;
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

            // Create 3D part geometry (hollow tubes or plates with holes)
            let partMesh = null;

            if (isAluminumTube) {
                // Create hollow box tube with holes from DXF
                // Use actual tube dimensions from backend if available, otherwise fall back to G-code bounds
                const actualTubeWidth = tubeDimensions ? tubeDimensions.width : stockWidth;
                const actualTubeHeight = tubeDimensions ? tubeDimensions.height : stockHeight;
                const actualTubeLength = tubeDimensions ? tubeDimensions.length : stockDepth;

                console.log('Creating tube with dimensions:', {
                    width: actualTubeWidth,
                    height: actualTubeHeight,
                    length: actualTubeLength,
                    fromBackend: !!tubeDimensions
                });

                partMesh = createTubeGeometry(actualTubeWidth, actualTubeHeight, actualTubeLength, materialThickness, minX, minY, maxX, maxY, dxfGeometry);
            } else {
                // Create plate with holes from DXF, aligned to G-code bounds
                partMesh = createPartFromDxf(dxfGeometry, materialThickness, minX, minY, maxX, maxY);
            }

            if (partMesh) {
                partMesh.renderOrder = -1;
                scene.add(partMesh);
            } else {
                // Fallback to simple stock box
                const stockGeometry = new THREE.BoxGeometry(stockWidth, stockHeight, stockDepth);
                const stockMaterial = new THREE.MeshStandardMaterial({
                    color: 0xE8F0FF,
                    transparent: true,
                    opacity: 0.15,
                    metalness: 0.3,
                    roughness: 0.7,
                    side: THREE.DoubleSide,
                    depthWrite: false
                });

                const stockMesh = new THREE.Mesh(stockGeometry, stockMaterial);
                stockMesh.position.set(
                    (minX + maxX) / 2,
                    stockHeight / 2,
                    -(minY + maxY) / 2
                );
                stockMesh.renderOrder = -1;
                scene.add(stockMesh);
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
                `${moveType}: ${currentMove.line.substring(0, 40)}`;

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
                        const blob = new Blob([dxfContent], { type: 'application/dxf' });
                        const file = new File([blob], dxfFile, { type: 'application/dxf' });
                        
                        // Set file state and enable generate button
                        uploadedFile = file;
                        suggestedFilename = onshapeSuggestedFilename || null; // Store suggested name
                        fileName.textContent = dxfFile;
                        fileSize.textContent = formatFileSize(dxfContent.length);
                        fileInfo.style.display = 'flex';
                        generateBtn.disabled = false;
                        generateBtn.textContent = '🚀 Generate Program';
                        hideError();
                        hideResults();
                        
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

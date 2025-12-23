// @ts-check

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
        document.getElementById('rotationDisplay').textContent = rotationAngle + '°';
        renderDxfSetup();
    });
    
    // Mode toggle listeners
    document.querySelectorAll('.mode-button').forEach(btn => {
        btn.addEventListener('click', () => switchMode(btn.dataset.mode));
    });
}

// Parse DXF geometry from file using dxf-parser library
function parseDxfForSetup(dxfContent) {
    try {
        // sooo uhhhhh....
        // https://github.com/gdsestimating/dxf-parser/blob/887a26af9181c62bb5d016b606882d9897f5b7d7/src/DxfParser.js
        // thats the library that is supposed to be used
        // but mr claude ai never actually loaded it
        // so this entire 100 line function is dead code?

        // Check if library loaded
        if (typeof window.DxfParser === 'undefined') {
            console.error('DxfParser library not loaded');
            // Fall back to simple manual parsing
            parseDxfManually(dxfContent);
            return;
        }
        alert("Testing to see if dead code, ping me if you see this");
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
        if (line === 'CIRCLE' || line === 'ARC' || line === 'LINE' || line === 'LWPOLYLINE') {
            if (currentEntity) {
                entities.push(createEntity(currentEntity, entityData));
            }
            currentEntity = line;
            entityData = { type: line };
            if (line === 'LWPOLYLINE') {
                entityData.vertices = [];
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

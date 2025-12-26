// @ts-check

/**
 * DXFSetupPreview
 * Handles the visualization, layout, and manipulation of DXF parts on a canvas.
 */
class DXFSetupPreview {
    constructor(config) {
        this.canvas = typeof config.canvas === 'string' ? document.querySelector(config.canvas) : config.canvas;
        this.ctx = this.canvas.getContext('2d');
        this.container = this.canvas.parentElement;

        // Bind Input Elements
        this.inputs = {
            x: document.querySelector(config.inputs.x),
            y: document.querySelector(config.inputs.y),
            r: document.querySelector(config.inputs.rotation),
            s: document.querySelector(config.inputs.scale)
        };
        this.toolDiameter = config.toolDiameter || 1.0;

        // State
        this.parts = [];
        this.selectedPartId = null;

        // Viewport State
        this.view = {
            scale: 10,
            pan: { x: 0, y: 0 }
        };

        // Interaction State
        this.interaction = null;

        // Configuration
        this.colors = config.colors || [
            "#58A6FF", "#7EE787", "#D29922", "#DB6D28", "#F85149", "#A371F7"
        ];

        this.initEventListeners();
        this.initResizeHandler();
    }
    // --- Public Methods ---
    addPart(id, name, entities) {
        let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;

        const updateBounds = (x, y) => {
            minX = Math.min(minX, x); maxX = Math.max(maxX, x);
            minY = Math.min(minY, y); maxY = Math.max(maxY, y);
        };

        entities.forEach(ent => {
            if (ent.type === 'LINE') {
                updateBounds(ent.vertices[0].x, ent.vertices[0].y);
                updateBounds(ent.vertices[1].x, ent.vertices[1].y);
            } else if (ent.vertices) {
                ent.vertices.forEach(v => updateBounds(v.x, v.y));
            } else if (ent.center && ent.radius) {
                updateBounds(ent.center.x - ent.radius, ent.center.y - ent.radius);
                updateBounds(ent.center.x + ent.radius, ent.center.y + ent.radius);
            }
        });

        if (minX === Infinity) return;

        const width = maxX - minX;
        const height = maxY - minY;

        // Calculate Stack Position (Start next to previous, aligned to Y=0)
        let visualStartX = 0;
        if (this.parts.length > 0) {
            const last = this.parts[this.parts.length - 1];
            // Calculate where the last part visually ends in World Space
            const lastHalfWidth = (last.bounds.width * last.transform.s) / 2;
            visualStartX = last.transform.x + lastHalfWidth + 2.0; // +2.0 padding
        }

        const part = {
            id: id,
            name: name,
            entities: entities,
            bounds: { width, height, minX, maxX, minY, maxY },
            center: { x: (minX + maxX) / 2, y: (minY + maxY) / 2 },
            // Position center so bottom-left is at (visualStartX, 0)
            transform: { 
                x: visualStartX + (width / 2), 
                y: 0 + (height / 2), 
                r: 0, 
                s: 1 
            },
            color: this.colors[this.parts.length % this.colors.length],
            isColliding: false
        };

        this.parts.push(part);
        this.autoFit();
        this.selectPart(id);
    }
    removePart(id) {
        this.parts = this.parts.filter(p => p.id !== id);
        if (this.selectedPartId === id) this.selectPart(null);
        this.render();
    }
    selectPart(id) {
        this.selectedPartId = id;
        this.updateInputFields();
        this.render();
    }
    clear() {
        this.parts = [];
        this.selectedPartId = null;
        this.updateInputFields();
        this.render();
    }
    show() {
        this.canvas.style.display = 'block';
        this.handleResize();
    }
    hide() {
        this.canvas.style.display = 'none';
    }
    setCollisions(collisionIds) {
        this.parts.forEach(p => {
            p.isColliding = collisionIds.includes(p.id);
        });
        this.render();
    }
    // --- Resize Logic (Fixes Squishing) ---
    initResizeHandler() {
        const observer = new ResizeObserver(() => {
            this.handleResize();
        });
        observer.observe(this.container);
        // Initial call
        requestAnimationFrame(() => this.handleResize());
    }
    handleResize() {
        const rect = this.container.getBoundingClientRect();

        // Ensure the canvas attribute matches the rendered CSS size exactly
        // floor() prevents blurry sub-pixel rendering
        this.canvas.width = Math.floor(rect.width);
        this.canvas.height = Math.floor(rect.height);

        this.render();
    }
    // --- Coordinate Systems ---
    getMousePos(evt) {
        const rect = this.canvas.getBoundingClientRect();
        return {
            x: (evt.clientX - rect.left) * (this.canvas.width / rect.width),
            y: (evt.clientY - rect.top) * (this.canvas.height / rect.height)
        };
    }
    worldToScreen(wx, wy) {
        const cx = this.canvas.width / 2;
        const cy = this.canvas.height / 2;
        // Uses uniform scale for X and Y to prevent distortion
        return {
            x: cx + (wx - this.view.pan.x) * this.view.scale,
            y: cy - (wy - this.view.pan.y) * this.view.scale
        };
    }
    screenToWorld(sx, sy) {
        const cx = this.canvas.width / 2;
        const cy = this.canvas.height / 2;
        return {
            x: this.view.pan.x + (sx - cx) / this.view.scale,
            y: this.view.pan.y - (sy - cy) / this.view.scale
        };
    }
    // --- Interaction Logic ---
    initEventListeners() {
        const self = this;

        // 1. Canvas Events
        this.canvas.addEventListener('mousedown', (e) => self.handleMouseDown(e));
        window.addEventListener('mousemove', (e) => self.handleMouseMove(e));
        window.addEventListener('mouseup', (e) => self.handleMouseUp(e));
        this.canvas.addEventListener('wheel', (e) => self.handleWheel(e), { passive: false });

        // 2. Input Events (Updated to catch changes immediately)
        const updateFromInputs = () => {
            if (!self.selectedPartId) return;

            const part = self.parts.find(p => p.id === self.selectedPartId);
            if (part) {
                // Parse inputs safely
                const x = parseFloat(self.inputs.x.value);
                const y = parseFloat(self.inputs.y.value);
                const r = parseFloat(self.inputs.r.value);
                const s = parseFloat(self.inputs.s.value);

                // Only update if numbers are valid
                if (!isNaN(x)) part.transform.x = x;
                if (!isNaN(y)) part.transform.y = y;
                if (!isNaN(r)) part.transform.r = r;
                if (!isNaN(s) && s > 0) part.transform.s = s;

                self.render();
            }
        };

        // Attach to 'input' (typing) and 'change' (blur/enter)
        Object.values(this.inputs).forEach(input => {
            if (input) {
                input.addEventListener('input', updateFromInputs);
                input.addEventListener('change', updateFromInputs);
            }
        });
    }
    getInteractionType(ex, ey) {
        if (!this.selectedPartId) return null;

        const part = this.parts.find(p => p.id === this.selectedPartId);
        if (!part) return null;

        const tf = part.transform;
        const rad = (-tf.r * Math.PI) / 180;
        const w = part.bounds.width * tf.s;
        const h = part.bounds.height * tf.s;

        const toScreen = (lx, ly) => {
            const wx = tf.x + (lx * Math.cos(rad) - ly * Math.sin(rad));
            const wy = tf.y + (lx * Math.sin(rad) + ly * Math.cos(rad));
            return this.worldToScreen(wx, wy);
        };

        // Rotation Handle
        const rotHandle = toScreen(0, h / 2 + 0.5);
        if (Math.hypot(ex - rotHandle.x, ey - rotHandle.y) < 15) return 'rotate';

        // Scale Handles
        const corners = [
            { x: -w / 2, y: -h / 2 }, { x: w / 2, y: -h / 2 },
            { x: w / 2, y: h / 2 }, { x: -w / 2, y: h / 2 }
        ];
        for (let c of corners) {
            const s = toScreen(c.x, c.y);
            if (Math.hypot(ex - s.x, ey - s.y) < 15) return 'scale';
        }

        // Drag Body
        const wm = this.screenToWorld(ex, ey);
        const dx = wm.x - tf.x;
        const dy = wm.y - tf.y;
        const localX = dx * Math.cos(-rad) - dy * Math.sin(-rad);
        const localY = dx * Math.sin(-rad) + dy * Math.cos(-rad);

        if (Math.abs(localX) < w / 2 && Math.abs(localY) < h / 2) {
            return 'drag';
        }

        return null;
    }
    handleMouseDown(e) {
        const pos = this.getMousePos(e);
        const ex = pos.x;
        const ey = pos.y;

        const type = this.getInteractionType(ex, ey);

        if (type) {
            const part = this.parts.find(p => p.id === this.selectedPartId);
            this.interaction = {
                mode: type,
                startX: ex, startY: ey,
                startTf: { ...part.transform },
                part: part
            };
        } else {
            // Hit test
            let found = false;
            for (let i = this.parts.length - 1; i >= 0; i--) {
                const p = this.parts[i];
                const tf = p.transform;
                const rad = (-tf.r * Math.PI) / 180;
                const w = p.bounds.width * tf.s;
                const h = p.bounds.height * tf.s;

                const wm = this.screenToWorld(ex, ey);
                const dx = wm.x - tf.x;
                const dy = wm.y - tf.y;
                const localX = dx * Math.cos(-rad) - dy * Math.sin(-rad);
                const localY = dx * Math.sin(-rad) + dy * Math.cos(-rad);

                if (Math.abs(localX) < w / 2 && Math.abs(localY) < h / 2) {
                    this.selectPart(p.id);
                    found = true;
                    this.interaction = {
                        mode: 'drag',
                        startX: ex, startY: ey,
                        startTf: { ...p.transform },
                        part: p
                    };
                    break;
                }
            }

            if (!found) {
                this.selectPart(null);
                this.interaction = {
                    mode: 'pan',
                    startX: ex, startY: ey,
                    startPan: { ...this.view.pan }
                };
            }
        }
    }
    handleMouseMove(e) {
        if (!this.interaction) return;
        if (!this.canvas) return;

        const pos = this.getMousePos(e);
        const ex = pos.x;
        const ey = pos.y;
        const state = this.interaction;

        if (state.mode === 'pan') {
            const dx = (ex - state.startX) / this.view.scale;
            const dy = (ey - state.startY) / this.view.scale;
            this.view.pan.x = state.startPan.x - dx;
            this.view.pan.y = state.startPan.y + dy;
        } else {
            const part = state.part;

            if (state.mode === 'drag') {
                const wmStart = this.screenToWorld(state.startX, state.startY);
                const wmCurr = this.screenToWorld(ex, ey);
                part.transform.x = state.startTf.x + (wmCurr.x - wmStart.x);
                part.transform.y = state.startTf.y + (wmCurr.y - wmStart.y);
            } else if (state.mode === 'rotate') {
                const center = this.worldToScreen(part.transform.x, part.transform.y);
                const angle = -Math.atan2(ey - center.y, ex - center.x);
                let deg = (angle * 180 / Math.PI) + 270;
                part.transform.r = -deg;
            } else if (state.mode === 'scale') {
                const center = this.worldToScreen(part.transform.x, part.transform.y);
                const startDist = Math.hypot(state.startX - center.x, state.startY - center.y);
                const currDist = Math.hypot(ex - center.x, ey - center.y);
                const ratio = currDist / startDist;
                part.transform.s = Math.max(0.1, state.startTf.s * ratio);
            }

            this.updateInputFields();
        }

        this.render();
    }
    handleMouseUp() {
        this.interaction = null;
    }
    handleWheel(e) {
        e.preventDefault();
        const zoomSpeed = 0.001;
        this.view.scale *= (1 - e.deltaY * zoomSpeed);
        this.view.scale = Math.max(0.5, Math.min(this.view.scale, 1000));
        this.render();
    }
    // --- Updates & Rendering ---
    updateInputFields() {
        if (!this.selectedPartId) return;
        const part = this.parts.find(p => p.id === this.selectedPartId);

        // Check if element exists before setting value to avoid errors
        if (this.inputs.x) this.inputs.x.value = part.transform.x.toFixed(3);
        if (this.inputs.y) this.inputs.y.value = part.transform.y.toFixed(3);
        if (this.inputs.r) this.inputs.r.value = Math.round(part.transform.r);
        if (this.inputs.s) this.inputs.s.value = part.transform.s.toFixed(3);
    }
    autoFit() {
        if (this.parts.length === 0) return;

        let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;

        this.parts.forEach(p => {
            const w = p.bounds.width * p.transform.s;
            const h = p.bounds.height * p.transform.s;
            minX = Math.min(minX, p.transform.x - w / 2);
            maxX = Math.max(maxX, p.transform.x + w / 2);
            minY = Math.min(minY, p.transform.y - h / 2);
            maxY = Math.max(maxY, p.transform.y + h / 2);
        });

        const cx = (minX + maxX) / 2;
        const cy = (minY + maxY) / 2;
        const ww = maxX - minX || 10;
        const wh = maxY - minY || 10;

        this.view.pan = { x: cx, y: cy };
        this.view.scale = Math.min(
            (this.canvas.width * 0.8) / ww,
            (this.canvas.height * 0.8) / wh
        );
        this.render();
    }
    drawGrid() {
        const ctx = this.ctx;
        const p0 = this.screenToWorld(0, this.canvas.height);
        const p1 = this.screenToWorld(this.canvas.width, 0);

        ctx.strokeStyle = '#1e2632';
        ctx.lineWidth = 1;
        ctx.beginPath();

        for (let x = Math.floor(p0.x); x <= Math.ceil(p1.x); x += 1) {
            const s = this.worldToScreen(x, 0);
            ctx.moveTo(s.x, 0); ctx.lineTo(s.x, this.canvas.height);
        }
        for (let y = Math.floor(p0.y); y <= Math.ceil(p1.y); y += 1) {
            const s = this.worldToScreen(0, y);
            ctx.moveTo(0, s.y); ctx.lineTo(this.canvas.width, s.y);
        }
        ctx.stroke();

        const org = this.worldToScreen(0, 0);
        ctx.lineWidth = 3;
        ctx.strokeStyle = '#DA3633'; ctx.beginPath(); ctx.moveTo(org.x, org.y); ctx.lineTo(org.x + 20, org.y); ctx.stroke();
        ctx.strokeStyle = '#2EA043'; ctx.beginPath(); ctx.moveTo(org.x, org.y); ctx.lineTo(org.x, org.y - 20); ctx.stroke();
        ctx.lineWidth = 1;

    }
    drawOverallBounds() {
        let globalMaxX = 0;
        let globalMaxY = 0;
        let hasParts = false;

        this.parts.forEach(p => {
            const tf = p.transform;
            const rad = (-tf.r * Math.PI) / 180;
            const cos = Math.cos(rad), sin = Math.sin(rad);

            p.entities.forEach(ent => {
                // Get precise points along the entity edges
                const pts = this.discretizeEntity(ent);

                pts.forEach(pt => {
                    // Transform point to World Space
                    const lx = (pt.x - p.center.x) * tf.s;
                    const ly = (pt.y - p.center.y) * tf.s;
                    
                    const wx = tf.x + (lx * cos - ly * sin);
                    const wy = tf.y + (lx * sin + ly * cos);

                    // Track the furthest positive extent from (0,0)
                    globalMaxX = Math.max(globalMaxX, wx);
                    globalMaxY = Math.max(globalMaxY, wy);
                });
            });

            if (p.entities.length > 0) hasParts = true;
        });

        // Only draw if we have parts and they extend into the positive area
        if (!hasParts || (globalMaxX <= 0 && globalMaxY <= 0)) return;

        const ctx = this.ctx;
        
        // Anchor the start point to World (0,0)
        const sOrigin = this.worldToScreen(0, 0); 
        // Set end point to the calculated Max Extent
        const sMax = this.worldToScreen(globalMaxX, globalMaxY); 

        // Calculate screen dimensions
        // Note: Canvas Y is inverted, so sMax.y is visually "higher" (smaller value)
        const sWidth = sMax.x - sOrigin.x;
        const sHeight = sOrigin.y - sMax.y; 

        ctx.save();
        
        // 1. Draw Dashed Box from (0,0) to Max Extent
        ctx.beginPath();
        ctx.strokeStyle = 'rgba(0, 255, 255, 0.6)';
        ctx.lineWidth = 2;
        ctx.setLineDash([10, 5]);
        ctx.strokeRect(sOrigin.x, sMax.y, sWidth, sHeight);

        // 2. Annotate Dimensions
        const dimText = `${globalMaxX.toFixed(2)} × ${globalMaxY.toFixed(2)}`;

        ctx.font = 'bold 14px sans-serif';
        ctx.textAlign = 'center';
        ctx.fillStyle = '#00ffff';
        ctx.textBaseline = 'bottom';
        
        // Draw text centered above the top edge
        ctx.fillText(dimText, sOrigin.x + sWidth / 2, sMax.y - 8);

        ctx.restore();
    }
    // --- Updates & Rendering ---

    // 1. REPLACEMENT: Main Render Function
    render() {
        const ctx = this.ctx;
        const w = this.canvas.width;
        const h = this.canvas.height;

        ctx.clearRect(0, 0, w, h);
        ctx.fillStyle = '#0a0e14';
        ctx.fillRect(0, 0, w, h);

        if (this.parts.length === 0) {
            ctx.fillStyle = '#30363d';
            ctx.font = '16px sans-serif';
            ctx.textAlign = 'center';
            ctx.fillText("No parts loaded", w / 2, h / 2);
            return;
        }

        // Optimization: Pre-calculate world points for all parts once per frame
        // This prevents O(N^2) heavy transformation calculations inside the draw loops
        this.cacheGlobalPartVertices();

        this.drawOverallBounds();
        this.drawGrid();

        // Draw Parts
        this.parts.forEach(p => {
            this.drawPart(p);
        });
    }

    // 2. NEW: Helper to cache world coordinates for proximity checks
    cacheGlobalPartVertices() {
        this.cachedVertices = [];
        
        this.parts.forEach(p => {
            const tf = p.transform;
            const rad = (-tf.r * Math.PI) / 180;
            const cos = Math.cos(rad);
            const sin = Math.sin(rad);

            p.entities.forEach(ent => {
                const localPts = this.discretizeEntity(ent);
                // Sub-sample points for performance if the resolution is very high
                // For collision detection, we map local points to world space
                localPts.forEach(pt => {
                    const lx = (pt.x - p.center.x) * tf.s;
                    const ly = (pt.y - p.center.y) * tf.s;
                    this.cachedVertices.push({
                        x: tf.x + (lx * cos - ly * sin),
                        y: tf.y + (lx * sin + ly * cos),
                        partId: p.id
                    });
                });
            });
        });
    }

    // 3. REPLACEMENT: Draw Part with collision/bounds highlighting
    drawPart(p) {
        const ctx = this.ctx;
        const tf = p.transform;
        const rad = (-tf.r * Math.PI) / 180;
        const cos = Math.cos(rad);
        const sin = Math.sin(rad);

        // Pre-calculate the World Transform function for this part
        const toWorld = (lx, ly) => {
            const dx = (lx - p.center.x) * tf.s;
            const dy = (ly - p.center.y) * tf.s;
            return {
                x: tf.x + (dx * cos - dy * sin),
                y: tf.y + (dx * sin + dy * cos)
            };
        };

        p.entities.forEach(ent => {
            const pts = this.discretizeEntity(ent);
            if (pts.length < 2) return;

            // To highlight specific sections, we must draw segment by segment
            // instead of one continuous ctx.stroke() path.
            
            // Calculate world positions for the whole entity first
            const worldPts = pts.map(pt => toWorld(pt.x, pt.y));

            for (let i = 0; i < worldPts.length - 1; i++) {
                const wp1 = worldPts[i];
                const wp2 = worldPts[i+1];
                
                // Check errors for this specific segment
                const isError = this.isSegmentInvalid(wp1, wp2, p.id);

                // Convert to Screen Space for drawing
                const sp1 = this.worldToScreen(wp1.x, wp1.y);
                const sp2 = this.worldToScreen(wp2.x, wp2.y);

                ctx.beginPath();
                ctx.moveTo(sp1.x, sp1.y);
                ctx.lineTo(sp2.x, sp2.y);

                if (isError) {
                    ctx.strokeStyle = '#FF3333'; // Bright Red for error
                    ctx.lineWidth = 3;           // Thicker line
                    ctx.shadowBlur = 5;
                    ctx.shadowColor = '#FF0000';
                } else {
                    ctx.strokeStyle = (p.id === this.selectedPartId) ? '#FDB515' : p.color;
                    ctx.lineWidth = (p.id === this.selectedPartId) ? 2 : 1;
                    ctx.shadowBlur = 0;
                }

                ctx.stroke();
                
                // Reset shadow for next iteration
                ctx.shadowBlur = 0; 
            }
        });

        if (p.id === this.selectedPartId) {
            this.drawControls(p);
        }
    }

    // 4. NEW: Logic to check bounds and tool spacing
    isSegmentInvalid(p1, p2, currentPartId) {
        // 1. Check Negative Space (Build Surface is Positive only)
        // If any part of the segment is in negative coordinates
        if (p1.x < 0 || p1.y < 0 || p2.x < 0 || p2.y < 0) {
            return true;
        }

        // 2. Check Proximity (Tool Diameter)
        // We check if the midpoint of this segment is too close to any point of another part
        const midX = (p1.x + p2.x) / 2;
        const midY = (p1.y + p2.y) / 2;
        const safeDist = this.toolDiameter || 1.0; 
        const safeDistSq = safeDist * safeDist;

        // Optimization: Use a classic loop for performance over .find() or .some()
        // We iterate over the cached global vertices
        for (let i = 0; i < this.cachedVertices.length; i++) {
            const other = this.cachedVertices[i];
            
            // Don't check against self
            if (other.partId === currentPartId) continue;

            // Fast Distance Squared check
            const distSq = (midX - other.x) ** 2 + (midY - other.y) ** 2;

            if (distSq < safeDistSq) {
                return true; // Collision or too close
            }
        }

        return false;
    }
    drawControls(part) {
        const ctx = this.ctx;
        const tf = part.transform;
        const rad = (-tf.r * Math.PI) / 180;
        const w = part.bounds.width * tf.s;
        const h = part.bounds.height * tf.s;

        const toScreen = (lx, ly) => {
            const wx = tf.x + (lx * Math.cos(rad) - ly * Math.sin(rad));
            const wy = tf.y + (lx * Math.sin(rad) + ly * Math.cos(rad));
            return this.worldToScreen(wx, wy);
        };

        const corners = [
            { x: -w / 2, y: -h / 2 }, { x: w / 2, y: -h / 2 },
            { x: w / 2, y: h / 2 }, { x: -w / 2, y: h / 2 }
        ];

        // 1. Draw Dashed Bounding Box
        ctx.beginPath();
        ctx.strokeStyle = '#FDB515';
        ctx.setLineDash([5, 5]);
        const c0 = toScreen(corners[0].x, corners[0].y);
        ctx.moveTo(c0.x, c0.y);
        for (let i = 1; i < 4; i++) {
            const c = toScreen(corners[i].x, corners[i].y);
            ctx.lineTo(c.x, c.y);
        }
        ctx.closePath();
        ctx.stroke();
        ctx.setLineDash([]);

        // 2. Draw Corner Handles
        ctx.fillStyle = '#FDB515';
        corners.forEach(c => {
            const s = toScreen(c.x, c.y);
            ctx.fillRect(s.x - 4, s.y - 4, 8, 8);
        });

        // 3. Draw Rotation Handle
        const topMid = toScreen(0, h / 2);
        const rotHandle = toScreen(0, h / 2 + 0.5);
        
        ctx.beginPath();
        ctx.moveTo(topMid.x, topMid.y);
        ctx.lineTo(rotHandle.x, rotHandle.y);
        ctx.stroke();
        
        ctx.beginPath();
        ctx.arc(rotHandle.x, rotHandle.y, 5, 0, Math.PI * 2);
        ctx.fillStyle = '#FDB515';
        ctx.fill();

        // 4. Draw ROTATING Center Crosshair
        const center = toScreen(0, 0); 
        const cSize = 6;
        
        // Calculate screen-space rotation components
        // Note: Canvas Y is inverted relative to World Y, effectively flipping sin component
        const sCos = Math.cos(rad);
        const sSin = Math.sin(rad); 

        ctx.beginPath();
        ctx.strokeStyle = '#FDB515';
        ctx.lineWidth = 2;

        // Local X Axis (Rotated)
        // Mathematically: (x * cos - y * sin), but simplified for pure axis lines
        ctx.moveTo(center.x - cSize * sCos, center.y + cSize * sSin);
        ctx.lineTo(center.x + cSize * sCos, center.y - cSize * sSin);
        
        // Local Y Axis (Rotated)
        // Perpendicular to X
        ctx.moveTo(center.x + cSize * sSin, center.y + cSize * sCos);
        ctx.lineTo(center.x - cSize * sSin, center.y - cSize * sCos);
        
        ctx.stroke();
        ctx.lineWidth = 1; // Reset
    }
        
    discretizeEntity(ent) {
        const pts = [];
        if (ent.type === 'LINE') {
            pts.push(ent.vertices[0], ent.vertices[1]);
        } else if (ent.type === 'LWPOLYLINE' || ent.type === 'POLYLINE') {
            if (ent.vertices) ent.vertices.forEach(v => pts.push(v));
        } else if (ent.type === 'CIRCLE' || ent.type === 'ARC') {
            const steps = 36;
            const start = ent.startAngle || 0;
            const end = ent.endAngle || Math.PI * 2;
            let range = end - start;
            if (range <= 0) range += Math.PI * 2;
            for (let i = 0; i <= steps; i++) {
                const theta = start + (range * i / steps);
                pts.push({
                    x: ent.center.x + Math.cos(theta) * ent.radius,
                    y: ent.center.y + Math.sin(theta) * ent.radius
                });
            }
        } else if (ent.type === 'SPLINE') {
            const degree = ent.degreeOfSplineCurve || 3;
            const knots = ent.knotValues;
            const cp = ent.controlPoints;

            if (knots && cp && cp.length >= degree + 1) {
                // Determine range of t (domain)
                // Standard domain is [knots[degree], knots[knots.length - 1 - degree]]
                const tStart = knots[degree];
                const tEnd = knots[knots.length - 1 - degree];
                
                // Resolution: Increase steps for smoother curves
                const steps = 40; 
                
                for (let i = 0; i <= steps; i++) {
                    const t = tStart + (tEnd - tStart) * (i / steps);
                    // Avoid floating point overshoot on the last point
                    const safeT = Math.min(t, tEnd - 1e-9); 
                    const pt = this.interpolateBSpline(i === steps ? tEnd : safeT, degree, knots, cp);
                    pts.push(pt);
                }
            } else if (cp) {
                // Fallback: just draw control points if invalid spline data
                cp.forEach(p => pts.push(p));
            }
        }
        return pts;
    }
    interpolateBSpline(t, degree, knots, cp) {
            // 1. Find knot span index 'k' such that knots[k] <= t < knots[k+1]
            let k = -1;
            for (let i = degree; i < knots.length - 1 - degree; i++) {
                if (t >= knots[i] && t <= knots[i + 1]) {
                    k = i;
                    // If we hit the exact end knot, allow it (for closed curves)
                    if (t < knots[i+1]) break; 
                }
            }
            // Safety fallback if t is out of bounds
            if (k === -1) k = knots.length - degree - 2;
    
            // 2. De Boor's Algorithm
            // Copy initial control points for this span
            const d = [];
            for (let j = 0; j <= degree; j++) {
                d[j] = { x: cp[k - degree + j].x, y: cp[k - degree + j].y };
            }
    
            // Iteratively interpolate
            for (let r = 1; r <= degree; r++) {
                for (let j = degree; j >= r; j--) {
                    const knotIndex = k + j - degree;
                    const denom = knots[knotIndex + degree + 1 - r] - knots[knotIndex];
                    
                    // If denominator is 0, alpha is 0
                    const alpha = denom === 0 ? 0 : (t - knots[knotIndex]) / denom;
    
                    d[j].x = (1 - alpha) * d[j - 1].x + alpha * d[j].x;
                    d[j].y = (1 - alpha) * d[j - 1].y + alpha * d[j].y;
                }
            }
    
            return d[degree]; // The final interpolated point
        }
    stitchPartsTogether() {
        // 1. Helper for safe DXF formatting
        const lines = [];
        const add = (code, value) => {
            lines.push(code);
            lines.push(value);
        };

        // Helper to format floats to avoid scientific notation
        const fmt = (num) => parseFloat(num).toFixed(6);

        // 2. Transformation Logic (Matches your Render logic exactly)
        const transform = (x, y, tf) => {
            const rad = (-tf.r * Math.PI) / 180; // Match render rotation direction
            const cos = Math.cos(rad);
            const sin = Math.sin(rad);

            // Scale relative to part center
            const dx = (x - tf.partCenter.x) * tf.s;
            const dy = (y - tf.partCenter.y) * tf.s;

            // Rotate and Translate
            return {
                x: tf.x + (dx * cos - dy * sin),
                y: tf.y + (dx * sin + dy * cos)
            };
        };

        // 3. Header Section
        add(0, "SECTION");
        add(2, "HEADER");
        add(9, "$ACADVER");
        add(1, "AC1015"); // AutoCAD 2000
        add(0, "ENDSEC");

        // 4. Entities Section
        add(0, "SECTION");
        add(2, "ENTITIES");

        this.parts.forEach(p => {
            // Capture transform state including the original center for pivot calculations
            const tf = { 
                ...p.transform, 
                partCenter: p.center 
            };
            
            // Sanitize layer name
            const layer = (p.name || "0").replace(/[^a-zA-Z0-9_ -]/g, "");

            p.entities.forEach(ent => {
                // Common Entity Start
                add(0, ent.type);
                add(100, "AcDbEntity");
                add(8, layer); 
                // Color (62) can be added here if needed, but usually handled by layer

                if (ent.type === 'LINE') {
                    add(100, "AcDbLine");
                    const start = transform(ent.vertices[0].x, ent.vertices[0].y, tf);
                    const end = transform(ent.vertices[1].x, ent.vertices[1].y, tf);

                    add(10, fmt(start.x)); add(20, fmt(start.y)); add(30, 0.0);
                    add(11, fmt(end.x));   add(21, fmt(end.y));   add(31, 0.0);

                } else if (ent.type === 'LWPOLYLINE') {
                    add(100, "AcDbPolyline");
                    add(90, ent.vertices.length);
                    // Group 70: 1 = Closed, 0 = Open
                    add(70, ent.closed ? 1 : 0);
                    add(43, 0.0); // Constant width

                    ent.vertices.forEach(v => {
                        const pt = transform(v.x, v.y, tf);
                        add(10, fmt(pt.x));
                        add(20, fmt(pt.y));
                        if (v.bulge) add(42, fmt(v.bulge));
                    });

                } else if (ent.type === 'CIRCLE') {
                    add(100, "AcDbCircle");
                    const c = transform(ent.center.x, ent.center.y, tf);
                    add(10, fmt(c.x)); add(20, fmt(c.y)); add(30, 0.0);
                    add(40, fmt(ent.radius * tf.s));

                } else if (ent.type === 'ARC') {
                    add(100, "AcDbCircle");
                    const c = transform(ent.center.x, ent.center.y, tf);
                    add(10, fmt(c.x)); add(20, fmt(c.y)); add(30, 0.0);
                    add(40, fmt(ent.radius * tf.s));

                    add(100, "AcDbArc");
                    
                    // Rotate angles. Note: DXF is Counter-Clockwise (CCW).
                    // If part rotation 'r' is in degrees:
                    // We subtract 'r' because in your system positive 'r' seems to be visual CW?
                    // Adjust this sign if your arcs rotate the wrong way.
                    let start = (ent.startAngle * 180 / Math.PI) - tf.r;
                    let end = (ent.endAngle * 180 / Math.PI) - tf.r;

                    // Normalize to 0-360
                    start = ((start % 360) + 360) % 360;
                    end = ((end % 360) + 360) % 360;

                    add(50, fmt(start));
                    add(51, fmt(end));

                } else if (ent.type === 'SPLINE') {
                    add(100, "AcDbSpline");
                    
                    const degree = ent.degreeOfSplineCurve || 3;
                    const knots = ent.knotValues || [];
                    const cps = ent.controlPoints || [];

                    add(210, 0.0); add(220, 0.0); add(230, 1.0); // Normal vector

                    // Flags: 8 = Planar, 1 = Closed (Bitmask)
                    let flags = 8;
                    if (ent.closed) flags += 1;
                    add(70, flags); 

                    add(71, degree);
                    add(72, knots.length);
                    add(73, cps.length);
                    add(74, 0); // No fit points

                    // Knots
                    knots.forEach(k => add(40, fmt(k)));

                    // Control Points
                    cps.forEach(cp => {
                        const pt = transform(cp.x, cp.y, tf);
                        add(10, fmt(pt.x));
                        add(20, fmt(pt.y));
                        add(30, 0.0);
                    });
                }
            });
        });

        // 5. Footer
        add(0, "ENDSEC");
        add(0, "EOF");

        // 6. Join and Download
        const result = lines.join('\n');
        return result;
    }
}

// @ts-check

/**
 * DXFSetupPreview
 * Handles the visualization, layout, and manipulation of DXF parts on a canvas.
 */
function DXFSetupPreview(config) {
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

DXFSetupPreview.prototype.addPart = function(id, name, entities) {
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

    // Stack to right of last part
    let startX = 0;
    if (this.parts.length > 0) {
        const last = this.parts[this.parts.length - 1];
        startX = last.transform.x + (last.bounds.width * last.transform.s / 2) + (width / 2) + 1.0;
    }

    const part = {
        id: id,
        name: name,
        entities: entities,
        bounds: { width, height, minX, maxX, minY, maxY },
        center: { x: (minX + maxX) / 2, y: (minY + maxY) / 2 },
        transform: { x: startX, y: 0, r: 0, s: 1 }, 
        color: this.colors[this.parts.length % this.colors.length],
        isColliding: false
    };

    this.parts.push(part);
    this.autoFit();
    this.selectPart(id);
};

DXFSetupPreview.prototype.removePart = function(id) {
    this.parts = this.parts.filter(p => p.id !== id);
    if (this.selectedPartId === id) this.selectPart(null);
    this.render();
};

DXFSetupPreview.prototype.selectPart = function(id) {
    this.selectedPartId = id;
    this.updateInputFields();
    this.render();
};

DXFSetupPreview.prototype.clear = function() {
    this.parts = [];
    this.selectedPartId = null;
    this.updateInputFields();
    this.render();
};

DXFSetupPreview.prototype.show = function() {
    this.canvas.style.display = 'block';
    this.handleResize(); 
};

DXFSetupPreview.prototype.hide = function() {
    this.canvas.style.display = 'none';
};

DXFSetupPreview.prototype.setCollisions = function(collisionIds) {
    this.parts.forEach(p => {
        p.isColliding = collisionIds.includes(p.id);
    });
    this.render();
};

// --- Resize Logic (Fixes Squishing) ---

DXFSetupPreview.prototype.initResizeHandler = function() {
    const observer = new ResizeObserver(() => {
        this.handleResize();
    });
    observer.observe(this.container);
    // Initial call
    requestAnimationFrame(() => this.handleResize());
};

DXFSetupPreview.prototype.handleResize = function() {
    const rect = this.container.getBoundingClientRect();
    
    // Ensure the canvas attribute matches the rendered CSS size exactly
    // floor() prevents blurry sub-pixel rendering
    this.canvas.width = Math.floor(rect.width);
    this.canvas.height = Math.floor(rect.height);
    
    this.render();
};

// --- Coordinate Systems ---

DXFSetupPreview.prototype.getMousePos = function(evt) {
    const rect = this.canvas.getBoundingClientRect();
    return {
        x: (evt.clientX - rect.left) * (this.canvas.width / rect.width),
        y: (evt.clientY - rect.top) * (this.canvas.height / rect.height)
    };
};

DXFSetupPreview.prototype.worldToScreen = function(wx, wy) {
    const cx = this.canvas.width / 2;
    const cy = this.canvas.height / 2;
    // Uses uniform scale for X and Y to prevent distortion
    return {
        x: cx + (wx - this.view.pan.x) * this.view.scale,
        y: cy - (wy - this.view.pan.y) * this.view.scale 
    };
};

DXFSetupPreview.prototype.screenToWorld = function(sx, sy) {
    const cx = this.canvas.width / 2;
    const cy = this.canvas.height / 2;
    return {
        x: this.view.pan.x + (sx - cx) / this.view.scale,
        y: this.view.pan.y - (sy - cy) / this.view.scale
    };
};

// --- Interaction Logic ---

DXFSetupPreview.prototype.initEventListeners = function() {
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
};

DXFSetupPreview.prototype.getInteractionType = function(ex, ey) {
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
        {x: -w/2, y: -h/2}, {x: w/2, y: -h/2},
        {x: w/2, y: h/2},   {x: -w/2, y: h/2}
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

    if (Math.abs(localX) < w/2 && Math.abs(localY) < h/2) {
        return 'drag';
    }

    return null;
};

DXFSetupPreview.prototype.handleMouseDown = function(e) {
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

            if (Math.abs(localX) < w/2 && Math.abs(localY) < h/2) {
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
};

DXFSetupPreview.prototype.handleMouseMove = function(e) {
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
};

DXFSetupPreview.prototype.handleMouseUp = function() {
    this.interaction = null;
};

DXFSetupPreview.prototype.handleWheel = function(e) {
    e.preventDefault();
    const zoomSpeed = 0.001;
    this.view.scale *= (1 - e.deltaY * zoomSpeed);
    this.view.scale = Math.max(0.5, Math.min(this.view.scale, 1000));
    this.render();
};

// --- Updates & Rendering ---

DXFSetupPreview.prototype.updateInputFields = function() {
    if (!this.selectedPartId) return;
    const part = this.parts.find(p => p.id === this.selectedPartId);
    
    // Check if element exists before setting value to avoid errors
    if(this.inputs.x) this.inputs.x.value = part.transform.x.toFixed(3);
    if(this.inputs.y) this.inputs.y.value = part.transform.y.toFixed(3);
    if(this.inputs.r) this.inputs.r.value = Math.round(part.transform.r);
    if(this.inputs.s) this.inputs.s.value = part.transform.s.toFixed(3);
};

DXFSetupPreview.prototype.autoFit = function() {
    if (this.parts.length === 0) return;
    
    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
    
    this.parts.forEach(p => {
        const w = p.bounds.width * p.transform.s;
        const h = p.bounds.height * p.transform.s;
        minX = Math.min(minX, p.transform.x - w/2);
        maxX = Math.max(maxX, p.transform.x + w/2);
        minY = Math.min(minY, p.transform.y - h/2);
        maxY = Math.max(maxY, p.transform.y + h/2);
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
};

DXFSetupPreview.prototype.render = function() {
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

    this.drawGrid();

    // Draw Parts
    this.parts.forEach(p => {
        this.drawPart(p);
    });
};

DXFSetupPreview.prototype.drawGrid = function() {
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

    const org = this.worldToScreen(0,0);
    ctx.strokeStyle = '#DA3633'; ctx.beginPath(); ctx.moveTo(org.x, org.y); ctx.lineTo(org.x+20, org.y); ctx.stroke();
    ctx.strokeStyle = '#2EA043'; ctx.beginPath(); ctx.moveTo(org.x, org.y); ctx.lineTo(org.x, org.y-20); ctx.stroke();
};

DXFSetupPreview.prototype.drawPart = function(p) {
    const ctx = this.ctx;
    const tf = p.transform;
    const rad = (-tf.r * Math.PI) / 180;
    const cos = Math.cos(rad);
    const sin = Math.sin(rad);

    ctx.beginPath();
    
    p.entities.forEach(ent => {
        const pts = this.discretizeEntity(ent);
        if (pts.length < 2) return;

        const tp = (pt) => {
            const lx = (pt.x - p.center.x) * tf.s;
            const ly = (pt.y - p.center.y) * tf.s;
            const wx = tf.x + (lx * cos - ly * sin);
            const wy = tf.y + (lx * sin + ly * cos);
            return this.worldToScreen(wx, wy);
        };

        const start = tp(pts[0]);
        ctx.moveTo(start.x, start.y);
        for(let i=1; i<pts.length; i++) {
            const next = tp(pts[i]);
            ctx.lineTo(next.x, next.y);
        }
    });

    if (p.isColliding) {
        ctx.fillStyle = 'rgba(218, 54, 51, 0.3)';
        ctx.fill();
        ctx.strokeStyle = '#DA3633';
        ctx.lineWidth = 2;
    } else {
        ctx.strokeStyle = (p.id === this.selectedPartId) ? '#FDB515' : p.color;
        ctx.lineWidth = (p.id === this.selectedPartId) ? 2 : 1;
    }
    ctx.stroke();

    if (p.id === this.selectedPartId) {
        this.drawControls(p);
    }
};

DXFSetupPreview.prototype.drawControls = function(part) {
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
        {x: -w/2, y: -h/2}, {x: w/2, y: -h/2},
        {x: w/2, y: h/2},   {x: -w/2, y: h/2}
    ];

    ctx.beginPath();
    ctx.strokeStyle = '#FDB515';
    ctx.setLineDash([5, 5]);
    const c0 = toScreen(corners[0].x, corners[0].y);
    ctx.moveTo(c0.x, c0.y);
    for(let i=1; i<4; i++) {
        const c = toScreen(corners[i].x, corners[i].y);
        ctx.lineTo(c.x, c.y);
    }
    ctx.closePath();
    ctx.stroke();
    ctx.setLineDash([]);

    ctx.fillStyle = '#FDB515';
    corners.forEach(c => {
        const s = toScreen(c.x, c.y);
        ctx.fillRect(s.x-4, s.y-4, 8, 8);
    });

    const topMid = toScreen(0, h/2);
    const rotHandle = toScreen(0, h/2 + 0.5);
    ctx.beginPath();
    ctx.moveTo(topMid.x, topMid.y);
    ctx.lineTo(rotHandle.x, rotHandle.y);
    ctx.stroke();
    ctx.beginPath();
    ctx.arc(rotHandle.x, rotHandle.y, 5, 0, Math.PI*2);
    ctx.fillStyle = '#2188ff';
    ctx.fill();
};

DXFSetupPreview.prototype.discretizeEntity = function(ent) {
    const pts = [];
    if (ent.type === 'LINE') {
        pts.push(ent.vertices[0], ent.vertices[1]);
    } else if (ent.type === 'LWPOLYLINE' || ent.type === 'POLYLINE') {
        if(ent.vertices) ent.vertices.forEach(v => pts.push(v));
    } else if (ent.type === 'CIRCLE' || ent.type === 'ARC') {
        const steps = 36;
        const start = ent.startAngle || 0;
        const end = ent.endAngle || Math.PI * 2;
        let range = end - start;
        if (range <= 0) range += Math.PI * 2;
        for(let i=0; i<=steps; i++) {
            const theta = start + (range * i / steps);
            pts.push({
                x: ent.center.x + Math.cos(theta) * ent.radius,
                y: ent.center.y + Math.sin(theta) * ent.radius
            });
        }
    }
    return pts;
};

DXFSetupPreview.prototype.stitchPartsTogether = function() {
    const parts = this.parts;
    function transformPoint(x, y, t) {
        const rad = (t.r * Math.PI) / 180; // Convert degrees to radians
        const cos = Math.cos(rad);
        const sin = Math.sin(rad);

        // 1. Scale
        const sx = x * t.s;
        const sy = y * t.s;

        // 2. Rotate & 3. Translate
        return {
            x: (sx * cos - sy * sin) + t.x,
            y: (sx * sin + sy * cos) + t.y
        };
    }
    function transformAngle(angleDegrees, rotationDegrees) {
        let newAngle = angleDegrees + rotationDegrees;
        return newAngle % 360;
    }
    let dxfString = "";

    // --- DXF HEADER ---
    // Minimal header to satisfy CAD programs
    dxfString += "0\nSECTION\n2\nHEADER\n0\nENDSEC\n";
    dxfString += "0\nSECTION\n2\nENTITIES\n";

    parts.forEach(part => {
        if (!part.entities) return;

        part.entities.forEach(entity => {
            const t = part.transform;

            // Common Group Codes (Entity Type & Layer)
            // We use the part name as the layer name for organization
            dxfString += `0\n${entity.type}\n`; 
            dxfString += `8\n${part.name || '0'}\n`; 

            switch (entity.type) {
                case 'LINE':
                    // Transform Start
                    const start = transformPoint(entity.vertices[0].x, entity.vertices[0].y, t);
                    // Transform End
                    const end = transformPoint(entity.vertices[1].x, entity.vertices[1].y, t);

                    dxfString += `10\n${start.x.toFixed(4)}\n20\n${start.y.toFixed(4)}\n`;
                    dxfString += `11\n${end.x.toFixed(4)}\n21\n${end.y.toFixed(4)}\n`;
                    break;

                case 'LWPOLYLINE':
                    // Polyline Header
                    dxfString += `66\n1\n`; // Entities follow (if needed, usually 0 for LW)
                    dxfString += `90\n${entity.vertices.length}\n`; // Number of vertices
                    dxfString += `70\n${entity.closed ? 1 : 0}\n`; // Closed flag

                    // Transform and write each vertex
                    entity.vertices.forEach(v => {
                        const pt = transformPoint(v.x, v.y, t);
                        dxfString += `10\n${pt.x.toFixed(4)}\n`;
                        dxfString += `20\n${pt.y.toFixed(4)}\n`;
                        
                        // Handle bulge (curved polyline segments) if present
                        // Note: Bulge scaling is invariant, but mirroring would flip sign. 
                        // Assuming uniform scale > 0 here.
                        if (v.bulge) {
                            dxfString += `42\n${v.bulge}\n`;
                        }
                    });
                    break;

                case 'CIRCLE':
                    const cCenter = transformPoint(entity.center.x, entity.center.y, t);
                    const cRadius = entity.radius * t.s;

                    dxfString += `10\n${cCenter.x.toFixed(4)}\n20\n${cCenter.y.toFixed(4)}\n`;
                    dxfString += `40\n${cRadius.toFixed(4)}\n`;
                    break;

                case 'ARC':
                    const aCenter = transformPoint(entity.center.x, entity.center.y, t);
                    const aRadius = entity.radius * t.s;
                    const startAngle = transformAngle(entity.startAngle, t.r);
                    const endAngle = transformAngle(entity.endAngle, t.r);

                    dxfString += `10\n${aCenter.x.toFixed(4)}\n20\n${aCenter.y.toFixed(4)}\n`;
                    dxfString += `40\n${aRadius.toFixed(4)}\n`;
                    dxfString += `50\n${startAngle.toFixed(4)}\n`;
                    dxfString += `51\n${endAngle.toFixed(4)}\n`;
                    break;
            }
        });
    });

    // --- DXF FOOTER ---
    dxfString += "0\nENDSEC\n0\nEOF\n";

    return dxfString;
}

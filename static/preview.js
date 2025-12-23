// @ts-check

/** @type {import("three")} */
const THREE = /** @type {any} */ (window).THREE;

/**
 * GCodePreview
 * Handles the 3D visualization, parsing, and playback of G-code toolpaths.
 */
function GCodePreview(config) {
    // 1. Setup DOM & Context
    this.container = typeof config.container === 'string' ? document.querySelector(config.container) : config.container;
    this.canvas = typeof config.canvas === 'string' ? document.querySelector(config.canvas) : config.canvas;
    
    // 2. Bind UI Elements
    this.ui = {
        scrubber: document.querySelector(config.ui.scrubber),
        scrubberLabel: document.querySelector(config.ui.scrubberLabel),
        scrubberOp: document.querySelector(config.ui.scrubberOp),
        playBtn: document.querySelector(config.ui.playBtn),
        restartBtn: document.querySelector(config.ui.restartBtn),
        speedSelect: document.querySelector(config.ui.speedSelect),
        playIcon: document.querySelector(config.ui.playIcon),
        pauseIcon: document.querySelector(config.ui.pauseIcon),
        resetViewBtn: document.querySelector(config.ui.resetViewBtn)
    };

    // 3. Bind Material Inputs
    this.inputs = {
        material: document.querySelector(config.inputs.material),
        thickness: document.querySelector(config.inputs.thickness),
        tubeHeight: document.querySelector(config.inputs.tubeHeight)
    };

    // 4. Three.js State
    this.scene = null;
    this.camera = null;
    this.renderer = null;
    this.refs = {
        tool: null,
        stock: null,
        completedLine: null,
        upcomingLine: null,
        grid: null
    };

    // 5. App State
    this.moves = [];
    this.bounds = { minX: 0, maxX: 0, minY: 0, maxY: 0, minZ: 0, maxZ: 0 };
    this.playback = {
        isPlaying: false,
        interval: null,
        speed: 40
    };
    this.cameraState = {
        optimalPos: null,
        optimalLookAt: null
    };

    this.initThree();
    this.initEventListeners();
    this.initResizeHandler();
    this.animate();
}

// --- Initialization ---

GCodePreview.prototype.initThree = function() {
    // Scene
    this.scene = new THREE.Scene();
    this.scene.background = new THREE.Color(0x0A0E14);

    // Camera
    const aspect = this.container.clientWidth / this.container.clientHeight;
    this.camera = new THREE.PerspectiveCamera(45, aspect, 0.1, 1000);
    this.camera.position.set(10, 10, 10);
    this.camera.lookAt(0, 0, 0);

    // Renderer
    this.renderer = new THREE.WebGLRenderer({ canvas: this.canvas, antialias: true });
    this.renderer.setSize(this.container.clientWidth, this.container.clientHeight);
    this.renderer.setPixelRatio(window.devicePixelRatio);

    // Lights
    const ambientLight = new THREE.AmbientLight(0xffffff, 0.6);
    this.scene.add(ambientLight);

    const dirLight = new THREE.DirectionalLight(0xffffff, 0.8);
    dirLight.position.set(5, 10, 7.5);
    this.scene.add(dirLight);
};

GCodePreview.prototype.initEventListeners = function() {
    const self = this;

    // Scrubber
    if (this.ui.scrubber) {
        this.ui.scrubber.oninput = (e) => {
            const idx = parseInt(e.target.value);
            self.updateToolpathDisplay(idx);
        };
    }

    // Playback Controls
    if (this.ui.playBtn) this.ui.playBtn.addEventListener('click', () => self.togglePlayback());
    if (this.ui.restartBtn) this.ui.restartBtn.addEventListener('click', () => self.restartPlayback());
    if (this.ui.speedSelect) {
        this.ui.speedSelect.addEventListener('change', (e) => {
            self.playback.speed = parseInt(e.target.value);
            if (self.playback.isPlaying) {
                self.stopPlayback();
                self.startPlayback();
            }
        });
    }

    // Camera Controls
    this.initMouseControls();
    if (this.ui.resetViewBtn) {
        this.ui.resetViewBtn.addEventListener('click', () => self.resetView());
    }
};

GCodePreview.prototype.initResizeHandler = function() {
    const observer = new ResizeObserver(() => this.handleResize());
    observer.observe(this.container);
};

// --- Core Logic ---

GCodePreview.prototype.load = function(gcode) {
    this.parseGcode(gcode);
    
    if (this.moves.length === 0) return;

    this.buildScene();
    
    // Reset Scrubber
    if (this.ui.scrubber) {
        this.ui.scrubber.max = this.moves.length - 1;
        this.ui.scrubber.value = 0;
    }

    this.updateToolpathDisplay(0);
};

GCodePreview.prototype.parseGcode = function(gcode) {
    const lines = gcode.split('\n');
    this.moves = [];
    
    let cur = { x: 0, y: 0, z: 0 };
    let b = { minX: Infinity, maxX: -Infinity, minY: Infinity, maxY: -Infinity, minZ: Infinity, maxZ: -Infinity };

    const updateBounds = (x, y, z) => {
        b.minX = Math.min(b.minX, x); b.maxX = Math.max(b.maxX, x);
        b.minY = Math.min(b.minY, y); b.maxY = Math.max(b.maxY, y);
        b.minZ = Math.min(b.minZ, z); b.maxZ = Math.max(b.maxZ, z);
    };

    for (const line of lines) {
        const trimmed = line.trim();
        if (trimmed.startsWith('(') || trimmed.startsWith(';') || !trimmed) continue;
        
        const gMatch = trimmed.match(/^(G[0-3])/);
        if (!gMatch) continue;

        const moveType = gMatch[1];
        const xMatch = trimmed.match(/X([-\d.]+)/);
        const yMatch = trimmed.match(/Y([-\d.]+)/);
        const zMatch = trimmed.match(/Z([-\d.]+)/);

        const newPos = {
            x: xMatch ? parseFloat(xMatch[1]) : cur.x,
            y: yMatch ? parseFloat(yMatch[1]) : cur.y,
            z: zMatch ? parseFloat(zMatch[1]) : cur.z
        };

        // Handle Arcs (G2/G3)
        if (moveType === 'G2' || moveType === 'G3') {
            const iMatch = trimmed.match(/I([-\d.]+)/);
            const jMatch = trimmed.match(/J([-\d.]+)/);

            if (iMatch && jMatch) {
                const arcI = parseFloat(iMatch[1]);
                const arcJ = parseFloat(jMatch[1]);
                const centerX = cur.x + arcI;
                const centerY = cur.y + arcJ;
                
                const startAngle = Math.atan2(cur.y - centerY, cur.x - centerX);
                const endAngle = Math.atan2(newPos.y - centerY, newPos.x - centerX);
                const radius = Math.sqrt(arcI * arcI + arcJ * arcJ);
                
                let sweep = endAngle - startAngle;
                const isCW = moveType === 'G2';

                if (isCW) {
                    if (sweep > 0) sweep -= 2 * Math.PI;
                    if (Math.abs(sweep) < 0.001) sweep = -2 * Math.PI;
                } else {
                    if (sweep < 0) sweep += 2 * Math.PI;
                    if (Math.abs(sweep) < 0.001) sweep = 2 * Math.PI;
                }

                if (!isNaN(radius) && radius > 0) {
                    const startZ = cur.z;
                    const numSegs = Math.max(8, Math.ceil(Math.abs(sweep) * radius * 10));
                    const zStep = (newPos.z - startZ) / numSegs;

                    for (let i = 0; i < numSegs; i++) {
                        const t = (i + 1) / numSegs;
                        const angle = startAngle + sweep * t;
                        const arcX = centerX + radius * Math.cos(angle);
                        const arcY = centerY + radius * Math.sin(angle);
                        const arcZ = startZ + zStep * (i + 1);

                        this.moves.push({
                            type: moveType,
                            from: { ...cur },
                            to: { x: arcX, y: arcY, z: arcZ },
                            line: trimmed
                        });
                        
                        cur = { x: arcX, y: arcY, z: arcZ };
                        updateBounds(cur.x, cur.y, cur.z);
                    }
                    continue; // Skip linear add
                }
            }
        }

        // Linear Moves
        if (newPos.x !== cur.x || newPos.y !== cur.y || newPos.z !== cur.z) {
            this.moves.push({
                type: moveType,
                from: { ...cur },
                to: { ...newPos },
                line: trimmed
            });
            cur = { ...newPos };
            updateBounds(cur.x, cur.y, cur.z);
        }
    }
    
    this.bounds = b;
};

GCodePreview.prototype.buildScene = function() {
    // Clear old dynamic children
    const toRemove = [];
    this.scene.traverse(child => {
        if (child.isMesh || child.isLine || child.isGridHelper || child.isAxesHelper) {
            toRemove.push(child);
        }
    });
    toRemove.forEach(c => this.scene.remove(c));
    this.refs = { tool: null, stock: null, completedLine: null, upcomingLine: null };

    const b = this.bounds;
    const maxDim = Math.max(b.maxX, b.maxY, b.maxZ);
    
    // Grid & Axes
    const gridSize = Math.max(b.maxX * 1.3, b.maxY * 1.3, 15);
    const gridHelper = new THREE.GridHelper(gridSize, Math.ceil(gridSize), 0x30363D, 0x1E2632);
    gridHelper.position.set(gridSize/3, 0, -gridSize/3);
    this.scene.add(gridHelper);

    const axesHelper = new THREE.AxesHelper(Math.max(maxDim, 5) * 1.2);
    this.scene.add(axesHelper);

    // Stock Material
    const matType = this.inputs.material ? this.inputs.material.value : 'generic';
    const matThick = this.inputs.thickness ? parseFloat(this.inputs.thickness.value) : 1;
    const isTube = (matType === 'aluminum_tube');
    const stockH = isTube && this.inputs.tubeHeight ? parseFloat(this.inputs.tubeHeight.value) : matThick;

    const stockW = b.maxX - b.minX;
    const stockD = b.maxY - b.minY;

    // Boundaries Outline
    const outlines = new THREE.Group();
    const outlineGeo = new THREE.BufferGeometry().setFromPoints([
        new THREE.Vector3(b.minX, matThick, -b.minY),
        new THREE.Vector3(b.maxX, matThick, -b.minY),
        new THREE.Vector3(b.maxX, matThick, -b.maxY),
        new THREE.Vector3(b.minX, matThick, -b.maxY),
        new THREE.Vector3(b.minX, matThick, -b.minY)
    ]);
    outlines.add(new THREE.Line(outlineGeo, new THREE.LineBasicMaterial({ color: 0x8B949E, opacity: 0.5, transparent: true })));
    this.scene.add(outlines);

    // Solid Stock Body
    const stockGeo = new THREE.BoxGeometry(stockW, stockH, stockD);
    const stockMat = new THREE.MeshStandardMaterial({
        color: 0xE8F0FF, transparent: true, opacity: 0.15,
        metalness: 0.3, roughness: 0.7, side: THREE.DoubleSide, depthWrite: false
    });
    const stockMesh = new THREE.Mesh(stockGeo, stockMat);
    stockMesh.position.set((b.minX + b.maxX)/2, stockH/2, -(b.minY + b.maxY)/2);
    stockMesh.renderOrder = -1;
    this.scene.add(stockMesh);

    // Tool
    const toolLen = Math.max(b.maxZ * 1.5, 1.0);
    const toolGeo = new THREE.CylinderGeometry(0.08, 0.08, toolLen, 16); // radius ~2mm
    const toolMat = new THREE.MeshStandardMaterial({ color: 0xC0C0C0, metalness: 0.8, roughness: 0.2 });
    this.refs.tool = new THREE.Mesh(toolGeo, toolMat);
    this.refs.tool.userData.len = toolLen;
    this.scene.add(this.refs.tool);

    // Camera Positioning
    const viewDist = maxDim * 2;
    this.cameraState.optimalPos = new THREE.Vector3(viewDist * 0.7, viewDist * 0.7, viewDist * 0.7);
    this.cameraState.optimalLookAt = new THREE.Vector3(b.maxX/3, b.maxZ/3, -b.maxY/3);
    this.resetView();
};

GCodePreview.prototype.updateToolpathDisplay = function(moveIndex) {
    if (this.moves.length === 0) return;

    // UI Updates
    if (this.ui.scrubberLabel) this.ui.scrubberLabel.textContent = `Move ${moveIndex + 1} of ${this.moves.length}`;
    if (this.ui.scrubberOp) {
        const move = this.moves[moveIndex];
        const type = move.type === 'G0' ? 'Rapid' : 'Cut';
        this.ui.scrubberOp.textContent = `${type}: ${move.line.substring(0, 40)}`;
    }

    // Tool Position
    if (this.refs.tool) {
        const pos = this.moves[moveIndex].to;
        const len = this.refs.tool.userData.len;
        this.refs.tool.position.set(pos.x, pos.z + len/2, -pos.y);
    }

    // Lines
    if (this.refs.completedLine) this.scene.remove(this.refs.completedLine);
    if (this.refs.upcomingLine) this.scene.remove(this.refs.upcomingLine);

    // 1. Upcoming (Gold)
    if (moveIndex < this.moves.length - 1) {
        const pts = [];
        // Add current point
        pts.push(new THREE.Vector3(this.moves[moveIndex].from.x, this.moves[moveIndex].from.z, -this.moves[moveIndex].from.y));
        for (let i = moveIndex; i < this.moves.length; i++) {
            pts.push(new THREE.Vector3(this.moves[i].to.x, this.moves[i].to.z, -this.moves[i].to.y));
        }
        const geo = new THREE.BufferGeometry().setFromPoints(pts);
        this.refs.upcomingLine = new THREE.Line(geo, new THREE.LineBasicMaterial({ 
            color: 0xFDB515, linewidth: 3, opacity: 0.8, transparent: true 
        }));
        this.scene.add(this.refs.upcomingLine);
    }

    // 2. Completed (Green)
    if (moveIndex > 0) {
        const pts = [];
        pts.push(new THREE.Vector3(this.moves[0].from.x, this.moves[0].from.z, -this.moves[0].from.y));
        for (let i = 0; i <= moveIndex; i++) {
            pts.push(new THREE.Vector3(this.moves[i].to.x, this.moves[i].to.z, -this.moves[i].to.y));
        }
        const geo = new THREE.BufferGeometry().setFromPoints(pts);
        this.refs.completedLine = new THREE.Line(geo, new THREE.LineBasicMaterial({ color: 0x2EA043, linewidth: 3 }));
        this.scene.add(this.refs.completedLine);
    }
};

// --- Playback Logic ---

GCodePreview.prototype.togglePlayback = function() {
    if (this.playback.isPlaying) this.stopPlayback();
    else this.startPlayback();
};

GCodePreview.prototype.startPlayback = function() {
    if (!this.ui.scrubber) return;
    
    this.playback.isPlaying = true;
    if (this.ui.playBtn) this.ui.playBtn.classList.add('playing');
    if (this.ui.playIcon) this.ui.playIcon.style.display = 'none';
    if (this.ui.pauseIcon) this.ui.pauseIcon.style.display = 'block';

    const intervalMs = 1000 / this.playback.speed;
    
    this.playback.interval = setInterval(() => {
        const current = parseInt(this.ui.scrubber.value);
        const max = parseInt(this.ui.scrubber.max);
        
        if (current >= max) {
            this.stopPlayback();
            return;
        }

        this.ui.scrubber.value = current + 1;
        this.updateToolpathDisplay(current + 1);
    }, intervalMs);
};

GCodePreview.prototype.stopPlayback = function() {
    this.playback.isPlaying = false;
    if (this.ui.playBtn) this.ui.playBtn.classList.remove('playing');
    if (this.ui.playIcon) this.ui.playIcon.style.display = 'block';
    if (this.ui.pauseIcon) this.ui.pauseIcon.style.display = 'none';

    if (this.playback.interval) {
        clearInterval(this.playback.interval);
        this.playback.interval = null;
    }
};

GCodePreview.prototype.restartPlayback = function() {
    if (this.ui.scrubber) {
        this.ui.scrubber.value = 0;
        this.updateToolpathDisplay(0);
    }
    if (this.playback.isPlaying) {
        this.stopPlayback();
        setTimeout(() => this.startPlayback(), 100);
    }
};

// --- View & Controls ---

GCodePreview.prototype.resetView = function() {
    if (this.cameraState.optimalPos) {
        this.camera.position.copy(this.cameraState.optimalPos);
        this.camera.lookAt(this.cameraState.optimalLookAt);
    }
};

GCodePreview.prototype.handleResize = function() {
    if (!this.container || !this.camera || !this.renderer) return;
    
    const w = this.container.clientWidth;
    const h = this.container.clientHeight;
    
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
    this.renderer.setSize(w, h);
};

GCodePreview.prototype.animate = function() {
    requestAnimationFrame(() => this.animate());
    if (this.renderer && this.scene && this.camera) {
        this.renderer.render(this.scene, this.camera);
    }
};

GCodePreview.prototype.initMouseControls = function() {
    let isDragging = false;
    let prevPos = { x: 0, y: 0 };
    const canvas = this.canvas;
    const camera = this.camera;

    canvas.addEventListener('mousedown', (e) => {
        isDragging = true;
        prevPos = { x: e.clientX, y: e.clientY };
    });

    window.addEventListener('mousemove', (e) => {
        if (!isDragging) return;
        const dx = e.clientX - prevPos.x;
        const dy = e.clientY - prevPos.y;

        // Manual orbit logic to match original behavior
        const rotSpeed = 0.005;
        const x = camera.position.x;
        const z = camera.position.z;
        
        camera.position.x = x * Math.cos(dx * rotSpeed) - z * Math.sin(dx * rotSpeed);
        camera.position.z = x * Math.sin(dx * rotSpeed) + z * Math.cos(dx * rotSpeed);
        camera.position.y += dy * rotSpeed * 5;
        camera.lookAt(0, 0, 0);

        prevPos = { x: e.clientX, y: e.clientY };
    });

    window.addEventListener('mouseup', () => isDragging = false);

    canvas.addEventListener('wheel', (e) => {
        e.preventDefault();
        const dist = camera.position.length();
        const newDist = dist * (1 + e.deltaY * 0.001);
        camera.position.multiplyScalar(newDist / dist);
    }, { passive: false });
};
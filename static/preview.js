// @ts-check

/** @type {import("three")} */
const THREE = /** @type {any} */ (window).THREE;


/** toolpathMoves is mutable and will be populated populated */
function visualizeGcode(gcode, toolpathMoves) {
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

    // Create tool representation (endmill)
    const toolDiameter = 0.157; // 4mm default
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
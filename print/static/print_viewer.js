/* Mesh-on-bed viewer for the 3D print wizard. Parses binary or ASCII STL, shows
 * the part on a plane the size of the printer bed, orbit controls, reset view.
 * No scrubber, no animation. Requires THREE and THREE.OrbitControls (loaded by
 * templates/print_wizard.html). parseSTL is exported for node tests.
 */
(function () {
  'use strict';

  // Case-insensitive "solid" at byte 0, the ASCII STL marker.
  function startsWithSolid(bytes) {
    var solid = [115, 111, 108, 105, 100];  // s o l i d
    if (bytes.length < 5) return false;
    for (var i = 0; i < 5; i++) {
      if ((bytes[i] | 32) !== solid[i]) return false;
    }
    return true;
  }

  function parseSTL(arrayBuffer) {
    var bytes = new Uint8Array(arrayBuffer);
    if (bytes.length < 15) throw new Error('STL file is empty or truncated');
    var count = 0, positions, i, o;
    var dv = new DataView(arrayBuffer);
    var binaryCount = bytes.length >= 84 ? dv.getUint32(80, true) : -1;
    // Like three.js's STLLoader: a binary STL may carry trailing bytes, so the length
    // only has to reach the triangle table. An exact fit is binary whatever the header
    // says; a longer file is binary only when it does not start with "solid".
    var fits = bytes.length >= 84 && bytes.length >= 84 + 50 * binaryCount;
    var isBinary = fits && (bytes.length === 84 + 50 * binaryCount || !startsWithSolid(bytes));
    if (isBinary) {
      count = binaryCount;
      positions = new Float32Array(count * 9);
      for (i = 0; i < count; i++) {
        o = 84 + i * 50 + 12;
        for (var k = 0; k < 9; k++) positions[i * 9 + k] = dv.getFloat32(o + k * 4, true);
      }
    } else {
      var text = '';
      for (i = 0; i < bytes.length; i += 8192) text += String.fromCharCode.apply(null, bytes.subarray(i, i + 8192));
      var re = /vertex\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)/g, m, list = [];
      while ((m = re.exec(text)) !== null) list.push(parseFloat(m[1]), parseFloat(m[2]), parseFloat(m[3]));
      count = Math.floor(list.length / 9);
      positions = new Float32Array(list.slice(0, count * 9));
    }
    if (count === 0) throw new Error('STL file has no triangles');
    var min = [Infinity, Infinity, Infinity], max = [-Infinity, -Infinity, -Infinity];
    for (i = 0; i < positions.length; i += 3) {
      for (var a = 0; a < 3; a++) {
        if (positions[i + a] < min[a]) min[a] = positions[i + a];
        if (positions[i + a] > max[a]) max[a] = positions[i + a];
      }
    }
    return { positions: positions, count: count, min: min, max: max };
  }

  function themeColors() {
    var light = typeof document !== 'undefined' && document.documentElement.getAttribute('data-theme') === 'light';
    return light ? { bg: 0xf2f4f7, bed: 0xd8dee6, grid: 0xb8c2cc, part: 0x2f81f7 }
                 : { bg: 0x0a0e14, bed: 0x1e2632, grid: 0x30363d, part: 0x4c9aff };
  }

  // Camera distance for a part of span partSpan on a bedX x bedY bed. Scaling off the
  // bed alone turns a 12 mm part on a 256 mm bed into a speck, so the part's own span
  // leads and the bed only sets a floor that keeps the whole plate roughly in frame.
  function fitDistance(partSpan, bedX, bedY) {
    return Math.max(partSpan * 2.5, Math.max(bedX, bedY) * 0.35);
  }

  function PrintViewer(els) {
    this.els = els;
    this.mesh = null;
    this.bed = null;
    this.grid = null;
    this.fit = { pos: [200, 200, 200], target: [0, 0, 0] };
    this._initScene();
    var self = this;
    if (els.resetButton) els.resetButton.addEventListener('click', function () { self.resetView(); });
  }

  PrintViewer.prototype._initScene = function () {
    var canvas = this.els.canvas;
    var container = this.els.container || canvas.parentElement;
    this.container = container;
    var w = container.clientWidth || 600, h = container.clientHeight || 320;
    this.scene = new THREE.Scene();
    this.scene.background = new THREE.Color(themeColors().bg);
    this.camera = new THREE.PerspectiveCamera(45, w / h, 0.1, 5000);
    this.renderer = new THREE.WebGLRenderer({ canvas: canvas, antialias: true });
    this.renderer.setSize(w, h);
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    this.scene.add(new THREE.AmbientLight(0xffffff, 0.6));
    var dir = new THREE.DirectionalLight(0xffffff, 0.8);
    dir.position.set(100, 300, 200);
    this.scene.add(dir);
    this.controls = new THREE.OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = 0.1;
    this.controls.maxPolarAngle = Math.PI / 2 + 0.05;
    this.controls.mouseButtons = { LEFT: THREE.MOUSE.ROTATE, MIDDLE: THREE.MOUSE.PAN, RIGHT: null };
    this.resetView();
    var self = this;
    (function animate() {
      requestAnimationFrame(animate);
      self.controls.update();
      self.renderer.render(self.scene, self.camera);
    })();
    window.addEventListener('resize', function () { self._resize(); });
  };

  PrintViewer.prototype._resize = function () {
    var w = this.container.clientWidth || 600, h = this.container.clientHeight || 320;
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
    this.renderer.setSize(w, h);
  };

  // STL (x, y, z) -> THREE (x, z, -y): z up, like the G-code viewer.
  PrintViewer.prototype.load = function (arrayBuffer, printer) {
    var stl = parseSTL(arrayBuffer);
    var colors = themeColors();
    var bedX = (printer && printer.bed_mm && printer.bed_mm.x) || 256;
    var bedY = (printer && printer.bed_mm && printer.bed_mm.y) || 256;
    if (this.mesh) this.scene.remove(this.mesh);
    if (this.bed) this.scene.remove(this.bed);
    if (this.grid) this.scene.remove(this.grid);

    var cx = (stl.min[0] + stl.max[0]) / 2, cy = (stl.min[1] + stl.max[1]) / 2, z0 = stl.min[2];
    var arr = new Float32Array(stl.positions.length);
    for (var i = 0; i < stl.positions.length; i += 3) {
      arr[i] = stl.positions[i] - cx;
      arr[i + 1] = stl.positions[i + 2] - z0;
      arr[i + 2] = -(stl.positions[i + 1] - cy);
    }
    var geom = new THREE.BufferGeometry();
    geom.setAttribute('position', new THREE.BufferAttribute(arr, 3));
    geom.computeVertexNormals();
    this.mesh = new THREE.Mesh(geom, new THREE.MeshPhongMaterial({ color: colors.part, side: THREE.DoubleSide }));
    this.scene.add(this.mesh);

    this.bed = new THREE.Mesh(new THREE.PlaneGeometry(bedX, bedY),
                              new THREE.MeshPhongMaterial({ color: colors.bed, side: THREE.DoubleSide }));
    this.bed.rotation.x = -Math.PI / 2;
    this.bed.position.y = 0;                 // the bed sits at THREE y = 0
    this.scene.add(this.bed);
    // The grid floats a hair above the bed so it does not z-fight the part's bottom face.
    this._gridSpec = { size: Math.max(bedX, bedY), divisions: Math.round(Math.max(bedX, bedY) / 10) };
    this.grid = new THREE.GridHelper(this._gridSpec.size, this._gridSpec.divisions, colors.grid, colors.grid);
    this.grid.position.y = 0.01;
    this.scene.add(this.grid);

    var size = { x: stl.max[0] - stl.min[0], y: stl.max[1] - stl.min[1], z: stl.max[2] - stl.min[2] };
    var span = fitDistance(Math.max(size.x, size.y, size.z), bedX, bedY);
    this.fit = { pos: [span * 0.55, span * 0.6, span * 0.75], target: [0, (stl.max[2] - z0) / 2, 0] };
    this.resetView();
    if (this.els.emptyState) this.els.emptyState.style.display = 'none';
    this._resize();
  };

  PrintViewer.prototype.resetView = function () {
    this.camera.position.set(this.fit.pos[0], this.fit.pos[1], this.fit.pos[2]);
    this.controls.target.set(this.fit.target[0], this.fit.target[1], this.fit.target[2]);
    this.controls.update();
  };

  PrintViewer.prototype.setTheme = function () {
    var colors = themeColors();
    if (!this.scene) return;
    this.scene.background = new THREE.Color(colors.bg);
    if (this.mesh && this.mesh.material && this.mesh.material.color) this.mesh.material.color.set(colors.part);
    if (this.bed && this.bed.material && this.bed.material.color) this.bed.material.color.set(colors.bed);
    // A GridHelper bakes its colours into vertex colours, so it cannot be recoloured in
    // place: build a fresh helper with the same size and divisions.
    if (this.grid && this._gridSpec) {
      this.scene.remove(this.grid);
      this.grid = new THREE.GridHelper(this._gridSpec.size, this._gridSpec.divisions, colors.grid, colors.grid);
      this.grid.position.y = 0.01;
      this.scene.add(this.grid);
    }
  };

  if (typeof window !== 'undefined') window.PrintViewer = PrintViewer;
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = { parseSTL: parseSTL, PrintViewer: PrintViewer, fitDistance: fitDistance };
  }
})();

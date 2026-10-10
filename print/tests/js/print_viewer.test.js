'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs');
const v = require(path.join(__dirname, '..', '..', 'static', 'print_viewer.js'));

function binaryStl(tris) {
  const buf = Buffer.alloc(84 + tris.length * 50);
  buf.writeUInt32LE(tris.length, 80);
  tris.forEach((t, i) => {
    let o = 84 + i * 50 + 12;   // skip the normal
    for (const vert of t) for (const c of vert) { buf.writeFloatLE(c, o); o += 4; }
  });
  return buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.length);
}

test('parseSTL reads binary triangles and bounds', () => {
  const buf = binaryStl([[[0, 0, 0], [3, 0, 0], [0, 5, 2]], [[-1, 0, 0], [0, 0, 0], [0, 1, 0]]]);
  const r = v.parseSTL(buf);
  assert.equal(r.count, 2);
  assert.equal(r.positions.length, 18);
  assert.deepEqual(r.min, [-1, 0, 0]);
  assert.deepEqual(r.max, [3, 5, 2]);
});

test('parseSTL reads ASCII', () => {
  const text = 'solid t\n facet normal 0 0 1\n  outer loop\n   vertex 0 0 0\n   vertex 10 0 0\n   vertex 0 20 5\n  endloop\n endfacet\nendsolid t\n';
  const r = v.parseSTL(new TextEncoder().encode(text).buffer);
  assert.equal(r.count, 1);
  assert.deepEqual(r.max, [10, 20, 5]);
});

test('parseSTL handles the checked-in sample part', () => {
  const data = fs.readFileSync(path.join(__dirname, '..', '..', 'sample_part.stl'));
  const r = v.parseSTL(data.buffer.slice(data.byteOffset, data.byteOffset + data.length));
  assert.equal(r.count, 20);
  assert.deepEqual(r.min, [0, 0, 0]);
  assert.deepEqual(r.max, [40, 30, 12]);
});

test('parseSTL rejects an empty file', () => {
  assert.throws(() => v.parseSTL(new ArrayBuffer(0)), /STL/);
});

// load() needs WebGL in a real browser, so the node tests drive it against a
// stub THREE instead: it guards the stated
// coordinate contract, STL (x, y, z) -> THREE (x, z, -y), footprint centred on
// the bed and the lowest point at y = 0.
function stubThree() {
  class BufferGeometry {
    setAttribute(name, attr) { this.attributes = this.attributes || {}; this.attributes[name] = attr; }
    computeVertexNormals() { this.normalsComputed = true; }
  }
  // three.js materials hold a Color object, not a number; the stub mirrors that so the
  // theme repaint (material.color.set) can be checked.
  class Color { constructor(value) { this.value = value; } set(value) { this.value = value; } }
  return {
    Color,
    DoubleSide: 2,
    BufferGeometry,
    BufferAttribute: class { constructor(array, itemSize) { this.array = array; this.itemSize = itemSize; } },
    Mesh: class { constructor(geometry, material) { this.geometry = geometry; this.material = material; this.rotation = { x: 0 }; this.position = { y: 0 }; } },
    MeshPhongMaterial: class { constructor(opts) { Object.assign(this, opts); this.color = new Color(opts.color); } },
    PlaneGeometry: class { constructor(w, h) { this.w = w; this.h = h; } },
    GridHelper: class {
      constructor(size, divisions, c1, c2) {
        this.size = size; this.divisions = divisions; this.c1 = c1; this.c2 = c2;
        this.position = { y: 0 };
      }
    },
  };
}

function stubViewer() {
  const added = [];
  const viewer = Object.create(v.PrintViewer.prototype);
  viewer.els = { emptyState: { style: {} } };
  viewer.container = { clientWidth: 600, clientHeight: 320 };
  viewer.scene = {
    add: (o) => added.push(o),
    remove: (o) => { const i = added.indexOf(o); if (i >= 0) added.splice(i, 1); },
  };
  viewer.camera = { position: { set: () => {} }, aspect: 1, updateProjectionMatrix: () => {} };
  viewer.controls = { target: { set: () => {} }, update: () => {} };
  viewer.renderer = { setSize: () => {} };
  viewer.mesh = viewer.bed = viewer.grid = null;
  return { viewer, added };
}

test('load maps STL coordinates, centres the footprint and sits the part on the bed', () => {
  const saved = global.THREE;
  global.THREE = stubThree();
  try {
    const { viewer } = stubViewer();
    // One triangle spanning x 0..40, y 0..30, z 4..12.
    const buf = binaryStl([[[0, 0, 4], [40, 0, 12], [0, 30, 4]]]);
    viewer.load(buf, { name: 'Test printer', bed_mm: { x: 256, y: 256 }, height_mm: 250 });

    const p = Array.from(viewer.mesh.geometry.attributes.position.array);
    assert.deepEqual(p, [
      -20, 0, 15,     // STL (0, 0, 4)   -> x-20, z-4=0,  -(0-15)=15
      20, 8, 15,      // STL (40, 0, 12) -> x 20, z-4=8,  -(0-15)=15
      -20, 0, -15,    // STL (0, 30, 4)  -> x-20, z-4=0,  -(30-15)=-15
    ]);
    assert.equal(viewer.bed.geometry.w, 256);
    assert.equal(viewer.bed.geometry.h, 256);
    assert.equal(viewer.grid.size, 256);
    assert.equal(viewer.els.emptyState.style.display, 'none');
  } finally {
    if (saved === undefined) delete global.THREE; else global.THREE = saved;
  }
});

test('load falls back to a 256 mm bed when the printer has no bed_mm', () => {
  const saved = global.THREE;
  global.THREE = stubThree();
  try {
    const { viewer } = stubViewer();
    viewer.load(binaryStl([[[0, 0, 0], [1, 0, 0], [0, 1, 0]]]), null);
    assert.equal(viewer.bed.geometry.w, 256);
    assert.equal(viewer.bed.geometry.h, 256);
  } finally {
    if (saved === undefined) delete global.THREE; else global.THREE = saved;
  }
});

// --- fix wave 1: camera fit, tolerant binary detection, theme repaint -------

test('fitDistance leads with the part span and only floors on the bed', () => {
  // A 12 mm part on a 256 mm bed: the bed floor (256 * 0.35) wins, so the whole
  // plate stays in frame instead of the camera diving into the part.
  assert.equal(v.fitDistance(12, 256, 256), 89.6);
  // A 200 mm part: its own span wins, so a big part is not clipped.
  assert.equal(v.fitDistance(200, 256, 256), 500);
  assert.equal(v.fitDistance(0, 256, 256), 89.6);
});

test('load frames a small part by its own span, with the bed at y 0 and the grid above it', () => {
  const saved = global.THREE;
  global.THREE = stubThree();
  try {
    const { viewer } = stubViewer();
    // A 12 mm cube corner: bed-scaled framing alone would make it a speck.
    viewer.load(binaryStl([[[0, 0, 0], [12, 0, 0], [0, 12, 12]]]), null);
    const span = v.fitDistance(12, 256, 256);
    assert.deepEqual(viewer.fit.pos, [span * 0.55, span * 0.6, span * 0.75]);
    assert.deepEqual(viewer.fit.target, [0, 6, 0]);   // half the part height
    assert.equal(viewer.bed.position.y, 0);
    assert.equal(viewer.grid.position.y, 0.01);
  } finally {
    if (saved === undefined) delete global.THREE; else global.THREE = saved;
  }
});

test('parseSTL accepts a binary STL with trailing bytes', () => {
  const base = Buffer.from(new Uint8Array(binaryStl([
    [[0, 0, 0], [3, 0, 0], [0, 5, 2]],
    [[-1, 0, 0], [0, 0, 0], [0, 1, 0]],
    [[0, 0, 0], [0, 0, 4], [2, 0, 0]],
  ])));
  const buf = Buffer.concat([base, Buffer.from([0x0d, 0x0a])]);
  const r = v.parseSTL(buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.length));
  assert.equal(r.count, 3);
  assert.deepEqual(r.max, [3, 5, 4]);
});

test('parseSTL keeps an ASCII file ASCII when bytes 80..83 look like a triangle count', () => {
  const body = ' facet normal 0 0 1\n  outer loop\n   vertex 0 0 0\n   vertex 10 0 0\n' +
               '   vertex 0 20 5\n  endloop\n endfacet\nendsolid t\n';
  const buf = Buffer.from('solid ' + 'x'.repeat(100) + '\n' + body, 'ascii');
  buf.writeUInt32LE(1, 80);            // 84 + 50 * 1 = 134 bytes, well inside the file,
  assert.ok(buf.length >= 134);        // so only the "solid" header keeps this ASCII
  const r = v.parseSTL(buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.length));
  assert.equal(r.count, 1);
  assert.deepEqual(r.max, [10, 20, 5]);
});

test('setTheme repaints the bed, grid and part, not just the background', () => {
  const saved = global.THREE;
  global.THREE = stubThree();
  try {
    const { viewer, added } = stubViewer();
    viewer.load(binaryStl([[[0, 0, 0], [10, 0, 0], [0, 10, 5]]]), null);
    const oldGrid = viewer.grid;
    viewer.mesh.material.color.set(0x000000);
    viewer.bed.material.color.set(0x000000);

    viewer.setTheme();

    assert.equal(viewer.mesh.material.color.value, 0x4c9aff);   // dark-theme part
    assert.equal(viewer.bed.material.color.value, 0x1e2632);    // dark-theme bed
    assert.notEqual(viewer.grid, oldGrid);                      // helper swapped, not tinted
    assert.equal(viewer.grid.c1, 0x30363d);
    assert.equal(viewer.grid.position.y, 0.01);
    assert.ok(added.includes(viewer.grid));
    assert.ok(!added.includes(oldGrid));
    assert.equal(viewer.scene.background.value, 0x0a0e14);
  } finally {
    if (saved === undefined) delete global.THREE; else global.THREE = saved;
  }
});

// --- plate preview ------------------------------------------------------------

const G = require(path.join(__dirname, '..', '..', 'static', 'plate_geometry.js'));

test('transformPositions applies rotation and translation', () => {
  // +x (the part's x axis up), turned 90 degrees, centre at (100, 50): by hand,
  // M = Rz(90) R_+x = [[0, -1, 0], [0, 0, -1], [1, 0, 0]] and t = (100, 50, 0).
  const m = G.copyMatrix('+x', 90, [0, 0, 0], 100, 50);
  const out = v.transformPositions(new Float32Array([0, 0, 0, 1, 0, 0, 0, 1, 0]), m);
  assert.ok(out instanceof Float32Array);
  assert.deepEqual(Array.from(out), [100, 50, 0, 100, 50, 1, 99, 50, 0]);
  // Any offset: the same as applying copyMatrix's [M | t] to each vertex.
  const m2 = G.copyMatrix('-y', 37, [5, -3, 2], 120.5, 80);
  const pts = [3, 4, 5, -2, 7, 1];
  const got = v.transformPositions(new Float32Array(pts), m2);
  for (let i = 0; i < pts.length; i += 3) {
    for (let r = 0; r < 3; r++) {
      const want = m2[r][0] * pts[i] + m2[r][1] * pts[i + 1] + m2[r][2] * pts[i + 2] + m2[r][3];
      assert.ok(Math.abs(got[i + r] - want) < 1e-4, `vertex ${i / 3} axis ${r}`);
    }
  }
});

test('loadPlate draws one mesh per copy in plate coordinates, centred on the plate', () => {
  const saved = global.THREE;
  global.THREE = stubThree();
  try {
    const { viewer, added } = stubViewer();
    const stl = binaryStl([[[0, 0, 0], [1, 0, 0], [0, 1, 0]]]);
    const footprints = { '+z': { hull: [], height: 0, offset: [0, 0, 0] }, '+x': { hull: [], height: 1, offset: [0, 0, 0] } };
    const parts = { r1: { name: 'Tri', footprints } };
    const plate = { min: [0, 0], max: [200, 100], height: 250, margin: 3, spacing: 5 };
    const copies = [{ ref: 'r1', orientation: '+x', angle: 90, x: 100, y: 50, placed: true },
                    { ref: 'r1', orientation: '+z', angle: 0, x: 20, y: 30 }];
    viewer.loadPlate({ r1: stl }, copies, parts, plate);
    assert.equal(viewer.plateMeshes.length, 2);
    // Plate (x, y, z) -> THREE (x - 100, z, -(y - 50)).
    assert.deepEqual(Array.from(viewer.plateMeshes[0].geometry.attributes.position.array),
                     [0, 0, 0, 0, 1, 0, -1, 0, 0]);
    assert.deepEqual(Array.from(viewer.plateMeshes[1].geometry.attributes.position.array),
                     [-80, 0, 20, -79, 0, 20, -80, 0, 19]);
    assert.equal(viewer.bed.geometry.w, 200);
    assert.equal(viewer.bed.geometry.h, 100);
    assert.ok(viewer.plateMeshes.every((mesh) => added.includes(mesh)));
    // The sample part's load() takes the plate's meshes away again.
    viewer.load(stl, null);
    assert.equal(viewer.plateMeshes.length, 0);
    assert.equal(added.filter((o) => o.geometry && o.geometry.attributes).length, 1);
  } finally {
    if (saved === undefined) delete global.THREE; else global.THREE = saved;
  }
});

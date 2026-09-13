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
  const data = fs.readFileSync(path.join(__dirname, '..', '..', 'print', 'sample_part.stl'));
  const r = v.parseSTL(data.buffer.slice(data.byteOffset, data.byteOffset + data.length));
  assert.equal(r.count, 20);
  assert.deepEqual(r.min, [0, 0, 0]);
  assert.deepEqual(r.max, [40, 30, 12]);
});

test('parseSTL rejects an empty file', () => {
  assert.throws(() => v.parseSTL(new ArrayBuffer(0)), /STL/);
});

// The container's headless Chrome has no WebGL, so load() cannot be smoked in a
// browser here. Drive it against a stub THREE instead: it guards the stated
// coordinate contract, STL (x, y, z) -> THREE (x, z, -y), footprint centred on
// the bed and the lowest point at y = 0.
function stubThree() {
  class BufferGeometry {
    setAttribute(name, attr) { this.attributes = this.attributes || {}; this.attributes[name] = attr; }
    computeVertexNormals() { this.normalsComputed = true; }
  }
  return {
    DoubleSide: 2,
    BufferGeometry,
    BufferAttribute: class { constructor(array, itemSize) { this.array = array; this.itemSize = itemSize; } },
    Mesh: class { constructor(geometry, material) { this.geometry = geometry; this.material = material; this.rotation = { x: 0 }; this.position = { y: 0 }; } },
    MeshPhongMaterial: class { constructor(opts) { Object.assign(this, opts); } },
    PlaneGeometry: class { constructor(w, h) { this.w = w; this.h = h; } },
    GridHelper: class { constructor(size, divisions) { this.size = size; this.divisions = divisions; } },
  };
}

function stubViewer() {
  const added = [];
  const viewer = Object.create(v.PrintViewer.prototype);
  viewer.els = { emptyState: { style: {} } };
  viewer.container = { clientWidth: 600, clientHeight: 320 };
  viewer.scene = { add: (o) => added.push(o), remove: () => {} };
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
    viewer.load(buf, { name: 'X1C', bed_mm: { x: 256, y: 256 }, height_mm: 250 });

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

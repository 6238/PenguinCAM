'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const w = require(path.join(__dirname, '..', '..', 'static', 'print_wizard.js'));

test('formatDuration renders h m s and drops empty leading units', () => {
  assert.equal(w.formatDuration(790), '13m 10s');
  assert.equal(w.formatDuration(3725), '1h 2m 5s');
  assert.equal(w.formatDuration(59), '59s');
  assert.equal(w.formatDuration(3600), '1h 0m 0s');
  assert.equal(w.formatDuration(0), '0s');
});

test('fitsBed compares every axis', () => {
  const bed = { x: 256, y: 256 };
  assert.equal(w.fitsBed({ x: 40, y: 30, z: 12 }, bed, 250), true);
  assert.equal(w.fitsBed({ x: 300, y: 30, z: 12 }, bed, 250), false);
  assert.equal(w.fitsBed({ x: 40, y: 257, z: 12 }, bed, 250), false);
  assert.equal(w.fitsBed({ x: 40, y: 30, z: 251 }, bed, 250), false);
  assert.equal(w.fitsBed({ x: 256, y: 256, z: 250 }, bed, 250), true);
});

test('summaryLine omits null fields', () => {
  assert.equal(w.summaryLine({ print_time_s: 790, filament_g: 3.31, filament_m: 1.09, layer_count: 60 }),
    'Print time 13m 10s · Filament 3.31 g (1.09 m) · 60 layers');
  assert.equal(w.summaryLine({ print_time_s: null, filament_g: 3.31, filament_m: null, layer_count: 60 }),
    'Filament 3.31 g · 60 layers');
  assert.equal(w.summaryLine({ print_time_s: null, filament_g: null, filament_m: null, layer_count: null }), '');
  assert.equal(w.summaryLine({ print_time_s: 60, filament_g: 0.5, filament_m: 0.2, layer_count: 1 }),
    'Print time 1m 0s · Filament 0.5 g (0.2 m) · 1 layer');
});

test('printPageUrl carries source, theme and an encoded return path', () => {
  assert.equal(w.printPageUrl('upload', 'dark', '/app?config=x&y=1'),
    '/print?source=upload&theme=dark&return=%2Fapp%3Fconfig%3Dx%26y%3D1');
  assert.equal(w.printPageUrl('onshape', 'light', '/onshape-panel?d=1'),
    '/print?source=onshape&theme=light&return=%2Fonshape-panel%3Fd%3D1');
});

test('statusText maps states and appends elapsed seconds', () => {
  assert.equal(w.statusText({ state: 'queued', elapsed_s: 0.4 }), 'Queued, 0 s');
  assert.equal(w.statusText({ state: 'running', elapsed_s: 12.6 }), 'Slicing, 13 s');
  assert.equal(w.statusText({ state: 'running' }), 'Slicing');
});

test('tooBigMessage names both sizes', () => {
  assert.equal(w.tooBigMessage({ x: 300, y: 30, z: 12 }, { x: 256, y: 256 }, 250),
    'Part is larger than the bed: 300 x 30 x 12 mm on 256 x 256 x 250 mm.');
});

test('errorForStatus picks the student message', () => {
  assert.equal(w.errorForStatus(429, {}), 'Too many slices in a minute, try again shortly.');
  assert.equal(w.errorForStatus(503, { error: 'The slicer is busy, try again in a minute.' }),
    'The slicer is busy, try again in a minute.');
  assert.equal(w.errorForStatus(404, {}), 'This slice has expired, go back and slice again.');
  assert.equal(w.errorForStatus(401, { error: 'Session verification required.' }), 'Session verification required.');
  assert.equal(w.errorForStatus(500, {}), 'Slicing failed (HTTP 500).');
});

test('appendNote keeps a note already on the panel', () => {
  assert.equal(w.appendNote('', 'Could not load the 3D view: HTTP 500'),
    'Could not load the 3D view: HTTP 500');
  assert.equal(w.appendNote('⚠ Thin walls', 'Could not load the 3D view: HTTP 500'),
    '⚠ Thin walls\nCould not load the 3D view: HTTP 500');
  assert.equal(w.appendNote('⚠ One\n⚠ Two', 'Three'), '⚠ One\n⚠ Two\nThree');
});

test('fitCanvasSize keeps the 600:400 ratio inside both limits', () => {
  // Wide, short space: the height is the binding limit.
  assert.deepEqual(w.fitCanvasSize(900, 200, 600, 400), { w: 300, h: 200 });
  // Narrow, tall space: the width is the binding limit.
  assert.deepEqual(w.fitCanvasSize(360, 900, 600, 400), { w: 360, h: 240 });
  // Plenty of room: never upscaled past the backing store.
  assert.deepEqual(w.fitCanvasSize(2000, 1500, 600, 400), { w: 600, h: 400 });
  assert.deepEqual(w.fitCanvasSize(600, 400, 600, 400), { w: 600, h: 400 });
});

test('fitCanvasSize never goes below the 120 px width floor', () => {
  assert.deepEqual(w.fitCanvasSize(40, 30, 600, 400), { w: 120, h: 80 });
  assert.deepEqual(w.fitCanvasSize(800, 10, 600, 400), { w: 120, h: 80 });
  // A collapsed or not-yet-laid-out quadrant still yields a drawable size.
  assert.deepEqual(w.fitCanvasSize(-50, -50, 600, 400), { w: 120, h: 80 });
  assert.deepEqual(w.fitCanvasSize(NaN, 400, 600, 400), { w: 120, h: 80 });
});

test('makePageId returns 12 lowercase hex characters', () => {
  const fake = {
    getRandomValues(array) {
      assert.ok(array instanceof Uint8Array);
      assert.equal(array.length, 6);
      [0, 1, 0xab, 0xcd, 0xef, 0xff].forEach((v, i) => { array[i] = v; });
      return array;
    },
  };
  assert.equal(w.makePageId(fake), '0001abcdefff');
  assert.match(w.makePageId(globalThis.crypto), /^[0-9a-f]{12}$/);
});

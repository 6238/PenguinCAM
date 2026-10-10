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

const SOURCE = { documentId: 'd1', wvm: 'w', wvmId: 'w1', elementId: 'e1', partId: 'JHD' };
function part(ref, source, name) {
  return { ref, name: name || 'Box', size_mm: { x: 10, y: 20, z: 30 }, triangles: 12, source, footprints: {} };
}

test('addParts ignores a ref already listed', () => {
  let list = w.addParts([], [part('r1', SOURCE)]);
  assert.equal(list.length, 1);
  assert.equal(list[0].quantity, 1);
  list[0].quantity = 4;
  const again = w.addParts(list, [part('r1', SOURCE)]);
  assert.equal(again.length, 1);
  // A new ref with an identical source is the same part.
  assert.equal(w.addParts(list, [part('r2', Object.assign({}, SOURCE))]).length, 1);
  // A configuration makes a different source.
  const configured = w.addParts(list, [part('r3', Object.assign({ configuration: 'Size=20+mm' }, SOURCE))]);
  assert.deepEqual(configured.map((p) => [p.ref, p.quantity]), [['r1', 4], ['r3', 1]]);
  assert.equal(list.length, 1, 'the list passed in is not changed');
});

test('clampQuantity clamps to 1..max', () => {
  assert.equal(w.clampQuantity('3', 25), 3);
  assert.equal(w.clampQuantity(0, 25), 1);
  assert.equal(w.clampQuantity(-4, 25), 1);
  assert.equal(w.clampQuantity(31, 30), 30);
  assert.equal(w.clampQuantity(26, 25), 25);
  assert.equal(w.clampQuantity('', 25), 1);
  assert.equal(w.clampQuantity('abc', 25), 1);
  assert.equal(w.clampQuantity('2.7', 25), 2);
  assert.equal(w.clampQuantity(null, 25), 1);
});

test('swapRefreshed keeps quantities and order', () => {
  const sent = [Object.assign(part('r1', SOURCE), { quantity: 3 }),
                Object.assign(part('r2', Object.assign({}, SOURCE, { partId: 'JKD' }), 'Cyl'), { quantity: 2 })];
  const fresh = [part('n1', SOURCE), part('n2', Object.assign({}, SOURCE, { partId: 'JKD' }), 'Cyl')];
  assert.deepEqual(w.swapRefreshed(sent, sent, fresh).map((p) => [p.ref, p.quantity]), [['n1', 3], ['n2', 2]]);
  // One part failed to refresh: matched by source, and the other keeps its old ref.
  assert.deepEqual(w.swapRefreshed(sent, sent, [fresh[1]]).map((p) => [p.ref, p.quantity]), [['r1', 3], ['n2', 2]]);
  // A part removed while the refresh ran stays removed.
  assert.deepEqual(w.swapRefreshed([sent[1]], sent, fresh).map((p) => p.ref), ['n2']);
});

test('localTimeStamp pads', () => {
  assert.equal(w.localTimeStamp(new Date(2026, 0, 5, 7, 3)), '20260105-0703');
  assert.equal(w.localTimeStamp(new Date(2026, 11, 31, 23, 59)), '20261231-2359');
});

test('jobBody lists copies in order and drops UI fields', () => {
  const state = {
    copies: [{ ref: 'r2', orientation: '+x', angle: 30, x: 50.5, y: 60, placed: true, unplaced: false },
             { ref: 'r1', orientation: '+z', angle: 0, x: 120, y: 40.25, placed: false }],
    parts: [{ ref: 'r1', name: 'A' }, { ref: 'r2', name: 'B' }],
  };
  const body = w.jobBody(state, new Date(2026, 9, 9, 14, 32));
  assert.deepEqual(body, {
    copies: [{ ref: 'r2', orientation: '+x', angle: 30, x: 50.5, y: 60 },
             { ref: 'r1', orientation: '+z', angle: 0, x: 120, y: 40.25 }],
    overrides: {},
    local_time: '20261009-1432',
  });
  assert.ok(!('filament' in body) && !('process' in body), 'the server uses the default set');
  assert.equal(state.copies[0].placed, true, 'the state is not changed');
});

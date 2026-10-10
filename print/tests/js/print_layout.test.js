'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const L = require(path.join(__dirname, '..', '..', 'static', 'print_layout.js'));
const G = require(path.join(__dirname, '..', '..', 'static', 'plate_geometry.js'));

const PLATE = { min: [0, 0], max: [256, 256], height: 250, margin: 3, spacing: 5, printer: 'P' };
const LIMITS = { max_copies: 25, max_quantity: 25, max_job_triangles: 300000,
                 too_many_copies_message: 'Up to 25 copies per plate; print the rest in a second job.',
                 job_triangles_message: 'These copies are too detailed to slice together; lower the quantities.',
                 one_plate_message: 'Not every copy fits on one plate; lower the quantities and print the rest in a second job.' };
const PARKED = 'New copies are waiting beside the plate; press Arrange or drag them on.';

// A w x h rectangle footprint in every orientation, centred.
function rectFootprints(w, h, height) {
  const out = {};
  G.ORIENTATIONS.forEach((o) => {
    out[o] = { hull: [[-w / 2, -h / 2], [w / 2, -h / 2], [w / 2, h / 2], [-w / 2, h / 2]],
               height: height || 10, offset: [0, 0, 0] };
  });
  return out;
}
function part(ref, quantity, w, h, triangles) {
  return { ref, name: ref.toUpperCase(), quantity, triangles: triangles || 12, footprints: rectFootprints(w || 20, h || 10) };
}
function byRef(parts) {
  const out = {};
  parts.forEach((p) => { out[p.ref] = p; });
  return out;
}

test('plateView round trip', () => {
  const view = L.plateView(400, 300, PLATE);
  assert.ok(view.scale > 0);
  [[0, 0], [256, 256], [12.5, 200.25]].forEach(([x, y]) => {
    const [px, py] = L.plateToCanvas(view, x, y);
    assert.ok(px >= 0 && px <= 400 && py >= 0 && py <= 300, 'the plate is inside the canvas');
    const back = L.canvasToPlate(view, px, py);
    assert.ok(Math.abs(back[0] - x) < 1e-9 && Math.abs(back[1] - y) < 1e-9);
  });
  // Plate +y is drawn upward: a larger y is a smaller canvas y.
  assert.ok(L.plateToCanvas(view, 0, 200)[1] < L.plateToCanvas(view, 0, 10)[1]);
  // The plate is square, the canvas wide: the height binds and the plate is centred across.
  const left = L.plateToCanvas(view, 0, 0)[0], right = L.plateToCanvas(view, 256, 0)[0];
  assert.ok(Math.abs((left + right) / 2 - 200) < 1e-9);
});

test('hitCopy finds the copy under the point', () => {
  const parts = byRef([part('a', 2, 20, 10)]);
  const copies = [{ ref: 'a', orientation: '+z', angle: 0, x: 50, y: 50 },
                  { ref: 'a', orientation: '+z', angle: 0, x: 60, y: 50 }];
  assert.equal(L.hitCopy(copies, parts, [45, 50]), 0);
  assert.equal(L.hitCopy(copies, parts, [55, 50]), 1, 'the topmost (last drawn) copy wins');
  assert.equal(L.hitCopy(copies, parts, [69, 54]), 1);
  assert.equal(L.hitCopy(copies, parts, [100, 100]), -1);
  // Turned 90 degrees, the 20 x 10 footprint is 10 wide and 20 tall.
  const turned = [{ ref: 'a', orientation: '+z', angle: 90, x: 50, y: 50 }];
  assert.equal(L.hitCopy(turned, parts, [50, 58]), 0);
  assert.equal(L.hitCopy(turned, parts, [58, 50]), -1);
});

test('rotateCopy wraps to 0..359', () => {
  const c = { ref: 'a', orientation: '+z', angle: 0, x: 1, y: 2 };
  assert.equal(L.rotateCopy(c, -90).angle, 270);
  assert.equal(L.rotateCopy(c, 90).angle, 90);
  assert.equal(L.rotateCopy({ ...c, angle: 270 }, 90).angle, 0);
  assert.equal(L.rotateCopy({ ...c, angle: 300 }, 90).angle, 30);
  assert.equal(c.angle, 0, 'the copy passed in is not changed');
  assert.equal(L.rotateCopy(c, 90).x, 1);
});

test('setAngle ignores text and wraps 400 to 40', () => {
  const c = { ref: 'a', orientation: '+z', angle: 15, x: 0, y: 0 };
  assert.equal(L.setAngle(c, '400').angle, 40);
  assert.equal(L.setAngle(c, 400).angle, 40);
  assert.equal(L.setAngle(c, '-90').angle, 270);
  assert.equal(L.setAngle(c, 'abc').angle, 15);
  assert.equal(L.setAngle(c, '').angle, 15);
  assert.equal(L.setAngle(c, '12.5').angle, 15, 'integers only');
  assert.equal(L.setAngle(c, '360').angle, 0);
  assert.equal(L.setOrientation(c, '+x').orientation, '+x');
  assert.equal(L.setOrientation(c, 'sideways').orientation, '+z');
});

test('syncCopies keeps placed copies and parks new ones', () => {
  // Arranged, then back to Parts: a quantity raised and a new part added.
  let parts = [part('a', 2), part('b', 1)];
  let copies = L.syncCopies([], parts, PLATE);
  assert.deepEqual(copies.map((c) => c.ref), ['a', 'a', 'b']);
  assert.ok(copies.every((c) => c.placed === false));
  copies = G.arrangeCopies(copies, byRef(parts), PLATE).copies.map((c) => ({ ...c, placed: true }));
  copies[1] = L.setAngle(copies[1], 30);
  const before = copies.map((c) => [c.x, c.y, c.angle, c.orientation]);
  parts = [part('a', 3), part('b', 1), part('c', 1)];
  const next = L.syncCopies(copies, parts, PLATE);
  assert.deepEqual(next.map((c) => c.ref), ['a', 'a', 'a', 'b', 'c'], 'one copy per unit, in parts order');
  assert.deepEqual([next[0], next[1], next[3]].map((c) => [c.x, c.y, c.angle, c.orientation]),
                   [before[0], before[1], before[2]]);
  assert.ok([next[0], next[1], next[3]].every((c) => c.placed === true));
  const fresh = [next[2], next[4]];
  assert.ok(fresh.every((c) => c.placed === false && c.orientation === '+z' && c.angle === 0));
  // Parked beside the plate, not on it, and not on top of each other.
  fresh.forEach((c) => {
    const hull = G.copyHull(c, parts[0].footprints['+z']);
    assert.ok(hull.every((p) => p[0] > PLATE.max[0]), 'beside the plate');
  });
  assert.notEqual(fresh[0].y, fresh[1].y);
  // The sentence offers Arrange.
  const problems = G.checkPlacement(next, byRef(parts), PLATE);
  assert.equal(L.layoutSentence(problems, next, LIMITS, byRef(parts)), PARKED);
});

test('syncCopies drops the last copies when quantity falls', () => {
  const parts = [part('a', 3), part('b', 2)];
  let copies = L.syncCopies([], parts, PLATE).map((c, i) => ({ ...c, x: 10 * i, placed: true }));
  copies = L.syncCopies(copies, [part('a', 1), part('b', 2)], PLATE);
  assert.deepEqual(copies.map((c) => [c.ref, c.x]), [['a', 0], ['b', 30], ['b', 40]]);
  // A removed part drops all its copies.
  copies = L.syncCopies(copies, [part('b', 2)], PLATE);
  assert.deepEqual(copies.map((c) => [c.ref, c.x]), [['b', 30], ['b', 40]]);
});

test('layoutSentence prefers limits, then one plate, then parked, then the first problem', () => {
  const parts = byRef([part('a', 1, 20, 10, 12), part('big', 1, 20, 10, 200000)]);
  const placed = { ref: 'a', orientation: '+z', angle: 0, x: 50, y: 50, placed: true };
  const problem = [{ rule: 'too_close', copies: [0, 1], message: 'A #1 and A #2 are closer than 5 mm.' }];
  const many = Array.from({ length: 26 }, () => ({ ...placed }));
  const unplaced = { ...placed, unplaced: true };
  const parked = { ...placed, placed: false };
  assert.equal(L.layoutSentence(problem, many.concat([unplaced, parked]), LIMITS, parts), LIMITS.too_many_copies_message);
  const heavy = [{ ...placed, ref: 'big' }, { ...placed, ref: 'big' }];
  assert.equal(L.layoutSentence(problem, heavy, LIMITS, parts), LIMITS.job_triangles_message);
  assert.equal(L.layoutSentence(problem, [placed, unplaced, parked], LIMITS, parts), LIMITS.one_plate_message);
  assert.equal(L.layoutSentence(problem, [placed, parked], LIMITS, parts), PARKED);
  assert.equal(L.layoutSentence(problem, [placed, placed], LIMITS, parts), problem[0].message);
  assert.equal(L.layoutSentence([], [placed], LIMITS, parts), '');
  // Before /print/page answers there are no limits; the rest still applies.
  assert.equal(L.layoutSentence([], [placed, parked], null, parts), PARKED);
});

test('duplicate and delete', () => {
  const copies = [{ ref: 'a', orientation: '+x', angle: 30, x: 50, y: 60, placed: true },
                  { ref: 'b', orientation: '+z', angle: 0, x: 90, y: 60, placed: true }];
  const dup = L.duplicateCopy(copies, 0, PLATE, byRef([part('a', 1), part('b', 1)]));
  assert.equal(dup.length, 3);
  assert.equal(copies.length, 2, 'the list passed in is not changed');
  assert.deepEqual([dup[2].ref, dup[2].orientation, dup[2].angle, dup[2].placed], ['a', '+x', 30, false]);
  assert.ok(dup[2].x > PLATE.max[0], 'the duplicate waits beside the plate');
  const del = L.deleteCopy(dup, 1);
  assert.deepEqual(del.map((c) => c.ref), ['a', 'a']);
  assert.equal(dup.length, 3);
});

test('fitLabel shortens a copy label to fit, keeping its number', () => {
  const measure = (t) => t.length * 6;      // 6 px a character
  assert.equal(L.fitLabel('Long_Bar #1', 100, measure), 'Long_Bar #1');
  assert.equal(L.fitLabel('Long_Bar #1', 48, measure), 'Long\u2026 #1');
  assert.equal(L.fitLabel('Hub #12', 20, measure), '#12');
  assert.equal(L.fitLabel('Hub #12', 10, measure), '');
});

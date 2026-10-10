'use strict';
// Plate geometry in print/static/plate_geometry.js, against the same cases print/plate.py is
// tested with (print/tests/fixtures/plate_cases.json), plus arrangeCopies, which is JS only.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const g = require(path.join(__dirname, '..', '..', 'static', 'plate_geometry.js'));

const CASES = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'fixtures', 'plate_cases.json'), 'utf8'));
const SMALL = { min: [0, 0], max: [150, 150], height: 200, margin: 3, spacing: 5, printer: 'small' };

function square(side) {
  const h = side / 2;
  return { hull: [[-h, -h], [h, -h], [h, h], [-h, h]], height: 10, offset: [0, 0, 0] };
}

test('shared cases', () => {
  CASES.cases.forEach((c) => {
    assert.ok(c.copies.length, c.name + ' has copies');
    assert.deepEqual(g.checkPlacement(c.copies, CASES.parts, CASES.plate), c.problems, c.name);
  });
});

test('shared matrices', () => {
  CASES.matrices.forEach((m) => {
    const got = g.copyMatrix(m.orientation, m.angle, m.offset, m.x, m.y);
    assert.equal(got.length, 3);
    got.forEach((row, r) => {
      assert.equal(row.length, 4);
      row.forEach((v, c) => assert.ok(Math.abs(v - m.matrix[r][c]) <= 1e-9,
                                      m.orientation + ' ' + m.angle + ' [' + r + '][' + c + '] ' + v));
    });
  });
});

test('shared labels and sanitize', () => {
  CASES.labels.forEach((l) => assert.deepEqual(g.copyLabels(l.names), l.labels));
  CASES.sanitize.forEach((s) => assert.equal(g.sanitizePartName(s.in), s.out, JSON.stringify(s.in)));
});

test('matrices put the named axis up', () => {
  g.ORIENTATIONS.forEach((name) => {
    const axis = [0, 0, 0];
    axis['xyz'.indexOf(name[1])] = name[0] === '+' ? 1 : -1;
    [0, 37.5, 90].forEach((angle) => {
      const m = g.copyMatrix(name, angle, [0, 0, 0], 0, 0);
      const up = [0, 1, 2].map((r) => m[r][0] * axis[0] + m[r][1] * axis[1] + m[r][2] * axis[2]);
      [0, 0, 1].forEach((want, i) => assert.ok(Math.abs(up[i] - want) < 1e-12, name + ' ' + angle));
    });
  });
});

test('constants and the orientation table match the Python module', () => {
  assert.equal(g.SPACING_MM, 5.0);
  assert.equal(g.MARGIN_MM, 3.0);
  assert.equal(g.EPS_MM, 1e-6);
  assert.deepEqual(g.ORIENTATIONS, ['+z', '-z', '+x', '-x', '+y', '-y']);
  assert.deepEqual(g.ORIENTATION_MATRICES['+x'], [[0, 0, -1], [0, 1, 0], [1, 0, 0]]);
});

test('plateFromPrinter reads the default printer profile', () => {
  const printer = JSON.parse(fs.readFileSync(path.join(__dirname, '..', '..', 'profiles', 'printer.json'), 'utf8'));
  assert.deepEqual(g.plateFromPrinter(printer), CASES.plate);
});

test('orientation labels cover the six orientations', () => {
  assert.deepEqual(Object.keys(g.ORIENTATION_LABELS).sort(), g.ORIENTATIONS.slice().sort());
  assert.equal(g.ORIENTATION_LABELS['+z'], 'as modelled');
  assert.equal(g.ORIENTATION_LABELS['-z'], 'upside down');
  assert.equal(g.ORIENTATION_LABELS['+x'], 'on its side, −x face down');
  assert.equal(g.ORIENTATION_LABELS['-y'], 'on its side, +y face down');
});

test('arrangeCopies places six 40 mm squares in two rows', () => {
  const parts = { sq: { name: 'Sq', footprints: { '+z': square(40) } } };
  const copies = Array.from({ length: 6 }, () => ({ ref: 'sq', orientation: '+z', angle: 0, x: 0, y: 0 }));
  const out = g.arrangeCopies(copies, parts, SMALL);
  assert.deepEqual(out.unplaced, []);
  assert.deepEqual(out.copies.map((c) => [c.x, c.y]),
    [[23, 23], [68, 23], [113, 23], [23, 68], [68, 68], [113, 68]]);
});

test('arrangeCopies leaves what does not fit unplaced beside the plate', () => {
  const parts = { sq: { name: 'Sq', footprints: { '+z': square(40) } } };
  const copies = Array.from({ length: 11 }, () => ({ ref: 'sq', orientation: '+z', angle: 0, x: 0, y: 0 }));
  const out = g.arrangeCopies(copies, parts, SMALL);
  assert.deepEqual(out.unplaced, [9, 10]);
  assert.deepEqual([out.copies[9].x, out.copies[9].y], [175, 23]);
  assert.deepEqual([out.copies[10].x, out.copies[10].y], [175, 68]);
  assert.equal(g.checkPlacement(out.copies.slice(0, 9), parts, SMALL).length, 0);
});

test('arrangeCopies keeps orientation and angle', () => {
  const parts = CASES.parts;
  const copies = [{ ref: 'r-box', orientation: '+x', angle: 30, x: 0, y: 0 },
                  { ref: 'r-square', orientation: '+z', angle: 45, x: 0, y: 0 }];
  const out = g.arrangeCopies(copies, parts, CASES.plate);
  assert.deepEqual(out.copies.map((c) => [c.ref, c.orientation, c.angle]),
    [['r-box', '+x', 30], ['r-square', '+z', 45]]);
  assert.notEqual(out.copies, copies);
  assert.equal(copies[0].x, 0, 'the copies passed in are not changed');
});

test('arranged copies pass checkPlacement', () => {
  const parts = CASES.parts;
  const copies = [];
  ['r-box', 'r-square', 'r-bracket-a', 'r-bracket-b'].forEach((ref, i) => {
    for (let n = 0; n < 5; n++) copies.push({ ref, orientation: '+z', angle: (i * 30 + n * 17) % 360, x: 0, y: 0 });
  });
  copies.push({ ref: 'r-box', orientation: '+x', angle: 90, x: 0, y: 0 });
  const out = g.arrangeCopies(copies, parts, CASES.plate);
  assert.deepEqual(out.unplaced, []);
  assert.deepEqual(g.checkPlacement(out.copies, parts, CASES.plate), []);
});

test('arrangeCopies puts the largest bounding box first', () => {
  const parts = { small: { name: 'S', footprints: { '+z': square(20) } },
                  big: { name: 'B', footprints: { '+z': square(40) } } };
  const copies = [{ ref: 'small', orientation: '+z', angle: 0, x: 0, y: 0 },
                  { ref: 'big', orientation: '+z', angle: 0, x: 0, y: 0 }];
  const out = g.arrangeCopies(copies, parts, SMALL);
  assert.deepEqual(out.copies.map((c) => [c.x, c.y]), [[58, 13], [23, 23]]);
});

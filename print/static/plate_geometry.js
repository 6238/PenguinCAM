/* PenguinCAM 3D print page: plate geometry, the browser's twin of print/plate.py.
 * The browser checks a placement while the student drags; the server checks it again
 * before slicing and is the authority. Both are tested against
 * print/tests/fixtures/plate_cases.json, so the two cannot drift apart unnoticed.
 * arrangeCopies and the orientation labels live here only: the browser is their only user.
 */
(function () {
  'use strict';

  var SPACING_MM = 5.0;       // the plate answer from /print/page carries the server's value
  var MARGIN_MM = 3.0;
  var EPS_MM = 1e-6;
  var PART_NAME_MAX_CHARS = 40;

  var ORIENTATIONS = ['+z', '-z', '+x', '-x', '+y', '-y'];
  // Copied from print/plate.py: row-major rotations applied to the part's vertices; each
  // orientation is named by the part axis that ends up pointing up.
  var ORIENTATION_MATRICES = {
    '+z': [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
    '-z': [[1, 0, 0], [0, -1, 0], [0, 0, -1]],
    '+x': [[0, 0, -1], [0, 1, 0], [1, 0, 0]],
    '-x': [[0, 0, 1], [0, 1, 0], [-1, 0, 0]],
    '+y': [[1, 0, 0], [0, 0, -1], [0, 1, 0]],
    '-y': [[1, 0, 0], [0, 0, 1], [0, -1, 0]],
  };
  // Named by the face that rests on the plate: the opposite of the axis pointing up.
  var ORIENTATION_LABELS = {
    '+z': 'as modelled',
    '-z': 'upside down',
    '+x': 'on its side, −x face down',
    '-x': 'on its side, +x face down',
    '+y': 'on its side, −y face down',
    '-y': 'on its side, +y face down',
  };

  // cos and sin of `angle` degrees, exact on multiples of 90 (as print/plate.py).
  function cosSin(angle) {
    if (angle % 90 === 0) {
      var quarter = ((Math.floor(angle / 90) % 4) + 4) % 4;
      return [[1, 0], [0, 1], [-1, 0], [0, -1]][quarter];
    }
    var r = angle * Math.PI / 180;
    return [Math.cos(r), Math.sin(r)];
  }

  // The copy's 3x4 [M | t]: M = Rz(angle) R_o, t = (x, y, 0) - Rz(angle) offset.
  function copyMatrix(orientation, angle, offset, x, y) {
    var cs = cosSin(angle), c = cs[0], s = cs[1];
    var rz = [[c, -s, 0], [s, c, 0], [0, 0, 1]];
    var ro = ORIENTATION_MATRICES[orientation];
    var out = [];
    for (var i = 0; i < 3; i++) {
      var row = [];
      for (var j = 0; j < 3; j++) row.push(rz[i][0] * ro[0][j] + rz[i][1] * ro[1][j] + rz[i][2] * ro[2][j]);
      out.push(row);
    }
    var moved = [0, 1, 2].map(function (k) { return rz[k][0] * offset[0] + rz[k][1] * offset[1] + rz[k][2] * offset[2]; });
    out[0].push(x - moved[0]);
    out[1].push(y - moved[1]);
    out[2].push(-moved[2]);
    return out;
  }

  // The plate from an Orca printer profile (the server sends it; kept for symmetry).
  function plateFromPrinter(printer) {
    var corners = printer.printable_area.map(function (c) { return c.toLowerCase().split('x').map(Number); });
    var xs = corners.map(function (c) { return c[0]; }), ys = corners.map(function (c) { return c[1]; });
    return { min: [Math.min.apply(null, xs), Math.min.apply(null, ys)],
             max: [Math.max.apply(null, xs), Math.max.apply(null, ys)],
             height: Number(printer.printable_height), margin: MARGIN_MM, spacing: SPACING_MM,
             printer: printer.name === undefined ? null : printer.name };
  }

  // The footprint's hull turned by the copy's angle and moved to its (x, y).
  function copyHull(copy, footprint) {
    var cs = cosSin(copy.angle), c = cs[0], s = cs[1];
    return footprint.hull.map(function (p) {
      return [c * p[0] - s * p[1] + copy.x, s * p[0] + c * p[1] + copy.y];
    });
  }

  // Letters, digits, - and _; every other character (code point) becomes _, at most 40,
  // `part` when nothing is left.
  function sanitizePartName(name) {
    var chars = Array.from(typeof name === 'string' ? name : '').map(function (ch) {
      return /^[A-Za-z0-9_-]$/.test(ch) ? ch : '_';
    });
    return chars.slice(0, PART_NAME_MAX_CHARS).join('') || 'part';
  }

  function copyLabels(names) {
    var counts = {};
    return names.map(function (name) {
      var clean = sanitizePartName(name);
      counts[clean] = (counts[clean] || 0) + 1;
      return clean + ' #' + counts[clean];
    });
  }

  // Python's {:g} for the numbers in the sentences (340 -> "340", 2.5 -> "2.5").
  function formatG(value) { return String(parseFloat(Number(value).toPrecision(6))); }

  /* ------------------------------------------------- convex polygon distance */
  function project(poly, ax, ay) {
    var lo = Infinity, hi = -Infinity;
    poly.forEach(function (p) { var d = p[0] * ax + p[1] * ay; if (d < lo) lo = d; if (d > hi) hi = d; });
    return [lo, hi];
  }

  // Separating-axis test over both polygons' edge normals: true unless some axis separates.
  function convexOverlap(a, b) {
    var polys = [a, b];
    for (var k = 0; k < 2; k++) {
      var poly = polys[k];
      for (var i = 0; i < poly.length; i++) {
        var p = poly[i], q = poly[(i + 1) % poly.length];
        var ax = p[1] - q[1], ay = q[0] - p[0];
        var pa = project(a, ax, ay), pb = project(b, ax, ay);
        if (pa[1] < pb[0] || pb[1] < pa[0]) return false;
      }
    }
    return true;
  }

  function pointSegment(p, a, b) {
    var dx = b[0] - a[0], dy = b[1] - a[1];
    var len2 = dx * dx + dy * dy;
    var t = len2 > 0 ? ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / len2 : 0;
    t = Math.max(0, Math.min(1, t));
    var ex = a[0] + t * dx - p[0], ey = a[1] + t * dy - p[1];
    return Math.sqrt(ex * ex + ey * ey);
  }

  // 0 when the polygons overlap, else the least vertex-to-edge distance, both ways.
  function polygonDistance(a, b) {
    if (convexOverlap(a, b)) return 0;
    var best = Infinity;
    [[a, b], [b, a]].forEach(function (pair) {
      var pts = pair[0], poly = pair[1];
      pts.forEach(function (p) {
        for (var i = 0; i < poly.length; i++) best = Math.min(best, pointSegment(p, poly[i], poly[(i + 1) % poly.length]));
      });
    });
    return best;
  }

  /* ------------------------------------------------------------------ rules */
  // Problems in order: every off_plate, then every too_tall, then every too_close pair.
  function checkPlacement(copies, parts, plate) {
    var labels = copyLabels(copies.map(function (c) { return parts[c.ref].name; }));
    var footprints = copies.map(function (c) { return parts[c.ref].footprints[c.orientation]; });
    var hulls = copies.map(function (c, i) { return copyHull(c, footprints[i]); });
    var lo = [0, 1].map(function (k) { return plate.min[k] + plate.margin - EPS_MM; });
    var hi = [0, 1].map(function (k) { return plate.max[k] - plate.margin + EPS_MM; });
    var problems = [];
    hulls.forEach(function (hull, i) {
      var off = hull.some(function (p) { return p[0] < lo[0] || p[0] > hi[0] || p[1] < lo[1] || p[1] > hi[1]; });
      if (off) problems.push({ rule: 'off_plate', copies: [i], message: labels[i] + ' is not fully on the plate.' });
    });
    footprints.forEach(function (f, i) {
      if (f.height > plate.height + EPS_MM) {
        problems.push({ rule: 'too_tall', copies: [i],
                        message: labels[i] + ' is taller than the printer can print (' + formatG(plate.height) + ' mm).' });
      }
    });
    for (var i = 0; i < hulls.length; i++) {
      for (var j = i + 1; j < hulls.length; j++) {
        if (polygonDistance(hulls[i], hulls[j]) < plate.spacing - EPS_MM) {
          problems.push({ rule: 'too_close', copies: [i, j],
                          message: labels[i] + ' and ' + labels[j] + ' are closer than ' + formatG(plate.spacing) + ' mm.' });
        }
      }
    }
    return problems;
  }

  /* ---------------------------------------------------------------- arrange */
  // Shelf rows by the turned hulls' bounding boxes, largest area first (ties by index):
  // left to right from min + margin, each box `spacing` after the last, each row `spacing`
  // above the tallest box of the row before. Boxes `spacing` apart keep hulls at least that
  // far apart. Orientation and angle are kept. A copy that fits nowhere is unplaced, set
  // beside the plate at x = max.x + spacing + w/2 and stacked in y.
  function arrangeCopies(copies, parts, plate) {
    var spacing = plate.spacing, margin = plate.margin;
    var boxes = copies.map(function (c, i) {
      var hull = copyHull({ angle: c.angle, x: 0, y: 0 }, parts[c.ref].footprints[c.orientation]);
      var xs = hull.map(function (p) { return p[0]; }), ys = hull.map(function (p) { return p[1]; });
      var minX = Math.min.apply(null, xs), minY = Math.min.apply(null, ys);
      return { i: i, minX: minX, minY: minY, w: Math.max.apply(null, xs) - minX, h: Math.max.apply(null, ys) - minY };
    });
    var order = boxes.slice().sort(function (a, b) { return (b.w * b.h - a.w * a.h) || (a.i - b.i); });
    var out = copies.map(function (c) { return Object.assign({}, c); });
    var x0 = plate.min[0] + margin, y0 = plate.min[1] + margin;
    var xMax = plate.max[0] - margin + EPS_MM, yMax = plate.max[1] - margin + EPS_MM;
    var cx = x0, cy = y0, rowH = 0, inRow = 0, sideY = y0;
    var unplaced = [];
    order.forEach(function (b) {
      var fits = cx + b.w <= xMax && cy + b.h <= yMax;
      if (!fits && inRow > 0 && x0 + b.w <= xMax && cy + rowH + spacing + b.h <= yMax) {
        cy += rowH + spacing; cx = x0; rowH = 0; inRow = 0;
        fits = true;
      }
      if (!fits) {
        unplaced.push(b.i);
        out[b.i].x = plate.max[0] + spacing - b.minX;      // the box's left edge at max.x + spacing
        out[b.i].y = sideY - b.minY;
        sideY += b.h + spacing;
        return;
      }
      out[b.i].x = cx - b.minX;
      out[b.i].y = cy - b.minY;
      cx += b.w + spacing;
      rowH = Math.max(rowH, b.h);
      inRow++;
    });
    unplaced.sort(function (a, b) { return a - b; });
    return { copies: out, unplaced: unplaced };
  }

  var api = { SPACING_MM: SPACING_MM, MARGIN_MM: MARGIN_MM, EPS_MM: EPS_MM, ORIENTATIONS: ORIENTATIONS,
              ORIENTATION_MATRICES: ORIENTATION_MATRICES, ORIENTATION_LABELS: ORIENTATION_LABELS,
              copyMatrix: copyMatrix, plateFromPrinter: plateFromPrinter, copyHull: copyHull, sanitizePartName: sanitizePartName,
              copyLabels: copyLabels, checkPlacement: checkPlacement, polygonDistance: polygonDistance,
              arrangeCopies: arrangeCopies };
  if (typeof window !== 'undefined') {
    window.PenguinCAM = window.PenguinCAM || {};
    window.PenguinCAM.plateGeometry = api;
  }
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})();

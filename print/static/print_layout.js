/* PenguinCAM 3D print page: the Layout step's plate editor (Onshape mode).
 * A top-down 2D view of the plate: every copy drawn as its footprint, drag to move,
 * click to select. No 3D view and no keyboard shortcuts (spec 4.5); Preview's 3D view
 * is where the student checks the result. The geometry and the rules come from
 * plate_geometry.js (loaded first); print_wizard.js owns the state and the controls.
 *
 * A copy is {ref, orientation, angle, x, y} plus two UI fields the slice request drops:
 * `placed` (false for a new copy parked beside the plate, waiting for Arrange or a drag)
 * and `unplaced` (true for a copy Arrange could not fit on the plate).
 * Pure helpers are exported for node tests.
 */
(function () {
  'use strict';

  var G = (typeof module !== 'undefined' && module.exports)
    ? require('./plate_geometry.js') : window.PenguinCAM.plateGeometry;

  var ONE_PLATE_MESSAGE = 'Not every copy fits on one plate; lower the quantities and print the ' +
                          'rest in a second job.';   // limits.ONE_PLATE_MESSAGE, until /print/page answers
  var PARKED_MESSAGE = 'New copies are waiting beside the plate; press Arrange or drag them on.';
  var VIEW_PAD = 10;        // CSS pixels between the drawing and the canvas edge
  var GRID_MM = 50;
  var MIN_ASPECT = 0.45, MAX_ASPECT = 1.1;   // canvas height over width, whatever the bounds
  var MAX_HEIGHT_SHARE = 0.6, MIN_CANVAS_H = 220;   // of the window's height, in CSS pixels

  /* ----------------------------------------------------------- pure helpers */
  // Fits the rectangle plate.min..plate.max into the canvas, centred, plate +y drawn upward.
  function plateView(canvasW, canvasH, plate) {
    var spanX = plate.max[0] - plate.min[0], spanY = plate.max[1] - plate.min[1];
    var scale = Math.max(1e-6, Math.min((canvasW - 2 * VIEW_PAD) / spanX, (canvasH - 2 * VIEW_PAD) / spanY));
    return { scale: scale,
             ox: (canvasW - spanX * scale) / 2 - plate.min[0] * scale,
             oy: (canvasH + spanY * scale) / 2 + plate.min[1] * scale };
  }

  function plateToCanvas(view, x, y) { return [view.ox + x * view.scale, view.oy - y * view.scale]; }
  function canvasToPlate(view, px, py) { return [(px - view.ox) / view.scale, (view.oy - py) / view.scale]; }

  function hullOf(copy, parts) { return G.copyHull(copy, parts[copy.ref].footprints[copy.orientation]); }

  // Inside or on a convex polygon of either winding.
  function insideConvex(poly, p) {
    var sign = 0;
    for (var i = 0; i < poly.length; i++) {
      var a = poly[i], b = poly[(i + 1) % poly.length];
      var cross = (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0]);
      if (cross === 0) continue;
      var s = cross > 0 ? 1 : -1;
      if (sign && s !== sign) return false;
      sign = s;
    }
    return true;
  }

  // The topmost copy (drawn last) whose footprint contains the plate point, or -1.
  function hitCopy(copies, parts, point) {
    for (var i = copies.length - 1; i >= 0; i--) {
      if (insideConvex(hullOf(copies[i], parts), point)) return i;
    }
    return -1;
  }

  function wrapAngle(angle) { return ((angle % 360) + 360) % 360; }

  function rotateCopy(copy, delta) { return Object.assign({}, copy, { angle: wrapAngle(copy.angle + delta) }); }

  // Integers only, taken mod 360; anything else leaves the copy as it was.
  function setAngle(copy, value) {
    var text = typeof value === 'number' ? String(value) : String(value == null ? '' : value).trim();
    if (!/^-?\d+$/.test(text)) return copy;
    return Object.assign({}, copy, { angle: wrapAngle(parseInt(text, 10)) });
  }

  function setOrientation(copy, orientation) {
    if (G.ORIENTATIONS.indexOf(orientation) < 0) return copy;
    return Object.assign({}, copy, { orientation: orientation });
  }

  function partsByRef(parts) {
    var out = {};
    parts.forEach(function (p) { out[p.ref] = p; });
    return out;
  }

  // `fresh` copies set beside the plate, the left edge of each box `spacing` past the
  // plate's right edge, stacked upward above anything already waiting there.
  function park(waiting, fresh, parts, plate) {
    if (!plate) return fresh;
    var right = plate.max[0], top = plate.min[1] + plate.margin;
    waiting.forEach(function (c) {
      var hull = hullOf(c, parts);
      if (hull.every(function (p) { return p[0] > right; })) {
        top = Math.max(top, Math.max.apply(null, hull.map(function (p) { return p[1]; })) + plate.spacing);
      }
    });
    return fresh.map(function (c) {
      var hull = hullOf(Object.assign({}, c, { x: 0, y: 0 }), parts);
      var xs = hull.map(function (p) { return p[0]; }), ys = hull.map(function (p) { return p[1]; });
      var minY = Math.min.apply(null, ys);
      var out = Object.assign({}, c, { x: right + plate.spacing - Math.min.apply(null, xs), y: top - minY });
      top += Math.max.apply(null, ys) - minY + plate.spacing;
      return out;
    });
  }

  // The copy at i again, unplaced, appended; parked beside the plate when plate and parts
  // (by ref) are given.
  function duplicateCopy(copies, i, plate, parts) {
    var copy = Object.assign({}, copies[i], { placed: false, unplaced: false });
    if (plate && parts) copy = park(copies, [copy], parts, plate)[0];
    return copies.concat([copy]);
  }

  function deleteCopy(copies, i) { return copies.filter(function (_c, k) { return k !== i; }); }

  // One copy per unit of quantity, per part in `parts` order. Existing copies keep their
  // place, angle and orientation (a part's first copies first); a lowered quantity drops
  // that part's last copies; a removed part drops all of its copies; new copies are parked
  // beside the plate with placed: false.
  function syncCopies(copies, parts, plate) {
    var byPart = {};
    copies.forEach(function (c) { (byPart[c.ref] = byPart[c.ref] || []).push(c); });
    var kept = [], fresh = [], slots = [];
    parts.forEach(function (p) {
      var have = byPart[p.ref] || [];
      for (var n = 0; n < p.quantity; n++) {
        if (n < have.length) { kept.push(have[n]); slots.push(have[n]); continue; }
        var c = { ref: p.ref, orientation: '+z', angle: 0, x: 0, y: 0, placed: false, unplaced: false };
        fresh.push(c);
        slots.push(c);
      }
    });
    var parked = park(kept, fresh, partsByRef(parts), plate);
    return slots.map(function (c) { var k = fresh.indexOf(c); return k < 0 ? c : parked[k]; });
  }

  // The one sentence under the canvas, or '' when Next may go on: the limits first, then a
  // copy Arrange could not fit, then copies waiting beside the plate, then the first broken
  // placement rule. `parts` (by ref) carries the triangle counts.
  function layoutSentence(problems, copies, limits, parts) {
    if (limits) {
      if (copies.length > limits.max_copies) return limits.too_many_copies_message;
      var triangles = copies.reduce(function (n, c) {
        return n + ((parts && parts[c.ref] && parts[c.ref].triangles) || 0);
      }, 0);
      if (triangles > limits.max_job_triangles) return limits.job_triangles_message;
    }
    if (copies.some(function (c) { return c.unplaced; })) return (limits && limits.one_plate_message) || ONE_PLATE_MESSAGE;
    if (copies.some(function (c) { return c.placed === false; })) return PARKED_MESSAGE;
    return problems.length ? problems[0].message : '';
  }

  // The longest form of a copy label ("<name> #<n>") that is no wider than `width` by
  // measure(text): the whole label, then the name cut short with an ellipsis, then "#<n>";
  // '' when even that does not fit. Small copies in the narrow panel keep a number at least.
  function fitLabel(label, width, measure) {
    if (measure(label) <= width) return label;
    var at = label.lastIndexOf(' #');
    if (at < 0) return '';
    var name = label.slice(0, at), number = label.slice(at + 1);
    for (var k = name.length - 1; k >= 1; k--) {
      var short = name.slice(0, k) + '\u2026 ' + number;
      if (measure(short) <= width) return short;
    }
    return measure(number) <= width ? number : '';
  }

  /* ------------------------------------------------------------------ editor */
  // The plate and every copy, so a copy waiting beside the plate is in view too.
  function viewBounds(copies, parts, plate) {
    var min = plate.min.slice(), max = plate.max.slice();
    copies.forEach(function (c) {
      hullOf(c, parts).forEach(function (p) {
        min[0] = Math.min(min[0], p[0]); min[1] = Math.min(min[1], p[1]);
        max[0] = Math.max(max[0], p[0]); max[1] = Math.max(max[1], p[1]);
      });
    });
    return { min: min, max: max };
  }

  // getState() -> {copies, parts (by ref), plate, problems}. The editor writes a moved copy
  // back into getState().copies and calls onChange('select' | 'move' | 'drop').
  function createLayoutEditor(opts) {
    var canvas = opts.canvas, getState = opts.getState, onChange = opts.onChange || function () {};
    var selected = -1, drag = null, view = null, cssW = 0, cssH = 0;

    function color(name, fallback) {
      return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;
    }

    // The canvas fills the step's width at the screen's pixel ratio; its height follows the
    // bounds' shape, so the narrow panel gets a taller canvas than a square plate alone
    // would. On a wide page the height is capped so the whole plate stays in view.
    function size(bounds) {
      var box = canvas.parentElement, w = 300, border = canvas.offsetWidth - canvas.clientWidth;
      if (box && box.clientWidth) {
        var cs = getComputedStyle(box);
        w = box.clientWidth - (parseFloat(cs.paddingLeft) || 0) - (parseFloat(cs.paddingRight) || 0) - border;
      }
      var spanX = bounds.max[0] - bounds.min[0], spanY = bounds.max[1] - bounds.min[1];
      var aspect = Math.min(MAX_ASPECT, Math.max(MIN_ASPECT, spanY / spanX));
      var maxH = Math.max(MIN_CANVAS_H, Math.round((window.innerHeight || 800) * MAX_HEIGHT_SHARE));
      cssW = Math.max(120, Math.floor(Math.min(w, maxH / aspect)));
      cssH = Math.round(cssW * aspect);
      canvas.style.width = (cssW + border) + 'px';      // border-box sizing
      canvas.style.height = (cssH + border) + 'px';
      var dpr = window.devicePixelRatio || 1;
      var bw = Math.round(cssW * dpr), bh = Math.round(cssH * dpr);
      if (canvas.width !== bw) canvas.width = bw;
      if (canvas.height !== bh) canvas.height = bh;
      return dpr;
    }

    function path(ctx, points) {
      ctx.beginPath();
      points.forEach(function (p, k) {
        var q = plateToCanvas(view, p[0], p[1]);
        if (k) ctx.lineTo(q[0], q[1]); else ctx.moveTo(q[0], q[1]);
      });
      ctx.closePath();
    }

    function draw() {
      var s = getState(), ctx = canvas.getContext('2d');
      if (!s.plate) {
        ctx.setTransform(1, 0, 0, 1, 0, 0);
        ctx.fillStyle = color('--canvas-bg', '#11161d');
        ctx.fillRect(0, 0, canvas.width, canvas.height);
        return;
      }
      var plate = s.plate, copies = s.copies;
      var bounds = viewBounds(copies, s.parts, plate);
      var dpr = drag ? (window.devicePixelRatio || 1) : size(bounds);
      if (!drag) view = plateView(cssW, cssH, bounds);    // held still while a copy is dragged
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.fillStyle = color('--canvas-bg', '#11161d');
      ctx.fillRect(0, 0, cssW, cssH);

      var grid = color('--canvas-grid', '#30363d'), ink = color('--ink', '#e6edf3');
      var x0 = plate.min[0], y0 = plate.min[1], x1 = plate.max[0], y1 = plate.max[1];
      ctx.strokeStyle = grid;
      ctx.lineWidth = 1;
      ctx.beginPath();
      for (var gx = x0 + GRID_MM; gx < x1 - 0.5; gx += GRID_MM) {
        var a = plateToCanvas(view, gx, y0), b = plateToCanvas(view, gx, y1);
        ctx.moveTo(a[0], a[1]); ctx.lineTo(b[0], b[1]);
      }
      for (var gy = y0 + GRID_MM; gy < y1 - 0.5; gy += GRID_MM) {
        var c = plateToCanvas(view, x0, gy), d = plateToCanvas(view, x1, gy);
        ctx.moveTo(c[0], c[1]); ctx.lineTo(d[0], d[1]);
      }
      ctx.stroke();
      ctx.strokeStyle = color('--muted', '#9aa7b4');
      path(ctx, [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]);
      ctx.stroke();
      // The margin every footprint must stay inside.
      var m = plate.margin;
      ctx.setLineDash([4, 4]);
      ctx.strokeStyle = grid;
      path(ctx, [[x0 + m, y0 + m], [x1 - m, y0 + m], [x1 - m, y1 - m], [x0 + m, y1 - m]]);
      ctx.stroke();
      ctx.setLineDash([]);

      var bad = {};
      (s.problems || []).forEach(function (p) { p.copies.forEach(function (i) { bad[i] = true; }); });
      var accent = color('--accent', '#2f81f7'), danger = color('--danger', '#f85149');
      var labels = G.copyLabels(copies.map(function (cp) { return s.parts[cp.ref].name; }));
      ctx.font = '11px sans-serif';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      copies.forEach(function (cp, i) {
        var hull = hullOf(cp, s.parts);
        var hue = bad[i] || cp.placed === false || cp.unplaced ? danger : accent;
        path(ctx, hull);
        ctx.fillStyle = hue;
        ctx.globalAlpha = 0.35;
        ctx.fill();
        ctx.globalAlpha = 1;
        ctx.strokeStyle = i === selected ? ink : hue;
        ctx.lineWidth = i === selected ? 2.5 : 1.5;
        ctx.stroke();
        var centre = plateToCanvas(view, cp.x, cp.y);
        var xs = hull.map(function (p) { return p[0]; });
        var width = (Math.max.apply(null, xs) - Math.min.apply(null, xs)) * view.scale;
        var text = fitLabel(labels[i], width - 4, function (t) { return ctx.measureText(t).width; });
        if (text) {
          ctx.fillStyle = ink;
          ctx.fillText(text, centre[0], centre[1]);
        }
      });
      ctx.textAlign = 'left';
      ctx.textBaseline = 'alphabetic';
      ctx.lineWidth = 1;
    }

    // The pointer in plate millimetres; the scale guards against a width change since the
    // last draw.
    function platePoint(e) {
      var r = canvas.getBoundingClientRect(), k = cssW / (canvas.clientWidth || cssW);
      return canvasToPlate(view, (e.clientX - r.left - canvas.clientLeft) * k, (e.clientY - r.top - canvas.clientTop) * k);
    }

    // A scrollbar appearing narrows the step without a window resize: redraw at the new width.
    var boxWidth = 0;
    if (typeof ResizeObserver !== 'undefined' && canvas.parentElement) {
      new ResizeObserver(function () {
        var now = canvas.parentElement.clientWidth;
        if (now === boxWidth) return;
        boxWidth = now;
        if (!drag && now && getState().plate) draw();
      }).observe(canvas.parentElement);
    }

    function select(i) {
      selected = i;
      draw();
      onChange('select');
    }

    canvas.addEventListener('pointerdown', function (e) {
      var s = getState();
      if (!view || !s.plate) return;
      var p = platePoint(e), i = hitCopy(s.copies, s.parts, p);
      select(i);
      if (i < 0) return;
      drag = { i: i, dx: s.copies[i].x - p[0], dy: s.copies[i].y - p[1], moved: false };
      try { canvas.setPointerCapture(e.pointerId); } catch (err) { /* synthetic events */ }
      e.preventDefault();
    });
    canvas.addEventListener('pointermove', function (e) {
      if (!drag) return;
      var s = getState(), p = platePoint(e);
      s.copies[drag.i] = Object.assign({}, s.copies[drag.i], { x: p[0] + drag.dx, y: p[1] + drag.dy,
                                                             placed: true, unplaced: false });
      drag.moved = true;
      onChange('move');
    });
    function endDrag() {
      if (!drag) return;
      var moved = drag.moved;
      drag = null;
      if (moved) onChange('drop'); else draw();
    }
    canvas.addEventListener('pointerup', endDrag);
    canvas.addEventListener('pointercancel', endDrag);

    return {
      draw: draw,
      select: select,
      selected: function () { return selected; },
    };
  }

  var api = { ONE_PLATE_MESSAGE: ONE_PLATE_MESSAGE, PARKED_MESSAGE: PARKED_MESSAGE,
              plateView: plateView, plateToCanvas: plateToCanvas, canvasToPlate: canvasToPlate,
              hitCopy: hitCopy, rotateCopy: rotateCopy, setAngle: setAngle, setOrientation: setOrientation,
              duplicateCopy: duplicateCopy, deleteCopy: deleteCopy, syncCopies: syncCopies,
              layoutSentence: layoutSentence, fitLabel: fitLabel, createLayoutEditor: createLayoutEditor };
  if (typeof window !== 'undefined') {
    window.PenguinCAM = window.PenguinCAM || {};
    window.PenguinCAM.printLayout = api;
  }
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})();

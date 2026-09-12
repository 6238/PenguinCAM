/* PenguinCAM multi-part wizard.
 * One codebase for the standalone (/app, source=upload) and embedded
 * (/onshape-panel, source=onshape) contexts; the part source is the only
 * difference and is selected via window.PenguinCAM.source.
 */
(function () {
  'use strict';

  var CFG = window.PenguinCAM || { source: 'upload', bed: { width: 24, height: 24 }, defaultTool: 0.157, defaultToolText: '4mm', machines: {} };
  var DEBUG = /(?:^|[?&])debug=1(?:&|$)/.test(location.search);

  var ALL_STEPS = ['setup', 'parts', 'layout', 'preview'];
  // Every mode uses the Layout step. In 2D it nests parts on a sheet; in tubing it's
  // used only to orient the face(s) to the tube-jig axis (see the tube handling in the
  // rotate + validate paths below). 2.5D is a single part positioned at the origin.
  function steps() {
    return ALL_STEPS;
  }

  var state = {
    source: CFG.source,
    step: 'setup',
    mode: '2d',
    machine_id: null,
    material: 'plywood',
    tool_diameter: parseFloat(CFG.defaultTool) || 0.157,
    tool_diameter_text: CFG.defaultToolText || '4mm',  // user's raw input, shown verbatim (e.g. "4mm")
    thickness: 0.25,
    thickness_text: '0.25"',
    tab_spacing: 6.0,
    // Tubing-only settings.
    tubeHeight: 1.0,
    tubeHeight_text: '1"',
    squareEnd: false,
    cutToLength: false,
    // The machine envelope is a read-only constraint; the parts' combined bounding box
    // is the stock (G54 origin = its lower-left).
    machine: { width: CFG.bed.width || 24, height: CFG.bed.height || 24, name: CFG.machineName || 'Machine' },
    parts: [],            // {id,name,width,height,outline,holes,inner,file,cx,cy,rotation,flipped}
    selectedIds: [],
    zoom: 1,
    saveAction: 'download',   // 'download' | 'drive' (final-step action)
    lastResponse: null,
  };
  var partSeq = 0;
  var debugEvents = [];
  var viewer = null;

  /* ----------------------------------------------------------------- utils */
  function $(sel, root) { return (root || document).querySelector(sel); }
  function $all(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }
  function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }

  // Parse an Onshape-style length: a number with an optional unit; no unit means inches
  // (this app is inch-native). Returns the value in inches, or null if unparseable.
  // Inspired by frcdesign/FRCDesignApp's input-parser (github.com/frcdesign/FRCDesignApp).
  var LENGTH_TO_INCH = {
    '': 1, 'in': 1, 'inch': 1, 'inches': 1, '"': 1,
    'mm': 1 / 25.4, 'millimeter': 1 / 25.4, 'millimeters': 1 / 25.4,
    'cm': 1 / 2.54, 'centimeter': 1 / 2.54, 'centimeters': 1 / 2.54,
    'm': 1 / 0.0254, 'meter': 1 / 0.0254, 'meters': 1 / 0.0254,
    'ft': 12, 'foot': 12, 'feet': 12, "'": 12,
    'yd': 36, 'yard': 36, 'yards': 36,
  };
  function parseLength(text) {
    if (text == null) return null;
    var s = String(text).trim().toLowerCase();
    var value, rest;
    var frac = s.match(/^([+-]?\d+)\s*\/\s*(\d+)\s*(.*)$/);   // fractions (e.g. 1/8", common for SAE tools)
    if (frac) {
      var den = parseFloat(frac[2]);
      if (!den) return null;
      value = parseFloat(frac[1]) / den; rest = frac[3];
    } else {
      var dec = s.match(/^([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*(.*)$/);
      if (!dec) return null;
      value = parseFloat(dec[1]); rest = dec[2];
    }
    var factor = LENGTH_TO_INCH[rest.trim()];
    if (!isFinite(value) || factor == null) return null;
    var inches = value * factor;
    return inches > 0 ? inches : null;
  }

  // Wire a text input as an Onshape-style length field. Parses units live; on commit a
  // bare number/fraction gets the implied inch mark (never converts between unit systems,
  // so "4mm" stays "4mm"); invalid input is flagged and reverted on blur. onValid(inches,
  // text) is called with each accepted value; getText() returns the last valid text (for
  // revert). Shared by the tool-diameter and thickness fields.
  function bindLengthField(input, getText, onValid) {
    if (!input) return;
    function commit(raw) {
      raw = raw.trim();
      if (/^[+-]?[0-9.\/\s]+$/.test(raw)) raw += '"';   // bare number/fraction -> implied inch mark
      onValid(parseLength(raw), raw);
      input.value = raw;
    }
    if (parseLength(input.value)) commit(input.value);   // adopt the rendered default/config value
    input.addEventListener('input', function () {
      var inches = parseLength(this.value);
      if (inches) { onValid(inches, this.value.trim()); this.classList.remove('invalid'); }
      else { this.classList.add('invalid'); }            // keep last valid value; flag bad text
    });
    input.addEventListener('change', function () {
      this.classList.remove('invalid');
      if (parseLength(this.value)) commit(this.value);
      else { this.value = getText(); }                   // revert unparseable input on blur
    });
  }

  // Apply light/dark theme (tied to Onshape's theme). The server sets it on <html> at
  // render; this handles live changes if Onshape posts a theme update while open.
  function applyTheme(theme) {
    if (theme !== 'light' && theme !== 'dark') return;
    document.documentElement.setAttribute('data-theme', theme);
    if (viewer && viewer.setTheme) viewer.setTheme(theme);
    if (state.step === 'layout') drawLayout();
    dbg('theme', theme);
  }

  function dbg(label, data) {
    debugEvents.unshift({ t: new Date().toISOString().slice(11, 19), label: label, data: data });
    debugEvents = debugEvents.slice(0, 12);
    renderDebug();
  }

  function renderDebug() {
    if (!DEBUG) return;
    var el = $('#debug-content');
    if (!el) return;
    var snapshot = {
      source: state.source, step: state.step, mode: state.mode,
      tool: state.tool_diameter, machine: state.machine,
      parts: state.parts.map(function (p) {
        return { name: p.name, w: p.width, h: p.height, cx: p.cx, cy: p.cy, rot: p.rotation };
      }),
    };
    el.textContent = JSON.stringify(snapshot, null, 1) + '\n--- events ---\n' +
      debugEvents.map(function (e) { return e.t + ' ' + e.label + ' ' + (e.data != null ? JSON.stringify(e.data) : ''); }).join('\n');
  }

  function timestamp() {
    var d = new Date();
    function p(n) { return String(n).padStart(2, '0'); }
    return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' +
      p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
  }

  /* -------------------------------------------------------------- geometry */
  function rotatePoint(x, y, deg) {
    // Match the server: transform_coordinates rotates clockwise (angle_rad = -radians).
    var a = -deg * Math.PI / 180;
    return [x * Math.cos(a) - y * Math.sin(a), x * Math.sin(a) + y * Math.cos(a)];
  }

  // Returns the part's placed footprint: outline points normalized so the rotated
  // bounding-box minimum is (0,0), plus that footprint's width/height. Mirrors the
  // server pinning the rotated bbox-min to placement_offset.
  function placedShape(part) {
    var fx = part.flipped ? -1 : 1;   // horizontal flip (mirror across X) before rotating
    var pts = part.outline.map(function (pt) { return rotatePoint(fx * pt[0], pt[1], part.rotation); });
    var minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    pts.forEach(function (pt) {
      if (pt[0] < minX) minX = pt[0]; if (pt[0] > maxX) maxX = pt[0];
      if (pt[1] < minY) minY = pt[1]; if (pt[1] > maxY) maxY = pt[1];
    });
    var norm = pts.map(function (pt) { return [pt[0] - minX, pt[1] - minY]; });
    var holes = (part.holes || []).map(function (h) {
      var c = rotatePoint(fx * h.cx, h.cy, part.rotation);
      return { cx: c[0] - minX, cy: c[1] - minY, r: h.r };
    });
    // Internal feature rings (2.5D pockets/steps) ride the same flip+rotate+normalize.
    var inner = (part.inner || []).map(function (ring) {
      return ring.map(function (pt) {
        var c = rotatePoint(fx * pt[0], pt[1], part.rotation);
        return [c[0] - minX, c[1] - minY];
      });
    });
    return { pts: norm, holes: holes, inner: inner, w: maxX - minX, h: maxY - minY };
  }

  // Parts are stored by their center (cx, cy) so rotation happens in place. The
  // placement is the derived axis-aligned footprint whose bbox-min the server pins to
  // place_x/place_y.
  function placement(part) {
    var s = placedShape(part);
    return { x: part.cx - s.w / 2, y: part.cy - s.h / 2, w: s.w, h: s.h, shape: s };
  }

  function footprint(part) {
    var p = placement(part);
    return { minX: p.x, minY: p.y, maxX: p.x + p.w, maxY: p.y + p.h, shape: p.shape };
  }

  // The placed perimeter polygon in sheet coordinates (mirror of placed_polygon()).
  function placedPolygon(part) {
    var p = placement(part);
    return p.shape.pts.map(function (pt) { return [p.x + pt[0], p.y + pt[1]]; });
  }

  function segPointDist(px, py, ax, ay, bx, by) {
    var dx = bx - ax, dy = by - ay, len2 = dx * dx + dy * dy;
    if (len2 === 0) return Math.hypot(px - ax, py - ay);
    var t = ((px - ax) * dx + (py - ay) * dy) / len2;
    t = Math.max(0, Math.min(1, t));
    return Math.hypot(px - (ax + t * dx), py - (ay + t * dy));
  }
  function segsIntersect(a, b, c, d) {
    function ccw(p, q, r) { return (r[1] - p[1]) * (q[0] - p[0]) > (q[1] - p[1]) * (r[0] - p[0]); }
    return ccw(a, c, d) !== ccw(b, c, d) && ccw(a, b, c) !== ccw(a, b, d);
  }
  function pointInPoly(pt, poly) {
    var x = pt[0], y = pt[1], inside = false;
    for (var i = 0, j = poly.length - 1; i < poly.length; j = i++) {
      var xi = poly[i][0], yi = poly[i][1], xj = poly[j][0], yj = poly[j][1];
      if (((yi > y) !== (yj > y)) && (x < (xj - xi) * (y - yi) / (yj - yi) + xi)) inside = !inside;
    }
    return inside;
  }
  // Minimum distance between two polygon outlines (0 if they intersect or one
  // contains the other). Used for the kerf-gap proximity test.
  function polyMinDist(A, B) {
    if (pointInPoly(A[0], B) || pointInPoly(B[0], A)) return 0;
    var min = Infinity;
    for (var i = 0; i < A.length; i++) {
      var a1 = A[i], a2 = A[(i + 1) % A.length];
      for (var j = 0; j < B.length; j++) {
        var b1 = B[j], b2 = B[(j + 1) % B.length];
        if (segsIntersect(a1, a2, b1, b2)) return 0;
        var d = Math.min(
          segPointDist(a1[0], a1[1], b1[0], b1[1], b2[0], b2[1]),
          segPointDist(a2[0], a2[1], b1[0], b1[1], b2[0], b2[1]),
          segPointDist(b1[0], b1[1], a1[0], a1[1], a2[0], a2[1]),
          segPointDist(b2[0], b2[1], a1[0], a1[1], a2[0], a2[1])
        );
        if (d < min) min = d;
      }
    }
    return min;
  }

  // Combined footprint of a set of parts (all parts by default). This is the stock;
  // its lower-left is the G54 origin. Returns null when empty.
  function combinedBBox(parts) {
    parts = parts || state.parts;
    if (!parts.length) return null;
    var minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    parts.forEach(function (p) {
      var f = footprint(p);
      if (f.minX < minX) minX = f.minX; if (f.minY < minY) minY = f.minY;
      if (f.maxX > maxX) maxX = f.maxX; if (f.maxY > maxY) maxY = f.maxY;
    });
    return { minX: minX, minY: minY, maxX: maxX, maxY: maxY, w: maxX - minX, h: maxY - minY };
  }

  function isSelected(id) { return state.selectedIds.indexOf(id) >= 0; }
  function selectedParts() { return state.parts.filter(function (p) { return isSelected(p.id); }); }

  // Validate the layout: the combined bounding box (the stock) must fit the machine,
  // and no two parts may overlap or sit closer than one kerf. Real-geometry overlap so
  // a part nesting into another's concave region isn't a false positive.
  function validateLayout() {
    // Tubing isn't nested on a sheet: the two faces are opposite walls of one tube, so
    // the machine-bounds and part-overlap checks don't apply. The Layout step exists
    // only to orient the tube to the jig axis.
    if (state.mode === 'tubing') return { bad: {}, msgs: [], tooBig: false, bbox: combinedBBox() };
    var msgs = [];
    var bad = {};
    var gap = state.tool_diameter;
    var bbox = combinedBBox();
    var tooBig = false;
    if (bbox && (bbox.w > state.machine.width + 1e-6 || bbox.h > state.machine.height + 1e-6)) {
      tooBig = true;
      msgs.push('Parts (' + bbox.w.toFixed(2) + '" x ' + bbox.h.toFixed(2) + '") exceed the machine (' +
                state.machine.width + '" x ' + state.machine.height + '").');
    }
    var items = state.parts.map(function (p) { return { id: p.id, name: p.name, box: footprint(p), poly: placedPolygon(p) }; });
    for (var i = 0; i < items.length; i++) {
      for (var j = i + 1; j < items.length; j++) {
        var a = items[i].box, c = items[j].box;
        var clearX = (a.maxX + gap <= c.minX + 1e-6) || (c.maxX + gap <= a.minX + 1e-6);
        var clearY = (a.maxY + gap <= c.minY + 1e-6) || (c.maxY + gap <= a.minY + 1e-6);
        if (clearX || clearY) continue;
        if (polyMinDist(items[i].poly, items[j].poly) < gap - 1e-6) {
          bad[items[i].id] = true; bad[items[j].id] = true;
          msgs.push(items[i].name + ' and ' + items[j].name + ' overlap or are too close.');
        }
      }
    }
    return { bad: bad, msgs: msgs, tooBig: tooBig, bbox: bbox };
  }

  // Running summary of decisions, revealed progressively: material/thickness/tool from
  // Setup (shown on Parts+), part count (Layout+), stock bbox size (Preview). Lets the
  // user confirm at the end what they chose without navigating back.
  function updateSummary() {
    var box = $('#wiz-summary');
    if (!box) return;
    var list = steps();
    var idx = list.indexOf(state.step);
    if (idx < list.indexOf('parts')) { box.hidden = true; return; }
    box.hidden = false;

    var chips = [];
    if (state.mode === 'tubing') {
      chips.push('Aluminum Tube');
      chips.push(state.thickness_text + ' wall');
      chips.push('tube ' + state.tubeHeight_text + ' tall');
    } else {
      var msel = $('#f-material');
      chips.push(msel && msel.options[msel.selectedIndex] ? msel.options[msel.selectedIndex].text : state.material);
      chips.push(state.mode === '2.5d' ? '2.5D · thickness from CAD' : (state.thickness_text + ' thick'));
    }
    chips.push('⌀ ' + state.tool_diameter_text + ' tool');
    if (state.mode === 'tubing') {
      chips.push(state.parts.length + ' face' + (state.parts.length === 1 ? '' : 's'));
    } else {
      if (idx >= list.indexOf('layout')) {
        chips.push(state.parts.length + ' part' + (state.parts.length === 1 ? '' : 's'));
      }
      if (idx >= list.indexOf('preview')) {
        var st = state.lastResponse && state.lastResponse.stock;
        var w = st ? st.width : null, h = st ? st.height : null;
        if (w == null) { var bb = combinedBBox(); if (bb) { w = bb.w; h = bb.h; } }
        if (w != null) chips.push('stock ' + (+w).toFixed(2) + ' x ' + (+h).toFixed(2) + '"');
      }
    }

    box.innerHTML = '';
    chips.forEach(function (c) {
      var s = document.createElement('span');
      s.className = 'chip';
      s.textContent = c;
      box.appendChild(s);
    });
  }

  /* ------------------------------------------------------------ step nav */
  // Number and show only the steps in the current (mode-dependent) sequence. Numbers
  // reflect sequence position, so they don't renumber as you advance or complete steps.
  function renderStepbar() {
    var list = steps();
    $all('#stepbar li').forEach(function (li) {
      var i = list.indexOf(li.getAttribute('data-step'));
      if (i < 0) { li.hidden = true; }
      else { li.hidden = false; li.setAttribute('data-num', i + 1); }
    });
  }

  function gotoStep(name) {
    state.step = name;
    renderStepbar();
    // Full-page grid mode shows all four steps at once (2x2) and just HIGHLIGHTS
    // the current one; the narrow single-step mode (Onshape panel) hides the rest.
    // Either way the per-step side effects below still fire on transition, so a
    // quadrant stays "incomplete until you get there" (e.g. Preview generates only
    // when you actually reach it).
    var gridMode = $('#wizard').classList.contains('grid');
    $all('.step').forEach(function (s) {
      var isCurrent = s.getAttribute('data-step') === name;
      if (gridMode) { s.hidden = false; s.classList.toggle('current', isCurrent); }
      else { s.hidden = !isCurrent; s.classList.remove('current'); }
    });
    var order = steps();
    $all('#stepbar li').forEach(function (li) {
      var s = li.getAttribute('data-step');
      li.classList.toggle('active', s === name);
      li.classList.toggle('done', order.indexOf(s) >= 0 && order.indexOf(s) < order.indexOf(name));
    });
    var idx = order.indexOf(name);
    $('#btn-back').disabled = idx === 0;
    var nextBtn = $('#btn-next');
    var isPreview = name === 'preview';
    nextBtn.hidden = isPreview;
    $('#final-action').hidden = !isPreview;
    if (name === 'layout') {
      updateLayoutInfo();
      // Tubing shares one orientation across both faces; select them together so the
      // rotation handle drives the whole tube at once, and keep them packed tidily.
      if (state.mode === 'tubing') {
        restackTubeParts();
        state.selectedIds = state.parts.map(function (p) { return p.id; });
      }
      updateLayoutHint();
      resetHandleDir();
      refitView();
      drawLayout();
    }
    if (isPreview) {
      state.saveAction = preferredAction();
      $('#btn-do').disabled = true;   // enabled once generation finishes
      setupFinalAction();
      resetPreview();
      generate();                     // auto-generate on entry
    }
    // Keep Onshape in continuous face-selection mode only while on the Parts step.
    if (state.source === 'onshape' && window.PenguinCAM.startFaceSelection) {
      if (name === 'parts') {
        var el = $('#select-status'); if (el) el.textContent = 'Select a face in Onshape…';
        window.PenguinCAM.startFaceSelection();
      } else {
        window.PenguinCAM.stopFaceSelection();
      }
    }
    updateSummary();
    dbg('step', name);
  }

  function canLeave(name) {
    if (name === 'parts' && state.parts.length === 0) {
      alert('Add at least one part before continuing.');
      return false;
    }
    if (name === 'layout') {
      var v = validateLayout();
      if (v.msgs.length) {
        alert('Fix the layout first:\n' + v.msgs.join('\n'));
        return false;
      }
    }
    return true;
  }

  // Jump to a step via the stepbar. Backward is always allowed; forward must clear the
  // same gates as pressing Next through each intervening step.
  function navigateTo(name) {
    var order = steps();
    var target = order.indexOf(name), cur = order.indexOf(state.step);
    if (target < 0 || target === cur) return;
    if (target > cur) {
      for (var i = cur; i < target; i++) { if (!canLeave(order[i])) return; }
    }
    gotoStep(name);
  }

  /* --------------------------------------------------------------- setup */
  function bindSetup() {
    var machineSel = $('#f-machine');
    if (machineSel) {
      state.machine_id = machineSel.value;
      machineSel.addEventListener('change', function () { selectMachine(this.value); });
    }

    $all('input[name="mode"]').forEach(function (r) {
      r.addEventListener('change', function () {
        state.mode = this.value;
        applyModeUI();
        dbg('mode', state.mode);
      });
    });

    bindLengthField($('#f-tool'),
      function () { return state.tool_diameter_text; },
      function (inches, text) { state.tool_diameter = inches; state.tool_diameter_text = text; });
    bindLengthField($('#f-thickness'),
      function () { return state.thickness_text; },
      function (inches, text) { state.thickness = inches; state.thickness_text = text; });
    $('#f-material').addEventListener('change', function () { if (state.mode !== 'tubing') state.material = this.value; });
    bindLengthField($('#f-tube-height'),
      function () { return state.tubeHeight_text; },
      function (inches, text) { state.tubeHeight = inches; state.tubeHeight_text = text; });
    $('#f-square-end').addEventListener('change', function () { state.squareEnd = this.checked; });
    $('#f-cut-to-length').addEventListener('change', function () { state.cutToLength = this.checked; });
    applyModeUI();
  }

  // Switch the active machine: pull its bed size, name, tool default, and material list
  // from the per-machine data the server embedded (CFG.machines) so the Layout bounds,
  // tool default, and Material dropdown reflect the SELECTED machine, not the default.
  // Also persists the choice to the session so a reload keeps it.
  function selectMachine(mid) {
    var oldInfo = (CFG.machines || {})[state.machine_id];
    state.machine_id = mid;
    var info = (CFG.machines || {})[mid];
    if (info) {
      state.machine = { width: info.x_max || state.machine.width, height: info.y_max || state.machine.height, name: info.name || mid };
      rebuildMaterialOptions(info.materials);
      // Follow the new machine's default tool ONLY if the field still holds the previous
      // machine's default (i.e. the user hasn't typed a custom value) — never clobber input.
      var toolInput = $('#f-tool');
      if (toolInput && info.tool_text && oldInfo && state.tool_diameter_text === oldInfo.tool_text) {
        toolInput.value = info.tool_text;
        state.tool_diameter_text = info.tool_text;
        state.tool_diameter = info.tool || parseLength(info.tool_text) || state.tool_diameter;
      }
      if (state.step === 'layout') { updateLayoutInfo(); refitView(); drawLayout(); }
      updateSummary();
    }
    // Persist to the session (fire-and-forget) so a page/iframe reload keeps this machine.
    fetch('/set-machine', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ machine_id: mid }),
    }).catch(function () {});
    dbg('machine', mid);
  }

  // Rebuild the Material <select> for the selected machine, keeping the current material
  // if it still exists (else preferring plywood, then the first available).
  function rebuildMaterialOptions(materials) {
    var sel = $('#f-material');
    if (!sel || !materials || !materials.length) return;
    var prev = state.material;
    sel.innerHTML = '';
    var ids = [];
    materials.forEach(function (m) {
      var opt = document.createElement('option');
      opt.value = m.id; opt.textContent = m.name;
      sel.appendChild(opt);
      ids.push(m.id);
    });
    var pick = ids.indexOf(prev) >= 0 ? prev : (ids.indexOf('plywood') >= 0 ? 'plywood' : ids[0]);
    sel.value = pick;
    if (state.mode !== 'tubing') state.material = pick;
  }

  // Reshape the Setup form for the selected mode. Tubing forces the aluminum-tube
  // material (hiding the selector), relabels thickness as wall thickness, and reveals
  // the tube-only fields; 2.5D hides thickness (derived from CAD).
  function applyModeUI() {
    var is25 = state.mode === '2.5d';
    var isTube = state.mode === 'tubing';
    $('#thickness-field').style.display = is25 ? 'none' : '';
    $('#thickness-derived').style.display = is25 ? '' : 'none';
    var tl = $('#thickness-label'); if (tl) tl.textContent = isTube ? 'Tube wall thickness (in or mm)' : 'Material thickness (in or mm)';
    var mf = $('#material-field'); if (mf) mf.style.display = isTube ? 'none' : '';
    if (isTube) {
      state.material = 'aluminum_tube';
    } else {
      var msel = $('#f-material'); if (msel) state.material = msel.value;
    }
    var tf = $('#tube-fields'); if (tf) tf.hidden = !isTube;
    renderStepbar();
    updatePartsModeNote();
  }

  function updatePartsModeNote() {
    var note = $('#parts-mode-note');
    if (!note) return;
    if (state.mode === '2.5d') {
      note.textContent = '2.5D mode: one part per job (thickness comes from the CAD layers).';
    } else if (state.mode === 'tubing') {
      note.textContent = 'Tubing: add 1 face (mirrored onto the opposite side) or 2 faces (a distinct pattern per side).';
    } else {
      note.textContent = 'Add as many parts as fit on the sheet.';
    }
  }

  /* --------------------------------------------------------------- parts */
  function thumbnailSVG(part) {
    var W = 44, H = 44, pad = 4;
    var scale = Math.min((W - 2 * pad) / (part.width || 1), (H - 2 * pad) / (part.height || 1));
    function map(x, y) { return [pad + x * scale, H - pad - y * scale]; }
    function ringPath(ring) { return ring.map(function (pt, i) { var m = map(pt[0], pt[1]); return (i ? 'L' : 'M') + m[0].toFixed(1) + ' ' + m[1].toFixed(1); }).join(' ') + ' Z'; }
    var d = ringPath(part.outline);
    var holes = (part.holes || []).map(function (h) { var m = map(h.cx, h.cy); return '<circle cx="' + m[0].toFixed(1) + '" cy="' + m[1].toFixed(1) + '" r="' + Math.max(1, h.r * scale).toFixed(1) + '" fill="none" stroke="#9aa7b4"/>'; }).join('');
    var inner = (part.inner || []).map(function (ring) { return '<path d="' + ringPath(ring) + '" fill="none" stroke="#9aa7b4" stroke-width="1"/>'; }).join('');
    return '<svg width="' + W + '" height="' + H + '" viewBox="0 0 ' + W + ' ' + H + '"><path d="' + d + '" fill="none" stroke="#2f81f7" stroke-width="1.5"/>' + inner + holes + '</svg>';
  }

  function renderParts() {
    var ul = $('#parts-list');
    ul.innerHTML = '';
    state.parts.forEach(function (p) {
      var li = document.createElement('li');
      li.className = 'part-item';
      li.innerHTML = thumbnailSVG(p) +
        '<div class="meta"><div class="name"></div><div class="dims">' +
        p.width.toFixed(2) + '" x ' + p.height.toFixed(2) + '"</div></div>' +
        '<button class="remove" title="Remove" aria-label="Remove">&times;</button>';
      li.querySelector('.name').textContent = p.name;
      li.querySelector('.remove').addEventListener('click', function () { removePart(p.id); });
      ul.appendChild(li);
    });
    renderDebug();
  }

  function addPartFromOutline(data, file) {
    if (state.mode === '2.5d' && state.parts.length >= 1) {
      alert('2.5D mode allows only one part. Remove the current part first, or switch to 2D mode.');
      return;
    }
    if (state.mode === 'tubing' && state.parts.length >= 2) {
      alert('Tubing allows at most two faces (one per side). Remove a face first.');
      return;
    }
    // Adopt the CAD-discovered stock thickness when the server found one.
    //  - 2.5D: the field is hidden and thickness is authoritative (G-code re-derives it
    //    from the DXF regardless); always adopt.
    //  - 2D: seed the still-editable field from the first part's designed height so the
    //    user starts from the real value and can change it. Only the first part seeds it,
    //    so re-importing later parts won't clobber a value the user may have adjusted.
    if (data.thickness && (state.mode === '2.5d' || state.parts.length === 0)) {
      state.thickness = data.thickness;
      state.thickness_text = (Math.round(data.thickness * 10000) / 10000) + '"';   // shown in inches
      var tin = $('#f-thickness'); if (tin) tin.value = state.thickness_text;       // reflect in the editable 2D field
    }
    var p = {
      id: ++partSeq,
      name: data.name || ('part ' + (partSeq)),
      width: data.width, height: data.height,
      outline: data.outline, holes: data.holes || [], inner: data.inner || [],
      file: file,
      cx: 0, cy: 0, rotation: 0, flipped: false,
    };
    // Initial placement: bottom edge on Y=0, stacked to the right of existing parts.
    var s = placedShape(p);
    var startX = 0;
    state.parts.forEach(function (q) { startX = Math.max(startX, footprint(q).maxX + state.tool_diameter); });
    p.cx = startX + s.w / 2;
    p.cy = s.h / 2;
    state.parts.push(p);
    renderParts();
    dbg('part-added', { name: p.name, w: p.width, h: p.height });
  }

  function removePart(id) {
    state.parts = state.parts.filter(function (p) { return p.id !== id; });
    state.selectedIds = state.selectedIds.filter(function (sid) { return sid !== id; });
    renderParts();
    if (state.step === 'layout') drawLayout();
  }

  function uploadDxf(file) {
    if (!file || !/\.dxf$/i.test(file.name)) { alert('Please choose a .dxf file.'); return; }
    var fd = new FormData();
    fd.append('file', file);
    dbg('part-outline:req', file.name);
    fetch('/part-outline', { method: 'POST', body: fd })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (res) {
        if (!res.ok || !res.j.success) { dbg('part-outline:err', res.j.error); alert('Could not read DXF: ' + (res.j.error || 'unknown error')); return; }
        dbg('part-outline:ok', { name: res.j.name, w: res.j.width, h: res.j.height });
        addPartFromOutline(res.j, file);
      })
      .catch(function (e) { dbg('part-outline:fail', String(e)); alert('Upload failed: ' + e); });
  }

  function bindParts() {
    if (state.source === 'onshape') {
      $('#upload-source').hidden = true;
      $('#onshape-source').hidden = false;
      // Face selection is continuous while on the Parts step (armed by gotoStep); the
      // #select-status label just reflects state — no button.
      function setSel(msg) { var el = $('#select-status'); if (el) el.textContent = msg; }
      window.PenguinCAM.onPart = function (data, fileOrBlob) {
        var file = fileOrBlob instanceof File ? fileOrBlob : new File([fileOrBlob], (data.name || 'part') + '.dxf');
        addPartFromOutline(data, file);
      };
      window.PenguinCAM.onSelectionBusy = function (busy) {
        setSel(busy ? 'Importing face, please wait…' : 'Select a face in Onshape…');
      };
      window.PenguinCAM.onSelectionError = function (msg) {
        dbg('onshape:error', msg);
        // Strip any trailing sentence punctuation the server message already carries
        // so we don't render a double period before the appended instruction.
        var clean = String(msg == null ? '' : msg).replace(/[.!?\s]+$/, '');
        setSel('Import failed: ' + clean + '. Select a face to try again.');
      };
    } else {
      var dz = $('#dropzone'), input = $('#f-dxf');
      dz.addEventListener('click', function () { input.click(); });
      dz.addEventListener('keydown', function (e) { if (e.key === 'Enter' || e.key === ' ') input.click(); });
      input.addEventListener('change', function () { if (this.files[0]) uploadDxf(this.files[0]); this.value = ''; });
      ['dragover', 'dragenter'].forEach(function (ev) { dz.addEventListener(ev, function (e) { e.preventDefault(); dz.classList.add('drag'); }); });
      ['dragleave', 'drop'].forEach(function (ev) { dz.addEventListener(ev, function (e) { e.preventDefault(); dz.classList.remove('drag'); }); });
      dz.addEventListener('drop', function (e) { if (e.dataTransfer.files[0]) uploadDxf(e.dataTransfer.files[0]); });
    }
    updatePartsModeNote();
  }

  /* -------------------------------------------------------------- layout */
  var canvasState = { scale: 1, wcx: 0, wcy: 0, ccx: 0, ccy: 0, action: null, handleDir: [0, 1] };

  // Tube faces aren't positioned by the user (the backend ignores placement for tubes),
  // so keep them tidy: left-to-right, bottom-aligned at Y=0, one kerf apart, using each
  // face's current (rotated) footprint. Called after a tube rotation so the faces don't
  // drift apart when they change orientation. Same stacking as addPartFromOutline.
  function restackTubeParts() {
    var x = 0;
    state.parts.forEach(function (p) {
      var s = placedShape(p);
      p.cx = x + s.w / 2;
      p.cy = s.h / 2;
      x += s.w + state.tool_diameter;
    });
  }

  // Fit the parts' combined bounding box to ~80% of the canvas (times zoom), centered.
  // Called only on explicit events (entering Layout, zoom) — NOT every frame, so the
  // view stays put while dragging/rotating and part motion is actually visible.
  function refitView() {
    var canvas = $('#layout-canvas');
    if (!canvas) return;
    var bb = combinedBBox();
    var w = bb ? Math.max(bb.w, 0.001) : 10;
    var h = bb ? Math.max(bb.h, 0.001) : 10;
    canvasState.wcx = bb ? (bb.minX + bb.maxX) / 2 : 0;
    canvasState.wcy = bb ? (bb.minY + bb.maxY) / 2 : 0;
    canvasState.ccx = canvas.width / 2;
    canvasState.ccy = canvas.height / 2;
    canvasState.scale = Math.min(canvas.width / w, canvas.height / h) * 0.8 * state.zoom;
  }
  function worldToCanvas(x, y) {
    return [canvasState.ccx + (x - canvasState.wcx) * canvasState.scale,
            canvasState.ccy - (y - canvasState.wcy) * canvasState.scale];
  }
  function canvasToWorld(cx, cy) {
    return [canvasState.wcx + (cx - canvasState.ccx) / canvasState.scale,
            canvasState.wcy - (cy - canvasState.ccy) / canvasState.scale];
  }

  // The rotation handle orbits the selection center along canvasState.handleDir (a
  // world-space unit vector that follows the pointer while rotating and persists after).
  // The bounding box stays axis-aligned; only the handle moves around it.
  function selectionHandle(selBox) {
    var cxw = (selBox.minX + selBox.maxX) / 2, cyw = (selBox.minY + selBox.maxY) / 2;
    var ctr = worldToCanvas(cxw, cyw);
    var dir = canvasState.handleDir || [0, 1];
    var dp = worldToCanvas(cxw + dir[0], cyw + dir[1]);
    var ux = dp[0] - ctr[0], uy = dp[1] - ctr[1], ul = Math.hypot(ux, uy) || 1; ux /= ul; uy /= ul;
    var a = worldToCanvas(selBox.minX, selBox.minY), b = worldToCanvas(selBox.maxX, selBox.maxY);
    var hw = Math.abs(b[0] - a[0]) / 2, hh = Math.abs(b[1] - a[1]) / 2;
    // Distance from center to the box edge along the handle direction (ray-rectangle
    // hit), so the stem meets the box exactly instead of a circumscribed circle.
    var tEdge = Math.min(
      Math.abs(ux) > 1e-6 ? hw / Math.abs(ux) : Infinity,
      Math.abs(uy) > 1e-6 ? hh / Math.abs(uy) : Infinity
    );
    if (!isFinite(tEdge)) tEdge = 0;
    return { ex: ctr[0] + ux * tEdge, ey: ctr[1] + uy * tEdge, hx: ctr[0] + ux * (tEdge + 26), hy: ctr[1] + uy * (tEdge + 26) };
  }

  // Point the handle sensibly when the selection changes: along a single part's "up",
  // or north for a group.
  function resetHandleDir() {
    var sel = selectedParts();
    canvasState.handleDir = (sel.length === 1) ? rotatePoint(0, 1, sel[0].rotation) : [0, 1];
  }

  function drawLayout() {
    var canvas = $('#layout-canvas');
    if (!canvas) return;
    var ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, canvas.width, canvas.height);

    var v = validateLayout();
    $('#layout-errors').textContent = v.msgs.join('\n');
    // Flip is hidden in 2.5D (a mirror isn't recoverable when features live at specific
    // depths on one face) and in tubing (the opposite wall is handled server-side by
    // mirroring the pattern, not by a user flip).
    var flipBtn = $('#btn-flip');
    if (flipBtn) { flipBtn.hidden = (state.mode === '2.5d' || state.mode === 'tubing'); flipBtn.disabled = state.selectedIds.length === 0; }

    // Theme-aware colors (read the CSS variables so the canvas matches light/dark).
    var col = {
      ink: cssVar('--ink') || '#e6edf3',
      muted: cssVar('--muted') || '#9aa7b4',
      danger: cssVar('--danger') || '#f85149',
      accent: cssVar('--accent') || '#2f81f7',
      ok: cssVar('--ok') || '#3fb950',
    };

    // Stock = combined bounding box (dotted). Red if it exceeds the machine. The G54
    // origin marker sits at its lower-left.
    var bb = v.bbox;
    if (bb) {
      var a = worldToCanvas(bb.minX, bb.minY), c = worldToCanvas(bb.maxX, bb.maxY);
      ctx.save();
      ctx.setLineDash([5, 4]);
      ctx.strokeStyle = v.tooBig ? col.danger : col.muted; ctx.lineWidth = 1;
      ctx.strokeRect(Math.min(a[0], c[0]), Math.min(a[1], c[1]), Math.abs(c[0] - a[0]), Math.abs(c[1] - a[1]));
      ctx.setLineDash([]);
      ctx.restore();
    }

    // Parts.
    state.parts.forEach(function (p) {
      var pl = placement(p), s = pl.shape;
      var invalid = !!v.bad[p.id];
      var selected = isSelected(p.id);
      ctx.beginPath();
      s.pts.forEach(function (pt, i) {
        var pc = worldToCanvas(pl.x + pt[0], pl.y + pt[1]);
        if (i) ctx.lineTo(pc[0], pc[1]); else ctx.moveTo(pc[0], pc[1]);
      });
      ctx.closePath();
      ctx.fillStyle = invalid ? 'rgba(248,81,73,0.18)' : (selected ? 'rgba(47,129,247,0.22)' : 'rgba(154,167,180,0.12)');
      ctx.fill();
      ctx.strokeStyle = invalid ? col.danger : (selected ? col.accent : col.muted);
      ctx.lineWidth = selected ? 2 : 1;
      ctx.stroke();
      s.holes.forEach(function (h) {
        var hc = worldToCanvas(pl.x + h.cx, pl.y + h.cy);
        ctx.beginPath(); ctx.arc(hc[0], hc[1], Math.max(1, h.r * canvasState.scale), 0, 7); ctx.strokeStyle = col.muted; ctx.stroke();
      });
      (s.inner || []).forEach(function (ring) {
        ctx.beginPath();
        ring.forEach(function (pt, i) {
          var rc = worldToCanvas(pl.x + pt[0], pl.y + pt[1]);
          if (i) ctx.lineTo(rc[0], rc[1]); else ctx.moveTo(rc[0], rc[1]);
        });
        ctx.closePath(); ctx.strokeStyle = col.muted; ctx.lineWidth = 1; ctx.stroke();
      });
      var lc = worldToCanvas(pl.x, pl.y + pl.h);
      ctx.fillStyle = col.ink; ctx.font = '11px sans-serif';
      ctx.fillText(p.name + (p.flipped ? ' (flipped)' : ''), lc[0] + 3, lc[1] + 12);
    });

    // Selection box + rotation handle.
    var selBox = combinedBBox(selectedParts());
    if (selBox) {
      var a2 = worldToCanvas(selBox.minX, selBox.minY), b2 = worldToCanvas(selBox.maxX, selBox.maxY);
      ctx.save();
      ctx.strokeStyle = '#2f81f7'; ctx.lineWidth = 1; ctx.setLineDash([4, 3]);
      ctx.strokeRect(Math.min(a2[0], b2[0]), Math.min(a2[1], b2[1]), Math.abs(b2[0] - a2[0]), Math.abs(b2[1] - a2[1]));
      ctx.setLineDash([]);
      var hg = selectionHandle(selBox);
      ctx.beginPath(); ctx.moveTo(hg.ex, hg.ey); ctx.lineTo(hg.hx, hg.hy);
      ctx.strokeStyle = col.accent; ctx.lineWidth = 1.5; ctx.stroke();
      ctx.beginPath(); ctx.arc(hg.hx, hg.hy, 6, 0, 7); ctx.fillStyle = col.accent; ctx.fill();
      ctx.restore();
    }

    // Origin marker + labeled axes, drawn last so they sit on top of the parts. The
    // origin (green dot) is the G54 lower-left; X (red) points +X to the right, Y (green)
    // points +Y up. Colors match the 3D preview so both views read the same.
    if (bb) {
      var o = worldToCanvas(bb.minX, bb.minY);
      ctx.save();
      ctx.fillStyle = col.ok;
      ctx.beginPath(); ctx.arc(o[0], o[1], 4, 0, 7); ctx.fill();
      var L = 42, head = 6;
      [['#ff0000', 1, 0, 'X'], ['#2ea043', 0, -1, 'Y']].forEach(function (ax) {
        var color = ax[0], dx = ax[1], dy = ax[2], label = ax[3];
        var ex = o[0] + dx * L, ey = o[1] + dy * L;
        ctx.strokeStyle = color; ctx.fillStyle = color; ctx.lineWidth = 2;
        ctx.beginPath(); ctx.moveTo(o[0], o[1]); ctx.lineTo(ex, ey); ctx.stroke();
        var ang = Math.atan2(ey - o[1], ex - o[0]);
        ctx.beginPath(); ctx.moveTo(ex, ey);
        ctx.lineTo(ex - head * Math.cos(ang - 0.4), ey - head * Math.sin(ang - 0.4));
        ctx.lineTo(ex - head * Math.cos(ang + 0.4), ey - head * Math.sin(ang + 0.4));
        ctx.closePath(); ctx.fill();
        ctx.font = 'bold 12px sans-serif'; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
        ctx.fillText(label, o[0] + dx * (L + 11), o[1] + dy * (L + 11));
      });
      ctx.restore();
    }

    // Combined size readout, upper-right.
    ctx.save();
    ctx.textAlign = 'right'; ctx.font = '12px sans-serif';
    ctx.fillStyle = v.tooBig ? col.danger : col.muted;
    ctx.fillText(bb ? (bb.w.toFixed(2) + '" x ' + bb.h.toFixed(2) + '"') : 'no parts', canvas.width - 8, 16);
    ctx.restore();
  }

  function hitTest(wx, wy) {
    for (var i = state.parts.length - 1; i >= 0; i--) {
      var b = footprint(state.parts[i]);
      if (wx >= b.minX && wx <= b.maxX && wy >= b.minY && wy <= b.maxY) return state.parts[i];
    }
    return null;
  }

  function bindLayout() {
    var canvas = $('#layout-canvas');
    function evtCanvas(e) {
      var rect = canvas.getBoundingClientRect();
      var t = e.touches ? e.touches[0] : e;
      return [(t.clientX - rect.left) * (canvas.width / rect.width),
              (t.clientY - rect.top) * (canvas.height / rect.height)];
    }
    function down(e) {
      var c = evtCanvas(e), w = canvasToWorld(c[0], c[1]);
      var shift = e.shiftKey;
      var selBox = combinedBBox(selectedParts());
      // Rotation handle rotates the whole selection about its center.
      if (selBox) {
        var hg = selectionHandle(selBox);
        if (Math.hypot(c[0] - hg.hx, c[1] - hg.hy) <= 12) {
          var pivot = [(selBox.minX + selBox.maxX) / 2, (selBox.minY + selBox.maxY) / 2];
          // Tubing rotates all faces together (they share one orientation on the jig),
          // so snapshot every part, not just the selection.
          var rotParts = state.mode === 'tubing' ? state.parts : selectedParts();
          canvasState.action = {
            type: 'rotate', pivot: pivot,
            refAngle: Math.atan2(w[1] - pivot[1], w[0] - pivot[0]),
            snap: rotParts.map(function (p) { return { p: p, cx: p.cx, cy: p.cy, rot: p.rotation }; })
          };
          e.preventDefault();
          return;
        }
      }
      var hit = hitTest(w[0], w[1]);
      if (hit) {
        if (shift) {
          if (isSelected(hit.id)) state.selectedIds = state.selectedIds.filter(function (id) { return id !== hit.id; });
          else state.selectedIds.push(hit.id);
          resetHandleDir();
        } else {
          if (!isSelected(hit.id)) { state.selectedIds = [hit.id]; resetHandleDir(); }
          canvasState.action = {
            type: 'drag', startWorld: w,
            snap: selectedParts().map(function (p) { return { p: p, cx: p.cx, cy: p.cy }; })
          };
        }
        drawLayout();
        e.preventDefault();
      } else if (!shift) {
        state.selectedIds = [];  // click empty space to deselect
        drawLayout();
      }
    }
    function move(e) {
      var act = canvasState.action;
      if (!act) return;
      var c = evtCanvas(e), w = canvasToWorld(c[0], c[1]);
      if (act.type === 'drag') {
        var dx = w[0] - act.startWorld[0], dy = w[1] - act.startWorld[1];
        act.snap.forEach(function (s) { s.p.cx = s.cx + dx; s.p.cy = s.cy + dy; });
      } else if (act.type === 'rotate') {
        var cur = Math.atan2(w[1] - act.pivot[1], w[0] - act.pivot[0]);
        // Handle follows the pointer around the box (and persists after release).
        var hl = Math.hypot(w[0] - act.pivot[0], w[1] - act.pivot[1]) || 1;
        canvasState.handleDir = [(w[0] - act.pivot[0]) / hl, (w[1] - act.pivot[1]) / hl];
        var cwDeg = -(cur - act.refAngle) * 180 / Math.PI;  // clockwise-positive delta
        if (state.mode === 'tubing') {
          // A tube must stay square to its jig: hard-snap to 90 deg (no free angle) and
          // rotate every face in place about its own center so both walls keep the same
          // orientation. Position is irrelevant for a tube, so there's no orbit.
          cwDeg = Math.round(cwDeg / 90) * 90;
          act.snap.forEach(function (s) {
            s.p.rotation = (((s.rot + cwDeg) % 360) + 360) % 360;
          });
        } else {
          var snapped = Math.round(cwDeg / 45) * 45;
          if (Math.abs(snapped - cwDeg) <= 5) cwDeg = snapped;
          act.snap.forEach(function (s) {
            var vv = rotatePoint(s.cx - act.pivot[0], s.cy - act.pivot[1], cwDeg);
            s.p.cx = act.pivot[0] + vv[0];
            s.p.cy = act.pivot[1] + vv[1];
            s.p.rotation = (((s.rot + cwDeg) % 360) + 360) % 360;
          });
        }
      }
      drawLayout();
      e.preventDefault();
    }
    function up() {
      // After rotating a tube, re-pack the faces adjacently so a horizontal→vertical
      // flip doesn't leave them spread apart, then refit the view to the tidy bbox.
      if (canvasState.action && canvasState.action.type === 'rotate' && state.mode === 'tubing') {
        restackTubeParts();
        refitView();
        drawLayout();
      }
      canvasState.action = null;
    }
    canvas.addEventListener('mousedown', down);
    window.addEventListener('mousemove', move);
    window.addEventListener('mouseup', up);
    canvas.addEventListener('touchstart', down, { passive: false });
    canvas.addEventListener('touchmove', move, { passive: false });
    canvas.addEventListener('touchend', up);

    $('#btn-flip').addEventListener('click', function () {
      if (state.mode === '2.5d' || state.mode === 'tubing') return;  // flip not allowed in 2.5D/tubing
      selectedParts().forEach(function (p) { p.flipped = !p.flipped; });
      drawLayout();
    });
    $('#btn-zoom-in').addEventListener('click', function () { state.zoom = Math.min(5, state.zoom * 1.25); refitView(); drawLayout(); });
    $('#btn-zoom-out').addEventListener('click', function () { state.zoom = Math.max(0.2, state.zoom / 1.25); refitView(); drawLayout(); });
  }

  function updateLayoutInfo() {
    var el = $('#info-machine-name'); if (el) el.textContent = state.machine.name;
    el = $('#info-machine-size'); if (el) el.textContent = state.machine.width + '" x ' + state.machine.height + '"';
    el = $('#info-tool'); if (el) el.textContent = state.tool_diameter_text;
  }

  // The Layout hint reads differently for tubing: there's no sheet to nest on — the
  // step exists only to square the face(s) to the tube-jig axis (the machine's Y axis).
  function updateLayoutHint() {
    var el = $('#layout-hint');
    if (!el) return;
    if (state.mode === 'tubing') {
      el.textContent = 'Drag the round handle to rotate the tube in 90 deg steps. ' +
        'Orient each face so the tube runs vertically (the Y axis) — that is the axis of the ' +
        'tube jig on the machine. Both faces rotate together.';
    } else {
      el.textContent = '↔ Widen the panel for easier layout. Click to select (Shift-click for multiple), ' +
        'drag to move, drag the round handle to rotate (snaps to 45°). The dotted box is the stock; ' +
        'its lower-left is the G54 origin.';
    }
  }

  /* ------------------------------------------------------------- preview */
  function resetPreview() {
    var notesEl = $('#preview-notes');
    if (notesEl) { notesEl.textContent = ''; notesEl.hidden = true; }
    $('#preview-result').hidden = true;
    $('#preview-errors').textContent = '';
    $('#gen-status').textContent = '';
  }

  // Establish the anonymous upload session that the compute endpoints require. A
  // blocking #verify-overlay (rendered visible in the template) covers the wizard
  // until this succeeds, so a failed/incomplete check is obvious rather than a
  // mysterious 401 later. With a Turnstile site key we wait for the widget's token
  // (the template's token bridge buffers it, so ordering with the async widget
  // doesn't matter); without a key (local dev / unkeyed host) there's no overlay
  // and we mint the session directly — the backend bypasses verification then.
  function establishUploadSession() {
    if (!CFG.turnstileSiteKey) {
      verifyUploadSession(null);
      return;
    }
    window.__ptOnToken = verifyUploadSession;                 // token bridge
    // Error/expiry fire only after the widget renders (well after this script
    // loads), so plain globals are safe — no buffering needed.
    window.onTurnstileError = function () {
      setVerifyStatus('The check could not load. Disable ad/tracking blockers for this site, then reload.');
    };
    window.onTurnstileExpired = function () {
      setVerifyStatus('The check expired — completing it again…');
      resetTurnstile();
    };
    if (window.__ptToken) verifyUploadSession(window.__ptToken);  // token already arrived
  }

  function setVerifyStatus(msg) { var s = $('#verify-status'); if (s) s.textContent = msg; }

  function resetTurnstile() { try { if (window.turnstile) window.turnstile.reset(); } catch (e) {} }

  function verifyUploadSession(token) {
    fetch('/session/verify', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token: token })
    }).then(function (r) { return r.json(); })
      .then(function (j) {
        state.verified = !!(j && j.ok);
        dbg('session', { verified: state.verified });
        if (state.verified) {
          var o = $('#verify-overlay'); if (o) o.hidden = true;   // unblock the wizard
          autoLoadConfig();  // /session/config is gated, so load only after we're verified
        } else {
          setVerifyStatus('Verification failed — please try again.');
          resetTurnstile();
        }
      })
      .catch(function (e) {
        dbg('session:err', String(e));
        setVerifyStatus('Could not reach the server — please retry.');
        resetTurnstile();
      });
  }

  // ==========================================================================
  // Team-config source (upload flow). The team's YAML config lives at a URL the
  // BROWSER fetches (client-side, so there's no SSRF surface); we then POST the text
  // to /session/config for strict validation. The URL is remembered in localStorage
  // and reflected into the ?config= query param so a shared bookmark auto-loads it.
  // A successful load/clear reloads the page so the server re-renders the whole body
  // (bed size, tool default, machine + material lists) from the new config.
  // ==========================================================================
  var CONFIG_URL_KEY = 'penguincam_config_url';
  // Hosts that serve files but DON'T send permissive CORS headers, so a client-side
  // fetch always fails. We can't confirm CORS from JS (the browser only says "Failed
  // to fetch"), but for these known hosts we can give a confident, specific message.
  var NON_CORS_HOSTS = ['drive.google.com', 'docs.google.com', 'dropbox.com',
    'www.dropbox.com', 'onedrive.live.com', '1drv.ms'];
  var CONFIG_FETCH_TIMEOUT_MS = 15000;

  function getStoredConfigUrl() { try { return localStorage.getItem(CONFIG_URL_KEY); } catch (e) { return null; } }
  function setStoredConfigUrl(u) { try { localStorage.setItem(CONFIG_URL_KEY, u); } catch (e) {} }
  function clearStoredConfigUrl() { try { localStorage.removeItem(CONFIG_URL_KEY); } catch (e) {} }

  function getUrlParam(name) {
    try { return new URL(window.location.href).searchParams.get(name); } catch (e) { return null; }
  }
  function setConfigParam(url) {
    try {
      var u = new URL(window.location.href);
      if (url) u.searchParams.set('config', url); else u.searchParams.delete('config');
      history.replaceState(null, '', u.toString());
    } catch (e) {}
  }

  function setConfigStatus(msg, kind) {
    var el = $('#config-source-status');
    if (!el) return;
    el.textContent = msg || '';
    el.className = 'config-source-status' + (kind ? ' ' + kind : '');
  }

  function hostOf(url) { try { return new URL(url).hostname.toLowerCase(); } catch (e) { return ''; } }

  function showConfigError(url, err) {
    if (err && err.kind === 'http') {
      setConfigStatus('The file could not be loaded (HTTP ' + err.status + '). Make sure the URL is public and correct.', 'error');
      return;
    }
    // Opaque fetch rejection: almost always CORS or an unreachable/typo'd URL.
    var host = hostOf(url);
    if (NON_CORS_HOSTS.indexOf(host) !== -1) {
      setConfigStatus(host + ' blocks direct fetching (CORS), so its links can’t be used here. Host the file on GitHub (raw) or a gist instead.', 'error');
    } else {
      setConfigStatus('Could not fetch the file. This is usually a CORS restriction (the host must allow cross-origin fetch — GitHub raw and gists do) or a wrong/unreachable URL.', 'error');
    }
  }

  // Fetch the YAML client-side, POST it to /session/config, and (on success) reload so the
  // page re-renders from the applied config. opts.reload defaults to true.
  function loadConfigFromUrl(url, opts) {
    opts = opts || {};
    url = (url || '').trim();
    if (!url) return;
    if (!/^https?:\/\//i.test(url)) { setConfigStatus('Enter a full URL starting with https://', 'error'); return; }
    if (!state.verified) { setConfigStatus('Please complete the verification check first.', 'error'); return; }

    setConfigStatus('Loading configuration…');
    var controller = (typeof AbortController !== 'undefined') ? new AbortController() : null;
    var timer = controller ? setTimeout(function () { controller.abort(); }, CONFIG_FETCH_TIMEOUT_MS) : null;

    fetch(url, { signal: controller ? controller.signal : undefined, redirect: 'follow' })
      .then(function (r) {
        if (!r.ok) throw { kind: 'http', status: r.status };
        return r.text();
      })
      .then(function (text) { return postConfig(text, url, opts); })
      .catch(function (err) {
        if (err && err.name === 'AbortError') { setConfigStatus('Timed out fetching the file. Check the URL and try again.', 'error'); return; }
        showConfigError(url, err);
      })
      .then(function () { if (timer) clearTimeout(timer); });
  }

  function postConfig(text, url, opts) {
    return fetch('/session/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ yaml: text, url: url })
    }).then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (res) {
        if (!res.ok || !res.j || !res.j.ok) {
          setConfigStatus((res.j && res.j.error) || 'The configuration was rejected.', 'error');
          return;
        }
        setStoredConfigUrl(url);
        setConfigParam(url);
        // Surface sanitization notes across the reload.
        if (res.j.warnings && res.j.warnings.length) {
          try { sessionStorage.setItem('penguincam_config_warnings', JSON.stringify(res.j.warnings)); } catch (e) {}
        }
        if (opts.reload === false) { setConfigStatus('Configuration loaded.', 'ok'); return; }
        window.location.reload();
      });
  }

  function resetConfig() {
    setConfigStatus('Resetting to defaults…');
    fetch('/session/config', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ yaml: '' })
    }).then(function (r) { return r.json(); })
      .then(function () { clearStoredConfigUrl(); setConfigParam(null); window.location.reload(); })
      .catch(function () { setConfigStatus('Could not reach the server — please retry.', 'error'); });
  }

  // Auto-load once verified: ?config= param wins over a saved URL. If the server has
  // ALREADY applied this URL (not using defaults and the URL matches), do nothing -- this
  // is what stops the reload from re-fetching in an endless loop.
  function autoLoadConfig() {
    var param = getUrlParam('config');
    var url = param || getStoredConfigUrl();
    if (!url) return;
    var alreadyApplied = !CFG.usingDefault && CFG.configUrl && CFG.configUrl === url;
    if (alreadyApplied) {
      // Keep localStorage and the address bar in sync (e.g. arrived via a shared link).
      setStoredConfigUrl(url);
      if (!param) setConfigParam(url);
      return;
    }
    loadConfigFromUrl(url, { reload: true });
  }

  function bindConfigSource() {
    var input = $('#f-config-url');
    if (!input) return;  // not the upload flow
    // Prefill with whatever is applied/saved so the field reflects reality.
    input.value = CFG.configUrl || getUrlParam('config') || getStoredConfigUrl() || '';
    var resetBtn = $('#config-reset-btn');
    if (resetBtn && !CFG.usingDefault) resetBtn.hidden = false;

    var loadBtn = $('#config-load-btn');
    if (loadBtn) loadBtn.addEventListener('click', function () { loadConfigFromUrl(input.value, { reload: true }); });
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { e.preventDefault(); loadConfigFromUrl(input.value, { reload: true }); }
    });
    if (resetBtn) resetBtn.addEventListener('click', resetConfig);

    renderLoadedConfig();
    // Surface any sanitization warnings stashed before the last reload.
    try {
      var w = sessionStorage.getItem('penguincam_config_warnings');
      if (w) {
        sessionStorage.removeItem('penguincam_config_warnings');
        var list = JSON.parse(w);
        if (list && list.length) setConfigStatus(list.join(' '), 'warn');
      }
    } catch (e) {}
  }

  // Confidence signal (mirrors the Onshape header banner): once a config is applied, show
  // the team it parsed to, right by the field, without cluttering the box.
  function renderLoadedConfig() {
    var el = $('#config-source-loaded');
    if (!el) return;
    if (CFG.usingDefault || !CFG.configTeamNumber) { el.hidden = true; el.textContent = ''; return; }
    el.hidden = false;
    el.textContent = '';
    el.appendChild(document.createTextNode('Loaded config for team '));
    var strong = document.createElement('strong');
    strong.textContent = String(CFG.configTeamNumber);   // textContent: server-sanitized, but stay safe
    el.appendChild(strong);
    el.appendChild(document.createTextNode(' — ' + (CFG.configTeamName || 'Unknown')));
  }

  function generate() {
    $('#preview-errors').textContent = '';
    $('#gen-status').textContent = 'Generating…';

    if (state.mode === 'tubing') { generateTube(); }
    else if (state.mode === '2.5d') { generateSingle(); }
    else { generateJob(); }
  }

  // Shared POST /process handler for the single-file paths (2.5D and tubing).
  function submitToProcess(fd, label) {
    dbg(label + ':req');
    return fetch('/process', { method: 'POST', body: fd })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (res) {
        if (!res.ok || res.j.error) { $('#preview-errors').textContent = res.j.details || res.j.error || 'Generation failed'; $('#gen-status').textContent = ''; return; }
        dbg(label + ':ok', { time: res.j.cycle_time });
        state.lastResponse = res.j;
        showResult(res.j);
      })
      .catch(function (e) { $('#preview-errors').textContent = 'Request failed: ' + e; $('#gen-status').textContent = ''; });
  }

  function generateTube() {
    var p = state.parts[0];
    if (!p) { $('#preview-errors').textContent = 'Add a tube face first.'; $('#gen-status').textContent = ''; return Promise.resolve(); }
    var fd = new FormData();
    fd.append('file', p.file, p.name + '.dxf');
    // A second face (optional) machines a distinct pattern on the opposite side; with
    // only one face, the server mirrors it onto the other side.
    if (state.parts[1]) fd.append('file_face2', state.parts[1].file, state.parts[1].name + '.dxf');
    fd.append('material', 'aluminum_tube');
    if (state.machine_id) fd.append('machine_id', state.machine_id);
    fd.append('tool_diameter', state.tool_diameter);
    fd.append('thickness', state.thickness);       // tube wall thickness
    // Both faces share one orientation on the jig; the backend applies this single
    // rotation to every face. Tube rotation is hard-snapped to 90 deg in the Layout step.
    fd.append('rotation', ((Math.round((p.rotation || 0) / 90) * 90) % 360 + 360) % 360);
    fd.append('tube_height', state.tubeHeight);
    fd.append('square_end', state.squareEnd ? '1' : '0');
    fd.append('cut_to_length', state.cutToLength ? '1' : '0');
    fd.append('timestamp', timestamp());
    fd.append('suggested_filename', p.name);
    return submitToProcess(fd, 'tube');
  }

  // Filename base for a multi-part job: the distinct part names joined with "_", capped
  // in length so a big nest doesn't produce an absurd filename (extra names collapse to
  // "+N"). One part -> just its name; no parts -> "job". The timestamp is appended server
  // side, matching the single-part (2.5D/tube) paths that send suggested_filename=p.name.
  function jobFilename() {
    var names = [];
    state.parts.forEach(function (p) { if (names.indexOf(p.name) < 0) names.push(p.name); });
    if (!names.length) return 'job';
    var CAP = 64, kept = [], used = 0;
    for (var i = 0; i < names.length; i++) {
      var addLen = (kept.length ? 1 : 0) + names[i].length;
      if (kept.length && used + addLen > CAP) break;
      kept.push(names[i]); used += addLen;
    }
    var remaining = names.length - kept.length;
    return kept.join('_') + (remaining > 0 ? '+' + remaining : '');
  }

  function generateJob() {
    var fd = new FormData();
    // The parts' combined bounding box is the stock; its lower-left is the G54 origin,
    // so placements are normalized relative to it.
    var bb = combinedBBox() || { minX: 0, minY: 0, w: 0, h: 0 };
    var job = {
      material: state.material, tool_diameter: state.tool_diameter, machine_id: state.machine_id,
      thickness: state.thickness, tab_spacing: state.tab_spacing,
      stock: { width: bb.w, height: bb.h },
      name: jobFilename(), parts: [],
    };
    state.parts.forEach(function (p, i) {
      var pl = placement(p);
      job.parts.push({
        file_index: i, name: p.name,
        place_x: pl.x - bb.minX, place_y: pl.y - bb.minY,
        rotation: p.rotation, mirror: !!p.flipped,
      });
      fd.append('file_' + i, p.file, p.name + '.dxf');
    });
    fd.append('job', JSON.stringify(job));
    fd.append('timestamp', timestamp());
    dbg('process-job:req', { parts: job.parts.length });
    return fetch('/process-job', { method: 'POST', body: fd })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (res) {
        if (!res.ok || !res.j.success) { showGenErrors(res.j); return; }
        dbg('process-job:ok', { parts: (res.j.parts || []).length, time: res.j.cycle_time });
        state.lastResponse = res.j;
        showResult(res.j);
      })
      .catch(function (e) { dbg('process-job:fail', String(e)); $('#preview-errors').textContent = 'Request failed: ' + e; $('#gen-status').textContent = ''; });
  }

  function generateSingle() {
    var p = state.parts[0];
    if (!p) { $('#preview-errors').textContent = 'Add a part first.'; $('#gen-status').textContent = ''; return Promise.resolve(); }
    var fd = new FormData();
    fd.append('file', p.file, p.name + '.dxf');
    fd.append('material', state.material);
    if (state.machine_id) fd.append('machine_id', state.machine_id);
    fd.append('tool_diameter', state.tool_diameter);
    fd.append('thickness', state.thickness);
    fd.append('origin_corner', 'bottom-left');
    fd.append('rotation', Math.round(p.rotation) % 360);
    fd.append('mirror', '0');  // 2.5D never mirrors (flip disabled in this mode)
    fd.append('tab_spacing', state.tab_spacing);
    fd.append('timestamp', timestamp());
    fd.append('suggested_filename', p.name);
    return submitToProcess(fd, 'process');
  }

  function showGenErrors(j) {
    $('#gen-status').textContent = '';
    if (j && j.part_errors) {
      $('#preview-errors').textContent = j.part_errors.map(function (e) { return '• ' + e.error; }).join('\n');
    } else {
      $('#preview-errors').textContent = (j && (j.error || j.details)) || 'Generation failed';
    }
  }

  function showNotes(resp) {
    // Settings that were adapted to suit this part. Shown every run, because the operator
    // usually can't change the team config and needs to know what they're actually cutting.
    var el = $('#preview-notes');
    if (!el) return;
    var notes = (resp && resp.warnings) || [];
    el.textContent = notes.map(function (n) { return '\u26a0 ' + n; }).join('\n');
    el.hidden = !notes.length;
  }

  function showResult(resp) {
    $('#gen-status').textContent = '';
    $('#preview-result').hidden = false;
    showNotes(resp);
    var t = resp.cycle_time ? ('Estimated cycle time: ' + resp.cycle_time) : '';
    var n;
    if (state.mode === 'tubing') { n = state.parts.length + ' face' + (state.parts.length === 1 ? '' : 's'); }
    else { n = resp.parts ? (resp.parts.length + ' part(s)') : '1 part'; }
    $('#preview-stats').textContent = [n, t].filter(Boolean).join(' · ');
    $('#btn-do').disabled = false;   // gcode ready — enable the save/download action
    show3DPreview(resp);
    updateSummary();  // refresh the stock chip with the server-authoritative size
  }

  function show3DPreview(resp) {
    if (typeof THREE === 'undefined' || typeof GcodeViewer === 'undefined') {
      $('#viewer-empty').textContent = '3D preview unavailable (three.js failed to load).';
      return;
    }
    if (!viewer) {
      viewer = new GcodeViewer({
        canvas: $('#gcode-canvas'), container: $('#viewer-container'),
        scrubber: $('#toolpath-scrubber'), scrubberContainer: $('#scrubber-container'),
        scrubberLabel: $('#scrubber-label'), scrubberOp: $('#scrubber-op'),
        playbackControls: $('#playback-controls'), playButton: $('#play-button'),
        restartButton: $('#restart-button'), speedSelect: $('#playback-speed'),
        resetButton: $('#reset-view'), emptyState: $('#viewer-empty'),
      });
    }
    var W, D, stockH;
    if (state.mode === 'tubing') {
      // A tube face, not a sheet: use the first face's placed (rotated) extents so the
      // stock box matches the rotated G-code, plus the tube height for depth.
      var p0 = state.parts[0];
      var s0 = p0 ? placedShape(p0) : null;
      W = s0 ? s0.w : state.machine.width;
      D = s0 ? s0.h : state.machine.height;
      stockH = state.tubeHeight;
    } else {
      var bb = combinedBBox();
      W = (resp.stock && resp.stock.width) || (bb ? bb.w : state.machine.width);
      D = (resp.stock && resp.stock.height) || (bb ? bb.h : state.machine.height);
      stockH = state.thickness;
    }
    viewer.load(resp.gcode, {
      stockWidth: W, stockDepth: D,
      stockHeight: stockH, toolDiameter: state.tool_diameter,
    });
    dbg('preview', { w: W, d: D });
  }

  /* ------------------------------------------------- final action (save) */
  var SAVE_PREF_KEY = 'penguincam_save_action';
  function readSavePref() { try { return localStorage.getItem(SAVE_PREF_KEY); } catch (e) { return null; } }
  function writeSavePref(a) { try { localStorage.setItem(SAVE_PREF_KEY, a); } catch (e) {} }
  function actionLabel(a) { return a === 'drive' ? 'Send to Google Drive' : 'Download Program'; }

  // Choose the default action: remembered preference, but only 'drive' if Drive is
  // configured now (falls back to download if a saved 'drive' pref is no longer valid).
  function preferredAction() {
    var pref = readSavePref();
    return (pref === 'drive' && CFG.driveEnabled) ? 'drive' : 'download';
  }

  // Configure the split button: caret + Drive option only when Drive is configured.
  function setupFinalAction() {
    var driveOk = !!CFG.driveEnabled;
    if (state.saveAction === 'drive' && !driveOk) state.saveAction = 'download';
    $('#btn-do-caret').hidden = !driveOk;
    $('#final-action').classList.toggle('has-caret', driveOk);
    var driveItem = document.querySelector('#do-menu li[data-action="drive"]');
    if (driveItem) driveItem.hidden = !driveOk;
    $('#btn-do').textContent = actionLabel(state.saveAction);
  }

  function chooseAction(a) {
    state.saveAction = a;
    writeSavePref(a);
    $('#btn-do').textContent = actionLabel(a);
    $('#do-menu').hidden = true;
    performAction(a);
  }

  function performAction(a) {
    var resp = state.lastResponse;
    if (!resp || !resp.filename) return;  // not generated yet
    if (a === 'drive') driveSave(resp.filename);
    else doDownload(resp.filename);
  }

  function doDownload(token) {
    // Open in a new top-level tab rather than an in-frame anchor: a sandboxed Onshape
    // iframe can block in-frame downloads, but the response forces attachment so the
    // new tab downloads and closes. (Relies on popups, which OAuth already uses.)
    window.open('/download/' + token, '_blank');
    $('#gen-status').textContent = 'Download started.';
  }

  function driveSave(token) {
    var status = $('#gen-status'), errs = $('#preview-errors');
    errs.textContent = '';
    status.textContent = 'Checking Google Drive…';
    fetch('/drive/status', { credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (st) {
        if (!st.enabled) { errs.textContent = 'Google Drive is not configured for your team.'; status.textContent = ''; return; }
        if (!st.authenticated) {
          status.textContent = 'Complete Google sign-in in the popup…';
          var popup = window.open('/auth/login', 'penguincam_gauth', 'width=600,height=700');
          if (!popup) { errs.textContent = 'Popup blocked — allow popups, then try again.'; status.textContent = ''; return; }
          var iv = setInterval(function () {
            if (popup.closed) { clearInterval(iv); driveUpload(token); }
          }, 500);
          setTimeout(function () { clearInterval(iv); }, 180000);
          return;
        }
        driveUpload(token);
      })
      .catch(function (e) { errs.textContent = 'Drive check failed: ' + e; status.textContent = ''; });
  }

  function driveUpload(token) {
    var status = $('#gen-status'), errs = $('#preview-errors'), btn = $('#btn-do');
    status.textContent = 'Uploading to Google Drive…';
    btn.disabled = true;
    // POST as JSON so require_auth returns 401 (not an HTML redirect) if still unauthed.
    fetch('/drive/upload/' + token, {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' }, body: '{}'
    })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (res) {
        btn.disabled = false;
        if (res.ok && res.j.success) { status.textContent = res.j.message || 'Saved to Google Drive.'; }
        else { errs.textContent = (res.j && res.j.message) || 'Drive upload failed.'; status.textContent = ''; }
      })
      .catch(function (e) { btn.disabled = false; errs.textContent = 'Drive upload failed: ' + e; status.textContent = ''; });
  }

  function bindFinalAction() {
    $('#btn-do').addEventListener('click', function () { if (!this.disabled) performAction(state.saveAction); });
    $('#btn-do-caret').addEventListener('click', function (e) {
      e.stopPropagation();
      var m = $('#do-menu'); m.hidden = !m.hidden;
    });
    $('#do-menu').addEventListener('click', function (e) {
      var li = e.target.closest('li[data-action]');
      if (li) chooseAction(li.getAttribute('data-action'));
    });
    document.addEventListener('click', function (e) {
      if (!e.target.closest('#final-action')) $('#do-menu').hidden = true;
    });
  }

  /* ----------------------------------------------------------------- init */
  function bindNav() {
    $('#btn-next').addEventListener('click', function () {
      var order = steps();
      var idx = order.indexOf(state.step);
      if (idx < order.length - 1 && canLeave(state.step)) {
        gotoStep(order[idx + 1]);
      }
    });
    $('#btn-back').addEventListener('click', function () {
      var order = steps();
      var idx = order.indexOf(state.step);
      if (idx > 0) gotoStep(order[idx - 1]);
    });

    // Clickable stepper pills (delegated), keyboard-activatable.
    var bar = $('#stepbar');
    function pillActivate(e) {
      var li = e.target.closest ? e.target.closest('li[data-step]') : null;
      if (!li) return;
      if (e.type === 'keydown' && e.key !== 'Enter' && e.key !== ' ') return;
      e.preventDefault();
      navigateTo(li.getAttribute('data-step'));
    }
    if (bar) { bar.addEventListener('click', pillActivate); bar.addEventListener('keydown', pillActivate); }
  }

  function bindConnect() {
    var btn = $('#btn-connect');
    if (!btn) return;
    var watching = false;
    function setStatus(msg) { $('#connect-status').textContent = msg; }
    // Most common cause of a failed connect in the iframe/OAuth-popup flow: a pop-up
    // blocker stops the popup, or tracking/cookie prevention blocks the iframe from
    // seeing the session after sign-in. Append this to the failure messages.
    var BLOCKER_HINT = ' If it keeps failing, allow pop-ups and disable ad/tracking blockers for onshape.com and popcornpenguins.com, then retry.';

    // Confirm the iframe's own session is authenticated, then reload once to re-render
    // with the now-authenticated server context (config banner, material/tool options).
    function verifyAndEnter() {
      fetch('/onshape/authed', { credentials: 'same-origin' })
        .then(function (r) { return r.json(); })
        .then(function (j) {
          dbg('authed-check', j);
          if (j && j.authenticated) location.reload();
          else { watching = false; setStatus('Sign-in did not complete.' + BLOCKER_HINT); }
        })
        .catch(function (e) { watching = false; dbg('authed-check:err', String(e)); setStatus('Could not verify sign-in.' + BLOCKER_HINT); });
    }

    btn.addEventListener('click', function () {
      if (watching) return;
      var popup = window.open('/onshape/auth?popup=1', 'penguincam_oauth', 'width=520,height=720');
      if (!popup) { setStatus('Pop-up blocked — allow pop-ups for onshape.com and popcornpenguins.com, then click Connect.'); return; }
      watching = true;
      setStatus('Complete sign-in in the popup window…');
      // Watch the popup close locally (no server polling); cross-origin OAuth navigation
      // severs window.opener, so a postMessage from the popup isn't reliable. When it
      // closes, verify auth once. A safety cap stops the watcher if the popup lingers.
      var iv = setInterval(function () {
        if (popup.closed) { clearInterval(iv); setStatus('Finishing sign-in…'); verifyAndEnter(); }
      }, 500);
      setTimeout(function () { clearInterval(iv); }, 180000);
    });
  }

  function init() {
    if (DEBUG) { $('#debug-overlay').hidden = false; }
    window.PenguinCAM.debug = dbg; // let the Onshape adapter log into the debug overlay
    // Expose the live mode so the Onshape panel adapter can request a 2.5D
    // (multi-layer) export instead of a flat one when the user picked 2.5D.
    window.PenguinCAM.getMode = function () { return state.mode; };
    bindSetup();
    bindParts();
    bindLayout();
    bindNav();
    bindFinalAction();
    bindConnect();
    // Best-effort live theme sync: apply if Onshape posts a theme update while open.
    // (The load-time theme is already set server-side from the ?theme= URL param.)
    window.addEventListener('message', function (e) {
      var d = e.data;
      if (d && typeof d === 'object' && (d.theme === 'light' || d.theme === 'dark')) applyTheme(d.theme);
    });
    // Full-page (upload) mode shows all steps at once in a 2x2 grid; the narrow
    // Onshape panel iframe keeps the one-step-at-a-time wizard.
    if (state.source === 'upload') {
      $('#wizard').classList.add('grid');
      // 2.5D derives thickness/layers from Onshape's geometry APIs (build_multilayer_dxf)
      // and can't be produced from a plain DXF upload — offer only 2D and Tubing here.
      var opt25 = $('#opt-mode-25d'); if (opt25) opt25.hidden = true;
      var r25 = $('input[name="mode"][value="2.5d"]'); if (r25) r25.disabled = true;
      bindConfigSource();
      establishUploadSession();
    }
    gotoStep('setup');
    dbg('init', { source: state.source, authed: CFG.authenticated, theme: CFG.theme });
    if (state.source === 'onshape' && !CFG.authenticated) {
      $('#connect-overlay').hidden = false;
    }
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();

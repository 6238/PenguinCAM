/* PenguinCAM 3D print wizard (templates/print_wizard.html).
 * Separate from the CNC wizard on purpose: same look, own code. Talks to the
 * print blueprint (/print/part, /print-job, /print-job/<id>/events) and reuses
 * the download and Google Drive routes. Pure helpers are exported for node tests.
 *
 * Setup shows the team's printing configuration as /print/page answers it: the printer,
 * the filament and print settings menus and, when the team allows them, the four
 * overrides. A configuration error stops every step. The upload mode shows the default
 * set read-only.
 *
 * In Onshape mode the Parts step lists parts exported from Onshape: print_onshape.js
 * (loaded first) does the messaging and hands selections to onPartSelections, which
 * posts them to /print/onshape/parts.
 *
 * Every print request carries this page load's page id (X-Print-Page, or pid= on
 * the EventSource and polling URLs), which the server writes into its [PRINT] log
 * lines. Every error the student sees goes through showError, which also reports it.
 */
(function () {
  'use strict';

  var HAS_DOM = typeof document !== 'undefined';
  var CFG = (typeof window !== 'undefined' && window.PenguinCAM) ||
            { source: 'upload', theme: 'dark', driveEnabled: false, returnUrl: '/app' };
  var STEPS = ['setup', 'parts', 'layout', 'preview'];
  var EXPIRED_MESSAGE = 'This slice has expired, go back and slice again.';
  var RATE_MESSAGE = 'Too many slices in a minute, try again shortly.';
  var PARTS_RATE_MESSAGE = 'Too many parts added in a minute, try again shortly.';
  var MIN_CANVAS_W = 120;   // floor for the fitted layout canvas, in CSS pixels
  var STATUS_LINES = { preview: '#gen-status' };   // the status line a step's error replaces
  var ONSHAPE = CFG.source === 'onshape';
  var DEFAULT_MAX_QUANTITY = 25;     // limits.MAX_QUANTITY, until /print/page sends the limits
  var OVERRIDE_KEYS = ['infill', 'walls', 'supports', 'brim'];
  var NUMERIC_OVERRIDES = { infill: true, walls: true };
  var OVERRIDE_LABELS = {
    infill: function (v) { return v + ' %'; },
    walls: function (v) { return String(v); },
    supports: function (v) { return v === 'on' ? 'On (tree, auto)' : 'Off'; },
    brim: function (v) { return { auto: 'Auto', off: 'Off', outer: 'Outer only' }[v] || v; },
  };

  var state = {
    pid: HAS_DOM && typeof window !== 'undefined' && window.crypto ? makePageId(window.crypto) : null,
    source: CFG.source,
    step: 'setup',
    info: null,          // /print/part response
    fits: true,          // part fits the bed
    jobId: null,
    gen: 0,              // submit generation; a late response from an older one is dropped
    record: null,        // last job record seen
    token: null,         // download token once done
    events: null,        // EventSource
    poll: null,          // polling timer (fallback)
    saveAction: 'download',
    parts: [],           // Onshape mode: [{ref, name, size_mm, triangles, source, footprints, quantity}]
    limits: null,        // limits_for_browser(), once the server sends them
    plate: null,         // plate_from_printer(), from the /print/page answer
    copies: [],          // Onshape mode: the plate's copies (print_layout.js describes them)
    problems: [],        // checkPlacement(copies), after every change
    layoutSentence: '',  // the Layout step's sentence; Next waits while it is not empty
    jobKey: null,        // what the job in hand was submitted for (jobKey()); a match re-uses it
    jobCopies: null,     // Onshape mode: the copies that job slices, for the 3D view
    config: null,        // /print/page's `config`: printer, filaments, processes, warnings, error
    overrideChoices: null,   // /print/page's `overrides`: the values each override offers
    filament: null,      // the Setup choices sent with the slice
    process: null,
    overrides: {},       // overridesFromForm(), "Profile default" left out
  };
  var viewer = null;
  var editor = null;        // print_layout.js's plate editor, Onshape mode only
  var resizeTimer = null;   // window.resize debounce for the layout canvas

  function $(sel) { return document.querySelector(sel); }
  function $all(sel) { return Array.prototype.slice.call(document.querySelectorAll(sel)); }
  // The stylesheet's single-column fallback for the full-page grid (wizard.css).
  function narrowGrid() {
    return typeof window !== 'undefined' && window.matchMedia
      ? window.matchMedia('(max-width: 900px)').matches : false;
  }

  /* ----------------------------------------------------------- pure helpers */
  // The page id: 6 random bytes as 12 lowercase hex characters, new on every page load.
  function makePageId(cryptoObj) {
    var bytes = cryptoObj.getRandomValues(new Uint8Array(6));
    var out = '';
    for (var i = 0; i < bytes.length; i++) out += (bytes[i] < 16 ? '0' : '') + bytes[i].toString(16);
    return out;
  }

  function formatDuration(seconds) {
    var s = Math.max(0, Math.round(seconds || 0));
    var h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
    if (h > 0) return h + 'h ' + m + 'm ' + sec + 's';
    if (m > 0) return m + 'm ' + sec + 's';
    return sec + 's';
  }

  function fitsBed(size, bed, height) {
    return size.x <= bed.x && size.y <= bed.y && size.z <= height;
  }

  // Largest size with the canvas's natural aspect ratio that fits both limits, never
  // upscaled past the natural size and never narrower than MIN_CANVAS_W, so a crushed
  // quadrant still shows a readable bed instead of a sliver.
  function fitCanvasSize(availW, availH, naturalW, naturalH) {
    var ratio = naturalW / naturalH;
    var w = Math.min(availW, availH * ratio, naturalW);
    if (!(w >= MIN_CANVAS_W)) w = MIN_CANVAS_W;   // also catches NaN and negative space
    return { w: Math.round(w), h: Math.round(w / ratio) };
  }

  function summaryLine(summary) {
    var parts = [];
    if (summary.print_time_s != null) parts.push('Print time ' + formatDuration(summary.print_time_s));
    if (summary.filament_g != null) {
      var f = 'Filament ' + summary.filament_g + ' g';
      if (summary.filament_m != null) f += ' (' + summary.filament_m + ' m)';
      parts.push(f);
    }
    if (summary.layer_count != null) parts.push(summary.layer_count + (summary.layer_count === 1 ? ' layer' : ' layers'));
    return parts.join(' · ');
  }

  function printPageUrl(source, theme, returnPath) {
    return '/print?source=' + encodeURIComponent(source) + '&theme=' + encodeURIComponent(theme) +
           '&return=' + encodeURIComponent(returnPath);
  }

  function statusText(record) {
    var label = record.state === 'queued' ? 'Queued' : 'Slicing';
    if (record.elapsed_s == null) return label;
    return label + ', ' + Math.round(record.elapsed_s) + ' s';
  }

  // The viewer's own trouble is added to the slicer's warnings, never over them.
  function appendNote(existing, message) {
    return existing ? existing + '\n' + message : message;
  }

  function tooBigMessage(size, bed, height) {
    return 'Part is larger than the bed: ' + size.x + ' x ' + size.y + ' x ' + size.z + ' mm on ' +
           bed.x + ' x ' + bed.y + ' x ' + height + ' mm.';
  }

  // What makes two listed parts the same part (PartSource.key() on the server).
  function sourceKey(source) {
    var s = source || {};
    return [s.documentId, s.wvmId, s.elementId, s.partId, s.configuration || ''].join('|');
  }

  // A new list: `list` plus each incoming part whose ref and source are not listed yet,
  // at quantity 1. The server answers a part already stored with its existing ref.
  function addParts(list, incoming) {
    var out = list.slice();
    var refs = {}, keys = {};
    out.forEach(function (p) { refs[p.ref] = true; keys[sourceKey(p.source)] = true; });
    (incoming || []).forEach(function (p) {
      var key = sourceKey(p.source);
      if (refs[p.ref] || keys[key]) return;
      refs[p.ref] = true;
      keys[key] = true;
      out.push({ ref: p.ref, name: p.name, size_mm: p.size_mm, triangles: p.triangles, source: p.source,
                 footprints: p.footprints, quantity: 1 });
    });
    return out;
  }

  // An integer quantity from 1 to max; blank or not a number is 1.
  function clampQuantity(value, max) {
    var n = Math.floor(Number(value === '' || value == null ? NaN : value));
    if (!isFinite(n) || n < 1) return 1;
    return Math.min(n, max);
  }

  // After a refresh: each part of `current` that was `sent` takes its new ref, size and
  // footprints, keeping its quantity. The answer is in the order sent; when a part failed
  // and the counts differ, parts are matched by source instead, and an unmatched part keeps
  // its old ref (still readable until it expires). Parts removed meanwhile stay removed.
  function swapRefreshed(current, sent, incoming) {
    var byRef = {};
    if (incoming.length === sent.length) {
      sent.forEach(function (p, i) { byRef[p.ref] = incoming[i]; });
    } else {
      var byKey = {};
      incoming.forEach(function (p) { byKey[sourceKey(p.source)] = p; });
      sent.forEach(function (p) { if (byKey[sourceKey(p.source)]) byRef[p.ref] = byKey[sourceKey(p.source)]; });
    }
    return current.map(function (p) {
      var n = byRef[p.ref];
      if (!n) return p;
      return { ref: n.ref, name: n.name, size_mm: n.size_mm, triangles: n.triangles, source: n.source,
               footprints: n.footprints, quantity: p.quantity };
    });
  }

  // A refresh gives a part a new ref: its copies follow it and keep their places.
  // `before` and `after` are the part lists, index for index.
  function renameCopies(copies, before, after) {
    var renamed = {};
    before.forEach(function (p, i) { if (after[i] && after[i].ref !== p.ref) renamed[p.ref] = after[i].ref; });
    return copies.map(function (c) { return renamed[c.ref] ? Object.assign({}, c, { ref: renamed[c.ref] }) : c; });
  }

  // The browser's local time as YYYYMMDD-HHMM, for the delivered file's name.
  function localTimeStamp(date) {
    var pad = function (n) { return (n < 10 ? '0' : '') + n; };
    return date.getFullYear() + pad(date.getMonth() + 1) + pad(date.getDate()) + '-' +
           pad(date.getHours()) + pad(date.getMinutes());
  }

  // The slice request for the plate: each copy's placement only, in order, with the Setup
  // choices. Before /print/page has answered there is no choice, and the server takes the
  // first filament and process the team offers.
  function jobBody(st, now) {
    var body = {
      copies: st.copies.map(function (c) {
        return { ref: c.ref, orientation: c.orientation, angle: c.angle, x: c.x, y: c.y };
      }),
      overrides: Object.assign({}, st.overrides || {}),
      local_time: localTimeStamp(now || new Date()),
    };
    if (st.filament) body.filament = st.filament;
    if (st.process) body.process = st.process;
    return body;
  }

  // The four override menus' values as the server takes them: "Profile default" ('') sends
  // nothing, infill and walls are numbers, supports and brim strings.
  function overridesFromForm(values) {
    var out = {};
    OVERRIDE_KEYS.forEach(function (key) {
      var v = values ? values[key] : '';
      if (v === '' || v == null) return;
      out[key] = NUMERIC_OVERRIDES[key] ? Number(v) : String(v);
    });
    return out;
  }

  // A configuration error (an unknown printer, an empty list) stops every step and the slice.
  function canSlice(st) {
    return !(st && st.config && st.config.error);
  }

  // What a finished job is re-used for: the same body, whatever minute it is now.
  function jobKey(body) {
    if (!body) return 'sample';
    var same = Object.assign({}, body);
    delete same.local_time;
    return JSON.stringify(same);
  }

  function sizeText(size) { return size.x + ' x ' + size.y + ' x ' + size.z + ' mm'; }

  /* ------------------------------------------- print requests and errors */
  // fetch for a print route: the Flask session cookie and the page id header.
  function printFetch(url, opts) {
    var o = Object.assign({}, opts || {});
    o.credentials = 'same-origin';
    o.headers = Object.assign({}, o.headers || {}, { 'X-Print-Page': state.pid });
    return fetch(url, o);
  }

  // For the EventSource and polling URLs, which cannot carry a header.
  function withPid(url) {
    return url + (url.indexOf('?') >= 0 ? '&' : '?') + 'pid=' + encodeURIComponent(state.pid);
  }

  function postJson(url, body) {
    return printFetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' },
                             body: JSON.stringify(body) });
  }

  // The only code that writes an error line (#<step>-errors; the node test checks).
  // Shows the sentence, clears the step's status line and reports the message to the
  // server.
  function showError(step, message, code) {
    var errLine = $('#' + step + '-errors');
    if (errLine) errLine.textContent = message;
    var status = STATUS_LINES[step] ? $(STATUS_LINES[step]) : null;
    if (status) status.textContent = '';
    postJson('/print/client-event', { step: step, message: message, code: code || null })
      .catch(function () { /* reporting is best effort */ });
  }

  function clearError(step) {
    var errLine = $('#' + step + '-errors');
    if (errLine) errLine.textContent = '';
  }

  // Download, Drive or Send to Printer, as a deliver event for the current job.
  function reportDeliver(action, outcome) {
    if (!state.jobId) return;
    postJson('/print/deliver', { job_id: state.jobId, action: action, outcome: outcome })
      .catch(function () { /* reporting is best effort */ });
  }

  // How the page was opened, as the page event: the print page's first call. The answer
  // carries the plate and the limits the page checks.
  function postPageEvent() {
    var o = CFG.onshape || null;
    var isId = function (v) { return typeof v === 'string' && /^[0-9a-f]{24}$/.test(v); };
    var wvm = !o ? null : isId(o.workspaceId) ? 'w' : isId(o.versionId) ? 'v' : isId(o.microversionId) ? 'm' : null;
    postJson('/print/page', { source: state.source, theme: CFG.theme || null,
                              has_onshape_context: !!(o && isId(o.documentId)), wvm: wvm,
                              server: (o && o.server) || null })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (answer) {
        if (!answer) return;
        if (answer.plate) state.plate = answer.plate;
        if (answer.limits) state.limits = answer.limits;
        if (answer.overrides) state.overrideChoices = answer.overrides;
        if (answer.config) { state.config = answer.config; fillSetup(); }
        if (ONSHAPE && HAS_DOM && $('#btn-refresh-parts')) renderParts();   // the quantity's max
        if (ONSHAPE && state.step === 'layout') enterLayout();
      })
      .catch(function () { /* the page keeps its defaults */ });
  }

  /* ----------------------------------------------------------------- setup */
  // What Setup shows: /print/page's configuration, or, until it answers (or if it never
  // does), the default names from /print/part.
  function setupConfig() {
    if (state.config) return state.config;
    var i = state.info;
    if (!i) return null;
    return { printer: i.printer.name, filaments: [i.filament.name], processes: [i.process.name],
             allow_overrides: false, warnings: [], error: null, is_default: true };
  }

  function fillSelect(select, options, keep) {
    select.innerHTML = '';
    options.forEach(function (o) {
      var opt = document.createElement('option');
      opt.value = o.value;
      opt.textContent = o.text;
      select.appendChild(opt);
    });
    if (keep != null && options.some(function (o) { return o.value === keep; })) select.value = keep;
  }

  function nameOptions(names) { return names.map(function (n) { return { value: n, text: n }; }); }

  // Fills Setup from setupConfig(). Safe to call again: a choice still offered is kept.
  function fillSetup() {
    var c = setupConfig();
    if (!c || !HAS_DOM) return;
    $('#p-printer').textContent = c.printer;
    if (c.filaments.indexOf(state.filament) < 0) state.filament = c.filaments[0] || null;
    if (c.processes.indexOf(state.process) < 0) state.process = c.processes[0] || null;
    fillSelect($('#p-filament'), nameOptions(c.filaments), state.filament);
    fillSelect($('#p-process'), nameOptions(c.processes), state.process);
    // The upload mode slices the sample part with the default set: nothing to choose (R8).
    $('#p-filament').disabled = !ONSHAPE || !c.filaments.length;
    $('#p-process').disabled = !ONSHAPE || !c.processes.length;
    fillOverrides(ONSHAPE && c.allow_overrides && !c.error ? state.overrideChoices : null);
    var notes = $('#setup-notes');
    notes.innerHTML = '';
    (c.warnings || []).forEach(function (w) {
      var line = document.createElement('div');
      line.textContent = w;
      notes.appendChild(line);
    });
    notes.hidden = !(c.warnings && c.warnings.length);
    if (c.error) {
      if ($('#setup-errors').textContent !== c.error) showError('setup', c.error, 'config');
    } else clearError('setup');
    if (state.step !== 'preview') $('#btn-next').disabled = !canLeave(state.step);
    updateSummary();
  }

  // The four override menus, each starting at "Profile default"; hidden without choices.
  function fillOverrides(choices) {
    var box = $('#p-overrides');
    if (!box) return;
    box.hidden = !choices;
    if (!choices) { state.overrides = {}; return; }
    OVERRIDE_KEYS.forEach(function (key) {
      var select = $('#ov-' + key), keep = select.value;
      var options = [{ value: '', text: 'Profile default' }].concat((choices[key] || []).map(function (v) {
        return { value: String(v), text: OVERRIDE_LABELS[key](v) };
      }));
      fillSelect(select, options, keep);
    });
    state.overrides = overridesFromForm(overrideValues());
  }

  function overrideValues() {
    var values = {};
    OVERRIDE_KEYS.forEach(function (key) { values[key] = $('#ov-' + key).value; });
    return values;
  }

  function bindSetup() {
    $all('input[name="mode"]').forEach(function (r) {
      r.addEventListener('change', function () {
        // Any CNC mode goes back to the page the student came from, which reloads
        // with its default mode selected.
        if (this.value !== 'print') location.href = CFG.returnUrl;
      });
    });
    $('#p-filament').addEventListener('change', function () { state.filament = this.value; updateSummary(); });
    $('#p-process').addEventListener('change', function () { state.process = this.value; updateSummary(); });
    OVERRIDE_KEYS.forEach(function (key) {
      $('#ov-' + key).addEventListener('change', function () { state.overrides = overridesFromForm(overrideValues()); });
    });
  }

  /* ----------------------------------------------------------------- parts */
  function fillParts() {
    var i = state.info, list = $('#parts-list');
    list.innerHTML = '';
    var li = document.createElement('li');
    li.className = 'part-item';
    var meta = document.createElement('div');
    meta.className = 'meta';
    var name = document.createElement('div');
    name.className = 'name';
    name.textContent = i.part.name;
    var dims = document.createElement('div');
    dims.className = 'dims';
    dims.textContent = i.part.size_mm.x + ' x ' + i.part.size_mm.y + ' x ' + i.part.size_mm.z + ' mm';
    meta.appendChild(name);
    meta.appendChild(dims);
    li.appendChild(meta);
    list.appendChild(li);
  }

  function maxQuantity() {
    return (state.limits && state.limits.max_quantity) || DEFAULT_MAX_QUANTITY;
  }

  // The browser-only helpers, loaded before this file in Onshape mode.
  function geometry() { return window.PenguinCAM.plateGeometry; }
  function layoutLib() { return window.PenguinCAM.printLayout; }

  function messenger() {
    return (typeof window !== 'undefined' && window.PenguinCAM && window.PenguinCAM.onshapeMessenger) || null;
  }

  // Onshape mode: one row per listed part with its size, a quantity and a remove button.
  function renderParts() {
    var list = $('#parts-list');
    list.innerHTML = '';
    state.parts.forEach(function (p) {
      var li = document.createElement('li');
      li.className = 'part-item';
      var meta = document.createElement('div');
      meta.className = 'meta';
      var name = document.createElement('div');
      name.className = 'name';
      name.textContent = p.name;
      name.title = p.name;
      var dims = document.createElement('div');
      dims.className = 'dims';
      dims.textContent = sizeText(p.size_mm);
      meta.appendChild(name);
      meta.appendChild(dims);
      var qty = document.createElement('input');
      qty.type = 'number';
      qty.className = 'part-qty';
      qty.min = '1';
      qty.max = String(maxQuantity());
      qty.step = '1';
      qty.value = String(p.quantity);
      qty.setAttribute('aria-label', 'Copies of ' + p.name);
      qty.addEventListener('change', function () {
        p.quantity = clampQuantity(qty.value, maxQuantity());
        qty.value = String(p.quantity);
        updateSummary();
      });
      var remove = document.createElement('button');
      remove.type = 'button';
      remove.className = 'remove';
      remove.textContent = '\u00d7';
      remove.setAttribute('aria-label', 'Remove ' + p.name);
      remove.addEventListener('click', function () {
        state.parts = state.parts.filter(function (q) { return q.ref !== p.ref; });
        renderParts();
      });
      li.appendChild(meta);
      li.appendChild(qty);
      li.appendChild(remove);
      list.appendChild(li);
    });
    $('#btn-refresh-parts').disabled = state.parts.length === 0;
    if (state.step === 'parts') $('#btn-next').disabled = !canLeave('parts');
    updateSummary();
  }

  // The sign-in link sits next to the parts error line, never inside it.
  function showSigninLink(show) {
    var link = $('#parts-signin');
    if (!link) return;
    link.href = CFG.returnUrl;
    link.hidden = !show;
  }

  // Post selections to the server. Every error comes back as a sentence; each is shown,
  // and the last stays visible. A refresh swaps its answer in for the parts it sent.
  function requestParts(selections, via, sent) {
    var body = { context: CFG.onshape, via: via, selections: selections };
    if (via === 'refresh') body.replace = sent.map(function (p) { return p.ref; });
    $('#parts-status').textContent = via === 'refresh' ? 'Refreshing from Onshape\u2026' : 'Adding\u2026';
    return postJson('/print/onshape/parts', body)
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (j) { return { status: r.status, j: j }; });
      })
      .then(function (res) {
        $('#parts-status').textContent = '';
        if (res.status === 401 && res.j.code === 'signin_expired') {
          showError('parts', res.j.error, 'signin_expired');
          showSigninLink(true);
          return;
        }
        if (res.status === 429) { showError('parts', PARTS_RATE_MESSAGE, 'status_429'); return; }
        if (res.status !== 200) {
          showError('parts', res.j.error || 'Could not add the part (HTTP ' + res.status + ').', 'status_' + res.status);
          return;
        }
        showSigninLink(false);
        var errors = res.j.errors || [], parts = res.j.parts || [];
        if (via === 'refresh') {
          var before = state.parts;
          state.parts = swapRefreshed(before, sent, parts);
          state.copies = renameCopies(state.copies, before, state.parts);
        } else {
          state.parts = addParts(state.parts, parts);
        }
        if (errors.length) errors.forEach(function (e) { showError('parts', e.message, 'select'); });
        else clearError('parts');
        renderParts();
      })
      .catch(function (e) {
        $('#parts-status').textContent = '';
        showError('parts', 'Could not reach the server: ' + e.message, 'network');
      });
  }

  function refreshParts() {
    var sent = state.parts.slice();
    if (!sent.length) return;
    requestParts(sent.map(function (p) { return { source: p.source, name: p.name }; }), 'refresh', sent);
  }

  function bindParts() {
    if (!ONSHAPE) return;
    window.PenguinCAM.onPartSelections = function (selections, via) { requestParts(selections, via); };
    $('#btn-add-from-document').addEventListener('click', function () {
      var m = messenger();
      if (m) m.openDialog();
    });
    $('#btn-refresh-parts').addEventListener('click', refreshParts);
    renderParts();
    if (CFG.onshapeError) showError('parts', CFG.onshapeError, 'context');
  }

  /* ---------------------------------------------------------------- layout */
  function fillLayoutInfo() {
    var i = state.info, p = state.plate;
    if (ONSHAPE) {            // the configured printer's plate; the plate editor checks every copy
      if (!p) return;
      $('#info-printer-name').textContent = p.printer;
      $('#info-bed-size').textContent = (p.max[0] - p.min[0]) + ' x ' + (p.max[1] - p.min[1]) + ' mm, ' + p.height + ' mm tall';
      return;
    }
    $('#info-printer-name').textContent = i.printer.name;
    $('#info-bed-size').textContent = i.printer.bed_mm.x + ' x ' + i.printer.bed_mm.y + ' mm, ' + i.printer.height_mm + ' mm tall';
    state.fits = fitsBed(i.part.size_mm, i.printer.bed_mm, i.printer.height_mm);
    var tooBig = tooBigMessage(i.part.size_mm, i.printer.bed_mm, i.printer.height_mm);
    // This runs on every visit to Layout; a message already shown is not shown (and
    // reported) again.
    if (state.fits) clearError('layout');
    else if ($('#layout-errors').textContent !== tooBig) showError('layout', tooBig, 'too_big');
  }

  // In full-page grid mode the quadrant is a fixed cell, so the stylesheet's
  // `width: 100%; height: auto` makes the 600x400 canvas taller than the space left
  // under the heading, hint and info line and the quadrant scrolls inside itself.
  // Size it explicitly to what is left instead; the backing store stays 600x400.
  // In single-step mode the inline sizes come off so the stylesheet applies again.
  function fitLayoutCanvas() {
    if (!HAS_DOM) return;
    var wizard = $('#wizard'), canvas = $('#layout-canvas');
    if (!wizard || !canvas) return;
    // Below the stylesheet's 900 px breakpoint the grid is a single column of
    // `grid-auto-rows: minmax(220px, auto)` rows, so the section's height follows its
    // content, which includes the canvas. Measuring there would feed the fit back into
    // itself and ratchet the canvas down to the floor, one resize tick at a time. That
    // layout is not height-constrained, so treat it like single-step mode.
    if (!wizard.classList.contains('grid') || narrowGrid()) {
      canvas.style.removeProperty('width');
      canvas.style.removeProperty('height');
      return;
    }
    var section = canvas.closest('.step[data-step="layout"]');
    if (!section) return;
    var cs = getComputedStyle(section);
    var padX = (parseFloat(cs.paddingLeft) || 0) + (parseFloat(cs.paddingRight) || 0);
    var errors = $('#layout-errors');
    // The too-big message, when shown, plus its own margin-top: offsetHeight stops at the
    // border box, and that 8 px margin would otherwise eat the slack below the canvas.
    var below = 0;
    if (errors && errors.offsetHeight) {
      below = errors.offsetHeight + (parseFloat(getComputedStyle(errors).marginTop) || 0);
    }
    // Rects, not offsetTop: the section is not positioned, so an offset difference spans
    // its border and padding-top and would subtract the top padding a second time.
    // sr.bottom is the border box, so the bottom border comes off as well as the padding,
    // and the limits are floored: a fractional layout plus a rounded-up size overflows the
    // quadrant by a pixel, which is enough for the browser to report an inner scroll.
    var sr = section.getBoundingClientRect(), cr = canvas.getBoundingClientRect();
    var edgeB = (parseFloat(cs.paddingBottom) || 0) + (parseFloat(cs.borderBottomWidth) || 0);
    var availW = Math.floor(section.clientWidth - padX);
    var availH = Math.floor((sr.bottom - edgeB) - cr.top - below - 8);   // 8 px slack
    var fit = fitCanvasSize(availW, availH, canvas.width, canvas.height);
    canvas.style.width = fit.w + 'px';
    canvas.style.height = fit.h + 'px';
  }

  // Top-down drawing: the bed as a rectangle, the part's footprint centred on it.
  function drawLayout() {
    fitLayoutCanvas();
    var canvas = $('#layout-canvas'), ctx = canvas.getContext('2d');
    var i = state.info;
    var W = canvas.width, H = canvas.height, pad = 30;
    var css = getComputedStyle(document.documentElement);
    ctx.fillStyle = css.getPropertyValue('--canvas-bg').trim() || '#11161d';
    ctx.fillRect(0, 0, W, H);
    if (!i) return;
    var bed = i.printer.bed_mm, size = i.part.size_mm;
    var scale = Math.min((W - 2 * pad) / bed.x, (H - 2 * pad) / bed.y);
    var bw = bed.x * scale, bh = bed.y * scale;
    var bx = (W - bw) / 2, by = (H - bh) / 2;
    ctx.strokeStyle = css.getPropertyValue('--canvas-grid').trim() || '#30363d';
    ctx.lineWidth = 1;
    ctx.strokeRect(bx, by, bw, bh);
    var g = 50 * scale;   // 50 mm grid
    ctx.beginPath();
    for (var x = bx + g; x < bx + bw - 0.5; x += g) { ctx.moveTo(x, by); ctx.lineTo(x, by + bh); }
    for (var y = by + g; y < by + bh - 0.5; y += g) { ctx.moveTo(bx, y); ctx.lineTo(bx + bw, y); }
    ctx.stroke();
    var pw = size.x * scale, ph = size.y * scale;
    // Theme colours, so a too-big part is red in the light theme too. The fill is the
    // same colour at 35% through globalAlpha rather than a second hardcoded rgba().
    var footprint = state.fits ? (css.getPropertyValue('--accent').trim() || '#2f81f7')
                               : (css.getPropertyValue('--danger').trim() || '#f85149');
    ctx.fillStyle = footprint;
    ctx.strokeStyle = footprint;
    ctx.lineWidth = 2;
    ctx.globalAlpha = 0.35;
    ctx.fillRect((W - pw) / 2, (H - ph) / 2, pw, ph);
    ctx.globalAlpha = 1;
    ctx.strokeRect((W - pw) / 2, (H - ph) / 2, pw, ph);
    ctx.fillStyle = css.getPropertyValue('--ink').trim() || '#e6edf3';
    ctx.font = '12px sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText(i.part.name + ' ' + size.x + ' x ' + size.y + ' mm', W / 2, (H + ph) / 2 + 16);
    ctx.textAlign = 'left';
    ctx.fillText(bed.x + ' x ' + bed.y + ' mm bed', bx + 4, by - 6);
  }

  /* ------------------------------------------------ layout: plate editor */
  // Onshape mode. The copies follow the Parts step's quantities; the first visit arranges
  // them. Every change re-runs the placement check and the sentence under the canvas.
  function partsMap() {
    var out = {};
    state.parts.forEach(function (p) { out[p.ref] = p; });
    return out;
  }

  function enterLayout() {
    if (!state.plate) {          // /print/page has not answered yet; it calls back here
      $('#btn-next').disabled = true;
      return;
    }
    fillLayoutInfo();
    state.copies = layoutLib().syncCopies(state.copies, state.parts, state.plate);
    if (state.copies.length && state.copies.every(function (c) { return c.placed === false; })) arrange();
    editor.select(-1);           // the copies may have been renumbered on the way
    layoutChanged('enter');
  }

  function arrange() {
    var result = geometry().arrangeCopies(state.copies, partsMap(), state.plate);
    var left = {};
    result.unplaced.forEach(function (i) { left[i] = true; });
    state.copies = result.copies.map(function (c, i) { return Object.assign({}, c, { placed: true, unplaced: !!left[i] }); });
  }

  // The client-event code for the sentence: the rule it names, or what it is about.
  function sentenceCode(sentence) {
    var lim = state.limits || {};
    if (sentence === lim.too_many_copies_message) return 'too_many_copies';
    if (sentence === lim.job_triangles_message) return 'job_triangles';
    if (sentence === (lim.one_plate_message || layoutLib().ONE_PLATE_MESSAGE)) return 'one_plate';
    if (sentence === layoutLib().PARKED_MESSAGE) return 'parked';
    return state.problems.length ? state.problems[0].rule : null;
  }

  // After every change: check, redraw, and (except mid-drag, which would report a sentence
  // per pointer move) show the sentence and gate Next. A sentence already shown is not
  // shown, and reported, again.
  function layoutChanged(kind) {
    var parts = partsMap();
    state.problems = state.copies.length ? geometry().checkPlacement(state.copies, parts, state.plate) : [];
    editor.draw();
    if (kind === 'move') return;
    state.layoutSentence = layoutLib().layoutSentence(state.problems, state.copies, state.limits, parts);
    if (!state.layoutSentence) clearError('layout');
    else if ($('#layout-errors').textContent !== state.layoutSentence) {
      showError('layout', state.layoutSentence, sentenceCode(state.layoutSentence));
    }
    if (state.step === 'layout') $('#btn-next').disabled = !canLeave('layout');
    updateCopyControls();
    updateSummary();
  }

  function updateCopyControls() {
    var i = editor.selected(), copy = state.copies[i];
    var name = $('#layout-copy-name');
    $all('#layout-copy button, #layout-copy input, #layout-copy select').forEach(function (el) { el.disabled = !copy; });
    name.classList.toggle('selected', !!copy);
    if (!copy) {
      name.textContent = 'Click a copy to change it.';
      $('#layout-angle').value = '';
      return;
    }
    name.textContent = geometry().copyLabels(state.copies.map(function (c) { return partsMap()[c.ref].name; }))[i];
    if (document.activeElement !== $('#layout-angle')) $('#layout-angle').value = String(copy.angle);
    $('#layout-flat').value = copy.orientation;
  }

  // The selected copy, replaced by change(copy).
  function editSelected(change) {
    var i = editor.selected();
    if (i < 0 || !state.copies[i]) return;
    state.copies[i] = change(state.copies[i]);
    layoutChanged('edit');
  }

  // Duplicate and delete change the part's quantity too, so going back to Parts and
  // returning keeps the plate as it is.
  function setQuantity(ref, delta) {
    state.parts = state.parts.map(function (p) {
      return p.ref === ref ? Object.assign({}, p, { quantity: p.quantity + delta }) : p;
    }).filter(function (p) { return p.quantity > 0; });
    if ($('#btn-refresh-parts')) renderParts();
  }

  function bindLayout() {
    if (!ONSHAPE) return;
    var L = layoutLib(), labels = geometry().ORIENTATION_LABELS;
    editor = L.createLayoutEditor({
      canvas: $('#layout-canvas'),
      getState: function () { return { copies: state.copies, parts: partsMap(), plate: state.plate, problems: state.problems }; },
      onChange: function (kind) { if (kind === 'select') updateCopyControls(); else layoutChanged(kind); },
    });
    var flat = $('#layout-flat');
    geometry().ORIENTATIONS.forEach(function (o) {
      var opt = document.createElement('option');
      opt.value = o;
      opt.textContent = labels[o];
      flat.appendChild(opt);
    });
    $('#btn-rot-minus').addEventListener('click', function () { editSelected(function (c) { return L.rotateCopy(c, -90); }); });
    $('#btn-rot-plus').addEventListener('click', function () { editSelected(function (c) { return L.rotateCopy(c, 90); }); });
    $('#layout-angle').addEventListener('change', function () {
      var input = this;
      editSelected(function (c) { return L.setAngle(c, input.value); });
      var copy = state.copies[editor.selected()];
      if (copy) input.value = String(copy.angle);
    });
    flat.addEventListener('change', function () {
      var o = flat.value;
      editSelected(function (c) { return L.setOrientation(c, o); });
    });
    $('#btn-copy-dup').addEventListener('click', function () {
      var i = editor.selected();
      if (i < 0 || !state.plate) return;
      var ref = state.copies[i].ref;
      state.copies = L.duplicateCopy(state.copies, i, state.plate, partsMap());
      setQuantity(ref, 1);
      editor.select(state.copies.length - 1);
      layoutChanged('edit');
    });
    $('#btn-copy-del').addEventListener('click', function () {
      var i = editor.selected();
      if (i < 0) return;
      var ref = state.copies[i].ref;
      state.copies = L.deleteCopy(state.copies, i);
      setQuantity(ref, -1);
      editor.select(-1);
      layoutChanged('edit');
    });
    $('#btn-arrange').addEventListener('click', function () {
      if (!state.plate || !state.copies.length) return;
      arrange();
      layoutChanged('arrange');
    });
    updateCopyControls();
  }

  /* ---------------------------------------------------------- summary chips */
  function updateSummary() {
    var box = $('#wiz-summary'), i = state.info, c = setupConfig(), chips = [];
    var idx = STEPS.indexOf(state.step);
    if (c && idx >= STEPS.indexOf('parts')) {
      [c.printer, state.filament, state.process].forEach(function (name) { if (name) chips.push(name); });
    }
    if (ONSHAPE && idx >= STEPS.indexOf('layout') && state.parts.length) {
      // On and after Layout the plate's copies; duplicate and delete keep quantities equal.
      var copies = state.copies.length || state.parts.reduce(function (n, p) { return n + p.quantity; }, 0);
      chips.push(state.parts.length + (state.parts.length === 1 ? ' part, ' : ' parts, ') +
                 copies + (copies === 1 ? ' copy' : ' copies'));
    } else if (!ONSHAPE && i && idx >= STEPS.indexOf('layout')) chips.push('1 part');
    if (idx >= STEPS.indexOf('preview') && state.record && state.record.summary) {
      var s = state.record.summary;
      if (s.print_time_s != null) chips.push(formatDuration(s.print_time_s));
      if (s.filament_g != null) chips.push(s.filament_g + ' g');
    }
    box.hidden = chips.length === 0;
    box.innerHTML = '';
    chips.forEach(function (c) {
      var span = document.createElement('span');
      span.className = 'chip';
      span.textContent = c;
      box.appendChild(span);
    });
  }

  /* -------------------------------------------------------------- step nav */
  function gotoStep(name) {
    var leaving = state.step;
    state.step = name;
    var gridMode = $('#wizard').classList.contains('grid');
    $all('.step').forEach(function (s) {
      var isCurrent = s.getAttribute('data-step') === name;
      if (gridMode) { s.hidden = false; s.classList.toggle('current', isCurrent); }
      else { s.hidden = !isCurrent; s.classList.remove('current'); }
    });
    $all('#stepbar li').forEach(function (li) {
      var s = li.getAttribute('data-step');
      li.classList.toggle('active', s === name);
      li.classList.toggle('done', STEPS.indexOf(s) < STEPS.indexOf(name));
    });
    var idx = STEPS.indexOf(name);
    $('#btn-back').disabled = idx === 0;
    var isPreview = name === 'preview';
    $('#btn-next').hidden = isPreview;
    $('#final-action').hidden = !isPreview;
    if (leaving === 'preview' && !isPreview) stopFollowing();
    var m = ONSHAPE ? messenger() : null;
    if (m && leaving === 'parts' && name !== 'parts') m.leaveParts();
    if (m && name === 'parts' && (leaving !== 'parts' || !m.isActive())) m.enterParts();
    if (name === 'layout' && ONSHAPE) {
      enterLayout();
      $('#btn-next').disabled = !canLeave('layout');
    } else if (name === 'layout') {
      if (state.info) { fillLayoutInfo(); drawLayout(); }
      $('#btn-next').disabled = !state.fits;
    } else {
      $('#btn-next').disabled = !canLeave(name);
    }
    if (isPreview) enterPreview();
    updateSummary();
  }

  function canLeave(name) {
    if (!canSlice(state)) return false;
    if (name === 'parts' && ONSHAPE && state.parts.length === 0) return false;
    if (name === 'layout' && ONSHAPE) return !!state.plate && state.copies.length > 0 && !state.layoutSentence;
    if (name === 'layout' && !state.fits) return false;
    return true;
  }

  function navigateTo(name) {
    var target = STEPS.indexOf(name), cur = STEPS.indexOf(state.step);
    if (target < 0 || target === cur) return;
    if (target > cur) {
      for (var i = cur; i < target; i++) { if (!canLeave(STEPS[i])) return; }
    }
    gotoStep(name);
  }

  function bindNav() {
    $('#btn-next').addEventListener('click', function () {
      var i = STEPS.indexOf(state.step);
      if (i < STEPS.length - 1 && canLeave(state.step)) gotoStep(STEPS[i + 1]);
    });
    $('#btn-back').addEventListener('click', function () {
      var i = STEPS.indexOf(state.step);
      if (i > 0) gotoStep(STEPS[i - 1]);
    });
    $all('#stepbar li').forEach(function (li) {
      var go = function () { navigateTo(li.getAttribute('data-step')); };
      li.addEventListener('click', go);
      li.addEventListener('keydown', function (e) { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go(); } });
    });
  }

  /* ----------------------------------------------------------- data: part */
  function loadPart() {
    return printFetch('/print/part')
      .then(function (r) { if (!r.ok) throw new Error('part info ' + r.status); return r.json(); })
      .then(function (info) {
        state.info = info;
        fillSetup();                    // a no-op on the choices once /print/page has answered
        if (!ONSHAPE) fillParts();      // Onshape mode lists the student's parts instead
        fillLayoutInfo();
        // Part info arrives after gotoStep ran, so the layout gate needs re-applying:
        // a too-big part must disable Next even if the student is already on Layout.
        if (!ONSHAPE && state.step === 'layout') $('#btn-next').disabled = !state.fits;
        if (!ONSHAPE && (state.step === 'layout' || $('#wizard').classList.contains('grid'))) drawLayout();
        updateSummary();
      })
      .catch(function (e) {
        // Without part info there is nothing to slice, so Layout's gate stays shut
        // rather than letting the student walk on to Preview on stale defaults.
        if (ONSHAPE) return;     // the plate editor needs only the plate and the parts
        state.fits = false;
        if (state.step === 'layout') $('#btn-next').disabled = true;
        showError('layout', 'Could not load the part: ' + e.message, 'part_info');
      });
  }

  /* --------------------------------------------------------------- preview */
  function errorForStatus(status, body) {
    if (status === 429) return RATE_MESSAGE;
    if (status === 404) return EXPIRED_MESSAGE;
    if (body && body.error) return body.error;
    return 'Slicing failed (HTTP ' + status + ').';
  }

  function resetPreview() {
    $('#gen-status').textContent = '';
    clearError('preview');
    $('#preview-notes').textContent = '';
    $('#preview-notes').hidden = true;
    $('#preview-stats').textContent = '';
    $('#preview-result').hidden = true;
    $('#btn-do').disabled = true;
    state.token = null;
    state.record = null;
  }

  // Entering Preview submits a new slice: the plate's copies in Onshape mode, no body (the
  // sample part) in upload mode. A previous job's stream is closed first, and the generation
  // counter drops a response from a submit we have moved on from. Coming back to Preview on
  // the same page load with the same plate re-uses the job already in hand: the result is
  // good for an hour, and re-slicing would spend the three-a-minute budget.
  function enterPreview() {
    var gen = ++state.gen;
    stopFollowing();
    state.saveAction = preferredAction();
    setupFinalAction();
    var prev = state.record, prevId = state.jobId, prevToken = state.token;
    var body = ONSHAPE ? jobBody(state) : null, key = jobKey(body);
    resetPreview();
    if (!canSlice(state)) return;     // Setup shows why; Next and the step bar are blocked too
    if (key === state.jobKey) {
      if (prev && prev.state === 'done' && prevToken) { showDone(prev); return; }
      if (prev && prevId && prev.state !== 'failed') { showState(prev); followEvents(); return; }
    }
    state.jobId = null;
    state.jobKey = null;
    $('#gen-status').textContent = 'Queued';
    var request = body ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }
                       : { method: 'POST' };
    printFetch('/print-job', request)
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (j) { return { status: r.status, j: j }; });
      })
      .then(function (res) {
        if (gen !== state.gen || state.step !== 'preview') return;
        if (res.status !== 202) {
          var code = res.status === 400 && res.j.rule ? res.j.rule : 'status_' + res.status;
          failPreview(errorForStatus(res.status, res.j), code);
          return;
        }
        state.jobId = res.j.job_id;
        state.jobKey = key;
        state.jobCopies = body ? body.copies : null;
        followEvents();
      })
      .catch(function (e) {
        if (gen !== state.gen || state.step !== 'preview') return;
        failPreview('Could not reach the server: ' + e.message, 'network');
      });
  }

  function followEvents() {
    stopFollowing();    // two streams for one panel would fight over the status line
    var es = new EventSource(withPid('/print-job/' + encodeURIComponent(state.jobId) + '/events'));
    state.events = es;
    es.addEventListener('state', function (e) { showState(JSON.parse(e.data)); });
    es.addEventListener('done', function (e) { stopFollowing(); showDone(JSON.parse(e.data)); });
    es.addEventListener('failed', function (e) { stopFollowing(); showFailed(JSON.parse(e.data)); });
    // A proxy that cannot stream errors out before the terminal event: fall back to
    // polling the JSON record every two seconds so the job still finishes.
    es.onerror = function () {
      if (state.events !== es) return;    // already closed on purpose
      stopFollowing();
      startPolling();
    };
  }

  function startPolling() {
    state.poll = setInterval(function () {
      printFetch(withPid('/print-job/' + encodeURIComponent(state.jobId)))
        .then(function (r) {
          if (r.status === 404) { stopFollowing(); failPreview(EXPIRED_MESSAGE, 'expired'); return null; }
          if (!r.ok) { stopFollowing(); failPreview(errorForStatus(r.status, {}), 'status_' + r.status); return null; }
          return r.json();
        })
        .then(function (rec) {
          if (!rec) return;
          if (rec.state === 'done') { stopFollowing(); showDone(rec); }
          else if (rec.state === 'failed') { stopFollowing(); showFailed(rec); }
          else showState(rec);
        })
        .catch(function () { /* transient; try again next tick */ });
    }, 2000);
  }

  function stopFollowing() {
    if (state.events) { var es = state.events; state.events = null; es.close(); }
    if (state.poll) { clearInterval(state.poll); state.poll = null; }
  }

  function showState(rec) {
    state.record = rec;
    $('#gen-status').textContent = statusText(rec);
  }

  // A slice that cannot be saved: the error, and the save button off.
  function failPreview(message, code) {
    showError('preview', message, code);
    $('#btn-do').disabled = true;
  }

  function showFailed(rec) {
    state.record = rec;
    failPreview(rec.error || 'Slicing failed.', 'slice_failed');
  }

  function showDone(rec) {
    state.record = rec;
    state.token = rec.token;
    var s = rec.summary || {};
    $('#gen-status').textContent = 'Sliced.';
    $('#preview-stats').textContent = summaryLine(s);
    $('#preview-result').hidden = false;
    if (s.warnings && s.warnings.length) {
      $('#preview-notes').textContent = s.warnings.map(function (w) { return '⚠ ' + w; }).join('\n');
      $('#preview-notes').hidden = false;
    }
    $('#btn-do').disabled = false;
    updateSummary();
    showMesh();
  }

  // The viewer's trouble belongs on the panel next to the numbers, never instead of them.
  function noteViewerTrouble(message) {
    var box = $('#preview-notes');
    box.textContent = appendNote(box.textContent, message);
    box.hidden = false;
  }

  function showMesh() {
    // A blocked or failed CDN leaves the viewer's globals undefined. Say so here rather
    // than letting a ReferenceError land in the console with a silent panel.
    if (typeof PrintViewer === 'undefined' || typeof THREE === 'undefined') {
      noteViewerTrouble('3D preview unavailable (three.js failed to load).');
      return;
    }
    if (!ONSHAPE && !state.info) {
      noteViewerTrouble('Part information did not load; no 3D view.');
      return;
    }
    if (!viewer) {
      // A browser with no WebGL throws here. The slice is still good, so the panel
      // keeps its numbers and says why the picture is missing.
      try {
        viewer = new PrintViewer({ canvas: $('#print-canvas'), container: $('#viewer-container'),
                                   resetButton: $('#btn-reset-view'), emptyState: $('#viewer-empty') });
      } catch (e) {
        noteViewerTrouble('Could not start the 3D view: ' + e.message);
        return;
      }
    }
    if (ONSHAPE) { showPlate(); return; }
    printFetch('/print/part?stl=1')
      .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.arrayBuffer(); })
      .then(function (buf) { viewer.load(buf, state.info.printer); })
      .catch(function (e) {
        noteViewerTrouble('Could not load the 3D view: ' + e.message);
      });
  }

  // The plate as sliced: each distinct part's STL fetched once, every copy drawn with its
  // placement.
  function showPlate() {
    var copies = state.jobCopies || [], parts = partsMap(), plate = state.plate, refs = [];
    copies.forEach(function (c) { if (refs.indexOf(c.ref) < 0) refs.push(c.ref); });
    if (!plate || refs.some(function (ref) { return !parts[ref]; })) {
      noteViewerTrouble('The plate changed since this slice; no 3D view.');
      return;
    }
    Promise.all(refs.map(function (ref) {
      return printFetch('/print/parts/' + encodeURIComponent(ref) + '.stl')
        .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.arrayBuffer(); });
    }))
      .then(function (buffers) {
        var meshes = {};
        refs.forEach(function (ref, i) { meshes[ref] = buffers[i]; });
        viewer.loadPlate(meshes, copies, parts, plate);
      })
      .catch(function (e) {
        noteViewerTrouble('Could not load the 3D view: ' + e.message);
      });
  }

  function bindPreview() {
    if (typeof window !== 'undefined') {
      window.PenguinCAM.getJobState = function () { return state.record ? state.record.state : null; };
      // For static/printer_panel.js: the slice's download token (the same one Download
      // and Drive use), and the way its menu item records itself as the remembered
      // choice. The panel owns the item, its enabled state and the POST; this file owns
      // the split button's label and the localStorage preference.
      window.PenguinCAM.getDownloadToken = function () { return state.token; };
      window.PenguinCAM.choosePrinterAction = function () { chooseAction('printer'); };
      // For printer_panel.js: its errors go through the one error helper, and its sends
      // are reported as deliver events.
      window.PenguinCAM.showError = showError;
      window.PenguinCAM.reportDeliver = reportDeliver;
    }
  }

  /* ------------------------------------------------- final action (save) */
  // Same behaviour as the CNC wizard's split button (remembered choice, Drive only
  // when configured), but the code lives here on purpose.
  var SAVE_PREF_KEY = 'penguincam_save_action';
  function readSavePref() { try { return localStorage.getItem(SAVE_PREF_KEY); } catch (e) { return null; } }
  function writeSavePref(a) { try { localStorage.setItem(SAVE_PREF_KEY, a); } catch (e) {} }
  function actionLabel(a) {
    if (a === 'drive') return 'Send to Google Drive';
    if (a === 'printer') return 'Send to Printer';
    return 'Download Program';
  }

  function preferredAction() {
    var pref = readSavePref();
    if (pref === 'drive' && CFG.driveEnabled) return 'drive';
    if (pref === 'printer' && CFG.printerEnabled) return 'printer';
    return 'download';
  }

  function setupFinalAction() {
    var driveOk = !!CFG.driveEnabled;
    var printerOk = !!CFG.printerEnabled;
    if (state.saveAction === 'drive' && !driveOk) state.saveAction = 'download';
    if (state.saveAction === 'printer' && !printerOk) state.saveAction = 'download';
    // The caret opens the menu, which only has something to offer beyond Download when
    // Drive is configured or a printer is paired. printer_panel.js adds its own item to
    // the menu once the wizard is on Preview.
    var hasCaret = driveOk || printerOk;
    $('#btn-do-caret').hidden = !hasCaret;
    $('#final-action').classList.toggle('has-caret', hasCaret);
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
    if (!state.token) return;
    if (a === 'printer') {
      // The panel checks the printer's state and posts the token itself.
      var panel = typeof window !== 'undefined' && window.PenguinCAMPrinterPanel;
      if (panel && typeof panel.send === 'function') panel.send();
      return;
    }
    if (a === 'drive') driveSave(state.token);
    else doDownload(state.token);
  }

  function doDownload(token) {
    window.open('/download/' + token, '_blank');
    $('#gen-status').textContent = 'Download started.';
    reportDeliver('download', 'started');
  }

  function driveSave(token) {
    var status = $('#gen-status');
    clearError('preview');
    status.textContent = 'Checking Google Drive…';
    fetch('/drive/status', { credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (st) {
        if (!st.enabled) { driveFailed('Google Drive is not configured for your team.', 'drive_config'); return; }
        if (!st.authenticated) {
          status.textContent = 'Complete Google sign-in in the popup…';
          var popup = window.open('/auth/login', 'penguincam_gauth', 'width=600,height=700');
          if (!popup) { driveFailed('Popup blocked — allow popups, then try again.', 'drive_popup'); return; }
          var iv = setInterval(function () { if (popup.closed) { clearInterval(iv); driveUpload(token); } }, 500);
          setTimeout(function () { clearInterval(iv); }, 180000);
          return;
        }
        driveUpload(token);
      })
      .catch(function (e) { driveFailed('Drive check failed: ' + e, 'drive_check'); });
  }

  // A Drive save that did not happen: the error, and a failed deliver event.
  function driveFailed(message, code) {
    showError('preview', message, code);
    reportDeliver('drive', 'failed');
  }

  function driveUpload(token) {
    var status = $('#gen-status'), btn = $('#btn-do');
    status.textContent = 'Uploading to Google Drive…';
    btn.disabled = true;
    fetch('/drive/upload/' + token, { method: 'POST', credentials: 'same-origin',
                                      headers: { 'Content-Type': 'application/json' }, body: '{}' })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (res) {
        btn.disabled = false;
        if (res.ok && res.j.success) {
          status.textContent = res.j.message || 'Saved to Google Drive.';
          reportDeliver('drive', 'ok');
        } else {
          driveFailed((res.j && res.j.message) || 'Drive upload failed.', 'drive_upload');
        }
      })
      .catch(function (e) { btn.disabled = false; driveFailed('Drive upload failed: ' + e, 'drive_upload'); });
  }

  function bindFinalAction() {
    $('#btn-do').addEventListener('click', function () { if (!this.disabled) performAction(state.saveAction); });
    $('#btn-do-caret').addEventListener('click', function (e) { e.stopPropagation(); var m = $('#do-menu'); m.hidden = !m.hidden; });
    $('#do-menu').addEventListener('click', function (e) {
      var li = e.target.closest('li[data-action]');
      if (li) chooseAction(li.getAttribute('data-action'));
    });
    document.addEventListener('click', function (e) { if (!e.target.closest('#final-action')) $('#do-menu').hidden = true; });
  }

  /* ------------------------------------------------------------------ init */
  function init() {
    postPageEvent();
    bindSetup();
    bindNav();
    bindPreview();
    bindFinalAction();
    bindParts();
    bindLayout();
    if (state.source === 'upload') {
      $('#wizard').classList.add('grid');
      var opt25 = $('#opt-mode-25d'); if (opt25) opt25.hidden = true;
      var r25 = $('input[name="mode"][value="2.5d"]'); if (r25) r25.disabled = true;
    }
    // A back-navigation or a browser form restore can leave a CNC radio checked that
    // never fires a change event, so re-assert that this page is the 3D print one.
    var cur = $('input[name="mode"][value="print"]');
    if (cur) cur.checked = true;
    // A page restored from the back/forward cache does not run init again and keeps the
    // radio the student clicked on the way out, so re-assert it on restore as well.
    window.addEventListener('pageshow', function (e) {
      if (!e.persisted) return;
      var back = $('input[name="mode"][value="print"]');
      if (back) back.checked = true;
    });
    // Resizing the window changes the quadrant, so refit the canvas; debounced because
    // a drag fires resize continuously and each redraw repaints the whole bed.
    window.addEventListener('resize', function () {
      if (resizeTimer) clearTimeout(resizeTimer);
      resizeTimer = setTimeout(function () {
        resizeTimer = null;
        if (ONSHAPE) { if (editor && state.step === 'layout') editor.draw(); }
        else if (state.info) drawLayout();
      }, 100);
    });
    gotoStep('setup');
    loadPart();
  }

  if (HAS_DOM) {
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
    else init();
  }

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = { formatDuration: formatDuration, fitsBed: fitsBed, summaryLine: summaryLine,
                       fitCanvasSize: fitCanvasSize,
                       printPageUrl: printPageUrl, statusText: statusText, tooBigMessage: tooBigMessage,
                       errorForStatus: errorForStatus, appendNote: appendNote, makePageId: makePageId,
                       addParts: addParts, clampQuantity: clampQuantity, swapRefreshed: swapRefreshed,
                       sourceKey: sourceKey, localTimeStamp: localTimeStamp, jobBody: jobBody,
                       jobKey: jobKey, overridesFromForm: overridesFromForm, canSlice: canSlice };
  }
})();

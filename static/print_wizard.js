/* PenguinCAM 3D print wizard (templates/print_wizard.html).
 * Separate from the CNC wizard on purpose: same look, own code. Talks to the
 * print blueprint (/print/part, /print-job, /print-job/<id>/events) and reuses
 * the download and Google Drive routes. Pure helpers are exported for node tests.
 */
(function () {
  'use strict';

  var HAS_DOM = typeof document !== 'undefined';
  var CFG = (typeof window !== 'undefined' && window.PenguinCAM) ||
            { source: 'upload', theme: 'dark', driveEnabled: false, returnUrl: '/app' };
  var STEPS = ['setup', 'parts', 'layout', 'preview'];
  var EXPIRED_MESSAGE = 'This slice has expired, go back and slice again.';
  var RATE_MESSAGE = 'Too many slices in a minute, try again shortly.';

  var state = {
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
  };
  var viewer = null;

  function $(sel) { return document.querySelector(sel); }
  function $all(sel) { return Array.prototype.slice.call(document.querySelectorAll(sel)); }

  /* ----------------------------------------------------------- pure helpers */
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

  /* ----------------------------------------------------------------- setup */
  function fillSetup() {
    var i = state.info;
    $('#p-printer').textContent = i.printer.name;
    $('#p-filament').textContent = i.filament.name;
    $('#p-process').textContent = i.process.name;
  }

  function bindSetup() {
    $all('input[name="mode"]').forEach(function (r) {
      r.addEventListener('change', function () {
        // Any CNC mode goes back to the page the student came from, which reloads
        // with its default mode selected.
        if (this.value !== 'print') location.href = CFG.returnUrl;
      });
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

  /* ---------------------------------------------------------------- layout */
  function fillLayoutInfo() {
    var i = state.info;
    $('#info-printer-name').textContent = i.printer.name;
    $('#info-bed-size').textContent = i.printer.bed_mm.x + ' x ' + i.printer.bed_mm.y + ' mm, ' + i.printer.height_mm + ' mm tall';
    state.fits = fitsBed(i.part.size_mm, i.printer.bed_mm, i.printer.height_mm);
    $('#layout-errors').textContent = state.fits ? '' : tooBigMessage(i.part.size_mm, i.printer.bed_mm, i.printer.height_mm);
  }

  // Top-down drawing: the bed as a rectangle, the part's footprint centred on it.
  function drawLayout() {
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

  /* ---------------------------------------------------------- summary chips */
  function updateSummary() {
    var box = $('#wiz-summary'), i = state.info, chips = [];
    var idx = STEPS.indexOf(state.step);
    if (i && idx >= STEPS.indexOf('parts')) chips.push(i.printer.name, i.filament.name, i.process.name);
    if (i && idx >= STEPS.indexOf('layout')) chips.push('1 part');
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
    if (name === 'layout') {
      if (state.info) { fillLayoutInfo(); drawLayout(); }
      $('#btn-next').disabled = !state.fits;
    } else {
      $('#btn-next').disabled = false;
    }
    if (isPreview) enterPreview();
    updateSummary();
  }

  function canLeave(name) {
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
    return fetch('/print/part', { credentials: 'same-origin' })
      .then(function (r) { if (!r.ok) throw new Error('part info ' + r.status); return r.json(); })
      .then(function (info) {
        state.info = info;
        fillSetup();
        fillParts();
        fillLayoutInfo();
        // Part info arrives after gotoStep ran, so the layout gate needs re-applying:
        // a too-big part must disable Next even if the student is already on Layout.
        if (state.step === 'layout') $('#btn-next').disabled = !state.fits;
        if (state.step === 'layout' || $('#wizard').classList.contains('grid')) drawLayout();
        updateSummary();
      })
      .catch(function (e) {
        // Without part info there is nothing to slice, so Layout's gate stays shut
        // rather than letting the student walk on to Preview on stale defaults.
        state.fits = false;
        if (state.step === 'layout') $('#btn-next').disabled = true;
        $('#layout-errors').textContent = 'Could not load the part: ' + e.message;
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
    $('#preview-errors').textContent = '';
    $('#preview-notes').textContent = '';
    $('#preview-notes').hidden = true;
    $('#preview-stats').textContent = '';
    $('#preview-result').hidden = true;
    $('#btn-do').disabled = true;
    state.token = null;
    state.record = null;
  }

  // Entering Preview submits a new slice. A previous job's stream is closed first,
  // and the generation counter drops a response from a submit we have moved on from.
  // Coming back to Preview on the same page load re-uses the job already in hand: the
  // result is good for an hour, and re-slicing would spend the three-a-minute budget.
  function enterPreview() {
    var gen = ++state.gen;
    stopFollowing();
    state.saveAction = preferredAction();
    setupFinalAction();
    var prev = state.record, prevId = state.jobId, prevToken = state.token;
    resetPreview();
    if (prev && prev.state === 'done' && prevToken) { showDone(prev); return; }
    if (prev && prevId && prev.state !== 'failed') { showState(prev); followEvents(); return; }
    $('#gen-status').textContent = 'Queued';
    fetch('/print-job', { method: 'POST', credentials: 'same-origin' })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (j) { return { status: r.status, j: j }; });
      })
      .then(function (res) {
        if (gen !== state.gen || state.step !== 'preview') return;
        if (res.status !== 202) { showError(errorForStatus(res.status, res.j)); return; }
        state.jobId = res.j.job_id;
        followEvents();
      })
      .catch(function (e) {
        if (gen !== state.gen || state.step !== 'preview') return;
        showError('Could not reach the server: ' + e.message);
      });
  }

  function followEvents() {
    stopFollowing();    // two streams for one panel would fight over the status line
    var es = new EventSource('/print-job/' + encodeURIComponent(state.jobId) + '/events');
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
      fetch('/print-job/' + encodeURIComponent(state.jobId), { credentials: 'same-origin' })
        .then(function (r) {
          if (r.status === 404) { stopFollowing(); showError(EXPIRED_MESSAGE); return null; }
          if (!r.ok) { stopFollowing(); showError(errorForStatus(r.status, {})); return null; }
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

  function showError(message) {
    $('#gen-status').textContent = '';
    $('#preview-errors').textContent = message;
    $('#btn-do').disabled = true;
  }

  function showFailed(rec) {
    state.record = rec;
    showError(rec.error || 'Slicing failed.');
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
    if (!state.info) {
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
    fetch('/print/part?stl=1', { credentials: 'same-origin' })
      .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.arrayBuffer(); })
      .then(function (buf) { viewer.load(buf, state.info.printer); })
      .catch(function (e) {
        noteViewerTrouble('Could not load the 3D view: ' + e.message);
      });
  }

  function bindPreview() {
    if (typeof window !== 'undefined') {
      window.PenguinCAM.getJobState = function () { return state.record ? state.record.state : null; };
    }
  }

  /* ------------------------------------------------- final action (save) */
  // Same behaviour as the CNC wizard's split button (remembered choice, Drive only
  // when configured), but the code lives here on purpose.
  var SAVE_PREF_KEY = 'penguincam_save_action';
  function readSavePref() { try { return localStorage.getItem(SAVE_PREF_KEY); } catch (e) { return null; } }
  function writeSavePref(a) { try { localStorage.setItem(SAVE_PREF_KEY, a); } catch (e) {} }
  function actionLabel(a) { return a === 'drive' ? 'Send to Google Drive' : 'Download Program'; }

  function preferredAction() {
    var pref = readSavePref();
    return (pref === 'drive' && CFG.driveEnabled) ? 'drive' : 'download';
  }

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
    if (!state.token) return;
    if (a === 'drive') driveSave(state.token);
    else doDownload(state.token);
  }

  function doDownload(token) {
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
          var iv = setInterval(function () { if (popup.closed) { clearInterval(iv); driveUpload(token); } }, 500);
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
    fetch('/drive/upload/' + token, { method: 'POST', credentials: 'same-origin',
                                      headers: { 'Content-Type': 'application/json' }, body: '{}' })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (res) {
        btn.disabled = false;
        if (res.ok && res.j.success) status.textContent = res.j.message || 'Saved to Google Drive.';
        else { errs.textContent = (res.j && res.j.message) || 'Drive upload failed.'; status.textContent = ''; }
      })
      .catch(function (e) { btn.disabled = false; errs.textContent = 'Drive upload failed: ' + e; status.textContent = ''; });
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
    bindSetup();
    bindNav();
    bindPreview();
    bindFinalAction();
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
    gotoStep('setup');
    loadPart();
  }

  if (HAS_DOM) {
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
    else init();
  }

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = { formatDuration: formatDuration, fitsBed: fitsBed, summaryLine: summaryLine,
                       printPageUrl: printPageUrl, statusText: statusText, tooBigMessage: tooBigMessage,
                       errorForStatus: errorForStatus, appendNote: appendNote };
  }
})();

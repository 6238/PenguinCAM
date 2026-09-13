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
    ctx.fillStyle = state.fits ? 'rgba(47, 129, 247, 0.35)' : 'rgba(248, 81, 73, 0.35)';
    ctx.strokeStyle = state.fits ? '#2f81f7' : '#f85149';
    ctx.lineWidth = 2;
    ctx.fillRect((W - pw) / 2, (H - ph) / 2, pw, ph);
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
        if (state.step === 'layout' || $('#wizard').classList.contains('grid')) drawLayout();
        updateSummary();
      })
      .catch(function (e) {
        $('#layout-errors').textContent = 'Could not load the part: ' + e.message;
      });
  }

  /* --------------------------------------------------------------- preview */
  // Filled in by the Preview flow (Task 2): enterPreview, stopFollowing, bindPreview,
  // bindFinalAction. Task 1 ships these minimal versions so the page runs.
  function enterPreview() {}
  function stopFollowing() {}
  function bindPreview() {}
  function bindFinalAction() {}

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
    gotoStep('setup');
    loadPart();
  }

  if (HAS_DOM) {
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
    else init();
  }

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = { formatDuration: formatDuration, fitsBed: fitsBed, summaryLine: summaryLine,
                       printPageUrl: printPageUrl, statusText: statusText, tooBigMessage: tooBigMessage };
  }
})();

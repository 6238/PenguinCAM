/* PenguinCAM -- printer status line and "Send to Printer" for the print wizard.
 *
 * This file is the printer half of the print wizard's Preview step.
 * print/templates/print_wizard.html loads it after print_wizard.js; it attaches to that
 * page's DOM (#stepbar,
 * #preview-stats, #preview-errors, #do-menu, #btn-do-caret, #final-action) and never
 * assumes any of it is present: a selector that misses is logged once, by name, and the
 * panel carries on without it.
 *
 * The contract with print_wizard.js, which owns the split button and the remembered
 * choice:
 *   - window.PenguinCAM.getDownloadToken() returns the slice's download token, the same
 *     one Download and Drive use. Without it the status line still renders and the
 *     "Send to Printer" item is not offered, because there would be nothing to send.
 *   - window.PenguinCAM.choosePrinterAction() is what an enabled click on the item calls:
 *     the wizard records 'printer' as the remembered choice, relabels the main button,
 *     closes the menu and calls performAction('printer'), which delegates to send() below.
 *   - window.PenguinCAM.printerEnabled says whether this session's team config carries a
 *     pairing code; the wizard uses it to show the caret at load and to accept 'printer'
 *     as the remembered choice.
 *   - window.PenguinCAM.showError(step, message, code) is the wizard's one error helper;
 *     the panel's refusals go through it. window.PenguinCAM.reportDeliver(action,
 *     outcome) records how a send ended (queued, refused, unreachable) as a deliver event.
 * With neither getter present nothing runs.
 *
 * Plain ES2017, one IIFE, no dependencies, no build step. The pure parts are exported on
 * window.PenguinCAMPrinterPanel for tests/printer_panel/printer_panel.test.mjs.
 */
(function () {
  'use strict';

  var STATUS_URL = '/printer/status';
  var JOBS_URL = '/printer/jobs';
  var POLL_MS = 5000;
  var PREVIEW_STEP = 'preview';
  var SETUP_GUIDE_URL =
    'https://github.com/6238/PenguinCAM/blob/main/docs/PRINTER_SETUP.md';
  // A finished job's outcome is shown for ten minutes (section 7). The relay already drops
  // it at that age; the check here only keeps a stale poll response from outliving it.
  var LAST_RESULT_TTL_SECONDS = 600;
  // The '-j8f3a2c1' the daemon bakes into the uploaded file name, anchored on the
  // extension so only a real suffix matches. Mirrors strip_job_suffix in printer_relay.py;
  // it is a fallback only, since the routes send display_name already stripped.
  var JOB_SUFFIX_RE = /-j[0-9a-fA-F]{7,8}(?=\.gcode\.3mf$)/;
  // The extension is noise on the status line: the status line reads "Printing sample_part",
  // not "Printing sample_part.gcode.3mf".
  var EXTENSION_RE = /(\.gcode)?\.3mf$/i;

  var win = typeof window !== 'undefined' ? window : null;
  var doc = win && win.document;

  /* ---------------------------------------------------------------- pure helpers ----- */

  function isNumber(value) {
    return typeof value === 'number' && isFinite(value);
  }

  /** The printer's reported file name with the job id taken back out. */
  function stripJobSuffix(name) {
    if (typeof name !== 'string') return '';
    return name.replace(JOB_SUFFIX_RE, '');
  }

  /** The name to show for a job or a snapshot: the server's display_name, else our own
   *  stripping of the raw name, with the extension taken off either way, else a phrase
   *  that still reads as a sentence. */
  function displayName(record, rawKey) {
    if (!record) return 'a file';
    var name = record.display_name;
    if (typeof name !== 'string' || !name) name = stripJobSuffix(record[rawKey]);
    if (typeof name !== 'string') return 'a file';
    return name.replace(EXTENSION_RE, '') || 'a file';
  }

  /** A pairing code in its display form: two groups of four separated by a dash. */
  function formatCode(code) {
    if (typeof code !== 'string') return '';
    var bare = code.replace(/-/g, '').toUpperCase();
    if (bare.length !== 8) return bare;
    return bare.slice(0, 4) + '-' + bare.slice(4);
  }

  function failedLastResult(payload) {
    var result = payload.last_result;
    if (!result || result.ok) return null;
    if (isNumber(result.age_seconds) && result.age_seconds > LAST_RESULT_TTL_SECONDS) {
      return null;
    }
    return result;
  }

  /**
   * The status line's text: the first matching rule below, in order.
   * `payload` is the JSON body of GET /printer/status.
   */
  function renderStatusText(payload) {
    if (!payload || !payload.paired) return 'No printer paired';

    if (payload.waiting) {
      var code = formatCode(payload.code);
      return code
        ? 'Pi with code ' + code + ' has not reported since the service started'
        : 'The Pi has not reported since the service started';
    }

    var status = payload.status;
    if (!status) return 'No printer reporting';

    if (!payload.online) {
      if (!isNumber(payload.age_seconds)) return 'Printer offline';
      return 'Printer offline, last seen ' +
        Math.max(0, Math.floor(payload.age_seconds / 60)) + ' min ago';
    }

    var job = payload.job;
    if (job && job.state === 'pending') return 'Sending to printer';
    if (job) return 'Printer starting ' + displayName(job, 'filename');

    var name = displayName(status, 'file');
    if (status.state === 'running') {
      var text = 'Printing ' + name;
      if (isNumber(status.percent)) text += ', ' + Math.round(status.percent) + ' percent';
      if (isNumber(status.remaining_min)) {
        text += ', ' + Math.round(status.remaining_min) + ' min left';
      }
      return text;
    }
    if (status.state === 'paused') return 'Paused, ' + name;

    var failed = failedLastResult(payload);
    if (failed) return 'Upload to printer failed: ' + (failed.reason || 'unknown error');

    if (status.state === 'unreachable') return 'Printer unreachable from the Pi';
    if (status.state === 'error') return 'Printer error: ' + (status.error || 'unknown');
    if (status.state === 'finished') return 'Finished ' + name;
    if (status.state === 'idle') return 'Printer idle';
    return 'Printer status unknown';
  }

  // The states the relay refuses a send in (section 7): offering the option in any of them
  // would only earn a 409.
  var BUSY_STATES = ['running', 'paused', 'error', 'unreachable'];

  /**
   * May "Send to Printer" be offered? Paired and online, no job in flight, and the snapshot
   * state is none of BUSY_STATES. Anything this check cannot see -- a stale slice, a race
   * with another student -- the relay refuses with a 409 whose reason the panel shows, so
   * this is never the last word.
   */
  function canSend(payload) {
    if (!payload || !payload.paired || !payload.online) return false;
    if (payload.job || !payload.status) return false;
    return BUSY_STATES.indexOf(payload.status.state) === -1;
  }

  /* ------------------------------------------------------------------ the bootstrap -- */

  var logged = {};
  function log(message) {
    if (logged[message]) return;
    logged[message] = true;
    if (win && win.console && win.console.info) win.console.info(message);
  }

  var bootstrap = win && win.PenguinCAM;
  // Full-page mode (source=upload) never shows the option or the status line and never
  // calls the printer routes, whatever cookies the browser holds: a browser that used the
  // Onshape panel earlier may still carry a session, and must not reach that team's
  // printer. With no bootstrap at all there is no wizard to attach to.
  var enabled = !!bootstrap && bootstrap.source !== 'upload';
  if (!bootstrap && win) {
    log('printer panel: no window.PenguinCAM, standing down');
  }

  /** Is the post-rebase getter there? Asked afresh every time rather than cached at load:
   *  print_wizard.js may well assign it from its own deferred init, which can run after
   *  this file's, and a snapshot taken then would keep the menu item away for good. */
  function hasSendGetter() {
    return !!bootstrap && typeof bootstrap.getDownloadToken === 'function';
  }

  function downloadToken() {
    if (!enabled || !hasSendGetter()) return null;
    try {
      return bootstrap.getDownloadToken() || null;
    } catch (e) {
      return null;
    }
  }

  /* ------------------------------------------------------------------------- the DOM -- */

  var lastPayload = null;
  var timer = null;
  var inFlight = false;

  function $(selector) {
    try {
      return doc ? doc.querySelector(selector) : null;
    } catch (e) {
      return null;
    }
  }

  /** The one status line element, created on first use right after the slice summary. */
  function statusLine() {
    var existing = $('#printer-status');
    if (existing) return existing;
    var anchor = $('#preview-stats');
    if (!anchor || !doc || !anchor.insertAdjacentElement) {
      log('printer panel: no #preview-stats to hang the status line on');
      return null;
    }
    var el = doc.createElement('div');
    el.id = 'printer-status';
    el.className = 'printer-status';
    anchor.insertAdjacentElement('afterend', el);
    return el;
  }

  /** Paint the line. The "has not reported" row links to the setup guide (section 11). */
  function paintStatus(payload) {
    var el = statusLine();
    if (!el) return;
    var text = renderStatusText(payload);
    el.textContent = text;
    if (payload && payload.paired && payload.waiting && doc.createTextNode) {
      var link = doc.createElement('a');
      link.href = SETUP_GUIDE_URL;
      link.target = '_blank';
      link.rel = 'noopener';
      link.textContent = 'Setup guide';
      el.appendChild(doc.createTextNode(' '));
      el.appendChild(link);
    }
  }

  /** Tell the wizard how a send ended (a deliver print event); absent in standalone use. */
  function reportDeliver(outcome) {
    if (bootstrap && typeof bootstrap.reportDeliver === 'function') {
      try { bootstrap.reportDeliver('printer', outcome); } catch (e) { /* best effort */ }
    }
  }

  function errorBox(message) {
    // The wizard's one error helper shows the sentence and reports it. The panel's own
    // write below is only the fallback for a page without it (the standalone tests).
    if (bootstrap && typeof bootstrap.showError === 'function') {
      bootstrap.showError('preview', message, 'printer');
      return;
    }
    var box = $('#preview-errors');
    if (!box || !doc) {
      // Never silently dropped: a refused send has a reason the student needs, so it goes
      // on the status line instead. Called after the repaint, so it is not overwritten.
      log('printer panel: no #preview-errors, showing the reason on the status line');
      setLineText(message);
      return;
    }
    // Only ever one printer error at a time: a second refused send replaces the first
    // rather than stacking up under the summary.
    var stale = box.querySelectorAll ? box.querySelectorAll('.printer-error') : [];
    for (var i = 0; i < stale.length; i++) {
      if (stale[i].parentNode) stale[i].parentNode.removeChild(stale[i]);
    }
    var line = doc.createElement('div');
    line.className = 'printer-error';
    line.textContent = message;
    box.appendChild(line);
    if (box.hidden) box.hidden = false;
  }

  /** Add "Send to Printer" to the split button's menu, once. */
  function menuItem() {
    var existing = $('#do-menu li[data-action="printer"]');
    if (existing) return existing;
    var menu = $('#do-menu');
    if (!menu || !doc) {
      log('printer panel: no #do-menu, "Send to Printer" not offered');
      return null;
    }
    if (!hasSendGetter()) {
      log('printer panel: window.PenguinCAM.getDownloadToken is missing, ' +
          '"Send to Printer" not offered');
      return null;
    }
    var li = doc.createElement('li');
    li.setAttribute('data-action', 'printer');
    li.setAttribute('role', 'menuitem');
    li.textContent = 'Send to Printer';
    menu.appendChild(li);
    revealCaret();
    // Capture phase, and stopImmediatePropagation: the wizard's own delegated handler
    // calls chooseAction(li.dataset.action) for every item, and it must not see a click
    // on the DISABLED item at all. An enabled click is handed to the wizard through
    // choosePrinterAction, which records 'printer' as the remembered choice, relabels the
    // main button, closes the menu and calls back into send(). A wizard without that hook
    // (an older print_wizard.js) gets the send done here instead.
    menu.addEventListener('click', function (event) {
      var target = event.target;
      while (target && target !== menu) {
        if (target.getAttribute && target.getAttribute('data-action') === 'printer') {
          event.preventDefault();
          event.stopImmediatePropagation();
          if (target.getAttribute('aria-disabled') !== 'true') {
            menu.hidden = true;
            if (bootstrap && typeof bootstrap.choosePrinterAction === 'function') {
              bootstrap.choosePrinterAction();
            } else {
              send();
            }
          }
          return;
        }
        target = target.parentNode;
      }
    }, true);
    return li;
  }

  /** The split button's caret is hidden by the wizard unless the menu has a second
   *  entry; once Send to Printer is in it, the caret has to be there to reach it. */
  function revealCaret() {
    var caret = $('#btn-do-caret');
    if (caret) caret.hidden = false;
    var holder = $('#final-action');
    if (holder && holder.classList) holder.classList.add('has-caret');
  }

  function paintMenuItem(payload) {
    var li = menuItem();
    if (!li) return;
    if (canSend(payload)) {
      li.removeAttribute('aria-disabled');
      li.removeAttribute('title');
    } else {
      li.setAttribute('aria-disabled', 'true');
      li.setAttribute('title', renderStatusText(payload));
    }
  }

  function paint(payload) {
    lastPayload = payload;
    paintStatus(payload);
    paintMenuItem(payload);
  }

  function setLineText(text) {
    var el = statusLine();
    if (el) el.textContent = text;
  }

  /** The moment a send leaves: the line reads "Sending to printer" and the item is
   *  disabled, so a second click inside the five-second poll window cannot earn a 409. */
  function markSending() {
    setLineText('Sending to printer');
    var li = menuItem();
    if (li) {
      li.setAttribute('aria-disabled', 'true');
      li.setAttribute('title', 'Sending to printer');
    }
  }

  /* -------------------------------------------------------------------- the polling -- */

  function fetchJSON(url, options) {
    var f = win && win.fetch;
    if (!f) return Promise.reject(new Error('no fetch'));
    return f(url, options);
  }

  function poll() {
    if (!enabled || inFlight) return;
    inFlight = true;
    fetchJSON(STATUS_URL, {
      credentials: 'same-origin',
      headers: { Accept: 'application/json' }
    }).then(function (response) {
      if (!response || !response.ok) throw new Error('status ' + (response && response.status));
      return response.json();
    }).then(function (payload) {
      inFlight = false;
      // A response that lands after the student left Preview, or after pagehide, paints
      // nothing: the step it belongs to is gone.
      if (timer === null) return;
      paint(payload);
    })['catch'](function () {
      // A poll that fails changes nothing on screen: the line keeps the last thing it
      // knew rather than flickering an error every five seconds on a flaky panel.
      inFlight = false;
    });
  }

  function start() {
    if (!enabled || timer !== null) return;
    poll();
    timer = win.setInterval(poll, POLL_MS);
  }

  function stop() {
    if (timer === null) return;
    win.clearInterval(timer);
    timer = null;
  }

  /* ----------------------------------------------------------------------- the send -- */

  function send() {
    // Exported, so this is a public entry point: after post-rebase edit 3 the wizard's
    // performAction('printer') calls it, and a browser in full-page mode that remembered
    // 'printer' would otherwise POST /printer/jobs with the Onshape cookies it still
    // carries. Full-page mode never reaches the printer routes, by any path.
    if (!enabled) return;
    var token = downloadToken();
    if (!token) {
      errorBox('Slice again');
      return;
    }
    markSending();
    fetchJSON(JOBS_URL, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify({ token: token })
    }).then(function (response) {
      if (response.ok) {        // 202 from the relay
        reportDeliver('queued');
        markSending();          // and the five-second polling takes it from here
        return null;
      }
      reportDeliver('refused');
      // Repaint first, then the message: with no #preview-errors the message lands on
      // the status line, and painting afterwards would wipe it.
      if (response.status === 404) {
        paint(lastPayload);
        errorBox('Slice again');
        return null;
      }
      return response.json()['catch'](function () { return {}; }).then(function (body) {
        paint(lastPayload);
        errorBox((body && body.error) || 'The printer refused the job');
      });
    })['catch'](function () {
      reportDeliver('unreachable');
      paint(lastPayload);
      errorBox('Could not reach the server');
    });
  }

  /* ---------------------------------------------------------------- the Preview step -- */

  /** Is the wizard on Preview right now? null when the DOM cannot say. */
  function onPreview() {
    var bar = $('#stepbar');
    if (bar && bar.querySelector) {
      var active = bar.querySelector('li.active[data-step]');
      if (active) {
        return (active.getAttribute('data-step') || '') === PREVIEW_STEP;
      }
    }
    var section = $('section.step[data-step="' + PREVIEW_STEP + '"]');
    if (section) {
      if (section.classList && section.classList.contains('current')) return true;
      return !section.hidden;
    }
    log('printer panel: neither #stepbar li.active[data-step] nor ' +
        'section.step[data-step="' + PREVIEW_STEP + '"] answers, so the current step ' +
        'cannot be read and the panel does not poll');
    return null;
  }

  function syncToStep() {
    var here = onPreview();
    if (here === true) start();
    else if (here === false) stop();
  }

  var observer = null;

  /** The page is going away: stop polling and let the observer go. NOT done in stop(),
   *  which also runs on every step change -- disconnecting there would mean the panel
   *  never noticed the student coming back to Preview. */
  function teardown() {
    stop();
    if (observer) {
      observer.disconnect();
      observer = null;
    }
  }

  function init() {
    if (!enabled || !doc) return;
    var bar = $('#stepbar');
    if (bar && win.MutationObserver) {
      observer = new win.MutationObserver(syncToStep);
      observer.observe(bar, { attributes: true, attributeFilter: ['class'], subtree: true });
    }
    if (win.addEventListener) {
      win.addEventListener('pagehide', teardown);
    }
    syncToStep();
  }

  if (enabled && doc) {
    if (doc.readyState === 'loading' && doc.addEventListener) {
      doc.addEventListener('DOMContentLoaded', init);
    } else {
      init();
    }
  }

  // Exported for tests/printer_panel/printer_panel.test.mjs. start() is a no-op unless the
  // panel is enabled, so exporting it costs the upload flow nothing.
  if (win) win.PenguinCAMPrinterPanel = {
    renderStatusText: renderStatusText,
    canSend: canSend,
    start: start,
    stop: stop,
    // For post-rebase edit 3: performAction('printer') delegates here.
    send: send
  };
})();

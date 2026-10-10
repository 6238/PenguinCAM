/* PenguinCAM test bed -- the checklist strip and the panel's message recorder (spec 6).
 *
 * Loaded only by print/templates/print_wizard.html, and only when the test bed is on:
 *   <script src="/testbed/panel.js" data-mode="record|replay">
 * It changes nothing of the print page except a fixed strip at the top, which:
 *   - offers the scenarios (GET /testbed/scenarios) and makes one current
 *     (POST /testbed/scenario);
 *   - shows the current step's instruction, a button for the step's action, Next step and
 *     Done;
 *   - records every message it sends, and every message the page receives through a
 *     listener added when this script loads, before any of the page's own filters.
 * When the print page does its own Onshape messaging (window.PenguinCAM.onshapeMessenger,
 * print_onshape.js), the strip defers to it: it sends no applicationInit, hides its ask and
 * dialog buttons (only the reference Ask with no count stays), and records the page's own
 * messages on their way out. Otherwise it sends applicationInit itself.
 * It also:
 *   - posts what it recorded at Next step and Done (POST /testbed/messages), and at Done
 *     asks the server to save the message log (POST /testbed/done).
 *
 * The Onshape context comes from the query of window.PenguinCAM.returnUrl, the panel URL
 * Onshape opened. Messages go to window.parent with that context's `server` as
 * targetOrigin. Plain ES5, one IIFE, no dependencies. window.PenguinCAMTestbed exposes the
 * recorded events and the strip's actions for the browser harness.
 */
(function () {
  'use strict';

  var started = Date.now();
  var script = document.currentScript;
  var mode = (script && script.getAttribute('data-mode')) || '';
  var STORAGE_KEY = 'penguincam-testbed';
  var LABELS = {
    'ask-part': 'Ask for a part',
    'ask-two-parts': 'Ask for two parts',
    'dialog-part': 'Open the part dialog',
    'dialog-parts': 'Open the parts dialog',
    'close-dialog': 'Close the dialog',
    'ask-unbounded': 'Ask with no count'
  };
  // The buttons hidden when the print page asks Onshape for parts itself.
  var PAGE_ASKS = { 'ask-part': true, 'ask-two-parts': true, 'dialog-part': true,
                    'dialog-parts': true, 'close-dialog': true };

  var pending = [];        // events not yet posted to the server
  var recorded = [];       // every event since the scenario was chosen, with its step
  var messageCount = 0;    // messageId testbed-<n>, n counting from 1 per page load
  var scenarios = [];
  var current = null;      // the current scenario object
  var stepIndex = 0;
  var ui = {};

  // ---------------------------------------------------------------- recording

  function plain(data) {
    try {
      return JSON.parse(JSON.stringify(data));
    } catch (e) {
      return String(data);
    }
  }

  function currentStepName() {
    var step = current && current.steps[stepIndex];
    return step ? step.name : null;
  }

  function record(dir, origin, data) {
    var event = { dir: dir, origin: origin, data: plain(data), t_ms: Date.now() - started };
    pending.push(event);
    recorded.push({ step: currentStepName(), event: event });
  }

  // Each received message is recorded once, before any of the page's own filters.
  function recordIn(e) {
    if (e.testbedRecorded) return;
    e.testbedRecorded = true;
    record('in', e.origin, e.data);
  }
  window.addEventListener('message', recordIn);

  // ---------------------------------------------------------------- Onshape context

  function parseQuery(url) {
    var query = {};
    var at = (url || '').indexOf('?');
    if (at < 0) return query;
    var pairs = url.slice(at + 1).split('#')[0].split('&');
    for (var i = 0; i < pairs.length; i++) {
      if (!pairs[i]) continue;
      var eq = pairs[i].indexOf('=');
      var key = eq < 0 ? pairs[i] : pairs[i].slice(0, eq);
      var value = eq < 0 ? '' : pairs[i].slice(eq + 1);
      try {
        query[decodeURIComponent(key)] = decodeURIComponent(value.replace(/\+/g, ' '));
      } catch (err) {
        query[key] = value;
      }
    }
    return query;
  }

  var cfg = window.PenguinCAM || {};
  // print_onshape.js loads before this script, so its messenger exists by now.
  var deferred = !!cfg.onshapeMessenger;
  var realParent = window.parent;
  var q = parseQuery(cfg.returnUrl);
  var context = {
    documentId: q.documentId || '',
    workspaceId: q.workspaceId || '',
    elementId: q.elementId || '',
    versionId: q.versionId || '',
    microversionId: q.microversionId || '',
    server: q.server || ''
  };

  // Only an https://*.onshape.com origin on the default port, which includes the fake
  // Onshape host, ever receives the document ids.
  var ONSHAPE_ORIGIN = /^https:\/\/([a-z0-9-]+\.)+onshape\.com$/;

  function send(message) {
    if (!ONSHAPE_ORIGIN.test(context.server) || realParent === window) {
      var why = 'Not inside Onshape (server ' + JSON.stringify(context.server) +
        '): nothing sent.';
      if (window.console) window.console.warn('[testbed] ' + why);
      showStatus(why);
      return;
    }
    realParent.postMessage(message, context.server);
    record('out', context.server, message);
  }

  // The print page posts through window.parent.postMessage. The HTML standard makes
  // window.parent replaceable, so a stand-in records each message and passes it on
  // unchanged: the message log then holds the page's requests, which the fake Onshape
  // host matches to answer them.
  if (deferred && realParent !== window) {
    try {
      window.parent = {
        postMessage: function (message, targetOrigin) {
          record('out', context.server || targetOrigin, message);
          realParent.postMessage(message, targetOrigin);
        }
      };
    } catch (e) { /* not replaceable here: the page's messages go out unrecorded */ }
    // print_onshape.js listens before this script loads, and its handler may answer at
    // once (a selection re-arms). Recording inside its handler keeps a message ahead of
    // the page's answer to it; the listener above still records anything this misses.
    var handle = cfg.onshapeMessenger.handleMessage;
    cfg.onshapeMessenger.handleMessage = function (e) {
      if (e) recordIn(e);
      return handle.apply(this, arguments);
    };
  }

  function withContext(message) {
    message.documentId = context.documentId;
    message.workspaceId = context.workspaceId;
    message.elementId = context.elementId;
    return message;
  }

  function nextMessageId() {
    messageCount += 1;
    return 'testbed-' + messageCount;
  }

  function messageFor(action) {
    switch (action) {
      case 'ask-part':
      case 'ask-two-parts':
        return withContext({
          messageName: 'requestSelection', messageId: nextMessageId(),
          entityTypeSpecifier: ['BODY'],
          requiredSelectionCount: action === 'ask-part' ? 1 : 2
        });
      case 'dialog-part':
        return withContext({ messageName: 'openSelectItemDialog', selectParts: true });
      case 'dialog-parts':
        return withContext({ messageName: 'openSelectItemDialog', selectParts: true,
                             selectMultiple: true });
      case 'close-dialog':
        return withContext({ messageName: 'closeSelectItemDialog' });
      case 'ask-unbounded':        // for reference (spec 11): no requiredSelectionCount
        return withContext({
          messageName: 'requestSelection', messageId: nextMessageId(),
          entityTypeSpecifier: ['BODY']
        });
      default:
        return null;
    }
  }

  function act(action) {
    var message = messageFor(action);
    if (message) send(message);
  }

  // ---------------------------------------------------------------- server

  function request(method, url, body, done) {
    var xhr = new XMLHttpRequest();
    xhr.open(method, url, true);
    xhr.setRequestHeader('Content-Type', 'application/json');
    xhr.onload = function () {
      var data = null;
      try { data = JSON.parse(xhr.responseText); } catch (e) { data = null; }
      if (xhr.status >= 400) {
        showStatus((data && data.error) || ('HTTP ' + xhr.status + ' from ' + url));
      }
      if (done) done(xhr.status < 400, data);
    };
    xhr.onerror = function () {
      showStatus('Could not reach ' + url);
      if (done) done(false, null);
    };
    xhr.send(body === undefined ? null : JSON.stringify(body));
  }

  function postPending(done) {
    var step = currentStepName();
    if (!step) {
      if (done) done(true);
      return;
    }
    var events = pending;
    pending = [];
    request('POST', '/testbed/messages', { step: step, events: events }, function (ok) {
      if (!ok) pending = events.concat(pending);      // kept for the next try
      if (done) done(ok);
    });
  }

  // ---------------------------------------------------------------- state

  function remember() {
    try {
      window.sessionStorage.setItem(STORAGE_KEY, JSON.stringify(
        { scenario: current && current.name, step: stepIndex }));
    } catch (e) { /* storage blocked: the strip starts at the first step next time */ }
  }

  function remembered() {
    try {
      return JSON.parse(window.sessionStorage.getItem(STORAGE_KEY)) || {};
    } catch (e) {
      return {};
    }
  }

  function find(name) {
    for (var i = 0; i < scenarios.length; i++) {
      if (scenarios[i].name === name) return scenarios[i];
    }
    return null;
  }

  function chooseScenario(name) {
    request('POST', '/testbed/scenario', { name: name }, function (ok) {
      if (!ok) return;
      current = find(name);
      stepIndex = 0;
      pending = [];
      recorded.length = 0;     // the same array the harness holds
      remember();
      showStatus('');
      render();
    });
  }

  function nextStep() {
    if (!current) return;
    postPending(function (ok) {
      if (!ok) return;
      if (stepIndex < current.steps.length - 1) stepIndex += 1;
      remember();
      render();
    });
  }

  function finish() {
    if (!current) return;
    postPending(function (ok) {
      if (!ok) return;
      request('POST', '/testbed/done', undefined, function (saved, data) {
        if (!saved) return;
        showStatus('Saved ' + current.name + (data && data.cassette ? ' with its cassette.' : '.'));
        current = null;
        stepIndex = 0;
        remember();
        render();
      });
    });
  }

  // ---------------------------------------------------------------- strip

  function el(tag, text, style) {
    var node = document.createElement(tag);
    if (text) node.textContent = text;
    if (style) node.style.cssText = style;
    return node;
  }

  var BUTTON = 'margin-left:8px;padding:2px 10px;font:inherit;cursor:pointer;';

  function showStatus(text) {
    if (ui.status) ui.status.textContent = text;
  }

  function render() {
    var step = current && current.steps[stepIndex];
    ui.picker.value = current ? current.name : '';
    ui.instruction.textContent = step
      ? (stepIndex + 1) + '/' + current.steps.length + ' ' + step.name + ': ' + step.instruction
      : 'Choose a scenario.';
    ui.action.hidden = !(step && LABELS[step.action]) || (deferred && !!PAGE_ASKS[step.action]);
    ui.action.textContent = step && LABELS[step.action] ? LABELS[step.action] : '';
    ui.next.disabled = !step || stepIndex >= current.steps.length - 1;
    ui.done.disabled = !current;
  }

  function buildStrip() {
    var strip = el('div', '', 'position:fixed;top:0;left:0;right:0;z-index:2147483647;' +
      'display:flex;align-items:center;flex-wrap:wrap;gap:4px;padding:6px 12px;' +
      'background:#332b23;color:#f1ebdf;border-bottom:2px solid #e08a52;' +
      'font:13px/18px sans-serif;');
    strip.id = 'testbed-strip';
    strip.appendChild(el('strong', 'Test bed ' + mode, 'margin-right:8px;color:#e08a52;'));
    ui.picker = el('select', '', 'font:inherit;');
    ui.picker.id = 'testbed-scenario';
    ui.picker.appendChild(el('option', 'Scenario...'));
    ui.picker.options[0].value = '';
    for (var i = 0; i < scenarios.length; i++) {
      if (!scenarios[i].panel) continue;
      var option = el('option', scenarios[i].name);
      option.value = scenarios[i].name;
      ui.picker.appendChild(option);
    }
    ui.picker.onchange = function () {
      if (ui.picker.value) chooseScenario(ui.picker.value);
    };
    ui.instruction = el('span', '', 'margin-left:8px;flex:1 1 240px;');
    ui.action = el('button', '', BUTTON);
    ui.action.id = 'testbed-action';
    ui.action.type = 'button';
    ui.action.onclick = function () {
      var step = current && current.steps[stepIndex];
      if (step) act(step.action);
    };
    ui.next = el('button', 'Next step', BUTTON);
    ui.next.id = 'testbed-next';
    ui.next.type = 'button';
    ui.next.onclick = nextStep;
    ui.done = el('button', 'Done', BUTTON);
    ui.done.id = 'testbed-done';
    ui.done.type = 'button';
    ui.done.onclick = finish;
    ui.status = el('span', '', 'margin-left:8px;color:#a89e8b;');
    strip.appendChild(ui.picker);
    strip.appendChild(ui.instruction);
    strip.appendChild(ui.action);
    strip.appendChild(ui.next);
    strip.appendChild(ui.done);
    strip.appendChild(ui.status);
    document.body.appendChild(strip);
    document.body.style.paddingTop = strip.offsetHeight + 'px';
  }

  function start(listing) {
    scenarios = (listing && listing.scenarios) || [];
    var saved = remembered();
    current = listing && listing.current ? find(listing.current) : null;
    stepIndex = current && saved.scenario === current.name ? (saved.step || 0) : 0;
    if (current && stepIndex >= current.steps.length) stepIndex = 0;
    buildStrip();
    render();
  }

  window.PenguinCAMTestbed = {
    recorded: recorded,
    context: context,
    act: act,
    nextStep: nextStep,
    done: finish,
    currentStep: currentStepName,
    currentScenario: function () { return current ? current.name : null; },
    deferred: deferred
  };

  // applicationInit, unless the print page sends its own.
  if (!deferred && realParent !== window) {
    send(withContext({ messageName: 'applicationInit' }));
  }

  function load() {
    request('GET', '/testbed/scenarios', undefined, function (ok, data) {
      start(ok ? data : null);
    });
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', load);
  } else {
    load();
  }
})();

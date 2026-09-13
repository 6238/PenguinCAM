// Node tests for static/printer_panel.js -- the print wizard's printer status line and
// "Send to Printer" menu item.
//
// The script is a browser IIFE with no build step and no dependencies, so it is loaded
// here with `vm` into a deliberately tiny fake window/document (no jsdom: the pure parts,
// renderStatusText and canSend, need no DOM at all, and every querySelector in the script
// is written to tolerate null).
//
// Run with:  node --test 'tests/printer_panel/*.test.mjs'
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SCRIPT = path.join(HERE, '..', '..', 'static', 'printer_panel.js');
const SOURCE = fs.readFileSync(SCRIPT, 'utf8');

/** A deliberately small selector matcher: an optional tag name, any number of .classes
 *  and any number of [attr] or [attr="value"] tests, which is every selector
 *  printer_panel.js uses. Anything else throws rather than quietly matching nothing. */
function matches(el, selector) {
  const m = /^([a-z]+)?((?:\.[\w-]+)*)((?:\[[^\]]+\])*)$/.exec(selector.trim());
  if (!m) throw new Error('the fake DOM cannot parse the selector: ' + selector);
  if (m[1] && el.tagName !== m[1]) return false;
  const classes = m[2] ? m[2].slice(1).split('.') : [];
  if (!classes.every((name) => el.classList.contains(name))) return false;
  const attrs = m[3] ? m[3].slice(1, -1).split('][') : [];
  return attrs.every((attr) => {
    const parts = /^([\w-]+)(?:="([^"]*)")?$/.exec(attr);
    if (!parts) throw new Error('the fake DOM cannot parse the selector: ' + selector);
    const value = el.getAttribute(parts[1]);
    return parts[2] === undefined ? value !== null : value === parts[2];
  });
}

/** Every element below this one, in document order, as querySelectorAll searches. */
function descendants(el, out) {
  out = out || [];
  el.children.forEach((child) => { out.push(child); descendants(child, out); });
  return out;
}

/** One fake element: attributes, classes, children, text and listeners, and nothing else.
 *  The parts the panel's DOM wiring depends on -- insertAdjacentElement placing a sibling
 *  and classList really holding classes -- behave as a browser's do, so a test that asks
 *  where an element landed or which step is active is asking the fake a real question. */
function makeElement(doc, tag) {
  const classes = new Set();
  const el = {
    tagName: tag,
    id: '',
    textContent: '',
    hidden: false,
    href: '',
    attrs: {},
    children: [],
    listeners: [],
    parentNode: null,
    classList: {
      contains: (name) => classes.has(name),
      add: (name) => { classes.add(name); },
      remove: (name) => { classes.delete(name); },
    },
    setAttribute(name, value) {
      this.attrs[name] = String(value);
      if (name === 'id') this.id = String(value);
    },
    getAttribute(name) {
      return Object.prototype.hasOwnProperty.call(this.attrs, name) ? this.attrs[name] : null;
    },
    removeAttribute(name) { delete this.attrs[name]; },
    appendChild(child) {
      child.parentNode = this;
      this.children.push(child);
      doc.register(child);
      return child;
    },
    removeChild(child) {
      this.children = this.children.filter((c) => c !== child);
      child.parentNode = null;
      return child;
    },
    /** Really inserts: the node becomes this element's sibling, in the parent's list. */
    insertAdjacentElement(position, node) {
      const parent = this.parentNode;
      if (!parent) throw new Error('insertAdjacentElement on an element with no parent');
      const at = parent.children.indexOf(this);
      if (position === 'afterend') parent.children.splice(at + 1, 0, node);
      else if (position === 'beforebegin') parent.children.splice(at, 0, node);
      else throw new Error('the fake DOM does not implement position ' + position);
      node.parentNode = parent;
      doc.register(node);
      return node;
    },
    addEventListener(type, fn, capture) { this.listeners.push({ type, fn, capture }); },
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; },
    querySelectorAll(selector) {
      return descendants(this).filter((child) => matches(child, selector));
    },
    /** Deliver a click to this element's capture listeners, as the real menu would. */
    click(target) {
      const event = { target: target || this, defaultPrevented: false, stopped: false,
                      preventDefault() { this.defaultPrevented = true; },
                      stopImmediatePropagation() { this.stopped = true; } };
      this.listeners.filter((l) => l.type === 'click').forEach((l) => l.fn(event));
      return event;
    },
  };
  // A real className: the panel assigns it as a string, and the class it assigns is the
  // one a '.class' selector then has to find.
  Object.defineProperty(el, 'className', {
    enumerable: true,
    get: () => Array.from(classes).join(' '),
    set: (value) => {
      classes.clear();
      String(value).split(/\s+/).filter(Boolean).forEach((name) => classes.add(name));
    },
  });
  return el;
}

/** A fake document. Elements added by a test hang off body, so a test wires up only what
 *  it needs and the panel's own insertions land somewhere real. */
function makeDocument() {
  const doc = {
    readyState: 'complete',
    byId: {},
    register(el) { if (el && el.id) doc.byId[el.id] = el; },
    add(id, tag) {
      const el = makeElement(doc, tag || 'div');
      el.id = id;
      doc.body.appendChild(el);
      return el;
    },
    createElement: (tag) => makeElement(doc, tag),
    createTextNode: (text) => ({ textContent: text }),
    addEventListener: () => {},
    querySelector(selector) {
      const m = /^#([\w-]+)(?:\s+(.+))?$/.exec(selector);
      if (!m) return doc.body.querySelector(selector);
      const el = doc.byId[m[1]] || null;
      if (!el) return null;
      return m[2] ? el.querySelector(m[2]) : el;
    },
  };
  doc.body = makeElement(doc, 'body');
  return doc;
}

/** Load the script into a fake browser. With no `doc` every lookup misses, which is the
 *  case the script must survive: no #stepbar, no #preview-stats, no #do-menu. */
function loadPanel(bootstrap, doc) {
  const fetchCalls = [];
  const logs = [];
  const routes = {};
  // The callbacks the script hands to MutationObserver. A test fires them itself, which is
  // what the browser does when the wizard moves the 'active' class from one step to the next.
  const observed = [];
  const noop = () => {};
  doc = doc || makeDocument();
  const win = {
    document: doc,
    addEventListener: noop,
    fetch: (url, opts) => {
      fetchCalls.push([url, opts]);
      const handler = routes[url];
      if (!handler) return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) });
      return handler(opts);
    },
    setInterval: () => 1,
    clearInterval: noop,
    setTimeout: () => 1,
    clearTimeout: noop,
    MutationObserver: class {
      constructor(fn) { this.fn = fn; observed.push(fn); }
      observe() {}
      disconnect() { observed.splice(observed.indexOf(this.fn), 1); }
    },
    console: { log: (...a) => logs.push(a), info: (...a) => logs.push(a),
               warn: (...a) => logs.push(a), error: (...a) => logs.push(a) },
  };
  if (bootstrap !== undefined) win.PenguinCAM = bootstrap;
  const sandbox = { window: win, document: doc, console: win.console,
                    fetch: win.fetch, setInterval: win.setInterval,
                    clearInterval: win.clearInterval, setTimeout: win.setTimeout,
                    clearTimeout: win.clearTimeout,
                    MutationObserver: win.MutationObserver };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(SOURCE, sandbox, { filename: 'printer_panel.js' });
  const route = (url, handler) => { routes[url] = handler; };
  const mutate = () => observed.forEach((fn) => fn());
  return { panel: win.PenguinCAMPrinterPanel, fetchCalls, logs, win, doc, route, mutate,
           observed };
}

/** Respond to a fetch the way window.fetch does. */
function respond(status, body) {
  return () => Promise.resolve({ ok: status >= 200 && status < 300, status,
                                 json: () => Promise.resolve(body) });
}

const flush = () => new Promise((r) => setTimeout(r, 0));

const PANEL = loadPanel({ source: 'onshape', driveEnabled: true,
                          getDownloadToken: () => 'tok' }).panel;

/** A status payload in the shape printer_routes.py's /printer/status returns. */
function payload(over) {
  return Object.assign({ paired: true, waiting: false, online: true, age_seconds: 2,
                         status: null, job: null, last_result: null, code: '7K3M-Q9XW' },
                       over);
}
function snapshot(over) {
  return Object.assign({ state: 'idle', file: '', display_name: '', percent: null,
                         remaining_min: null, error: null }, over);
}

// --- section 4's table, row by row, in order ------------------------------------------

test('row 1: no pairing code', () => {
  assert.equal(PANEL.renderStatusText(payload({ paired: false, waiting: false, code: null })),
               'No printer paired');
});

test('row 1: a missing or unusable payload also reads as unpaired', () => {
  assert.equal(PANEL.renderStatusText(null), 'No printer paired');
  assert.equal(PANEL.renderStatusText({}), 'No printer paired');
});

test('row 2: paired but no daemon has ever reported, with the code', () => {
  assert.equal(PANEL.renderStatusText(payload({ waiting: true, online: false })),
               'Pi with code 7K3M-Q9XW has not reported since the service started');
});

test('row 2: an unformatted code is shown in two groups of four', () => {
  assert.equal(PANEL.renderStatusText(payload({ waiting: true, online: false, code: '7K3MQ9XW' })),
               'Pi with code 7K3M-Q9XW has not reported since the service started');
  assert.equal(PANEL.renderStatusText(payload({ waiting: true, online: false, code: '7k3mq9xw' })),
               'Pi with code 7K3M-Q9XW has not reported since the service started');
});

test('row 2: without a code field the text names no code', () => {
  assert.equal(PANEL.renderStatusText(payload({ waiting: true, online: false, code: null })),
               'The Pi has not reported since the service started');
});

test('row 3: a daemon has been heard but no snapshot is held', () => {
  assert.equal(PANEL.renderStatusText(payload({ online: false, age_seconds: null })),
               'No printer reporting');
});

test('row 4: the snapshot is stale', () => {
  assert.equal(PANEL.renderStatusText(payload({ online: false, age_seconds: 430,
                                                status: snapshot({ state: 'running' }) })),
               'Printer offline, last seen 7 min ago');
});

test('row 5: a job is pending', () => {
  assert.equal(PANEL.renderStatusText(payload({
    status: snapshot(),
    job: { id: 'j1', state: 'pending', filename: 'sample_part-j1a2b3c4.gcode.3mf',
           display_name: 'sample_part.gcode.3mf' } })),
    'Sending to printer');
});

test('row 6: a job has been handed to the daemon', () => {
  assert.equal(PANEL.renderStatusText(payload({
    status: snapshot(),
    job: { id: 'j1', state: 'handed_out', filename: 'sample_part-j1a2b3c4.gcode.3mf',
           display_name: 'sample_part' } })),
    'Printer starting sample_part');
});

test('row 7: printing', () => {
  assert.equal(PANEL.renderStatusText(payload({
    status: snapshot({ state: 'running', file: 'sample_part-j1a2b3c4.gcode.3mf',
                       display_name: 'sample_part', percent: 42, remaining_min: 31 }) })),
    'Printing sample_part, 42 percent, 31 min left');
});

test('row 7: printing with no percent or estimate yet', () => {
  assert.equal(PANEL.renderStatusText(payload({
    status: snapshot({ state: 'running', display_name: 'sample_part' }) })),
    'Printing sample_part');
});

test('row 8: paused', () => {
  assert.equal(PANEL.renderStatusText(payload({
    status: snapshot({ state: 'paused', display_name: 'sample_part', percent: 42 }) })),
    'Paused, sample_part');
});

test('row 9: the last job failed', () => {
  assert.equal(PANEL.renderStatusText(payload({
    status: snapshot({ state: 'idle' }),
    last_result: { ok: false, reason: 'connection refused', age_seconds: 12 } })),
    'Upload to printer failed: connection refused');
});

test('row 9: a failure with no reason still names the failure', () => {
  assert.equal(PANEL.renderStatusText(payload({
    status: snapshot({ state: 'idle' }),
    last_result: { ok: false, reason: null, age_seconds: 12 } })),
    'Upload to printer failed: unknown error');
});

test('row 9: a successful last result is not shown', () => {
  assert.equal(PANEL.renderStatusText(payload({
    status: snapshot({ state: 'idle' }),
    last_result: { ok: true, reason: null, age_seconds: 12 } })),
    'Printer idle');
});

test('row 9: a failure older than ten minutes is not shown', () => {
  assert.equal(PANEL.renderStatusText(payload({
    status: snapshot({ state: 'idle' }),
    last_result: { ok: false, reason: 'connection refused', age_seconds: 601 } })),
    'Printer idle');
});

test('row 10: unreachable', () => {
  assert.equal(PANEL.renderStatusText(payload({ status: snapshot({ state: 'unreachable' }) })),
               'Printer unreachable from the Pi');
});

test('row 11: printer error', () => {
  assert.equal(PANEL.renderStatusText(payload({
    status: snapshot({ state: 'error', error: 'HMS_0300_0100_0001_0004' }) })),
    'Printer error: HMS_0300_0100_0001_0004');
});

test('row 11: an error with no code', () => {
  assert.equal(PANEL.renderStatusText(payload({ status: snapshot({ state: 'error' }) })),
               'Printer error: unknown');
});

test('row 12: finished', () => {
  assert.equal(PANEL.renderStatusText(payload({
    status: snapshot({ state: 'finished', display_name: 'sample_part' }) })),
    'Finished sample_part');
});

test('row 13: idle', () => {
  assert.equal(PANEL.renderStatusText(payload({ status: snapshot({ state: 'idle' }) })),
               'Printer idle');
});

test('an unknown state is not claimed to be idle', () => {
  assert.equal(PANEL.renderStatusText(payload({ status: snapshot({ state: 'wat' }) })),
               'Printer status unknown');
});

// --- the table is ordered: the first matching row wins ---------------------------------

test('offline beats a pending job and a stale running snapshot', () => {
  assert.equal(PANEL.renderStatusText(payload({
    online: false, age_seconds: 130,
    status: snapshot({ state: 'running', display_name: 'x', percent: 3 }),
    job: { id: 'j1', state: 'pending', filename: 'x.gcode.3mf', display_name: 'x' } })),
    'Printer offline, last seen 2 min ago');
});

test('a pending job beats a running snapshot from the previous print', () => {
  assert.equal(PANEL.renderStatusText(payload({
    status: snapshot({ state: 'running', display_name: 'old', percent: 99 }),
    job: { id: 'j1', state: 'pending', filename: 'new.gcode.3mf', display_name: 'new' } })),
    'Sending to printer');
});

test('a paused snapshot beats a failed last result', () => {
  assert.equal(PANEL.renderStatusText(payload({
    status: snapshot({ state: 'paused', display_name: 'sample_part' }),
    last_result: { ok: false, reason: 'connection refused', age_seconds: 5 } })),
    'Paused, sample_part');
});

// --- the job-id suffix fallback --------------------------------------------------------

test('the job suffix is stripped when the server sends no display_name', () => {
  assert.equal(PANEL.renderStatusText(payload({
    status: snapshot({ state: 'finished', file: 'sample_part-j8f3a2c1.gcode.3mf' }) })),
    'Finished sample_part');
});

test('an eight-hex-digit job suffix is stripped too', () => {
  assert.equal(PANEL.renderStatusText(payload({
    status: snapshot({ state: 'finished', file: 'sample_part-j1a2b3c4d.gcode.3mf' }) })),
    'Finished sample_part');
});

test('a name that carries no job suffix keeps everything but the extension', () => {
  assert.equal(PANEL.renderStatusText(payload({
    status: snapshot({ state: 'finished', file: 'from_sd_card.3mf' }) })),
    'Finished from_sd_card');
});

test('the extension is stripped from the server display_name too', () => {
  assert.equal(PANEL.renderStatusText(payload({
    status: snapshot({ state: 'running', file: 'sample_part-j8f3a2c1.gcode.3mf',
                       display_name: 'sample_part.gcode.3mf', percent: 42,
                       remaining_min: 31 }) })),
    'Printing sample_part, 42 percent, 31 min left');
});

test('a name that is only an extension still reads as a sentence', () => {
  assert.equal(PANEL.renderStatusText(payload({
    status: snapshot({ state: 'finished', file: '.gcode.3mf' }) })),
    'Finished a file');
});

test('a job with only a filename falls back to stripping too', () => {
  assert.equal(PANEL.renderStatusText(payload({
    status: snapshot(),
    job: { id: 'j1', state: 'handed_out', filename: 'sample_part-j8f3a2c1.gcode.3mf' } })),
    'Printer starting sample_part');
});

test('a printing snapshot with no file name at all still reads as a sentence', () => {
  assert.equal(PANEL.renderStatusText(payload({
    status: snapshot({ state: 'running', percent: 7, remaining_min: 4 }) })),
    'Printing a file, 7 percent, 4 min left');
});

// --- canSend ---------------------------------------------------------------------------

test('canSend: an online, idle, unpaired-job printer accepts a send', () => {
  assert.equal(PANEL.canSend(payload({ status: snapshot({ state: 'idle' }) })), true);
});

test('canSend: finished is sendable', () => {
  assert.equal(PANEL.canSend(payload({ status: snapshot({ state: 'finished' }) })), true);
});

test('canSend: no pairing code', () => {
  assert.equal(PANEL.canSend(payload({ paired: false })), false);
});

test('canSend: no payload', () => {
  assert.equal(PANEL.canSend(null), false);
});

test('canSend: waiting for the Pi', () => {
  assert.equal(PANEL.canSend(payload({ waiting: true, online: false })), false);
});

test('canSend: offline', () => {
  assert.equal(PANEL.canSend(payload({ online: false, age_seconds: 300,
                                       status: snapshot({ state: 'idle' }) })), false);
});

test('canSend: running', () => {
  assert.equal(PANEL.canSend(payload({ status: snapshot({ state: 'running', percent: 2 }) })),
               false);
});

test('canSend: paused', () => {
  assert.equal(PANEL.canSend(payload({ status: snapshot({ state: 'paused' }) })), false);
});

test('canSend: a pending job is in flight', () => {
  assert.equal(PANEL.canSend(payload({ status: snapshot({ state: 'idle' }),
                                       job: { id: 'j1', state: 'pending',
                                              filename: 'x.gcode.3mf' } })), false);
});

test('canSend: a handed-out job is in flight', () => {
  assert.equal(PANEL.canSend(payload({ status: snapshot({ state: 'idle' }),
                                       job: { id: 'j1', state: 'handed_out',
                                              filename: 'x.gcode.3mf' } })), false);
});

// The relay refuses a send in these two states as well (section 7), so the item is
// disabled and carries the status line as its tooltip rather than earning a 409.
test('canSend: printer error', () => {
  assert.equal(PANEL.canSend(payload({ status: snapshot({ state: 'error', error: 'HMS' }) })),
               false);
});

test('canSend: printer unreachable from the Pi', () => {
  assert.equal(PANEL.canSend(payload({ status: snapshot({ state: 'unreachable' }) })), false);
});

test('canSend: a snapshot with no state at all is not sendable', () => {
  assert.equal(PANEL.canSend(payload({ status: null, online: true })), false);
});

// --- full-page mode --------------------------------------------------------------------

test('source=upload: start() never calls the printer routes', async () => {
  const { panel, fetchCalls } = loadPanel({ source: 'upload', driveEnabled: true,
                                            getDownloadToken: () => 'tok' });
  panel.start();
  panel.start();
  await new Promise((r) => setTimeout(r, 0));
  assert.equal(fetchCalls.length, 0);
  panel.stop();
});

test('source=upload: send() never calls the printer routes either', async () => {
  // send() is exported, so it is a public entry point: post-rebase, the wizard's
  // performAction('printer') calls it, and a browser that remembered 'printer' in the
  // Onshape panel still carries that session's cookies in full-page mode.
  const { panel, fetchCalls } = loadPanel({ source: 'upload',
                                            getDownloadToken: () => 'tok' });
  assert.doesNotThrow(() => panel.send());
  await flush();
  assert.equal(fetchCalls.length, 0);
});

test('no window.PenguinCAM at all: start() never calls the printer routes', async () => {
  const { panel, fetchCalls, logs } = loadPanel(undefined);
  panel.start();
  await new Promise((r) => setTimeout(r, 0));
  assert.equal(fetchCalls.length, 0);
  assert.equal(logs.length, 1, 'logs exactly once');
});

test('start() polls /printer/status once, with the session cookie', async () => {
  const { panel, fetchCalls } = loadPanel({ source: 'onshape',
                                            getDownloadToken: () => 'tok' });
  assert.equal(fetchCalls.length, 0, 'loading the script alone calls nothing');
  panel.start();
  await flush();
  // Objects made inside the vm realm have a different prototype, so compare structure.
  assert.equal(fetchCalls.length, 1);
  assert.equal(fetchCalls[0][0], '/printer/status');
  assert.deepEqual(JSON.parse(JSON.stringify(fetchCalls[0][1])),
                   { credentials: 'same-origin', headers: { Accept: 'application/json' } });
  panel.stop();
});

// --- the Preview DOM: the status line, the menu item and the send ----------------------

const SENDABLE = payload({ status: snapshot({ state: 'idle' }) });

/** A wizard on Preview: the summary line, the error box and the split button's menu.
 *  `omit` leaves one of those ids out, which is the case the panel has to survive until
 *  every selector has been checked against the real print_wizard.html. */
function previewPanel(bootstrap, status, omit) {
  const missing = omit || [];
  const has = (id) => missing.indexOf(id) === -1;
  const doc = makeDocument();
  const stats = has('preview-stats') ? doc.add('preview-stats') : null;
  const errors = has('preview-errors') ? doc.add('preview-errors') : null;
  const menu = has('do-menu') ? doc.add('do-menu') : null;
  const ctx = loadPanel(bootstrap || { source: 'onshape', getDownloadToken: () => 'tok' },
                        doc);
  ctx.route('/printer/status', respond(200, status || SENDABLE));
  return Object.assign(ctx, { stats, errors, menu,
                              item: () => doc.querySelector('#do-menu li[data-action="printer"]'),
                              line: () => doc.querySelector('#printer-status'),
                              errorText: () => ((errors && errors.children[0]) || {}).textContent });
}

test('the status line is created once, right after the slice summary', async () => {
  const p = previewPanel();
  p.panel.start();
  await flush();
  const line = p.line();
  assert.ok(line, 'a #printer-status element exists');
  assert.equal(line.className, 'printer-status');
  assert.equal(line.textContent, 'Printer idle');
  p.panel.stop();
  p.panel.start();                      // a second poll reuses the element it made
  await flush();
  assert.equal(p.line(), line);
  p.panel.stop();
});

test('the menu item is added once, as a menuitem, and enabled when a send is possible',
     async () => {
  const p = previewPanel();
  p.panel.start();
  await flush();
  const item = p.item();
  assert.ok(item);
  assert.equal(item.textContent, 'Send to Printer');
  assert.equal(item.getAttribute('role'), 'menuitem');
  assert.equal(item.getAttribute('aria-disabled'), null);
  assert.equal(p.menu.children.length, 1, 'added once');
  p.panel.stop();
});

test('a busy printer disables the item and shows the status line as its tooltip',
     async () => {
  const p = previewPanel(undefined, payload({
    status: snapshot({ state: 'running', display_name: 'sample_part', percent: 42,
                       remaining_min: 31 }) }));
  p.panel.start();
  await flush();
  const item = p.item();
  assert.equal(item.getAttribute('aria-disabled'), 'true');
  assert.equal(item.getAttribute('title'), 'Printing sample_part, 42 percent, 31 min left');
  p.panel.stop();
});

test('clicking the disabled item sends nothing and never reaches the page handler',
     async () => {
  const p = previewPanel(undefined, payload({ status: snapshot({ state: 'paused',
                                                                 display_name: 'x' }) }));
  p.panel.start();
  await flush();
  const event = p.menu.click(p.item());
  await flush();
  assert.equal(event.stopped, true, 'chooseAction never sees the click');
  assert.equal(event.defaultPrevented, true);
  assert.equal(p.menu.hidden, false, 'a refused click leaves the menu open');
  assert.deepEqual(p.fetchCalls.filter((c) => c[0] === '/printer/jobs'), []);
  p.panel.stop();
});

test('202: the token is posted, the menu closes and the item stays disabled', async () => {
  const p = previewPanel();
  p.route('/printer/jobs', respond(202, { ok: true, job_id: 'j8f3a2c1' }));
  p.panel.start();
  await flush();
  const event = p.menu.click(p.item());
  await flush();
  assert.equal(event.stopped, true);
  const post = p.fetchCalls.filter((c) => c[0] === '/printer/jobs');
  assert.equal(post.length, 1);
  assert.equal(post[0][1].method, 'POST');
  assert.equal(post[0][1].credentials, 'same-origin');
  assert.deepEqual(JSON.parse(post[0][1].body), { token: 'tok' });
  assert.equal(p.menu.hidden, true, 'the split button menu closes');
  assert.equal(p.line().textContent, 'Sending to printer');
  assert.equal(p.item().getAttribute('aria-disabled'), 'true');
  assert.equal(p.item().getAttribute('title'), 'Sending to printer');
  assert.equal(p.errors.children.length, 0, 'no error box entry');
  p.panel.stop();
});

test('404: the student is told to slice again, and the item comes back', async () => {
  const p = previewPanel();
  p.route('/printer/jobs', respond(404, { error: 'File not found or expired' }));
  p.panel.start();
  await flush();
  p.menu.click(p.item());
  await flush();
  assert.equal(p.errorText(), 'Slice again');
  assert.equal(p.errors.children[0].className, 'printer-error');
  assert.equal(p.item().getAttribute('aria-disabled'), null, 'repainted from the last poll');
  p.panel.stop();
});

test("409: the relay's reason is shown in the error box", async () => {
  const p = previewPanel();
  p.route('/printer/jobs', respond(409, { error: 'A job is already on its way to the printer' }));
  p.panel.start();
  await flush();
  p.menu.click(p.item());
  await flush();
  await flush();
  assert.equal(p.errorText(), 'A job is already on its way to the printer');
  assert.equal(p.item().getAttribute('aria-disabled'), null,
               'the item is repainted from the last poll, not left disabled');
  p.panel.stop();
});

test('409 with no body still says something', async () => {
  const p = previewPanel();
  p.route('/printer/jobs', () => Promise.resolve({ ok: false, status: 409,
                                                   json: () => Promise.reject(new Error('no body')) }));
  p.panel.start();
  await flush();
  p.menu.click(p.item());
  await flush();
  await flush();
  assert.equal(p.errorText(), 'The printer refused the job');
  p.panel.stop();
});

test('a network failure on the send says so', async () => {
  const p = previewPanel();
  p.route('/printer/jobs', () => Promise.reject(new Error('offline')));
  p.panel.start();
  await flush();
  p.menu.click(p.item());
  await flush();
  assert.equal(p.errorText(), 'Could not reach the server');
  assert.equal(p.item().getAttribute('aria-disabled'), null,
               'the item is repainted from the last poll, not left disabled');
  p.panel.stop();
});

test('only one printer error is shown at a time', async () => {
  const p = previewPanel();
  p.route('/printer/jobs', respond(404, {}));
  p.panel.start();
  await flush();
  p.menu.click(p.item());
  await flush();
  p.menu.click(p.item());
  await flush();
  assert.equal(p.errors.children.length, 1);
  p.panel.stop();
});

test('a poll that lands after the step is left paints nothing', async () => {
  const p = previewPanel();
  p.panel.start();
  p.panel.stop();
  await flush();
  assert.equal(p.line(), null, 'no status line was created');
  assert.equal(p.item(), null, 'no menu item was added');
});

// --- the post-rebase getter --------------------------------------------------------

test('without getDownloadToken the line still renders, the item does not, and it is '
     + 'logged once', async () => {
  const p = previewPanel({ source: 'onshape' });
  p.panel.start();
  await flush();
  p.panel.stop();
  p.panel.start();
  await flush();
  assert.equal(p.line().textContent, 'Printer idle', 'the status line still works');
  assert.equal(p.item(), null, 'nothing to send, so nothing is offered');
  const about = p.logs.filter((line) => /getDownloadToken/.test(line[0]));
  assert.equal(about.length, 1, 'logged once, not once per poll');
  p.panel.stop();
});

test('a getDownloadToken assigned after load is picked up on the next poll', async () => {
  const bootstrap = { source: 'onshape' };
  const p = previewPanel(bootstrap);
  p.panel.start();
  await flush();
  assert.equal(p.item(), null);
  bootstrap.getDownloadToken = () => 'late-token';   // print_wizard.js's deferred init
  p.panel.stop();
  p.panel.start();
  await flush();
  assert.ok(p.item(), 'the item appears without a reload');
  p.panel.stop();
});

// --- the DOM contract: where things land, and what happens when they are not there ------

test('the status line is inserted as the next sibling of the slice summary', async () => {
  const p = previewPanel();
  p.panel.start();
  await flush();
  const siblings = p.stats.parentNode.children;
  assert.equal(siblings[siblings.indexOf(p.stats) + 1], p.line(),
               '#printer-status sits directly after #preview-stats');
  p.panel.stop();
});

test('no #preview-stats: nothing is painted, and the selector is named once', async () => {
  const p = previewPanel(undefined, undefined, ['preview-stats']);
  p.panel.start();
  await flush();
  p.panel.stop();
  p.panel.start();
  await flush();
  assert.equal(p.line(), null, 'no status line anywhere');
  const about = p.logs.filter((line) => /#preview-stats/.test(line[0]));
  assert.equal(about.length, 1, 'named once, not once per poll');
  assert.ok(p.item(), 'the menu item is unaffected');
  p.panel.stop();
});

test('no #do-menu: the selector is named once and nothing throws', async () => {
  const p = previewPanel(undefined, undefined, ['do-menu']);
  p.panel.start();
  await flush();
  p.panel.stop();
  p.panel.start();
  await flush();
  assert.equal(p.line().textContent, 'Printer idle', 'the status line is unaffected');
  const about = p.logs.filter((line) => /#do-menu/.test(line[0]));
  assert.equal(about.length, 1);
  p.panel.stop();
});

test('409 with no #preview-errors: the reason goes to the status line instead', async () => {
  const p = previewPanel(undefined, undefined, ['preview-errors']);
  p.route('/printer/jobs', respond(409, { error: 'A job is already on its way to the printer' }));
  p.panel.start();
  await flush();
  p.menu.click(p.item());
  await flush();
  await flush();
  assert.equal(p.line().textContent, 'A job is already on its way to the printer',
               'the reason is never silently dropped');
  const about = p.logs.filter((line) => /#preview-errors/.test(line[0]));
  assert.equal(about.length, 1);
  p.panel.stop();
});

test('a 409 reason survives the repaint when the error box is there too', async () => {
  const p = previewPanel();
  p.route('/printer/jobs', respond(409, { error: 'The printer is busy' }));
  p.panel.start();
  await flush();
  p.menu.click(p.item());
  await flush();
  await flush();
  assert.equal(p.errorText(), 'The printer is busy');
  assert.equal(p.line().textContent, 'Printer idle', 'the line is back to the last poll');
  p.panel.stop();
});

// --- the Preview step, read from #stepbar ----------------------------------------------

/** A stepbar with two steps, the first of them active. */
function stepbarPanel() {
  const doc = makeDocument();
  doc.add('preview-stats');
  doc.add('do-menu');
  const bar = doc.add('stepbar');
  const steps = {};
  ['slice', 'preview'].forEach((name) => {
    const li = makeElement(doc, 'li');
    li.setAttribute('data-step', name);
    bar.appendChild(li);
    steps[name] = li;
  });
  steps.slice.classList.add('active');
  const ctx = loadPanel({ source: 'onshape', getDownloadToken: () => 'tok' }, doc);
  ctx.route('/printer/status', respond(200, SENDABLE));
  const activate = (name) => {
    Object.keys(steps).forEach((other) => steps[other].classList.remove('active'));
    steps[name].classList.add('active');
    ctx.mutate();
  };
  return Object.assign(ctx, { bar, steps, activate,
                              polls: () => ctx.fetchCalls.filter((c) => c[0] === '/printer/status').length,
                              line: () => doc.querySelector('#printer-status') });
}

test('#stepbar: polling starts on Preview and stops again when the step is left',
     async () => {
  const p = stepbarPanel();
  assert.equal(p.observed.length, 1, 'the stepbar is watched');
  await flush();
  assert.equal(p.polls(), 0, 'another step is active, so nothing is polled');

  p.activate('preview');
  await flush();
  assert.equal(p.polls(), 1, 'the active class moving to Preview starts the polling');
  assert.equal(p.line().textContent, 'Printer idle');

  p.activate('slice');
  await flush();
  assert.equal(p.polls(), 1, 'leaving Preview stops it');

  // start() is a no-op while the interval is still armed, so a second poll here is the
  // proof that leaving the step really called stop().
  p.activate('preview');
  await flush();
  assert.equal(p.polls(), 2, 'coming back starts it again');
  p.panel.stop();
});

test('no #stepbar and no Preview section: the step cannot be read, and it is said once',
     async () => {
  const p = previewPanel();
  p.mutate();
  const about = p.logs.filter((line) => /#stepbar/.test(line[0]));
  assert.equal(about.length, 1, 'named once, not once per mutation');
  assert.equal(p.fetchCalls.length, 0, 'and no polling is started on a guess');
});

test('an enabled click is handed to the wizard through choosePrinterAction, which owns '
     + 'the remembered choice and calls back into send()', async () => {
  const chosen = [];
  const bootstrap = { source: 'onshape', getDownloadToken: () => 'tok',
                      choosePrinterAction: () => { chosen.push('printer'); } };
  const p = previewPanel(bootstrap);
  p.panel.start();
  await flush();
  const event = p.menu.click(p.item());
  await flush();
  assert.equal(event.stopped, true, "the wizard's generic chooseAction never sees it");
  assert.equal(p.menu.hidden, true, 'the menu closes');
  assert.deepEqual(chosen, ['printer']);
  assert.deepEqual(p.fetchCalls.filter((c) => c[0] === '/printer/jobs'), [],
                   'the POST is left to send(), which the wizard calls');
  p.panel.stop();
});

test('without choosePrinterAction an enabled click still sends from here', async () => {
  const p = previewPanel();
  p.panel.start();
  await flush();
  p.menu.click(p.item());
  await flush();
  assert.equal(p.fetchCalls.filter((c) => c[0] === '/printer/jobs').length, 1);
  p.panel.stop();
});

test('adding the item reveals the split button caret the wizard hides by default',
     async () => {
  const p = previewPanel();
  const caret = p.doc.add('btn-do-caret', 'button');
  caret.hidden = true;
  const holder = p.doc.add('final-action');
  p.panel.start();
  await flush();
  assert.ok(p.item());
  assert.equal(caret.hidden, false);
  assert.equal(holder.classList.contains('has-caret'), true);
  p.panel.stop();
});

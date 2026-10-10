'use strict';
// The print page's own Onshape messaging (print/static/print_onshape.js). Answer shapes come
// from the synthetic message log testbed/tests/fixtures/messages/synthetic-print-selection.json
// (written by make_print_fixtures.py) until the real recordings land.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const o = require(path.join(__dirname, '..', '..', 'static', 'print_onshape.js'));

const LOG = JSON.parse(fs.readFileSync(path.join(__dirname, '..', '..', '..', 'testbed', 'tests', 'fixtures',
                                                 'messages', 'synthetic-print-selection.json'), 'utf8'));
const SERVER = 'https://cad.onshape.com';
const CONTEXT = { documentId: 'd00000000000000000000001', workspaceId: 'a00000000000000000000001',
                  versionId: '', microversionId: '', elementId: 'e00000000000000000000001', server: SERVER };

/** The data of the step's events in one direction. */
function step(name, dir) {
  const entry = LOG.steps.find((s) => s.step === name);
  assert.ok(entry, 'no step ' + name);
  return entry.events.filter((e) => e.dir === dir).map((e) => e.data);
}

function incoming(name) { return step(name, 'in')[0]; }

function harness() {
  const posted = [];
  const added = [];
  const messenger = o.createOnshapeMessenger({
    context: CONTEXT,
    post: (message, targetOrigin) => posted.push({ message, targetOrigin }),
    onSelections: (selections, via) => added.push({ selections, via }),
  });
  const deliver = (data, origin) => messenger.handleMessage({ origin: origin || SERVER, data });
  const names = () => posted.map((p) => p.message.messageName);
  return { messenger, posted, added, deliver, names };
}

test("every message carries the three ids and is posted to '*'", () => {
  const h = harness();
  h.messenger.init();
  h.messenger.enterParts();
  h.messenger.openDialog();
  h.messenger.leaveParts();
  assert.deepEqual(h.names(), ['applicationInit', 'requestSelection', 'openSelectItemDialog',
                               'stopRequest', 'closeSelectItemDialog']);
  h.posted.forEach(({ message, targetOrigin }) => {
    assert.equal(targetOrigin, '*');
    assert.equal(message.documentId, CONTEXT.documentId);
    assert.equal(message.workspaceId, CONTEXT.workspaceId);
    assert.equal(message.elementId, CONTEXT.elementId);
  });
});

test('isOnshapeOrigin accepts the server and onshape.com hosts only', () => {
  assert.equal(o.isOnshapeOrigin('https://cad.onshape.com', SERVER), true);
  assert.equal(o.isOnshapeOrigin('https://acme.onshape.com', SERVER), true);
  assert.equal(o.isOnshapeOrigin('https://onshape.com', SERVER), true);
  assert.equal(o.isOnshapeOrigin('http://cad.onshape.com', SERVER), false);
  assert.equal(o.isOnshapeOrigin('https://onshape.com.evil.io', SERVER), false);
  assert.equal(o.isOnshapeOrigin('https://evilonshape.com', SERVER), false);
  assert.equal(o.isOnshapeOrigin(undefined, SERVER), false);
  // The panel's own server counts even off onshape.com (it was validated on the server).
  assert.equal(o.isOnshapeOrigin('https://cad-testbed.example', 'https://cad-testbed.example'), true);
});

test('enterParts arms a count-1 solid body selection', () => {
  const h = harness();
  h.messenger.enterParts();
  const arm = h.posted[0].message;
  assert.equal(arm.messageName, 'requestSelection');
  assert.match(arm.messageId, /^penguincam-print-\d+$/);
  assert.equal(arm.filterType, 'simple');
  assert.deepEqual(arm.entityTypeSpecifier, ['BODY']);
  assert.deepEqual(arm.bodyTypeSpecifier, ['SOLID']);
  assert.equal(arm.requiredSelectionCount, 1);
  const expected = step('open parts', 'out')[1];
  ['filterType', 'entityTypeSpecifier', 'bodyTypeSpecifier', 'requiredSelectionCount']
    .forEach((k) => assert.deepEqual(arm[k], expected[k]));
  h.messenger.enterParts();
  assert.notEqual(h.posted[1].message.messageId, arm.messageId);   // a fresh messageId each time
});

test('an answer adds selections and re-arms', () => {
  const h = harness();
  h.messenger.enterParts();
  h.deliver(incoming('click a part by part id'));
  assert.deepEqual(h.added, [{ selections: [{ partId: 'JHD', selectionId: 'JHD' }], via: 'selection' }]);
  assert.deepEqual(h.names(), ['requestSelection', 'requestSelection']);
});

test('an empty answer re-arms and adds nothing', () => {
  const h = harness();
  h.messenger.enterParts();
  h.deliver(incoming('deselect'));
  assert.deepEqual(h.added, []);
  assert.deepEqual(h.names(), ['requestSelection', 'requestSelection']);
});

test('PENDING is ignored', () => {
  const h = harness();
  h.messenger.enterParts();
  h.deliver(incoming('open parts'));
  assert.deepEqual(h.added, []);
  assert.deepEqual(h.names(), ['requestSelection']);
});

test('answers while inactive are ignored', () => {
  const h = harness();
  h.deliver(incoming('click a part by part id'));
  h.messenger.enterParts();
  h.messenger.leaveParts();
  h.deliver(incoming('click a part by part id'));
  assert.deepEqual(h.added, []);
  assert.deepEqual(h.names(), ['requestSelection', 'stopRequest']);
});

test('messages from another origin are ignored', () => {
  const h = harness();
  h.messenger.enterParts();
  h.deliver(incoming('click a part by part id'), 'https://evil.example');
  h.deliver(incoming('pick in the dialog'), 'http://cad.onshape.com');
  assert.deepEqual(h.added, []);
  assert.deepEqual(h.names(), ['requestSelection']);
});

test('generic SELECTION is ignored', () => {
  const h = harness();
  h.messenger.enterParts();
  h.deliver(incoming('a generic selection event'));
  assert.deepEqual(h.added, []);
  assert.deepEqual(h.names(), ['requestSelection']);
});

test('dialog items arrive with via dialog, and dialog close re-arms', () => {
  const h = harness();
  h.messenger.enterParts();
  h.messenger.openDialog();
  const open = h.posted[1].message;
  assert.equal(open.selectParts, true);
  assert.equal(open.selectMultiple, true);
  h.deliver(incoming('pick in the dialog'));
  assert.deepEqual(h.added, [{ via: 'dialog', selections: [{
    documentId: 'd00000000000000000000002', versionId: 'b00000000000000000000002',
    elementId: 'e00000000000000000000003', elementConfiguration: '', partName: 'Box', idTag: 'idTagBox1',
    isSurface: false, isComposite: false, isSketch: false, isFlattenedBody: false }] }]);
  h.deliver(incoming('close the dialog'));
  assert.deepEqual(h.names(), ['requestSelection', 'openSelectItemDialog', 'requestSelection']);
  h.messenger.leaveParts();          // the dialog is closed now: no closeSelectItemDialog
  assert.deepEqual(h.names().slice(3), ['stopRequest']);
});

test('leaveParts stops the request and closes an open dialog', () => {
  const h = harness();
  h.messenger.enterParts();
  h.messenger.openDialog();
  h.messenger.leaveParts();
  assert.deepEqual(h.names(), ['requestSelection', 'openSelectItemDialog', 'stopRequest', 'closeSelectItemDialog']);
  // The dialog's close notice after leaving does not re-arm.
  h.deliver(incoming('close the dialog'));
  assert.equal(h.names().length, 4);
});

test('partSelectionsFromMessage accepts the known field names', () => {
  const read = (name) => o.partSelectionsFromMessage(incoming(name));
  assert.deepEqual(read('click a part by part id'), [{ partId: 'JHD', selectionId: 'JHD' }]);
  assert.deepEqual(read('click a part by selection id'), [{ selectionId: 'JKD' }]);
  assert.deepEqual(read('click a part by entity id'), [{ selectionId: 'JKD' }]);
  assert.deepEqual(read('click a part by body id'), [{ partId: 'JHD', name: 'Box' }]);
  assert.deepEqual(read('click a part in an assembly'),
    [{ selectionId: 'JHD', occurrencePath: ['MboxInstance1aaaa'] }]);
  assert.deepEqual(o.partSelectionsFromMessage({ selections: [{ occurrencePath: ['A', 'B'] }, { path: ['C'] },
                                                              { entityType: 'BODY' }, null, 'x'] }),
    [{ occurrencePath: ['A', 'B'] }, { occurrencePath: ['C'] }]);
  assert.deepEqual(o.partSelectionsFromMessage({}), []);
  assert.deepEqual(o.partSelectionsFromMessage(null), []);
});

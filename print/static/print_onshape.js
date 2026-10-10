/* PenguinCAM 3D print page: its own messaging with Onshape (templates/print_wizard.html,
 * Onshape mode only). Never loads or calls static/source_onshape.js: the CNC panel's
 * proven face-selection pattern is written again here for bodies.
 *
 * While the Parts step is active a one-body, solid-only requestSelection stays armed. Only
 * REQUESTED_SELECTION answers count (Onshape's generic SELECTION events are ignored); a
 * PENDING answer is ignored, an empty one (a deselection or a timeout) re-arms, one with
 * items none of which partSelectionsFromMessage can read goes to onUnreadable (the wizard
 * shows UNREADABLE_SELECTION_MESSAGE) and re-arms, and any other is handed to onSelections
 * and re-arms. "Add from another document…" opens
 * Onshape's part picker; each picked item is handed on, and its close re-arms the click.
 *
 * partSelectionsFromMessage is the one place that knows the shape of Onshape's selection
 * answers, which is not documented. Pure functions are exported for node tests.
 */
(function () {
  'use strict';

  var DIALOG_FIELDS = ['documentId', 'workspaceId', 'versionId', 'elementId', 'elementConfiguration',
                       'partName', 'idTag', 'isSurface', 'isComposite', 'isSketch', 'isFlattenedBody'];

  // Shown when Onshape answers a selection with items none of which name a part.
  var UNREADABLE_SELECTION_MESSAGE = 'Onshape sent a selection PenguinCAM could not read; '
    + 'try Add from another document\u2026 to pick the part.';

  function text(value) { return typeof value === 'string' && value ? value : null; }

  // True for the panel's own server (validated on the server when the page was built), or
  // for an https origin whose host is onshape.com or ends in .onshape.com.
  function isOnshapeOrigin(origin, server) {
    if (typeof origin !== 'string' || !origin) return false;
    if (server && origin === server) return true;
    var m = /^https:\/\/([^/:?#@]+)(:\d+)?$/.exec(origin);
    if (!m) return false;
    var host = m[1].toLowerCase();
    return host === 'onshape.com' || /\.onshape\.com$/.test(host);
  }

  // [{partId?, selectionId?, occurrencePath?, name?}] from a REQUESTED_SELECTION answer.
  // Accepts the field names the CNC code has seen (partId or bodyId, selectionId or
  // entityId) and an occurrence path as occurrencePath, occurrence.path or path. An item
  // with none of them says nothing about a part and is dropped.
  function partSelectionsFromMessage(message) {
    var items = message && Array.isArray(message.selections) ? message.selections : [];
    var out = [];
    items.forEach(function (item) {
      if (!item || typeof item !== 'object') return;
      var selection = {};
      var partId = text(item.partId) || text(item.bodyId);
      var selectionId = text(item.selectionId) || text(item.entityId);
      var path = Array.isArray(item.occurrencePath) ? item.occurrencePath
        : item.occurrence && Array.isArray(item.occurrence.path) ? item.occurrence.path
        : Array.isArray(item.path) ? item.path : null;
      if (partId) selection.partId = partId;
      if (selectionId) selection.selectionId = selectionId;
      if (path && path.length) selection.occurrencePath = path.slice();
      if (!partId && !selectionId && !selection.occurrencePath) return;
      if (text(item.name)) selection.name = item.name;
      out.push(selection);
    });
    return out;
  }

  // The fields of an itemSelectedInSelectItemDialog message the server resolves.
  function dialogItemToSelection(message) {
    var selection = {};
    DIALOG_FIELDS.forEach(function (key) {
      if (message && message[key] !== undefined) selection[key] = message[key];
    });
    return selection;
  }

  // post(message, targetOrigin) sends to the Onshape window; onSelections(selections, via)
  // receives parts, via 'selection' or 'dialog'; onUnreadable() (optional) is told of a
  // non-empty selection answer that names no part.
  function createOnshapeMessenger(opts) {
    var context = opts.context || {};
    var active = false, dialogOpen = false, counter = 0, lastId = null;

    // Every message carries the three ids. '*' as the CNC panel posts: the messages carry
    // no secret, and a return URL without `server` on an enterprise domain would otherwise
    // drop every one.
    function send(message) {
      message.documentId = context.documentId;
      message.workspaceId = context.workspaceId;
      message.elementId = context.elementId;
      opts.post(message, '*');
    }

    function arm() {
      counter++;
      lastId = 'penguincam-print-' + counter;
      send({ messageName: 'requestSelection', messageId: lastId, filterType: 'simple',
             entityTypeSpecifier: ['BODY'], bodyTypeSpecifier: ['SOLID'], requiredSelectionCount: 1 });
    }

    function onRequestedSelection(data) {
      if (!active) return;                       // a stray answer after leaving Parts
      var items = Array.isArray(data.selections) ? data.selections : [];
      var status = (data.status && data.status.statusCode) || (items.length ? 'SUCCESS' : '');
      if (status === 'PENDING') return;          // the student has not picked yet
      var selections = partSelectionsFromMessage(data);
      if (selections.length) opts.onSelections(selections, 'selection');
      else if (items.length && typeof opts.onUnreadable === 'function') opts.onUnreadable();
      arm();
    }

    return {
      init: function () { send({ messageName: 'applicationInit' }); },
      enterParts: function () { active = true; arm(); },
      leaveParts: function () {
        active = false;
        send({ messageName: 'stopRequest', messageId: lastId });
        if (dialogOpen) { dialogOpen = false; send({ messageName: 'closeSelectItemDialog' }); }
      },
      openDialog: function () {
        dialogOpen = true;
        send({ messageName: 'openSelectItemDialog', selectParts: true, selectMultiple: true });
      },
      isActive: function () { return active; },
      handleMessage: function (event) {
        if (!event || !isOnshapeOrigin(event.origin, context.server)) return;
        var data = event.data;
        if (!data || typeof data !== 'object') return;
        switch (data.messageName) {
          case 'REQUESTED_SELECTION': onRequestedSelection(data); break;
          case 'itemSelectedInSelectItemDialog':     // a pick crossing the close after leaving Parts is dropped
            if (active) opts.onSelections([dialogItemToSelection(data)], 'dialog');
            break;
          case 'selectItemDialogClosed': dialogOpen = false; if (active) arm(); break;
          default: break;                        // SELECTION and everything else
        }
      },
    };
  }

  if (typeof window !== 'undefined' && window.PenguinCAM && typeof document !== 'undefined') {
    var P = window.PenguinCAM;
    // The wizard (print_wizard.js, loaded after this file) sets P.onPartSelections.
    P.onshapeMessenger = createOnshapeMessenger({
      context: P.onshape || {},
      post: function (message, targetOrigin) {
        try { window.parent.postMessage(message, targetOrigin); } catch (e) { /* no parent window */ }
      },
      onSelections: function (selections, via) {
        if (typeof P.onPartSelections === 'function') P.onPartSelections(selections, via);
      },
      onUnreadable: function () {
        if (typeof P.onUnreadableSelection === 'function') P.onUnreadableSelection(UNREADABLE_SELECTION_MESSAGE);
      },
    });
    window.addEventListener('message', function (e) { P.onshapeMessenger.handleMessage(e); });
    P.onshapeMessenger.init();
  }

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = { partSelectionsFromMessage: partSelectionsFromMessage,
                       dialogItemToSelection: dialogItemToSelection,
                       createOnshapeMessenger: createOnshapeMessenger, isOnshapeOrigin: isOnshapeOrigin,
                       UNREADABLE_SELECTION_MESSAGE: UNREADABLE_SELECTION_MESSAGE };
  }
})();

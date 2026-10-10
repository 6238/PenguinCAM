/* PenguinCAM test bed -- the shapes of Onshape's side of the panel, shared by the fake
 * Onshape host (host.js, which replays recorded messages) and the local-mode stand-in
 * (local.js, which makes them up from uploaded parts).
 *
 * The panel URL is the one the Onshape extension opens; the message shapes are those of
 * the synthetic message logs (testbed/tests/fixtures/make_print_fixtures.py) until real
 * recordings land, and the same fields the print page reads (print/static/print_onshape.js).
 * Exposed as window.TestbedOnshape, and as a CommonJS module for node.
 */
(function (root) {
  'use strict';

  // The panel URL for a Part Studio or Assembly: {did, wid | vid, eid}. A panel opened on a
  // version gets versionId, and workspaceId as Onshape's raw, unsubstituted placeholder.
  function panelUrl(panelOrigin, ids, server, theme) {
    var onVersion = !ids.wid && !!ids.vid;
    return panelOrigin + '/onshape-panel?documentId=' + encodeURIComponent(ids.did) +
      '&workspaceId=' + (onVersion ? '{$workspaceId}' : encodeURIComponent(ids.wid)) +
      (onVersion ? '&versionId=' + encodeURIComponent(ids.vid) : '') +
      '&elementId=' + encodeURIComponent(ids.eid) +
      '&server=' + encodeURIComponent(server) + '&theme=' + (theme || 'light');
  }

  function copy(data) {
    return JSON.parse(JSON.stringify(data));
  }

  // `data` with its messageId replaced by `messageId`, when it has one to replace.
  function withMessageId(data, messageId) {
    var reply = copy(data);
    if (messageId && reply && typeof reply === 'object' && 'messageId' in reply) {
      reply.messageId = messageId;
    }
    return reply;
  }

  function context(ids) {
    return { documentId: ids.did, workspaceId: ids.wid, elementId: ids.eid };
  }

  // The answer to a requestSelection: the parts selected (each {partId}), or none. With no
  // parts and `pending`, the answer Onshape sends while nothing is picked yet.
  function requestedSelection(ids, messageId, parts, pending) {
    var message = context(ids);
    message.messageName = 'REQUESTED_SELECTION';
    message.messageId = messageId;
    message.selections = (parts || []).map(function (part) {
      return { elementId: ids.eid, entityType: 'BODY', partId: part.partId,
               selectionId: part.partId, selectionType: 'ENTITY' };
    });
    message.status = { statusCode: pending ? 'PENDING' : 'SUCCESS' };
    return message;
  }

  // One pick in Onshape's select-item dialog: a part ({partId, name}) of the Part Studio.
  function dialogItem(ids, part) {
    var message = context(ids);
    message.messageName = 'itemSelectedInSelectItemDialog';
    message.elementType = 'PARTSTUDIO';
    message.itemType = 'PART';
    message.idTag = part.partId;
    message.partName = part.name;
    message.isComposite = false;
    message.isFlattenedBody = false;
    message.isSketch = false;
    message.isSurface = false;
    return message;
  }

  function dialogClosed() {
    return { messageName: 'selectItemDialogClosed' };
  }

  var api = { panelUrl: panelUrl, withMessageId: withMessageId, requestedSelection: requestedSelection,
              dialogItem: dialogItem, dialogClosed: dialogClosed };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.TestbedOnshape = api;
})(this);

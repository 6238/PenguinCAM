/* Onshape "part source" adapter for the wizard (loaded only on /onshape-panel).
 *
 * Flow: announce to the Onshape host (applicationInit) -> on "Select a face",
 * postMessage a requestSelection -> Onshape returns REQUESTED_SELECTION with the
 * chosen face/part IDs -> POST those to /onshape/export-face, which exports a flat
 * DXF server-side and returns its bytes (base64) + outline -> hand the DXF File and
 * outline to the wizard via window.PenguinCAM.onPart. The browser keeps the bytes so
 * the job submit stays self-contained (serverless-safe).
 *
 * Contract with wizard.js:
 *   window.PenguinCAM.startFaceSelection()   - begin continuous face-selection (Parts step)
 *   window.PenguinCAM.stopFaceSelection()    - stop (left the Parts step)
 *   window.PenguinCAM.onPart(outlineData, File) - a part is ready to add
 *   window.PenguinCAM.onSelectionBusy(bool)  - import in progress
 *   window.PenguinCAM.onSelectionError(msg)  - something failed
 *   window.PenguinCAM.debug(label, data)     - optional debug logging
 */
(function () {
  'use strict';
  var P = window.PenguinCAM || (window.PenguinCAM = {});
  var ctx = P.onshape || {};
  var counter = 0;

  function dbg(label, data) { if (typeof P.debug === 'function') P.debug(label, data); }
  function post(msg) { try { window.parent.postMessage(msg, '*'); } catch (e) { dbg('onshape:post-fail', String(e)); } }

  function isOnshapeOrigin(origin) {
    var host = (origin || '').replace(/^https?:\/\//, '').split('/')[0] || '';
    return /(^|\.)onshape\.com$/.test(host);
  }

  function init() {
    post({ messageName: 'applicationInit', documentId: ctx.documentId, workspaceId: ctx.workspaceId, elementId: ctx.elementId });
    dbg('onshape:init', ctx);
  }

  // Continuous face-selection: while the Parts step is open we keep a face-restricted
  // requestSelection armed, so the user just clicks faces (no button) and can't select
  // non-faces. We only act on REQUESTED_SELECTION (the restricted response) and ignore
  // Onshape's generic SELECTION events. active gates stray responses after leaving.
  var active = false;

  function arm() {
    counter++;
    post({
      messageName: 'requestSelection',
      messageId: 'penguincam-sel-' + counter,
      documentId: ctx.documentId, workspaceId: ctx.workspaceId, elementId: ctx.elementId,
      filterType: 'simple',
      entityTypeSpecifier: ['FACE'],
      bodyTypeSpecifier: ['SOLID'],
      requiredSelectionCount: 1
    });
    dbg('onshape:arm', counter);
  }

  P.startFaceSelection = function () { active = true; arm(); };
  P.stopFaceSelection = function () { active = false; };

  window.addEventListener('message', function (e) {
    if (!isOnshapeOrigin(e.origin)) return;
    var d = e.data || {};
    if (d.messageName !== 'REQUESTED_SELECTION') return;  // ignore generic SELECTION (non-faces)
    if (!active) return;                                  // ignore stray responses off the Parts step
    var sels = d.selections || [];
    var status = (d.status && d.status.statusCode) || (sels.length ? 'SUCCESS' : '');
    dbg('onshape:REQUESTED_SELECTION', { n: sels.length, status: status });
    if (status === 'PENDING') return;                     // user hasn't picked yet
    if (!sels.length) { arm(); return; }                  // deselection / timeout - re-arm
    var s = sels[0];
    var faceId = s.selectionId || s.entityId || s.id;
    var partId = s.partId || s.bodyId || null;
    if (!faceId) { arm(); return; }
    exportFace(faceId, partId);
  });

  function exportFace(faceId, partId) {
    if (P.onSelectionBusy) P.onSelectionBusy(true);
    // In 2.5D mode the backend builds a depth-layered DXF of all parallel faces;
    // otherwise it exports the single selected face flat. Read the mode live so a
    // mid-session switch is honored.
    var multilayer = (typeof P.getMode === 'function' && P.getMode() === '2.5d');
    dbg('onshape:export', { faceId: faceId, partId: partId, multilayer: multilayer });
    fetch('/onshape/export-face', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        documentId: ctx.documentId, workspaceId: ctx.workspaceId, elementId: ctx.elementId,
        // Present when the panel was launched on an older version/microversion (no workspace).
        versionId: ctx.versionId, microversionId: ctx.microversionId,
        faceId: faceId, partId: partId, multilayer: multilayer
      })
    })
      // Read as text and parse defensively: an error response is not always JSON
      // (a proxy or CDN can substitute its own HTML error page), and calling
      // r.json() on that rejects with an opaque SyntaxError that tells the user
      // nothing about what actually went wrong.
      .then(function (r) {
        return r.text().then(function (body) {
          var j = null;
          try { j = JSON.parse(body); } catch (e) { /* not JSON; handled below */ }
          return { ok: r.ok, status: r.status, j: j, body: body };
        });
      })
      .then(function (res) {
        if (P.onSelectionBusy) P.onSelectionBusy(false);
        if (!res.ok || !res.j || !res.j.success) {
          var msg;
          if (res.j && res.j.error) {
            msg = res.j.error;
          } else if (!res.j) {
            // Non-JSON body: name the status so this is diagnosable at a glance.
            msg = 'Server error (HTTP ' + res.status + '). Please try again.';
            dbg('onshape:export-nonjson', { status: res.status, body: res.body.slice(0, 200) });
          } else {
            msg = 'export failed';
          }
          // Dead Onshape credentials: offer a reconnect rather than a dead end.
          if (res.status === 401 && res.j && res.j.auth_url) {
            if (P.onAuthExpired) {
              P.onAuthExpired(res.j.auth_url, msg);
              return;
            }
            msg = msg + ' Reconnect at ' + res.j.auth_url;
          }
          if (P.onSelectionError) P.onSelectionError(msg);
          if (active) arm();  // let them try another face
          return;
        }
        var bin = atob(res.j.dxf);
        var arr = new Uint8Array(bin.length);
        for (var i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
        var file = new File([arr], (res.j.name || 'part') + '.dxf', { type: 'application/dxf' });
        dbg('onshape:export-ok', { name: res.j.name, w: res.j.width, h: res.j.height });
        if (P.onPart) P.onPart(res.j, file);
        if (active) arm();  // imported — go straight back into select-a-face mode
      })
      .catch(function (err) {
        if (P.onSelectionBusy) P.onSelectionBusy(false);
        if (P.onSelectionError) P.onSelectionError(String(err));
        if (active) arm();
      });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();

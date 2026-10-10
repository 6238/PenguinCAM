/* PenguinCAM test bed, local mode -- the stand-in Onshape Part Studio's script.
 *
 * It signs the browser in through /testbed/sign-in, reads this session's stand-in ids and
 * uploaded parts from /testbed/local/state, and frames the real panel with the URL the
 * Onshape extension opens (onshape_shapes.js panelUrl; server= is this page's origin). Then
 * it plays Onshape's side of the panel's messages, live rather than from a message log:
 *   - requestSelection: answered PENDING at once (nothing picked yet), and remembered;
 *     each change of the selection in the Parts list then answers the latest request
 *     with every selected part, or with none (a deselection);
 *   - stopRequest: the request is forgotten;
 *   - openSelectItemDialog: a part picker lists the uploaded parts; each pick posts
 *     itemSelectedInSelectItemDialog, and Close posts selectItemDialogClosed;
 *   - closeSelectItemDialog: the picker closes.
 * Messages from anything but the panel's frame are ignored, and every message to it is
 * posted to this origin only. window.__local is the control surface for the browser test.
 */
(function () {
  'use strict';

  var shapes = window.TestbedOnshape;
  var DEFAULT_PANEL_WIDTH = 320;       // Onshape's right panel; ?panel=<px> tries another

  var state = { ids: null, origin: location.origin, parts: [], selected: [], request: null,
                dialogOpen: false, picked: {} };
  var log = [];            // every message from the panel: {data, t_ms}
  var sent = [];           // every message posted to the panel: {data, t_ms}
  var started = Date.now();
  var frame = null;

  function $(id) { return document.getElementById(id); }

  function setMessage(text) { $('messages').textContent = text || ''; }

  function post(data) {
    sent.push({ data: data, t_ms: Date.now() - started });
    if (frame && frame.contentWindow) frame.contentWindow.postMessage(data, state.origin);
  }

  function partById(partId) {
    for (var i = 0; i < state.parts.length; i++) if (state.parts[i].partId === partId) return state.parts[i];
    return null;
  }

  function sizeText(bytes) {
    return bytes >= 1048576 ? (bytes / 1048576).toFixed(1) + ' MB' : Math.max(1, Math.round(bytes / 1024)) + ' kB';
  }

  /* ------------------------------------------------------------ the Parts list */
  function renderParts() {
    var list = $('parts');
    list.innerHTML = '';
    $('parts-count').textContent = '(' + state.parts.length + ')';
    if (!state.parts.length) {
      var empty = document.createElement('li');
      empty.className = 'empty';
      empty.textContent = 'No parts yet: upload STL files.';
      list.appendChild(empty);
    }
    state.parts.forEach(function (part) {
      var li = document.createElement('li');
      li.dataset.partId = part.partId;
      if (state.selected.indexOf(part.partId) >= 0) li.className = 'selected';
      var icon = document.createElement('span');
      icon.className = 'icon';
      var name = document.createElement('span');
      name.className = 'name';
      name.textContent = part.name;
      var size = document.createElement('span');
      size.className = 'size';
      size.textContent = sizeText(part.bytes);
      li.appendChild(icon);
      li.appendChild(name);
      li.appendChild(size);
      li.addEventListener('click', function (e) { clickPart(part.partId, e.shiftKey || e.ctrlKey || e.metaKey); });
      list.appendChild(li);
    });
    renderDialog();
  }

  function clickPart(partId, adding) {
    var at = state.selected.indexOf(partId);
    if (adding) {
      if (at >= 0) state.selected.splice(at, 1);
      else state.selected.push(partId);
    } else {
      state.selected = [partId];
    }
    selectionChanged();
  }

  function setSelection(partIds) {
    state.selected = partIds.filter(function (id) { return !!partById(id); });
    selectionChanged();
  }

  // Onshape answers the panel's armed request when the selection changes; with no armed
  // request (the panel is not on its Parts step) nothing is sent.
  function selectionChanged() {
    renderParts();
    if (!state.request) return;
    var parts = state.selected.map(partById).filter(Boolean);
    var request = state.request;
    state.request = null;           // answered; the panel arms a new one
    post(shapes.requestedSelection(state.ids, request, parts, false));
  }

  /* --------------------------------------------------------- the part picker */
  function renderDialog() {
    var list = $('dialog-parts');
    list.innerHTML = '';
    if (!state.parts.length) {
      var empty = document.createElement('li');
      empty.textContent = 'No parts: upload STL files first.';
      list.appendChild(empty);
    }
    state.parts.forEach(function (part) {
      var li = document.createElement('li');
      li.dataset.partId = part.partId;
      li.textContent = part.name;
      if (state.picked[part.partId]) li.className = 'picked';
      li.addEventListener('click', function () { pick(part.partId); });
      list.appendChild(li);
    });
  }

  function openDialog() {
    state.dialogOpen = true;
    state.picked = {};
    renderDialog();
    $('dialog-back').hidden = false;
  }

  function hideDialog() {
    state.dialogOpen = false;
    $('dialog-back').hidden = true;
  }

  function pick(partId) {
    var part = partById(partId);
    if (!state.dialogOpen || !part) return false;
    state.picked[partId] = true;
    renderDialog();
    post(shapes.dialogItem(state.ids, part));
    return true;
  }

  function closeDialog() {
    if (!state.dialogOpen) return false;
    hideDialog();
    post(shapes.dialogClosed());
    return true;
  }

  /* ----------------------------------------------------- the panel's messages */
  window.addEventListener('message', function (e) {
    if (!frame || e.source !== frame.contentWindow || e.origin !== state.origin) return;
    var data = e.data;
    log.push({ data: data, t_ms: Date.now() - started });
    if (!data || typeof data !== 'object') return;
    switch (data.messageName) {
      case 'requestSelection':
        state.request = data.messageId || null;
        post(shapes.requestedSelection(state.ids, data.messageId, [], true));
        break;
      case 'stopRequest':
        if (!data.messageId || data.messageId === state.request) state.request = null;
        break;
      case 'openSelectItemDialog': openDialog(); break;
      case 'closeSelectItemDialog': hideDialog(); break;
      default: break;               // applicationInit and the CNC panel's messages
    }
  });

  /* ------------------------------------------------------------- uploading */
  function applyState(data) {
    state.ids = data.ids;
    state.origin = data.origin || location.origin;
    state.parts = data.parts || [];
    state.selected = state.selected.filter(function (id) { return !!partById(id); });
    renderParts();
    setMessage((data.errors || []).join('\n'));
  }

  function upload(files) {
    var list = Array.prototype.slice.call(files || []);
    if (!list.length) return Promise.resolve();
    var form = new FormData();
    list.forEach(function (file) { form.append('files', file, file.name); });
    setMessage('Uploading ' + list.length + (list.length === 1 ? ' file' : ' files') + '…');
    return fetch('/testbed/local/parts', { method: 'POST', body: form, credentials: 'same-origin' })
      .then(function (r) {
        return r.json().then(function (data) {
          if (!r.ok) throw new Error(data.error || ('upload answered HTTP ' + r.status));
          return data;
        }, function () { throw new Error('upload answered HTTP ' + r.status); });
      })
      .then(applyState)
      .catch(function (err) { setMessage(String(err.message || err)); });
  }

  $('btn-upload').addEventListener('click', function () { $('file-input').click(); });
  $('file-input').addEventListener('change', function () {
    upload(this.files).then(function () { $('file-input').value = ''; });
  });
  $('btn-clear').addEventListener('click', function () {
    fetch('/testbed/local/clear', { method: 'POST', credentials: 'same-origin' })
      .then(function (r) { return r.json(); }).then(applyState);
  });
  $('btn-reload').addEventListener('click', function () { loadPanel(); });
  $('dialog-close').addEventListener('click', closeDialog);

  var graphics = $('graphics');
  ['dragenter', 'dragover'].forEach(function (name) {
    document.addEventListener(name, function (e) {
      e.preventDefault();
      graphics.classList.add('dragging');
    });
  });
  ['dragleave', 'drop'].forEach(function (name) {
    document.addEventListener(name, function (e) {
      e.preventDefault();
      if (name === 'drop' || e.target === document.documentElement || !e.relatedTarget) {
        graphics.classList.remove('dragging');
      }
    });
  });
  document.addEventListener('drop', function (e) {
    if (e.dataTransfer && e.dataTransfer.files) upload(e.dataTransfer.files);
  });
  // A click in empty graphics space clears the selection, as in Onshape.
  graphics.addEventListener('click', function (e) {
    if (e.target === graphics || e.target.id === 'drop') setSelection([]);
  });

  /* --------------------------------------------------------------- start */
  function panelWidth() {
    var wanted = parseInt(new URLSearchParams(location.search).get('panel'), 10);
    return wanted >= 240 && wanted <= 800 ? wanted : DEFAULT_PANEL_WIDTH;
  }

  function panelUrl() {
    return shapes.panelUrl(state.origin, state.ids, state.origin, 'light');
  }

  function loadPanel() {
    state.request = null;
    hideDialog();
    if (!frame) {
      frame = document.createElement('iframe');
      frame.id = 'panel';
      frame.title = 'PenguinCAM panel';
      $('right').appendChild(frame);
    }
    frame.src = panelUrl();
  }

  document.documentElement.style.setProperty('--panel-width', panelWidth() + 'px');

  window.__local = {
    log: log,
    sent: sent,
    ready: false,
    state: state,
    upload: upload,
    selectParts: function (names) {
      setSelection(names.map(function (name) {
        for (var i = 0; i < state.parts.length; i++) if (state.parts[i].name === name) return state.parts[i].partId;
        return null;
      }));
    },
    pick: function (name) {
      for (var i = 0; i < state.parts.length; i++) if (state.parts[i].name === name) return pick(state.parts[i].partId);
      return false;
    },
    closeDialog: closeDialog,
    hasRequest: function () { return !!state.request; },
    panelUrl: function () { return frame ? frame.src : null; },
    reloadPanel: loadPanel
  };

  fetch('/testbed/sign-in', { credentials: 'same-origin' })
    .then(function (r) {
      if (r.status !== 204) throw new Error('the development sign-in answered HTTP ' + r.status);
      return fetch('/testbed/local/state', { credentials: 'same-origin' });
    })
    .then(function (r) {
      if (!r.ok) throw new Error('/testbed/local/state answered HTTP ' + r.status);
      return r.json();
    })
    .then(function (data) {
      applyState(data);
      loadPanel();
      window.__local.ready = true;
    })
    .catch(function (err) {
      window.__local.error = String(err);
      setMessage(String(err));
    });
})();

/* PenguinCAM test bed -- the fake Onshape host's script (spec 5.4).
 *
 * The page is /documents/<did>/w/<wid>/e/<eid> (or /v/<vid>/ for a version) with
 * ?scenario=<name>&port=<port>. It frames the panel from http://localhost:<port> with the
 * query Onshape sends, and answers the panel's messages from the scenario's message log,
 * fetched from /_testbed/messages/<scenario>.json:
 *   - a message from the panel is matched, in the current step's segment, to the next
 *     recorded `out` event with the same messageName and the same keys (never on
 *     messageId, which Onshape's select-dialog messages lack);
 *   - the `in` events after that event, up to the next `out`, are posted back to the
 *     panel; a reply's messageId becomes the live message's.
 * window.__testbed is the control surface for the browser harness.
 */
(function () {
  'use strict';

  var parts = location.pathname.split('/');        // ['', 'documents', did, w|v, id, 'e', eid]
  var query = new URLSearchParams(location.search);
  var scenario = query.get('scenario') || '';
  var panelOrigin = 'http://localhost:' + (query.get('port') || '6240');
  var onVersion = parts[3] === 'v';

  // A panel opened on a version gets versionId, and workspaceId as Onshape's raw,
  // unsubstituted placeholder.
  var panelUrl = panelOrigin + '/onshape-panel?documentId=' + encodeURIComponent(parts[2]) +
    '&workspaceId=' + (onVersion ? '{$workspaceId}' : encodeURIComponent(parts[4])) +
    (onVersion ? '&versionId=' + encodeURIComponent(parts[4]) : '') +
    '&elementId=' + encodeURIComponent(parts[6]) +
    '&server=' + encodeURIComponent(location.origin) + '&theme=light';

  var log = [];            // every message from the panel: {origin, data, t_ms}
  var sent = [];           // every message posted to the panel, for debugging
  var messageLog = null;
  var step = null;         // the current step's name
  var cursors = {};        // step name -> index in its segment after the last match
  var liveMessageId = null;  // the messageId of the panel's latest message that had one
  var waiting = [];        // messages that arrived before the message log
  var started = Date.now();

  var frame = document.createElement('iframe');
  frame.id = 'panel';
  frame.src = panelUrl;
  document.body.appendChild(frame);

  function segment(name) {
    var steps = (messageLog && messageLog.steps) || [];
    for (var i = 0; i < steps.length; i++) {
      if (steps[i].step === name) return steps[i].events;
    }
    return [];
  }

  function keySet(data) {
    return data && typeof data === 'object' ? Object.keys(data).sort().join(',') : '';
  }

  function copy(data) {
    return JSON.parse(JSON.stringify(data));
  }

  function withMessageId(data, messageId) {
    var reply = copy(data);
    if (messageId && reply && typeof reply === 'object' && 'messageId' in reply) {
      reply.messageId = messageId;
    }
    return reply;
  }

  // The `in` events from `start` up to the next `out`: [replies, index of that out].
  function repliesFrom(events, start) {
    var replies = [];
    var i = start;
    for (; i < events.length && events[i].dir === 'in'; i++) replies.push(events[i].data);
    return [replies, i];
  }

  // The match for `message` in step `name`, from `start`: [replies, next cursor] or null.
  function find(message, name, start) {
    var events = segment(name);
    var wanted = message && message.messageName;
    var keys = keySet(message);
    for (var i = start || 0; i < events.length; i++) {
      var event = events[i];
      if (event.dir === 'out' && event.data && event.data.messageName === wanted &&
          keySet(event.data) === keys) {
        return [repliesFrom(events, i + 1)[0], i + 1];
      }
    }
    return null;
  }

  function post(data) {
    sent.push({ data: data, t_ms: Date.now() - started });
    frame.contentWindow.postMessage(data, panelOrigin);
  }

  function answer(message) {
    if (message && message.messageId) liveMessageId = message.messageId;
    var found = find(message, step, cursors[step]);
    if (!found) return;
    cursors[step] = found[1];
    for (var i = 0; i < found[0].length; i++) {
      post(withMessageId(found[0][i], message.messageId));
    }
  }

  window.addEventListener('message', function (e) {
    if (e.source !== frame.contentWindow) return;
    log.push({ origin: e.origin, data: e.data, t_ms: Date.now() - started });
    if (messageLog) answer(e.data);
    else waiting.push(e.data);
  });

  function setStep(name) {
    step = name;
  }

  // Posts the `in` events that open step `name`: what Onshape sent when the selection
  // was made. They answer the panel's latest request, so they carry its messageId.
  function postStep(name) {
    setStep(name);
    var found = repliesFrom(segment(name), cursors[name] || 0);
    cursors[name] = found[1];
    for (var i = 0; i < found[0].length; i++) post(withMessageId(found[0][i], liveMessageId));
    return found[0].length;
  }

  window.__testbed = {
    log: log,
    sent: sent,
    ready: false,
    panelUrl: panelUrl,
    setStep: setStep,
    selectPart: function (name) { return postStep('select part ' + name); },
    selectParts: function (names) { return postStep('select parts ' + names.join(', ')); },
    deselect: function () { return postStep('deselect'); },
    dialogClosed: function () { return postStep('dialog closed'); },
    reloadPanel: function () { frame.src = panelUrl; },
    // The replies `message` would get in step `name`, without posting or moving on.
    match: function (message, name) {
      var found = find(message, name, 0);
      return found ? found[0].map(function (r) { return withMessageId(r, message.messageId); })
        : null;
    },
    currentStep: function () { return step; }
  };

  fetch('/_testbed/messages/' + encodeURIComponent(scenario) + '.json')
    .then(function (r) {
      if (!r.ok) throw new Error('no message log for ' + scenario + ': HTTP ' + r.status);
      return r.json();
    })
    .then(function (data) {
      messageLog = data;
      if (step === null && data.steps && data.steps.length) step = data.steps[0].step;
      var early = waiting;
      waiting = [];
      for (var i = 0; i < early.length; i++) answer(early[i]);
      window.__testbed.ready = true;
    })
    .catch(function (err) {
      window.__testbed.error = String(err);
      if (window.console) window.console.error('[fake host] ' + err);
    });
})();

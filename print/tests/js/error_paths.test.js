'use strict';
// Every error the student sees goes through showError (spec: one helper, which also reports
// the message to the server). This scans the print page's own scripts as text and fails if
// any other code writes to an error line (#<step>-errors). Reads, such as fitLayoutCanvas
// measuring #layout-errors, are allowed.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const STATIC = path.join(__dirname, '..', '..', 'static');
const SCRIPTS = ['print_wizard.js', 'print_onshape.js', 'print_layout.js'];
const ALLOWED = ['showError', 'clearError'];

function escapeRe(text) { return text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); }

/** [start, end] of the body of `function <name>(`, by brace matching; null if absent. */
function functionBody(source, name) {
  const at = source.indexOf('function ' + name + '(');
  if (at < 0) return null;
  const open = source.indexOf('{', at);
  let depth = 0;
  for (let i = open; i < source.length; i++) {
    if (source[i] === '{') depth++;
    else if (source[i] === '}' && --depth === 0) return [open, i];
  }
  return [open, source.length];
}

/** Every error-line handle: variables assigned from an expression mentioning -errors, and
 *  direct $('#…-errors') / getElementById('…-errors') expressions. As regex sources. */
function errorHandles(source) {
  const handles = new Set();
  const assign = /\b([A-Za-z_$][\w$]*)\s*=(?!=)\s*([^;,\n]*)/g;
  let m;
  while ((m = assign.exec(source))) {
    if (m[2].indexOf('-errors') >= 0) handles.add('\\b' + escapeRe(m[1]));
  }
  const direct = /(?:\$|document\.querySelector|document\.getElementById)\(\s*(['"])#?[\w-]*-errors\1\s*\)/g;
  while ((m = direct.exec(source))) handles.add(escapeRe(m[0]));
  return Array.from(handles);
}

/** Writes to an error line outside showError and clearError, as "file:line: text". */
function violations(source, file) {
  const bodies = ALLOWED.map((name) => functionBody(source, name)).filter(Boolean);
  const inside = (pos) => bodies.some(([a, b]) => pos > a && pos < b);
  const found = [];
  errorHandles(source).forEach((handle) => {
    const write = new RegExp(handle + '\\s*\\.\\s*(?:(?:textContent|innerHTML|innerText)\\s*=(?!=)|' +
                             '(?:append|appendChild|insertAdjacent\\w*)\\s*\\()', 'g');
    let m;
    while ((m = write.exec(source))) {
      if (inside(m.index)) continue;
      const line = source.slice(0, m.index).split('\n').length;
      found.push(file + ':' + line + ': ' + m[0]);
    }
  });
  return found;
}

test('the scanner finds a stray write (self-check)', () => {
  const snippet = [
    "function showError(step, message) { var errLine = $('#' + step + '-errors'); errLine.textContent = message; }",
    "function clearError(step) { var errLine = $('#' + step + '-errors'); errLine.textContent = ''; }",
    "function fit() { var errors = $('#layout-errors'); return errors.offsetHeight; }",
    "function drive() { var status = $('#gen-status'), errs = $('#preview-errors'); errs.textContent = 'x'; }",
  ].join('\n');
  assert.deepEqual(violations(snippet, 'snippet'), ['snippet:4: errs.textContent =']);
  assert.equal(violations("$('#parts-errors').innerHTML = 'y';", 's').length, 1);
});

test('only showError and clearError write an error line', () => {
  let scanned = 0;
  SCRIPTS.forEach((name) => {
    const file = path.join(STATIC, name);
    if (!fs.existsSync(file)) return;
    scanned++;
    const source = fs.readFileSync(file, 'utf8');
    assert.ok(functionBody(source, 'showError') || name !== 'print_wizard.js', 'showError exists');
    assert.deepEqual(violations(source, name), []);
  });
  assert.ok(scanned >= 1, 'print_wizard.js was scanned');
});

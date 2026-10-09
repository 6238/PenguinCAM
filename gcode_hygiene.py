#!/usr/bin/env python3
"""G-code output hygiene: the last gate before a program reaches a machine.

CNC controllers are far less forgiving than a G-code viewer. A nested parenthesis
comment ends the comment early and feeds the remainder of the line to the parser as
motion; a non-ASCII byte makes most controllers error out or drop the line. Both are
easy to introduce by accident - an f-string carrying a measurement, a comment that
quotes a parenthetical, a conditional branch no test happens to cover.

Every line PenguinCAM emits passes through `finalize_gcode()` on its way out, so a
controller-hostile construct can never reach a machine no matter which branch built it.
In strict mode (enabled by the test suite, see tests/__init__.py) the same check raises
instead of repairing, so the mistake gets fixed at its source rather than silently
papered over. `tests/test_gcode_hygiene.py` additionally lints the source for G-code
comment literals, which covers branches the tests never execute.

Rules enforced, all from CLAUDE.md's "G-code Generation Rules":
  1. No nested parenthesis comments   - inner parens become commas.
  2. Pure ASCII                       - known symbols transliterate, the rest are dropped.
  3. No square brackets in comments   - some controllers treat them as expressions.
"""

import os
import unicodedata
from typing import Iterable, List, Sequence

# Characters that show up in measurements and machining notes, with the ASCII the
# controller can actually read. Anything not listed is transliterated by Unicode
# decomposition (e.g. accented letters) and dropped if that fails.
ASCII_SUBSTITUTIONS = {
    '°': ' deg',   # degree
    '′': "'",      # prime -> feet
    '″': '"',      # double prime -> inches
    '‘': "'", '’': "'",           # curly single quotes
    '“': '"', '”': '"',           # curly double quotes
    '–': '-', '—': '-',           # en/em dash
    '…': '...',    # ellipsis
    '→': '->', '←': '<-',         # arrows
    '±': '+/-',
    '×': 'x',      # multiplication sign
    '÷': '/',
    '≤': '<=', '≥': '>=',
    '≠': '!=',
    'µ': 'u', 'μ': 'u',           # micro
    '⌀': 'dia', 'Ø': 'dia', '∅': 'dia',  # diameter
    '½': '1/2', '¼': '1/4', '¾': '3/4',
    ' ': ' ',      # non-breaking space
}

# Set by the test suite so a bad line fails loudly at its source instead of being
# quietly repaired. Never enabled in production: a cosmetic comment defect must not
# cost a team their job output.
STRICT = os.environ.get('PENGUINCAM_STRICT_GCODE', '') not in ('', '0', 'false')


class GCodeHygieneError(AssertionError):
    """Raised by `finalize_gcode` in strict mode when a line needed repair."""


def _rstrip(out: List[str]) -> None:
    """Drop trailing spaces already emitted, so a flattened paren reads "a, b" not "a , b"."""
    while out and out[-1] == ' ':
        out.pop()


def _to_ascii(text: str) -> str:
    """Render `text` as ASCII, transliterating what it can and dropping the rest."""
    if text.isascii():
        return text
    out = []
    for ch in text:
        if ch.isascii():
            out.append(ch)
            continue
        replacement = ASCII_SUBSTITUTIONS.get(ch)
        if replacement is None:
            # Accented letters decompose to a base letter; symbols and emoji do not
            # and are dropped rather than turned into mojibake.
            replacement = ''.join(
                c for c in unicodedata.normalize('NFKD', ch) if c.isascii()
            )
        out.append(replacement)
    return ''.join(out)


def scrub_gcode_line(line: str) -> str:
    """Return `line` with every construct a controller can choke on repaired.

    Parenthesis handling only applies to the part of the line outside a `;` comment,
    which already runs to end-of-line and so cannot be terminated early by a paren.
    """
    line = _to_ascii(line)

    out = []
    depth = 0
    for i, ch in enumerate(line):
        if ch == ';' and depth == 0:
            out.append(line[i:])          # rest of line is already a comment
            break
        if ch == '(':
            depth += 1
            if depth == 1:
                out.append('(')
            else:
                _rstrip(out)
                out.append(', ')
        elif ch == ')':
            if depth > 1:
                _rstrip(out)
                out.append(',')
                depth -= 1
            elif depth == 1:
                out.append(')')
                depth = 0
            # A stray ')' outside a comment is a parse error on its own; drop it.
        elif depth > 0 and ch in '[]':
            pass                          # brackets read as expressions on some controllers
        else:
            out.append(ch)
    if depth > 0:
        out.append(')')                   # never leave a comment open across lines

    scrubbed = ''.join(out)
    # Flattening "(a (b) c)" can leave comma runs and a comma butted against the close.
    while ', ,' in scrubbed or ',,' in scrubbed or ',  ' in scrubbed:
        scrubbed = scrubbed.replace(', ,', ', ').replace(',,', ',').replace(',  ', ', ')
    return scrubbed.replace(', )', ')').replace(',)', ')')


def describe_violations(line: str) -> List[str]:
    """Name the controller-hostile constructs in `line`, for error messages."""
    reasons = []
    code = line.split(';', 1)[0]
    depth = 0
    balanced = True
    for ch in code:
        if ch == '(':
            depth += 1
            if depth > 1 and 'nested parenthesis comment' not in reasons:
                reasons.append('nested parenthesis comment')
        elif ch == ')':
            depth -= 1
            if depth < 0:
                reasons.append('unbalanced ")"')
                balanced = False
                break
    if balanced and depth > 0:
        reasons.append('unclosed "("')
    if not line.isascii():
        bad = sorted({ch for ch in line if not ch.isascii()})
        reasons.append('non-ASCII character(s): ' + ' '.join(repr(c) for c in bad))
    if any(ch in '[]' for ch in _comment_text(code)):
        reasons.append('square bracket inside a comment')
    return reasons


def _comment_text(code: str) -> str:
    """The text inside parenthesis comments on `code`, concatenated."""
    out = []
    depth = 0
    for ch in code:
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth = max(0, depth - 1)
        elif depth > 0:
            out.append(ch)
    return ''.join(out)


def find_violations(lines: Iterable[str]) -> List[str]:
    """Human-readable report lines for every input line that needs repair."""
    problems = []
    for number, line in enumerate(lines, 1):
        reasons = describe_violations(line)
        if reasons:
            problems.append(f'  line {number}: {"; ".join(reasons)}\n    {line.strip()}')
    return problems


def finalize_gcode(lines: Sequence[str], strict: bool = None) -> str:
    """Join emitted G-code lines into the final program, scrubbed and machine-safe.

    This is the single exit through which every generated program passes. In strict
    mode it raises `GCodeHygieneError` instead of repairing, naming the offending
    lines so the source that produced them can be fixed.
    """
    if strict is None:
        strict = STRICT
    if strict:
        problems = find_violations(lines)
        if problems:
            raise GCodeHygieneError(
                f'{len(problems)} G-code line(s) would need scrubbing before reaching a '
                f'controller - fix the source that emits them:\n' + '\n'.join(problems)
            )
    return '\n'.join(scrub_gcode_line(line) for line in lines)

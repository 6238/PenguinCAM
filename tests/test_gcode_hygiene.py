"""Guards against G-code that CNC controllers cannot parse.

Two nets, because neither alone is enough:

  * `TestSourceLint` reads the source with `ast` and flags G-code comment literals that
    are already malformed on the page. This catches branches no test ever executes -
    exactly how "(===== PERIMETER (NO TABS) =====)" survived for so long behind an
    `if self.tabs_enabled:` the fixtures never took.
  * `TestScrubber` pins the repair behaviour of `gcode_hygiene`, the runtime gate every
    generated program passes through. Combined with strict mode (tests/__init__.py),
    any generation test in the suite now fails on a bad line rather than emitting it.
"""

import ast
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gcode_hygiene import (
    GCodeHygieneError, describe_violations, finalize_gcode, scrub_gcode_line,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

# Modules that emit G-code. Anything added here gets its comment literals linted.
GCODE_SOURCES = ['frc_cam_postprocessor.py', 'safe_test_mode.py', 'gcode_hygiene.py']


def _looks_like_gcode_comment(text: str) -> bool:
    """True when a string literal is (or ends in) a G-code comment.

    Deliberately narrow: a parenthetical inside ordinary prose - an error message, a
    stats string, a CLI note - is not destined for a controller and must not fail the
    lint. What reaches a machine is either a bare comment, "(...)", or a comment
    appended to a motion/M-word line, "G1 X1 (feed)".
    """
    stripped = text.strip()
    if stripped.startswith('('):
        return True
    return bool(stripped[:1] in 'GMTFS' and stripped[1:2].isdigit() and '(' in stripped)


def _string_literals(tree):
    """Every string constant in `tree` with its line number, docstrings excluded.

    Docstrings are prose, not output, and routinely contain parentheticals.
    """
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, 'body', None)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                docstrings.add(id(body[0].value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            yield node.lineno, node.value


class TestSourceLint(unittest.TestCase):
    """Static check: no source literal spells a G-code comment a controller would reject."""

    def test_gcode_comment_literals_are_controller_safe(self):
        problems = []
        for name in GCODE_SOURCES:
            path = REPO_ROOT / name
            tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
            for lineno, text in _string_literals(tree):
                if not _looks_like_gcode_comment(text):
                    continue
                # An f-string's literal chunks arrive here individually, so an unbalanced
                # paren is expected; only the substantive rules apply.
                reasons = [r for r in describe_violations(text)
                           if 'unclosed' not in r and 'unbalanced' not in r]
                if reasons:
                    problems.append(f'{name}:{lineno}: {"; ".join(reasons)}\n    {text!r}')
        self.assertEqual(
            [], problems,
            'G-code comment literals that a controller cannot parse - see CLAUDE.md, '
            '"G-code Generation Rules":\n' + '\n'.join(problems)
        )

    def test_lint_would_catch_a_regression(self):
        """The lint is only worth having if it fires; prove it on the original bug."""
        self.assertTrue(_looks_like_gcode_comment('(===== PERIMETER (NO TABS) =====)'))
        self.assertIn('nested parenthesis comment',
                      describe_violations('(===== PERIMETER (NO TABS) =====)'))
        # ...and stays quiet on prose that merely contains a parenthetical.
        self.assertFalse(_looks_like_gcode_comment('Flip tube 180 deg around Y-axis (M0)'))


class TestScrubber(unittest.TestCase):
    """Runtime gate: whatever a branch emits, what leaves is machine-safe."""

    def test_nested_comments_flatten_to_commas(self):
        self.assertEqual('(===== PERIMETER, NO TABS, =====)',
                         scrub_gcode_line('(===== PERIMETER (NO TABS) =====)'))
        self.assertEqual('G1 X1.0 Y2.0 (feed, rapid in)',
                         scrub_gcode_line('G1 X1.0 Y2.0 (feed (rapid in))'))

    def test_unicode_becomes_readable_ascii(self):
        self.assertEqual('(Cut depth: 0.25" at 4 deg)',
                         scrub_gcode_line('(Cut depth: 0.25″ at 4°)'))
        self.assertEqual('(Feedrate -> 75 IPM)', scrub_gcode_line('(Feedrate → 75 IPM)'))
        self.assertTrue(scrub_gcode_line('(Tol +/-0.001)').isascii())

    def test_unknown_unicode_is_dropped_not_mangled(self):
        scrubbed = scrub_gcode_line('(SAFE TEST MODE ⚠️)')
        self.assertTrue(scrubbed.isascii())
        self.assertIn('SAFE TEST MODE', scrubbed)

    def test_square_brackets_leave_comments(self):
        self.assertEqual('(bounds 0,1)', scrub_gcode_line('(bounds [0,1])'))

    def test_semicolon_comments_and_plain_motion_pass_through(self):
        for line in ['G0 X1.0 Y2.0', 'M5', '', '(plain comment)', 'G1 Z-0.25 ; plunge (fast)']:
            self.assertEqual(line, scrub_gcode_line(line))

    def test_comments_never_stay_open_across_lines(self):
        self.assertEqual('(truncated)', scrub_gcode_line('(truncated'))
        self.assertEqual('G0 X1', scrub_gcode_line('G0 X1)'))

    def test_scrubbing_is_idempotent(self):
        for line in ['(a (b) c)', '(x ((y)) z)', 'G1 (p (q))', '(0.5″ [ref])']:
            once = scrub_gcode_line(line)
            self.assertEqual(once, scrub_gcode_line(once))
            self.assertEqual([], describe_violations(once))

    def test_strict_mode_raises_instead_of_repairing(self):
        bad = ['G0 X1', '(===== PERIMETER (NO TABS) =====)']
        with self.assertRaises(GCodeHygieneError) as ctx:
            finalize_gcode(bad, strict=True)
        self.assertIn('nested parenthesis comment', str(ctx.exception))
        self.assertIn('line 2', str(ctx.exception))
        # Production never raises: a cosmetic defect must not cost a team their output.
        self.assertIn('(===== PERIMETER, NO TABS, =====)', finalize_gcode(bad, strict=False))

    def test_strict_mode_is_on_for_this_suite(self):
        """tests/__init__.py must have armed strict mode, or the suite guards nothing."""
        import gcode_hygiene
        self.assertTrue(gcode_hygiene.STRICT)


if __name__ == '__main__':
    unittest.main()

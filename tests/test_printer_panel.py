"""Run the print wizard's browser-side printer panel tests under `make test`.

static/printer_panel.js is plain browser JavaScript with no build step, so its tests are
node's built-in runner (tests/printer_panel/*.test.mjs) rather than unittest. This shells
out to node so the one `make test` command still covers them, and skips with a clear
message where node is not installed. The skip covers a whole suite, so it names how to
install node, and PENGUINCAM_REQUIRE_NODE=1 turns it into a failure for anywhere that is
meant to be running it."""
import os
import shutil
import subprocess
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# A glob, not the directory: this node build treats a directory argument as a module to
# import rather than a tree to search, and fails with MODULE_NOT_FOUND.
TEST_GLOB = os.path.join('tests', 'printer_panel', '*.test.mjs')
# Set this to 1 to turn the missing-node skip into a failure.
REQUIRE_NODE = 'PENGUINCAM_REQUIRE_NODE'


# Said in full at the skip, because a one-word skip in a long run is a suite nobody
# notices has stopped running.
NO_NODE = (
    'node is not installed, so the printer panel JS tests did not run at all. Install it '
    '(Debian, Ubuntu and Raspberry Pi OS: sudo apt install nodejs; macOS: brew install '
    'node) and run them with: node --test "%s". Set %s=1 to make this a failure rather '
    'than a skip.')


class PrinterPanelJsTestCase(unittest.TestCase):
    def test_node_suite_passes(self):
        node = shutil.which('node')
        if not node:
            message = NO_NODE % (TEST_GLOB, REQUIRE_NODE)
            # The escape exists for a machine that is meant to cover the panel: there, a
            # silently skipped suite is worse than a red run.
            if os.environ.get(REQUIRE_NODE) == '1':
                self.fail(message)
            self.skipTest(message)
        result = subprocess.run([node, '--test', TEST_GLOB], cwd=REPO_ROOT,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0,
                         'node --test failed:\n%s\n%s' % (result.stdout, result.stderr))


class RequireNodeTestCase(unittest.TestCase):
    """The skip above hides a whole test suite, so it has to be loud and it has to be
    escapable. These run the case with node made invisible and read the outcome."""

    def _run_case(self, require):
        env = {REQUIRE_NODE: require} if require is not None else {}
        with mock.patch.object(shutil, 'which', return_value=None), \
                mock.patch.dict(os.environ, env):
            if require is None:
                os.environ.pop(REQUIRE_NODE, None)
            return PrinterPanelJsTestCase('test_node_suite_passes').run()

    def test_without_node_it_skips_and_says_how_to_install_node(self):
        result = self._run_case(None)
        self.assertEqual(len(result.skipped), 1, 'a missing node is a skip by default')
        self.assertEqual(result.failures, [])
        reason = result.skipped[0][1]
        self.assertIn('apt install nodejs', reason)
        self.assertIn('node --test', reason)
        self.assertIn(REQUIRE_NODE, reason)

    def test_the_environment_escape_turns_the_skip_into_a_failure(self):
        """A continuous integration job that means to cover the panel sets this, and finds
        out that it did not rather than reading a green run."""
        result = self._run_case('1')
        self.assertEqual(result.skipped, [])
        self.assertEqual(len(result.failures), 1)
        self.assertIn('apt install nodejs', result.failures[0][1])


if __name__ == '__main__':
    unittest.main()

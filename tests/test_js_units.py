"""Runs the node unit tests for the print wizard's pure JavaScript helpers."""
import shutil
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# Node 24 treats a positional argument as a glob, so a bare directory is loaded as a
# module and fails; the pattern is what makes it discover the test files.
JS_TESTS = REPO / "tests" / "js" / "*.test.js"


class NodeUnitTests(unittest.TestCase):
    def test_node_suite_passes(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not installed; the JS helper tests need it (CI and the dev machines have it)")
        result = subprocess.run([node, "--test", str(JS_TESTS)], capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout[-3000:] + result.stderr[-3000:])


if __name__ == "__main__":
    unittest.main()

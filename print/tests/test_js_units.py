"""Runs the node unit tests for the print wizard's pure JavaScript helpers."""
import shutil
import subprocess
import unittest
from pathlib import Path

JS_TESTS = Path(__file__).resolve().parent / "js"


class NodeUnitTests(unittest.TestCase):
    def test_node_suite_passes(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not installed; install node to run the JS helper tests")
        # Glob in Python, not in node: a bare directory argument is loaded as a module by
        # node's test runner, and an empty argument list would make it discover the whole
        # repository. Both would report something other than these tests.
        files = sorted(str(p) for p in JS_TESTS.glob("*.test.js"))
        self.assertTrue(files, f"no *.test.js files found in {JS_TESTS}")
        result = subprocess.run([node, "--test", *files], capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout[-3000:] + result.stderr[-3000:])


if __name__ == "__main__":
    unittest.main()

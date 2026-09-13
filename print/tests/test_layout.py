"""Guards the print/ package layout.

`print` is a legal package name in Python 3, but it is also a builtin, so a bare
`import print` binds the name in the importing module and shadows `print()` there.
Only the `from print import ...` forms are safe, and this test keeps it that way.
"""
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]        # the repository root
PKG = REPO / "print"

# `import print`, `import print.jobs`, `import print as p` - every form that binds
# the name. `from print import x` and `from print.routes import y` are fine.
BARE_IMPORT = re.compile(r"^\s*import\s+print(\s|\.|$)", re.M)

SKIP_DIRS = {".git", ".venv", "venv", "tools", "node_modules", "__pycache__", ".worktrees"}


def python_files():
    for path in REPO.rglob("*.py"):
        if not SKIP_DIRS.isdisjoint(path.relative_to(REPO).parts):
            continue
        yield path


class ImportFormTest(unittest.TestCase):
    def test_no_bare_import_print(self):
        offenders = [str(p.relative_to(REPO)) for p in python_files()
                     if BARE_IMPORT.search(p.read_text(encoding="utf-8", errors="replace"))]
        self.assertEqual(offenders, [], "a bare `import print` shadows the builtin; "
                                        "use `from print import <module>` instead")

    def test_regex_catches_the_forms_it_claims_to(self):
        for bad in ("import print", "import print.jobs", "    import print as p"):
            self.assertTrue(BARE_IMPORT.search(bad), bad)
        for good in ("from print import jobs", "from print.routes import init_print_routes",
                     "import printer_relay"):
            self.assertFalse(BARE_IMPORT.search(good), good)


class LayoutTest(unittest.TestCase):
    def test_the_package_holds_the_whole_print_path(self):
        for name in ("__init__.py", "routes.py", "jobs.py", "slicer.py", "sample_part.stl",
                     "profiles", "templates/print_wizard.html", "static/print_wizard.js",
                     "static/print_viewer.js", "scripts/install-orca.sh", "tests/js",
                     # The printer relay and the Pi daemon are part of the print path too.
                     "printer_relay.py", "printer_routes.py", "static/printer_panel.js",
                     "printer_daemon/__main__.py", "printer_daemon/install.sh",
                     "printer_daemon/requirements.txt", "tests/printer_panel"):
            self.assertTrue((PKG / name).exists(), f"print/{name} is missing")

    def test_nothing_print_shaped_is_left_at_the_top_level(self):
        strays = sorted(p.name for p in REPO.glob("print_*.py"))
        strays += sorted(p.name for p in REPO.glob("printer_*.py"))
        strays += [p.name for p in (REPO / "static").glob("printer_*.js")]
        strays += [p.name for p in (REPO / "tests").glob("test_printer_*.py")]
        self.assertEqual(strays, [], "print and printer modules belong in print/")


if __name__ == "__main__":
    unittest.main()

"""What the slicer writes and what the daemon sends to the printer must agree.

The daemon never imports the backend -- it runs on a Raspberry Pi where the backend is not
installed -- so nothing at run time makes these two agree. This file is the only place they
meet, and it fails the build when one side moves without the other.

Nothing here runs Orca. These are the two modules' own constants, which is what makes the
test cheap enough for the fast suite; print/tests/orca_integration_test.py checks the
sliced file itself.
"""
import re
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from print import slicer                                  # noqa: E402
from print.printer_daemon import printer as daemon        # noqa: E402


class ArchiveNameTest(unittest.TestCase):
    def test_the_daemon_recognises_the_extension_the_slicer_writes(self):
        """suffixed_name has to split the name the slicer produced, not guess at it."""
        self.assertIn(slicer.ARCHIVE_SUFFIX, daemon._EXTENSIONS)

    def test_the_job_id_goes_before_that_extension_and_comes_back_off(self):
        name = f"sample_part{slicer.ARCHIVE_SUFFIX}"
        sent = daemon.suffixed_name(name, 'j8f3a2c1')
        self.assertEqual(sent, f"sample_part-j8f3a2c1{slicer.ARCHIVE_SUFFIX}")
        self.assertEqual(daemon.strip_job_suffix(sent), name)

    def test_the_longest_extension_wins(self):
        """'.3mf' also appears in the list, and would otherwise split .gcode.3mf wrongly."""
        self.assertEqual(daemon.suffixed_name('part.gcode.3mf', 'j8f3a2c1'),
                         'part-j8f3a2c1.gcode.3mf')


class PlateNumberTest(unittest.TestCase):
    def test_the_daemon_starts_the_plate_the_slicer_exported(self):
        """print/slicer.py reads Metadata/plate_<n>.gcode back out of the archive; the
        daemon asks the printer to start plate <n>. Same n."""
        match = re.search(r'plate_(\d+)\.gcode$', slicer.GCODE_ENTRY)
        self.assertIsNotNone(match, slicer.GCODE_ENTRY)
        self.assertEqual(daemon.PLATE_NUMBER, int(match.group(1)))


class MaterialTest(unittest.TestCase):
    def test_the_daemon_names_no_material_plate_or_nozzle(self):
        """The profile set is the backend's business. Everything the printer needs to know
        about the filament, the plate and the nozzle travels inside the 3MF, which is why
        upstream can change profiles -- PLA to PETG, 0.4 to 0.6 High Flow, smooth plate to
        textured -- without touching the daemon. If a name ever appears here, this file has
        to say what keeps it in step with print/profiles/."""
        source = (REPO / 'print' / 'printer_daemon' / 'printer.py').read_text()
        code = '\n'.join(line for line in source.splitlines()
                         if not line.lstrip().startswith('#'))
        code = re.sub(r'"""(?:.|\n)*?"""', '', code)      # drop the docstrings
        # Whole words: PLATE_NUMBER is not the filament PLA.
        for word in ('PETG', 'PLA', 'ABS', 'Textured', 'High Flow', 'nozzle_diameter'):
            found = re.search(r'\b' + word.replace(' ', r'\s') + r'\b', code)
            self.assertIsNone(found, f'{word!r} names a profile choice, which belongs in '
                                     f'print/profiles/ and not in the daemon')


if __name__ == '__main__':
    unittest.main()

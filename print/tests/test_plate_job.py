"""run_plate_job (print/plate_job.py) with slice_plate replaced: no Orca run."""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[2]        # the repository root
sys.path.insert(0, str(REPO))

from print import plate_job  # noqa: E402
from print.plate_3mf import LEFT_OFF_MESSAGE  # noqa: E402
from print.plate_job import JobPart, PlateJob, run_plate_job  # noqa: E402
from print.slicer import DEFAULT_PROFILES, SliceError, SliceResult  # noqa: E402
from print.tests.test_plate_3mf import make_part, write_archive  # noqa: E402


def job_part(directory, name, size):
    part = make_part(directory, name, size)
    return JobPart(name=part['name'], stl_path=part['stl_path'], footprints=part['footprints'], triangles=12)


def make_job(directory):
    parts = {'r1' * 11: job_part(directory, 'Box', (20, 20, 10)), 'r2' * 11: job_part(directory, 'Peg', (8, 8, 30))}
    copies = [{'ref': 'r1' * 11, 'orientation': '+z', 'angle': 0, 'x': 50.0, 'y': 50.0},
              {'ref': 'r1' * 11, 'orientation': '+z', 'angle': 90, 'x': 100.0, 'y': 50.0},
              {'ref': 'r2' * 11, 'orientation': '+x', 'angle': 15, 'x': 150.0, 'y': 50.0}]
    return PlateJob(copies=copies, parts=parts, profiles=DEFAULT_PROFILES, overrides={}, filament='PETG',
                    process='0.24mm', delivered_name='Box_plus1-20261009-1432.gcode.3mf', pid='abcdef012345',
                    team=6238)


def fake_slice_plate(names, calls):
    def fake(model_3mf, profiles, output_dir, *args, **kwargs):
        calls.append((Path(model_3mf), profiles, Path(output_dir), args, kwargs))
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        archive = Path(output_dir) / 'plate.gcode.3mf'
        write_archive(archive, names)
        return SliceResult(output_path=archive, print_time_s=60.0, filament_m=1.0, filament_g=3.0,
                           layer_count=40, warnings=[])
    return fake


class RunPlateJobTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='plate-job-'))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        (self.tmp / 'stls').mkdir()
        self.scratch = self.tmp / 'scratch'
        self.scratch.mkdir()
        self.job = make_job(self.tmp / 'stls')

    def test_names_match_passes(self):
        calls = []
        fake = fake_slice_plate(['Peg #1', 'Box #2', 'Box #1'], calls)
        with mock.patch.object(plate_job, 'slice_plate', fake):
            result = run_plate_job(self.job, self.scratch)
        self.assertEqual(result.layer_count, 40)
        model, profiles, output_dir, _args, _kwargs = calls[0]
        self.assertTrue(model.is_file())
        self.assertEqual(model.parent, self.scratch)
        self.assertEqual(profiles, DEFAULT_PROFILES)
        self.assertTrue(str(output_dir).startswith(str(self.scratch)))

    def test_missing_name_fails_left_off(self):
        for names in (['Box #1', 'Box #2'], ['Box #1', 'Box #2', ''], ['Box #1', 'Box #2', 'Peg #1', 'Peg #1']):
            with self.subTest(names=names):
                with mock.patch.object(plate_job, 'slice_plate', fake_slice_plate(names, [])):
                    with self.assertRaises(SliceError) as ctx:
                        run_plate_job(self.job, self.scratch)
                self.assertEqual(ctx.exception.message, LEFT_OFF_MESSAGE)


if __name__ == '__main__':
    unittest.main()

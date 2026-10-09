"""Tests for testbed/__main__.py: the command line.

No test touches the network. The record tests answer every live call from a synthetic
cassette: the replay adapter stands in for the network under the recording adapter.
"""
import contextlib
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from requests.adapters import HTTPAdapter

from testbed import adapters, settings
from testbed.__main__ import main
from testbed.adapters import ReplayAdapter
from testbed.cassette import Cassette, Exchange

REPO = Path(__file__).resolve().parents[2]
STL_DIR = Path(__file__).resolve().parent / 'fixtures' / 'stl'
JSON = {'Content-Type': 'application/json'}
STL = {'Content-Type': 'application/octet-stream'}
DOCUMENTS = {'tb-box': {'did': 'd1', 'wid': 'w1', 'eid': 'e1', 'version_id': 'v1',
                        'parts': {'Box': 'JHD'}}}
STL_PATH = '/api/v13/parts/d/d1/w/w1/e/e1/partid/JHD/stl'
MM = {'units': ['millimeter'], 'mode': ['binary']}


def _exchange(path, status=200, query=None, body=None, stl=None):
    return Exchange(method='GET', host='cad.onshape.com', path=path, query=query or {},
                    request_body=None, status=status, headers=STL if stl else JSON,
                    body_text=None if stl else json.dumps(body if body is not None else {}),
                    body_file=None,
                    body_bytes=(STL_DIR / stl).read_bytes() if stl else None)


OWNER_LOOKUPS = [
    _exchange('/api/v13/users/sessioninfo',
              body={'id': '5f0e1d2c3b4a59687766aabb', 'email': 'owner@school.example.org',
                    'name': 'Real Owner'}),
    _exchange('/api/v13/companies', query={'activeOnly': ['true'], 'includeAll': ['false']},
              body={'items': []}),
]


class _CliCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        env = {k: v for k, v in os.environ.items()
               if k not in ('PENGUINCAM_TESTBED', 'ONSHAPE_ACCESS_KEY', 'ONSHAPE_SECRET_KEY',
                            'TESTBED_BUDGET', 'TESTBED_CYCLE_START', 'TESTBED_RUN_CAP')}
        env['XDG_STATE_HOME'] = str(self.tmp / 'state')
        self.enterContext(mock.patch.dict(os.environ, env, clear=True))
        for name, sub in (('FRESH_DIR', 'fresh'), ('CASSETTE_DIR', 'cassettes'),
                          ('MESSAGE_DIR', 'messages'), ('DOCUMENTS_FILE', 'documents.json')):
            self.enterContext(mock.patch.object(settings, name, self.tmp / sub))
        settings.reset_testbed_settings_for_tests()
        self.addCleanup(settings.reset_testbed_settings_for_tests)
        adapters.reset_testbed_adapters_for_tests()
        self.addCleanup(adapters.reset_testbed_adapters_for_tests)
        self.enterContext(mock.patch.object(
            socket.socket, 'connect', side_effect=AssertionError('network used')))

    @property
    def ledger_path(self):
        return self.tmp / 'state' / 'penguincam-testbed' / 'ledger.jsonl'

    def ledger_lines(self):
        if not self.ledger_path.exists():
            return []
        return [json.loads(line) for line in self.ledger_path.read_text().splitlines()]

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def give_keys(self):
        os.environ['ONSHAPE_ACCESS_KEY'] = 'test-access'
        os.environ['ONSHAPE_SECRET_KEY'] = 'test-secret'

    def give_ui_credentials(self):
        os.environ.update(ONSHAPE_USERNAME='testbed@example.invalid', ONSHAPE_PASSWORD='pw',
                          ONSHAPE_CLIENT_ID='devclientid0000', ONSHAPE_CLIENT_SECRET='dev')

    def onshape_answers_from(self, *exchanges):
        """Every live call the recording adapter makes is answered from these exchanges."""
        Cassette('part-export', list(exchanges), synthetic=True).save(settings.CASSETTE_DIR)
        settings.DOCUMENTS_FILE.write_text(json.dumps(DOCUMENTS))
        replay = ReplayAdapter()

        def network(adapter, request, **kwargs):
            return replay.send(request, **kwargs)
        self.enterContext(mock.patch.object(HTTPAdapter, 'send', network))


class RecordTest(_CliCase):
    def test_record_without_keys_names_variable(self):
        code, out, err = self.run_cli('record')
        self.assertEqual(code, 2)
        self.assertIn('missing environment variable: ONSHAPE_ACCESS_KEY', err)
        os.environ['ONSHAPE_ACCESS_KEY'] = 'test-access'
        code, out, err = self.run_cli('record', 'part-export')
        self.assertEqual(code, 2)
        self.assertIn('missing environment variable: ONSHAPE_SECRET_KEY', err)
        self.assertNotIn('test-access', out + err)
        self.assertEqual(self.ledger_lines(), [])

    def test_record_refuses_over_budget(self):
        self.give_keys()
        os.environ['TESTBED_BUDGET'] = '10'
        code, out, err = self.run_cli('record', 'part-export')
        self.assertEqual(code, 4)
        self.assertIn('budget of 10', err)
        self.assertEqual(self.ledger_lines(), [])
        # An unreadable ledger refuses too, rather than undercount.
        os.environ['TESTBED_BUDGET'] = '250'
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        self.ledger_path.write_text('not json\n')
        code, out, err = self.run_cli('record', 'part-export')
        self.assertEqual(code, 4)
        self.assertIn('line 1 is unreadable', err)

    def test_record_rejects_unknown_and_panel_scenarios(self):
        self.give_keys()
        code, _, err = self.run_cli('record', 'no-such-scenario')
        self.assertEqual(code, 1)
        self.assertIn('unknown scenario: no-such-scenario', err)
        code, _, err = self.run_cli('record', 'part-select')
        self.assertEqual(code, 1)
        self.assertIn('record --ui', err)

    def test_record_writes_fresh_cassette_and_drift_report(self):
        self.give_keys()
        self.onshape_answers_from(
            *OWNER_LOOKUPS,
            _exchange(STL_PATH, stl='box_inch.stl'),
            _exchange(STL_PATH, query=MM, stl='box_mm.stl'),
            _exchange('/api/v13/translations/translationformats', body=[]),
            _exchange('/api/v13/parts/d/d1/v/v1/e/e1/partid/JHD/stl', query=MM,
                      stl='box_mm.stl'))
        code, out, err = self.run_cli('record', 'part-export')
        self.assertEqual(code, 0, err)
        fresh = Cassette.load(settings.fresh_cassette_dir(), 'part-export')
        self.assertEqual(len(fresh.exchanges), 4)         # the owner lookups are not in it
        self.assertTrue(settings.drift_report_path().is_file())
        self.assertIn('counted calls: 6, remaining budget: 244', out)
        self.assertEqual(sum(line['counted'] for line in self.ledger_lines()), 6)

    def test_record_stops_on_402(self):
        self.give_keys()
        self.onshape_answers_from(*OWNER_LOOKUPS,
                                  _exchange(STL_PATH, status=402, body={'message': 'limit'}))
        code, out, err = self.run_cli('record', 'part-export')
        self.assertEqual(code, 4)
        self.assertIn('402', err)
        self.assertIn('part-export', err)
        self.assertIn('counted calls: 2, remaining budget: 248', out)
        statuses = [(line['status'], line['counted']) for line in self.ledger_lines()]
        self.assertEqual(statuses, [(200, True), (200, True), (402, False)])
        self.assertFalse((settings.fresh_cassette_dir() / 'part-export.json').exists())

    def test_record_reports_a_partial_run(self):
        self.give_keys()
        self.onshape_answers_from(*OWNER_LOOKUPS, _exchange(STL_PATH, stl='box_inch.stl'),
                                  _exchange(STL_PATH, query=MM, status=500))
        code, out, err = self.run_cli('record', 'part-export')
        self.assertEqual(code, 1)
        self.assertIn('stopped during part-export', err)
        self.assertIn('HTTP 500', err)
        self.assertIn('2 exchanges', err)                  # the 200 and the 500
        self.assertIn('counted calls: 3, remaining budget: 247', out)
        self.assertFalse((settings.fresh_cassette_dir() / 'part-export.json').exists())


class OtherCommandsTest(_CliCase):
    def test_ledger_command_prints_totals(self):
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        lines = [{'t': '2026-10-09T12:00:00+00:00', 'scenario': 'part-export', 'method': 'GET',
                  'path': f'/api/v13/call-{n}', 'status': 200 if n % 2 else 307,
                  'kind': 'response', 'counted': True} for n in range(12)]
        lines.append(dict(lines[0], path='/api/v13/limit', status=402, counted=False))
        self.ledger_path.write_text(''.join(json.dumps(line) + '\n' for line in lines))
        env = dict(os.environ, TESTBED_BUDGET='100', TESTBED_CYCLE_START='2026-01-01')
        result = subprocess.run([sys.executable, '-m', 'testbed', 'ledger'], cwd=REPO, env=env,
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('counted calls since 2026-01-01: 12', result.stdout)
        self.assertIn('budget: 100', result.stdout)
        self.assertIn('remaining: 88', result.stdout)
        self.assertIn('/api/v13/limit', result.stdout)
        self.assertIn('/api/v13/call-3', result.stdout)
        self.assertNotIn('/api/v13/call-2', result.stdout)  # only the last ten lines

    def test_accept_unknown_scenario_errors(self):
        code, out, err = self.run_cli('accept', 'part-export', 'no-such-scenario')
        self.assertEqual(code, 1)
        self.assertIn('unknown scenario: no-such-scenario', err)
        code, out, err = self.run_cli('accept', 'part-export')
        self.assertEqual(code, 1)
        self.assertIn('no fresh recording of part-export', err)

    def test_accept_and_drift(self):
        code, out, err = self.run_cli('drift')
        self.assertEqual(code, 1)
        self.assertIn('nothing fresh', err)
        recording = Cassette('part-export', OWNER_LOOKUPS[1:])
        recording.save(settings.CASSETTE_DIR)
        recording.save(settings.fresh_cassette_dir())
        code, out, err = self.run_cli('drift')
        self.assertEqual(code, 0, err)
        self.assertIn('clean', settings.drift_report_path().read_text())
        self.assertIn('clean', out)
        code, out, err = self.run_cli('accept', 'part-export')
        self.assertEqual(code, 0, err)
        self.assertIn(str(settings.CASSETTE_DIR / 'part-export.json'), out)
        self.assertFalse((settings.fresh_cassette_dir() / 'part-export.json').exists())

    def test_build_docs_without_folder_names_variable(self):
        self.give_keys()
        code, out, err = self.run_cli('build-docs')
        self.assertEqual(code, 2)
        self.assertIn('TESTBED_FOLDER_ID', err)
        self.assertIn("last segment of the folder's URL", err)
        self.assertEqual([line for line in self.ledger_lines() if line['counted']], [])

    def test_build_docs_tells_owner_to_move_documents_outside_folder(self):
        self.give_keys()
        os.environ['TESTBED_FOLDER_ID'] = 'f00000000000000000000f01'
        fixture = Path(__file__).resolve().parent / 'fixtures' / 'cassettes'
        cassette = Cassette.load(fixture, 'synthetic-build-docs')
        for ex in cassette.exchanges:
            if ex.method == 'GET' and ex.path == '/api/v13/documents':
                ex.body_text = json.dumps({'items': []})     # parentId placed nothing
        cassette.scenario = 'build-docs'
        cassette.save(settings.CASSETTE_DIR)
        replay = ReplayAdapter()
        self.enterContext(mock.patch.object(
            HTTPAdapter, 'send', lambda adapter, request, **kw: replay.send(request, **kw)))
        code, out, err = self.run_cli('build-docs')
        self.assertEqual(code, 1, err)
        self.assertIn('tb-box was created outside the test folder', out)
        self.assertIn('move it into the test folder in Onshape', out)
        self.assertIn('counted calls: 8', out)        # two listings and six creations

    def test_build_docs_refusal_is_one_line(self):
        self.give_keys()
        os.environ['TESTBED_FOLDER_ID'] = 'f00000000000000000000f01'
        from testbed import documents
        refused = documents.NotATestDocument('tb-box is not listed in the test folder')
        with mock.patch.object(documents, 'build_test_documents', side_effect=refused):
            code, out, err = self.run_cli('build-docs')
        self.assertEqual(code, 1)
        self.assertIn('build-docs stopped: tb-box is not listed', err)
        self.assertNotIn('Traceback', err)

    def test_record_ui_without_credentials_names_variable(self):
        code, out, err = self.run_cli('record', '--ui', 'panel-load')
        self.assertEqual(code, 2)
        self.assertIn('ONSHAPE_USERNAME', err)

    def test_record_ui_stopped_by_onshape_check_exits_3(self):
        from testbed import ui_run
        self.give_ui_credentials()
        stop = ui_run.OnshapeCheckStopped('a challenge frame (sign-in page); screenshot: x.png')
        with mock.patch.object(ui_run, 'run_onshape_ui', side_effect=stop):
            code, out, err = self.run_cli('record', '--ui', 'panel-load')
        self.assertEqual(code, 3)
        self.assertIn('stopped by an Onshape check: a challenge frame', err)
        self.assertIn('counted calls: 0', out)

    def test_record_ui_failed_step_exits_1(self):
        from testbed import ui_run
        self.give_ui_credentials()
        failed = ui_run.OnshapeStepFailed("panel-load: step 'open panel' failed")
        with mock.patch.object(ui_run, 'run_onshape_ui', side_effect=failed):
            code, out, err = self.run_cli('record', '--ui', 'panel-load')
        self.assertEqual(code, 1)
        self.assertIn("step 'open panel' failed", err)


if __name__ == '__main__':
    unittest.main()

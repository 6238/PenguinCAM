"""Tests for the print scenarios (testbed/scenarios): definitions, recording state and the
print-export runner, replayed from a synthetic cassette. No browser."""
import os
import shutil
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from onshape_integration import OnshapeClient
from testbed import adapters, settings
from testbed.adapters import ReplayAdapter, set_current_scenario
from testbed.cassette import Cassette
from testbed.scenarios import (ACTIONS, SCENARIOS, TEST_DOCUMENT_NAMES, run_print_export,
                               run_scenario_api, scenario_is_recorded)

FIXTURE_CASSETTES = Path(__file__).resolve().parent / 'fixtures' / 'cassettes'
# The ids make_print_fixtures.py writes the synthetic cassettes with.
DID, WID = 'd00000000000000000000001', 'a00000000000000000000001'
EID_PS, EID_ASM = 'e00000000000000000000001', 'e00000000000000000000002'
DID2, VID2, EID2 = 'd00000000000000000000002', 'b00000000000000000000002', 'e00000000000000000000003'
SYNTHETIC_DOCUMENTS = {
    'tb-two-parts': {'did': DID, 'wid': WID, 'eid': EID_PS,
                     'parts': {'Box': 'JHD', 'Cylinder': 'JKD'}},
    'tb-assembly': {'did': DID, 'wid': WID, 'eid': EID_ASM, 'parts': {}},
}
PRINT_ESTIMATES = {'print-select-parts': 13, 'print-dialog-parts': 11,
                   'print-assembly-part': 10, 'print-export': 12}


class PrintScenarioDefinitionsTest(unittest.TestCase):
    def test_print_scenarios_defined_with_known_documents_and_estimates(self):
        # A changed step list must come with a recount (R13).
        self.assertEqual({name: SCENARIOS[name].estimate for name in PRINT_ESTIMATES},
                         PRINT_ESTIMATES)
        documents = {name: SCENARIOS[name].document for name in PRINT_ESTIMATES}
        self.assertEqual(documents, {'print-select-parts': 'tb-two-parts',
                                     'print-dialog-parts': 'tb-two-parts',
                                     'print-assembly-part': 'tb-assembly',
                                     'print-export': 'tb-two-parts'})
        for name in PRINT_ESTIMATES:
            scenario = SCENARIOS[name]
            with self.subTest(scenario=name):
                self.assertIn(scenario.document, TEST_DOCUMENT_NAMES)
                self.assertEqual(scenario.panel, name != 'print-export')
                for step in scenario.steps:
                    self.assertIn(step.action, ACTIONS + (None,))
                if scenario.panel:
                    actions = [step.action for step in scenario.steps]
                    self.assertEqual(actions[1:3], ['choose-print', 'print-parts'])
                    # The print page asks for parts itself: no strip ask or dialog button.
                    self.assertFalse(set(actions) & {'ask-part', 'ask-two-parts', 'dialog-part',
                                                     'dialog-parts', 'close-dialog'})
        select = SCENARIOS['print-select-parts'].steps
        self.assertEqual([s.name for s in select if not s.recorded_only],
                         ['open panel', 'choose print', 'print parts', 'select part Box',
                          'select part Cylinder', 'deselect'])
        reference = [s for s in select if s.recorded_only]
        self.assertEqual([s.action for s in reference], ['ask-unbounded', None])
        self.assertEqual(reference[1].select, ['Box', 'Cylinder'])
        dialog = [s.action for s in SCENARIOS['print-dialog-parts'].steps]
        self.assertIn('print-dialog', dialog)
        self.assertEqual(SCENARIOS['print-dialog-parts'].steps[-1].name, 'dialog closed')
        self.assertEqual(SCENARIOS['print-assembly-part'].steps[-1].select, ['Box <1>'])

    def test_unrecorded_print_scenarios_reported(self):
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        cassettes, messages = tmp / 'cassettes', tmp / 'messages'
        self.enterContext(mock.patch.object(settings, 'CASSETTE_DIR', cassettes))
        self.enterContext(mock.patch.object(settings, 'MESSAGE_DIR', messages))
        for name in PRINT_ESTIMATES:
            self.assertFalse(scenario_is_recorded(name), name)
        for name in PRINT_ESTIMATES:
            Cassette(name, [], synthetic=True).save(cassettes)
        # A panel scenario needs its message log too; print-export is API only.
        self.assertEqual({name: scenario_is_recorded(name) for name in PRINT_ESTIMATES},
                         {'print-select-parts': False, 'print-dialog-parts': False,
                          'print-assembly-part': False, 'print-export': True})


class PrintExportRunnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        env = {k: v for k, v in os.environ.items()
               if k not in ('PENGUINCAM_TESTBED', 'ONSHAPE_ACCESS_KEY', 'ONSHAPE_SECRET_KEY')}
        env.update(PENGUINCAM_TESTBED='replay', XDG_STATE_HOME=str(self.tmp / 'state'))
        self.enterContext(mock.patch.dict(os.environ, env, clear=True))
        settings.reset_testbed_settings_for_tests()
        self.addCleanup(settings.reset_testbed_settings_for_tests)
        adapters.reset_testbed_adapters_for_tests()
        self.addCleanup(adapters.reset_testbed_adapters_for_tests)
        cassettes = self.tmp / 'cassettes'
        shutil.copytree(FIXTURE_CASSETTES, cassettes)
        self.enterContext(mock.patch.object(settings, 'CASSETTE_DIR', cassettes))
        self.enterContext(mock.patch.object(socket.socket, 'connect',
                                            side_effect=AssertionError('network used')))
        self.answered = []          # (method, path, status) of every replayed request
        real_send = ReplayAdapter.send

        def spy(adapter, request, **kwargs):
            response = real_send(adapter, request, **kwargs)
            self.answered.append((request.method, request.path_url.split('?')[0],
                                  response.status_code))
            return response
        self.enterContext(mock.patch.object(ReplayAdapter, 'send', spy))

    def test_print_export_runs_against_synthetic_cassette(self):
        cassette = Cassette.load(settings.CASSETTE_DIR, 'synthetic-print-export-run')
        self.assertTrue(cassette.synthetic)
        set_current_scenario('synthetic-print-export-run')
        client = OnshapeClient.from_api_keys()
        results = run_print_export(client, SYNTHETIC_DOCUMENTS)
        self.assertEqual([r.source.part_id for r in results], ['JHD', 'JKD', 'JHD'])
        self.assertEqual(results[2].source.document_id, DID2)
        self.assertEqual(results[2].source.link_document_id, DID)
        self.assertTrue(all(r.stl[80:84] != b'\0\0\0\0' for r in results))
        api = '/api/v13'
        self.assertEqual([path for _m, path, _s in self.answered], [
            f'{api}/documents/d/{DID}/w/{WID}/elements',
            f'{api}/elements/d/{DID}/w/{WID}/e/{EID_PS}/configuration',
            f'{api}/parts/d/{DID}/w/{WID}/e/{EID_PS}',
            f'{api}/parts/d/{DID}/w/{WID}/e/{EID_PS}/partid/JHD/stl',
            f'{api}/blob/synthetic/d-{DID}-w-{WID}-e-{EID_PS}-JHD.stl',
            f'{api}/parts/d/{DID}/w/{WID}/e/{EID_PS}/partid/JKD/stl',
            f'{api}/blob/synthetic/d-{DID}-w-{WID}-e-{EID_PS}-JKD.stl',
            f'{api}/assemblies/d/{DID}/w/{WID}/e/{EID_ASM}',
            f'{api}/documents/d/{DID}/w/{WID}/elements',
            f'{api}/parts/d/{DID2}/v/{VID2}/e/{EID2}/partid/JHD/stl',
            f'{api}/blob/synthetic/d-{DID2}-v-{VID2}-e-{EID2}-JHD.stl',
        ])
        counted = sum(1 for _m, _p, status in self.answered if 200 <= status < 400)
        self.assertEqual(counted, 11)
        self.assertLessEqual(counted, SCENARIOS['print-export'].estimate)
        # The cassette holds exactly these calls, no more.
        self.assertEqual(len(cassette.exchanges), len(self.answered))
        self.answered.clear()
        run_scenario_api('print-export', client, SYNTHETIC_DOCUMENTS)     # the CLI's entry
        self.assertEqual(len(self.answered), 11)


if __name__ == '__main__':
    unittest.main()

"""Tests for testbed/scenarios: the definitions, the part-export runner and the mesh checks.

No browser and no Orca. The mesh checks run against the synthetic STL fixtures until
part-export is recorded, and against the recorded exports, replayed, from then on.
"""
import json
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from onshape_integration import OnshapeClient
from print.slicer import part_info
from testbed import adapters, settings
from testbed.adapters import ReplayAdapter, set_current_scenario
from testbed.cassette import Cassette, Exchange
from testbed.mesh import mesh_size_mm
from testbed.scenarios import (ACTIONS, SCENARIOS, TEST_DOCUMENT_NAMES, Scenario, Step,
                               export_part_stl, run_scenario_api, scenario_is_recorded)

STL_DIR = Path(__file__).resolve().parent / 'fixtures' / 'stl'
MM_BINARY = {'units': 'millimeter', 'mode': 'binary'}
# The fixture standing in for each (document, units) export until part-export is recorded.
FIXTURE_EXPORTS = {
    ('tb-box', 'millimeter'): 'box_mm.stl',
    ('tb-box', 'inch'): 'box_inch.stl',
    ('tb-inch', 'millimeter'): 'inch_mm.stl',
    ('tb-oversized', 'millimeter'): 'slab_mm.stl',
}
EXPORT_PARAMS = {'inch': None, 'millimeter': MM_BINARY}     # None: Onshape's defaults, inches


def _testbed_env(mode, state):
    env = {k: v for k, v in os.environ.items()
           if k not in ('PENGUINCAM_TESTBED', 'ONSHAPE_ACCESS_KEY', 'ONSHAPE_SECRET_KEY')}
    env['XDG_STATE_HOME'] = str(state)
    if mode:
        env['PENGUINCAM_TESTBED'] = mode
    return env


def _replayed_export(document, units):
    """The recorded part-export answer for one export, replayed with no live call."""
    documents = json.loads(settings.DOCUMENTS_FILE.read_text())
    doc = documents[document]
    with tempfile.TemporaryDirectory() as state, \
            mock.patch.dict(os.environ, _testbed_env('replay', state), clear=True):
        settings.reset_testbed_settings_for_tests()
        adapters.reset_testbed_adapters_for_tests()
        try:
            set_current_scenario('part-export')
            client = OnshapeClient.from_api_keys()
            body, _hops = export_part_stl(client, doc, next(iter(doc['parts'].values())),
                                          EXPORT_PARAMS[units])
        finally:
            adapters.reset_testbed_adapters_for_tests()
            settings.reset_testbed_settings_for_tests()
    return body


def exported_stl(document, units):
    """The STL bytes of `document` exported in `units`: recorded if part-export is, else a fixture."""
    if scenario_is_recorded('part-export') and settings.DOCUMENTS_FILE.exists():
        return _replayed_export(document, units)
    return (STL_DIR / FIXTURE_EXPORTS[(document, units)]).read_bytes()


class MeshChecksTest(unittest.TestCase):
    def assertSizeNear(self, got, expected, tolerance=0.1):
        for g, e in zip(got, expected, strict=True):
            self.assertLessEqual(abs(g - e), tolerance, f'{got} is not {expected} ± {tolerance}')

    def test_fixture_dimensions(self):
        self.assertSizeNear(mesh_size_mm(exported_stl('tb-box', 'millimeter'), 'millimeter'),
                            (10, 30, 40))
        self.assertSizeNear(mesh_size_mm(exported_stl('tb-inch', 'millimeter'), 'millimeter'),
                            (12.7, 25.4, 50.8))
        # Onshape's default export is in inches whatever the document's units.
        self.assertSizeNear(mesh_size_mm(exported_stl('tb-box', 'inch'), 'inch'), (10, 30, 40))

    def test_oversized_exceeds_plate(self):
        plate = part_info()['printer']['bed_mm']
        largest = max(mesh_size_mm(exported_stl('tb-oversized', 'millimeter'), 'millimeter'))
        self.assertGreater(largest, max(plate['x'], plate['y']))


class DefinitionsTest(unittest.TestCase):
    def test_definitions_reference_known_documents(self):
        # The print scenarios' own estimates are checked in test_print_scenarios.
        self.assertEqual(set(SCENARIOS), {'panel-load', 'to-print', 'part-select',
                                          'multi-select', 'assembly-select', 'part-export',
                                          'print-select-parts', 'print-dialog-parts',
                                          'print-assembly-part', 'print-export',
                                          'print-configured-part'})
        estimates = {name: scenario.estimate for name, scenario in SCENARIOS.items()
                     if not name.startswith('print-')}
        self.assertEqual(estimates, {'panel-load': 6, 'to-print': 6, 'part-select': 2,
                                     'multi-select': 2, 'assembly-select': 2, 'part-export': 45})
        for name, scenario in SCENARIOS.items():
            with self.subTest(scenario=name):
                self.assertIsInstance(scenario, Scenario)
                self.assertEqual(scenario.name, name)
                self.assertIn(scenario.document, TEST_DOCUMENT_NAMES)
                self.assertEqual(scenario.panel, name not in ('part-export', 'print-export'))
                names = [step.name for step in scenario.steps]
                self.assertEqual(len(names), len(set(names)), 'step names must be unique')
                for step in scenario.steps:
                    self.assertIsInstance(step, Step)
                    self.assertIn(step.action, ACTIONS + (None,))
                    self.assertTrue(step.instruction)
                    if step.select:     # the fake Onshape host finds selections by step name
                        prefix = 'select part ' if len(step.select) == 1 else 'select parts '
                        self.assertEqual(step.name, prefix + ', '.join(step.select))
        # The selection scenarios ask both ways (spec 5.7).
        for name in ('part-select', 'multi-select', 'assembly-select'):
            actions = {step.action for step in SCENARIOS[name].steps}
            self.assertTrue(actions & {'ask-part', 'ask-two-parts'}, name)
            self.assertTrue(actions & {'dialog-part', 'dialog-parts'}, name)
            self.assertIn('close-dialog', actions, name)

    def test_unrecorded_scenario_reported(self):
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        cassettes, messages = tmp / 'cassettes', tmp / 'messages'
        self.enterContext(mock.patch.object(settings, 'CASSETTE_DIR', cassettes))
        self.enterContext(mock.patch.object(settings, 'MESSAGE_DIR', messages))
        self.assertFalse(scenario_is_recorded('part-select'))
        self.assertFalse(scenario_is_recorded('part-export'))
        Cassette('part-select', [], synthetic=True).save(cassettes)
        self.assertFalse(scenario_is_recorded('part-select'))      # a panel needs its messages
        messages.mkdir()
        (messages / 'part-select.json').write_text('{}')
        self.assertTrue(scenario_is_recorded('part-select'))
        Cassette('part-export', [], synthetic=True).save(cassettes)
        self.assertTrue(scenario_is_recorded('part-export'))       # API only: the cassette


class PartExportRunnerTest(unittest.TestCase):
    def test_part_export_runner_calls(self):
        documents = {
            'tb-box': {'did': 'D1', 'wid': 'W1', 'eid': 'E1', 'version_id': 'V1',
                       'parts': {'Box': 'JHD'}},
            'tb-two-parts': {'did': 'D2', 'wid': 'W2', 'eid': 'E2',
                             'parts': {'Box': 'JAD', 'Cylinder': 'JBD'}},
            # An assembly has no part STL path; its parts are exported from tb-box.
            'tb-assembly': {'did': 'D3', 'wid': 'W3', 'eid': 'E3', 'parts': {'Box <1>': 'JKD'}},
            # Configured: part-export would export only its default; print-configured-part
            # exercises it.
            'tb-configured': {'did': 'D4', 'wid': 'W4', 'eid': 'E4', 'parts': {'Spacer': 'JSD'}},
        }
        client = mock.Mock()
        client._make_api_request.return_value = mock.Mock(status_code=200, text='{}')
        with mock.patch('testbed.scenarios.export_part_stl',
                        return_value=(b'solid', [])) as export:
            run_scenario_api('part-export', client, documents)
            exports = [(c.args[1]['did'], c.args[1].get('vid'), c.args[2], c.args[3])
                       for c in export.call_args_list]
            self.assertEqual(sorted(exports, key=repr), sorted([
                ('D1', None, 'JHD', None), ('D1', None, 'JHD', MM_BINARY),
                ('D2', None, 'JAD', None), ('D2', None, 'JAD', MM_BINARY),
                ('D2', None, 'JBD', None), ('D2', None, 'JBD', MM_BINARY),
                ('D1', 'V1', 'JHD', MM_BINARY),
            ], key=repr))
            client._make_api_request.assert_called_once_with(
                'GET', '/translations/translationformats')
            export.reset_mock()
            client.reset_mock()
            run_scenario_api('part-select', client, documents)    # no API actions
            export.assert_not_called()
            client._make_api_request.assert_not_called()


class ExportRedirectTest(unittest.TestCase):
    """export_part_stl over the replay adapter, with a synthetic cassette."""

    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(mock.patch.dict(os.environ, _testbed_env('replay', self.tmp / 'state'),
                                          clear=True))
        settings.reset_testbed_settings_for_tests()
        self.addCleanup(settings.reset_testbed_settings_for_tests)
        adapters.reset_testbed_adapters_for_tests()
        self.addCleanup(adapters.reset_testbed_adapters_for_tests)
        self.cassettes = self.tmp / 'cassettes'
        self.enterContext(mock.patch.object(settings, 'CASSETTE_DIR', self.cassettes))
        self.enterContext(mock.patch.object(socket.socket, 'connect',
                                            side_effect=AssertionError('network used')))
        self.sent = []          # (url, Authorization header or None) of every request
        real_send = ReplayAdapter.send

        def spy(adapter, request, **kwargs):
            self.sent.append((request.url, request.headers.get('Authorization')))
            return real_send(adapter, request, **kwargs)
        self.enterContext(mock.patch.object(ReplayAdapter, 'send', spy))
        self.doc = {'did': 'D1', 'wid': 'W1', 'eid': 'E1'}
        self.path = '/api/v13/parts/d/D1/w/W1/e/E1/partid/JHD/stl'
        Cassette('export', [
            self.ex(self.path, status=307,
                    headers={'Location': 'https://cad.onshape.com/api/v13/blob/part.stl'}),
            self.ex('/api/v13/blob/part.stl', 'solid onshape'),
            self.ex(self.path, status=307, query={'units': ['millimeter'], 'mode': ['binary']},
                    headers={'Location': 'https://example.com/bucket/part.stl'}),
            self.ex('/bucket/part.stl', 'solid elsewhere', host='example.com'),
            self.ex('/api/v13/parts/d/D1/w/W1/e/E1/partid/LOOP/stl', status=307,
                    headers={'Location': 'https://cad.onshape.com/api/v13/parts/d/D1/w/W1/e/E1/partid/LOOP/stl'}),
            self.ex('/api/v13/parts/d/D1/w/W1/e/E1/partid/NOWHERE/stl', status=307),
        ], synthetic=True).save(self.cassettes)

    def ex(self, path, body='', status=200, host='cad.onshape.com', query=None, headers=None):
        return Exchange(method='GET', host=host, path=path, query=query or {},
                        request_body=None, status=status, headers=headers or {},
                        body_text=body, body_file=None)

    def run_export(self, client, params, part_id='JHD'):
        set_current_scenario('export')
        self.sent.clear()
        return export_part_stl(client, self.doc, part_id, params)

    def test_export_follows_redirect_and_reattaches_auth_only_for_onshape(self):
        apikey = OnshapeClient.from_api_keys()
        oauth = OnshapeClient()
        oauth.access_token = 'replay-token'
        for client, scheme in ((apikey, 'Basic '), (oauth, 'Bearer ')):
            with self.subTest(auth=client.auth_mode):
                with mock.patch.object(client.session, 'request',
                                       wraps=client.session.request) as request:
                    body, hops = self.run_export(client, None)
                # allow_redirects reaches session.request through _make_api_request's kwargs.
                self.assertIs(request.call_args_list[0].kwargs['allow_redirects'], False)
                self.assertEqual(body, b'solid onshape')
                self.assertEqual(hops, ['https://cad.onshape.com/api/v13/blob/part.stl'])
                self.assertEqual([url for url, _ in self.sent],
                                 [f'https://cad.onshape.com{self.path}',
                                  'https://cad.onshape.com/api/v13/blob/part.stl'])
                for url, auth in self.sent:
                    self.assertTrue(auth and auth.startswith(scheme), url)

                body, hops = self.run_export(client, MM_BINARY)
                self.assertEqual(body, b'solid elsewhere')
                self.assertEqual(hops, ['https://example.com/bucket/part.stl'])
                (first, first_auth), (foreign, foreign_auth) = self.sent
                self.assertTrue(first_auth.startswith(scheme))
                self.assertEqual(foreign, 'https://example.com/bucket/part.stl')
                self.assertIsNone(foreign_auth)

                with self.assertRaisesRegex(RuntimeError, 'redirect'):
                    self.run_export(client, None, part_id='LOOP')
                self.assertEqual(len(self.sent), 4)     # the request and three hops
                with self.assertRaisesRegex(RuntimeError, '307 without Location'):
                    self.run_export(client, None, part_id='NOWHERE')


if __name__ == '__main__':
    unittest.main()

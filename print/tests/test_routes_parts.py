"""POST /print/onshape/parts and GET /print/parts/<ref>.stl through the Flask test client.

Each request replays one synthetic flow cassette (testbed/tests/fixtures/cassettes,
written by testbed/tests/fixtures/make_print_fixtures.py); no call reaches Onshape.
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[2]        # the repository root
sys.path.insert(0, str(REPO))

from frc_cam_gui_app import app, limiter  # noqa: E402
from print import events  # noqa: E402
from print import limits  # noqa: E402
from print import routes as print_routes  # noqa: E402
from print.onshape_parts import CONFIGURED_MESSAGE, NO_CONTEXT_MESSAGE, ElementCache  # noqa: E402
from print.part_store import PartStore  # noqa: E402
from print.plate import ORIENTATIONS  # noqa: E402
from print.tests.onshape_replay import replay_client  # noqa: E402
from testbed.adapters import set_current_scenario  # noqa: E402

PID, OTHER = "abcdef012345", "0123456789ab"
DID = 'd00000000000000000000001'
WID = 'a00000000000000000000001'
EID_PS = 'e00000000000000000000001'
CONTEXT = {'documentId': DID, 'workspaceId': WID, 'versionId': '', 'microversionId': '',
           'elementId': EID_PS, 'server': 'https://cad.onshape.com'}
BOX_BYTES = 84 + 12 * 50
ACCESS_KEY, SECRET_KEY = 'AKdistinctaccesskey0001', 'SKdistinctsecretkey0001'


class PartsRouteBase(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        self.client = app.test_client()
        with self.client.session_transaction() as sess:
            sess["app_verified"] = True
        limiter.reset()
        limiter.enabled = False
        self.addCleanup(setattr, limiter, "enabled", True)
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = PartStore(root / "print_parts")
        self.onshape_session = True
        self.client_obj = None
        self.lines = []
        for name, value in (("part_store", self.store), ("element_cache", ElementCache()),
                            ("has_onshape_session", lambda: self.onshape_session),
                            ("onshape_client", lambda: self.client_obj)):
            patch = mock.patch.object(print_routes.ctx, name, value, create=True)
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(events.configure_print_events, events._log, events._metrics)
        events.configure_print_events(self.lines.append, mock.Mock())

    def replay(self, scenario):
        """Enter the replay client for `scenario`; it answers ctx.onshape_client()."""
        client, sent = self.enterContext(replay_client(scenario))
        client.api_access_key, client.api_secret_key = ACCESS_KEY, SECRET_KEY
        self.client_obj = client
        return sent

    def post_parts(self, selections, via='selection', pid=PID, **extra):
        body = dict({'context': CONTEXT, 'via': via, 'selections': selections}, **extra)
        return self.client.post('/print/onshape/parts', json=body, headers={'X-Print-Page': pid})

    def stl(self, ref, pid=PID):
        return self.client.get(f'/print/parts/{ref}.stl', headers={'X-Print-Page': pid})


class PartsRouteTest(PartsRouteBase):
    def test_signin_gate_401_with_code(self):
        sent = self.replay('synthetic-print-none')
        self.onshape_session = False
        resp = self.post_parts([{'partId': 'JHD'}])
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.get_json(), {'error': print_routes.SIGNIN_EXPIRED_MESSAGE,
                                           'code': 'signin_expired'})
        # A session that passes the gate but whose client is gone or dead answers the same.
        self.onshape_session = True
        self.client_obj.credentials_dead = True
        self.assertEqual(self.post_parts([{'partId': 'JHD'}]).status_code, 401)
        self.client_obj = None
        self.assertEqual(self.post_parts([{'partId': 'JHD'}]).get_json()['code'], 'signin_expired')
        self.assertEqual(sent, [])

    def test_page_id_and_context_checked(self):
        self.replay('synthetic-print-none')
        resp = self.client.post('/print/onshape/parts', json={'context': CONTEXT, 'via': 'selection',
                                                              'selections': []})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()['error'], print_routes.PAGE_ID_MESSAGE)
        resp = self.client.post('/print/onshape/parts', json={'context': dict(CONTEXT, elementId=''),
                                                              'via': 'selection', 'selections': []},
                                headers={'X-Print-Page': PID})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()['error'], NO_CONTEXT_MESSAGE)

    def test_two_parts_listed_with_sizes_triangles_and_footprints(self):
        self.replay('synthetic-print-ps-two')
        resp = self.post_parts([{'partId': 'JHD'}, {'selectionId': 'JKD'}])
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data['errors'], [])
        self.assertEqual([p['name'] for p in data['parts']], ['Box', 'Cylinder'])
        box, cylinder = data['parts']
        self.assertEqual(box['size_mm'], {'x': 10.0, 'y': 20.0, 'z': 30.0})
        self.assertEqual(box['triangles'], 12)
        self.assertEqual(box['source'], {'documentId': DID, 'wvm': 'w', 'wvmId': WID,
                                         'elementId': EID_PS, 'partId': 'JHD'})
        self.assertEqual(set(box['footprints']), set(ORIENTATIONS))
        self.assertEqual(box['footprints']['+z']['height'], 30.0)
        self.assertEqual(cylinder['size_mm']['z'], 15.0)
        self.assertNotEqual(box['ref'], cylinder['ref'])

    def test_same_source_returns_existing_ref(self):
        self.replay('synthetic-print-ps-one')
        first = self.post_parts([{'partId': 'JHD'}]).get_json()
        set_current_scenario('synthetic-print-none')        # any call now fails
        second = self.post_parts([{'partId': 'JHD'}]).get_json()
        self.assertEqual(second['errors'], [])
        self.assertEqual([p['ref'] for p in second['parts']], [first['parts'][0]['ref']])

    def test_refresh_issues_new_refs_and_supersedes_old(self):
        self.replay('synthetic-print-ps-two')
        old = self.post_parts([{'partId': 'JHD'}, {'partId': 'JKD'}]).get_json()['parts']
        set_current_scenario('synthetic-print-ps-refresh')
        resp = self.post_parts([{'source': p['source'], 'name': p['name']} for p in old], via='refresh',
                               replace=[p['ref'] for p in old])
        data = resp.get_json()
        self.assertEqual(data['errors'], [])
        self.assertEqual([p['name'] for p in data['parts']], ['Box', 'Cylinder'])
        self.assertTrue(set(p['ref'] for p in data['parts']).isdisjoint(p['ref'] for p in old))
        self.assertTrue(all(self.store.get(p['ref'], PID).superseded for p in old))
        resp = self.stl(old[0]['ref'])           # a queued job may still read it
        self.assertEqual(resp.status_code, 200)
        resp.close()
        # The superseded entries no longer answer for their source: the new one does.
        key = tuple(self.store.get(data['parts'][0]['ref'], PID).source_key)
        self.assertEqual(self.store.find_source(PID, key).ref, data['parts'][0]['ref'])

    def test_errors_are_sentences(self):
        self.replay('synthetic-print-ps-configured')
        data = self.post_parts([{'partId': 'JHD'}]).get_json()
        self.assertEqual(data, {'parts': [], 'errors': [{'message': CONFIGURED_MESSAGE}]})

    def test_call_failure_is_a_sentence_not_a_body(self):
        self.replay('synthetic-print-none')           # every call answers 599 from the replay
        data = self.post_parts([{'partId': 'JHD'}]).get_json()
        self.assertEqual(data['parts'], [])
        self.assertEqual(data['errors'], [{'message': print_routes.RESOLVE_FAILED_MESSAGE}])
        self.assertNotIn('cassette', str(data))

    def fill_page(self, count):
        """`count` stored parts for PID that no cassette knows, as earlier clicks would leave."""
        info = {'triangles': 12, 'size_mm': {'x': 1.0, 'y': 1.0, 'z': 1.0}, 'footprints': {}}
        for n in range(count):
            self.store.add(PID, f'Filler {n}', {'partId': f'F{n}'}, ('d', 'w', 'w1', 'e', f'F{n}', ''), b'x', info)

    def test_too_many_parts_refused_before_export(self):
        sent = self.replay('synthetic-print-ps-one')
        self.fill_page(limits.MAX_PARTS_PER_PAGE)
        data = self.post_parts([{'partId': 'JHD'}]).get_json()
        self.assertEqual(data, {'parts': [], 'errors': [{'message': limits.TOO_MANY_PARTS_MESSAGE}]})
        self.assertFalse([url for url, _headers in sent if '/stl' in url])     # resolved, never exported

    def test_reclick_at_the_part_limit_succeeds(self):
        self.replay('synthetic-print-ps-one')
        self.fill_page(limits.MAX_PARTS_PER_PAGE - 1)
        first = self.post_parts([{'partId': 'JHD'}]).get_json()
        self.assertEqual(first['errors'], [])
        set_current_scenario('synthetic-print-none')        # the 20th part is listed; no call now
        again = self.post_parts([{'partId': 'JHD'}, {'partId': 'JHD'}]).get_json()
        self.assertEqual(again, {'parts': first['parts'], 'errors': []})

    def test_refresh_of_a_page_over_half_its_budget(self):
        self.replay('synthetic-print-ps-one')
        with mock.patch.object(limits, 'MAX_PAGE_BYTES', BOX_BYTES + 100):    # one box is over half
            old = self.post_parts([{'partId': 'JHD'}]).get_json()['parts']
            set_current_scenario('synthetic-print-ps-refresh')
            data = self.post_parts([{'source': old[0]['source'], 'name': 'Box'}], via='refresh',
                                   replace=[old[0]['ref']]).get_json()
        self.assertEqual(data['errors'], [])
        self.assertNotEqual(data['parts'][0]['ref'], old[0]['ref'])
        self.assertTrue(self.store.get(old[0]['ref'], PID).superseded)

    def test_failed_refresh_keeps_the_old_part(self):
        self.replay('synthetic-print-ps-one')
        old = self.post_parts([{'partId': 'JHD'}]).get_json()['parts']
        set_current_scenario('synthetic-print-none')        # the re-export fails
        data = self.post_parts([{'source': old[0]['source'], 'name': 'Box'}], via='refresh',
                               replace=[old[0]['ref']]).get_json()
        self.assertEqual(data['parts'], [])
        self.assertFalse(self.store.get(old[0]['ref'], PID).superseded)

    def test_402_gives_limit_sentence(self):
        self.replay('synthetic-print-ps-402')
        data = self.post_parts([{'partId': 'JHD'}]).get_json()
        self.assertEqual(data, {'parts': [], 'errors': [{'name': 'Box',
                                                         'message': print_routes.LIMIT_REACHED_MESSAGE}]})
        self.assertEqual(print_routes.LIMIT_REACHED_MESSAGE, "Onshape refused the export (API limit reached).")
        self.assertTrue(any(line.startswith('[PRINT] export_failed ') and 'status=402' in line
                            for line in self.lines))

    def test_bad_body_refused(self):
        self.replay('synthetic-print-none')
        for body in ({'context': CONTEXT, 'via': 'other', 'selections': []},
                     {'context': CONTEXT, 'via': 'selection', 'selections': 'x'}, ['x']):
            with self.subTest(body=body):
                resp = self.client.post('/print/onshape/parts', json=body, headers={'X-Print-Page': PID})
                self.assertEqual(resp.status_code, 400)
                self.assertEqual(resp.get_json()['error'], print_routes.BAD_PARTS_REQUEST_MESSAGE)

    def test_stl_route_owner_only(self):
        self.replay('synthetic-print-ps-one')
        ref = self.post_parts([{'partId': 'JHD'}]).get_json()['parts'][0]['ref']
        resp = self.stl(ref)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, 'model/stl')
        self.assertEqual(len(resp.data), BOX_BYTES)
        resp.close()
        resp = self.stl(ref, pid=OTHER)
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json()['error'], print_routes.PART_EXPIRED_MESSAGE)
        self.assertEqual(self.stl('A' * 22).status_code, 404)
        self.assertEqual(self.client.get(f'/print/parts/{ref}.stl').status_code, 400)
        resp = self.client.get(f'/print/parts/{ref}.stl?pid={PID}')    # a GET may carry pid=
        self.assertEqual(resp.status_code, 200)
        resp.close()
        bare = app.test_client()
        self.assertEqual(bare.get(f'/print/parts/{ref}.stl', headers={'X-Print-Page': PID}).status_code, 401)

    def test_export_events_logged_without_secrets(self):
        self.replay('synthetic-print-ps-one')
        self.post_parts([{'partId': 'JHD'}])
        exports = [line for line in self.lines if line.startswith('[PRINT] export ')]
        self.assertEqual(len(exports), 1)
        self.assertIn(f'pid={PID}', exports[0])
        for field in ('part=JHD', f'element={EID_PS}', f'bytes={BOX_BYTES}', 'triangles=12',
                      'size_mm=10x20x30', 'calls=2', 'outcome=ok'):
            self.assertIn(field, exports[0])
        selects = [line for line in self.lines if line.startswith('[PRINT] select ')]
        self.assertEqual(len(selects), 1)
        for field in ('via=selection', 'count=1', 'element_type=PARTSTUDIO'):
            self.assertIn(field, selects[0])
        cookie = self.client.get_cookie('session')
        text = '\n'.join(self.lines)
        self.assertNotIn('Authorization', text)
        self.assertNotIn(ACCESS_KEY, text)
        self.assertNotIn(SECRET_KEY, text)
        if cookie is not None:
            self.assertNotIn(cookie.value, text)


class InitPartStoreTest(unittest.TestCase):
    def test_store_gets_its_own_folder_under_uploads(self):
        from flask import Flask
        with tempfile.TemporaryDirectory() as uploads, \
                mock.patch.object(print_routes, 'print_bp', print_routes.Blueprint('p2', __name__)), \
                mock.patch.object(print_routes, 'ctx', print_routes.types.SimpleNamespace()), \
                mock.patch.object(PartStore, 'start_sweeper') as sweeper:
            limiter_stub = mock.Mock()
            limiter_stub.limit.return_value = lambda view: view
            print_routes.init_print_routes(Flask('t'), limiter=limiter_stub, require_session=None,
                                           has_onshape_session=None, onshape_client=None, token_manager=None,
                                           upload_folder=uploads, output_folder=uploads, template_context=None,
                                           metrics=None, log=print, pool=mock.Mock())
            self.assertEqual(print_routes.ctx.part_store.root, Path(uploads) / 'print_parts')
            self.assertTrue((Path(uploads) / 'print_parts').is_dir())
            sweeper.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()

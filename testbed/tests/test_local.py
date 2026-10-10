"""Tests for local mode: testbed/local.py (the stand-in Part Studio and LocalAdapter) and
its routes in testbed/flask_hooks.py, on a minimal Flask app."""
import io
import os
import socket
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests
from flask import Flask
from urllib3.util.retry import Retry

from onshape_integration import OnshapeClient
from print.limits import MAX_PART_BYTES
from print.onshape_parts import (ELEMENT_CACHE, ElementCache, PartSource, PartTooLarge, ResolvedPart,
                                 export_part_mesh, resolve_selections, rest_address)
from testbed import adapters, flask_hooks, local, settings
from testbed.scrub import STANDIN_EMAIL, STANDIN_NAME
from testbed.tests.fixtures.make_print_fixtures import box_stl, cylinder_stl

ASCII_TRIANGLE = b"""solid t
facet normal 0 0 1
 outer loop
  vertex 0 0 0
  vertex 10 0 0
  vertex 0 10 0
 endloop
endfacet
endsolid t
"""
API = 'https://cad.onshape.com/api/v13'
_DROPPED = ('PENGUINCAM_TESTBED', 'TESTBED_LOCAL_CONFIG', 'ONSHAPE_ACCESS_KEY',
            'ONSHAPE_SECRET_KEY', 'FLASK_ENV', 'RAILWAY_ENVIRONMENT', 'VERCEL')


class _LocalCase(unittest.TestCase):
    """Local mode on, the state directory in a temporary folder, no socket may open."""

    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        env = {k: v for k, v in os.environ.items() if k not in _DROPPED}
        env.update(XDG_STATE_HOME=str(self.tmp / 'state'), PENGUINCAM_TESTBED='local',
                   ONSHAPE_ACCESS_KEY='real-looking-key', ONSHAPE_SECRET_KEY='real-looking-secret')
        self.enterContext(mock.patch.dict(os.environ, env, clear=True))
        settings.reset_testbed_settings_for_tests()
        self.addCleanup(settings.reset_testbed_settings_for_tests)
        adapters.reset_testbed_adapters_for_tests()
        self.addCleanup(adapters.reset_testbed_adapters_for_tests)

        def no_network(*args, **kwargs):
            raise AssertionError('local mode opened a network connection')
        self.enterContext(mock.patch.object(socket, 'create_connection', no_network))
        self.enterContext(mock.patch.object(socket.socket, 'connect', no_network))
        self.ids = local.new_part_studio_ids()

    def client(self):
        return OnshapeClient.from_api_keys()

    def get(self, path, **kwargs):
        return self.client()._make_api_request('GET', path, **kwargs)


class StandInPartStudioTest(_LocalCase):
    def test_ids_are_24_hex(self):
        self.assertTrue(local.valid_ids(self.ids))
        self.assertFalse(local.valid_ids({'did': 'x', 'wid': 'y', 'eid': 'z'}))

    def test_parts_named_from_files_unique_and_ids_never_reused(self):
        first = local.add_part(self.ids, 'Bracket.STL', box_stl())
        second = local.add_part(self.ids, 'dir/Bracket.stl', box_stl())
        self.assertEqual((first['name'], second['name']), ('Bracket', 'Bracket (2)'))
        self.assertEqual((first['partId'], second['partId']), ('L1', 'L2'))
        local.clear_parts(self.ids)
        self.assertEqual(local.list_parts(self.ids), [])
        self.assertEqual(local.add_part(self.ids, 'Bracket.stl', box_stl())['partId'], 'L3')

    def test_ascii_stl_is_converted_to_binary(self):
        binary = local.stl_as_binary(ASCII_TRIANGLE)
        self.assertEqual(struct.unpack_from('<I', binary, 80)[0], 1)
        self.assertEqual(len(binary), 84 + 50)

    def test_binary_kept_and_junk_refused(self):
        stl = box_stl()
        self.assertIs(local.stl_as_binary(stl), stl)
        for junk in (b'', b'hello', b'solid nothing here\n', b'\xff' * 200):
            with self.subTest(junk=junk[:10]):
                with self.assertRaises(local.UploadRefused):
                    local.stl_as_binary(junk)

    def test_no_new_cap_on_upload(self):
        # The print path's byte and triangle caps apply when the part is exported, not here.
        big = b'x' * 80 + struct.pack('<I', 200_000) + b'\0' * (50 * 200_000)
        self.assertGreater(len(big), MAX_PART_BYTES)
        self.assertEqual(local.add_part(self.ids, 'Big.stl', big)['bytes'], len(big))


class LocalAdapterTest(_LocalCase):
    def setUp(self):
        super().setUp()
        local.add_part(self.ids, 'Box.stl', box_stl())
        local.add_part(self.ids, 'Cylinder.stl', cylinder_stl())
        self.path = f"d/{self.ids['did']}/w/{self.ids['wid']}"
        self.element = f"{self.path}/e/{self.ids['eid']}"

    def test_client_holds_only_placeholder_keys(self):
        client = self.client()
        self.assertEqual((client.api_access_key, client.api_secret_key),
                         settings.REPLAY_PLACEHOLDER_KEYS)

    def test_install_mounts_the_local_adapter(self):
        session = requests.Session()
        adapters.install_testbed_adapters(session, Retry(total=0))
        self.assertIsInstance(session.get_adapter('https://cad.onshape.com/'), local.LocalAdapter)
        self.assertIsInstance(session.get_adapter('http://example.com/'), local.LocalAdapter)

    def test_sessioninfo_and_companies(self):
        info = self.get('/users/sessioninfo').json()
        self.assertEqual((info['name'], info['email']), (STANDIN_NAME, STANDIN_EMAIL))
        companies = self.get('/companies?activeOnly=true&includeAll=false').json()['items']
        self.assertEqual(len(companies), 1)

    def test_element_parts_and_configuration(self):
        elements = self.get(f'/documents/{self.path}/elements',
                            params={'elementId': self.ids['eid']}).json()
        self.assertEqual(elements[0]['elementType'], 'PARTSTUDIO')
        parts = self.get(f'/parts/{self.element}').json()
        self.assertEqual([(p['partId'], p['name'], p['bodyType']) for p in parts],
                         [('L1', 'Box', 'solid'), ('L2', 'Cylinder', 'solid')])
        self.assertEqual(self.get(f'/elements/{self.element}/configuration').json()
                         ['configurationParameters'], [])

    def test_export_redirects_to_storage_then_serves_the_upload(self):
        params = {'mode': 'binary', 'units': 'millimeter'}
        first = self.get(f'/parts/{self.element}/partid/L1/stl', params=params,
                         allow_redirects=False)
        self.assertEqual(first.status_code, 307)
        self.assertTrue(first.headers['Location'].startswith(f'https://{local.STORAGE_HOST}/'))
        followed = self.client().session.get(first.headers['Location'])
        self.assertEqual(followed.content, box_stl())

    def test_export_in_other_units_is_refused_not_guessed(self):
        answer = self.get(f'/parts/{self.element}/partid/L1/stl',
                          params={'mode': 'binary', 'units': 'inch'}, allow_redirects=False)
        self.assertEqual(answer.status_code, local.MISS_STATUS)
        self.assertTrue(answer.text.startswith(local.MISS_PREFIX))

    def test_unknown_ids_are_404(self):
        other = local.new_part_studio_ids()
        self.assertEqual(self.get(f"/parts/d/{other['did']}/w/{other['wid']}/e/{other['eid']}")
                         .status_code, 404)
        self.assertEqual(self.get(f'/parts/{self.element}/partid/L9/stl',
                                  params={'mode': 'binary', 'units': 'millimeter'}).status_code, 404)

    def test_anything_else_is_a_clear_miss_never_a_real_call(self):
        for method, url in (('GET', f'{API}/documents/d/{self.ids["did"]}/w/{self.ids["wid"]}'
                                    f'/e/{self.ids["eid"]}/faces'),
                            ('POST', f'{API}/partstudios/{self.element}/export'),
                            ('GET', 'https://example.com/anything'),
                            ('GET', 'https://cad.onshape.com/oauth/token')):
            with self.subTest(url=url):
                answer = self.client().session.request(method, url)
                self.assertEqual(answer.status_code, local.MISS_STATUS)
                self.assertIn(local.MISS_PREFIX, answer.text)

    def test_print_path_resolves_and_exports_through_its_own_code(self):
        context = {'documentId': self.ids['did'], 'workspaceId': self.ids['wid'],
                   'elementId': self.ids['eid']}
        address = rest_address(context)
        client = self.client()
        resolved, errors = resolve_selections(client, address, [{'partId': 'L2'}], 'selection',
                                              cache=ElementCache())
        self.assertEqual((errors, [p.name for p in resolved]), ([], ['Cylinder']))
        result = export_part_mesh(client, resolved[0], byte_cap=MAX_PART_BYTES, cache=ElementCache())
        self.assertEqual(result.stl, cylinder_stl())
        self.assertEqual(result.calls, 2)              # the export and its redirect hop
        with self.assertRaises(PartTooLarge):
            export_part_mesh(client, resolved[0], byte_cap=1000, cache=ElementCache())

    def test_new_upload_found_after_the_parts_list_was_cached(self):
        address = rest_address({'documentId': self.ids['did'], 'workspaceId': self.ids['wid'],
                                'elementId': self.ids['eid']})
        cache = ElementCache()
        resolve_selections(self.client(), address, [{'partId': 'L1'}], 'selection', cache=cache)
        local.add_part(self.ids, 'Lid.stl', box_stl(5, 5, 5))
        resolved, errors = resolve_selections(self.client(), address, [{'partId': 'L3'}],
                                              'selection', cache=cache)
        self.assertEqual(([p.name for p in resolved], errors), (['Lid'], []))

    def test_team_config_is_the_fixture_unless_named(self):
        from testbed.scrub import TEAM_CONFIG_FIXTURE
        self.assertEqual(self.client().fetch_config_file(self.ids['did']),
                         TEAM_CONFIG_FIXTURE.read_text())
        config = self.tmp / 'team.yaml'
        config.write_text('team:\n  number: 9999\n  name: Local Team\n')
        os.environ['TESTBED_LOCAL_CONFIG'] = str(config)
        self.assertEqual(self.client().fetch_config_file(self.ids['did']), config.read_text())


class LocalHooksTest(_LocalCase):
    """The local routes on a minimal app, with a stand-in for the panel's framing rule."""

    def setUp(self):
        super().setUp()
        self.app = Flask(__name__)
        self.app.secret_key = 'test'

        @self.app.route('/framed')
        def framed():
            return 'panel', 200, {'Content-Security-Policy': flask_hooks.PANEL_FRAME_ANCESTORS}

        flask_hooks.register_testbed_routes(self.app)
        self.http = self.app.test_client()

    def upload(self, *files):
        return self.http.post('/testbed/local/parts', content_type='multipart/form-data',
                              data={'files': [(io.BytesIO(data), name) for name, data in files]})

    def test_page_and_scripts(self):
        page = self.http.get('/testbed/local')
        self.addCleanup(page.close)
        self.assertEqual(page.status_code, 200)
        self.assertIn('/testbed/local/local.js', page.get_data(as_text=True))
        for name in flask_hooks.LOCAL_SCRIPTS:
            script = self.http.get(f'/testbed/local/{name}')
            self.addCleanup(script.close)
            self.assertEqual(script.status_code, 200)
        self.assertEqual(self.http.get('/testbed/local/host.js').status_code, 404)
        self.assertEqual(self.http.get('/testbed/local/..%2Fsettings.py').status_code, 404)

    def test_sign_in(self):
        self.assertEqual(self.http.get('/testbed/sign-in').status_code, 204)
        with self.http.session_transaction() as cookie:
            self.assertTrue(cookie['testbed_apikey'])
            self.assertEqual(cookie['user_email'], STANDIN_EMAIL)

    def test_upload_lists_parts_per_session(self):
        answer = self.upload(('Box.stl', box_stl()), ('Notes.txt', b'hello'),
                             ('Tri.stl', ASCII_TRIANGLE)).get_json()
        self.assertEqual([p['name'] for p in answer['parts']], ['Box', 'Tri'])
        self.assertEqual(answer['errors'], ['Notes.txt: not an STL file'])
        state = self.http.get('/testbed/local/state').get_json()
        self.assertEqual(state['ids'], answer['ids'])
        self.assertEqual(len(state['parts']), 2)
        other = self.app.test_client().get('/testbed/local/state').get_json()
        self.assertNotEqual(other['ids'], state['ids'])
        self.assertEqual(other['parts'], [])
        cleared = self.http.post('/testbed/local/clear').get_json()
        self.assertEqual(cleared['parts'], [])

    def test_upload_needs_files(self):
        self.assertEqual(self.http.post('/testbed/local/parts').status_code, 400)

    def test_framing_rule_relaxed_and_oauth_refused(self):
        self.assertEqual(self.http.get('/framed').headers['Content-Security-Policy'],
                         flask_hooks.LOCAL_FRAME_ANCESTORS)
        self.assertEqual(self.http.get('/onshape/auth').status_code, 403)
        self.assertEqual(self.http.get('/onshape/oauth/callback?code=x').status_code, 403)

    def test_recording_routes_absent(self):
        self.assertEqual(self.http.get('/testbed/scenarios').status_code, 404)
        self.assertEqual(self.http.get('/testbed/panel.js').status_code, 404)


class ReplayLeavesFramingAloneTest(unittest.TestCase):
    def test_replay_mode_keeps_the_rule_and_has_no_local_routes(self):
        with mock.patch.dict(os.environ, {'PENGUINCAM_TESTBED': 'replay'}):
            settings.reset_testbed_settings_for_tests()
            self.addCleanup(settings.reset_testbed_settings_for_tests)
            app = Flask(__name__)
            app.secret_key = 'test'

            @app.route('/framed')
            def framed():
                return 'panel', 200, {'Content-Security-Policy': flask_hooks.PANEL_FRAME_ANCESTORS}

            flask_hooks.register_testbed_routes(app)
            http = app.test_client()
            self.assertEqual(http.get('/framed').headers['Content-Security-Policy'],
                             flask_hooks.PANEL_FRAME_ANCESTORS)
            self.assertEqual(http.get('/testbed/local').status_code, 404)
            self.assertEqual(http.get('/onshape/auth').status_code, 404)


if __name__ == '__main__':
    unittest.main()

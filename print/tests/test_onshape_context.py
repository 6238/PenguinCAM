"""The panel context, the REST address and the export transport of print/onshape_parts.py.

The transport runs over the test bed's replay adapter with cassettes written in a temp dir.
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import quote, urlencode

REPO = Path(__file__).resolve().parents[2]        # the repository root
sys.path.insert(0, str(REPO))

from print.onshape_parts import (DEFAULT_SERVER, NO_CONTEXT_MESSAGE, STL_ACCEPT,  # noqa: E402
                                 DownloadTooLarge, ExportHTTPError, NoOnshapeContext,
                                 fetch_following_redirects, panel_context_from_return,
                                 rest_address)
from print.tests.onshape_replay import exchange, replay_client  # noqa: E402
from testbed.cassette import Cassette  # noqa: E402

DID = '0123456789abcdef01234567'
WID = '11111111111111111111aaaa'
VID = '22222222222222222222bbbb'
MID = '33333333333333333333cccc'
EID = '44444444444444444444dddd'
STL_PATH = f'/parts/d/{DID}/w/{WID}/e/{EID}/partid/JHD/stl'


def return_url(**query):
    return '/onshape/panel?' + urlencode(query, safe='{}$')


class PanelContextTest(unittest.TestCase):
    def test_six_fields_and_configuration_read_from_return(self):
        context = panel_context_from_return(return_url(
            documentId=DID, workspaceId=WID, versionId=VID, microversionId=MID, elementId=EID,
            server='https://cad.onshape.com', configuration='Length=2+in;' + 'x' * 2100,
            userId='ignored'))
        self.assertEqual({k: context[k] for k in ('documentId', 'workspaceId', 'versionId',
                                                  'microversionId', 'elementId', 'server')},
                         {'documentId': DID, 'workspaceId': WID, 'versionId': VID,
                          'microversionId': MID, 'elementId': EID,
                          'server': 'https://cad.onshape.com'})
        self.assertEqual(len(context['configuration']), 2000)
        self.assertTrue(context['configuration'].startswith('Length=2+in;'))
        self.assertNotIn('userId', context)
        self.assertNotIn('configuration', panel_context_from_return(return_url(documentId=DID)))

    def test_placeholder_kept_raw_and_junk_dropped(self):
        context = panel_context_from_return(return_url(
            documentId=DID, workspaceId='{$workspaceId}', elementId='abc'))
        self.assertEqual(context['workspaceId'], '{$workspaceId}')
        self.assertEqual(context['elementId'], '')
        self.assertEqual(context['versionId'], '')
        self.assertEqual(panel_context_from_return('/app')['documentId'], '')

    def test_server_validated(self):
        for bad in ('https://evil.com', 'http://cad.onshape.com', 'https://onshape.com.evil.com',
                    'https://cad.onshape.com@evil.com', 'javascript:alert(1)'):
            with self.subTest(server=bad):
                self.assertEqual(panel_context_from_return(return_url(server=bad))['server'],
                                 DEFAULT_SERVER)
        self.assertEqual(panel_context_from_return(return_url(server='https://cad-testbed.onshape.com'))
                         ['server'], 'https://cad-testbed.onshape.com')
        self.assertEqual(panel_context_from_return('/app')['server'], DEFAULT_SERVER)


class RestAddressTest(unittest.TestCase):
    def test_rest_address_prefers_workspace_then_version_then_microversion(self):
        full = {'documentId': DID, 'workspaceId': WID, 'versionId': VID, 'microversionId': MID,
                'elementId': EID}
        address = rest_address(full)
        self.assertEqual((address.wvm, address.wvm_id), ('w', WID))
        self.assertEqual(address.document_path, f'd/{DID}/w/{WID}')
        self.assertEqual(address.element_path, f'd/{DID}/w/{WID}/e/{EID}')
        self.assertIsNone(address.configuration)
        address = rest_address(dict(full, workspaceId=''))
        self.assertEqual((address.wvm, address.wvm_id), ('v', VID))
        address = rest_address(dict(full, workspaceId='', versionId=''))
        self.assertEqual((address.wvm, address.wvm_id), ('m', MID))
        self.assertEqual(rest_address(dict(full, configuration='A=1')).configuration, 'A=1')

    def test_version_context_with_placeholder_workspace(self):
        address = rest_address({'documentId': DID, 'workspaceId': '{$workspaceId}',
                                'versionId': VID, 'elementId': EID})
        self.assertEqual(address.wvm, 'v')
        self.assertEqual(address.wvm_id, VID)

    def test_no_wvm_raises_no_context(self):
        for context in ({'documentId': DID, 'workspaceId': '{$workspaceId}', 'elementId': EID},
                        {'documentId': DID, 'workspaceId': WID, 'elementId': ''},
                        {'documentId': '', 'workspaceId': WID, 'elementId': EID}, {}):
            with self.subTest(context=context):
                with self.assertRaises(NoOnshapeContext) as caught:
                    rest_address(context)
                self.assertEqual(caught.exception.message, NO_CONTEXT_MESSAGE)


class FetchFollowingRedirectsTest(unittest.TestCase):
    def setUp(self):
        self.cassettes = Path(self.enterContext(tempfile.TemporaryDirectory()))
        api = '/api/v13'
        Cassette('transport', [
            # JHD: 307 to Onshape, then 307 to a foreign host, then the body.
            exchange(api + STL_PATH, status=307, headers={'Location': 'https://cad.onshape.com/x'}),
            exchange('/x', status=307, headers={'Location': 'https://example.com/y'}),
            exchange('/y', 'solid elsewhere', host='example.com'),
            # BIG: a 100-byte body.
            exchange(api + STL_PATH.replace('JHD', 'BIG'), body_bytes=bytes(100)),
            # HDR: 307 -> 307 -> 200, all on Onshape.
            exchange(api + STL_PATH.replace('JHD', 'HDR'), status=307, query={'mode': ['binary']},
                     headers={'Location': 'https://cad.onshape.com/h1'}),
            exchange('/h1', status=307, headers={'Location': '/h2'}),
            exchange('/h2', 'solid hdr'),
            # The three error cases.
            exchange(api + STL_PATH.replace('JHD', 'LOOP'), status=307,
                     headers={'Location': 'https://cad.onshape.com' + api + STL_PATH.replace('JHD', 'LOOP')}),
            exchange(api + STL_PATH.replace('JHD', 'NOWHERE'), status=307),
            exchange(api + STL_PATH.replace('JHD', 'FAIL'), status=500),
        ], synthetic=True).save(self.cassettes)

    def client(self):
        return replay_client('transport', self.cassettes)

    def test_redirect_reattaches_auth_only_for_onshape(self):
        with self.client() as (client, sent):
            with mock.patch.object(client, 'request_absolute', wraps=client.request_absolute) as absolute:
                body, hops = fetch_following_redirects(client, STL_PATH)
        self.assertEqual(body, b'solid elsewhere')
        self.assertEqual(hops, ['https://cad.onshape.com/x', 'https://example.com/y'])
        # The request itself (through _make_api_request) and the first hop; never the foreign one.
        self.assertEqual([c.args[1] for c in absolute.call_args_list],
                         [f'https://cad.onshape.com/api/v13{STL_PATH}', 'https://cad.onshape.com/x'])
        auth = {url: headers.get('Authorization') for url, headers in sent}
        self.assertTrue(auth[f'https://cad.onshape.com/api/v13{STL_PATH}'].startswith('Basic '))
        self.assertTrue(auth['https://cad.onshape.com/x'].startswith('Basic '))
        self.assertIsNone(auth['https://example.com/y'])

    def test_byte_cap_stops_download(self):
        with self.client() as (client, _sent):
            with self.assertRaises(DownloadTooLarge):
                fetch_following_redirects(client, STL_PATH.replace('JHD', 'BIG'), byte_cap=50)
            body, hops = fetch_following_redirects(client, STL_PATH.replace('JHD', 'BIG'), byte_cap=100)
        self.assertEqual((len(body), hops), (100, []))

    def test_headers_sent_on_request_and_every_hop(self):
        with self.client() as (client, sent):
            body, hops = fetch_following_redirects(client, STL_PATH.replace('JHD', 'HDR'),
                                                   {'mode': 'binary'}, headers={'Accept': STL_ACCEPT})
        self.assertEqual(body, b'solid hdr')
        self.assertEqual(hops, ['https://cad.onshape.com/h1', 'https://cad.onshape.com/h2'])
        self.assertEqual(len(sent), 3)
        for url, headers in sent:
            self.assertEqual(headers.get('Accept'), 'application/vnd.onshape.v1+octet-stream', url)

    def test_redirect_errors_are_runtime_errors_with_test_bed_messages(self):
        with self.client() as (client, sent):
            with self.assertRaisesRegex(RuntimeError, 'redirect') as caught:
                fetch_following_redirects(client, STL_PATH.replace('JHD', 'LOOP'))
            self.assertIsInstance(caught.exception, ExportHTTPError)
            self.assertEqual(caught.exception.status, 310)
            self.assertEqual(len(sent), 4)          # the request and three hops
            with self.assertRaisesRegex(RuntimeError, '307 without Location') as caught:
                fetch_following_redirects(client, STL_PATH.replace('JHD', 'NOWHERE'))
            self.assertEqual(caught.exception.status, 307)
            with self.assertRaisesRegex(RuntimeError, 'HTTP 500') as caught:
                fetch_following_redirects(client, STL_PATH.replace('JHD', 'FAIL'))
            self.assertEqual(caught.exception.status, 500)


class PrintPageContextTest(unittest.TestCase):
    def setUp(self):
        from frc_cam_gui_app import app
        app.config['TESTING'] = True
        self.client = app.test_client()
        with self.client.session_transaction() as sess:
            sess['app_verified'] = True

    def test_print_page_passes_context_and_no_context_sentence(self):
        target = return_url(documentId=DID, workspaceId='{$workspaceId}', elementId=EID,
                            server='https://cad.onshape.com')
        html = self.client.get('/print?source=onshape&return=' + quote(target, safe='')).get_data(as_text=True)
        self.assertIn('window.PenguinCAM', html)
        self.assertIn('onshape: {', html)
        self.assertIn(DID, html)
        self.assertIn('{$workspaceId}', html)
        self.assertIn(f'onshapeError: "{NO_CONTEXT_MESSAGE}"', html)

        target = return_url(documentId=DID, workspaceId=WID, elementId=EID)
        html = self.client.get('/print?source=onshape&return=' + quote(target, safe='')).get_data(as_text=True)
        self.assertIn('onshapeError: null', html)
        self.assertNotIn(NO_CONTEXT_MESSAGE, html)


if __name__ == '__main__':
    unittest.main()

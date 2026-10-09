"""Tests for testbed/documents.py: the test documents builder, replayed with no live call.

The synthetic cassette is a full build into an empty test folder. A test that needs another
folder state edits a copy of it: the folder listings' answers, or a DELETE for --rebuild.
"""
import json
import os
import shutil
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from onshape_integration import OnshapeClient
from testbed import adapters, settings
from testbed.adapters import set_current_scenario
from testbed.cassette import Cassette, Exchange
from testbed.documents import (TEST_DESCRIPTION, TEST_DOCUMENTS, NotATestDocument,
                               assert_test_document, build_test_documents, extrude_feature,
                               sketch_circle_feature, sketch_rectangle_feature)
from testbed.scenarios import TEST_DOCUMENT_NAMES

FIXTURE_CASSETTES = Path(__file__).resolve().parent / 'fixtures' / 'cassettes'
SCENARIO = 'synthetic-build-docs'
FOLDER = 'f00000000000000000000f01'
LISTING_PATH = '/api/v13/documents'


def _doc(name, did, description=TEST_DESCRIPTION):
    return {'id': did, 'name': name, 'description': description,
            'defaultWorkspace': {'id': 'w' + did[1:]}}


class _BuildCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        env = {k: v for k, v in os.environ.items()
               if k not in ('PENGUINCAM_TESTBED', 'ONSHAPE_ACCESS_KEY', 'ONSHAPE_SECRET_KEY')}
        env.update(PENGUINCAM_TESTBED='replay', XDG_STATE_HOME=str(self.tmp / 'state'))
        self.enterContext(mock.patch.dict(os.environ, env, clear=True))
        self.cassettes = self.tmp / 'cassettes'
        shutil.copytree(FIXTURE_CASSETTES, self.cassettes)
        self.enterContext(mock.patch.object(settings, 'CASSETTE_DIR', self.cassettes))
        self.enterContext(mock.patch.object(settings, 'DOCUMENTS_FILE',
                                            self.tmp / 'documents.json'))
        self.enterContext(mock.patch.object(
            socket.socket, 'connect', side_effect=AssertionError('network used')))
        settings.reset_testbed_settings_for_tests()
        self.addCleanup(settings.reset_testbed_settings_for_tests)
        adapters.reset_testbed_adapters_for_tests()
        self.addCleanup(adapters.reset_testbed_adapters_for_tests)
        self.cassette = Cassette.load(self.cassettes, SCENARIO)
        self.full_listing = json.loads(self.listings()[1].body_text)['items']

    def listings(self):
        return [ex for ex in self.cassette.exchanges
                if ex.method == 'GET' and ex.path == LISTING_PATH]

    def answer_listings(self, first, second=None):
        """The folder listing answers `first`, then `second` (default: every test document)."""
        one, two = self.listings()
        one.body_text = json.dumps({'items': first})
        two.body_text = json.dumps({'items': self.full_listing if second is None else second})

    def build(self, rebuild=None):
        self.cassette.save(self.cassettes)
        set_current_scenario(SCENARIO)
        client = OnshapeClient.from_api_keys()
        self.calls = []
        real = client._make_api_request

        def spy(method, endpoint, **kwargs):
            self.calls.append((method, endpoint, kwargs))
            return real(method, endpoint, **kwargs)
        client._make_api_request = spy
        return build_test_documents(client, FOLDER, rebuild=rebuild)

    def calls_to(self, method, prefix):
        return [(e, kw) for m, e, kw in self.calls if m == method and e.startswith(prefix)]

    def full_documents(self):
        return {item['name']: item for item in self.full_listing}


class BuildTest(_BuildCase):
    def test_build_creates_only_missing_documents(self):
        built = self.build()
        self.assertEqual(sorted(built), sorted(TEST_DOCUMENT_NAMES))
        everything = self.full_documents()
        kept = [everything[name] for name in TEST_DOCUMENT_NAMES if name != 'tb-oversized']
        settings.DOCUMENTS_FILE.write_text(json.dumps(
            {name: entry for name, entry in built.items() if name != 'tb-oversized'}))
        adapters.reset_testbed_adapters_for_tests()
        self.answer_listings(kept)
        again = self.build()
        created = [kw['json']['name'] for _e, kw in self.calls_to('POST', '/documents')
                   if _e == '/documents']
        self.assertEqual(created, ['tb-oversized'])
        self.assertEqual(again, built)
        # Nothing but tb-oversized was written to.
        written = {e.split('/')[3] for m, e, _kw in self.calls
                   if m == 'POST' and e.startswith(('/partstudios/', '/metadata/', '/assemblies/',
                                                    '/documents/d/'))}
        self.assertEqual(written, {everything['tb-oversized']['id']})

    def test_documents_json_written(self):
        self.build()
        written = json.loads(settings.DOCUMENTS_FILE.read_text())
        self.assertEqual(sorted(written), sorted(TEST_DOCUMENT_NAMES))
        everything = self.full_documents()
        for name, entry in written.items():
            self.assertEqual(entry['did'], everything[name]['id'])
            self.assertEqual(entry['wid'], everything[name]['defaultWorkspace']['id'])
            self.assertTrue(entry['eid'])
        self.assertEqual(list(written['tb-box']['parts']), ['Box'])
        self.assertEqual(sorted(written['tb-two-parts']['parts']), ['Box', 'Cylinder'])
        self.assertEqual(written['tb-assembly']['parts'], {})
        self.assertNotEqual(written['tb-assembly']['eid'], written['tb-box']['eid'])

    def test_version_saved_for_tb_box(self):
        built = self.build()
        box = built['tb-box']
        versions = self.calls_to('POST', f"/documents/d/{box['did']}/versions")
        self.assertEqual(len(versions), 1)
        self.assertEqual(len([e for e, _kw in self.calls_to('POST', '/documents/d/')
                              if e.endswith('/versions')]), 1)
        self.assertTrue(box['version_id'])
        # The version holds the part already renamed, so the instances are Box <1>, Box <2>.
        rename = self.calls_to('POST', f"/metadata/d/{box['did']}/")
        self.assertLess(self.calls.index(('POST', rename[0][0], rename[0][1])),
                        self.calls.index(('POST', versions[0][0], versions[0][1])))
        instances = self.calls_to('POST', f"/assemblies/d/{built['tb-assembly']['did']}/")
        instances = [kw['json'] for e, kw in instances if e.endswith('/instances')]
        self.assertEqual(len(instances), 2)
        for body in instances:
            self.assertEqual(body['versionId'], box['version_id'])
            self.assertEqual(body['documentId'], box['did'])
            self.assertEqual(body['partId'], box['parts']['Box'])

    def test_rebuild_deletes_only_named_test_document(self):
        everything = self.full_documents()
        foreign = _doc('tb-inch-copy', 'd00000000000000000000f99', description='mine')
        built = self.build()
        settings.DOCUMENTS_FILE.write_text(json.dumps(built))
        adapters.reset_testbed_adapters_for_tests()
        inch = everything['tb-inch']['id']
        self.cassette.exchanges.append(Exchange(
            method='DELETE', host='cad.onshape.com', path=f'/api/v13/documents/{inch}',
            query={}, request_body=None, status=200, headers={}, body_text=None,
            body_file=None))
        self.answer_listings(self.full_listing + [foreign], self.full_listing + [foreign])
        self.build(rebuild='tb-inch')
        self.assertEqual([e for e, _kw in self.calls_to('DELETE', '/')], [f'/documents/{inch}'])
        created = [kw['json']['name'] for e, kw in self.calls_to('POST', '/documents')
                   if e == '/documents']
        self.assertEqual(created, ['tb-inch'])

    def test_rebuild_refuses_unknown_or_foreign_document(self):
        with self.assertRaises(ValueError):
            self.build(rebuild='not-a-test-document')
        self.assertEqual(self.calls, [])
        everything = self.full_documents()
        tampered = dict(everything['tb-inch'], description='my real robot part')
        others = [item for item in self.full_listing if item['name'] != 'tb-inch']
        self.answer_listings(others + [tampered])
        adapters.reset_testbed_adapters_for_tests()
        with self.assertRaises(NotATestDocument):
            self.build(rebuild='tb-inch')
        self.assertEqual(self.calls_to('DELETE', '/'), [])

    def test_write_refused_when_created_document_not_in_folder(self):
        # parentId did not place the new documents in the folder: no write follows.
        self.answer_listings([], [])
        with self.assertRaises(NotATestDocument) as caught:
            self.build()
        self.assertIn('not listed in the test folder', str(caught.exception))
        self.assertEqual(self.calls_to('POST', '/partstudios/'), [])


class RulesTest(unittest.TestCase):
    def test_write_refused_for_foreign_document(self):
        good = _doc('tb-box', 'd00000000000000000000001')
        listing = {'items': [good]}
        assert_test_document(good, listing)                  # all three rules hold
        assert_test_document(good, listing['items'])         # a bare list of items too
        broken = {
            'not in the folder': (good, {'items': []}),
            'name without tb-': (dict(good, name='box'), {'items': [dict(good, name='box')]}),
            'other description': (dict(good, description='created by me'),
                                  {'items': [dict(good, description='created by me')]}),
        }
        for rule, (document, folder) in broken.items():
            with self.subTest(rule=rule), self.assertRaises(NotATestDocument):
                assert_test_document(document, folder)

    def test_folder_id_missing_explains_where_to_find_it(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(settings.MissingEnvironmentVariable) as caught:
                settings.testbed_folder_id()
        self.assertEqual(caught.exception.name, 'TESTBED_FOLDER_ID')
        self.assertIn("last segment of the folder's URL", str(caught.exception))
        with mock.patch.dict(os.environ, {'TESTBED_FOLDER_ID': FOLDER}, clear=True):
            self.assertEqual(settings.testbed_folder_id(), FOLDER)


class FeatureBodiesTest(unittest.TestCase):
    def test_sketch_geometry_in_metres_and_depth_as_expression(self):
        sketch = sketch_rectangle_feature('Top', 0.04, 0.03)
        self.assertEqual(sketch['btType'], 'BTFeatureDefinitionCall-1406')
        feature = sketch['feature']
        self.assertEqual(feature['featureType'], 'newSketch')
        lines = [e['geometry'] for e in feature['entities']]
        xs = {g['pntX'] for g in lines}
        ys = {g['pntY'] for g in lines}
        self.assertEqual(xs, {0.0, 0.04})
        self.assertEqual(ys, {0.0, 0.03})
        self.assertEqual({e['endParam'] for e in feature['entities']}, {0.04, 0.03})
        circle = sketch_circle_feature('Top', 0.01)['feature']['entities'][0]['geometry']
        self.assertEqual(circle['radius'], 0.01)
        # Plain numbers only in the sketch: no expression with units.
        self.assertNotIn('expression', json.dumps(sketch))
        extrude = extrude_feature('FsketchId', '0.5 in')
        self.assertEqual(extrude['btType'], 'BTFeatureDefinitionCall-1406')
        parameters = {p['parameterId']: p for p in extrude['feature']['parameters']}
        self.assertEqual(parameters['depth']['expression'], '0.5 in')
        self.assertEqual(parameters['entities']['queries'][0]['featureId'], 'FsketchId')
        # tb-inch is dimensioned in inches: 2 x 1 in sketched in metres, 0.5 in deep.
        inch = next(spec for spec in TEST_DOCUMENTS if spec.name == 'tb-inch')
        entities = inch.features[0]['feature']['entities']
        self.assertEqual({e['endParam'] for e in entities}, {0.0508, 0.0254})
        depths = [p['expression'] for f in inch.features for p in f['feature']['parameters']
                  if p.get('parameterId') == 'depth']
        self.assertEqual(depths, ['0.5 in'])

    def test_six_documents_of_the_spec(self):
        self.assertEqual([spec.name for spec in TEST_DOCUMENTS], list(TEST_DOCUMENT_NAMES))
        kinds = {spec.name: spec.kind for spec in TEST_DOCUMENTS}
        self.assertEqual(kinds['tb-assembly'], 'assembly')
        self.assertEqual(TEST_DESCRIPTION, 'created by PenguinCAM test bed')


if __name__ == '__main__':
    unittest.main()

"""Tests for testbed/drift.py: comparing a fresh recording with the stored one, and accept."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from testbed import settings
from testbed.cassette import Cassette, Exchange
from testbed.drift import (Difference, RecordingNotClean, accept_fresh_recording, diff_cassettes,
                           diff_message_logs, json_shape, write_drift_report)
from testbed.messages import save_message_log

STL_DIR = Path(__file__).resolve().parent / 'fixtures' / 'stl'
JSON = {'Content-Type': 'application/json'}
STL = {'Content-Type': 'application/octet-stream'}


def _exchange(path='/api/v13/documents', body=None, **fields):
    base = dict(method='GET', host='cad.onshape.com', path=path, query={}, request_body=None,
                status=200, headers=JSON,
                body_text=json.dumps(body if body is not None else {'items': []}),
                body_file=None)
    base.update(fields)
    return Exchange(**base)


def _cassette(*exchanges, name='part-export'):
    return Cassette(name, list(exchanges))


def _stl_exchange(fixture):
    return _exchange('/api/v13/parts/d/a/w/b/e/c/partid/p/stl',
                     query={'units': ['millimeter'], 'mode': ['binary']}, headers=STL,
                     body_text=None, body_bytes=(STL_DIR / fixture).read_bytes())


def _log(*events, name='part-select'):
    return {'scenario': name, 'synthetic': True,
            'steps': [{'step': 'ask for a part', 'events': list(events)}]}


class JsonShapeTest(unittest.TestCase):
    def test_shape_keeps_keys_and_types(self):
        self.assertEqual(json_shape({'a': 1, 'b': ['x', 'y'], 'c': {'d': None}, 'e': []}),
                         {'a': 'number', 'b': ['string'], 'c': {'d': 'null'}, 'e': []})
        self.assertEqual(json_shape([1.5, 'x']), ['number'])

    def test_volatile_keys_are_their_type_only(self):
        self.assertEqual(json_shape({'id': {'deep': 1}, 'microversionId': 'abc', 'x': {'y': 2}}),
                         {'id': 'object', 'microversionId': 'string', 'x': {'y': 'number'}})


class DiffCassettesTest(unittest.TestCase):
    def test_identical_is_clean(self):
        stored = _cassette(_exchange(body={'items': [{'name': 'tb-box'}]}),
                           _stl_exchange('box_mm.stl'))
        fresh = _cassette(_exchange(body={'items': [{'name': 'tb-box'}]}),
                          _stl_exchange('box_mm.stl'))
        self.assertEqual(diff_cassettes(stored, fresh), [])

    def test_changed_request_path_is_request_kind(self):
        stored = _cassette(_exchange('/api/v13/documents'))
        fresh = _cassette(_exchange('/api/v13/documents/d/abc'))
        diffs = diff_cassettes(stored, fresh)
        self.assertTrue(diffs)
        self.assertEqual({d.kind for d in diffs}, {'request'})
        self.assertEqual(diffs[0].scenario, 'part-export')

    def test_changed_request_body_is_request_kind(self):
        stored = _cassette(_exchange(method='POST', request_body='{"name": "tb-box"}'))
        fresh = _cassette(_exchange(method='POST', request_body='{"name": "tb-cube"}'))
        self.assertEqual([d.kind for d in diff_cassettes(stored, fresh)], ['request'])

    def test_new_response_key_is_response_kind(self):
        stored = _cassette(_exchange(body={'items': [{'name': 'tb-box'}]}))
        fresh = _cassette(_exchange(body={'items': [{'name': 'tb-box', 'owner': 'x'}]}))
        diffs = diff_cassettes(stored, fresh)
        self.assertEqual([d.kind for d in diffs], ['response'])
        self.assertIn('owner', diffs[0].detail)

    def test_changed_status_and_value_type_are_response_kind(self):
        stored = _cassette(_exchange(body={'count': 1, 'size': 2}), _exchange('/api/v13/x'))
        fresh = _cassette(_exchange(body={'count': '1', 'size': 2.5}),
                          _exchange('/api/v13/x', status=404))
        self.assertEqual([(d.index, d.kind) for d in diff_cassettes(stored, fresh)],
                         [(0, 'response'), (1, 'response')])

    def test_volatile_ids_ignored(self):
        stored = _cassette(_exchange(query={'microversionId': ['m1']},
                                     body={'id': 'aaa', 'microversionId': 'm1',
                                           'modifiedAt': '2026-10-01T00:00:00Z'}))
        fresh = _cassette(_exchange(query={'microversionId': ['m2']},
                                    body={'id': 'bbb', 'microversionId': 'm2',
                                          'modifiedAt': '2026-10-09T12:00:00Z'}))
        self.assertEqual(diff_cassettes(stored, fresh), [])

    def test_sign_in_only_calls_ignored(self):
        sessioninfo = _exchange('/api/v13/users/sessioninfo', body={'id': 'u'})
        stored = _cassette(sessioninfo, _exchange())
        fresh = _cassette(_exchange())
        self.assertEqual(diff_cassettes(stored, fresh), [])
        self.assertEqual(diff_cassettes(fresh, stored), [])
        # Any other call in only one recording is reported.
        extra = _cassette(_exchange('/api/v13/companies'), _exchange())
        self.assertEqual([d.kind for d in diff_cassettes(fresh, extra)], ['request'])

    def test_mesh_bbox_change_reported(self):
        stored = _cassette(_stl_exchange('box_mm.stl'))
        fresh = _cassette(_stl_exchange('slab_mm.stl'))
        diffs = diff_cassettes(stored, fresh)
        self.assertEqual([d.kind for d in diffs], ['response'])
        self.assertIn('bounding box', diffs[0].detail)

    def test_redirect_target_change_is_response_kind(self):
        def hop(location):
            return _exchange('/api/v13/parts/stl', status=307, body_text=None,
                             headers={'Location': location})
        stored = _cassette(hop('https://a.example/one'),
                           _exchange('/one', host='a.example', headers=STL, body_text='solid'))
        fresh = _cassette(hop('https://b.example/two'),
                          _exchange('/two', host='b.example', headers=STL, body_text='solid'))
        self.assertEqual({d.kind for d in diff_cassettes(stored, fresh)}, {'response'})


class DiffMessageLogsTest(unittest.TestCase):
    def _event(self, direction, name, **data):
        return {'dir': direction, 'origin': 'https://cad.onshape.com', 't_ms': 5,
                'data': {'messageName': name, **data}}

    def test_identical_logs_are_clean(self):
        log = _log(self._event('out', 'requestSelection', messageId='testbed-1'),
                   self._event('in', 'REQUESTED_SELECTION', selections=[]))
        later = _log(self._event('out', 'requestSelection', messageId='testbed-2'),
                     self._event('in', 'REQUESTED_SELECTION', selections=[]))
        self.assertEqual(diff_message_logs(log, later), [])

    def test_sent_change_is_request_and_received_change_is_response(self):
        stored = _log(self._event('out', 'requestSelection'),
                      self._event('in', 'REQUESTED_SELECTION', selections=[]))
        fresh = _log(self._event('out', 'requestSelectionV2'),
                     self._event('in', 'REQUESTED_SELECTION', selections=[], status={}))
        self.assertEqual([d.kind for d in diff_message_logs(stored, fresh)],
                         ['request', 'response'])


class ReportTest(unittest.TestCase):
    def test_report_clean_and_listing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'drift.md'
            write_drift_report([], path)
            self.assertIn('clean', path.read_text())
            write_drift_report([Difference('part-export', 3, 'response', 'status 200 -> 404')],
                               path)
            text = path.read_text()
        self.assertNotIn('clean', text)
        self.assertIn('part-export', text)
        self.assertIn('status 200 -> 404', text)
        self.assertIn('Onshape changed', text)


class AcceptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for name, sub in (('FRESH_DIR', 'fresh'), ('CASSETTE_DIR', 'cassettes'),
                          ('MESSAGE_DIR', 'messages')):
            self.enterContext(mock.patch.object(settings, name, self.tmp / sub))

    def test_accept_moves_fresh_into_place(self):
        # A stored recording with an old binary the new one does not have.
        _cassette(_stl_exchange('box_mm.stl'), _stl_exchange('box_mm.stl'),
                  name='part-select').save(settings.CASSETTE_DIR)
        _cassette(_exchange(), _stl_exchange('slab_mm.stl'),
                  name='part-select').save(settings.fresh_cassette_dir())
        fresh_log = save_message_log(_log())

        moved = accept_fresh_recording('part-select')

        self.assertEqual(sorted(moved), sorted([
            settings.CASSETTE_DIR / 'part-select.json',
            settings.CASSETTE_DIR / 'part-select',
            settings.MESSAGE_DIR / 'part-select.json']))
        stored = Cassette.load(settings.CASSETTE_DIR, 'part-select')
        self.assertEqual(stored.exchanges[1].body(), (STL_DIR / 'slab_mm.stl').read_bytes())
        self.assertFalse((settings.CASSETTE_DIR / 'part-select' / '000.bin').exists())
        self.assertEqual(json.loads((settings.MESSAGE_DIR / 'part-select.json').read_text()),
                         _log())
        self.assertEqual(list(settings.fresh_cassette_dir().iterdir()), [])
        self.assertFalse(fresh_log.exists())

    def _assert_refused_and_nothing_moved(self, *expected):
        with self.assertRaises(RecordingNotClean) as caught:
            accept_fresh_recording('part-select')
        message = str(caught.exception)
        for fragment in expected:
            self.assertIn(fragment, message)
        self.assertNotIn('Real Person', message)          # file:line and kind, never the value
        self.assertFalse((settings.CASSETTE_DIR / 'part-select.json').exists())
        self.assertFalse((settings.MESSAGE_DIR / 'part-select.json').exists())
        self.assertTrue((settings.fresh_cassette_dir() / 'part-select.json').exists())
        self.assertTrue((settings.fresh_message_dir() / 'part-select.json').exists())

    def test_accept_refuses_an_identity_leak_and_moves_nothing(self):
        owner = {'owner': {'id': 'ab' * 12, 'name': 'Real Person'}}
        _cassette(_exchange(body=owner), name='part-select').save(settings.fresh_cassette_dir())
        save_message_log(_log())
        self._assert_refused_and_nothing_moved('part-select.json:', 'user-id', 'owner-name')

    def test_accept_refuses_a_secret_in_the_message_log_and_moves_nothing(self):
        _cassette(_exchange(), name='part-select').save(settings.fresh_cassette_dir())
        save_message_log(_log({'dir': 'in', 'data': {'messageName': 'x', 'mail': 'someone@team.org'}}))
        self._assert_refused_and_nothing_moved('messages/part-select.json:', 'email')

    def test_accept_refuses_a_secret_from_the_environment(self):
        _cassette(_exchange(body={'note': 'key-abc-123'}), name='part-select').save(settings.fresh_cassette_dir())
        save_message_log(_log())
        with mock.patch.dict('os.environ', {'ONSHAPE_ACCESS_KEY': 'key-abc-123'}):
            self._assert_refused_and_nothing_moved('literal')

    def test_accept_without_fresh_recording_raises(self):
        with self.assertRaisesRegex(FileNotFoundError, 'no fresh recording of part-select'):
            accept_fresh_recording('part-select')


if __name__ == '__main__':
    unittest.main()

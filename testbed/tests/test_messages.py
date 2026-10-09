"""Tests for testbed/messages.py: the message log format, saving scrubbed, and segments."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from testbed import adapters, settings
from testbed.messages import load_message_log, message_log_segment, save_message_log
from testbed.scrub import STANDIN_EMAIL, STANDIN_NAME, Scrubber, find_secrets

REAL_EMAIL = 'real.owner@school.example.org'
JWT = 'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJlLXZhbHVl'


def _log(events_by_step, scenario='part-select'):
    return {'scenario': scenario, 'synthetic': True,
            'steps': [{'step': step, 'events': events} for step, events in events_by_step]}


ASK = [{'dir': 'out', 'origin': 'https://cad.onshape.com',
        'data': {'messageName': 'requestSelection', 'messageId': 'testbed-1',
                 'entityTypeSpecifier': ['BODY'], 'requiredSelectionCount': 1},
        't_ms': 10},
       {'dir': 'in', 'origin': 'https://cad.onshape.com',
        'data': {'messageName': 'REQUESTED_SELECTION', 'messageId': 'testbed-1',
                 'status': 'PENDING'}, 't_ms': 25}]


class MessageLogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        adapters.reset_testbed_adapters_for_tests()
        self.addCleanup(adapters.reset_testbed_adapters_for_tests)

    def test_message_log_roundtrip_and_segment(self):
        log = _log([('choose print', []), ('ask for a part', ASK)])
        path = save_message_log(log, directory=self.tmp)
        self.assertEqual(path, self.tmp / 'part-select.json')
        self.assertEqual(load_message_log('part-select', directory=self.tmp), log)
        self.assertEqual(message_log_segment(log, 'ask for a part'), ASK)
        self.assertEqual(message_log_segment(log, 'choose print'), [])
        with self.assertRaisesRegex(KeyError, 'select part Box'):
            message_log_segment(log, 'select part Box')
        # The defaults are the committed and the fresh directories, read when called.
        with mock.patch.object(settings, 'FRESH_DIR', self.tmp / 'fresh'), \
                mock.patch.object(settings, 'MESSAGE_DIR', self.tmp / 'fresh'):
            save_message_log(log)
            self.assertEqual(load_message_log('part-select'), log)

    def test_saved_message_log_is_scrubbed(self):
        event = {'dir': 'in', 'origin': 'https://cad.onshape.com', 't_ms': 5,
                 'data': {'messageName': 'applicationInit', 'user': REAL_EMAIL,
                          'note': f'Bearer abc123 and {JWT}', REAL_EMAIL: 'as a key',
                          'nested': [{'owner': 'Ada Lovelace'}]}}
        log = _log([('open panel', [event])])
        scrubber = Scrubber.from_owner({'email': REAL_EMAIL, 'name': 'Ada Lovelace'}, [])
        text = save_message_log(log, directory=self.tmp, scrubber=scrubber).read_text()
        self.assertEqual(find_secrets(text, []), [])
        self.assertNotIn('Lovelace', text)
        saved = json.loads(text)['steps'][0]['events'][0]['data']
        self.assertEqual(saved['user'], STANDIN_EMAIL)
        self.assertEqual(saved[STANDIN_EMAIL], 'as a key')
        self.assertEqual(saved['nested'], [{'owner': STANDIN_NAME}])
        self.assertEqual(log['steps'][0]['events'][0]['data']['user'], REAL_EMAIL)  # not mutated

        # Without a scrubber: the recording's, when one is in progress...
        client = mock.Mock()
        client._make_api_request.side_effect = [
            mock.Mock(status_code=200, json=lambda: {'email': REAL_EMAIL, 'id': 'a' * 24}),
            mock.Mock(status_code=200, json=lambda: {'items': []})]
        with mock.patch.dict(os.environ, {'XDG_STATE_HOME': str(self.tmp / 'state')}):
            adapters.begin_recording('panel-load', client)
        text = save_message_log(_log([('open panel', [event])], 'panel-load'),
                                directory=self.tmp).read_text()
        self.assertNotIn(REAL_EMAIL, text)
        self.assertEqual(find_secrets(text, []), [])
        # ...and otherwise one that still removes every credential.
        adapters.reset_testbed_adapters_for_tests()
        text = save_message_log(log, directory=self.tmp).read_text()
        self.assertNotIn('abc123', text)
        self.assertNotIn(JWT, text)


if __name__ == '__main__':
    unittest.main()

"""Unit tests for printer_relay: the in-memory printer records, code handling and tokens.

No Flask app is involved -- printer_relay imports nothing from Flask, which is what lets
frc_cam_gui_app import it without a cycle."""
import os
import re
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from print import printer_relay
from print.printer_relay import PrinterRelay


class FakeClock:
    """An injectable wall clock. All relay ages and deadlines read through this."""

    def __init__(self, now=1_789_300_000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class TestCodeGeneration(unittest.TestCase):
    def test_code_is_eight_symbols_from_the_alphabet(self):
        for _ in range(200):
            code = printer_relay.generate_code()
            self.assertEqual(len(code), printer_relay.CODE_LENGTH)
            for ch in code:
                self.assertIn(ch, printer_relay.ALPHABET)

    def test_alphabet_omits_the_lookalike_letters(self):
        for ch in 'ILOU':
            self.assertNotIn(ch, printer_relay.ALPHABET)

    def test_code_is_never_all_digits(self):
        # An all-digit code would be loaded from YAML as an int unless the mentor quotes it.
        for _ in range(500):
            self.assertFalse(printer_relay.generate_code().isdigit())

    def test_format_code_groups_in_fours(self):
        self.assertEqual(printer_relay.format_code('7K3MQ9XW'), '7K3M-Q9XW')


class TestNormalization(unittest.TestCase):
    def test_uppercases_and_strips_separators(self):
        self.assertEqual(printer_relay.normalize_code(' 7k3m-q9xw '), '7K3MQ9XW')
        self.assertEqual(printer_relay.normalize_code('7K3M Q9XW'), '7K3MQ9XW')

    def test_maps_lookalike_letters(self):
        self.assertEqual(printer_relay.normalize_code('OIL23456'), '01123456')
        self.assertEqual(printer_relay.normalize_code('oil23456'), '01123456')

    def test_accepts_an_integer_from_unquoted_yaml(self):
        self.assertEqual(printer_relay.normalize_code(12345678), '12345678')

    def test_rejects_wrong_length_and_bad_symbols(self):
        self.assertIsNone(printer_relay.normalize_code('7K3MQ9X'))
        self.assertIsNone(printer_relay.normalize_code('7K3MQ9XWZ'))
        self.assertIsNone(printer_relay.normalize_code('7K3M-Q9X!'))
        self.assertIsNone(printer_relay.normalize_code(''))
        self.assertIsNone(printer_relay.normalize_code(None))
        self.assertIsNone(printer_relay.normalize_code({'a': 1}))


class TestTokens(unittest.TestCase):
    def test_round_trip_carries_the_code_and_issue_time(self):
        token = printer_relay.issue_token('7K3MQ9XW', 'secret-one', issued_at=1234.0)
        code, issued_at = printer_relay.read_token(token, 'secret-one')
        self.assertEqual(code, '7K3MQ9XW')
        self.assertEqual(issued_at, 1234.0)

    def test_token_is_rejected_under_a_different_key(self):
        token = printer_relay.issue_token('7K3MQ9XW', 'secret-one')
        self.assertIsNone(printer_relay.read_token(token, 'secret-two'))

    def test_garbage_is_rejected_without_raising(self):
        self.assertIsNone(printer_relay.read_token('not-a-token', 'secret-one'))
        self.assertIsNone(printer_relay.read_token('', 'secret-one'))
        self.assertIsNone(printer_relay.read_token(None, 'secret-one'))

    def test_token_survives_reuse_of_the_same_key(self):
        # The deploy case: a new process with the same FLASK_SECRET_KEY still accepts it.
        token = printer_relay.issue_token('7K3MQ9XW', 'persistent-key')
        self.assertEqual(printer_relay.read_token(token, 'persistent-key')[0], '7K3MQ9XW')


STATUS_DAY = 24 * 60 * 60

IDLE = {'state': 'idle', 'file': '', 'percent': None, 'remaining_min': None,
        'layer': 0, 'layers': 0, 'error': None,
        'printer': {'model': 'H2S', 'name': 'Shop H2S', 'serial': '01P00A'}}


def running(file_name, percent=42):
    snap = dict(IDLE)
    snap.update({'state': 'running', 'file': file_name, 'percent': percent,
                 'remaining_min': 31, 'layer': 87, 'layers': 210})
    return snap


class RelayTestCase(unittest.TestCase):
    """Shared fixture: a relay on a fake clock, with one code already presented."""

    CODE = '7K3MQ9XW'

    def setUp(self):
        self.clock = FakeClock()
        self.relay = PrinterRelay(clock=self.clock)
        self.relay.note_code(self.CODE)

    def hand_out_a_job(self, filename='sample_part.gcode.3mf'):
        """Exchange, sync, send and hand out -- the state most job tests start from."""
        self.assertTrue(self.relay.begin_exchange(self.CODE))
        self.relay.note_daemon(self.CODE)
        self.relay.store_status(self.CODE, IDLE)
        ok, reason, job = self.relay.claim_job(
            self.CODE, job_id='j8f3a2c1', download_path='/download/tok',
            filename=filename, team_number=6238)
        self.assertTrue(ok, reason)
        handed = self.relay.store_status(self.CODE, IDLE)
        self.assertEqual(handed['id'], 'j8f3a2c1')
        return handed


class TestNoteCodeAndExchange(RelayTestCase):
    def test_note_code_creates_a_record_and_returns_the_normal_form(self):
        fresh = PrinterRelay(clock=self.clock)
        self.assertEqual(fresh.note_code('7k3m-q9xw'), self.CODE)
        self.assertTrue(fresh.status_for(self.CODE)['paired'])

    def test_note_code_ignores_a_malformed_code(self):
        fresh = PrinterRelay(clock=self.clock)
        self.assertIsNone(fresh.note_code('nope'))
        self.assertIsNone(fresh.note_code(None))

    def test_exchange_refused_for_a_code_no_session_has_presented(self):
        self.assertFalse(self.relay.begin_exchange('ABCDEFGH'))

    def test_exchange_allowed_once_then_refused_while_the_daemon_is_heard(self):
        self.assertTrue(self.relay.begin_exchange(self.CODE))
        self.assertFalse(self.relay.begin_exchange(self.CODE))

    def test_exchange_allowed_again_after_two_minutes_of_silence(self):
        self.assertTrue(self.relay.begin_exchange(self.CODE))
        self.clock.advance(121)
        self.assertTrue(self.relay.begin_exchange(self.CODE))

    def test_seed_code_creates_a_record_like_a_session_would(self):
        fresh = PrinterRelay(clock=self.clock)
        fresh.seed_code(self.CODE)
        self.assertTrue(fresh.begin_exchange(self.CODE))


class TestStatusShape(RelayTestCase):
    def test_unpaired_when_there_is_no_code(self):
        payload = self.relay.status_for(None)
        self.assertEqual(payload['paired'], False)
        self.assertIsNone(payload['status'])
        self.assertIsNone(payload['job'])

    def test_waiting_before_any_daemon_is_heard(self):
        payload = self.relay.status_for(self.CODE)
        self.assertTrue(payload['paired'])
        self.assertTrue(payload['waiting'])
        self.assertFalse(payload['online'])
        self.assertEqual(payload['code'], '7K3M-Q9XW')

    def test_heard_but_no_snapshot_is_neither_waiting_nor_online(self):
        self.relay.note_daemon(self.CODE)
        payload = self.relay.status_for(self.CODE)
        self.assertFalse(payload['waiting'])
        self.assertFalse(payload['online'])
        self.assertIsNone(payload['status'])

    def test_online_and_age_come_from_the_relay_clock_not_the_snapshot(self):
        self.relay.note_daemon(self.CODE)
        self.relay.store_status(self.CODE, IDLE)   # the snapshot carries no timestamp
        self.clock.advance(7)
        payload = self.relay.status_for(self.CODE)
        self.assertTrue(payload['online'])
        self.assertEqual(payload['age_seconds'], 7)
        self.assertEqual(payload['status']['state'], 'idle')

    def test_offline_once_the_snapshot_is_older_than_two_minutes(self):
        self.relay.note_daemon(self.CODE)
        self.relay.store_status(self.CODE, IDLE)
        self.clock.advance(121)
        payload = self.relay.status_for(self.CODE)
        self.assertFalse(payload['online'])
        self.assertEqual(payload['age_seconds'], 121)

    def test_status_dropped_after_twenty_four_hours(self):
        """Only a daemon sync refreshes the snapshot, so a record a session keeps alive
        still loses its day-old status. The two TTLs are equal, so the touch has to fall
        between them for this to exercise the status branch rather than the record one."""
        self.relay.note_daemon(self.CODE)
        self.relay.store_status(self.CODE, IDLE)
        self.clock.advance(12 * 60 * 60)
        self.relay.note_code(self.CODE)      # a session read keeps the record alive
        self.clock.advance(13 * 60 * 60)     # the snapshot is now 25 hours old
        payload = self.relay.status_for(self.CODE)
        self.assertTrue(payload['paired'])   # the record survived
        self.assertIsNone(payload['status'])
        self.assertIsNone(payload['age_seconds'])
        self.assertFalse(payload['online'])

    def test_record_dropped_when_nothing_touches_it_for_a_day(self):
        self.clock.advance(STATUS_DAY + 1)
        payload = self.relay.status_for(self.CODE)
        self.assertFalse(payload['paired'])


class TestSendRefusals(RelayTestCase):
    def _claim(self):
        return self.relay.claim_job(self.CODE, job_id='jaaaaaaaa',
                                    download_path='/download/tok',
                                    filename='sample_part.gcode.3mf', team_number=6238)

    def test_refused_when_no_daemon_has_ever_been_heard(self):
        ok, reason, job = self._claim()
        self.assertFalse(ok)
        self.assertIn('has not reported', reason)
        self.assertIsNone(job)

    def test_refused_when_the_daemon_has_gone_quiet(self):
        self.relay.note_daemon(self.CODE)
        self.relay.store_status(self.CODE, IDLE)
        self.clock.advance(7 * 60)
        ok, reason, _ = self._claim()
        self.assertFalse(ok)
        self.assertIn('offline', reason)
        self.assertIn('7 min', reason)

    def test_refused_while_the_printer_is_running(self):
        self.relay.note_daemon(self.CODE)
        self.relay.store_status(self.CODE, running('other-jdeadbeef.gcode.3mf'))
        ok, reason, _ = self._claim()
        self.assertFalse(ok)
        self.assertIn('busy', reason)
        self.assertIn('42', reason)

    def test_refused_while_paused_unreachable_or_in_error(self):
        self.relay.note_daemon(self.CODE)
        for state, needle in (('paused', 'Paused'),
                              ('unreachable', 'unreachable'),
                              ('error', 'error')):
            snap = dict(IDLE)
            snap['state'] = state
            snap['error'] = 'HMS_0300_0100_0001_0004' if state == 'error' else None
            self.relay.store_status(self.CODE, snap)
            ok, reason, _ = self._claim()
            self.assertFalse(ok, state)
            self.assertIn(needle, reason)

    def test_refused_when_a_job_is_already_in_flight(self):
        self.hand_out_a_job()
        ok, reason, _ = self._claim()
        self.assertFalse(ok)
        self.assertIn('already on its way', reason)

    def test_accepted_when_idle_and_online(self):
        self.relay.note_daemon(self.CODE)
        self.relay.store_status(self.CODE, IDLE)
        ok, reason, job = self._claim()
        self.assertTrue(ok, reason)
        self.assertEqual(job['state'], 'pending')
        self.assertEqual(job['download_path'], '/download/tok')
        self.assertEqual(self.relay.status_for(self.CODE)['job']['state'], 'pending')


class TestJobLifecycle(RelayTestCase):
    def test_sync_hands_out_a_pending_job_once_then_repeats_it(self):
        self.relay.note_daemon(self.CODE)
        self.relay.store_status(self.CODE, IDLE)
        self.relay.claim_job(self.CODE, job_id='j8f3a2c1', download_path='/download/tok',
                             filename='sample_part.gcode.3mf', team_number=6238)
        first = self.relay.store_status(self.CODE, IDLE)
        self.assertEqual(first['state'], 'handed_out')
        again = self.relay.store_status(self.CODE, IDLE)
        self.assertEqual(again['id'], 'j8f3a2c1')     # a restarted daemon sees it again

    def test_sync_hands_out_nothing_when_there_is_no_job(self):
        self.relay.note_daemon(self.CODE)
        self.assertIsNone(self.relay.store_status(self.CODE, IDLE))

    def test_ack_records_the_outcome_and_clears_the_job(self):
        self.hand_out_a_job()
        found, team = self.relay.finish_job(self.CODE, 'j8f3a2c1', ok=True, reason=None)
        self.assertTrue(found)
        self.assertEqual(team, 6238)
        payload = self.relay.status_for(self.CODE)
        self.assertIsNone(payload['job'])
        self.assertTrue(payload['last_result']['ok'])

    def test_ack_for_an_unknown_job_is_not_found(self):
        self.hand_out_a_job()
        found, team = self.relay.finish_job(self.CODE, 'jffffffff', ok=True, reason=None)
        self.assertFalse(found)
        self.assertIsNone(team)

    def test_failed_ack_keeps_the_reason_for_ten_minutes(self):
        self.hand_out_a_job()
        self.relay.finish_job(self.CODE, 'j8f3a2c1', ok=False,
                              reason='Upload to printer failed: connection refused')
        self.assertIn('connection refused',
                      self.relay.status_for(self.CODE)['last_result']['reason'])
        self.clock.advance(601)
        self.assertIsNone(self.relay.status_for(self.CODE)['last_result'])

    def test_pending_job_expires_after_three_minutes(self):
        self.relay.note_daemon(self.CODE)
        self.relay.store_status(self.CODE, IDLE)
        self.relay.claim_job(self.CODE, job_id='j8f3a2c1', download_path='/download/tok',
                             filename='sample_part.gcode.3mf', team_number=6238)
        self.clock.advance(181)
        payload = self.relay.status_for(self.CODE)
        self.assertIsNone(payload['job'])
        self.assertFalse(payload['last_result']['ok'])
        self.assertIn('did not pick up', payload['last_result']['reason'])
        events = self.relay.drain_expiry_events()
        self.assertEqual([e.job_id for e in events], ['j8f3a2c1'])
        self.assertEqual(events[0].team_number, 6238)
        self.assertEqual(self.relay.drain_expiry_events(), [])

    def test_handed_out_job_expires_after_fifteen_minutes(self):
        self.hand_out_a_job()
        self.clock.advance(901)
        payload = self.relay.status_for(self.CODE)
        self.assertIsNone(payload['job'])
        self.assertIn('did not confirm', payload['last_result']['reason'])
        self.assertEqual([e.reason for e in self.relay.drain_expiry_events()],
                         ['printer did not confirm the job'])

    def test_snapshot_naming_the_job_clears_a_failed_last_result(self):
        """The printer has the job whatever the acknowledgement said."""
        self.hand_out_a_job()
        self.relay.finish_job(self.CODE, 'j8f3a2c1', ok=False, reason='timed out waiting')
        self.relay.store_status(self.CODE, running('sample_part-j8f3a2c1.gcode.3mf'))
        self.assertIsNone(self.relay.status_for(self.CODE)['last_result'])

    def test_snapshot_naming_another_file_leaves_last_result_alone(self):
        self.hand_out_a_job()
        self.relay.finish_job(self.CODE, 'j8f3a2c1', ok=False, reason='timed out waiting')
        self.relay.store_status(self.CODE, running('someone-else.gcode.3mf'))
        self.assertIsNotNone(self.relay.status_for(self.CODE)['last_result'])


class TestDaemonSurvivesARelayRestart(RelayTestCase):
    def test_a_valid_token_recreates_the_record_after_a_restart(self):
        """A redeploy empties the relay. The daemon's next sync must work, so status is
        back before any session opens PenguinCAM."""
        fresh = PrinterRelay(clock=self.clock)          # stands in for the new process
        self.assertIsNone(fresh.status_for(self.CODE)['status'])
        fresh.note_daemon(self.CODE)                    # what a valid bearer token does
        fresh.store_status(self.CODE, running('sample_part-j8f3a2c1.gcode.3mf'))
        payload = fresh.status_for(self.CODE)
        self.assertTrue(payload['paired'])
        self.assertTrue(payload['online'])
        self.assertEqual(payload['status']['state'], 'running')


class TestPing(RelayTestCase):
    def test_ping_reports_flags_and_stores_nothing(self):
        """`penguincam-printer status` must not be able to disturb the running service, so
        ping does NOT record daemon_seen_at -- otherwise a mentor running status could
        block the real daemon's re-exchange for two minutes."""
        flags = self.relay.ping(self.CODE)
        self.assertEqual(flags, {'ok': True, 'waiting': True, 'online': False})
        self.assertTrue(self.relay.begin_exchange(self.CODE))


class TestJobIds(unittest.TestCase):
    def test_job_id_is_j_plus_eight_hex_digits(self):
        for _ in range(200):
            job_id = printer_relay.new_job_id()
            self.assertEqual(len(job_id), 9)
            self.assertTrue(job_id.startswith('j'))
            self.assertTrue(all(ch in '0123456789abcdef' for ch in job_id[1:]), job_id)

    def test_job_ids_do_not_repeat(self):
        self.assertEqual(len({printer_relay.new_job_id() for _ in range(500)}), 500)


class TestEveryEntryPointNormalizes(RelayTestCase):
    """The rule: everywhere a code is read, it is normalised before use.

    A code reaches the relay from a YAML file, a typed pairing form and a bearer token, so
    the display form has to work wherever the canonical one does -- and a malformed one has
    to be refused rather than raise or key a ghost record."""

    TYPED = '7k3m-q9xw'

    def test_begin_exchange_accepts_the_display_form(self):
        self.assertTrue(self.relay.begin_exchange(self.TYPED))

    def test_note_daemon_accepts_the_display_form_without_a_ghost_record(self):
        self.assertEqual(self.relay.note_daemon(self.TYPED), self.CODE)
        self.assertFalse(self.relay.status_for(self.CODE)['waiting'])
        self.assertFalse(self.relay.status_for(self.TYPED)['waiting'])

    def test_store_status_and_finish_job_accept_the_display_form(self):
        self.hand_out_a_job()
        self.relay.store_status(self.TYPED, IDLE)
        found, team = self.relay.finish_job(self.TYPED, 'j8f3a2c1', ok=True, reason=None)
        self.assertTrue(found)
        self.assertEqual(team, 6238)

    def test_ping_accepts_the_display_form(self):
        self.relay.note_daemon(self.CODE)
        self.assertFalse(self.relay.ping(self.TYPED)['waiting'])

    def test_a_malformed_code_is_refused_everywhere_and_never_raises(self):
        for bad in (None, '', 'nope', 'OIL!', {'a': 1}):
            self.assertIsNone(self.relay.note_daemon(bad), bad)
            self.assertFalse(self.relay.begin_exchange(bad), bad)
            self.assertIsNone(self.relay.store_status(bad, IDLE), bad)
            self.assertEqual(self.relay.finish_job(bad, 'j8f3a2c1', ok=True, reason=None),
                             (False, None))
            ok, _, job = self.relay.claim_job(bad, job_id='jaaaaaaaa',
                                              download_path='/download/tok',
                                              filename='sample_part.gcode.3mf',
                                              team_number=6238)
            self.assertFalse(ok, bad)
            self.assertIsNone(job, bad)
            self.assertFalse(self.relay.ping(bad)['online'], bad)
        self.assertTrue(self.relay.begin_exchange(self.CODE))   # no ghost record blocked it


class TestFreshnessIsOneRule(RelayTestCase):
    def test_status_and_send_agree_on_a_fractional_boundary(self):
        """At 120.5 seconds the status line said online while a send refused as offline,
        because one comparison truncated the age to whole seconds first."""
        self.relay.note_daemon(self.CODE)
        self.relay.store_status(self.CODE, IDLE)
        self.clock.advance(120.5)
        payload = self.relay.status_for(self.CODE)
        self.assertFalse(payload['online'])
        self.assertFalse(self.relay.ping(self.CODE)['online'])
        ok, reason, _ = self.relay.claim_job(self.CODE, job_id='jaaaaaaaa',
                                             download_path='/download/tok',
                                             filename='sample_part.gcode.3mf',
                                             team_number=6238)
        self.assertFalse(ok)
        self.assertIn('offline', reason)

    def test_exactly_two_minutes_is_still_fresh_everywhere(self):
        self.relay.note_daemon(self.CODE)
        self.relay.store_status(self.CODE, IDLE)
        self.clock.advance(120)
        self.assertTrue(self.relay.status_for(self.CODE)['online'])
        self.assertTrue(self.relay.ping(self.CODE)['online'])
        ok, reason, _ = self.relay.claim_job(self.CODE, job_id='jaaaaaaaa',
                                             download_path='/download/tok',
                                             filename='sample_part.gcode.3mf',
                                             team_number=6238)
        self.assertTrue(ok, reason)


class TestReadsDoNotLeakTheStoredRecord(RelayTestCase):
    def test_mutating_the_returned_status_does_not_change_the_record(self):
        self.relay.note_daemon(self.CODE)
        self.relay.store_status(self.CODE, IDLE)
        payload = self.relay.status_for(self.CODE)
        payload['status']['state'] = 'running'
        payload['status']['printer']['name'] = 'somewhere else'
        again = self.relay.status_for(self.CODE)
        self.assertEqual(again['status']['state'], 'idle')
        self.assertEqual(again['status']['printer']['name'], 'Shop H2S')


class TestModuleLevelDelegate(unittest.TestCase):
    def test_note_code_creates_a_record_in_the_singleton(self):
        """frc_cam_gui_app calls printer_relay.note_code, not RELAY.note_code."""
        code = printer_relay.generate_code()
        self.assertEqual(printer_relay.note_code(printer_relay.format_code(code).lower()),
                         code)
        self.assertTrue(printer_relay.RELAY.status_for(code)['paired'])

    def test_note_code_ignores_a_malformed_code(self):
        self.assertIsNone(printer_relay.note_code('nope'))


class TestNoFlaskImport(unittest.TestCase):
    def test_the_module_never_imports_flask(self):
        """frc_cam_gui_app imports this module, so a Flask import here would be a cycle."""
        with open(printer_relay.__file__, encoding='utf-8') as handle:
            source = handle.read()
        for line in source.splitlines():
            self.assertIsNone(re.match(r'\s*(import|from)\s+flask\b', line, re.IGNORECASE),
                              line)


class TestStripJobSuffix(unittest.TestCase):
    """The daemon uploads 'sample_part-<job id>.gcode.3mf' so the printer's own display
    names the job; PenguinCAM shows the name without that suffix."""

    def test_strips_the_job_id_the_daemon_added(self):
        self.assertEqual(printer_relay.strip_job_suffix('sample_part-j8f3a2c1.gcode.3mf'),
                         'sample_part.gcode.3mf')

    def test_strips_a_real_new_job_id(self):
        job_id = printer_relay.new_job_id()
        self.assertEqual(
            printer_relay.strip_job_suffix(f'sample_part-{job_id}.gcode.3mf'),
            'sample_part.gcode.3mf')

    def test_leaves_any_other_name_alone(self):
        for name in ('sample_part.gcode.3mf',          # no suffix at all
                     'sample_part-j8f3a2.gcode.3mf',   # too short to be a job id
                     'sample_part-jzzzzzzz.gcode.3mf',  # not hex
                     'sample_part-j8f3a2c1.3mf',       # a different extension
                     'sample_part-j8f3a2c1.gcode.3mf.bak',  # not at the end
                     'holder v2.gcode.3mf',
                     ''):
            self.assertEqual(printer_relay.strip_job_suffix(name), name)

    def test_a_non_string_passes_through(self):
        self.assertIsNone(printer_relay.strip_job_suffix(None))


class TestDisplayNames(RelayTestCase):
    JOB_FILE = 'sample_part-j8f3a2c1.gcode.3mf'

    def test_status_carries_both_the_reported_and_the_display_name(self):
        self.relay.note_daemon(self.CODE)
        self.relay.store_status(self.CODE, running(self.JOB_FILE))
        status = self.relay.status_for(self.CODE)['status']
        self.assertEqual(status['file'], self.JOB_FILE)
        self.assertEqual(status['display_name'], 'sample_part.gcode.3mf')

    def test_job_carries_both_names(self):
        self.hand_out_a_job(filename=self.JOB_FILE)
        job = self.relay.status_for(self.CODE)['job']
        self.assertEqual(job['filename'], self.JOB_FILE)
        self.assertEqual(job['display_name'], 'sample_part.gcode.3mf')

    def test_the_busy_refusal_shows_the_name_without_the_job_id(self):
        self.relay.note_daemon(self.CODE)
        self.relay.store_status(self.CODE, running(self.JOB_FILE))
        ok, reason, _ = self.relay.claim_job(self.CODE, job_id='jaaaaaaaa',
                                             download_path='/download/tok',
                                             filename='sample_part.gcode.3mf',
                                             team_number=6238)
        self.assertFalse(ok)
        self.assertIn('sample_part.gcode.3mf', reason)
        self.assertNotIn('j8f3a2c1', reason)

    def test_the_paused_refusal_shows_the_name_without_the_job_id(self):
        self.relay.note_daemon(self.CODE)
        snap = running(self.JOB_FILE)
        snap['state'] = 'paused'
        self.relay.store_status(self.CODE, snap)
        ok, reason, _ = self.relay.claim_job(self.CODE, job_id='jaaaaaaaa',
                                             download_path='/download/tok',
                                             filename='sample_part.gcode.3mf',
                                             team_number=6238)
        self.assertFalse(ok)
        self.assertIn('Paused, sample_part.gcode.3mf', reason)

    def test_display_name_is_not_written_back_into_the_stored_snapshot(self):
        """status_for adds display_name to its own copy; the record keeps what the daemon
        sent, which is what the job-id match in store_status reads."""
        self.hand_out_a_job(filename=self.JOB_FILE)
        self.relay.finish_job(self.CODE, 'j8f3a2c1', ok=False, reason='timed out waiting')
        self.relay.status_for(self.CODE)
        self.relay.store_status(self.CODE, running(self.JOB_FILE))
        self.assertIsNone(self.relay.status_for(self.CODE)['last_result'])


class TestTokenDurability(unittest.TestCase):
    """A token signed with the app secret must survive a restart
    when FLASK_SECRET_KEY is set, and must be rejected under a different key.

    This is what makes FLASK_SECRET_KEY a requirement rather than a recommendation: without
    it every deploy mints a new random key, every daemon token dies, and every printer sits
    at 'has not reported' until someone opens PenguinCAM from Onshape."""

    def test_a_token_survives_a_process_restart_with_the_same_key(self):
        key = 'a-persistent-flask-secret-key'
        token = printer_relay.issue_token('7K3MQ9XW', key)
        # A new process reads the same key out of the environment and builds a new
        # serializer; nothing is carried over in memory.
        code, _ = printer_relay.read_token(token, key)
        self.assertEqual(code, '7K3MQ9XW')

    def test_a_token_minted_by_another_process_verifies_here(self):
        """The restart, for real: a second interpreter signs the token, this one reads it.

        The in-process test above cannot tell a durable signature from a cached serializer;
        a separate process can carry nothing over in memory, so this is the assertion that
        actually stands behind the deployment guide's FLASK_SECRET_KEY requirement."""
        key = 'a-persistent-flask-secret-key'
        repo_root = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))
        minted = subprocess.run(
            [sys.executable, '-c',
             'import sys; sys.path.insert(0, sys.argv[1]); '
             'from print import printer_relay; '
             'sys.stdout.write(printer_relay.issue_token(sys.argv[2], sys.argv[3]))',
             repo_root, '7K3MQ9XW', key],
            capture_output=True, text=True, check=True, cwd=repo_root).stdout.strip()
        self.assertTrue(minted)
        self.assertEqual(printer_relay.read_token(minted, key)[0], '7K3MQ9XW')

    def test_a_token_is_rejected_after_the_key_changes(self):
        token = printer_relay.issue_token('7K3MQ9XW', 'key-before-the-deploy')
        self.assertIsNone(printer_relay.read_token(token, 'key-after-the-deploy'))

    def test_the_salt_is_what_separates_printer_tokens_from_session_cookies(self):
        """A signature under the same key but a different salt must not verify here."""
        from itsdangerous import URLSafeSerializer
        forged = URLSafeSerializer('shared-key', salt='some-other-purpose').dumps(
            {'code': '7K3MQ9XW', 'iat': 0.0})
        self.assertIsNone(printer_relay.read_token(forged, 'shared-key'))

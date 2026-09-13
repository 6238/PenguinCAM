"""The run loop, with relay_client and printer replaced by fakes.

Every interval is on the injected monotonic clock and every sleep is recorded rather than
taken, so the whole suite runs in milliseconds."""
import os
import shutil
import tempfile
import unittest

from printer_daemon import printer as pr
from printer_daemon import run_loop as rl
from printer_daemon.config import DaemonConfig
from printer_daemon.relay_client import (
    ChallengeDetected, RateLimited, RelayError, TokenRejected,
)

IDLE = {'state': pr.IDLE, 'file': '', 'percent': None, 'remaining_min': None,
        'layer': 0, 'layers': 0, 'error': None,
        'printer': {'model': 'H2S', 'name': 'Shop H2S', 'serial': '01P00A'}}


def running(file_name):
    return dict(IDLE, state=pr.RUNNING, file=file_name, percent=3, remaining_min=41)


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeRelay:
    """Stands in for RelayClient. Queue exceptions to make a call fail."""

    def __init__(self, token=None):
        self.token = token
        self.exchanges = []
        self.syncs = []
        self.acks = []
        self.downloads = []
        self.exchange_result = 'signed'
        self.exchange_error = None
        self.sync_job = None
        self.sync_error = None
        self.ack_result = True
        self.download_error = None

    def exchange(self, code):
        self.exchanges.append(code)
        if self.exchange_error:
            error, self.exchange_error = self.exchange_error, None
            raise error
        self.token = self.exchange_result
        return self.exchange_result

    def sync(self, snapshot):
        self.syncs.append(snapshot)
        if self.sync_error:
            error, self.sync_error = self.sync_error, None
            raise error
        job, self.sync_job = self.sync_job, None
        return job

    def ack(self, job_id, ok, error=None):
        self.acks.append((job_id, ok, error))
        return self.ack_result

    def download(self, path, dest):
        self.downloads.append((path, dest))
        if self.download_error:
            raise self.download_error
        with open(dest, 'wb') as fh:
            fh.write(b'3mf-bytes')


class FakePrinter:
    def __init__(self, snapshot=None):
        self._snapshot = snapshot or dict(IDLE)
        self.uploads = []
        self.starts = []
        self.upload_error = None
        self.start_result = True

    def set(self, snapshot):
        self._snapshot = snapshot

    def snapshot(self):
        return dict(self._snapshot)

    def upload(self, local_path, remote_name):
        if self.upload_error:
            raise self.upload_error
        with open(local_path, 'rb') as fh:
            self.uploads.append((remote_name, fh.read()))

    def start(self, remote_name):
        self.starts.append(remote_name)
        return self.start_result


class StepExecutor:
    """Holds a submitted job until the test releases it.

    A real executor runs the job on another thread, so the loop keeps ticking while the job
    is still in flight. An inline executor cannot show that -- the job is over before the
    assertion -- so the tests that care about concurrency use this one instead."""

    def __init__(self):
        self.pending = []

    def submit(self, fn, *args, **kwargs):
        self.pending.append((fn, args, kwargs))

    def release(self):
        fn, args, kwargs = self.pending.pop(0)
        fn(*args, **kwargs)

    def shutdown(self, wait=True):
        pass


class RunLoopTestCase(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.slept = []
        self.logged = []
        self.saved_tokens = []
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.config = DaemonConfig(backend_url='https://example.test',
                                   printer_ip='10.0.0.42', printer_serial='01P00A',
                                   access_code='12345678', pairing_code='7K3MQ9XW',
                                   token='', path=os.path.join(self.tmp, 'config.toml'))
        self.relay = FakeRelay()
        self.printer = FakePrinter()

    def make(self, **overrides):
        kwargs = dict(clock=self.clock, sleep=self.slept.append,
                      log=lambda *a: self.logged.append(' '.join(str(x) for x in a)),
                      executor=rl.InlineExecutor(),
                      download_dir=self.tmp,
                      save_token=lambda token, path: self.saved_tokens.append(token))
        kwargs.update(overrides)
        return rl.RunLoop(self.config, self.relay, self.printer, **kwargs)


class TestExchange(RunLoopTestCase):
    def test_the_first_tick_exchanges_and_saves_the_token(self):
        loop = self.make()
        loop.tick()
        self.assertEqual(self.relay.exchanges, ['7K3MQ9XW'])
        self.assertEqual(self.saved_tokens, ['signed'])
        self.assertEqual(self.relay.token, 'signed')

    def test_a_404_retries_every_thirty_seconds_and_logs_once_a_minute(self):
        self.relay.exchange_result = None
        loop = self.make()
        for _ in range(5):
            loop.tick()
            self.clock.advance(30)
        self.assertEqual(len(self.relay.exchanges), 5)
        self.assertTrue(any('waiting for the pairing code' in line for line in self.logged))
        self.assertLessEqual(len([l for l in self.logged
                                  if 'waiting for the pairing code' in l]), 3)

    def test_no_exchange_before_thirty_seconds_have_passed(self):
        self.relay.exchange_result = None
        loop = self.make()
        loop.tick()
        self.clock.advance(29)
        loop.tick()
        self.assertEqual(len(self.relay.exchanges), 1)

    def test_a_429_honours_retry_after(self):
        self.relay.exchange_error = RateLimited(90.0)
        loop = self.make()
        loop.tick()
        self.clock.advance(60)
        loop.tick()
        self.assertEqual(len(self.relay.exchanges), 1)   # still inside the 90 s window
        self.clock.advance(31)
        loop.tick()
        self.assertEqual(len(self.relay.exchanges), 2)

    def test_a_401_on_sync_blanks_the_token_and_re_exchanges(self):
        self.relay.token = 'stale'
        self.config.token = 'stale'
        loop = self.make()
        self.relay.sync_error = TokenRejected('repair')
        loop.tick()                        # the sync that gets rejected
        self.assertIsNone(self.relay.token)
        self.assertEqual(self.saved_tokens, [''])
        self.clock.advance(30)
        loop.tick()                        # now it re-exchanges
        self.assertEqual(self.relay.exchanges, ['7K3MQ9XW'])


class TestSync(RunLoopTestCase):
    def make_paired(self, **overrides):
        self.relay.token = 'signed'
        self.config.token = 'signed'
        return self.make(**overrides)

    def test_the_snapshot_is_posted_every_five_seconds(self):
        loop = self.make_paired()
        loop.tick()
        self.assertEqual(len(self.relay.syncs), 1)
        self.clock.advance(4)
        loop.tick()
        self.assertEqual(len(self.relay.syncs), 1)
        self.clock.advance(2)
        loop.tick()
        self.assertEqual(len(self.relay.syncs), 2)

    def test_a_state_change_syncs_at_once(self):
        loop = self.make_paired()
        loop.tick()
        self.clock.advance(1)
        self.printer.set(running('sample_part-j8f3a2c1.gcode.3mf'))
        loop.tick()
        self.assertEqual(len(self.relay.syncs), 2)
        self.assertEqual(self.relay.syncs[-1]['state'], pr.RUNNING)

    def test_a_network_error_backs_off_doubling_to_a_cap(self):
        loop = self.make_paired()
        waits = []
        for _ in range(6):
            self.relay.sync_error = RelayError('connection refused')
            loop.tick()
            waits.append(loop.backoff_until - self.clock.now)   # the wait just applied
            self.clock.advance(waits[-1] + 1)
        self.assertEqual(waits, [5.0, 10.0, 20.0, 40.0, 60.0, 60.0])

    def test_a_successful_sync_resets_the_backoff(self):
        loop = self.make_paired()
        self.relay.sync_error = RelayError('boom')
        loop.tick()
        self.assertEqual(loop.backoff_until - self.clock.now, 5.0)
        self.clock.advance(10)
        loop.tick()
        self.assertEqual(loop.backoff, rl.BACKOFF_START)
        self.assertIsNone(loop.backoff_until)

    def test_a_cloudflare_challenge_is_named_in_the_log(self):
        loop = self.make_paired()
        self.relay.sync_error = ChallengeDetected('got HTML from the backend')
        loop.tick()
        self.assertTrue(any('HTML' in line or 'challenge' in line.lower()
                            for line in self.logged), self.logged)
        self.assertEqual(loop.last_error_kind, 'challenge')

    def test_a_429_on_sync_waits_out_retry_after(self):
        loop = self.make_paired()
        self.relay.sync_error = RateLimited(90.0)
        loop.tick()
        self.assertEqual(loop.backoff_until, self.clock.now + 90.0)
        self.clock.advance(60)
        loop.tick()
        self.assertEqual(len(self.relay.syncs), 1)       # still inside the window


JOB = {'id': 'j8f3a2c1', 'download_path': '/download/tok',
       'filename': 'sample_part.gcode.3mf', 'state': 'handed_out',
       'created_at': 1_789_300_000}
SUFFIXED = 'sample_part-j8f3a2c1.gcode.3mf'


class TestJobHandling(RunLoopTestCase):
    def make_paired(self, **overrides):
        self.relay.token = 'signed'
        self.config.token = 'signed'
        return self.make(**overrides)

    def deliver(self, loop, job=None):
        """Let one sync return a job."""
        self.relay.sync_job = dict(job or JOB)
        loop.tick()

    def test_a_job_is_downloaded_uploaded_started_and_acknowledged(self):
        loop = self.make_paired()
        plain_start = self.printer.start

        def start(remote_name):
            accepted = plain_start(remote_name)         # the fake records the name
            self.printer.set(running(remote_name))      # the printer picks it up at once
            return accepted

        self.printer.start = start
        self.deliver(loop)
        self.assertEqual(self.relay.downloads[0][0], '/download/tok')
        self.assertEqual(self.printer.uploads, [(SUFFIXED, b'3mf-bytes')])
        self.assertEqual(self.printer.starts, [SUFFIXED])
        self.assertEqual(self.relay.acks, [('j8f3a2c1', True, None)])

    def test_the_uploaded_name_carries_the_job_id(self):
        loop = self.make_paired()
        self.printer.start = lambda name: self.printer.set(running(name)) or True
        self.deliver(loop)
        self.assertEqual(self.printer.uploads[0][0], SUFFIXED)

    def test_a_download_failure_is_acknowledged_with_one_line(self):
        loop = self.make_paired()
        self.relay.download_error = RelayError('download answered 404')
        self.deliver(loop)
        job_id, ok, reason = self.relay.acks[0]
        self.assertFalse(ok)
        self.assertIn('404', reason)
        self.assertEqual(len(reason.splitlines()), 1)
        self.assertEqual(self.printer.uploads, [])

    def test_an_upload_failure_is_acknowledged_with_one_line(self):
        loop = self.make_paired()
        self.printer.upload_error = OSError('connection refused')
        self.deliver(loop)
        job_id, ok, reason = self.relay.acks[0]
        self.assertFalse(ok)
        self.assertIn('connection refused', reason)

    def test_a_start_that_returns_false_is_acknowledged_as_a_failure(self):
        loop = self.make_paired()
        self.printer.start_result = False
        self.deliver(loop)
        self.assertFalse(self.relay.acks[0][1])

    def test_a_printer_that_never_reports_running_times_out_at_sixty_seconds(self):
        """The printer stays idle after the start command."""
        loop = self.make_paired()
        ticks = {'n': 0}

        def sleep(seconds):
            ticks['n'] += 1
            self.clock.advance(seconds)

        loop.sleep = sleep
        started_at = self.clock.now
        self.deliver(loop)
        job_id, ok, reason = self.relay.acks[0]
        self.assertFalse(ok)
        self.assertIn('did not start', reason)
        self.assertEqual(self.clock.now - started_at, rl.START_WAIT_SECONDS)
        self.assertEqual(ticks['n'], rl.START_WAIT_SECONDS / rl.START_POLL_SECONDS)

    def test_the_same_job_returned_again_after_its_ack_is_ignored(self):
        """A sync in flight when the ack landed returns the job once more."""
        loop = self.make_paired()
        self.printer.start = lambda name: self.printer.set(running(name)) or True
        self.deliver(loop)
        self.assertEqual(len(self.relay.acks), 1)
        self.clock.advance(6)
        self.deliver(loop)
        self.assertEqual(len(self.relay.acks), 1)
        self.assertEqual(len(self.printer.uploads), 1)

    def test_the_same_job_offered_on_every_sync_is_handled_once(self):
        """The relay re-offers a handed-out job until it is acknowledged, so the loop must
        dedupe on every sync, not only across a restart."""
        loop = self.make_paired()
        self.printer.start = lambda name: self.printer.set(running(name)) or True
        for _ in range(4):
            self.deliver(loop)
            self.clock.advance(6)
        self.assertEqual(len(self.printer.uploads), 1)
        self.assertEqual(self.relay.acks, [('j8f3a2c1', True, None)])

    def test_a_job_the_printer_already_runs_is_acknowledged_without_uploading(self):
        """A daemon that died after the start command must not print the part twice."""
        loop = self.make_paired()
        self.printer.set(running(SUFFIXED))
        self.deliver(loop)
        self.assertEqual(self.printer.uploads, [])
        self.assertEqual(self.relay.acks, [('j8f3a2c1', True, None)])

    def test_a_job_the_printer_has_already_finished_is_acknowledged_ok(self):
        loop = self.make_paired()
        self.printer.set(dict(IDLE, state=pr.FINISHED, file=SUFFIXED, percent=100))
        self.deliver(loop)
        self.assertEqual(self.printer.uploads, [])
        self.assertEqual(self.relay.acks, [('j8f3a2c1', True, None)])

    def test_a_job_the_printer_paused_is_acknowledged_ok(self):
        loop = self.make_paired()
        self.printer.set(dict(IDLE, state=pr.PAUSED, file=SUFFIXED))
        self.deliver(loop)
        self.assertEqual(self.relay.acks, [('j8f3a2c1', True, None)])

    def test_a_job_the_printer_errored_on_is_acknowledged_with_the_printer_code(self):
        loop = self.make_paired()
        self.printer.set(dict(IDLE, state=pr.ERROR, file=SUFFIXED,
                              error='HMS_0300_0100_0001_0004'))
        self.deliver(loop)
        job_id, ok, reason = self.relay.acks[0]
        self.assertFalse(ok)
        self.assertIn('HMS_0300_0100_0001_0004', reason)
        self.assertEqual(self.printer.uploads, [])
        self.assertEqual([l for l in self.logged if 'already has' in l], [])

    def test_a_404_on_the_acknowledgement_is_final_not_retried(self):
        loop = self.make_paired()
        self.relay.ack_result = False
        self.printer.start = lambda name: self.printer.set(running(name)) or True
        self.deliver(loop)
        self.assertEqual(len(self.relay.acks), 1)
        self.assertTrue(any('no longer' in line or '404' in line for line in self.logged),
                        self.logged)
        self.clock.advance(6)
        self.deliver(loop)
        self.assertEqual(len(self.relay.acks), 1)     # never sent again

    def test_syncs_continue_while_a_job_runs(self):
        """The job stays in flight on the executor -- as it would on its own thread -- and
        the loop must keep posting the snapshot the whole time."""
        executor = StepExecutor()
        loop = self.make_paired(executor=executor)
        self.printer.start = lambda name: self.printer.set(running(name)) or True
        self.deliver(loop)
        self.assertEqual(len(executor.pending), 1)          # still running
        self.assertEqual(loop.running_job_id, 'j8f3a2c1')
        before = len(self.relay.syncs)
        for _ in range(3):
            self.clock.advance(6)
            loop.tick()
        self.assertEqual(len(self.relay.syncs), before + 3)
        self.assertEqual(self.relay.acks, [])               # the job has not finished yet
        executor.release()
        self.assertEqual(self.relay.acks, [('j8f3a2c1', True, None)])
        self.assertIsNone(loop.running_job_id)

    def test_a_job_re_offered_while_it_is_still_running_is_not_started_twice(self):
        executor = StepExecutor()
        loop = self.make_paired(executor=executor)
        self.deliver(loop)
        self.clock.advance(6)
        self.deliver(loop)                                  # the relay offers it again
        self.assertEqual(len(executor.pending), 1)

    def test_a_second_job_offered_while_one_runs_is_left_for_later(self):
        executor = StepExecutor()
        loop = self.make_paired(executor=executor)
        self.deliver(loop)
        self.clock.advance(6)
        self.deliver(loop, job=dict(JOB, id='j00000002'))
        self.assertEqual(len(executor.pending), 1)
        self.assertEqual(loop.running_job_id, 'j8f3a2c1')

    def test_a_job_that_raises_before_its_first_ack_releases_the_slot(self):
        """running_job_id is cleared in a finally, so one broken job cannot wedge the
        daemon until it is restarted."""
        loop = self.make_paired()
        calls = {'n': 0}
        healthy = self.printer.snapshot

        def flaky():
            calls['n'] += 1
            if calls['n'] > 1:                  # the sync's snapshot is the first call
                raise RuntimeError('mqtt blew up')
            return healthy()

        self.printer.snapshot = flaky
        self.deliver(loop)
        self.assertIsNone(loop.running_job_id)
        job_id, ok, reason = self.relay.acks[0]
        self.assertEqual(job_id, 'j8f3a2c1')
        self.assertFalse(ok)
        self.assertIn('mqtt blew up', reason)
        self.assertTrue(any('Traceback' in line for line in self.logged), self.logged)

    def test_the_downloaded_file_is_cleaned_up(self):
        loop = self.make_paired()
        self.printer.start = lambda name: self.printer.set(running(name)) or True
        self.deliver(loop)
        dest = self.relay.downloads[0][1]
        self.assertFalse(os.path.exists(dest), dest)


class TestJobSlotAndAcknowledgement(RunLoopTestCase):
    """The bookkeeping around a job: who owns the slot, and which ids are remembered."""

    def make_paired(self, **overrides):
        self.relay.token = 'signed'
        self.config.token = 'signed'
        return self.make(**overrides)

    def deliver(self, loop, job=None):
        self.relay.sync_job = dict(job or JOB)
        loop.tick()

    def test_a_job_finishing_late_does_not_blank_its_successors_slot(self):
        """The slot has one owner: a finished job clears it only while it still holds it."""
        loop = self.make_paired()
        self.printer.start = lambda name: self.printer.set(running(name)) or True
        loop.running_job_id = 'jbbbbbbb'          # B took the slot while A was finishing
        loop.handle_job(dict(JOB))                # A finishes late
        self.assertEqual(self.relay.acks, [('j8f3a2c1', True, None)])
        self.assertEqual(loop.running_job_id, 'jbbbbbbb')

    def test_a_transient_acknowledgement_failure_is_retried_on_the_re_offer(self):
        """A dropped ack must not strand a print that actually ran: the id is remembered
        only on a definite answer, and the re-offer acknowledges it from the printer's own
        report without a second upload."""
        loop = self.make_paired()
        self.printer.start = lambda name: self.printer.set(running(name)) or True

        def flaky_ack(job_id, ok, error=None):
            self.relay.acks.append((job_id, ok, error))
            if len(self.relay.acks) == 1:
                raise RelayError('connection reset by peer')
            return True

        self.relay.ack = flaky_ack
        self.deliver(loop)
        self.assertNotIn('j8f3a2c1', loop.acked_job_ids)
        self.assertIsNone(loop.running_job_id)
        self.clock.advance(6)
        self.deliver(loop)                         # the relay offers it again
        self.assertEqual(len(self.printer.uploads), 1)      # not uploaded a second time
        self.assertEqual(len(self.relay.acks), 2)
        self.assertIn('j8f3a2c1', loop.acked_job_ids)

    def test_the_remembered_acknowledgements_are_bounded(self):
        loop = self.make_paired()
        for n in range(rl.ACK_MEMORY + 5):
            loop._ack(f'j{n:08x}', True)
        self.assertEqual(len(loop.acked_job_ids), rl.ACK_MEMORY)
        self.assertNotIn('j00000000', loop.acked_job_ids)
        self.assertIn(f'j{rl.ACK_MEMORY + 4:08x}', loop.acked_job_ids)

    def test_a_deferred_job_is_logged_once_not_once_per_sync(self):
        executor = StepExecutor()
        loop = self.make_paired(executor=executor)
        self.deliver(loop)
        for _ in range(3):
            self.clock.advance(6)
            self.deliver(loop, job=dict(JOB, id='j00000002'))
        deferrals = [l for l in self.logged if 'j00000002' in l]
        self.assertEqual(len(deferrals), 1, self.logged)


class TestUntrustedJobFields(RunLoopTestCase):
    """The filename and the download path come from the relay, so neither may reach the
    Pi's filesystem or a status line unexamined."""

    def make_paired(self, **overrides):
        self.relay.token = 'signed'
        self.config.token = 'signed'
        return self.make(**overrides)

    def deliver(self, loop, job=None):
        self.relay.sync_job = dict(job or JOB)
        loop.tick()

    def test_a_filename_with_a_path_cannot_escape_the_download_directory(self):
        loop = self.make_paired()
        self.printer.start = lambda name: self.printer.set(running(name)) or True
        self.deliver(loop, job=dict(JOB, filename='../../etc/evil.gcode.3mf'))
        dest = self.relay.downloads[0][1]
        self.assertEqual(os.path.dirname(os.path.abspath(dest)),
                         os.path.abspath(self.tmp))
        self.assertEqual(self.printer.uploads[0][0], 'evil-j8f3a2c1.gcode.3mf')

    def test_a_download_token_never_reaches_the_acknowledgement(self):
        loop = self.make_paired()
        self.relay.download_error = RelayError(
            'GET /download/9f3c2b1a-very-secret failed: timed out')
        self.deliver(loop)
        reason = self.relay.acks[0][2]
        self.assertNotIn('9f3c2b1a', reason)
        self.assertIn('/download/<token>', reason)

    def test_a_long_multi_line_failure_becomes_one_short_line(self):
        loop = self.make_paired()
        self.printer.upload_error = OSError('x' * 400 + '\nand a second line')
        self.deliver(loop)
        reason = self.relay.acks[0][2]
        self.assertEqual(len(reason.splitlines()), 1)
        self.assertLessEqual(len(reason), 200)
        self.assertTrue(any('Traceback' in line for line in self.logged), self.logged)


class TestPreflightAndRunForever(RunLoopTestCase):
    def test_a_plain_http_backend_url_is_refused(self):
        self.config.backend_url = 'http://example.test'
        loop = self.make()
        with self.assertRaises(ValueError):
            loop.preflight()

    def test_a_dev_backend_url_is_warned_about_by_name(self):
        self.config.backend_url = 'http://10.0.0.5:6238'
        self.config.dev = True
        loop = self.make()
        loop.preflight()
        self.assertTrue(any('10.0.0.5:6238' in line and 'WARNING' in line
                            for line in self.logged), self.logged)

    def test_an_https_backend_url_warns_about_nothing(self):
        loop = self.make()
        loop.preflight()
        self.assertEqual([l for l in self.logged if 'WARNING' in l], [])

    def test_run_forever_keeps_going_after_an_unexpected_error(self):
        loop = self.make()
        calls = {'n': 0}

        def sleep(seconds):
            calls['n'] += 1
            self.clock.advance(seconds)
            if calls['n'] >= 3:
                raise KeyboardInterrupt      # the only way out of a forever loop

        def boom():
            raise RuntimeError('kaboom')

        loop.sleep = sleep
        loop.tick = boom
        with self.assertRaises(KeyboardInterrupt):
            loop.run_forever()
        self.assertEqual(calls['n'], 3)
        self.assertTrue(any('kaboom' in line for line in self.logged), self.logged)
        self.assertTrue(any('Traceback' in line for line in self.logged), self.logged)


if __name__ == '__main__':
    unittest.main()


class TestJobProgressIsLogged(TestJobHandling):
    """A mentor running the daemon by hand on a laptop watches its output, so every stage
    of a job, and its outcome, has to appear there. The journal on a Pi gets the same."""

    def test_a_successful_job_logs_its_stages_and_the_ok(self):
        loop = self.make_paired()
        self.printer.start = lambda name: self.printer.set(running(name)) or True
        self.deliver(loop)
        text = '\n'.join(self.logged)
        self.assertIn('j8f3a2c1', text)
        self.assertIn(SUFFIXED, text)
        self.assertTrue(any('acknowledged: ok' in line for line in self.logged), self.logged)

    def test_a_failed_job_logs_the_reason_it_acknowledged(self):
        loop = self.make_paired()
        self.printer.start = lambda name: False
        self.deliver(loop)
        self.assertTrue(any('acknowledged' in line and 'refused' in line
                            for line in self.logged), self.logged)

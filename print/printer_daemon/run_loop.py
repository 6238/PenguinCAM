"""The `run` command's loop.

Written as a tick() that does at most one thing and returns, with every interval measured
on the monotonic clock through an injected `clock`. That is what makes the thirty-second
exchange, the five-second sync, the sixty-second start wait and the backoff testable
without sleeping -- and it is also why a Pi with no network time source, or one whose clock
jumps when it finally gets one, behaves exactly like any other.

One job runs at a time, on a one-worker executor, so syncs keep going during an upload and
the printer never looks offline while a file is on its way.
"""

import os
import re
import tempfile
import time
import traceback
from collections import deque
from concurrent.futures import ThreadPoolExecutor

from printer_daemon import config as config_module
from printer_daemon import printer as printer_module
from printer_daemon.relay_client import (
    ChallengeDetected, RateLimited, RelayError, TokenRejected,
)

EXCHANGE_INTERVAL = 30.0
SYNC_INTERVAL = 5.0
BACKOFF_START = 5.0
BACKOFF_CAP = 60.0
START_WAIT_SECONDS = 60.0
START_POLL_SECONDS = 2.0
LOG_EVERY = 60.0
TICK_SECONDS = 1.0

# How many acknowledged job ids to remember. The set only has to outlive the re-offers of a
# job the relay has not yet marked done, so a couple of hundred is generous; unbounded, it
# would be the one structure in the daemon that grows for as long as the Pi is up.
ACK_MEMORY = 200

# A download path carries a bearer token. It must not travel back to the relay in an
# acknowledgement reason, which is shown in the wizard and kept in the job record.
_DOWNLOAD_PATH_RE = re.compile(r'(/download/)[^\s\'"]+')

# The printer states that mean it accepted the file: any of them answers the only question
# an acknowledgement asks, which is whether the job reached the machine.
_ACCEPTED = (printer_module.RUNNING, printer_module.PAUSED, printer_module.FINISHED)


class InlineExecutor:
    """Runs the job on the calling thread. Used by the tests, so no thread is involved."""

    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)

    def shutdown(self, wait=True):
        pass


class RunLoop:
    """One printer, one backend, one job at a time."""

    def __init__(self, config, relay, printer, *, clock=time.monotonic, sleep=time.sleep,
                 log=print, executor=None, download_dir=None,
                 save_token=config_module.save_token):
        self.config = config
        self.relay = relay
        self.printer = printer
        self.clock = clock
        self.sleep = sleep
        self.log = log
        self.executor = executor if executor is not None else ThreadPoolExecutor(max_workers=1)
        self.download_dir = download_dir or tempfile.gettempdir()
        self.save_token = save_token

        self.backoff = BACKOFF_START
        self.backoff_until = None
        self.last_error_kind = None
        self.last_exchange_at = None
        self.last_exchange_log_at = None
        self.next_exchange_at = None
        self.last_sync_at = None
        self.last_state = None
        self.running_job_id = None
        self.acked_job_ids = set()
        self._acked_order = deque()
        self._deferred_logged = set()

    # -- helpers -----------------------------------------------------------------------

    def _due(self, last, interval):
        return last is None or self.clock() - last >= interval

    def _blocked(self):
        return self.backoff_until is not None and self.clock() < self.backoff_until

    def _fail(self, kind, message):
        """Log, name the failure, and back off, doubling to a cap."""
        self.last_error_kind = kind
        self.log(f"{kind}: {message}; retrying in {self.backoff:.0f}s")
        self.backoff_until = self.clock() + self.backoff
        self.backoff = min(self.backoff * 2, BACKOFF_CAP)

    def _succeeded(self):
        self.backoff = BACKOFF_START
        self.backoff_until = None
        self.last_error_kind = None

    # -- the two halves of a tick -------------------------------------------------------

    def _exchange(self):
        """Trade the pairing code for a token. Runs until there is one, then never again
        unless a sync answers 401."""
        if self.next_exchange_at is not None and self.clock() < self.next_exchange_at:
            return
        if not self._due(self.last_exchange_at, EXCHANGE_INTERVAL):
            return
        self.last_exchange_at = self.clock()
        try:
            token = self.relay.exchange(self.config.pairing_code)
        except RateLimited as e:
            self.next_exchange_at = self.clock() + e.retry_after
            self.log(f"pairing is rate limited; waiting {e.retry_after:.0f}s")
            return
        except ChallengeDetected as e:
            self._fail('challenge', str(e))
            return
        except RelayError as e:
            self._fail('network', str(e))
            return
        self.next_exchange_at = None
        if not token:
            # Once a minute, not every thirty seconds: this is the normal state between
            # running `pair` on the Pi and the mentor pasting the code into the YAML.
            if self._due(self.last_exchange_log_at, LOG_EVERY):
                self.last_exchange_log_at = self.clock()
                self.log("waiting for the pairing code to appear in the team config")
            return
        self.save_token(token, self.config.path)
        self.config.token = token
        self._succeeded()
        self.log("paired with the backend")

    def _sync(self):
        """Post the snapshot; hand any returned job to the job thread."""
        snapshot = self.printer.snapshot()
        state_changed = snapshot.get('state') != self.last_state
        if not state_changed and not self._due(self.last_sync_at, SYNC_INTERVAL):
            return
        self.last_sync_at = self.clock()
        self.last_state = snapshot.get('state')
        try:
            job = self.relay.sync(snapshot)
        except TokenRejected:
            self.log("the backend rejected our token; re-pairing")
            self.relay.token = None
            self.config.token = ''
            self.save_token('', self.config.path)
            self.last_exchange_at = None
            return
        except RateLimited as e:
            self.log(f"the backend is rate limiting us; waiting {e.retry_after:.0f}s")
            self.backoff_until = self.clock() + e.retry_after
            return
        except ChallengeDetected as e:
            self._fail('challenge', str(e))
            return
        except RelayError as e:
            self._fail('network', str(e))
            return
        self._succeeded()
        if job:
            self._maybe_start_job(job)

    def tick(self):
        """One pass. Does at most one network thing, then returns."""
        if self._blocked():
            return
        if not self.relay.token:
            self._exchange()
            return
        self._sync()

    def preflight(self):
        """Refuse a backend URL the daemon must not talk to, and name a development one.

        load_config and save_config deliberately do not enforce this -- a config file the
        daemon cannot use must still be readable by `status` -- so the check belongs here,
        once, before the first request."""
        config_module.check_backend_url(self.config.backend_url, dev=self.config.dev)
        if self.config.dev:
            self.log(f"WARNING: development mode, talking to {self.config.backend_url} "
                     "over an unverified connection")

    def run_forever(self):
        """Never exits on a single failure; systemd restarts the process if it dies for any
        other reason."""
        self.preflight()
        while True:
            try:
                self.tick()
            except Exception as e:                  # nothing may kill the loop
                self._fail('unexpected', repr(e))
                self.log(traceback.format_exc())     # the whole of it, for the journal
            self.sleep(TICK_SECONDS)

    # -- the job -----------------------------------------------------------------------

    def _maybe_start_job(self, job):
        """Hand a job to the job thread, unless this process has already dealt with it.

        The relay re-offers a handed-out job on every sync until it is acknowledged, so the
        guard runs on every sync and not only after a restart: the set of acknowledged ids
        covers a job already finished, and running_job_id covers the one in flight."""
        job_id = job.get('id')
        if not job_id or job_id in self.acked_job_ids:
            return
        if self.running_job_id is not None:
            # Once per deferred job, not once per sync: the relay re-offers it every five
            # seconds, and a journal full of the same line hides everything else.
            if job_id != self.running_job_id and job_id not in self._deferred_logged:
                self._deferred_logged.add(job_id)
                self.log(f"job {job_id} offered while {self.running_job_id} is still "
                         "running; leaving it for the next sync")
            return
        self._deferred_logged.discard(job_id)
        self.running_job_id = job_id
        self.executor.submit(self.handle_job, job)

    def _ack(self, job_id, ok, reason=None):
        """Acknowledge once, and remember the job only when the answer was definite.

        A False answer means the relay no longer holds the job (404), which the daemon
        treats as final rather than retrying. A RelayError is transient, so the id is NOT
        remembered: the relay keeps re-offering the job, and the next attempt acknowledges
        it from the printer's own report without uploading anything a second time. Marking
        it acknowledged here would strand a print that actually ran until the relay expired
        it."""
        try:
            held = self.relay.ack(job_id, ok, reason)
        except RelayError as e:
            self.log(f"could not acknowledge {job_id}: {e}; will try again on the re-offer")
            return
        self._remember_acked(job_id)
        self.log(f"job {job_id} acknowledged: {'ok' if ok else reason}")
        if not held:
            self.log(f"the backend no longer holds job {job_id} (404); treating as final")

    def _remember_acked(self, job_id):
        """Add an id to the bounded set of jobs this process has finished with."""
        if job_id in self.acked_job_ids:
            return
        if len(self._acked_order) >= ACK_MEMORY:
            self.acked_job_ids.discard(self._acked_order.popleft())
        self._acked_order.append(job_id)
        self.acked_job_ids.add(job_id)

    def handle_job(self, job):
        """Run one job, and free the slot however it ends.

        The slot is cleared in a finally: a job that raises before its first acknowledgement
        -- a malformed job document, a printer link that throws -- must not leave the daemon
        refusing every later job until someone restarts it. It is cleared only while this
        job still owns it, so a job finishing late cannot blank the slot of the one that
        has since taken it."""
        job_id = job.get('id')
        try:
            self._run_job(job)
        except Exception as e:
            self.log(f"job {job_id} failed unexpectedly: {e!r}")
            self.log(traceback.format_exc())          # the whole of it, for the journal
            if job_id and job_id not in self.acked_job_ids:
                self._ack(job_id, False, self._one_line(e))
        finally:
            if self.running_job_id == job_id:
                self.running_job_id = None

    def _run_job(self, job):
        """Download, upload, start, wait, acknowledge. Every failure acknowledges with one
        line of reason; the full traceback stays in the Pi's journal."""
        job_id = job['id']
        # basename first: the filename comes from the relay, and neither the name sent to
        # the printer nor the path written on the Pi may contain a directory of its own.
        remote_name = printer_module.suffixed_name(os.path.basename(job['filename']), job_id)

        # The printer already has this job: a daemon that died after the start command must
        # not print the part a second time. The note is logged only if the printer is
        # actually reporting the file as accepted, so an error is not announced as a success.
        if self._acknowledge_if_the_printer_has_it(
                job_id, remote_name,
                note=f"the printer already has {remote_name}; not uploading it again"):
            return

        dest = os.path.join(self.download_dir, os.path.basename(remote_name))
        # Each stage is logged as it starts, so a mentor watching the output (or the
        # journal) can see where a job is when it stalls. The download path is not logged:
        # it carries the file's bearer token.
        try:
            try:
                self.log(f"job {job_id}: downloading the sliced file")
                self.relay.download(job['download_path'], dest)
                self.log(f"job {job_id}: uploading to the printer as {remote_name}")
                self.printer.upload(dest, remote_name)
            finally:
                self._discard(dest)
            self.log(f"job {job_id}: starting the print")
            if not self.printer.start(remote_name):
                self._ack(job_id, False, 'the printer refused the print start command')
                return
            self.log(f"job {job_id}: start accepted; waiting for the printer to report "
                     f"{remote_name}")
        except Exception as e:
            self.log(f"job {job_id} failed: {e!r}")
            self.log(traceback.format_exc())          # the whole of it, for the journal
            self._ack(job_id, False, self._one_line(e))
            return

        # Wait for the printer to confirm it is running OUR file, so a start that silently
        # did nothing is reported rather than shown as a success.
        deadline = self.clock() + START_WAIT_SECONDS
        while self.clock() < deadline:
            if self._acknowledge_if_the_printer_has_it(job_id, remote_name):
                return
            self.sleep(START_POLL_SECONDS)
        self._ack(job_id, False, 'the printer did not start the file within '
                                 f'{START_WAIT_SECONDS:.0f}s')

    def _acknowledge_if_the_printer_has_it(self, job_id, remote_name, *, note=None):
        """Acknowledge when the printer is reporting our file. `note` is logged only when
        the printer has accepted it.

        The file name carries the job id, so this cannot mistake the previous print for
        ours -- which is what lets a restarted daemon recognise a job it already started."""
        snapshot = self.printer.snapshot()
        if snapshot.get('file') != remote_name:
            return False
        state = snapshot.get('state')
        if state in _ACCEPTED:
            if note:
                self.log(note)
            self._ack(job_id, True)
            return True
        if state == printer_module.ERROR:
            self._ack(job_id, False,
                      f"printer error: {snapshot.get('error') or 'unknown'}")
            return True
        return False

    @staticmethod
    def _discard(path):
        """The sliced file is the relay's copy, not the Pi's: it goes as soon as the printer
        has it, so a shop Pi's small card cannot fill up with old prints."""
        try:
            os.unlink(path)
        except OSError:
            pass

    @staticmethod
    def _one_line(error):
        """One line for the status line; the traceback belongs in the journal only.

        A download path is a bearer token, and this string goes back to the relay and into
        the wizard, so any that survived into the message is redacted first."""
        text = str(error) or error.__class__.__name__
        text = _DOWNLOAD_PATH_RE.sub(r'\1<token>', text)
        return text.splitlines()[0][:200]

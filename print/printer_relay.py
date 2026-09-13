"""In-memory relay state for the Bambu printer daemon.

Keyed by the team's eight-character pairing code, which is the ONLY identity the relay
trusts: an Onshape session can claim any team number it likes in its own YAML, but the
pairing code is forty random bits that live only in the team's Onshape documents and on the
team's Pi. Nothing here is written to disk -- a restart empties the relay, and the daemon
recovers by syncing again.

Deliberately free of Flask imports: frc_cam_gui_app imports this module, so an import of
Flask here would be a cycle. Everything Flask-shaped lives in printer_routes.py.
"""

import copy
import re
import secrets
import threading
import time
from collections import namedtuple, deque

from itsdangerous import URLSafeSerializer

# Crockford base32 without I, L, O and U, so no two symbols look alike when a mentor reads
# the code off a Pi's console and types it into a YAML file.
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
CODE_LENGTH = 8

TOKEN_SALT = "penguincam-printer"

# A daemon counts as heard, and a snapshot as fresh, for two minutes.
DAEMON_ONLINE_SECONDS = 120
STATUS_ONLINE_SECONDS = 120
# Records and snapshots are dropped a day after anything last touched them.
RECORD_TTL_SECONDS = 24 * 60 * 60
STATUS_TTL_SECONDS = 24 * 60 * 60
# Job deadlines: pick-up and confirmation.
JOB_PENDING_TIMEOUT_SECONDS = 3 * 60
JOB_HANDED_OUT_TIMEOUT_SECONDS = 15 * 60
# How long a finished job's outcome is shown on the status line.
LAST_RESULT_TTL_SECONDS = 10 * 60

# Emitted by the relay when a job dies on a deadline; the routes drain these and log a
# printer_ack metrics event, so the metrics show every job's end, not just the acked ones.
ExpiryEvent = namedtuple('ExpiryEvent', 'code job_id team_number reason')


def generate_code():
    """A fresh pairing code: eight symbols, forty bits, never all digits.

    An all-digit code would be read out of an unquoted YAML value as an int, so it is
    redrawn -- that way the mentor can quote it or not and the file still works."""
    while True:
        code = ''.join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))
        if not code.isdigit():
            return code


def normalize_code(value):
    """The canonical form of a pairing code, or None when it is not one.

    Accepts what a mentor might actually type or what YAML might hand us: lower case, a
    dash or spaces, an int from an unquoted all-digit value. O becomes 0; I and L become 1.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        value = str(value)
    if not isinstance(value, str):
        return None
    cleaned = value.strip().upper().replace('-', '').replace(' ', '')
    cleaned = cleaned.replace('O', '0').replace('I', '1').replace('L', '1')
    if len(cleaned) != CODE_LENGTH:
        return None
    if any(ch not in ALPHABET for ch in cleaned):
        return None
    return cleaned


def format_code(code):
    """Display form: two groups of four separated by a dash."""
    return f"{code[:4]}-{code[4:]}"


# The job id the daemon bakes into the uploaded file name, e.g. the '-j8f3a2c1' in
# 'sample_part-j8f3a2c1.gcode.3mf'. Anchored on the extension so only a real suffix matches.
# new_job_id() draws eight hex digits; the seven-digit 'j8f3a2c1' of the spec's examples is
# accepted too, so a name written by hand from the documentation still reads back cleanly.
_JOB_SUFFIX_RE = re.compile(r'-j[0-9a-fA-F]{7,8}(?=\.gcode\.3mf$)')


def strip_job_suffix(name):
    """The printer's reported file name with the job id taken back out.

    The daemon uploads 'sample_part-j8f3a2c1.gcode.3mf' so the printer's own display names
    the job; a student reading PenguinCAM should see 'sample_part.gcode.3mf'. Any other
    name -- one printed from an SD card, say -- is left exactly as the printer reported it."""
    if not isinstance(name, str):
        return name
    return _JOB_SUFFIX_RE.sub('', name)


def new_job_id():
    """A short opaque job id, e.g. 'j8f3a2c1'. It is also baked into the file name the
    daemon uploads, so the printer's reported name identifies the job."""
    return 'j' + secrets.token_hex(4)


def _serializer(secret_key):
    return URLSafeSerializer(secret_key, salt=TOKEN_SALT)


def issue_token(code, secret_key, *, issued_at=None):
    """Sign a daemon credential carrying the code and the issue time.

    No expiry: retiring a Pi means taking the code out of the team's YAML, after which no
    fresh session names it. The issue time is for logging only and is never compared with
    any clock."""
    payload = {'code': code, 'iat': float(issued_at if issued_at is not None else time.time())}
    return _serializer(secret_key).dumps(payload)


def read_token(token, secret_key):
    """(code, issued_at) for a correctly signed token, else None. Never raises."""
    if not token or not isinstance(token, str):
        return None
    try:
        payload = _serializer(secret_key).loads(token)
    except Exception:  # a bad signature, a bad payload -- any decode failure is just "no"
        return None
    if not isinstance(payload, dict):
        return None
    code = normalize_code(payload.get('code'))
    if code is None:
        return None
    try:
        issued_at = float(payload.get('iat', 0.0))
    except (TypeError, ValueError):
        issued_at = 0.0
    return code, issued_at


class _Record:
    """One printer, keyed by its normalised pairing code. Plain attributes; every access
    happens under PrinterRelay's single lock."""

    __slots__ = ('session_seen_at', 'daemon_seen_at', 'status', 'status_received_at',
                 'job', 'last_result', 'last_result_at', 'touched_at')

    def __init__(self, now):
        self.session_seen_at = None
        self.daemon_seen_at = None
        self.status = None
        self.status_received_at = None
        self.job = None                 # {'id','download_path','filename','created_at',
                                        #  'state','team_number','handed_out_at'}
        self.last_result = None         # {'ok': bool, 'reason': str | None,
                                        #  'job_id': str, 'team_number': int | None}
        self.last_result_at = None
        self.touched_at = now


class PrinterRelay:
    """Every printer record the process knows about, behind one lock.

    Expiry is lazy: there is no thread. Each read applies the job deadlines, drops a
    day-old snapshot and deletes a record nothing has touched for a day, all against the
    injected clock -- which is what makes the deadlines testable without sleeping.
    """

    def __init__(self, clock=time.time):
        self._clock = clock
        self._lock = threading.RLock()
        self._records = {}
        self._expiry_events = deque()

    # -- housekeeping -----------------------------------------------------------------

    def _touch(self, record):
        record.touched_at = self._clock()

    def _is_fresh(self, record, now):
        """Is this record's snapshot inside the two-minute window? Caller holds the lock.

        The single definition of "online": status_for, ping and _refusal all read it, so the
        status line and a send refusal can never disagree about the same instant. The
        comparison is on the raw float -- truncating the age to whole seconds first would
        call a 120.5 second old snapshot fresh in one place and stale in another."""
        if record.status_received_at is None:
            return False
        return now - record.status_received_at <= STATUS_ONLINE_SECONDS

    def _expire(self, code, record):
        """Apply every lazy deadline to one record. Caller holds the lock.

        Returns False when the record itself has expired and should be dropped."""
        now = self._clock()
        if now - record.touched_at > RECORD_TTL_SECONDS:
            return False
        if record.status_received_at is not None and now - record.status_received_at > STATUS_TTL_SECONDS:
            record.status = None
            record.status_received_at = None
        job = record.job
        if job is not None:
            if job['state'] == 'pending' and now - job['created_at'] > JOB_PENDING_TIMEOUT_SECONDS:
                self._finish(code, record, job, ok=False,
                             reason='printer did not pick up the job', record_event=True)
            elif (job['state'] == 'handed_out'
                  and now - job['handed_out_at'] > JOB_HANDED_OUT_TIMEOUT_SECONDS):
                self._finish(code, record, job, ok=False,
                             reason='printer did not confirm the job', record_event=True)
        if (record.last_result_at is not None
                and now - record.last_result_at > LAST_RESULT_TTL_SECONDS):
            record.last_result = None
            record.last_result_at = None
        return True

    def _finish(self, code, record, job, *, ok, reason, record_event=False):
        """Record a job's outcome and clear it. Caller holds the lock.

        `record_event` is set only by the lazy deadlines: a job that dies on a deadline is
        never acknowledged, so the relay raises the metrics event itself. A real
        acknowledgement is logged by the ack route instead, so setting it here too would
        double-count."""
        record.job = None
        record.last_result = {'ok': ok, 'reason': reason, 'job_id': job['id'],
                              'team_number': job.get('team_number')}
        record.last_result_at = self._clock()
        if record_event:
            self._expiry_events.append(
                ExpiryEvent(code, job['id'], job.get('team_number'), reason))

    def _get(self, code, create=False):
        """The live record for a code, expiring it first. Caller holds the lock."""
        if code is None:
            return None
        record = self._records.get(code)
        if record is not None and not self._expire(code, record):
            del self._records[code]
            record = None
        if record is None and create:
            record = _Record(self._clock())
            self._records[code] = record
        return record

    def drain_expiry_events(self):
        """Take and clear the expiry events recorded since the last call. The routes log a
        printer_ack metrics event for each, so a job that died on a deadline still shows up
        in the metrics."""
        with self._lock:
            events = list(self._expiry_events)
            self._expiry_events.clear()
        return events

    # -- how a code becomes known ------------------------------------------------------

    def note_code(self, code):
        """An Onshape session has presented this pairing code. The only way a code becomes
        known to the relay. Returns the normalised code, or None if it was not one."""
        code = normalize_code(code)
        if code is None:
            return None
        with self._lock:
            record = self._get(code, create=True)
            record.session_seen_at = self._clock()
            self._touch(record)
        return code

    def seed_code(self, code):
        """Development only: pretend a session presented this code, so a daemon can pair
        against a server nobody has opened in Onshape. Wired to PRINTER_DEV_PAIRING_CODE,
        which printer_routes reads ONLY in development mode."""
        return self.note_code(code)

    def note_daemon(self, code):
        """A request carrying a valid token for this code arrived. Creates the record if
        the relay restarted since the session last presented the code.

        Returns the normalised code, or None if it was not one -- a malformed code makes no
        record and raises nothing."""
        code = normalize_code(code)
        if code is None:
            return None
        with self._lock:
            record = self._get(code, create=True)
            record.daemon_seen_at = self._clock()
            self._touch(record)
        return code

    def begin_exchange(self, code):
        """The pairing exchange, atomically: True (and the caller mints a token) only when
        a session has presented this code and no daemon has been heard for two minutes.

        Marking daemon_seen_at here is what refuses a SECOND Pi carrying a copied config
        file while the first one is alive."""
        code = normalize_code(code)
        if code is None:
            return False
        with self._lock:
            record = self._get(code)
            if record is None or record.session_seen_at is None:
                return False
            now = self._clock()
            if (record.daemon_seen_at is not None
                    and now - record.daemon_seen_at <= DAEMON_ONLINE_SECONDS):
                return False
            record.daemon_seen_at = now
            self._touch(record)
            return True

    # -- reads -------------------------------------------------------------------------

    UNPAIRED = {'paired': False, 'waiting': False, 'online': False, 'age_seconds': None,
                'status': None, 'job': None, 'last_result': None, 'code': None}

    def status_for(self, code):
        """Everything the wizard's status line needs, computed from the relay's own clock.

        The snapshot carries no timestamp: `age_seconds` is measured from when the sync
        arrived here, so a Pi with a wrong clock changes nothing."""
        code = normalize_code(code)
        if code is None:
            return dict(self.UNPAIRED)
        with self._lock:
            record = self._get(code)
            if record is None:
                return dict(self.UNPAIRED)
            now = self._clock()
            age = None if record.status_received_at is None else int(now - record.status_received_at)
            last_result = None
            if record.last_result is not None:
                last_result = {'ok': record.last_result['ok'],
                               'reason': record.last_result['reason'],
                               'age_seconds': int(now - record.last_result_at)}
            job = None
            if record.job is not None:
                job = {'id': record.job['id'], 'state': record.job['state'],
                       'filename': record.job['filename'],
                       'display_name': strip_job_suffix(record.job['filename'])}
            # The raw name stays, so a caller can still match the job id in it; display_name
            # is what the wizard shows.
            status = copy.deepcopy(record.status) if record.status else None
            if status is not None:
                status['display_name'] = strip_job_suffix(status.get('file') or '')
            return {
                'paired': True,
                'waiting': record.daemon_seen_at is None,
                'online': self._is_fresh(record, now),
                'age_seconds': age,
                'status': status,
                'job': job,
                'last_result': last_result,
                'code': format_code(code),
            }

    # -- writes ------------------------------------------------------------------------

    def _refusal(self, record):
        """Why a send cannot go ahead right now, or None. Caller holds the lock.

        The text is what the wizard shows, so it reads as a sentence to a student."""
        now = self._clock()
        if record.daemon_seen_at is None:
            return ('The Pi has not reported since the service started; '
                    'check the setup guide.')
        if record.status_received_at is None:
            return 'No printer reporting yet; try again in a moment.'
        if not self._is_fresh(record, now):
            age = now - record.status_received_at
            return f'Printer offline, last seen {int(age // 60)} min ago'
        state = (record.status or {}).get('state')
        if state == 'running':
            percent = (record.status or {}).get('percent')
            name = strip_job_suffix((record.status or {}).get('file')) or 'a file'
            return f'Printer busy, printing {name} at {percent} percent'
        if state == 'paused':
            paused_name = strip_job_suffix((record.status or {}).get('file')) or 'a file'
            return f'Paused, {paused_name}'
        if state == 'unreachable':
            return 'Printer unreachable from the Pi'
        if state == 'error':
            return f"Printer error: {(record.status or {}).get('error') or 'unknown'}"
        if record.job is not None:
            return 'A job is already on its way to the printer'
        return None

    def claim_job(self, code, *, job_id, download_path, filename, team_number):
        """Store a pending job, or refuse with a reason.

        The whole check-and-store is one lock acquisition, so two Previews pressing Send at
        the same instant cannot both win. Returns (ok, reason, job)."""
        code = normalize_code(code)
        with self._lock:
            record = self._get(code)
            if record is None:
                return False, 'No printer paired', None
            refusal = self._refusal(record)
            if refusal is not None:
                return False, refusal, None
            job = {'id': job_id, 'download_path': download_path, 'filename': filename,
                   'created_at': self._clock(), 'state': 'pending',
                   'team_number': team_number, 'handed_out_at': None}
            record.job = job
            self._touch(record)
            return True, None, dict(job)

    def store_status(self, code, snapshot):
        """Store a sync's snapshot and hand out the job, if any.

        Returns the job the daemon should act on -- a pending job promoted to handed_out,
        or the handed_out job again for a daemon that restarted mid-job -- else None."""
        code = normalize_code(code)
        if code is None:
            return None
        with self._lock:
            record = self._get(code, create=True)
            now = self._clock()
            record.status = dict(snapshot) if isinstance(snapshot, dict) else None
            record.status_received_at = now
            self._touch(record)
            # The printer has the job whatever the acknowledgement said.
            file_name = (record.status or {}).get('file') or ''
            if (record.last_result is not None
                    and record.last_result.get('job_id')
                    and record.last_result['job_id'] in file_name):
                record.last_result = None
                record.last_result_at = None
            job = record.job
            if job is None:
                return None
            if job['state'] == 'pending':
                job['state'] = 'handed_out'
                job['handed_out_at'] = now
            return dict(job)

    def finish_job(self, code, job_id, *, ok, reason):
        """Record an acknowledgement. Returns (found, team_number); found False means the
        daemon acked a job this relay no longer holds, which the daemon treats as final."""
        code = normalize_code(code)
        if code is None:
            return False, None
        with self._lock:
            record = self._get(code)
            if record is None or record.job is None or record.job['id'] != job_id:
                return False, None
            job = record.job
            team_number = job.get('team_number')
            self._finish(code, record, job, ok=bool(ok), reason=reason)
            self._touch(record)
            return True, team_number

    def ping(self, code):
        """Read-only health answer for `penguincam-printer status`.

        Stores nothing -- deliberately NOT recording daemon_seen_at, so a mentor running
        status while the service is down cannot block the service's next exchange."""
        code = normalize_code(code)
        with self._lock:
            record = self._get(code)
            if record is None:
                return {'ok': True, 'waiting': True, 'online': False}
            return {'ok': True,
                    'waiting': record.daemon_seen_at is None,
                    'online': self._is_fresh(record, self._clock())}


# The process-wide singleton. printer_routes uses it through the module attribute (so tests
# can swap it), and frc_cam_gui_app calls note_code() below from
# _load_team_config_into_session.
RELAY = PrinterRelay()


def note_code(code):
    """Tell the relay a session has presented this pairing code. Called from
    _load_team_config_into_session on login, on the ten-minute TTL refresh and on the
    header's refresh link -- so every way a YAML reaches a session also tells the relay its
    code, and pairing needs no wizard step."""
    return RELAY.note_code(code)

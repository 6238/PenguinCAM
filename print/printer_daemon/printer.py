"""The only module that touches bambulabs_api.

Two jobs. `map_status` turns the printer's own report document into the six-word vocabulary
the relay and the wizard use -- a pure function of a dictionary, so a captured fixture pins
it. `PrinterLink` is the thin shim over the library: connect, read, upload, start.

The library is imported lazily, inside connect(), so this module imports (and its tests
run) on a machine that does not have it -- which is what keeps `make test` honest when
bambulabs_api is absent.
"""

import os
import re
import socket

# The only state names that leave this module.
IDLE = 'idle'
RUNNING = 'running'
PAUSED = 'paused'
FINISHED = 'finished'
ERROR = 'error'
UNREACHABLE = 'unreachable'

# bambulabs_api's GcodeState, which reads the printer's `gcode_state` field. PREPARE is a
# real print in its first seconds, so it maps to running: a status line that said "idle"
# right after a send would read as a failed send.
GCODE_STATE_MAP = {
    'IDLE': IDLE,
    'PREPARE': RUNNING,
    'RUNNING': RUNNING,
    'PAUSE': PAUSED,
    'FINISH': FINISHED,
    'FAILED': ERROR,
    'UNKNOWN': IDLE,
}

# bambulabs_api's PrintStatus (the `stg_cur` field). 255 is IDLE and 0 is PRINTING; every
# PAUSED_* member means the printer is waiting for a human.
STG_IDLE = 255
STG_PAUSED = frozenset({5, 6, 16, 17, 20, 21, 23, 26, 27, 28, 30, 32, 33, 34, 35})

# The compound extensions a sliced file can carry, longest first.
_EXTENSIONS = ('.gcode.3mf', '.3mf', '.gcode')

#: The plate inside the archive to print. PenguinCAM slices one plate and Orca numbers it
#: 1, which print/slicer.py reads back as Metadata/plate_1.gcode. Named rather than written
#: into the start call, so print/tests/test_slice_daemon_contract.py can pin the two
#: together: the daemon never imports the backend, so a test is the only place they meet.
PLATE_NUMBER = 1

# A job id is 'j' + hex: the relay builds it as "j" + secrets.token_hex(4), and a leading
# zero can be dropped, so 7 or 8 hex digits -- the same rule the relay applies. Deliberately
# tight: a wider pattern would strip suffixes this daemon never wrote, turning a user's
# `part-jaded.gcode.3mf` into `part.gcode.3mf`.
_JOB_ID_RE = re.compile(r'^j[0-9a-f]{7,8}$')

# An HMS entry's severity lives in the high 16 bits of its `code` word: 1 fatal, 2 serious,
# 3 common, 4 info (OpenBambuAPI's hms.md, which is also how Bambu Studio colours the entry
# -- bambulabs_api 2.6.6 exposes no HMS accessor at all, so there is no library constant to
# use). Only fatal and serious stop a print; an info entry can sit in the list through a
# perfectly healthy job. PROVISIONAL with the rest of the mapping until the Task 11 capture.
HMS_SEVERITY_STOPS = frozenset({1, 2})

# SSDP: Bambu printers announce themselves to this group and port. Binding the port is not
# enough on Linux -- without joining the group the kernel drops every datagram.
DISCOVERY_PORT = 2021
DISCOVERY_GROUP = '239.255.255.250'


def format_hms(attr, code):
    """The printer's HMS error in the form the screen and the wiki use.

    attr and code are two 32-bit integers; each becomes two four-hex-digit groups."""
    attr, code = int(attr), int(code)
    return (f"HMS_{(attr >> 16) & 0xFFFF:04X}_{attr & 0xFFFF:04X}"
            f"_{(code >> 16) & 0xFFFF:04X}_{code & 0xFFFF:04X}")


def suffixed_name(filename, job_id):
    """`sample_part.gcode.3mf` + `j8f3a2c1` -> `sample_part-j8f3a2c1.gcode.3mf`.

    The job id is in the name the printer reports back, which is how the daemon tells its
    own job from the previous one and how a restarted daemon recognises a job already
    running. The wizard strips the suffix for display."""
    for ext in _EXTENSIONS:
        if filename.endswith(ext):
            return f"{filename[:-len(ext)]}-{job_id}{ext}"
    stem, ext = os.path.splitext(filename)
    return f"{stem}-{job_id}{ext}"


def strip_job_suffix(filename):
    """Undo suffixed_name for display. A name this daemon did not write is left alone."""
    for ext in _EXTENSIONS:
        if filename.endswith(ext):
            stem = filename[:-len(ext)]
            head, dash, tail = stem.rpartition('-')
            if dash and _JOB_ID_RE.match(tail):
                return f"{head}{ext}"
            return filename
    return filename


def _as_int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _hms_severity(entry):
    """The severity number in the high 16 bits of an HMS entry's code word."""
    return ((_as_int(entry.get('code'), 0) or 0) >> 16) & 0xFFFF


def _worst_hms(entries):
    """The one entry to put on the status line.

    A printer lists its HMS entries in its own order, not in severity order, so a fatal
    fault can sit behind an informational one. A stopping entry outranks an entry that only
    reports something, and below that the lower severity number wins (1 fatal is worse than
    4 info). min() keeps the first of equals, which is the order the printer sent."""
    return min(entries, key=lambda entry: (_hms_severity(entry) not in HMS_SEVERITY_STOPS,
                                           _hms_severity(entry)))


def map_status(report, *, printer=None):
    """Turn one `mqtt_dump()['print']` document into the snapshot the relay stores.

    An empty document means no report has arrived over MQTT, which from the wizard's point
    of view is the printer being unreachable from the Pi."""
    identity = printer or {'model': None, 'name': None, 'serial': None}
    if not report:
        return {'state': UNREACHABLE, 'file': '', 'percent': None, 'remaining_min': None,
                'layer': 0, 'layers': 0, 'error': None, 'printer': identity}

    state = GCODE_STATE_MAP.get(str(report.get('gcode_state', '')).upper(), IDLE)
    stg_cur = _as_int(report.get('stg_cur'), STG_IDLE)

    # The printer reports gcode_state IDLE with a preparation stage for the first seconds
    # after a start command; treat any non-idle, non-paused stage as running.
    if state == IDLE and stg_cur not in (STG_IDLE, None) and stg_cur not in STG_PAUSED:
        state = RUNNING
    if state in (IDLE, RUNNING) and stg_cur in STG_PAUSED:
        state = PAUSED

    # An HMS entry is reported as information; only some of them mean the print stopped.
    # Every entry counts, not just the first: one fatal entry anywhere in the list stops the
    # print, and the most severe entry is the one the status line names.
    error = None
    stopped = state == ERROR                      # gcode_state FAILED
    entries = [h for h in report.get('hms') or [] if isinstance(h, dict)]
    print_error = _as_int(report.get('print_error'), 0) or 0
    if entries:
        worst = _worst_hms(entries)
        error = format_hms(_as_int(worst.get('attr'), 0) or 0,
                           _as_int(worst.get('code'), 0) or 0)
        if any(_hms_severity(entry) in HMS_SEVERITY_STOPS for entry in entries):
            stopped = True
    elif print_error:
        error = f"PRINT_ERROR_{print_error & 0xFFFFFFFF:08X}"
    if print_error:
        stopped = True
    if stopped:
        state = ERROR

    return {
        'state': state,
        'file': report.get('gcode_file') or report.get('subtask_name') or '',
        # mc_percent is a whole percent; mc_remaining_time is MINUTES (the library's
        # docstring says seconds, but every Bambu printer documented by OpenBambuAPI
        # reports minutes here). Verification task 1 settles it against the real H2S.
        'percent': _as_int(report.get('mc_percent')),
        'remaining_min': _as_int(report.get('mc_remaining_time')),
        'layer': _as_int(report.get('layer_num'), 0),
        'layers': _as_int(report.get('total_layer_num'), 0),
        'error': error,
        'printer': identity,
    }


class PrinterLink:
    """One Bambu printer on the LAN.

    `printer_factory` exists so the tests can substitute a fake for bambulabs_api.Printer
    without the library being installed."""

    def __init__(self, ip, serial, access_code, *, model=None, name=None,
                 printer_factory=None):
        self.ip = ip
        self.serial = serial
        self.access_code = access_code
        self.model = model
        self.name = name
        self._factory = printer_factory
        self._printer = None

    def _make(self):
        if self._factory is not None:
            return self._factory(self.ip, self.access_code, self.serial)
        # Imported here, not at module scope: printer.py must import on a machine without
        # the library so the rest of the daemon's tests still run.
        import bambulabs_api
        return bambulabs_api.Printer(self.ip, self.access_code, self.serial)

    def connect(self):
        """Open the MQTT subscription. Deliberately NOT Printer.connect(), which also
        starts the camera: the daemon never needs camera frames, and decoding them through
        Pillow is the one avoidable memory cost on a small Pi."""
        self._printer = self._make()
        self._printer.mqtt_start()
        return self._printer

    def disconnect(self):
        if self._printer is not None:
            try:
                self._printer.mqtt_stop()
            except Exception:
                pass
            self._printer = None

    def _require_printer(self):
        if self._printer is None:
            raise RuntimeError('not connected')
        return self._printer

    @property
    def identity(self):
        return {'model': self.model, 'name': self.name, 'serial': self.serial}

    def raw_report(self):
        """The printer's own report document, or {} when nothing has arrived. Never raises:
        a library that throws must look like an unreachable printer, not crash the daemon."""
        if self._printer is None:
            return {}
        try:
            if not self._printer.mqtt_client_connected():
                return {}
            return self._printer.mqtt_dump().get('print') or {}
        except Exception:
            return {}

    def snapshot(self):
        """The status snapshot the daemon posts in every sync."""
        return map_status(self.raw_report(), printer=self.identity)

    def upload(self, local_path, remote_name):
        """Send the sliced file to the printer over FTPS under the job-suffixed name.

        bambulabs_api closes the file object itself in its finally block, so this does not
        use a `with` -- but it can raise before reaching that block, hence the except."""
        printer = self._require_printer()
        handle = open(local_path, 'rb')
        try:
            return printer.upload_file(handle, remote_name)
        except Exception:
            handle.close()
            raise

    def start(self, remote_name):
        """Start the uploaded file. Plate 1 is what the slicer writes.

        use_ams is False: the slice is single-material, and an AMS mapping the team has not
        asked for would be a surprising default. Revisit with the owner if the shop prints
        from an AMS.

        That choice puts the EXTERNAL SPOOL in charge of the material, and the printer
        checks the spool it has against the filament the slice asks for. The fixed profile
        set slices Generic PETG for a 0.6 mm High Flow nozzle on the Textured PEI Plate
        (print/profiles/, docs/3D_PRINTING.md), so an external spool the printer still
        believes is PLA makes the start fail or the print come out wrong. Nothing here can
        detect that: the daemon reports whatever the printer reports.

        Nothing in this call names the plate or the material. Both travel inside the 3MF,
        which is why a profile change upstream needs no change here."""
        return self._require_printer().start_print(
            remote_name, PLATE_NUMBER, use_ams=False, ams_mapping=[0],
            flow_calibration=True)


def discover(timeout=5.0, *, sock_factory=None):
    """Listen for Bambu printers announcing themselves on UDP 2021.

    Returns a list of {'serial', 'model', 'name', 'ip'}. School networks often block
    broadcasts, so `pair` treats an empty list as normal and asks for an IP address.

    PROVISIONAL: the exact SSDP header names are verification task 3. The parser below
    accepts any `Key: value` header line and reads the ones Bambu is documented to send, so
    an unexpected name shows up as a missing field rather than a crash."""
    import time as _time

    if sock_factory is not None:
        sock = sock_factory()
    else:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(('', DISCOVERY_PORT))
    # Join the SSDP group on every interface. A network (or a container) that refuses
    # membership is not fatal: some setups deliver the announcements to the plain bound
    # port anyway, so fall through and listen rather than give up.
    try:
        mreq = socket.inet_aton(DISCOVERY_GROUP) + socket.inet_aton('0.0.0.0')
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    except OSError:
        pass
    sock.settimeout(1.0)

    found = {}
    deadline = _time.monotonic() + timeout
    try:
        while _time.monotonic() < deadline:
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                # A real socket error (the interface went away, the socket was closed)
                # will not fix itself; spinning on it until the deadline helps nobody.
                break
            headers = {}
            for line in data.decode('utf-8', 'replace').splitlines():
                key, sep, value = line.partition(':')
                if sep:
                    headers[key.strip().lower()] = value.strip()
            serial = headers.get('usn') or headers.get('devserial.bambu.com')
            if not serial:
                continue
            found[serial] = {
                'serial': serial,
                'model': headers.get('devmodel.bambu.com', ''),
                'name': headers.get('devname.bambu.com', ''),
                'ip': addr[0],
            }
    finally:
        try:
            sock.close()
        except Exception:
            pass
    return list(found.values())

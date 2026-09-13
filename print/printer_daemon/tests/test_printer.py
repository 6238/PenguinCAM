"""The printer wrapper: the state mapping, the job-suffixed file name, and the thin shim
over bambulabs_api with the library replaced by a fake.

The library is never imported here -- printer.py imports it lazily inside connect() -- so
this suite passes whether or not bambulabs_api is installed."""
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import unittest

from printer_daemon import printer as pr

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures')

with open(os.path.join(FIXTURES, 'h2s_status.json')) as _fh:
    H2S = json.load(_fh)


class TestStateMapping(unittest.TestCase):
    """Pinned by the captured fixture. The relay and the wizard only ever see these six
    state names, whatever the printer's own vocabulary is."""

    def test_idle(self):
        self.assertEqual(pr.map_status(H2S['idle'])['state'], pr.IDLE)

    def test_preparing_counts_as_running(self):
        self.assertEqual(pr.map_status(H2S['preparing'])['state'], pr.RUNNING)

    def test_heating_counts_as_running_even_while_gcode_state_says_idle(self):
        """The printer reports IDLE with a preparation stage for the first few seconds
        after a start command. Showing 'Printer idle' there would read as a failed send."""
        self.assertEqual(pr.map_status(H2S['heating_while_idle_state'])['state'], pr.RUNNING)

    def test_running(self):
        snap = pr.map_status(H2S['running'])
        self.assertEqual(snap['state'], pr.RUNNING)
        self.assertEqual(snap['percent'], 42)
        self.assertEqual(snap['remaining_min'], 31)
        self.assertEqual(snap['layer'], 87)
        self.assertEqual(snap['layers'], 210)
        self.assertEqual(snap['file'], 'sample_part-j8f3a2c1.gcode.3mf')
        self.assertIsNone(snap['error'])

    def test_paused(self):
        self.assertEqual(pr.map_status(H2S['paused'])['state'], pr.PAUSED)

    def test_finished(self):
        self.assertEqual(pr.map_status(H2S['finished'])['state'], pr.FINISHED)

    def test_failed_reports_the_hms_code(self):
        snap = pr.map_status(H2S['failed_with_hms'])
        self.assertEqual(snap['state'], pr.ERROR)
        self.assertEqual(snap['error'], 'HMS_0300_0100_0001_0004')

    def test_a_print_error_without_hms_still_reports_something(self):
        report = dict(H2S['running'], print_error=50336003, hms=[])
        snap = pr.map_status(report)
        self.assertEqual(snap['state'], pr.ERROR)
        self.assertEqual(snap['error'], 'PRINT_ERROR_03001103')

    def test_an_unknown_gcode_state_falls_back_to_idle(self):
        snap = pr.map_status({'gcode_state': 'SOMETHING_NEW', 'stg_cur': 255})
        self.assertEqual(snap['state'], pr.IDLE)

    def test_an_empty_report_is_unreachable(self):
        self.assertEqual(pr.map_status({})['state'], pr.UNREACHABLE)
        self.assertEqual(pr.map_status(None)['state'], pr.UNREACHABLE)

    SNAPSHOT_KEYS = {'state', 'file', 'percent', 'remaining_min', 'layer', 'layers',
                     'error', 'printer'}

    def test_the_snapshot_has_exactly_the_keys_the_relay_expects(self):
        self.assertEqual(set(pr.map_status(H2S['running'])), self.SNAPSHOT_KEYS)

    def test_the_unreachable_snapshot_has_the_same_keys(self):
        """The relay reads the same fields whatever the state, so the empty report must
        not return a shorter dictionary."""
        self.assertEqual(set(pr.map_status({})), self.SNAPSHOT_KEYS)


class TestHmsSeverity(unittest.TestCase):
    """An HMS entry is information. Only a fatal or serious one means the print stopped:
    an info entry can sit in the list through a perfectly healthy job, and a filament
    runout is a pause the operator can clear, not a failure."""

    INFO = 0x00040001        # severity 4, info
    COMMON = 0x00030001      # severity 3, common -- e.g. the filament runout pause
    SERIOUS = 0x00020001     # severity 2, serious
    FATAL = 0x00010004       # severity 1, fatal

    def test_an_info_entry_leaves_a_running_print_running(self):
        snap = pr.map_status(dict(H2S['running'], hms=[{'attr': 50331904, 'code': self.INFO}]))
        self.assertEqual(snap['state'], pr.RUNNING)

    def test_an_info_entry_is_still_reported_as_information(self):
        snap = pr.map_status(dict(H2S['running'], hms=[{'attr': 50331904, 'code': self.INFO}]))
        self.assertEqual(snap['error'], 'HMS_0300_0100_0004_0001')

    def test_a_pause_with_an_entry_is_still_paused(self):
        snap = pr.map_status(dict(H2S['paused'], hms=[{'attr': 50331904, 'code': self.COMMON}]))
        self.assertEqual(snap['state'], pr.PAUSED)
        self.assertEqual(snap['error'], 'HMS_0300_0100_0003_0001')

    def test_a_fatal_entry_stops_a_running_print(self):
        snap = pr.map_status(dict(H2S['running'], hms=[{'attr': 50331904, 'code': self.FATAL}]))
        self.assertEqual(snap['state'], pr.ERROR)

    def test_a_failed_state_is_an_error_whatever_the_entry_says(self):
        snap = pr.map_status(dict(H2S['failed_with_hms'],
                                  hms=[{'attr': 50331904, 'code': self.INFO}]))
        self.assertEqual(snap['state'], pr.ERROR)

    def test_a_fatal_entry_behind_an_info_entry_still_stops_the_print(self):
        """The printer lists its entries in its own order, not in severity order. Reading
        only the first one would report the info code and leave the state running."""
        snap = pr.map_status(dict(H2S['running'],
                                  hms=[{'attr': 50331904, 'code': self.INFO},
                                       {'attr': 50331904, 'code': self.FATAL}]))
        self.assertEqual(snap['state'], pr.ERROR)
        self.assertEqual(snap['error'], 'HMS_0300_0100_0001_0004')

    def test_a_fatal_entry_ahead_of_an_info_entry_is_the_one_reported(self):
        snap = pr.map_status(dict(H2S['running'],
                                  hms=[{'attr': 50331904, 'code': self.FATAL},
                                       {'attr': 50331904, 'code': self.INFO}]))
        self.assertEqual(snap['state'], pr.ERROR)
        self.assertEqual(snap['error'], 'HMS_0300_0100_0001_0004')

    def test_a_serious_entry_stops_the_print_too(self):
        snap = pr.map_status(dict(H2S['running'],
                                  hms=[{'attr': 50331904, 'code': self.INFO},
                                       {'attr': 50331904, 'code': self.SERIOUS}]))
        self.assertEqual(snap['state'], pr.ERROR)
        self.assertEqual(snap['error'], 'HMS_0300_0100_0002_0001')

    def test_the_first_of_two_equally_severe_entries_is_reported(self):
        snap = pr.map_status(dict(H2S['running'],
                                  hms=[{'attr': 50331904, 'code': self.INFO},
                                       {'attr': 50331905, 'code': self.INFO}]))
        self.assertEqual(snap['state'], pr.RUNNING)
        self.assertEqual(snap['error'], 'HMS_0300_0100_0004_0001')

    def test_several_informational_entries_leave_the_print_running(self):
        snap = pr.map_status(dict(H2S['running'],
                                  hms=[{'attr': 50331904, 'code': self.COMMON},
                                       {'attr': 50331904, 'code': self.INFO}]))
        self.assertEqual(snap['state'], pr.RUNNING)
        self.assertEqual(snap['error'], 'HMS_0300_0100_0003_0001')

    def test_an_entry_with_no_code_never_outranks_a_real_fault(self):
        """A malformed entry reads as severity zero. It must not push the fatal entry off
        the status line just by sorting lower."""
        snap = pr.map_status(dict(H2S['running'],
                                  hms=[{}, {'attr': 50331904, 'code': self.FATAL}]))
        self.assertEqual(snap['state'], pr.ERROR)
        self.assertEqual(snap['error'], 'HMS_0300_0100_0001_0004')


class TestHmsFormatting(unittest.TestCase):
    def test_attr_and_code_become_the_four_group_form(self):
        self.assertEqual(pr.format_hms(50331904, 65540), 'HMS_0300_0100_0001_0004')


class TestFileNames(unittest.TestCase):
    def test_the_job_id_goes_before_the_compound_extension(self):
        self.assertEqual(pr.suffixed_name('sample_part.gcode.3mf', 'j8f3a2c1'),
                         'sample_part-j8f3a2c1.gcode.3mf')

    def test_a_plain_3mf_works_too(self):
        self.assertEqual(pr.suffixed_name('part.3mf', 'j1'), 'part-j1.3mf')

    def test_an_unknown_extension_still_gets_the_suffix(self):
        self.assertEqual(pr.suffixed_name('part.bin', 'j1'), 'part-j1.bin')

    def test_stripping_the_suffix_round_trips(self):
        name = pr.suffixed_name('sample_part.gcode.3mf', 'j8f3a2c1')
        self.assertEqual(pr.strip_job_suffix(name), 'sample_part.gcode.3mf')

    def test_a_real_generated_job_id_round_trips(self):
        """The relay builds the id as 'j' + secrets.token_hex(4), so the stripper has to
        recognise what it will really be handed, not just the example in the spec."""
        for _ in range(200):
            job_id = 'j' + secrets.token_hex(4)
            name = pr.suffixed_name('sample_part.gcode.3mf', job_id)
            self.assertEqual(pr.strip_job_suffix(name), 'sample_part.gcode.3mf')

    def test_stripping_leaves_a_file_the_daemon_did_not_name(self):
        self.assertEqual(pr.strip_job_suffix('someone-else.gcode.3mf'),
                         'someone-else.gcode.3mf')

    def test_a_word_that_merely_looks_like_hex_is_not_a_job_id(self):
        """'jaded' is j plus four hex letters. A loose pattern would eat it and rename
        somebody's file."""
        self.assertEqual(pr.strip_job_suffix('part-jaded.gcode.3mf'),
                         'part-jaded.gcode.3mf')
        self.assertEqual(pr.strip_job_suffix('part-jbeadedface.gcode.3mf'),
                         'part-jbeadedface.gcode.3mf')


class FakeBambuPrinter:
    """Stands in for bambulabs_api.Printer."""

    def __init__(self, ip_address, access_code, serial):
        self.ip_address, self.access_code, self.serial = ip_address, access_code, serial
        self.report = {}
        self.started = None
        self.uploaded = None
        self.mqtt_running = False
        self.connected = True

    def mqtt_start(self):
        self.mqtt_running = True

    def mqtt_stop(self):
        self.mqtt_running = False

    def camera_start(self):          # must never be called: Pillow frames on a small Pi
        raise AssertionError('the daemon must not start the camera')

    def mqtt_client_connected(self):
        return self.connected

    def mqtt_dump(self):
        return {'print': self.report}

    def upload_file(self, file_obj, filename):
        self.uploaded = (filename, file_obj.read())
        file_obj.close()
        return filename

    def start_print(self, filename, plate_number, use_ams=True, ams_mapping=(0,),
                    skip_objects=None, flow_calibration=True):
        self.started = {'filename': filename, 'plate_number': plate_number,
                        'use_ams': use_ams}
        return True


class TestPrinterLink(unittest.TestCase):
    def link(self):
        return pr.PrinterLink('10.0.0.42', '01P00A', '12345678',
                              model='H2S', name='Shop H2S',
                              printer_factory=FakeBambuPrinter)

    def test_connect_starts_mqtt_only(self):
        link = self.link()
        link.connect()
        self.assertTrue(link._printer.mqtt_running)      # and camera_start would have raised

    def test_snapshot_before_any_report_is_unreachable(self):
        link = self.link()
        link.connect()
        self.assertEqual(link.snapshot()['state'], pr.UNREACHABLE)

    def test_snapshot_carries_the_printer_identity(self):
        link = self.link()
        link.connect()
        link._printer.report = H2S['running']
        snap = link.snapshot()
        self.assertEqual(snap['printer'],
                         {'model': 'H2S', 'name': 'Shop H2S', 'serial': '01P00A'})

    def test_a_disconnected_client_reports_unreachable(self):
        link = self.link()
        link.connect()
        link._printer.report = H2S['running']
        link._printer.connected = False
        self.assertEqual(link.snapshot()['state'], pr.UNREACHABLE)

    def test_upload_sends_the_bytes_under_the_suffixed_name(self):
        link = self.link()
        link.connect()
        with tempfile.NamedTemporaryFile(suffix='.3mf', delete=False) as fh:
            fh.write(b'3mf-bytes')
            path = fh.name
        try:
            link.upload(path, 'sample_part-j8f3a2c1.gcode.3mf')
        finally:
            os.unlink(path)
        self.assertEqual(link._printer.uploaded,
                         ('sample_part-j8f3a2c1.gcode.3mf', b'3mf-bytes'))

    def test_start_uses_plate_one(self):
        link = self.link()
        link.connect()
        self.assertTrue(link.start('sample_part-j8f3a2c1.gcode.3mf'))
        self.assertEqual(link._printer.started['plate_number'], 1)
        self.assertEqual(link._printer.started['filename'],
                         'sample_part-j8f3a2c1.gcode.3mf')

    def test_upload_before_connect_says_so(self):
        with self.assertRaisesRegex(RuntimeError, 'not connected'):
            pr.PrinterLink('10.0.0.42', '01P00A', '12345678').upload('/nonexistent', 'x.3mf')

    def test_start_before_connect_says_so(self):
        with self.assertRaisesRegex(RuntimeError, 'not connected'):
            pr.PrinterLink('10.0.0.42', '01P00A', '12345678').start('x.3mf')

    def test_a_failed_upload_does_not_leak_the_file_handle(self):
        """bambulabs_api closes the file in its own finally, but only once it gets that
        far; a connection refused on the way in must not leave the handle open."""
        class Refusing(FakeBambuPrinter):
            def upload_file(self, file_obj, filename):
                self.attempted = file_obj
                raise OSError('ftps refused')

        link = pr.PrinterLink('10.0.0.42', '01P00A', '12345678',
                              printer_factory=Refusing)
        link.connect()
        with tempfile.NamedTemporaryFile(suffix='.3mf', delete=False) as fh:
            fh.write(b'3mf-bytes')
            path = fh.name
        try:
            with self.assertRaises(OSError):
                link.upload(path, 'sample_part-j8f3a2c1.gcode.3mf')
        finally:
            os.unlink(path)
        self.assertTrue(link._printer.attempted.closed)

    def test_snapshot_never_raises_when_the_library_misbehaves(self):
        class Exploding(FakeBambuPrinter):
            def mqtt_dump(self):
                raise RuntimeError('mqtt is gone')

        link = pr.PrinterLink('10.0.0.42', '01P00A', '12345678',
                              printer_factory=Exploding)
        link.connect()
        self.assertEqual(link.snapshot()['state'], pr.UNREACHABLE)


class FakeSocket:
    """A UDP socket that never touches the network. recvfrom hands back the queued
    datagrams and then raises a real socket error, which ends discover()'s loop at once --
    so the test costs nothing and also pins the break-do-not-spin behaviour."""

    def __init__(self, packets=(), membership_error=None):
        self.packets = list(packets)
        self.membership_error = membership_error
        self.options = []
        self.timeout = None
        self.closed = False

    def setsockopt(self, level, optname, value):
        self.options.append((level, optname, value))
        if optname == socket.IP_ADD_MEMBERSHIP and self.membership_error is not None:
            raise self.membership_error

    def settimeout(self, value):
        self.timeout = value

    def recvfrom(self, _bufsize):
        if self.packets:
            return self.packets.pop(0)
        raise OSError('the interface went away')

    def close(self):
        self.closed = True


ANNOUNCEMENT = (b'NOTIFY * HTTP/1.1\r\n'
                b'HOST: 239.255.255.250:2021\r\n'
                b'USN: 01P00A123456789\r\n'
                b'DevModel.bambu.com: BL-H2S\r\n'
                b'DevName.bambu.com: Shop H2S\r\n\r\n')


class TestDiscovery(unittest.TestCase):
    def discover_with(self, sock):
        return pr.discover(timeout=5.0, sock_factory=lambda: sock)

    def test_it_joins_the_ssdp_multicast_group(self):
        """Binding UDP 2021 is not enough on Linux: without the membership the kernel
        drops every announcement and discovery silently finds nothing."""
        sock = FakeSocket()
        self.discover_with(sock)
        joins = [opt for opt in sock.options if opt[1] == socket.IP_ADD_MEMBERSHIP]
        self.assertEqual(len(joins), 1)
        level, _optname, value = joins[0]
        self.assertEqual(level, socket.IPPROTO_IP)
        self.assertEqual(value,
                         socket.inet_aton('239.255.255.250') + socket.inet_aton('0.0.0.0'))

    def test_an_announcement_becomes_an_entry(self):
        found = self.discover_with(FakeSocket([(ANNOUNCEMENT, ('10.0.0.42', 2021))]))
        self.assertEqual(found, [{'serial': '01P00A123456789', 'model': 'BL-H2S',
                                  'name': 'Shop H2S', 'ip': '10.0.0.42'}])

    def test_a_network_that_refuses_membership_still_listens(self):
        sock = FakeSocket([(ANNOUNCEMENT, ('10.0.0.42', 2021))],
                          membership_error=OSError('no such device'))
        self.assertEqual(len(self.discover_with(sock)), 1)

    def test_the_socket_is_closed_even_when_the_loop_breaks(self):
        sock = FakeSocket()
        self.discover_with(sock)
        self.assertTrue(sock.closed)


class TestTheLibraryStaysUnimported(unittest.TestCase):
    def test_importing_printer_does_not_import_bambulabs_api(self):
        """The lazy import is what lets `make test` run on a machine without the library.
        A stray module-level import would pass this suite (the library IS installed in the
        dev venv) and only break on the Pi, so check it in a clean interpreter."""
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        program = ("import sys\n"
                   "import printer_daemon.printer\n"
                   "sys.exit(2 if 'bambulabs_api' in sys.modules else 0)\n")
        done = subprocess.run([sys.executable, '-c', program], cwd=root,
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 0,
                         f'rc={done.returncode} stdout={done.stdout!r} '
                         f'stderr={done.stderr!r}')


if __name__ == '__main__':
    unittest.main()

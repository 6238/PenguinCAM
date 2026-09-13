"""The Pi's config file: /etc/penguincam-printer/config.toml.

`pair` writes the whole file as root; `run` rewrites the whole file as the penguincam user
to change only the token. The file carries no comments, so a full rewrite loses nothing."""
import os
import tempfile
import unittest
from unittest import mock

from printer_daemon import config as cfg


class ConfigTempFile(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, 'config.toml')

    def write(self, **overrides):
        values = dict(backend_url='https://penguincam.popcornpenguins.com',
                      printer_ip='10.0.0.42', printer_serial='01P00A',
                      access_code='12345678', pairing_code='7K3MQ9XW', token='')
        values.update(overrides)
        config = cfg.DaemonConfig(**values)
        cfg.save_config(config, path=self.path)
        return config


class TestRoundTrip(ConfigTempFile):
    def test_every_field_survives_a_round_trip(self):
        written = self.write()
        read = cfg.load_config(self.path)
        self.assertEqual(read.backend_url, written.backend_url)
        self.assertEqual(read.printer_ip, '10.0.0.42')
        self.assertEqual(read.printer_serial, '01P00A')
        self.assertEqual(read.access_code, '12345678')
        self.assertEqual(read.pairing_code, '7K3MQ9XW')
        self.assertEqual(read.token, '')
        self.assertFalse(read.dev)

    def test_the_file_is_mode_600(self):
        """It holds the printer's LAN access code."""
        self.write()
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_a_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            cfg.load_config(os.path.join(self.dir, 'absent.toml'))

    def test_unknown_keys_in_the_file_are_ignored(self):
        """A newer daemon's config on an older daemon must not crash the service."""
        with open(self.path, 'w') as fh:
            fh.write('backend_url = "https://example.test"\nfuture_option = 3\n')
        self.assertEqual(cfg.load_config(self.path).backend_url, 'https://example.test')


class TestAtomicSave(ConfigTempFile):
    """A crash mid-write must not cost the printer credentials only `pair` can restore."""

    def test_a_successful_save_leaves_no_temporary_file(self):
        self.write()
        self.assertEqual(sorted(os.listdir(self.dir)), ['config.toml'])

    def test_a_failed_rename_leaves_the_original_file_intact(self):
        self.write()
        replacement = cfg.load_config(self.path)
        replacement.token = 'half-written'
        replacement.access_code = 'clobbered'
        with mock.patch.object(cfg.os, 'replace', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                cfg.save_config(replacement, path=self.path)
        read = cfg.load_config(self.path)
        self.assertEqual(read.token, '')
        self.assertEqual(read.access_code, '12345678')
        self.assertEqual(sorted(os.listdir(self.dir)), ['config.toml'])

    def test_a_failed_write_leaves_the_original_file_intact(self):
        self.write()
        replacement = cfg.load_config(self.path)
        replacement.token = 'half-written'
        with mock.patch.object(cfg.tomli_w, 'dump', side_effect=OSError('no space left')):
            with self.assertRaises(OSError):
                cfg.save_config(replacement, path=self.path)
        read = cfg.load_config(self.path)
        self.assertEqual(read.token, '')
        self.assertEqual(read.printer_ip, '10.0.0.42')
        self.assertEqual(read.access_code, '12345678')
        self.assertEqual(sorted(os.listdir(self.dir)), ['config.toml'])


class TestDevFlag(ConfigTempFile):
    def test_a_real_boolean_is_kept(self):
        with open(self.path, 'w') as fh:
            fh.write('backend_url = "https://example.test"\ndev = true\n')
        self.assertIs(cfg.load_config(self.path).dev, True)

    def test_a_non_boolean_dev_value_is_false(self):
        """Fail closed: dev switches off the HTTPS requirement, so a truthy string must not
        be enough to set it."""
        for literal in ('"false"', '"true"', '1', '"yes"'):
            with self.subTest(literal=literal):
                with open(self.path, 'w') as fh:
                    fh.write(f'backend_url = "https://example.test"\ndev = {literal}\n')
                self.assertIs(cfg.load_config(self.path).dev, False)


class TestSaveToken(ConfigTempFile):
    def test_only_the_token_changes(self):
        self.write(dev=True)
        cfg.save_token('a-signed-token', path=self.path)
        read = cfg.load_config(self.path)
        self.assertEqual(read.token, 'a-signed-token')
        self.assertEqual(read.printer_ip, '10.0.0.42')
        self.assertEqual(read.access_code, '12345678')
        self.assertEqual(read.pairing_code, '7K3MQ9XW')
        self.assertTrue(read.dev)

    def test_the_mode_stays_600_after_a_rewrite(self):
        self.write()
        cfg.save_token('t', path=self.path)
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)


class TestBackendUrlPolicy(ConfigTempFile):
    def test_https_is_always_accepted(self):
        cfg.check_backend_url('https://penguincam.popcornpenguins.com', dev=False)

    def test_http_is_refused_without_dev(self):
        with self.assertRaises(ValueError) as caught:
            cfg.check_backend_url('http://192.168.1.5:6238', dev=False)
        self.assertIn('--dev', str(caught.exception))

    def test_http_is_accepted_with_dev(self):
        cfg.check_backend_url('http://192.168.1.5:6238', dev=True)

    def test_a_url_that_is_not_a_url_is_refused(self):
        with self.assertRaises(ValueError):
            cfg.check_backend_url('penguincam.popcornpenguins.com', dev=True)

    def test_a_trailing_slash_is_stripped_on_save(self):
        self.write(backend_url='https://example.test/')
        self.assertEqual(cfg.load_config(self.path).backend_url, 'https://example.test')


class TestWaitForConfig(ConfigTempFile):
    def test_returns_at_once_when_the_file_exists(self):
        self.write()
        slept = []
        got = cfg.wait_for_config(self.path, sleep=slept.append, log=lambda *a: None)
        self.assertEqual(slept, [])
        self.assertEqual(got.pairing_code, '7K3MQ9XW')

    def test_waits_and_logs_once_a_minute_instead_of_exiting(self):
        """`run` starting before `pair` must not die into a systemd restart loop."""
        ticks = {'n': 0}
        logged = []

        def sleep(_seconds):
            ticks['n'] += 1
            if ticks['n'] == 20:          # 20 * 5 s = 100 s of waiting
                self.write()

        def clock():
            return ticks['n'] * 5.0

        got = cfg.wait_for_config(self.path, sleep=sleep, clock=clock,
                                  log=lambda *a: logged.append(' '.join(str(x) for x in a)))
        self.assertEqual(got.pairing_code, '7K3MQ9XW')
        self.assertEqual(ticks['n'], 20)
        self.assertEqual(len(logged), 2)   # once at the start, once a minute later
        self.assertIn('config.toml', logged[0])


    def test_an_unreadable_file_is_waited_out_and_explained(self):
        """A wrong owner after a botched install must not become a restart loop either."""
        self.write()
        logged = []
        calls = {'n': 0}
        real_load = cfg.load_config

        def flaky(path):
            calls['n'] += 1
            if calls['n'] <= 3:
                raise PermissionError(13, 'Permission denied', path)
            return real_load(path)

        with mock.patch.object(cfg, 'load_config', flaky):
            got = cfg.wait_for_config(self.path, sleep=lambda _s: None, clock=lambda: 0.0,
                                      log=lambda *a: logged.append(' '.join(str(x) for x in a)))
        self.assertEqual(got.pairing_code, '7K3MQ9XW')
        self.assertEqual(calls['n'], 4)
        self.assertEqual(len(logged), 1)    # the same once-a-minute cadence
        self.assertIn('Permission denied', logged[0])

    def test_an_unparseable_file_is_waited_out_and_explained(self):
        """`pair` killed mid-write, or a half-finished hand edit."""
        with open(self.path, 'w') as fh:
            fh.write('backend_url = "https://exa')
        logged = []
        ticks = {'n': 0}

        def sleep(_seconds):
            ticks['n'] += 1
            if ticks['n'] == 2:
                self.write()

        got = cfg.wait_for_config(self.path, sleep=sleep, clock=lambda: ticks['n'] * 5.0,
                                  log=lambda *a: logged.append(' '.join(str(x) for x in a)))
        self.assertEqual(got.pairing_code, '7K3MQ9XW')
        self.assertEqual(len(logged), 1)
        self.assertIn('config.toml', logged[0])
        self.assertIn('cannot read it', logged[0])


class TestResolvePairingCode(unittest.TestCase):
    def test_an_existing_code_is_kept_by_default(self):
        """Re-running pair after a printer swap must not force a YAML edit."""
        self.assertEqual(
            cfg.resolve_pairing_code('7K3MQ9XW', new_code=False, generate=lambda: 'NEWCODE1'),
            '7K3MQ9XW')

    def test_new_code_mints_one(self):
        self.assertEqual(
            cfg.resolve_pairing_code('7K3MQ9XW', new_code=True, generate=lambda: 'NEWCODE1'),
            'NEWCODE1')

    def test_no_existing_code_mints_one(self):
        for existing in ('', None):
            self.assertEqual(
                cfg.resolve_pairing_code(existing, new_code=False,
                                         generate=lambda: 'NEWCODE1'),
                'NEWCODE1')


if __name__ == '__main__':
    unittest.main()

"""Static checks on the installer.

It cannot be executed here -- it makes users, writes to /opt and /etc, and talks to apt --
so these tests read it. They exist to catch the mistakes that would only show up on a Pi in
a shop at the worst possible moment."""
import os
import re
import shutil
import stat
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(ROOT, 'printer_daemon', 'install.sh')


class TestInstallScript(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(SCRIPT) as fh:
            cls.text = fh.read()

    def test_it_is_executable(self):
        self.assertTrue(os.stat(SCRIPT).st_mode & stat.S_IXUSR)

    def test_it_parses_as_shell(self):
        """A typo in a heredoc or a quote only shows up on the Pi otherwise."""
        bash = shutil.which('bash')
        if not bash:
            self.skipTest('bash is not installed')
        result = subprocess.run([bash, '-n', SCRIPT], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_it_fails_fast(self):
        self.assertIn('set -euo pipefail', self.text)

    def test_the_sha_placeholder_is_guarded(self):
        """Nothing is committed yet, so the SHA cannot be real. The guard is what stops a
        half-finished installer from downloading a tarball named REPLACE_BEFORE_MERGE."""
        self.assertIn('PENGUINCAM_SHA="REPLACE_BEFORE_MERGE"', self.text)
        self.assertIn('REPLACE_BEFORE_MERGE', self.text.split('PENGUINCAM_SHA=')[1])
        self.assertRegex(self.text, r'if\s+\[\s+"\$PENGUINCAM_SHA"\s+=\s+"REPLACE_BEFORE_MERGE"')

    def test_it_requires_aarch64(self):
        """There is no 32-bit Pillow wheel on PyPI, so an armv7l install would try to build
        it from source and fail after a long wait."""
        self.assertIn('aarch64', self.text)
        self.assertIn('armv7l', self.text)

    def test_it_tests_the_userland_architecture_not_the_kernel(self):
        """A 32-bit Raspberry Pi OS on a Pi 4 or 5 runs a 64-bit kernel and reports
        `uname -m` as armv8l, which slips past an aarch64 test."""
        self.assertIn('ARCH="$(dpkg --print-architecture)"', self.text)
        self.assertIn('arm64', self.text)
        # Named in the comment that explains the trap, never run.
        self.assertNotIn('$(uname', self.text)

    def test_it_installs_wheels_only(self):
        """--only-binary=:all: makes a future wheel gap fail loudly instead of starting a
        Pillow source build on a Pi."""
        self.assertIn('--only-binary=:all:', self.text)

    def test_it_installs_python3_venv(self):
        """Bookworm is PEP 668 externally-managed: a venv is mandatory, not tidy."""
        self.assertIn('python3-venv', self.text)

    def test_it_downloads_from_the_pinned_sha(self):
        self.assertIn('github.com/6238/PenguinCAM/archive/', self.text)
        self.assertIn('$PENGUINCAM_SHA', self.text)

    def test_it_extracts_only_the_daemon_directory(self):
        # Two levels: the archive's PenguinCAM-<sha> and the print/ package, so the
        # daemon lands at $APP_DIR/printer_daemon and `python -m printer_daemon` works.
        self.assertIn('--strip-components=2', self.text)
        self.assertIn("'*/print/printer_daemon/*'", self.text)

    def test_it_creates_the_service_user_and_enables_without_starting(self):
        self.assertIn('useradd', self.text)
        self.assertIn('penguincam', self.text)
        self.assertIn('systemctl enable', self.text)
        self.assertNotIn('systemctl start penguincam-printer', self.text)

    def test_the_unit_restarts_always(self):
        self.assertIn('Restart=always', self.text)
        self.assertIn('ExecStart=', self.text)

    def test_pair_is_run_with_stdin_from_the_tty(self):
        """The script itself arrived on standard input from the curl pipe, so pair would
        read the rest of the script as its answers without this."""
        self.assertRegex(self.text, r'pair\s*<\s*/dev/tty')

    def test_the_config_directory_is_handed_to_the_service_user_after_pairing(self):
        """`run` rewrites config.toml by writing a temporary file and renaming it over the
        original, which needs write permission on the directory as well as the file. pair
        runs as root, so the installer chowns again once it returns."""
        match = re.search(r'pair\s*<\s*/dev/tty', self.text)
        self.assertIsNotNone(match, 'the script never runs pair with stdin from the tty')
        after = self.text[match.start():]
        self.assertRegex(after, r'chown[^\n]*"\$SERVICE_USER:\$SERVICE_USER"[^\n]*\$CONFIG_DIR')

    def test_the_unit_is_hardened(self):
        """The daemon is installed as root from a piped script and then runs unattended in
        a shop. It writes two directories and nothing else, so the unit says so."""
        for directive in ('NoNewPrivileges=true', 'ProtectSystem=strict',
                          'ProtectHome=true', 'PrivateTmp=true'):
            self.assertIn(directive, self.text)
        match = re.search(r'^ReadWritePaths=(.+)$', self.text, re.MULTILINE)
        self.assertIsNotNone(match, 'the unit grants no writable path')
        # ProtectSystem=strict makes everything else read-only, so both directories the
        # daemon writes have to be named here.
        self.assertIn('$CONFIG_DIR', match.group(1))
        self.assertIn('$DOWNLOAD_DIR', match.group(1))

    def test_the_service_user_does_not_own_its_own_code(self):
        """The app and the virtual environment stay root-owned and world-readable: nothing
        in the daemon writes there, and a service that can rewrite its own code is a much
        larger target than one that cannot."""
        self.assertNotIn('chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"', self.text)
        self.assertNotRegex(self.text,
                            r'chown[^\n]*"\$SERVICE_USER:\$SERVICE_USER"[^\n]*INSTALL_DIR')

    def test_the_download_directory_is_created_for_the_service_user_only(self):
        """The daemon downloads sliced files there. It creates the directory itself if it
        is missing, but the installer sets the ownership and the mode once, as root."""
        self.assertIn('DOWNLOAD_DIR="/var/tmp/penguincam-printer"', self.text)
        self.assertRegex(self.text, r'chown\s+"\$SERVICE_USER:\$SERVICE_USER"\s+"\$DOWNLOAD_DIR"')
        self.assertRegex(self.text, r'chmod\s+700\s+"\$DOWNLOAD_DIR"')

    def test_the_documented_command_is_the_github_raw_url_with_dash_f(self):
        """A Cloudflare challenge page piped into bash fails confusingly, and free-plan Bot
        Fight Mode cannot be skipped per path."""
        self.assertIn('raw.githubusercontent.com/6238/PenguinCAM', self.text)
        self.assertIn('curl -fsSL', self.text)

    def test_it_never_passes_dev(self):
        """A shop install must not be able to talk plain HTTP."""
        self.assertNotIn('--dev', self.text)

    def test_it_contains_no_secret(self):
        self.assertNotRegex(self.text, r'(?i)(api[_-]?key|password|secret)\s*=')


if __name__ == '__main__':
    unittest.main()

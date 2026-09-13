"""Tests for print/scripts/install-orca.sh. No network: uname and curl are faked on PATH."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]        # the repository root
SCRIPT = REPO / "print" / "scripts" / "install-orca.sh"


class InstallOrcaScriptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="orca-install-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        (self.tmp / "scripts").mkdir()
        self.script = self.tmp / "scripts" / "install-orca.sh"
        shutil.copy(SCRIPT, self.script)
        self.script.chmod(0o755)
        self.fakebin = self.tmp / "fakebin"
        self.fakebin.mkdir()
        self.dest = self.tmp / "tools" / "orca"

    def fake(self, name, body):
        path = self.fakebin / name
        path.write_text("#!/usr/bin/env bash\n" + body)
        path.chmod(0o755)

    def fake_uname(self, system, machine):
        self.fake("uname", 'case "$1" in -m) echo %s;; *) echo %s;; esac\n' % (machine, system))

    def fake_curl_garbage(self):
        # Writes junk to whatever -o names and records that it was called.
        self.fake("curl", 'out=""; while [ $# -gt 0 ]; do case "$1" in -o) out="$2"; shift;; esac; shift; done\n'
                          'echo called >> "%s"\n'
                          'mkdir -p "$(dirname "$out")"; echo garbage > "$out"\n' % (self.tmp / "curl-calls"))

    def run_script(self):
        env = dict(os.environ)
        env["PATH"] = f"{self.fakebin}:{env['PATH']}"
        return subprocess.run(["bash", str(self.script)], env=env,
                              capture_output=True, text=True, timeout=120)

    def test_refuses_unknown_platform_naming_both_values(self):
        self.fake_uname("FreeBSD", "amd64")
        self.fake_curl_garbage()
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("FreeBSD", result.stderr)
        self.assertIn("amd64", result.stderr)
        self.assertFalse((self.tmp / "curl-calls").exists(), "must not download")
        self.assertFalse((self.dest / "VERSION").exists())

    def test_exits_without_downloading_when_version_and_arch_match(self):
        self.fake_uname("Linux", "aarch64")
        self.fake_curl_garbage()
        self.dest.mkdir(parents=True)
        (self.dest / "VERSION").write_text("2.4.2 linux-aarch64\n")
        wrapper = self.dest / "orca-slicer"
        wrapper.write_text("#!/bin/sh\nexit 0\n")
        wrapper.chmod(0o755)
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.tmp / "curl-calls").exists(), "must not download")
        self.assertEqual((self.dest / "VERSION").read_text().strip(), "2.4.2 linux-aarch64")

    def test_replaces_install_from_another_architecture(self):
        self.fake_uname("Linux", "aarch64")
        self.fake_curl_garbage()
        self.dest.mkdir(parents=True)
        (self.dest / "VERSION").write_text("2.4.2 darwin-arm64\n")
        (self.dest / "orca-slicer").write_text("#!/bin/sh\nexit 0\n")
        (self.dest / "orca-slicer").chmod(0o755)
        result = self.run_script()
        # The fake download is garbage, so the run must fail at the checksum,
        # proving it tried to replace the foreign install rather than trusting it.
        # The wrapper is executable, so only the architecture half of the stamp
        # can have forced the reinstall.
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.tmp / "curl-calls").exists(), "must download")
        self.assertFalse((self.dest / "VERSION").exists(), "stale VERSION must be removed")

    def test_checksum_mismatch_deletes_download_and_fails(self):
        self.fake_uname("Linux", "x86_64")
        self.fake_curl_garbage()
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("checksum", result.stderr.lower())
        leftovers = list(self.dest.rglob("*.AppImage")) if self.dest.exists() else []
        self.assertEqual(leftovers, [], "corrupt download must be deleted")
        self.assertFalse((self.dest / "VERSION").exists())

    def test_downloads_the_asset_for_the_platform(self):
        self.fake_uname("Darwin", "arm64")
        self.fake("curl", 'while [ $# -gt 0 ]; do case "$1" in -o) out="$2"; shift;; http*) echo "$1" >> "%s";; esac; shift; done\n'
                          'mkdir -p "$(dirname "$out")"; echo garbage > "$out"\n' % (self.tmp / "curl-urls"))
        self.run_script()
        urls = (self.tmp / "curl-urls").read_text()
        self.assertIn("https://github.com/OrcaSlicer/OrcaSlicer/releases/download/v2.4.2/OrcaSlicer_Mac_universal_V2.4.2.dmg", urls)


if __name__ == "__main__":
    unittest.main()

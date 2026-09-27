#!/usr/bin/env python3
"""Run this BY HAND against a real Bambu printer. It starts a real print.

    PENGUINCAM_PRINTER_IP=10.0.0.42 \
    PENGUINCAM_PRINTER_SERIAL=01P00A... \
    PENGUINCAM_PRINTER_ACCESS_CODE=12345678 \
    PENGUINCAM_SMOKE_FILE=/path/to/a/small/sliced.gcode.3mf \
    uv run python printer_daemon/tests/smoke_test.py

It is deliberately NOT named test_*.py, so `unittest discover` never finds it and
`make test-daemon` never starts a print.

What it is for: the mocked tests are written against bambulabs_api's documented field
names, because the shop H2S was not connected when this feature was built. This script
prints the printer's RAW status document, which becomes the replacement for
printer_daemon/tests/fixtures/h2s_status.json -- after which the state-mapping tests are
pinned to the real thing. It also answers three open questions: the status field names and
units, the file-name form the printer reports, and how long the printer takes to report
`running` after the start command.
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from printer_daemon.printer import PrinterLink, map_status, suffixed_name  # noqa: E402

WAIT_FOR_RUNNING_SECONDS = 120.0


def env(name):
    value = os.environ.get(name)
    if not value:
        sys.exit(f"set {name} in the environment (see this file's docstring)")
    return value


def main():
    ip = env('PENGUINCAM_PRINTER_IP')
    serial = env('PENGUINCAM_PRINTER_SERIAL')
    access_code = env('PENGUINCAM_PRINTER_ACCESS_CODE')
    source = env('PENGUINCAM_SMOKE_FILE')
    if not os.path.exists(source):
        sys.exit(f"{source} does not exist")

    job_id = 'j5a0ce001'
    remote_name = suffixed_name(os.path.basename(source), job_id)

    link = PrinterLink(ip, serial, access_code)
    print(f"connecting to {ip} ...")
    link.connect()
    try:
        for _ in range(40):
            if link.raw_report():
                break
            time.sleep(0.5)
        raw = link.raw_report()
        if not raw:
            sys.exit("no MQTT report arrived. Check LAN-only mode, Developer Mode and the "
                     "access code.")

        print("\n--- raw status before the print (status field names and units) ---")
        print(json.dumps(raw, indent=2, sort_keys=True, default=str))
        print("\n--- mapped snapshot ---")
        print(json.dumps(map_status(raw, printer=link.identity), indent=2, sort_keys=True))

        print(f"\nuploading as {remote_name} ...")
        link.upload(source, remote_name)
        print("starting the print ...")
        started = link.start(remote_name)
        print(f"start_print returned {started!r}")

        # How long from the start command to a reported `running`,
        # including the printer's preparing states.
        began = time.monotonic()
        seen = []
        while time.monotonic() - began < WAIT_FOR_RUNNING_SECONDS:
            raw = link.raw_report()
            snapshot = map_status(raw, printer=link.identity)
            marker = (snapshot['state'], raw.get('gcode_state'), raw.get('stg_cur'),
                      snapshot['file'])
            if not seen or seen[-1] != marker:
                seen.append(marker)
                print(f"  t+{time.monotonic() - began:5.1f}s  {marker}")
            if snapshot['state'] == 'running' and snapshot['file'] == remote_name:
                print(f"\nreported running after "
                      f"{time.monotonic() - began:.1f}s")
                break
            time.sleep(1.0)
        else:
            print("\nthe printer never reported running with our file name; "
                  "check the reported file-name form below")

        final = link.raw_report()
        print("\n--- raw status while printing (this becomes the fixture) ---")
        print(json.dumps(final, indent=2, sort_keys=True, default=str))
        print("\nReported file name: "
              f"{final.get('gcode_file')!r} / subtask {final.get('subtask_name')!r}")
    finally:
        link.disconnect()


if __name__ == '__main__':
    main()

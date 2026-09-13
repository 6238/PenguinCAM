"""The PenguinCAM printer daemon: a small always-on process for a Raspberry Pi on the shop
LAN that relays print jobs from PenguinCAM to one Bambu Lab printer.

It is NOT a pip package and has no pyproject.toml -- the repository's CLAUDE.md forbids one,
so the installer copies this directory onto the Pi and runs it with
`python -m printer_daemon`. The backend never imports this package, and this package never
imports the backend; the only thing they share is the HTTP protocol in relay_client.py.
"""

VERSION = "1.0.0"
USER_AGENT = f"penguincam-printer/{VERSION}"

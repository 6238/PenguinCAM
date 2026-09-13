"""`penguincam-printer pair | run | status`.

The installer writes /usr/local/bin/penguincam-printer as a two-line wrapper that runs
`python -m printer_daemon "$@"` out of the daemon's venv, so this is the whole CLI.

`pair` is the only command a mentor runs by hand, and it runs under sudo: it writes
/etc/penguincam-printer/config.toml as root, hands the directory and the file to the
penguincam user the service runs as, and restarts the unit. `run` is what systemd starts.
`status` is what a mentor runs when the wizard says the printer is offline; it posts
nothing, so it cannot disturb the running service.
"""

import argparse
import os
import pwd
import secrets
import subprocess
import sys
import time
from urllib.parse import urlparse

from printer_daemon import VERSION
from printer_daemon import config as cfg
from printer_daemon.printer import PrinterLink, discover
from printer_daemon.relay_client import ChallengeDetected, RelayClient, RelayError
from printer_daemon.run_loop import RunLoop

# Crockford base32 without I, L, O and U. Deliberately duplicated from printer_relay.py:
# the backend is not installed on the Pi and the daemon never imports it. A test pins the
# two literals together, so a change to one is caught.
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
CODE_LENGTH = 8

SERVICE_NAME = 'penguincam-printer'
SERVICE_USER = 'penguincam'
CONNECT_WAIT_SECONDS = 20.0
CONNECT_POLL_SECONDS = 0.5
STATUS_WAIT_SECONDS = 10.0
DISCOVERY_SECONDS = 5.0

#: Where a job's sliced file lands while it is on its way to the printer. Not /tmp: on a Pi
#: that is often a tmpfs, and a hundred-megabyte 3MF would be spent RAM.
DOWNLOAD_DIR = '/var/tmp/penguincam-printer'


def generate_pairing_code():
    """Forty bits, never all digits -- an all-digit code would load out of an unquoted YAML
    value as an int."""
    while True:
        code = ''.join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))
        if not code.isdigit():
            return code


def format_pairing_code(code):
    """Display form: two groups of four separated by a dash."""
    return f"{code[:4]}-{code[4:]}"


def hand_to_service_user(path, log, *, lookup=pwd.getpwnam, chown=os.chown):
    """Give the config directory and file to the user the service runs as.

    `pair` runs as root under sudo, so everything it writes is root's. `run` runs as
    penguincam and rewrites the whole file to store its token, which needs write permission
    on the file AND on its directory (the write is a temporary file renamed into place). A
    development machine has no penguincam user; say so and carry on rather than fail -- the
    same config file is perfectly usable by the user who wrote it.

    Only the daemon's OWN directory is handed over. `pair --config /etc/config.toml` must
    chown /etc/config.toml and nothing else: giving away whatever directory a typed path
    happens to sit in would be a far bigger change than the one that was asked for."""
    try:
        entry = lookup(SERVICE_USER)
    except KeyError:
        log(f"note: no {SERVICE_USER} user on this machine, so {path} keeps its current "
            f"owner (fine for development, not for a Pi)")
        return False
    directory = os.path.dirname(os.path.abspath(path))
    own_directory = os.path.dirname(cfg.DEFAULT_CONFIG_PATH)
    targets = ([directory] if directory == own_directory else []) + [path]
    for target in targets:
        try:
            chown(target, entry.pw_uid, entry.pw_gid)
        except OSError as e:
            log(f"warning: could not give {target} to {SERVICE_USER} ({e}); the service "
                f"will not be able to store its token")
            return False
    return True


def restart_service(log, *, run=subprocess.run):
    """Start (or restart) the systemd unit. A machine without systemd just gets a note."""
    try:
        run(['systemctl', 'restart', SERVICE_NAME], check=True)
        log(f"restarted {SERVICE_NAME}")
    except Exception as e:
        log(f"could not restart {SERVICE_NAME} ({e}); start it yourself with "
            f"`sudo systemctl restart {SERVICE_NAME}`")


def _connect_and_identify(link, *, wait=CONNECT_WAIT_SECONDS, clock=time.monotonic,
                          sleep=time.sleep):
    """Connect and wait for the printer's first MQTT report. Returns the snapshot, or None
    when nothing arrived before the deadline.

    The clock and the sleep are injected so the tests spend no real seconds here."""
    link.connect()
    deadline = clock() + wait
    while clock() < deadline:
        snapshot = link.snapshot()
        if snapshot.get('state') != 'unreachable':
            return snapshot
        sleep(CONNECT_POLL_SECONDS)
    return None


def _ask_until_answered(prompt, text):
    """Ask again on an empty line. Every answer `pair` asks for is required: an empty one
    would be written straight into the config file as a printer that cannot be reached."""
    while True:
        answer = prompt(text)
        if answer:
            return answer


def _choose_printer(prompt, say):
    """Ask the mentor which printer to pair with. Returns (ip, serial, model, name)."""
    try:
        found = discover(timeout=DISCOVERY_SECONDS)
    except OSError as e:
        # Binding the discovery port can fail (another listener, a container without
        # multicast). That is not a reason to stop: the typed path always works.
        say(f"Could not listen for printers ({e}); enter the address by hand.")
        found = []

    if found:
        say("Printers heard on the network:")
        for index, entry in enumerate(found, start=1):
            say(f"  {index}) {entry['model'] or 'Bambu printer'} "
                f"{entry['name'] or ''} {entry['serial']} at {entry['ip']}")
        say("Enter a number, or an IP address if your printer is not listed.")
        while True:
            answer = prompt("printer: ")
            if not answer:
                continue
            if not answer.isdigit():
                ip = answer                     # an IP address, not a menu choice
                break
            if 1 <= int(answer) <= len(found):
                chosen = found[int(answer) - 1]
                return chosen['ip'], chosen['serial'], chosen['model'], chosen['name']
            # A bare number that is not on the menu is a slip, not an address.
            say(f"There is no printer {answer} on that list.")
    else:
        say("No printers heard; enter the IP address shown on the printer's screen.")
        say("(School networks usually block the broadcast the printer uses to announce "
            "itself, so this is the normal path.)")
        ip = _ask_until_answered(prompt, "printer IP address: ")
    return ip, _ask_until_answered(prompt, "printer serial: "), None, None


def cmd_pair(args, *, ask=input, out=sys.stdout, clock=time.monotonic, sleep=time.sleep):
    """Find the printer, confirm it answers, write the config file, print the YAML lines."""
    def say(*parts):
        print(*parts, file=out)

    def prompt(text):
        """The prompt is printed rather than passed to `ask`, so a test can inject a plain
        zero-argument reader and a real run still shows the question."""
        print(text, end='', file=out, flush=True)
        return (ask() or '').strip()

    backend = args.backend or cfg.DEFAULT_BACKEND_URL
    try:
        cfg.check_backend_url(backend, dev=bool(args.dev))
    except ValueError as e:
        say(f"error: {e}")
        return 2

    # dev is the permission to talk to THIS backend over plain HTTP, so it is only recorded
    # when the URL actually is plain HTTP. Writing it for an https backend would leave a
    # config that silently accepts an http one after a later hand edit.
    dev = bool(args.dev) and urlparse(backend.strip()).scheme == 'http'
    if args.dev and not dev:
        say(f"note: --dev was not needed for {backend}; not recording development mode.")

    try:
        existing = cfg.load_config(args.config)
    except (OSError, ValueError):
        # Missing is the normal case; unreadable or half-written means the old pairing code
        # is gone either way, and this run writes a whole new file over it.
        existing = None

    ip, serial, model, name = _choose_printer(prompt, say)
    access_code = _ask_until_answered(prompt, "printer LAN access code: ")

    # Anything the link raises -- a mistyped address, a refused connection, bambulabs_api
    # missing from the venv -- is the same answer to the mentor as silence, and it gets the
    # same advice rather than a traceback.
    link = None
    try:
        link = PrinterLink(ip, serial, access_code, model=model, name=name)
        snapshot = _connect_and_identify(link, clock=clock, sleep=sleep)
    except Exception as e:
        say(f"error: could not talk to the printer at {ip}: {e}")
        snapshot = None
    finally:
        if link is not None:
            link.disconnect()
    if snapshot is None:
        say("")
        say(f"The printer at {ip} did not answer.")
        say("Check that it is in LAN-only mode with Developer Mode ON, and that the")
        say("access code matches the one on the printer's screen. Developer Mode is")
        say("required: Bambu firmware rejects print starts from third-party software")
        say("in any other mode. See docs/PRINTER_SETUP.md.")
        return 1

    identity = snapshot.get('printer') or {}
    say(f"Connected to {identity.get('model') or model or 'the printer'} "
        f"{identity.get('name') or name or ''} ({serial}).")

    code = cfg.resolve_pairing_code(existing.pairing_code if existing else None,
                                    new_code=bool(args.new_code),
                                    generate=generate_pairing_code)
    config = cfg.DaemonConfig(backend_url=backend, printer_ip=ip, printer_serial=serial,
                              access_code=access_code, pairing_code=code, token='',
                              dev=dev, path=args.config)
    cfg.save_config(config, path=args.config)
    hand_to_service_user(args.config, say)

    say("")
    say(f"Backend: {config.backend_url}")
    say(f"Config:  {args.config}")
    say("")
    say("Paste these two lines into your team's PenguinCAM-config.yaml in Onshape,")
    say("then open PenguinCAM from Onshape and click the config refresh link:")
    say("")
    say("printing:")
    say(f'  pairing_code: "{format_pairing_code(code)}"')
    say("")
    restart_service(say)
    return 0


def is_pairing_code(value):
    """True for the eight symbols `pair` writes. Anything else -- an empty field, a
    hand-edited YAML paste with its dash still in it, a truncated line -- is not a code the
    relay could ever recognise."""
    return (isinstance(value, str) and len(value) == CODE_LENGTH
            and all(ch in ALPHABET for ch in value))


def wait_for_paired_config(path, *, log=print, sleep=time.sleep, clock=time.monotonic,
                           interval=5.0, log_every=60.0):
    """Block until the config file is there AND carries a usable pairing code.

    A config with no code cannot pair, so starting the loop on one would only fill the
    journal with exchanges the backend answers 404. Waiting beats exiting for the same
    reason wait_for_config waits: an exit becomes a systemd restart loop that buries the
    one message a mentor needs to see. Logs once at the start and then once a minute."""
    started = clock()
    last_log = None
    while True:
        config = cfg.wait_for_config(path, log=log, sleep=sleep, clock=clock,
                                     interval=interval, log_every=log_every)
        if is_pairing_code(config.pairing_code):
            return config
        now = clock()
        if last_log is None or now - last_log >= log_every:
            log(f"{path} has no pairing code -- run `sudo penguincam-printer pair` first "
                f"(waited {int(now - started)}s)")
            last_log = now
        sleep(interval)


def _ensure_download_dir(path):
    """Make the scratch directory, readable only by the service user."""
    os.makedirs(path, mode=0o700, exist_ok=True)
    try:
        # A directory left behind by an older install may be looser than makedirs would
        # have made it; exist_ok does not touch the mode of one that is already there.
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def cmd_run(args, *, out=sys.stdout, download_dir=DOWNLOAD_DIR, clock=time.monotonic,
            sleep=time.sleep):
    """The service. Waits for the config file rather than exiting into a restart loop."""
    def log(*parts):
        print(*parts, file=out, flush=True)

    try:
        config = wait_for_paired_config(args.config, log=log, clock=clock, sleep=sleep)
    except KeyboardInterrupt:
        # Ctrl-C while waiting, from a mentor running this by hand: not a crash.
        log("stopping")
        return 0
    # Refuse a backend URL the daemon must not talk to before anything is opened. This is
    # the same check preflight() makes; only preflight logs the development warning, so
    # asking here costs no duplicate line.
    try:
        cfg.check_backend_url(config.backend_url, dev=config.dev)
    except ValueError as e:
        log(f"error: {e}")
        return 2

    printer = PrinterLink(config.printer_ip, config.printer_serial, config.access_code)
    relay = RelayClient(config.backend_url, token=config.token or None)
    loop = RunLoop(config, relay, printer, log=log,
                   download_dir=_ensure_download_dir(download_dir))
    log(f"penguincam-printer {VERSION} running against {config.backend_url}")
    status = 0
    try:
        printer.connect()
        loop.run_forever()
    except ValueError as e:
        # Backstop: run_forever() calls preflight(), which makes the same check again.
        log(f"error: {e}")
        status = 2
    except KeyboardInterrupt:
        log("stopping")
    except Exception as e:
        # A connect that throws -- no bambulabs_api in the venv, a socket the Pi cannot
        # open -- is a message and an exit code, not a traceback in the journal.
        log(f"error: could not start: {e}")
        status = 1
    finally:
        loop.executor.shutdown(wait=True)
        printer.disconnect()
    return status


def cmd_status(args, *, out=sys.stdout, clock=time.monotonic, sleep=time.sleep):
    """What a mentor runs when the wizard says offline. Posts no snapshot, so it cannot
    disturb the running service."""
    def say(*parts):
        print(*parts, file=out)

    try:
        config = cfg.load_config(args.config)
    except FileNotFoundError:
        say(f"No config file at {args.config}. Run `sudo penguincam-printer pair`.")
        return 1

    say(f"penguincam-printer {VERSION}")
    say(f"backend: {config.backend_url}")
    say(f"printer IP: {config.printer_ip}")
    say(f"serial: {config.printer_serial}")
    code = format_pairing_code(config.pairing_code) if config.pairing_code else '(none)'
    say(f"pairing code: {code}")
    # The token itself is a secret and is never printed, here or anywhere else.
    say(f"token: {'set' if config.token else 'not set'}")
    say(f"dev mode: {'yes' if config.dev else 'no'}")

    printer = PrinterLink(config.printer_ip, config.printer_serial, config.access_code)
    try:
        snapshot = _connect_and_identify(printer, wait=STATUS_WAIT_SECONDS, clock=clock,
                                         sleep=sleep)
    except Exception as e:
        snapshot = None
        say(f"printer: could not connect ({e})")
    finally:
        printer.disconnect()
    if snapshot is not None:
        say(f"printer: answering, state {snapshot.get('state')}")
    else:
        say("printer: no MQTT report -- check LAN-only mode, Developer Mode and the "
            "access code")

    relay = RelayClient(config.backend_url, token=config.token or None)
    try:
        flags = relay.ping()
        say(f"backend: reachable, waiting={flags.get('waiting')} "
            f"online={flags.get('online')}")
    except ChallengeDetected as e:
        say(f"backend: blocked by a bot challenge -- {e}")
    except RelayError as e:
        say(f"backend: not reachable ({e})")
    return 0


def build_parser():
    parser = argparse.ArgumentParser(prog='penguincam-printer',
                                     description='PenguinCAM printer daemon')
    # --config is declared on each subcommand, not here: a top-level copy would shadow the
    # subparser's value with its own default.
    sub = parser.add_subparsers(dest='command', required=False)

    pair = sub.add_parser('pair', help='find the printer and mint a pairing code')
    pair.add_argument('--config', default=cfg.DEFAULT_CONFIG_PATH)
    pair.add_argument('--new-code', action='store_true',
                      help='mint a fresh pairing code instead of keeping the existing one')
    pair.add_argument('--backend', default=None,
                      help='backend URL (defaults to the hosted service)')
    pair.add_argument('--dev', action='store_true',
                      help='allow a plain-HTTP backend URL (development only)')

    run = sub.add_parser('run', help='run the relay loop (what systemd starts)')
    run.add_argument('--config', default=cfg.DEFAULT_CONFIG_PATH)

    status = sub.add_parser('status', help='report on the config, printer and backend')
    status.add_argument('--config', default=cfg.DEFAULT_CONFIG_PATH)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == 'pair':
        return cmd_pair(args)
    if args.command == 'run':
        return cmd_run(args)
    if args.command == 'status':
        return cmd_status(args)
    parser.print_help()
    return 1


if __name__ == '__main__':
    sys.exit(main())

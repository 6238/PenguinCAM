"""Read and write the Pi's config file, /etc/penguincam-printer/config.toml.

`pair` runs under sudo and writes the whole file as root, then hands the directory and file
to the penguincam user. `run` runs as penguincam and rewrites the whole file to change only
the token. A full rewrite is safe because the file carries no comments.

Reading needs no dependency: tomllib is in the standard library from Python 3.11, which is
what Raspberry Pi OS Bookworm ships. Only writing needs tomli_w.
"""

import os
import time
import tomllib
from dataclasses import dataclass, fields
from urllib.parse import urlparse

import tomli_w

DEFAULT_CONFIG_PATH = "/etc/penguincam-printer/config.toml"
DEFAULT_BACKEND_URL = "https://penguincam.popcornpenguins.com"

# Fields written to the file, in this order. `path` is where the file lives, not part of it.
_FILE_FIELDS = ('backend_url', 'printer_ip', 'printer_serial', 'access_code',
                'pairing_code', 'token', 'dev')


@dataclass
class DaemonConfig:
    """Everything the daemon needs. The access code and the token are secrets, which is why
    the file is mode 600 in a directory owned by the penguincam user."""

    backend_url: str = DEFAULT_BACKEND_URL
    printer_ip: str = ""
    printer_serial: str = ""
    access_code: str = ""
    pairing_code: str = ""
    token: str = ""
    dev: bool = False
    path: str = DEFAULT_CONFIG_PATH

    def to_dict(self):
        return {name: getattr(self, name) for name in _FILE_FIELDS}


def _normalize_url(url):
    return (url or "").strip().rstrip('/')


def check_backend_url(url, *, dev):
    """Raise ValueError unless this is a URL the daemon may talk to.

    A plain-HTTP backend is refused unless `pair` was given --dev, which writes dev = true.
    The installer never passes --dev, so a shop install cannot talk HTTP. The flag exists
    because the development server is reached from the Pi by the Mac's LAN address, not by
    localhost, so a hostname rule would not do."""
    parsed = urlparse(_normalize_url(url))
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        raise ValueError(f"{url!r} is not a backend URL; it must start with https://")
    if parsed.scheme == 'http' and not dev:
        raise ValueError(
            f"Refusing a plain-HTTP backend URL ({url}). Re-run pair with --dev if this is "
            "a development server you trust on your own network.")


def load_config(path=DEFAULT_CONFIG_PATH):
    """Parse the config file. Raises FileNotFoundError when it is not there yet.

    Unknown keys are ignored rather than fatal, so a config written by a newer daemon does
    not stop an older one."""
    with open(path, 'rb') as fh:
        data = tomllib.load(fh)
    known = {f.name for f in fields(DaemonConfig)} - {'path'}
    values = {k: v for k, v in data.items() if k in known}
    values['backend_url'] = _normalize_url(values.get('backend_url') or DEFAULT_BACKEND_URL)
    # Fail closed: only a real TOML boolean turns dev on. A hand-edited dev = "false" is a
    # string, and a truthy one, so anything that is not a boolean has to mean "not dev".
    values['dev'] = values.get('dev') is True
    return DaemonConfig(path=path, **values)


def save_config(config, path=None):
    """Write the whole file atomically, mode 600, creating its directory if needed."""
    path = path or config.path or DEFAULT_CONFIG_PATH
    config.backend_url = _normalize_url(config.backend_url)
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    # Write a temporary file and rename it over the old one, rather than truncating the live
    # file: a crash mid-write would otherwise lose the printer's IP, serial and access code,
    # which only a second `pair` run can restore. `pair` chowns the directory and the file to
    # the penguincam user before `run` ever writes, so the rename succeeds unprivileged.
    # The temporary file is created 0600, so the access code is never world-readable.
    tmp = f"{path}.tmp"
    try:
        handle = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(handle, 'wb') as fh:
            tomli_w.dump(config.to_dict(), fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)   # in case the temporary file already existed, looser
        os.replace(tmp, path)
    except BaseException:
        # Leave no half-written file behind for the next writer to reuse.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    config.path = path


def save_token(token, path=DEFAULT_CONFIG_PATH):
    """Change only the token. Everything else is read back and written out unchanged."""
    config = load_config(path)
    config.token = token or ""
    save_config(config, path=path)
    return config


def wait_for_config(path=DEFAULT_CONFIG_PATH, *, sleep=time.sleep, clock=time.monotonic,
                    log=print, interval=5.0, log_every=60.0):
    """Block until the config file can be read, then return it.

    `run` may start before `pair` has been run -- systemd enables the unit at install time.
    Waiting beats exiting: an exit would become a restart loop that buries the real message
    in the journal. The same goes for a file that is there but unreadable (wrong owner after
    a botched install) or unparseable (a half-finished hand edit, or a `pair` that died
    mid-write): wait and say why, because the fix is a human on the Pi either way. Logs once
    at the start and then once a minute, on the monotonic clock, so a Pi with no network time
    source behaves like any other."""
    started = clock()
    last_log = None
    while True:
        try:
            return load_config(path)
        except FileNotFoundError:
            reason = "run `sudo penguincam-printer pair`"
        except (OSError, tomllib.TOMLDecodeError) as exc:
            reason = f"cannot read it: {exc}"
        now = clock()
        if last_log is None or now - last_log >= log_every:
            waited = int(now - started)
            log(f"waiting for {path} -- {reason} (waited {waited}s)")
            last_log = now
        sleep(interval)


def resolve_pairing_code(existing, *, new_code, generate):
    """The code `pair` should write.

    Re-running pair on the same Pi keeps the existing code, so a printer swap or a
    reinstall needs no YAML edit. --new-code mints a fresh one, for when a former student
    still knows the old one."""
    if existing and not new_code:
        return existing
    return generate()

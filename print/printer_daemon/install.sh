#!/usr/bin/env bash
#
# PenguinCAM printer daemon installer for Raspberry Pi OS Bookworm (64-bit).
#
#   curl -fsSL https://raw.githubusercontent.com/6238/PenguinCAM/<sha>/print/printer_daemon/install.sh | sudo bash
#
# The -f matters: curl then exits non-zero and prints nothing on any 4xx or 5xx, so a
# blocked or moved URL can never be piped into bash. (https://penguincam.popcornpenguins.com/install-printer.sh
# serves the same script and is a convenience alias, but the GitHub URL is the documented
# one: the site sits behind Cloudflare, free-plan Bot Fight Mode cannot be skipped per
# path, and a challenge page piped into bash produces a pile of syntax errors instead of a
# clear failure.)
#
# Re-running upgrades in place and runs `pair` again, which keeps the existing pairing code
# unless a new one is asked for.

set -euo pipefail

# The one place the daemon's version is written. The pull request that changes anything in
# print/printer_daemon/ updates it to that pull request's merge commit.
PENGUINCAM_SHA="REPLACE_BEFORE_MERGE"

INSTALL_DIR="/opt/penguincam-printer"
APP_DIR="$INSTALL_DIR/app"
VENV_DIR="$INSTALL_DIR/venv"
CONFIG_DIR="/etc/penguincam-printer"
# Where `run` downloads a sliced file before sending it to the printer. The daemon creates
# it if it is missing; the installer creates it once, as root, with the ownership and mode
# it should have. Kept in step with DOWNLOAD_DIR in print/printer_daemon/__main__.py.
DOWNLOAD_DIR="/var/tmp/penguincam-printer"
SERVICE_USER="penguincam"
SERVICE_NAME="penguincam-printer"
WRAPPER="/usr/local/bin/penguincam-printer"

die() { echo "error: $*" >&2; exit 1; }

if [ "$PENGUINCAM_SHA" = "REPLACE_BEFORE_MERGE" ]; then
  die "this installer has no pinned commit yet.
Set PENGUINCAM_SHA in print/printer_daemon/install.sh to the commit you want to install before
opening the pull request."
fi

[ "$(id -u)" -eq 0 ] || die "run this with sudo"

# The userland, not the kernel: a 32-bit Raspberry Pi OS on a Pi 4 or 5 runs a 64-bit
# kernel and reports uname -m as armv8l, which sails past an aarch64 test and then fails
# hours later in a compiler.
ARCH="$(dpkg --print-architecture)"
if [ "$ARCH" != "arm64" ]; then
  die "64-bit Raspberry Pi OS (arm64) is required; this one is $ARCH.
A 32-bit userland (armhf, as armv7l and armv6l images run) cannot install one of the
daemon's dependencies: Pillow publishes no 32-bit wheel, so pip would try to compile it
and fail after a long wait. Re-image with the 64-bit (aarch64) Bookworm release and run
this again."
fi

echo "==> installing packages"
export DEBIAN_FRONTEND=noninteractive
MISSING=""
for pkg in python3-venv curl tar; do
  dpkg -s "$pkg" >/dev/null 2>&1 || MISSING="$MISSING $pkg"
done
# Only when something is missing, so a re-run on a Pi that cannot reach a mirror still
# works.
if [ -n "$MISSING" ]; then
  apt-get update -qq
  # Unquoted on purpose: $MISSING is a list of package names.
  # shellcheck disable=SC2086
  apt-get install -y -qq $MISSING
else
  echo "    already installed"
fi

echo "==> creating the $SERVICE_USER user and $INSTALL_DIR"
id -u "$SERVICE_USER" >/dev/null 2>&1 || \
  useradd --system --home-dir "$INSTALL_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
mkdir -p "$APP_DIR" "$CONFIG_DIR"
# The config directory belongs to the service user from the start: `run` rewrites the file
# in place by renaming a temporary file over it, which needs write permission on the
# directory itself, not just on the file.
chown "$SERVICE_USER:$SERVICE_USER" "$CONFIG_DIR"
chmod 750 "$CONFIG_DIR"
# Nobody but the service reads a sliced file on its way to the printer.
mkdir -p "$DOWNLOAD_DIR"
chown "$SERVICE_USER:$SERVICE_USER" "$DOWNLOAD_DIR"
chmod 700 "$DOWNLOAD_DIR"

echo "==> downloading PenguinCAM $PENGUINCAM_SHA"
TARBALL="$(mktemp)"
trap 'rm -f "$TARBALL"' EXIT
curl -fsSL "https://github.com/6238/PenguinCAM/archive/$PENGUINCAM_SHA.tar.gz" -o "$TARBALL"

echo "==> extracting printer_daemon"
rm -rf "${APP_DIR:?}/printer_daemon"
# The archive holds PenguinCAM-<sha>/print/printer_daemon/...; strip both of those leading
# directories so the package lands at $APP_DIR/printer_daemon and `python -m printer_daemon`
# finds it, exactly as it did before the print/ package existed.
tar -xzf "$TARBALL" -C "$APP_DIR" --strip-components=2 '*/print/printer_daemon/*'

echo "==> creating the virtual environment"
python3 -m venv "$VENV_DIR"
# --only-binary=:all: so a future wheel gap fails here rather than starting a Pillow source
# build on a Raspberry Pi.
"$VENV_DIR/bin/pip" install --quiet --upgrade --only-binary=:all: \
  -r "$APP_DIR/printer_daemon/requirements.txt"

echo "==> writing $WRAPPER"
cat > "$WRAPPER" <<WRAPPER_EOF
#!/bin/sh
exec env PYTHONPATH="$APP_DIR" "$VENV_DIR/bin/python" -m printer_daemon "\$@"
WRAPPER_EOF
chmod 755 "$WRAPPER"

echo "==> writing the systemd unit"
cat > "/etc/systemd/system/$SERVICE_NAME.service" <<UNIT_EOF
[Unit]
Description=PenguinCAM printer daemon
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
ExecStart=$WRAPPER run
Restart=always
RestartSec=10
# The daemon reads its code and writes two directories. Everything else is off limits:
# ProtectSystem=strict makes the whole hierarchy read-only apart from ReadWritePaths, and
# PrivateTmp gives the service its own /tmp and /var/tmp.
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
# The leading dash on the download directory is not decoration: PrivateTmp means the
# service starts with an empty private /var/tmp, and a ReadWritePaths entry that does not
# exist inside the namespace stops the unit from starting. Ignored when missing, honoured
# when a future unit drops PrivateTmp.
ReadWritePaths=$CONFIG_DIR -$DOWNLOAD_DIR

[Install]
WantedBy=multi-user.target
UNIT_EOF

# The app and the virtual environment stay root-owned and world-readable. Nothing in the
# daemon writes there, and a service that can rewrite its own code is a far larger target
# than one that cannot. The chmod only restates the usual umask, so a root shell with a
# stricter one cannot leave the service unable to read its own code.
chmod -R a+rX "$INSTALL_DIR"
systemctl daemon-reload
# Enabled but NOT started: `pair` writes the config file and starts it when it finishes.
systemctl enable "$SERVICE_NAME" >/dev/null

echo
echo "==> pairing"
# This script arrived on standard input from the curl pipe, so pair must read the console
# rather than the rest of the script -- and there has to be a console to read.
: < /dev/tty 2>/dev/null || die "no terminal; run: sudo penguincam-printer pair"
"$WRAPPER" pair < /dev/tty || die "pairing did not finish; re-run: sudo penguincam-printer pair"

# Load-bearing, not decoration: `pair` chowns what it writes, but someone who runs
# `sudo penguincam-printer pair` later -- to re-pair, or with a config file left by an
# older daemon -- has no installer to do this, so the ownership the service depends on is
# set here every time the installer runs. `run` renames a temporary file over config.toml,
# which needs write permission on the directory as well as the file.
chown -R "$SERVICE_USER:$SERVICE_USER" "$CONFIG_DIR"
# Restarts only if it is already running, so the service picks up the config file and the
# ownership above instead of logging a permission error at the next boot.
systemctl try-restart "$SERVICE_NAME"

echo
echo 'Done. Run: penguincam-printer status'

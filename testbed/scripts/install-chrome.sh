#!/usr/bin/env bash
# Install Google Chrome for the automated Onshape UI run, without root (spec 5.9).
#
# Downloads Google's ARM64 Linux .deb into the test bed's state directory, unpacks it with
# dpkg-deb -x into ~/.local/opt/google-chrome/ and writes the version there as VERSION.
# Then lists the libraries the binary cannot find (ldd), and exits 0 only when the binary
# starts: --headless=new --no-sandbox --version. A Chrome that cannot start is not fatal
# to the test bed: testbed/ui_run.py falls back to /usr/bin/chromium and says so.
set -euo pipefail

URL="https://dl.google.com/linux/direct/google-chrome-stable_current_arm64.deb"
STATE="${XDG_STATE_HOME:-$HOME/.local/state}/penguincam-testbed"
DEB="$STATE/google-chrome-stable_current_arm64.deb"
DEST="$HOME/.local/opt/google-chrome"
BINARY="$DEST/opt/google/chrome/chrome"

die() { echo "install-chrome: $*" >&2; exit 1; }

[ "$(uname -s)/$(uname -m)" = "Linux/aarch64" ] || die "only Linux aarch64 is supported"
command -v dpkg-deb >/dev/null || die "dpkg-deb is needed to unpack the .deb"

mkdir -p "$STATE" "$DEST"
status="$(curl -sS -L -o "$DEB.part" -w '%{http_code}' "$URL")" || die "download of $URL failed"
if [ "$status" != "200" ]; then
    rm -f "${DEB:?}.part"
    die "download of $URL answered HTTP $status"
fi
mv "$DEB.part" "$DEB"

dpkg-deb -x "$DEB" "$DEST"
version="$(dpkg-deb -f "$DEB" Version)"
echo "$version" > "$DEST/VERSION"
echo "install-chrome: Google Chrome $version unpacked into $DEST"
[ -x "$BINARY" ] || die "no chrome binary at $BINARY after unpacking"

missing="$(ldd "$BINARY" 2>&1 | awk '/not found/ {print $1}' | sort -u)"
if [ -n "$missing" ]; then
    echo "install-chrome: libraries the binary cannot find:"
    echo "$missing" | sed 's/^/  /'
else
    echo "install-chrome: ldd finds every library"
fi

if out="$("$BINARY" --headless=new --no-sandbox --version 2>&1)"; then
    echo "install-chrome: starts: $out"
    exit 0
fi
echo "install-chrome: the binary does not start:" >&2
echo "$out" | tail -5 | sed 's/^/  /' >&2
die "Chrome cannot start here; the UI run will use /usr/bin/chromium"

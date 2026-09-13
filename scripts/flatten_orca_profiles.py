#!/usr/bin/env python3
"""Flatten Orca Slicer profile inheritance chains into self-contained JSON.

Orca's vendor profiles are chains: a Bambu printer profile inherits from a
family profile, which inherits from a base, and each file holds only the keys
it overrides. The command line does not resolve those chains: it loads exactly
the keys in the files it is given and fills the rest with built-in defaults,
silently. The desktop app's export does not help either: it writes the user
preset's differences plus its parent's name. So the profiles PenguinCAM ships
in print/profiles/ are produced by this script from the tree inside the
installed Orca build, and an integration test fails when they drift.

Usage (from the repository root, after `make install`):

    uv run python scripts/flatten_orca_profiles.py

Options:
    --profile-dir DIR   Orca's BBL vendor tree (default: inside tools/orca)
    --out DIR           Output directory (default: print/profiles)
"""
import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Output file stem -> (subdirectory of the vendor tree, profile name).
PROFILE_SOURCES = {
    "printer": ("machine", "Bambu Lab H2S 0.4 nozzle"),
    "filament": ("filament", "Bambu PLA Basic @BBL H2S"),
    "process": ("process", "0.20mm Standard @BBL H2S"),
}

# Keys that only mean something inside Orca's own preset database.
DROP_KEYS = ("instantiation", "setting_id")


def bbl_profile_dir():
    """The BBL vendor tree inside the installed Orca, Linux or macOS layout."""
    linux = REPO / "tools" / "orca" / "squashfs-root" / "resources" / "profiles" / "BBL"
    macos = REPO / "tools" / "orca" / "OrcaSlicer.app" / "Contents" / "Resources" / "profiles" / "BBL"
    for candidate in (linux, macos):
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError(f"no Orca profile tree at {linux} or {macos}; run `make install`")


def _load(profile_dir, subdir, name):
    path = Path(profile_dir) / subdir / f"{name}.json"
    if not path.is_file():
        raise FileNotFoundError(f"profile '{name}' not found at {path}")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def flatten_profile(profile_dir, subdir, name):
    """Resolve `name` in `profile_dir/subdir` into one dict, child keys over parent keys."""
    chain = []
    current = name
    seen = set()
    while current:
        if current in seen:
            raise ValueError(f"inheritance loop at '{current}' under {subdir}")
        seen.add(current)
        data = _load(profile_dir, subdir, current)
        chain.append(data)
        current = data.get("inherits", "")
    merged = {}
    for data in reversed(chain):
        merged.update(data)
    merged["inherits"] = ""
    merged["from"] = "system"
    for key in DROP_KEYS:
        merged.pop(key, None)
    return merged


def write_profiles(profile_dir, out_dir, sources=PROFILE_SOURCES):
    """Write one flattened JSON per entry of `sources` into `out_dir`."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for stem, (subdir, name) in sources.items():
        flat = flatten_profile(profile_dir, subdir, name)
        text = json.dumps(flat, indent=4, ensure_ascii=False, sort_keys=True) + "\n"
        (out_dir / f"{stem}.json").write_text(text, encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--profile-dir", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=REPO / "print" / "profiles")
    args = parser.parse_args(argv)
    profile_dir = args.profile_dir or bbl_profile_dir()
    write_profiles(profile_dir, args.out)
    for stem, (subdir, name) in PROFILE_SOURCES.items():
        print(f"{stem}.json <- {subdir}/{name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

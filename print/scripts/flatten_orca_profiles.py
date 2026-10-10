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

It writes two things: the three default profiles (print/profiles/printer.json,
filament.json, process.json), and the catalog in print/profiles/catalog/:
every printer of the H2S family, and every filament and process from the BBL
tree that names one of them, each flattened, with an index.json listing them.
Profiles are found by their `name` field through an index over the BBL tree
and Orca's separate filament library, because many BBL filaments sit in vendor
subfolders and inherit from a library base.

Two selections cannot be inherited because they are not part of any vendor
profile - the desktop app makes them from the device or the plate picker, and
the command line has no such thing. This script writes them in explicitly:

  * the High Flow nozzle variant, through `nozzle_volume_type` (see below), and
  * the Textured PEI Plate, through `curr_bed_type`.

Usage (from the repository root, after `make install`):

    uv run python print/scripts/flatten_orca_profiles.py

Options:
    --profile-dir DIR   Orca's BBL vendor tree (default: inside tools/orca)
    --out DIR           Output directory (default: print/profiles)
    --catalog-out DIR   Catalog directory (default: print/profiles/catalog)
    --no-catalog        Write the three defaults only
    --audit-only        Print the audit, write nothing
"""
import argparse
import json
import re
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent     # the print/ directory
REPO = PKG.parent
CATALOG_OUT = PKG / "profiles" / "catalog"

# The printer family the catalog covers. Another family is one more list here
# (and a `printers=` argument to build_catalog).
H2S_PRINTERS = [
    "Bambu Lab H2S 0.2 nozzle",
    "Bambu Lab H2S 0.4 nozzle",
    "Bambu Lab H2S 0.6 nozzle",
    "Bambu Lab H2S 0.8 nozzle",
]

# Catalog kind -> (subdirectory of the vendor tree, index.json list name).
CATALOG_KINDS = {
    "printer": ("machine", "printers"),
    "filament": ("filament", "filaments"),
    "process": ("process", "processes"),
}

# Output file stem -> (subdirectory of the vendor tree, profile name).
PROFILE_SOURCES = {
    "printer": ("machine", "Bambu Lab H2S 0.6 nozzle"),
    "filament": ("filament", "Generic PETG @BBL H2S"),
    "process": ("process", "0.24mm Balanced Quality @BBL H2S 0.6 nozzle"),
}

# The nozzle we print with. Orca stores every variant-aware value as an array
# parallel to the profile's *_extruder_variant array, and resolves it at slice
# time: Print::apply calls DynamicPrintConfig::update_values_to_printer_extruders,
# which asks get_index_for_extruder for the index whose variant string equals
# "<extruder_type> <nozzle_volume_type>". Nothing in the vendor profiles selects
# a variant; the machine profile only records default_nozzle_volume_type, which
# the command line never reads. Without the explicit nozzle_volume_type below,
# the CLI falls back to nvtStandard = index 0.
EXTRUDER_TYPE = "Direct Drive"
NOZZLE_VOLUME_TYPE = "High Flow"
EXTRUDER_VARIANT = f"{EXTRUDER_TYPE} {NOZZLE_VOLUME_TYPE}"

# The plate. The CLI reads curr_bed_type (a coEnum parsed through
# s_keys_map_BedType, so the name below is the reliable spelling) and picks the
# filament's bed temperature key from it: btPTE -> textured_plate_temp.
#
# default_bed_type is NOT a substitute. Preset::get_default_bed_type claims to
# accept "both numeric and string format" but runs atoi() on the value, so a
# name yields 0, fails the `> 0` test, logs "invalid bed type" and returns
# btPEI - the smooth High Temp Plate, not this one. It is also GUI-only: no CLI
# path reads it. It is written here numerically (btPTE = 4) purely so the value
# is right if a generated 3mf is ever opened in the desktop app.
BED_TYPE = "Textured PEI Plate"
BED_TYPE_ENUM = "4"

# The variant array in each profile kind, and the id array beside it. Orca
# matches EXTRUDER_VARIANT against these to pick the index for every
# variant-aware value of that kind.
VARIANT_KEYS = {
    "machine": "printer_extruder_variant",
    "filament": "filament_extruder_variant",
    "process": "print_extruder_variant",
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


def library_profile_dir():
    """Orca's filament library, the BBL tree's sibling, which BBL filaments inherit from."""
    return bbl_profile_dir().parent / "OrcaFilamentLibrary"


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def build_name_index(roots, kind):
    """Index every `root/kind/**/*.json` by its `name` field.

    Two files with one name inside one root fail the build. Across roots the
    earlier root wins: BBL.json registers its own copy of each filament the
    library also has, so the BBL file is the one Orca loads for a BBL printer.
    Returns (index, the number of later-root names shadowed).
    """
    index, shadowed = {}, 0
    for root in roots:
        local = {}
        base = Path(root) / kind
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.json")):
            name = _read(path).get("name")
            if not name:
                continue
            if name in local:
                raise ValueError(f"two {kind} profiles named '{name}' in {root}: {local[name]} and {path}")
            local[name] = path
        for name, path in local.items():
            if name in index:
                shadowed += 1
            else:
                index[name] = path
    return index, shadowed


def _roots(profile_dir):
    """The BBL tree, then the library beside it when there is one."""
    library = Path(profile_dir).parent / "OrcaFilamentLibrary"
    return [Path(profile_dir)] + ([library] if library.is_dir() else [])


def flatten_indexed(index, kind, name, provenance=None):
    """Resolve `name` through `index` into one dict, child keys over parent keys.

    When `provenance` is a dict it is filled with key -> the profile in the
    chain whose value won, which is what the audit reports.
    """
    chain = []
    current = name
    seen = set()
    while current:
        if current in seen:
            raise ValueError(f"inheritance loop at '{current}' under {kind}")
        seen.add(current)
        if current not in index:
            raise FileNotFoundError(f"{kind} profile '{current}' not found (wanted for '{name}')")
        data = _read(index[current])
        chain.append(data)
        current = data.get("inherits", "")
    merged = {}
    for data in reversed(chain):
        merged.update(data)
        if provenance is not None:
            for key in data:
                provenance[key] = data.get("name", "?")
    merged["inherits"] = ""
    merged["from"] = "system"
    for key in DROP_KEYS:
        merged.pop(key, None)
    return merged


def flatten_profile(profile_dir, subdir, name, provenance=None):
    """Resolve `name` from the tree at `profile_dir` (and the library beside it)."""
    index, _ = build_name_index(_roots(profile_dir), subdir)
    return flatten_indexed(index, subdir, name, provenance=provenance)


def apply_selections(subdir, flat, provenance=None):
    """Write the two choices no vendor profile carries into the machine profile."""
    if subdir != "machine":
        return flat
    flat["nozzle_volume_type"] = [NOZZLE_VOLUME_TYPE]
    flat["curr_bed_type"] = BED_TYPE
    flat["default_bed_type"] = BED_TYPE_ENUM
    if provenance is not None:
        for key in ("nozzle_volume_type", "curr_bed_type", "default_bed_type"):
            provenance[key] = "flatten_orca_profiles.py"
    return flat


def variant_index(subdir, flat):
    """The index EXTRUDER_VARIANT occupies in this profile's variant array.

    Raises when the profile has no variant array or does not offer the variant,
    because every variant-aware value would then silently resolve to index 0.
    """
    key = VARIANT_KEYS[subdir]
    variants = flat.get(key)
    if not isinstance(variants, list) or not variants:
        raise ValueError(f"{flat.get('name', '?')}: no {key}; cannot select {EXTRUDER_VARIANT}")
    if EXTRUDER_VARIANT not in variants:
        raise ValueError(f"{flat.get('name', '?')}: {key} has no {EXTRUDER_VARIANT!r}, only {variants}")
    return variants.index(EXTRUDER_VARIANT)


def check_selection(profiles):
    """Fail unless all three profiles resolve EXTRUDER_VARIANT to the same index."""
    indexes = {}
    for stem, (subdir, _name) in PROFILE_SOURCES.items():
        indexes[stem] = variant_index(subdir, profiles[stem])
    if len(set(indexes.values())) != 1:
        raise ValueError(f"{EXTRUDER_VARIANT} sits at different indexes per profile: {indexes}")
    machine = profiles["printer"]
    if machine.get("nozzle_volume_type") != [NOZZLE_VOLUME_TYPE]:
        raise ValueError("the machine profile does not select the nozzle volume type")
    if machine.get("extruder_type") != [EXTRUDER_TYPE]:
        raise ValueError(f"extruder_type is {machine.get('extruder_type')}, not [{EXTRUDER_TYPE!r}]")
    return next(iter(indexes.values()))


def build_profiles(profile_dir, sources=PROFILE_SOURCES):
    """Flatten every source, apply the selections, and return (profiles, provenance)."""
    profiles, provenance, indexes = {}, {}, {}
    for stem, (subdir, name) in sources.items():
        if subdir not in indexes:
            indexes[subdir], _ = build_name_index(_roots(profile_dir), subdir)
        prov = {}
        flat = flatten_indexed(indexes[subdir], subdir, name, provenance=prov)
        apply_selections(subdir, flat, provenance=prov)
        profiles[stem], provenance[stem] = flat, prov
    return profiles, provenance


def _json_text(data):
    return json.dumps(data, indent=4, ensure_ascii=False, sort_keys=True) + "\n"


def dump_profiles(profiles, out_dir):
    """Write already-built profiles as sorted, indented JSON."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for stem, flat in profiles.items():
        (out_dir / f"{stem}.json").write_text(_json_text(flat), encoding="utf-8")


def profile_file_stem(name):
    """A profile name as a file stem: anything outside [A-Za-z0-9._-] becomes `_`."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)


def _orca_version():
    stamp = REPO / "tools" / "orca" / "VERSION"
    return stamp.read_text().split()[0] if stamp.is_file() else "unknown"


def build_catalog(bbl_dir, library_dir, printers=H2S_PRINTERS, orca_version=None):
    """Flatten `printers` and every BBL filament and process that names one of them.

    A profile whose variant array lacks EXTRUDER_VARIANT is skipped with its
    reason. Per printer, a filament or process whose variant index differs from
    the printer's is dropped for that printer (today's check_selection, for
    every combination). Returns {'profiles': {kind: {name: flat}}, 'index': the
    index.json content, 'shadowed': {kind: count}}.
    """
    bbl_dir = Path(bbl_dir)
    roots = [bbl_dir, Path(library_dir)]
    profiles = {kind: {} for kind in CATALOG_KINDS}
    index = {"orca_version": orca_version or _orca_version(),
             "printers": [], "filaments": [], "processes": [], "skipped": [], "dropped": []}
    shadowed = {}

    machines, shadowed["printer"] = build_name_index(roots, "machine")
    printer_index = {}
    for name in printers:
        flat = apply_selections("machine", flatten_indexed(machines, "machine", name))
        try:
            printer_index[name] = variant_index("machine", flat)
        except ValueError as exc:
            index["skipped"].append({"kind": "printer", "name": name, "reason": str(exc)})
            continue
        profiles["printer"][name] = flat

    for kind in ("filament", "process"):
        subdir, list_name = CATALOG_KINDS[kind]
        names, shadowed[kind] = build_name_index(roots, subdir)
        for name, path in sorted(names.items()):
            if not path.is_relative_to(bbl_dir) or _read(path).get("instantiation") != "true":
                continue
            flat = flatten_indexed(names, subdir, name)
            compatible = flat.get("compatible_printers") or []
            wanted = [p for p in printers if p in compatible]
            if not wanted:
                continue
            try:
                own = variant_index(subdir, flat)
            except ValueError as exc:
                index["skipped"].append({"kind": kind, "name": name, "reason": str(exc)})
                continue
            fits = []
            for printer in wanted:
                if printer not in printer_index:
                    continue
                if printer_index[printer] != own:
                    index["dropped"].append({
                        "printer": printer, "kind": kind, "name": name,
                        "reason": f"{EXTRUDER_VARIANT} is index {own} here and "
                                  f"{printer_index[printer]} on the printer"})
                    continue
                fits.append(printer)
            if fits:
                profiles[kind][name] = flat
                index[list_name].append({"name": name, "compatible_printers": fits})

    index["printers"] = [{"name": name} for name in profiles["printer"]]
    for kind, (_subdir, list_name) in CATALOG_KINDS.items():
        stems = {}
        for entry in index[list_name]:
            stem = profile_file_stem(entry["name"])
            if stem in stems:
                raise ValueError(f"{kind} profiles '{stems[stem]}' and '{entry['name']}' share the file stem {stem}")
            stems[stem] = entry["name"]
            entry["file"] = f"{kind}/{stem}.json"
        index[list_name].sort(key=lambda e: e["name"])
    return {"profiles": profiles, "index": index, "shadowed": shadowed}


def write_catalog(catalog, out_dir):
    """Write the catalog's files and index.json, removing files no longer in it."""
    out_dir = Path(out_dir)
    for kind, (_subdir, list_name) in CATALOG_KINDS.items():
        kind_dir = out_dir / kind
        kind_dir.mkdir(parents=True, exist_ok=True)
        wanted = set()
        for entry in catalog["index"][list_name]:
            path = out_dir / entry["file"]
            wanted.add(path.name)
            path.write_text(_json_text(catalog["profiles"][kind][entry["name"]]), encoding="utf-8")
        for stale in kind_dir.glob("*.json"):
            if stale.name not in wanted:
                stale.unlink()
    (out_dir / "index.json").write_text(_json_text(catalog["index"]), encoding="utf-8")


def write_profiles(profile_dir, out_dir, sources=PROFILE_SOURCES):
    """Flatten every entry of `sources`, check the selection, write it to `out_dir`."""
    profiles, _ = build_profiles(profile_dir, sources)
    if sources is PROFILE_SOURCES:
        check_selection(profiles)
    dump_profiles(profiles, out_dir)
    return profiles


def _at(value, index):
    """The variant-aware element of `value`, or the value itself when it is scalar."""
    if isinstance(value, list):
        if len(value) > index:
            return value[index]
        if len(value) == 1:
            return value[0]
    return value


def audit_rows(profiles, provenance, index):
    """The (field, value, source) rows the audit prints, in reading order."""
    machine, filament, process = profiles["printer"], profiles["filament"], profiles["process"]
    mprov, fprov, pprov = provenance["printer"], provenance["filament"], provenance["process"]
    variants = machine["printer_extruder_variant"]

    def row(label, value, source):
        return (label, "null" if value is None else str(value), source)

    return [
        row("nozzle diameter", _at(machine.get("nozzle_diameter"), 0) + " mm", mprov.get("nozzle_diameter")),
        row("nozzle type", _at(machine.get("nozzle_type"), index), mprov.get("nozzle_type")),
        row("nozzle volume type", _at(machine.get("nozzle_volume_type"), 0), mprov.get("nozzle_volume_type")),
        row("extruder variant", f"{variants[index]}  (index {index} of {len(variants)})",
            mprov.get("printer_extruder_variant")),
        row("nozzle volume", _at(machine.get("nozzle_volume"), index) + " mm3", mprov.get("nozzle_volume")),
        row("layer height", _at(process.get("layer_height"), 0) + " mm", pprov.get("layer_height")),
        row("initial layer height", _at(process.get("initial_layer_print_height"), 0) + " mm",
            pprov.get("initial_layer_print_height")),
        row("filament", _at(filament.get("filament_type"), 0), fprov.get("filament_type")),
        row("filament MVS", _at(filament.get("filament_max_volumetric_speed"), index) + " mm3/s",
            fprov.get("filament_max_volumetric_speed")),
        row("nozzle temperature", _at(filament.get("nozzle_temperature"), index) + " C",
            fprov.get("nozzle_temperature")),
        row("nozzle temperature, initial layer",
            _at(filament.get("nozzle_temperature_initial_layer"), index) + " C",
            fprov.get("nozzle_temperature_initial_layer")),
        row("filament flow ratio", _at(filament.get("filament_flow_ratio"), index),
            fprov.get("filament_flow_ratio")),
        row("bed type", machine.get("curr_bed_type"), mprov.get("curr_bed_type")),
        row("textured plate temperature", _at(filament.get("textured_plate_temp"), 0) + " C",
            fprov.get("textured_plate_temp")),
        row("textured plate temperature, initial layer",
            _at(filament.get("textured_plate_temp_initial_layer"), 0) + " C",
            fprov.get("textured_plate_temp_initial_layer")),
    ]


def print_audit(profiles, provenance, index, out=sys.stdout):
    """Print the resolved values and the profile each one came from."""
    rows = audit_rows(profiles, provenance, index)
    width = max(len(label) for label, _v, _s in rows)
    vwidth = max(len(value) for _l, value, _s in rows)
    print(f"\nSelected variant: {EXTRUDER_VARIANT} (index {index})", file=out)
    print(f"{'field'.ljust(width)}  {'value'.ljust(vwidth)}  from", file=out)
    print(f"{'-' * width}  {'-' * vwidth}  {'-' * 40}", file=out)
    for label, value, source in rows:
        print(f"{label.ljust(width)}  {value.ljust(vwidth)}  {source}", file=out)

    differing = []
    for stem, (subdir, _name) in PROFILE_SOURCES.items():
        vkey = VARIANT_KEYS[subdir]
        count = len(profiles[stem].get(vkey, []))
        for key, value in sorted(profiles[stem].items()):
            if key != vkey and isinstance(value, list) and len(value) == count > 1 and len(set(value)) > 1:
                differing.append((stem, key, value))
    print("\nVariant-aware values that actually differ between Standard and High Flow:", file=out)
    if not differing:
        print("  (none: every variant-aware value is the same for both nozzles)", file=out)
    for stem, key, value in differing:
        chosen = value[index]
        print(f"  {stem}.{key}: {value} -> {chosen}", file=out)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--profile-dir", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=PKG / "profiles")
    parser.add_argument("--catalog-out", type=Path, default=CATALOG_OUT)
    parser.add_argument("--no-catalog", action="store_true", help="write the three defaults only")
    parser.add_argument("--audit-only", action="store_true", help="print the audit, write nothing")
    args = parser.parse_args(argv)
    profile_dir = args.profile_dir or bbl_profile_dir()

    profiles, provenance = build_profiles(profile_dir)
    index = check_selection(profiles)
    if not args.audit_only:
        dump_profiles(profiles, args.out)
        for stem, (subdir, name) in PROFILE_SOURCES.items():
            print(f"{stem}.json <- {subdir}/{name}")
    print_audit(profiles, provenance, index)
    if not args.no_catalog and not args.audit_only:
        catalog = build_catalog(profile_dir, Path(profile_dir).parent / "OrcaFilamentLibrary")
        write_catalog(catalog, args.catalog_out)
        print_catalog_log(catalog)
    return 0


def print_catalog_log(catalog, out=sys.stdout):
    """The build log: counts, the shadowed names, every skipped profile and dropped pair."""
    idx = catalog["index"]
    print(f"\nCatalog: {len(idx['printers'])} printers, {len(idx['filaments'])} filaments, "
          f"{len(idx['processes'])} processes", file=out)
    shadowed = ", ".join(f"{kind} {count}" for kind, count in catalog["shadowed"].items())
    print(f"Library names shadowed by the BBL tree: {shadowed}", file=out)
    print(f"Skipped ({len(idx['skipped'])}):", file=out)
    for entry in idx["skipped"]:
        print(f"  {entry['kind']} {entry['name']}: {entry['reason']}", file=out)
    print(f"Dropped ({len(idx['dropped'])}):", file=out)
    for entry in idx["dropped"]:
        print(f"  {entry['printer']} / {entry['kind']} {entry['name']}: {entry['reason']}", file=out)


if __name__ == "__main__":
    sys.exit(main())

"""The team's printing configuration, read against the profile catalog, and the four
student overrides. Flask-free.

The team YAML's `printing:` section names a printer, its filaments and its processes by
Orca's own profile names (print/profiles/catalog/index.json lists them). A team with no
`printer:` key prints with the default set in print/profiles/. A printer name the catalog
does not know fails closed: no fallback, slicing disabled. A filament or process the
catalog does not know, or that does not fit the printer, is dropped with a warning.
See docs/3D_PRINTING.md, "Team printing configuration".
"""
import functools
import json
from dataclasses import dataclass, field
from pathlib import Path

from team_config import TeamConfig

from .slicer import DEFAULT_PROFILES, PROFILE_DIR, SliceProfiles

PKG = Path(__file__).resolve().parent      # the print/ directory
CATALOG_DIR = PKG / 'profiles' / 'catalog'

UNKNOWN_PRINTER_MESSAGE = ("The team configuration names printer '{name}', which PenguinCAM does not know. "
                           "Ask a mentor to fix printing.printer.")
DROPPED_MESSAGE = ("printing.{key} names '{name}', which PenguinCAM does not know or which does not fit "
                   "printer '{printer}'; it was left out.")
EMPTY_LIST_MESSAGE = ("The team configuration lists no {what} that fits printer '{printer}'. "
                      "Ask a mentor to fix printing.{key}.")
BAD_OVERRIDES_MESSAGE = "These print settings could not be read; choose them from the lists."
OVERRIDES_NOT_ALLOWED_MESSAGE = "Your team does not allow changing print settings."

# Catalog kind -> index.json list name.
_LISTS = {'printer': 'printers', 'filament': 'filaments', 'process': 'processes'}

OVERRIDE_CHOICES = {
    'infill': [10, 15, 20, 30, 40, 60, 100],
    'walls': [2, 3, 4, 6],
    'supports': ['off', 'on'],
    'brim': ['auto', 'off', 'outer'],
}
_SUPPORTS = {'off': {'enable_support': '0'}, 'on': {'enable_support': '1', 'support_type': 'tree(auto)'}}
_BRIM = {'auto': 'auto_brim', 'off': 'no_brim', 'outer': 'outer_only'}


class Catalog:
    """print/profiles/catalog/index.json, by kind and name."""

    def __init__(self, directory, index):
        self.directory = Path(directory)
        self.orca_version = index.get('orca_version')
        self._entries = {kind: {e['name']: e for e in index.get(list_name, [])}
                         for kind, list_name in _LISTS.items()}

    @classmethod
    def load(cls, directory=CATALOG_DIR):
        with open(Path(directory) / 'index.json', encoding='utf-8') as fh:
            return cls(directory, json.load(fh))

    def has_printer(self, name):
        return isinstance(name, str) and name in self._entries['printer']

    def profile_path(self, kind, name):
        return self.directory / self._entries[kind][name]['file']

    def compatible(self, kind, name, printer):
        entry = self._entries[kind].get(name) if isinstance(name, str) else None
        return entry is not None and printer in entry.get('compatible_printers', [])


@functools.lru_cache(maxsize=1)
def load_catalog():
    """The catalog, read once per process: it is committed and changes only with a deploy."""
    return Catalog.load()


@dataclass
class PrintChoices:
    printer: str
    filaments: list[str]
    processes: list[str]
    allow_overrides: bool
    warnings: list[str] = field(default_factory=list)
    error: str | None = None
    skipped: list[str] = field(default_factory=list)
    is_default: bool = False
    # (kind, name) -> profile file. Never sent to the browser.
    paths: dict = field(default_factory=dict, repr=False)

    def to_json(self):
        return {'printer': self.printer, 'filaments': list(self.filaments), 'processes': list(self.processes),
                'allow_overrides': self.allow_overrides, 'warnings': list(self.warnings), 'error': self.error,
                'skipped': list(self.skipped), 'is_default': self.is_default}

    def slice_profiles(self, filament, process):
        """The three files for a slice. Raises ValueError when the configuration has an
        error, or when the filament or process is not one this team offers."""
        if self.error:
            raise ValueError(self.error)
        if filament not in self.filaments or process not in self.processes:
            raise ValueError(f"filament {filament!r} or process {process!r} is not offered")
        if self.is_default:
            return DEFAULT_PROFILES
        return SliceProfiles(self.paths[('printer', self.printer)], self.paths[('filament', filament)],
                             self.paths[('process', process)])

    def printer_profile(self):
        """The printer's flattened profile, for plate_from_printer. With a configuration
        error there is no printer to slice for; the default printer's plate is drawn."""
        path = self.paths.get(('printer', self.printer)) or DEFAULT_PROFILES.printer
        with open(path, encoding='utf-8') as fh:
            return json.load(fh)


def _profile_name(path):
    with open(path, encoding='utf-8') as fh:
        return json.load(fh)['name']


def default_choices():
    """The default set: the three files in print/profiles/, overrides allowed."""
    printer, filament, process = (_profile_name(PROFILE_DIR / f'{kind}.json')
                                  for kind in ('printer', 'filament', 'process'))
    return PrintChoices(printer=printer, filaments=[filament], processes=[process], allow_overrides=True,
                        is_default=True,
                        paths={('printer', printer): DEFAULT_PROFILES.printer,
                               ('filament', filament): DEFAULT_PROFILES.filament,
                               ('process', process): DEFAULT_PROFILES.process})


def resolve_print_config(printing, catalog):
    """PrintChoices for a raw `printing:` section (or None)."""
    if not isinstance(printing, dict) or 'printer' not in printing:
        return default_choices()
    allow = printing.get('allow_overrides', True)
    allow = allow if isinstance(allow, bool) else True
    printer = printing['printer']
    if not catalog.has_printer(printer):
        name = printer if isinstance(printer, str) else str(printer)
        return PrintChoices(printer=name, filaments=[], processes=[], allow_overrides=allow,
                            error=UNKNOWN_PRINTER_MESSAGE.format(name=name))
    choices = PrintChoices(printer=printer, filaments=[], processes=[], allow_overrides=allow,
                           paths={('printer', printer): catalog.profile_path('printer', printer)})
    for kind, key, target, what in (('filament', 'filaments', choices.filaments, 'filament'),
                                    ('process', 'processes', choices.processes, 'process')):
        names = printing.get(key)
        for name in names if isinstance(names, list) else []:
            if catalog.compatible(kind, name, printer):
                if name not in target:
                    target.append(name)
                    choices.paths[(kind, name)] = catalog.profile_path(kind, name)
                continue
            text = name if isinstance(name, str) else str(name)
            choices.warnings.append(DROPPED_MESSAGE.format(key=key, name=text, printer=printer))
            choices.skipped.append(text)
        if not target and choices.error is None:
            choices.error = EMPTY_LIST_MESSAGE.format(what=what, printer=printer, key=key)
    return choices


def choices_for_request(session_data, force_defaults):
    """The request's PrintChoices. `session_data` is session['team_config_data'];
    `force_defaults` is True for the upload mode, which always prints with the defaults."""
    if force_defaults:
        return default_choices()
    return resolve_print_config(TeamConfig.from_dict(session_data or {}).printing_section(), load_catalog())


class OverrideError(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.message = message


def _choice(raw, key):
    """The value for `key` if it is exactly one of the listed choices (type included)."""
    value = raw[key]
    for allowed in OVERRIDE_CHOICES[key]:
        if type(value) is type(allowed) and value == allowed:
            return value
    raise OverrideError(BAD_OVERRIDES_MESSAGE)


def validate_overrides(raw, allowed):
    """The browser's overrides as Orca process keys. Only the four keys and their listed
    values; a non-empty set when the team does not allow overrides is refused."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise OverrideError(BAD_OVERRIDES_MESSAGE)
    if raw and not allowed:
        raise OverrideError(OVERRIDES_NOT_ALLOWED_MESSAGE)
    if any(key not in OVERRIDE_CHOICES for key in raw):
        raise OverrideError(BAD_OVERRIDES_MESSAGE)
    orca = {}
    if 'infill' in raw:
        orca['sparse_infill_density'] = f"{_choice(raw, 'infill')}%"
    if 'walls' in raw:
        orca['wall_loops'] = str(_choice(raw, 'walls'))
    if 'supports' in raw:
        orca.update(_SUPPORTS[_choice(raw, 'supports')])
    if 'brim' in raw:
        orca['brim_type'] = _BRIM[_choice(raw, 'brim')]
    return orca


def write_process_with_overrides(process_path, overrides, out_path):
    """The process profile with `overrides` merged over it, written to `out_path`."""
    with open(process_path, encoding='utf-8') as fh:
        process = json.load(fh)
    process.update(overrides)
    out_path = Path(out_path)
    out_path.write_text(json.dumps(process, indent=4, ensure_ascii=False, sort_keys=True) + '\n', encoding='utf-8')
    return out_path

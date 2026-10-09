"""The drift report: a fresh recording compared with the stored one, and accept (spec 5.8).

API traffic must be the same requests in the same order, with the same statuses and
response bodies of the same structure (keys and value types). Exported meshes compare by
bounding box and triangle count. Message logs must hold the same message names, fields and
value types. A difference is a 'request' when PenguinCAM changed what it sends, and a
'response' when only Onshape's answer changed.
"""
import difflib
import json
import shutil
import struct
from dataclasses import dataclass
from pathlib import Path

from print.slicer import stl_bounds
from testbed import settings
from testbed.adapters import VOLATILE_KEYS, _normal_query
from testbed.cassette import Cassette
from testbed.scrub import host_is_onshape

# Compared by type only: they change on every rebuild or edit.
COMPARED_BY_TYPE = VOLATILE_KEYS | {'id'}
# Made by the OAuth sign-in, so a recording made through it may hold them and another not.
SIGN_IN_PATH = '/users/sessioninfo'
MESH_TOLERANCE_MM = 0.01
_REDIRECTS = (301, 302, 303, 307, 308)
# Onshape exports in inches unless the request asks otherwise, whatever the document's units.
_MM_PER_UNIT = {'millimeter': 1.0, 'centimeter': 10.0, 'meter': 1000.0, 'inch': 25.4,
                'foot': 304.8, 'yard': 914.4}
_KIND_MEANING = {'request': 'PenguinCAM changed', 'response': 'Onshape changed'}


@dataclass(frozen=True)
class Difference:
    scenario: str
    index: int              # the exchange's (or message's) position in the fresh recording
    kind: str               # 'request' or 'response'
    detail: str


def _type_name(value):
    if value is None:
        return 'null'
    if isinstance(value, bool):
        return 'bool'
    if isinstance(value, (int, float)):
        return 'number'                     # JSON does not tell 2 from 2.0
    if isinstance(value, str):
        return 'string'
    if isinstance(value, dict):
        return 'object'
    if isinstance(value, list):
        return 'array'
    return type(value).__name__


def json_shape(value, key=None):
    """The structure of a JSON value: keys kept, a list as its first element, values as types.

    A key in COMPARED_BY_TYPE is its value's type alone, even when that is an object.
    """
    if key in COMPARED_BY_TYPE:
        return _type_name(value)
    if isinstance(value, dict):
        return {k: json_shape(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [json_shape(value[0])] if value else []
    return _type_name(value)


def _shape_differences(stored, fresh, where='$'):
    if isinstance(stored, dict) and isinstance(fresh, dict):
        for key in sorted(stored.keys() - fresh.keys()):
            yield f'{where}.{key} is gone'
        for key in sorted(fresh.keys() - stored.keys()):
            yield f'{where}.{key} is new'
        for key in sorted(stored.keys() & fresh.keys()):
            yield from _shape_differences(stored[key], fresh[key], f'{where}.{key}')
    elif isinstance(stored, list) and isinstance(fresh, list):
        if stored and fresh:                # an empty list says nothing about its items
            yield from _shape_differences(stored[0], fresh[0], f'{where}[0]')
    elif stored != fresh:
        def shown(shape):
            return shape if isinstance(shape, str) else _type_name(shape)
        yield f'{where} was {shown(stored)}, now {shown(fresh)}'


# ---------------------------------------------------------------------------
# Cassettes
# ---------------------------------------------------------------------------

def _request_form(text):
    """A request body as compared: JSON values exact but for COMPARED_BY_TYPE, else the text."""
    if not text:
        return None
    try:
        value = json.loads(text)
    except ValueError:
        return text

    def form(item, key=None):
        if key in COMPARED_BY_TYPE:
            return _type_name(item)
        if isinstance(item, dict):
            return {k: form(v, k) for k, v in item.items()}
        if isinstance(item, list):
            return [form(v) for v in item]
        return item
    return form(value)


def _identity(ex):
    # A foreign host's path is often a signed object key, so only the host identifies it.
    if host_is_onshape(ex.host):
        return (ex.method.upper(), ex.host.lower(), ex.path)
    return (ex.method.upper(), ex.host.lower(), '')


def _shown(ex):
    return f'{ex.method} {ex.host}{ex.path}'


def _follows_redirect(exchanges, index):
    return index > 0 and exchanges[index - 1].status in _REDIRECTS


def _export_scale(exchanges, index):
    """Millimetres per unit of the mesh exchange `index`: the units the chain's start asked for."""
    while _follows_redirect(exchanges, index):
        index -= 1
    units = (exchanges[index].query or {}).get('units', ['inch'])[0]
    return _MM_PER_UNIT.get(units, 1.0)


def _mesh(ex):
    """(lo, hi, triangle count) when the body is an STL, else None."""
    data = ex.body()
    if ex.body_bytes is None and not data.lstrip().startswith(b'solid'):
        return None
    try:
        return stl_bounds(data)
    except (ValueError, struct.error):
        return None


def _mesh_differences(stored, fresh, stored_scale, fresh_scale):
    (s_lo, s_hi, s_count), (f_lo, f_hi, f_count) = stored, fresh
    if s_count != f_count:
        yield f'mesh triangle count was {s_count}, now {f_count}'
    s_box = [v * stored_scale for v in s_lo + s_hi]
    f_box = [v * fresh_scale for v in f_lo + f_hi]
    if any(abs(s - f) > MESH_TOLERANCE_MM for s, f in zip(s_box, f_box)):
        def shown(box):
            return '(' + ', '.join(f'{v:.2f}' for v in box) + ') mm'
        yield f'mesh bounding box was {shown(s_box)}, now {shown(f_box)}'


def _body_differences(stored_list, si, fresh_list, fi):
    stored, fresh = stored_list[si], fresh_list[fi]
    stored_mesh, fresh_mesh = _mesh(stored), _mesh(fresh)
    if stored_mesh and fresh_mesh:
        yield from _mesh_differences(stored_mesh, fresh_mesh, _export_scale(stored_list, si),
                                     _export_scale(fresh_list, fi))
        return
    if stored.body_bytes is not None or fresh.body_bytes is not None:
        if stored.body() != fresh.body():
            yield f'binary body changed ({len(stored.body())} bytes, now {len(fresh.body())})'
        return
    try:
        stored_json, fresh_json = json.loads(stored.body_text), json.loads(fresh.body_text)
    except (TypeError, ValueError):
        if (stored.body_text or '') != (fresh.body_text or ''):
            yield 'body text changed'
        return
    yield from _shape_differences(json_shape(stored_json), json_shape(fresh_json))


def _request_differences(stored, fresh):
    if _identity(stored) != _identity(fresh):
        yield f'{_shown(stored)} is now {_shown(fresh)}'
        return
    if host_is_onshape(fresh.host) and \
            _normal_query(stored.query or {}) != _normal_query(fresh.query or {}):
        yield f'{_shown(fresh)}: query {stored.query} is now {fresh.query}'
    if _request_form(stored.request_body) != _request_form(fresh.request_body):
        yield f'{_shown(fresh)}: request body changed'


def _compare_pair(scenario, stored_list, si, fresh_list, fi):
    stored, fresh = stored_list[si], fresh_list[fi]
    requests = list(_request_differences(stored, fresh))
    if requests:
        # A redirect hop's request is whatever Location the last answer gave.
        kind = 'response' if _follows_redirect(fresh_list, fi) else 'request'
        return [Difference(scenario, fi, kind, detail) for detail in requests]
    if stored.status != fresh.status:
        return [Difference(scenario, fi, 'response',
                           f'{_shown(fresh)}: status {stored.status} is now {fresh.status}')]
    return [Difference(scenario, fi, 'response', f'{_shown(fresh)}: {detail}')
            for detail in _body_differences(stored_list, si, fresh_list, fi)]


def _is_sign_in(ex):
    return ex.path.endswith(SIGN_IN_PATH)


def diff_cassettes(stored: Cassette, fresh: Cassette) -> list:
    """Every Difference between the stored and the fresh recording of one scenario."""
    scenario = fresh.scenario
    old, new = stored.exchanges, fresh.exchanges
    matcher = difflib.SequenceMatcher(None, [_identity(ex) for ex in old],
                                      [_identity(ex) for ex in new], autojunk=False)
    diffs = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == 'equal':
            pairs = list(zip(range(i1, i2), range(j1, j2)))
            gone, added = [], []
        else:
            # A sign-in call in only one recording is no drift.
            olds = [i for i in range(i1, i2) if not _is_sign_in(old[i])]
            news = [j for j in range(j1, j2) if not _is_sign_in(new[j])]
            pairs = list(zip(olds, news))
            gone, added = olds[len(pairs):], news[len(pairs):]
        for i, j in pairs:
            diffs.extend(_compare_pair(scenario, old, i, new, j))
        for i in gone:
            diffs.append(Difference(scenario, j2, 'request',
                                    f'{_shown(old[i])} (stored #{i}) is no longer made'))
        for j in added:
            diffs.append(Difference(scenario, j, 'request', f'{_shown(new[j])} is new'))
    return sorted(diffs, key=lambda d: d.index)


# ---------------------------------------------------------------------------
# Message logs
# ---------------------------------------------------------------------------

def _message_name(event):
    data = event.get('data')
    return data.get('messageName') if isinstance(data, dict) else None


def _message_kind(*events):
    # A message the panel sent is PenguinCAM's; one it received is Onshape's.
    return 'request' if any(e.get('dir') == 'out' for e in events) else 'response'


def _shown_message(event):
    return f"{event.get('dir')} {_message_name(event) or _type_name(event.get('data'))}"


def diff_message_logs(stored: dict, fresh: dict) -> list:
    """Every Difference between two message logs of one scenario, step by step."""
    scenario = fresh['scenario']
    old_steps = {entry['step']: entry['events'] for entry in stored['steps']}
    diffs, offset = [], 0
    for entry in fresh['steps']:
        step, new = entry['step'], entry['events']
        if step not in old_steps:
            diffs.append(Difference(scenario, offset, 'request', f'step {step!r} is new'))
            offset += len(new)
            continue
        old = old_steps[step]
        matcher = difflib.SequenceMatcher(
            None, [(e.get('dir'), _message_name(e)) for e in old],
            [(e.get('dir'), _message_name(e)) for e in new], autojunk=False)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            pairs = list(zip(range(i1, i2), range(j1, j2)))
            for i, j in pairs:
                a, b = old[i], new[j]
                where = f'step {step!r}: {_shown_message(b)}'
                if tag != 'equal':
                    diffs.append(Difference(scenario, offset + j, _message_kind(a, b),
                                            f'{where} was {_shown_message(a)}'))
                    continue
                for detail in _shape_differences(json_shape(a.get('data')),
                                                 json_shape(b.get('data'))):
                    diffs.append(Difference(scenario, offset + j, _message_kind(b),
                                            f'{where}: {detail}'))
            for i in range(i1 + len(pairs), i2):
                diffs.append(Difference(scenario, offset + j2, _message_kind(old[i]),
                                        f'step {step!r}: {_shown_message(old[i])} is gone'))
            for j in range(j1 + len(pairs), j2):
                diffs.append(Difference(scenario, offset + j, _message_kind(new[j]),
                                        f'step {step!r}: {_shown_message(new[j])} is new'))
        offset += len(new)
    fresh_steps = {entry['step'] for entry in fresh['steps']}
    for step in old_steps:
        if step not in fresh_steps:
            diffs.append(Difference(scenario, offset, 'request', f'step {step!r} is gone'))
    return diffs


# ---------------------------------------------------------------------------
# The fresh recordings, the report and accept
# ---------------------------------------------------------------------------

def fresh_scenarios() -> list:
    """The scenarios with a fresh cassette or message log, sorted."""
    names = set()
    for directory in (settings.fresh_cassette_dir(), settings.fresh_message_dir()):
        if directory.is_dir():
            names.update(path.stem for path in directory.glob('*.json'))
    return sorted(names)


def diff_fresh_recordings(scenarios=None) -> list:
    """Compare each fresh recording (of `scenarios`, default all) with the stored one."""
    diffs = []
    for name in fresh_scenarios() if scenarios is None else scenarios:
        pairs = (
            (settings.fresh_cassette_dir(), settings.CASSETTE_DIR, 'cassette',
             lambda d, n: Cassette.load(d, n), diff_cassettes),
            (settings.fresh_message_dir(), settings.MESSAGE_DIR, 'message log',
             lambda d, n: json.loads((d / f'{n}.json').read_text()), diff_message_logs),
        )
        for fresh_dir, stored_dir, what, load, compare in pairs:
            if not (fresh_dir / f'{name}.json').is_file():
                continue
            if not (Path(stored_dir) / f'{name}.json').is_file():
                diffs.append(Difference(name, 0, 'request',
                                        f'no stored {what} to compare with; accept to store it'))
                continue
            diffs.extend(compare(load(stored_dir, name), load(fresh_dir, name)))
    return diffs


def _cell(text):
    return str(text).replace('|', '\\|').replace('\n', ' ')


def write_drift_report(diffs, path, compared=None) -> None:
    """Write the Markdown report: `clean` when `diffs` is empty, else a table per scenario.

    `compared`, the scenarios compared, is listed when given.
    """
    lines = ['# Drift report', '']
    if compared is not None:
        lines += [f"Compared: {', '.join(compared) or 'nothing'}.", '']
    if not diffs:
        lines.append('clean: every fresh recording matches the stored one.')
    else:
        lines += [f'{len(diffs)} differences. A request difference means PenguinCAM changed '
                  'what it sends; a response difference means Onshape changed its answer.']
        for scenario in sorted({d.scenario for d in diffs}):
            lines += ['', f'## {scenario}', '', '| # | Kind | Difference |', '|---|---|---|']
            for d in diffs:
                if d.scenario == scenario:
                    lines.append(f'| {d.index} | {d.kind} ({_KIND_MEANING[d.kind]}) | '
                                 f'{_cell(d.detail)} |')
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('\n'.join(lines) + '\n')


def _replace(source, target):
    if target.is_dir():
        shutil.rmtree(target)
    elif target.exists():
        target.unlink()
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(target))
    return target


def accept_fresh_recording(scenario) -> list:
    """Move the fresh cassette, its binaries and message log over the stored ones.

    Returns the paths written. The stored binaries go even when the fresh cassette has none,
    so none is left orphaned.
    """
    if not scenario or '/' in scenario or '\\' in scenario or scenario.startswith('.'):
        raise ValueError(f'not a scenario name: {scenario!r}')
    fresh_cassettes, fresh_messages = settings.fresh_cassette_dir(), settings.fresh_message_dir()
    cassette = fresh_cassettes / f'{scenario}.json'
    message_log = fresh_messages / f'{scenario}.json'
    if not cassette.is_file() and not message_log.is_file():
        raise FileNotFoundError(f'no fresh recording of {scenario} in {settings.FRESH_DIR}')
    moved = []
    if cassette.is_file():
        stored_dir = Path(settings.CASSETTE_DIR)
        binaries, stored_binaries = fresh_cassettes / scenario, stored_dir / scenario
        if binaries.is_dir():
            moved.append(_replace(binaries, stored_binaries))
        elif stored_binaries.is_dir():
            shutil.rmtree(stored_binaries)
        moved.append(_replace(cassette, stored_dir / f'{scenario}.json'))
    if message_log.is_file():
        moved.append(_replace(message_log, Path(settings.MESSAGE_DIR) / f'{scenario}.json'))
    return moved

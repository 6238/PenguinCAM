"""Scenario definitions and the part-export runner (spec 5.7 and 6).

A scenario is a named, scripted run. Those with a panel part are a list of steps the
checklist strip shows on the print page: each step has an instruction for whoever drives
the panel (the owner, or the automated Onshape UI run) and, optionally, the action button
the strip offers for it. The fake Onshape host finds a selection's answer by the step's
name, so a selection step is named `select part <name>` or `select parts <a>, <b>`.

The print scenarios drive the print page's own Onshape messaging (print_onshape.js): the
strip only shows their instructions. Only part-export and print-export make API calls of
their own; run_scenario_api() runs them.
"""
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import quote

from print.limits import MAX_PART_BYTES
from print.onshape_parts import (STL_ACCEPT, ElementCache, export_part_mesh,
                                 fetch_following_redirects, resolve_selections, rest_address)
from testbed import settings

# The test documents of spec 5.1, by the name that is their key in documents.json.
TEST_DOCUMENT_NAMES = ('tb-box', 'tb-two-parts', 'tb-inch', 'tb-sideways', 'tb-oversized',
                       'tb-assembly', 'tb-configured')
# Test documents whose element is an assembly, not a Part Studio: no part STL export path.
ASSEMBLY_DOCUMENTS = ('tb-assembly',)
# Test documents with a configured Part Studio. part-export leaves them out: without a
# configuration it would export only the default; print-configured-part exercises them.
CONFIGURED_DOCUMENTS = ('tb-configured',)
# What a step's button does. The first five and ask-unbounded send a panel message from
# the strip; choose-print and back are done in the panel itself, and print-parts (Next to
# the Parts step, which arms the page's own selection) and print-dialog (Add from another
# document...) in the print page, so the strip shows only their instruction.
ACTIONS = ('ask-part', 'ask-two-parts', 'dialog-part', 'dialog-parts', 'close-dialog',
           'choose-print', 'back', 'print-parts', 'print-dialog', 'ask-unbounded')

MM_BINARY = {'units': 'millimeter', 'mode': 'binary'}


@dataclass
class Step:
    name: str
    instruction: str
    action: Optional[str] = None
    select: list = field(default_factory=list)      # the part names to select in Onshape
    # Recorded for reference only: replay skips the step and leaves it out of the comparison.
    recorded_only: bool = False


@dataclass
class Scenario:
    name: str
    document: str
    panel: bool
    steps: list
    estimate: int                                   # counted Onshape calls, for the budget


def _open_panel(document):
    return Step('open panel', f'Open {document}, open the PenguinCAM panel and connect '
                              'through Onshape when it asks.')


def _choose_print():
    return Step('choose print', 'Choose 3D Printing in the panel. This strip appears on the '
                                'print page.', 'choose-print')


def _select(parts, where="Onshape's parts list or feature tree"):
    noun = 'part' if len(parts) == 1 else 'parts'
    shown = ', '.join(parts)
    return Step(f'select {noun} {shown}', f'Select {shown} in {where}.', None, list(parts))


def _close_dialog():
    return Step('close dialog', 'Press Close the dialog.', 'close-dialog')


def _print_parts():
    return Step('print parts', 'Press Next on the print page to reach the Parts step. The page '
                               'asks Onshape for a part itself.', 'print-parts')


def _print_dialog():
    return Step('print dialog', 'Press Add from another document\u2026 on the Parts step.',
                'print-dialog')


def _dialog_closed():
    return Step('dialog closed', "Close Onshape's part dialog.")


def _ask_part(document, first, second):
    """Ask for one part both ways: requestSelection, then the select dialog."""
    return [
        _open_panel(document), _choose_print(),
        Step('ask for a part', 'Press Ask for a part.', 'ask-part'),
        _select([first]),
        Step('open part dialog', "Press Open the part dialog.", 'dialog-part'),
        _select([second], "Onshape's select dialog"),
        _close_dialog(),
    ]


SCENARIOS = {scenario.name: scenario for scenario in (
    Scenario('panel-load', 'tb-box', True, [
        _open_panel('tb-box'),
        Step('team config', 'Check the panel shows the team config banner.'),
        _choose_print(),
    ], estimate=6),
    Scenario('to-print', 'tb-box', True, [
        _open_panel('tb-box'),
        _choose_print(),
        Step('back', 'Press Back on the print page. The panel must return to tb-box.', 'back'),
    ], estimate=6),
    Scenario('part-select', 'tb-two-parts', True,
             _ask_part('tb-two-parts', 'Cylinder', 'Box'), estimate=2),
    Scenario('multi-select', 'tb-two-parts', True, [
        _open_panel('tb-two-parts'), _choose_print(),
        Step('ask for two parts', 'Press Ask for two parts.', 'ask-two-parts'),
        _select(['Box', 'Cylinder']),
        Step('deselect', 'Clear the selection in Onshape.'),
        Step('open parts dialog', 'Press Open the parts dialog.', 'dialog-parts'),
        # The other order, so the step name differs and the order Onshape reports shows.
        _select(['Cylinder', 'Box'], "Onshape's select dialog"),
        _close_dialog(),
    ], estimate=2),
    Scenario('assembly-select', 'tb-assembly', True,
             _ask_part('tb-assembly', 'Box <1>', 'Box <2>'), estimate=2),
    Scenario('part-export', 'tb-box', False, [
        Step('export parts', 'API only: exports every test document\'s parts from the '
                             'command line. No panel.'),
    ], estimate=45),
    # The print scenarios' estimates count what the ledger counts (responses 200 to 399, so
    # an export's 307 and its 200 are two). Panel load is 6, as in panel-load; the print
    # page itself makes no Onshape call.
    # 13: panel 6; the box: elements, configuration, parts, export 2 (5); the cylinder:
    # export 2, the element answers cached; the deselection and the reference selection 0.
    Scenario('print-select-parts', 'tb-two-parts', True, [
        _open_panel('tb-two-parts'), _choose_print(), _print_parts(),
        _select(['Box']), _select(['Cylinder']),
        Step('deselect', 'Clear the selection in Onshape. The parts list keeps both parts.'),
        # Spec 11 step 3: how Onshape answers a request with no count. Back on Setup the
        # page has stopped its own request, so the answer reaches only the strip.
        Step('ask unbounded', 'For reference: press Back on the print page, then press Ask '
                              'with no count.', 'ask-unbounded', recorded_only=True),
        Step('select parts Box, Cylinder', 'Select Box, then Cylinder, in Onshape.', None,
             ['Box', 'Cylinder'], recorded_only=True),
    ], estimate=13),
    # 11: panel 6; the export by idTag 2; if idTag is not the part id, its failed export is
    # a 4xx (not counted), then the parts list 1 and the export 2.
    Scenario('print-dialog-parts', 'tb-two-parts', True, [
        _open_panel('tb-two-parts'), _choose_print(), _print_parts(), _print_dialog(),
        _select(['Box'], "Onshape's part dialog, in the tb-box document"),
        _dialog_closed(),
    ], estimate=11),
    # 10: panel 6; elements, the assembly definition, export 2.
    Scenario('print-assembly-part', 'tb-assembly', True, [
        _open_panel('tb-assembly'), _choose_print(), _print_parts(), _select(['Box <1>']),
    ], estimate=10),
    # 13: panel 6; the click, refused while the panel URL has no {$configuration}:
    # elements, configuration (2), or once it is registered elements, parts, export 2 (4);
    # the dialog pick, which carries its configuration: export by idTag 2, or a failed 4xx
    # then parts 1 and export 2 (3).
    Scenario('print-configured-part', 'tb-configured', True, [
        _open_panel('tb-configured'), _choose_print(), _print_parts(),
        Step('select part Spacer', 'Select Spacer in the parts list. Until the panel URL '
                                   'carries {$configuration}, the page refuses it and says to '
                                   'use Add from another document\u2026', None, ['Spacer']),
        _print_dialog(),
        Step('select part Spacer (Large)', "In Onshape's part dialog, pick Spacer from this "
                                           'document with Size set to Large.', None,
             ['Spacer (Large)']),
        _dialog_closed(),
    ], estimate=13),
    # 12: tb-two-parts elements, configuration, parts, two exports 4 (7); tb-assembly the
    # runner's read of the definition for the path 1, elements 1, the definition again
    # from the cache 0, export 2 (4); 11 in all, plus 1 in case the definition is read twice.
    Scenario('print-export', 'tb-two-parts', False, [
        Step('export print parts', "API only: resolves and exports tb-two-parts' parts and "
                                   "tb-assembly's first instance as the print path does. "
                                   'No panel.'),
    ], estimate=12),
)}


def scenario_is_recorded(name) -> bool:
    """The cassette exists and, for a scenario with a panel part, the message log too."""
    if not (settings.CASSETTE_DIR / f'{name}.json').is_file():
        return False
    return not SCENARIOS[name].panel or (settings.MESSAGE_DIR / f'{name}.json').is_file()


def export_part_stl(client, doc, part_id, params):
    """One part's STL: (body, the URL of each redirect hop followed).

    `doc` holds did and eid, and wid for the workspace or vid for a version. The export
    answers 307; print/onshape_parts.py follows the hops, at most three, re-attaching the
    authentication only on an *.onshape.com host.
    """
    where = f"v/{doc['vid']}" if doc.get('vid') else f"w/{doc['wid']}"
    path = f"/parts/d/{doc['did']}/{where}/e/{doc['eid']}/partid/{quote(part_id, safe='')}/stl"
    return fetch_following_redirects(client, path, params, headers={'Accept': STL_ACCEPT})


def _context(doc):
    return {'documentId': doc['did'], 'workspaceId': doc['wid'], 'elementId': doc['eid']}


def _resolved(answer, document):
    parts, errors = answer
    if errors:
        raise RuntimeError(f"print-export: {document}: {errors[0]['message']}")
    return parts


def _first_occurrence_path(definition):
    root = definition.get('rootAssembly') or {}
    for occurrence in root.get('occurrences') or []:
        if occurrence.get('path'):
            return list(occurrence['path'])
    instances = root.get('instances') or []
    if not instances:
        raise RuntimeError('print-export: tb-assembly has no instances')
    return [instances[0]['id']]


def run_print_export(client, documents: dict) -> list:
    """print-export: resolve and export as the print path does, through
    print/onshape_parts.py. Returns the ExportResults.

    Both parts of tb-two-parts are click selections by part id; tb-assembly's first
    instance is a click selection by an occurrence path read from its definition. A fresh
    element cache, so every recording makes the same calls.
    """
    cache = ElementCache()
    two = documents['tb-two-parts']
    address = rest_address(_context(two))
    parts = _resolved(resolve_selections(client, address, [{'partId': part_id} for part_id
                                                           in two['parts'].values()],
                                         'selection', cache), 'tb-two-parts')
    results = [export_part_mesh(client, part, byte_cap=MAX_PART_BYTES, cache=cache)
               for part in parts]
    address = rest_address(_context(documents['tb-assembly']))
    path = _first_occurrence_path(cache.assembly_definition(client, address))
    parts = _resolved(resolve_selections(client, address, [{'occurrencePath': path}],
                                         'selection', cache), 'tb-assembly')
    results += [export_part_mesh(client, part, byte_cap=MAX_PART_BYTES, cache=cache)
                for part in parts]
    return results


def run_scenario_api(name: str, client, documents: dict) -> None:
    """Run a scenario's API actions with `client`. Only part-export and print-export have any.

    `documents` is documents.json: {name: {did, wid, eid, version_id?, parts: {name: id}}}.
    """
    if name == 'print-export':
        run_print_export(client, documents)
        return
    if name != 'part-export':
        return
    for name, doc in documents.items():
        if name in ASSEMBLY_DOCUMENTS:      # its parts export through their Part Studio
            continue
        if name in CONFIGURED_DOCUMENTS:
            continue
        for part_id in doc.get('parts', {}).values():
            export_part_stl(client, doc, part_id, None)          # Onshape's defaults: inches
            export_part_stl(client, doc, part_id, MM_BINARY)
    response = client._make_api_request('GET', '/translations/translationformats')
    if response.status_code != 200:
        raise RuntimeError(f'/translations/translationformats answered HTTP '
                           f'{response.status_code}')
    box = documents['tb-box']
    if not box.get('version_id'):
        raise RuntimeError('tb-box has no saved version in documents.json; run build-docs')
    version = {'did': box['did'], 'vid': box['version_id'], 'eid': box['eid']}
    export_part_stl(client, version, next(iter(box['parts'].values())), MM_BINARY)

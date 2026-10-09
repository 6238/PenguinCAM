"""Scenario definitions and the part-export runner (spec 5.7 and 6).

A scenario is a named, scripted run. Those with a panel part are a list of steps the
checklist strip shows on the print page: each step has an instruction for whoever drives
the panel (the owner, or the automated Onshape UI run) and, optionally, the action button
the strip offers for it. The fake Onshape host finds a selection's answer by the step's
name, so a selection step is named `select part <name>` or `select parts <a>, <b>`.

Only part-export makes API calls of its own; run_scenario_api() runs them.
"""
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import quote, urljoin, urlsplit

from testbed import settings
from testbed.scrub import host_is_onshape

# The test documents of spec 5.1, by the name that is their key in documents.json.
TEST_DOCUMENT_NAMES = ('tb-box', 'tb-two-parts', 'tb-inch', 'tb-sideways', 'tb-oversized',
                       'tb-assembly')
# What a step's button does. The first five send a panel message; choose-print and back
# are done in the panel itself, so the strip shows only their instruction.
ACTIONS = ('ask-part', 'ask-two-parts', 'dialog-part', 'dialog-parts', 'close-dialog',
           'choose-print', 'back')

MM_BINARY = {'units': 'millimeter', 'mode': 'binary'}
_REDIRECTS = (301, 302, 303, 307, 308)
_MAX_HOPS = 3
_STL_ACCEPT = 'application/vnd.onshape.v1+octet-stream'


@dataclass
class Step:
    name: str
    instruction: str
    action: Optional[str] = None
    select: list = field(default_factory=list)      # the part names to select in Onshape


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
)}


def scenario_is_recorded(name) -> bool:
    """The cassette exists and, for a scenario with a panel part, the message log too."""
    if not (settings.CASSETTE_DIR / f'{name}.json').is_file():
        return False
    return not SCENARIOS[name].panel or (settings.MESSAGE_DIR / f'{name}.json').is_file()


def _auth_for(client):
    """(requests auth, headers) that authenticate `client` on an *.onshape.com host."""
    if client.auth_mode == 'apikey':
        return (client.api_access_key, client.api_secret_key), {}
    return None, {'Authorization': f'Bearer {client.access_token}'}


def export_part_stl(client, doc, part_id, params):
    """One part's STL: (body, the URL of each redirect hop followed).

    `doc` holds did and eid, and wid for the workspace or vid for a version. The export
    answers 307; requests would drop the authentication on a hop to another host, and
    Onshape requires it, so the hops are followed here, at most three, re-attaching it
    only on an *.onshape.com host. A later plan moves this to print/onshape_parts.py.
    """
    where = f"v/{doc['vid']}" if doc.get('vid') else f"w/{doc['wid']}"
    path = f"/parts/d/{doc['did']}/{where}/e/{doc['eid']}/partid/{quote(part_id, safe='')}/stl"
    response = client._make_api_request('GET', path, params=params, allow_redirects=False,
                                        headers={'Accept': _STL_ACCEPT})
    hops = []
    while response.status_code in _REDIRECTS:
        if len(hops) == _MAX_HOPS:
            raise RuntimeError(f'export of part {part_id} still redirects after '
                               f'{_MAX_HOPS} hops')
        location = urljoin(response.url, response.headers['Location'])
        hops.append(location)
        auth, headers = None, {'Accept': _STL_ACCEPT}
        if host_is_onshape(urlsplit(location).hostname or ''):
            auth, extra = _auth_for(client)
            headers.update(extra)
        response = client.session.get(location, auth=auth, headers=headers,
                                      allow_redirects=False)
    if response.status_code != 200:
        raise RuntimeError(f'export of part {part_id} answered HTTP {response.status_code}')
    return response.content, hops


def run_scenario_api(name: str, client, documents: dict) -> None:
    """Run a scenario's API actions with `client`. Only part-export has any.

    `documents` is documents.json: {name: {did, wid, eid, version_id?, parts: {name: id}}}.
    """
    if name != 'part-export':
        return
    for doc in documents.values():
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

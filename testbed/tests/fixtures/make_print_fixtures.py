"""Regenerate the print path's synthetic cassettes, one per end-to-end request flow, and
the synthetic message log of the print page's Parts step.

    uv run python testbed/tests/fixtures/make_print_fixtures.py

Written from the documented OpenAPI shapes (elements, configuration, parts, assembly
definition, part STL export), not recorded: every id is a made-up 24-hex string and every
cassette is marked "synthetic": true. Each cassette is the whole call sequence of one flow,
exports included, because replay answers from the one current cassette. They are deleted
when the real recordings land; no scenario ever replays one.
"""
import json
import math
import struct
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2]))          # the repository root

from print.onshape_parts import STL_PARAMS  # noqa: E402
from testbed.cassette import Cassette, Exchange  # noqa: E402

CASSETTE_DIR = HERE / 'cassettes'
MESSAGE_DIR = HERE / 'messages'
API = '/api/v13'

DID = 'd00000000000000000000001'                  # the open document
WID = 'a00000000000000000000001'
MID = 'c00000000000000000000001'
EID_PS = 'e00000000000000000000001'               # its Part Studio
EID_ASM = 'e00000000000000000000002'              # its Assembly
EID_SUB = 'e00000000000000000000004'              # a subassembly in it
DID2 = 'd00000000000000000000002'                 # another document, by version
VID2 = 'b00000000000000000000002'
MID2 = 'c00000000000000000000002'
EID2 = 'e00000000000000000000003'
BOX, CYLINDER, LID, NEW = 'JHD', 'JKD', 'JSD', 'JND'
IDTAG = 'idTagBox1'
CONFIGURATION = 'Size=20+mm'


# ---------------------------------------------------------------------------
# STL bodies: binary, with real normals
# ---------------------------------------------------------------------------

def _binary_stl(triangles):
    out = [b'synthetic print fixture'.ljust(80, b' '), struct.pack('<I', len(triangles))]
    for a, b, c in triangles:
        u = [b[i] - a[i] for i in range(3)]
        v = [c[i] - a[i] for i in range(3)]
        n = [u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0]]
        length = math.sqrt(sum(x * x for x in n)) or 1.0
        out.append(struct.pack('<12fH', *(x / length for x in n), *a, *b, *c, 0))
    return b''.join(out)


def box_stl(x=10.0, y=20.0, z=30.0):
    p = [(i * x, j * y, k * z) for i in (0, 1) for j in (0, 1) for k in (0, 1)]

    def v(i, j, k):
        return p[i * 4 + j * 2 + k]
    quads = [  # each counterclockwise seen from outside
        (v(0, 0, 0), v(0, 1, 0), v(1, 1, 0), v(1, 0, 0)),     # bottom, -z
        (v(0, 0, 1), v(1, 0, 1), v(1, 1, 1), v(0, 1, 1)),     # top, +z
        (v(0, 0, 0), v(1, 0, 0), v(1, 0, 1), v(0, 0, 1)),     # -y
        (v(0, 1, 0), v(0, 1, 1), v(1, 1, 1), v(1, 1, 0)),     # +y
        (v(0, 0, 0), v(0, 0, 1), v(0, 1, 1), v(0, 1, 0)),     # -x
        (v(1, 0, 0), v(1, 1, 0), v(1, 1, 1), v(1, 0, 1)),     # +x
    ]
    triangles = []
    for a, b, c, d in quads:
        triangles += [(a, b, c), (a, c, d)]
    return _binary_stl(triangles)


def cylinder_stl(radius=10.0, height=15.0, segments=64):
    ring = [(radius * math.cos(2 * math.pi * i / segments),
             radius * math.sin(2 * math.pi * i / segments)) for i in range(segments)]
    triangles = []
    for i in range(segments):
        (x0, y0), (x1, y1) = ring[i], ring[(i + 1) % segments]
        triangles += [((0, 0, 0), (x1, y1, 0), (x0, y0, 0)),               # bottom
                      ((0, 0, height), (x0, y0, height), (x1, y1, height)),  # top
                      ((x0, y0, 0), (x1, y1, 0), (x1, y1, height)),          # side
                      ((x0, y0, 0), (x1, y1, height), (x0, y0, height))]
    return _binary_stl(triangles)


# ---------------------------------------------------------------------------
# Exchanges
# ---------------------------------------------------------------------------

def _json(path, value, query=None, status=200):
    return Exchange(method='GET', host='cad.onshape.com', path=API + path, query=query or {},
                    request_body=None, status=status,
                    headers={'Content-Type': 'application/json;charset=UTF-8'},
                    body_text=json.dumps(value), body_file=None)


def _q(**values):
    return {key: [value] for key, value in values.items()}


def elements(element_type, eid, wvm=f'w/{WID}'):
    name = {'PARTSTUDIO': 'Part Studio 1', 'ASSEMBLY': 'Assembly 1'}[element_type]
    return _json(f'/documents/d/{DID}/{wvm}/elements',
                 [{'id': eid, 'name': name, 'elementType': element_type,
                   'dataType': f'onshape/{element_type.lower()}', 'type': name.rsplit(' ', 1)[0]}],
                 _q(elementId=eid))


def configuration(parameters=()):
    return _json(f'/elements/d/{DID}/w/{WID}/e/{EID_PS}/configuration',
                 {'btType': 'BTConfigurationResponse-2019', 'configurationParameters': list(parameters),
                  'currentConfiguration': []})


SIZE_PARAMETER = {'btType': 'BTMConfigurationParameterQuantity-1826', 'parameterId': 'Size',
                  'parameterName': 'Size', 'quantityType': 'LENGTH',
                  'rangeAndDefault': {'minValue': 10, 'maxValue': 40, 'defaultValue': 20,
                                      'units': 'millimeter'}}


def _part(part_id, name, body_type='solid', eid=EID_PS):
    return {'partId': part_id, 'name': name, 'bodyType': body_type, 'elementId': eid,
            'isHidden': False, 'isMesh': False, 'partNumber': None, 'state': 'IN_PROGRESS',
            'appearance': {'color': {'red': 200, 'green': 200, 'blue': 200}, 'opacity': 255,
                           'isGenerated': False}}


PS_PARTS = [_part(BOX, 'Box'), _part(CYLINDER, 'Cylinder'), _part(LID, 'Lid sketch', 'sheet')]


def parts(listed=PS_PARTS, where=f'd/{DID}/w/{WID}/e/{EID_PS}', query=None):
    return _json(f'/parts/{where}', listed, query)


def export(part_id, body, where=f'd/{DID}/w/{WID}/e/{EID_PS}', extra=None, status=307):
    """The export's 307 and the blob's 200, or one answer when status is not 307."""
    query = {key: [value] for key, value in dict(STL_PARAMS, **(extra or {})).items()}
    path = f'{API}/parts/{where}/partid/{part_id}/stl'
    if status != 307:
        return [Exchange(method='GET', host='cad.onshape.com', path=path, query=query,
                         request_body=None, status=status,
                         headers={'Content-Type': 'application/json;charset=UTF-8'},
                         body_text=json.dumps({'message': 'synthetic error', 'status': status}),
                         body_file=None)]
    blob = f'{API}/blob/synthetic/{where.replace("/", "-")}-{part_id}.stl'
    return [Exchange(method='GET', host='cad.onshape.com', path=path, query=query,
                     request_body=None, status=307,
                     headers={'Location': f'https://cad.onshape.com{blob}'},
                     body_text=None, body_file=None),
            Exchange(method='GET', host='cad.onshape.com', path=blob, query={}, request_body=None,
                     status=200, headers={'Content-Type': 'application/octet-stream'},
                     body_text=None, body_file=None, body_bytes=body)]


def _instance(instance_id, name, part_id, document_id, element_id, *, version=None,
              microversion=MID2, kind='Part', standard=False, configuration=''):
    instance = {'id': instance_id, 'type': kind, 'name': name, 'documentId': document_id,
                'elementId': element_id, 'documentMicroversion': microversion,
                'configuration': configuration, 'fullConfiguration': configuration or 'default',
                'suppressed': False, 'isStandardContent': standard}
    if kind == 'Part':
        instance['partId'] = part_id
    if version:
        instance['documentVersion'] = version
    return instance


def _occurrence(path):
    return {'path': path, 'transform': [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],
            'fixed': False, 'hidden': False}


def assembly(instances, sub_assemblies=(), occurrences=()):
    return _json(f'/assemblies/d/{DID}/w/{WID}/e/{EID_ASM}',
                 {'rootAssembly': {'documentId': DID, 'elementId': EID_ASM, 'documentMicroversion': MID,
                                   'instances': list(instances), 'occurrences': list(occurrences),
                                   'patterns': [], 'features': [], 'fullConfiguration': 'default'},
                  'subAssemblies': list(sub_assemblies), 'parts': [], 'partStudioFeatures': []})


LINKED_BOX = _instance('MboxInstance1aaaa', 'Box <1>', BOX, DID2, EID2, version=VID2)
LINKED_EXPORT = dict(where=f'd/{DID2}/v/{VID2}/e/{EID2}', extra={'linkDocumentId': DID})
SUB_INSTANCE = _instance('MsubInstance1aaaa', 'Bracket assembly <1>', None, DID, EID_SUB,
                         microversion=MID, kind='Assembly')
SUB_CYLINDER = _instance('McylInstance1aaaa', 'Cylinder <2>', CYLINDER, DID, EID_PS, microversion=MID)
SCREW = _instance('MscrewInstance1aa', 'Socket Head Cap Screw <1>', 'JAD',
                  '5a00000000000000000000aa', '5e00000000000000000000aa', version='5b00000000000000000000aa',
                  standard=True)

BOX_STL, CYLINDER_STL = box_stl(), cylinder_stl()

FLOWS = {
    'ps-one': [elements('PARTSTUDIO', EID_PS), configuration(), parts(), *export(BOX, BOX_STL)],
    'ps-two': [elements('PARTSTUDIO', EID_PS), configuration(), parts(), *export(BOX, BOX_STL),
               *export(CYLINDER, CYLINDER_STL)],
    'ps-refresh': [*export(BOX, BOX_STL), *export(CYLINDER, CYLINDER_STL)],
    'ps-configured': [elements('PARTSTUDIO', EID_PS), configuration([SIZE_PARAMETER])],
    'ps-configured-passed': [elements('PARTSTUDIO', EID_PS), configuration([SIZE_PARAMETER]),
                             parts(query=_q(configuration=CONFIGURATION)),
                             *export(BOX, BOX_STL, extra={'configuration': CONFIGURATION})],
    'ps-resolve-twice': [elements('PARTSTUDIO', EID_PS), configuration(), parts(),
                         elements('PARTSTUDIO', EID_PS), configuration(), parts()],
    'ps-cache-miss': [elements('PARTSTUDIO', EID_PS), configuration(), parts(),
                      parts(PS_PARTS + [_part(NEW, 'Spacer')]), *export(NEW, BOX_STL)],
    'ps-402': [elements('PARTSTUDIO', EID_PS), configuration(), parts(),
               *export(BOX, None, status=402)],
    'asm-one': [elements('ASSEMBLY', EID_ASM),
                assembly([LINKED_BOX], occurrences=[_occurrence([LINKED_BOX['id']])]),
                *export(BOX, BOX_STL, **LINKED_EXPORT)],
    'asm-sub': [elements('ASSEMBLY', EID_ASM),
                assembly([SUB_INSTANCE],
                         [{'documentId': DID, 'elementId': EID_SUB, 'documentMicroversion': MID,
                           'fullConfiguration': 'default', 'instances': [SUB_CYLINDER],
                           'patterns': [], 'features': []}],
                         [_occurrence([SUB_INSTANCE['id']]),
                          _occurrence([SUB_INSTANCE['id'], SUB_CYLINDER['id']])]),
                *export(CYLINDER, CYLINDER_STL)],
    'asm-standard': [elements('ASSEMBLY', EID_ASM),
                     assembly([SCREW], occurrences=[_occurrence([SCREW['id']])])],
    'asm-cache-miss': [elements('ASSEMBLY', EID_ASM), assembly([]),
                       assembly([LINKED_BOX], occurrences=[_occurrence([LINKED_BOX['id']])]),
                       *export(BOX, BOX_STL, **LINKED_EXPORT)],
    'dialog-one': [*export(IDTAG, BOX_STL, **LINKED_EXPORT)],
    'dialog-fallback': [*export(IDTAG, None, status=404, **LINKED_EXPORT),
                        parts([_part(BOX, 'Box', eid=EID2), _part(CYLINDER, 'Cylinder', eid=EID2)],
                              where=f'd/{DID2}/v/{VID2}/e/{EID2}', query=_q(linkDocumentId=DID)),
                        *export(BOX, BOX_STL, **LINKED_EXPORT)],
    'dialog-duplicate': [*export(IDTAG, None, status=404, **LINKED_EXPORT),
                         parts([_part(BOX, 'Box', eid=EID2), _part('JQD', 'Box', eid=EID2)],
                               where=f'd/{DID2}/v/{VID2}/e/{EID2}', query=_q(linkDocumentId=DID))],
    'none': [],
    # Exactly the calls run_scenario_api makes for print-export (testbed/scenarios), for its
    # unit test; no scenario replays it.
    'export-run': [elements('PARTSTUDIO', EID_PS), configuration(), parts(), *export(BOX, BOX_STL),
                   *export(CYLINDER, CYLINDER_STL),
                   assembly([LINKED_BOX], occurrences=[_occurrence([LINKED_BOX['id']])]),
                   elements('ASSEMBLY', EID_ASM), *export(BOX, BOX_STL, **LINKED_EXPORT)],
}


# ---------------------------------------------------------------------------
# The Parts step's messages (print/static/print_onshape.js): answer shapes for its node tests
# ---------------------------------------------------------------------------

ORIGIN = 'https://cad.onshape.com'
_IDS = {'documentId': DID, 'workspaceId': WID, 'elementId': EID_PS}


def _event(direction, t_ms, data, ids=True):
    return {'dir': direction, 'origin': ORIGIN, 't_ms': t_ms, 'data': dict(_IDS, **data) if ids else data}


def _answer(t_ms, message_id, selections, status='SUCCESS'):
    return _event('in', t_ms, {'messageName': 'REQUESTED_SELECTION', 'messageId': message_id,
                               'selections': selections, 'status': {'statusCode': status}})


def _body(**fields):
    return dict({'entityType': 'BODY', 'selectionType': 'ENTITY', 'elementId': EID_PS,
                 'workspaceMicroversionId': MID}, **fields)


def _arm(t_ms, n):
    return _event('out', t_ms, {'messageName': 'requestSelection', 'messageId': f'penguincam-print-{n}',
                                'filterType': 'simple', 'entityTypeSpecifier': ['BODY'],
                                'bodyTypeSpecifier': ['SOLID'], 'requiredSelectionCount': 1})


def selection_message_log():
    """Every answer shape the Parts step must read: the field names the CNC code has seen
    (selectionId, entityId, partId, bodyId) and an assembly occurrence path."""
    dialog_item = {'messageName': 'itemSelectedInSelectItemDialog', 'documentId': DID2,
                   'versionId': VID2, 'elementId': EID2, 'elementConfiguration': '',
                   'elementType': 'PARTSTUDIO', 'itemType': 'PART', 'partName': 'Box',
                   'idTag': IDTAG, 'isSurface': False, 'isComposite': False, 'isSketch': False,
                   'isFlattenedBody': False}
    steps = [
        ('open parts', [_event('out', 0, {'messageName': 'applicationInit'}), _arm(10, 1),
                        _answer(40, 'penguincam-print-1', [], 'PENDING')]),
        ('click a part by part id', [_answer(1000, 'penguincam-print-1',
                                             [_body(partId=BOX, selectionId=BOX)]), _arm(1010, 2)]),
        ('click a part by selection id', [_answer(2000, 'penguincam-print-2',
                                                  [_body(selectionId=CYLINDER)]), _arm(2010, 3)]),
        ('click a part by entity id', [_answer(3000, 'penguincam-print-3',
                                               [_body(entityId=CYLINDER)]), _arm(3010, 4)]),
        ('click a part by body id', [_answer(4000, 'penguincam-print-4',
                                             [_body(bodyId=BOX, name='Box')]), _arm(4010, 5)]),
        ('click a part in an assembly', [_answer(5000, 'penguincam-print-5', [_body(
            selectionId='JHD', occurrence={'path': ['MboxInstance1aaaa']})]), _arm(5010, 6)]),
        ('deselect', [_answer(6000, 'penguincam-print-6', []), _arm(6010, 7)]),
        ('a generic selection event', [_event('in', 6500, {'messageName': 'SELECTION',
                                                           'selections': [_body(partId=LID)]})]),
        ('open the part dialog', [_event('out', 7000, {'messageName': 'openSelectItemDialog',
                                                       'selectParts': True, 'selectMultiple': True})]),
        ('pick in the dialog', [_event('in', 8000, dialog_item, ids=False)]),        # the item's own ids
        ('close the dialog', [_event('in', 9000, {'messageName': 'selectItemDialogClosed'}),
                              _arm(9010, 8)]),
        ('leave parts', [_event('out', 10000, {'messageName': 'stopRequest',
                                               'messageId': 'penguincam-print-8'})]),
    ]
    return {'scenario': 'synthetic-print-selection', 'synthetic': True,
            'steps': [{'step': name, 'events': events} for name, events in steps]}


def main():
    for flow, exchanges in FLOWS.items():
        path = Cassette(f'synthetic-print-{flow}', exchanges, synthetic=True).save(CASSETTE_DIR)
        print(path.relative_to(HERE.parents[2]))
    log = selection_message_log()
    path = MESSAGE_DIR / f"{log['scenario']}.json"
    path.write_text(json.dumps(log, indent=1, sort_keys=True) + '\n')
    print(path.relative_to(HERE.parents[2]))


if __name__ == '__main__':
    main()

"""The test documents builder: `python -m testbed build-docs` (spec 5.1).

Builds the seven test documents in the test folder through the API, one `POST .../features`
per sketch or extrude (and, for tb-configured, one `POST .../configuration` first), and
writes their ids to testbed/documents.json:

    {name: {did, wid, eid, version_id?, parts: {partName: partId}}}

Re-running is safe: the folder is listed, documents already in it are kept, and only the
missing ones are built. Nothing is written to or deleted unless assert_test_document()
says the target is a test document.
"""
import json
import sys
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import quote

from testbed import settings

TEST_PREFIX = 'tb-'
TEST_DESCRIPTION = 'created by PenguinCAM test bed'
# The counted calls of a full build, for the budget check: two folder listings; per part
# document its creation, elements, two features per part, parts list and one rename per
# part; tb-configured's configuration; tb-box's version; the assembly document's creation,
# its assembly element and two instances. Plus one for a --rebuild's delete.
BUILD_DOCS_ESTIMATE = 48

# The default planes, found by name with the query string Onshape documents rather than
# by deterministic ids, which it says must not be hard-coded.
PLANES = ('Top', 'Front', 'Right')
# The id of Onshape's Name part property, set through the metadata endpoint.
NAME_PROPERTY_ID = '57f3fb8efa3416c06701d60d'
VERSION_NAME = 'tb-box for part-export'
# A documents.json entry with this key was created but not built; its value says why.
PENDING = 'pending'
NOT_IN_FOLDER = 'not in the test folder'
FOLDER_PAGE = 20                    # GET /documents answers at most 20 items at a time
_FEATURE_CALL = 'BTFeatureDefinitionCall-1406'


class NotATestDocument(Exception):
    """A write or delete was about to touch a document that is not a test document."""


@dataclass
class DocumentSpec:
    name: str
    kind: str                       # 'partstudio' or 'assembly'
    features: list                  # feature call bodies, posted in order
    # For an assembly: (test document, how many instances) of that document's first part.
    instances: Optional[list] = None
    # The name for each part, in the order the features create them (Part 1, Part 2, ...).
    part_names: list = field(default_factory=list)
    # A Part Studio's configuration, posted before its features (BTConfigurationUpdateCall).
    configuration: Optional[dict] = None


# ---------------------------------------------------------------------------
# Feature bodies
# ---------------------------------------------------------------------------

def _plane_query(plane):
    if plane not in PLANES:
        raise ValueError(f'plane must be one of {PLANES}, not {plane!r}')
    return {'btType': 'BTMParameterQueryList-148', 'parameterId': 'sketchPlane',
            'queries': [{'btType': 'BTMIndividualQuery-138', 'queryString':
                         f'query=qCreatedBy(makeId("{plane}"), '
                         'EntityType.FACE);'}]}


def _sketch(plane, name, entities, constraints):
    return {'btType': _FEATURE_CALL, 'feature': {
        'btType': 'BTMSketch-151', 'featureType': 'newSketch', 'name': name,
        'parameters': [_plane_query(plane)], 'entities': entities,
        'constraints': constraints}}


def _line(entity_id, x, y, dx, dy, length):
    return {'btType': 'BTMSketchCurveSegment-155', 'entityId': entity_id,
            'startPointId': f'{entity_id}.start', 'endPointId': f'{entity_id}.end',
            'startParam': 0.0, 'endParam': length,
            'geometry': {'btType': 'BTCurveGeometryLine-117', 'pntX': x, 'pntY': y,
                         'dirX': dx, 'dirY': dy}}


def _coincident(entity_id, first, second):
    return {'btType': 'BTMSketchConstraint-2', 'constraintType': 'COINCIDENT',
            'entityId': entity_id, 'parameters': [
                {'btType': 'BTMParameterString-149', 'parameterId': 'localFirst',
                 'value': first},
                {'btType': 'BTMParameterString-149', 'parameterId': 'localSecond',
                 'value': second}]}


def sketch_rectangle_feature(plane, width_m, height_m, name='Sketch'):
    """A sketch of a width_m x height_m rectangle with a corner at the plane's origin."""
    w, h = float(width_m), float(height_m)
    lines = [_line('bottom', 0.0, 0.0, 1.0, 0.0, w), _line('right', w, 0.0, 0.0, 1.0, h),
             _line('top', w, h, -1.0, 0.0, w), _line('left', 0.0, h, 0.0, -1.0, h)]
    corners = [_coincident(f'corner{i}', f"{a['entityId']}.end", f"{b['entityId']}.start")
               for i, (a, b) in enumerate(zip(lines, lines[1:] + lines[:1]))]
    return _sketch(plane, name, lines, corners)


def sketch_circle_feature(plane, radius_m, center_m=(0.0, 0.0), name='Sketch'):
    """A sketch of one circle; geometry in metres."""
    x, y = (float(v) for v in center_m)
    circle = {'btType': 'BTMSketchCurve-4', 'entityId': 'circle', 'centerId': 'circle.center',
              'geometry': {'btType': 'BTCurveGeometryCircle-115', 'radius': float(radius_m),
                           'xCenter': x, 'yCenter': y, 'xDir': 1.0, 'yDir': 0.0,
                           'clockwise': False}}
    return _sketch(plane, name, [circle], [])


def _quantity(parameter_id, expression):
    return {'btType': 'BTMParameterQuantity-147', 'parameterId': parameter_id,
            'expression': expression}


def _enum(parameter_id, enum_name, value):
    return {'btType': 'BTMParameterEnum-145', 'parameterId': parameter_id,
            'enumName': enum_name, 'value': value}


def extrude_feature(sketch_feature_id, depth_expression, name='Extrude'):
    """A new solid from every region of a sketch, blind to `depth_expression` ('10 mm'),
    or to a ready depth parameter (a dict, such as configured_quantity's).

    In a DocumentSpec the sketch is named by its feature name; the builder swaps in the
    feature id Onshape gave the sketch.
    """
    depth = (depth_expression if isinstance(depth_expression, dict)
             else _quantity('depth', depth_expression))
    return {'btType': _FEATURE_CALL, 'feature': {
        'btType': 'BTMFeature-134', 'featureType': 'extrude', 'name': name, 'parameters': [
            _enum('bodyType', 'ExtendedToolBodyType', 'SOLID'),
            _enum('operationType', 'NewBodyOperationType', 'NEW'),
            {'btType': 'BTMParameterQueryList-148', 'parameterId': 'entities',
             'queries': [{'btType': 'BTMIndividualSketchRegionQuery-140',
                          'featureId': sketch_feature_id}]},
            _enum('endBound', 'BoundingType', 'BLIND'),
            depth]}}


def list_configuration(input_id, enum_name, options, default):
    """A configuration with one list input: the body of POST .../configuration."""
    return {'btType': 'BTConfigurationUpdateCall-2933', 'configurationParameters': [{
        'btType': 'BTMConfigurationParameterEnum-105', 'parameterId': input_id,
        'parameterName': input_id, 'enumName': enum_name, 'defaultValue': default,
        'options': [{'btType': 'BTMEnumOption-592', 'option': option, 'optionName': option}
                    for option in options]}],
        'currentConfiguration': [_enum(input_id, enum_name, default)]}


def configured_quantity(parameter_id, input_id, enum_name, expressions):
    """A feature parameter driven by a list input: {option: expression}."""
    return {'btType': 'BTMParameterConfigured-2222', 'parameterId': parameter_id,
            'configurationParameterId': input_id, 'values': [
                {'btType': 'BTMConfiguredValueByEnum-1923', 'enumName': enum_name,
                 'enumValue': option, 'value': _quantity(parameter_id, expression)}
                for option, expression in expressions.items()]}


def cube_feature(side_expression, name='Cube'):
    """Onshape's cube feature: the cheapest solid where a cube will do."""
    return {'btType': _FEATURE_CALL, 'feature': {
        'btType': 'BTMFeature-134', 'featureType': 'cube', 'name': name,
        'parameters': [_quantity('sideLength', side_expression)]}}


def _rectangle_part(name, plane, width_m, height_m, depth):
    sketch = f'{name} sketch'
    return [sketch_rectangle_feature(plane, width_m, height_m, sketch),
            extrude_feature(sketch, depth, name)]


# tb-configured's list input and the depth each of its options gives.
CONFIGURATION_INPUT = 'Size'
CONFIGURATION_ENUM = 'Size_conf'
CONFIGURATION_DEPTHS = {'Small': '10 mm', 'Large': '20 mm'}

# None of the shapes is a cube, so cube_feature is not used here.
TEST_DOCUMENTS = [
    DocumentSpec('tb-box', 'partstudio', _rectangle_part('Box', 'Top', 0.040, 0.030, '10 mm'),
                 part_names=['Box']),
    DocumentSpec('tb-two-parts', 'partstudio', [
        *_rectangle_part('Box', 'Top', 0.040, 0.030, '10 mm'),
        # Beside the box, not touching it: a 20 mm diameter, 20 mm tall cylinder.
        sketch_circle_feature('Top', 0.010, (0.070, 0.015), 'Cylinder sketch'),
        extrude_feature('Cylinder sketch', '20 mm', 'Cylinder'),
    ], part_names=['Box', 'Cylinder']),
    DocumentSpec('tb-inch', 'partstudio',
                 _rectangle_part('Block', 'Top', 0.0508, 0.0254, '0.5 in'),
                 part_names=['Block']),
    # A 60 mm post, 10 x 10 mm, lying along X: its sketch is on the Right plane.
    DocumentSpec('tb-sideways', 'partstudio',
                 _rectangle_part('Post', 'Right', 0.010, 0.010, '60 mm'),
                 part_names=['Post']),
    DocumentSpec('tb-oversized', 'partstudio',
                 _rectangle_part('Slab', 'Top', 0.400, 0.050, '10 mm'),
                 part_names=['Slab']),
    DocumentSpec('tb-assembly', 'assembly', [], instances=[('tb-box', 2)]),
    # A 20 x 20 mm spacer whose list input Size sets its depth: Small 10 mm, Large 20 mm.
    DocumentSpec('tb-configured', 'partstudio', [
        sketch_rectangle_feature('Top', 0.020, 0.020, 'Spacer sketch'),
        extrude_feature('Spacer sketch', configured_quantity(
            'depth', CONFIGURATION_INPUT, CONFIGURATION_ENUM, CONFIGURATION_DEPTHS), 'Spacer'),
    ], part_names=['Spacer'], configuration=list_configuration(
        CONFIGURATION_INPUT, CONFIGURATION_ENUM, list(CONFIGURATION_DEPTHS), 'Small')),
]


# ---------------------------------------------------------------------------
# The rules for writing
# ---------------------------------------------------------------------------

def _items(folder_listing):
    if isinstance(folder_listing, dict):
        return folder_listing.get('items', [])
    return list(folder_listing)


def assert_test_document(document_json, folder_listing) -> None:
    """Raise NotATestDocument unless the document is listed in the test folder, its name
    starts with tb- and its description is exactly TEST_DESCRIPTION.

    `folder_listing` is GET /documents?parentId=<folder>'s answer, or its items.
    """
    did = document_json.get('id')
    name = document_json.get('name') or ''
    if did not in {item.get('id') for item in _items(folder_listing)}:
        raise NotATestDocument(f'{name or did} is not listed in the test folder; refusing '
                               'to write to it')
    if not name.startswith(TEST_PREFIX):
        raise NotATestDocument(f'{name!r} does not start with {TEST_PREFIX!r}; refusing to '
                               'write to it')
    if document_json.get('description') != TEST_DESCRIPTION:
        raise NotATestDocument(f'{name} does not have the description {TEST_DESCRIPTION!r}; '
                               'refusing to write to it')


# ---------------------------------------------------------------------------
# The builder
# ---------------------------------------------------------------------------

def _call(client, method, endpoint, **kwargs):
    # OnshapeClient has no public method for an arbitrary endpoint; its private one keeps
    # the authentication and the test bed's record and replay adapters.
    response = client._make_api_request(method, endpoint, **kwargs)
    if not 200 <= response.status_code < 300:
        raise RuntimeError(f'{method} {endpoint} answered HTTP {response.status_code}')
    return response.json() if response.content else None


def _list_folder(client, folder_id):
    """Every document in the folder: GET /documents?parentId=<folder>, page by page."""
    items, params = [], {'parentId': folder_id}
    while True:
        answer = _call(client, 'GET', '/documents', params=params)
        page = _items(answer)
        items.extend(page)
        if not (isinstance(answer, dict) and answer.get('next')) or not page:
            return items
        params = {'parentId': folder_id, 'offset': len(items), 'limit': FOLDER_PAGE}


def _post_features(client, where, spec):
    """Post the spec's features in order; an extrude's sketch name becomes its feature id."""
    ids = {}
    for body in spec.features:
        body = json.loads(json.dumps(body))
        for parameter in body['feature']['parameters']:
            for query in parameter.get('queries', []):
                if query.get('featureId') in ids:
                    query['featureId'] = ids[query['featureId']]
        answer = _call(client, 'POST', f'/partstudios/{where}/features', json=body)
        status = (answer.get('featureState') or {}).get('featureStatus', 'OK')
        if status != 'OK':
            raise RuntimeError(f"{spec.name}: feature {body['feature']['name']!r} is {status}")
        ids[body['feature']['name']] = answer['feature']['featureId']


def _name_parts(client, where, spec):
    """{part name: part id}, renaming Part 1, Part 2, ... to the spec's part names."""
    parts = _call(client, 'GET', f'/parts/{where}') or []
    named = {}
    for part, name in zip(parts, spec.part_names):
        if part.get('name') != name:
            _call(client, 'POST', f"/metadata/{where}/p/{quote(part['partId'], safe='')}",
                  json={'jsonType': 'metadata-part', 'partId': part['partId'],
                        'properties': [{'propertyId': NAME_PROPERTY_ID, 'value': name}]})
        named[name] = part['partId']
    if len(named) != len(spec.part_names):
        raise RuntimeError(f'{spec.name}: expected parts {spec.part_names}, Onshape listed '
                           f"{[part.get('name') for part in parts]}")
    return named


def _build_part_studio(client, document, spec):
    did, wid = document['id'], document['defaultWorkspace']['id']
    elements = _call(client, 'GET', f'/documents/d/{did}/w/{wid}/elements') or []
    studio = next((e for e in elements if e.get('elementType') == 'PARTSTUDIO'), None)
    if studio is None:
        raise RuntimeError(f'{spec.name} has no Part Studio')
    where = f"d/{did}/w/{wid}/e/{studio['id']}"
    if spec.configuration:              # features may refer to its inputs, so it goes first
        _call(client, 'POST', f'/elements/{where}/configuration', json=spec.configuration)
    _post_features(client, where, spec)
    entry = {'did': did, 'wid': wid, 'eid': studio['id'],
             'parts': _name_parts(client, where, spec)}
    if spec.name == 'tb-box':          # part-export exports from a saved version
        version = _call(client, 'POST', f'/documents/d/{did}/versions',
                        json={'documentId': did, 'workspaceId': wid, 'name': VERSION_NAME,
                              'description': TEST_DESCRIPTION})
        entry['version_id'] = version['id']
    return entry


def _build_assembly(client, document, spec, built):
    did, wid = document['id'], document['defaultWorkspace']['id']
    assembly = _call(client, 'POST', f'/assemblies/d/{did}/w/{wid}', json={'name': 'Assembly'})
    for source_name, count in spec.instances or []:
        source = built.get(source_name)
        if not source or not source.get('version_id'):
            raise RuntimeError(f'{spec.name} needs a saved version of {source_name}')
        instance = {'documentId': source['did'], 'versionId': source['version_id'],
                    'elementId': source['eid'], 'partId': next(iter(source['parts'].values())),
                    'isAssembly': False, 'isWholePartStudio': False}
        for _ in range(count):
            _call(client, 'POST', f"/assemblies/d/{did}/w/{wid}/e/{assembly['id']}/instances",
                  json=instance)
    # Assemblies have no Part Studio STL export path, so no parts are listed.
    return {'did': did, 'wid': wid, 'eid': assembly['id'], 'parts': {}}


def _write_documents_json(built):
    settings.DOCUMENTS_FILE.write_text(json.dumps(built, indent=1, sort_keys=True) + '\n')


def pending_documents(documents: dict) -> dict:
    """{name: why} for the documents.json entries that were created but not built yet."""
    return {name: entry[PENDING] for name, entry in documents.items() if entry.get(PENDING)}


def build_test_documents(client, folder_id: str, rebuild: Optional[str] = None) -> dict:
    """Build the test documents missing from the folder; return and write documents.json.

    With `rebuild`, that one test document is deleted first and built again. A document
    POST /documents created outside the folder is not written to: its entry is marked
    pending (NOT_IN_FOLDER) and it is built by the first run that finds it in the folder.
    """
    names = [spec.name for spec in TEST_DOCUMENTS]
    if rebuild is not None and rebuild not in names:
        raise ValueError(f"--rebuild takes a test document's name ({', '.join(names)}), "
                         f'not {rebuild!r}')
    previous = {}
    if settings.DOCUMENTS_FILE.is_file():
        previous = json.loads(settings.DOCUMENTS_FILE.read_text())

    listing = _list_folder(client, folder_id)
    present = {item['name']: item for item in listing if item.get('name') in names}
    if rebuild in present:
        assert_test_document(present[rebuild], listing)
        _call(client, 'DELETE', f"/documents/{present.pop(rebuild)['id']}")
        previous.pop(rebuild, None)
        if rebuild == 'tb-box' and 'tb-assembly' in present:
            print('build-docs: tb-assembly still places the old version of tb-box; run '
                  'build-docs --rebuild tb-assembly next', file=sys.stderr)

    built, to_build, created = {}, {}, {}
    for name, item in present.items():
        entry = previous.get(name, {})
        if entry.get('did') != item['id']:
            raise RuntimeError(f'{name} is in the test folder but not in documents.json; '
                               f'run build-docs --rebuild {name}')
        if entry.get(PENDING):
            to_build[name] = item                 # moved into the folder since: build it
        else:
            built[name] = entry
    for spec in TEST_DOCUMENTS:
        entry = previous.get(spec.name, {})
        if spec.name in present:
            continue
        if entry.get(PENDING) == NOT_IN_FOLDER:   # created before, still not moved
            built[spec.name] = entry
            continue
        created[spec.name] = to_build[spec.name] = _call(client, 'POST', '/documents', json={
            'name': spec.name, 'description': TEST_DESCRIPTION, 'parentId': folder_id})

    if created:
        # Listed again: the rules need each new document to be in the folder before any
        # write. This is also the check that parentId places a document in the folder.
        listing = _list_folder(client, folder_id)
    listed = {item.get('id') for item in listing}
    for spec in TEST_DOCUMENTS:
        document = to_build.get(spec.name)
        if document is None:
            continue
        stub = {'did': document['id'], 'wid': document['defaultWorkspace']['id'], 'parts': {}}
        if document['id'] not in listed:
            built[spec.name] = dict(stub, **{PENDING: NOT_IN_FOLDER})
        elif spec.kind == 'assembly' and any(
                built.get(source, {}).get(PENDING) or source not in built
                for source, _count in spec.instances or []):
            built[spec.name] = dict(stub, **{PENDING: 'waits for the documents it places'})
        else:
            assert_test_document(document, listing)
            if spec.kind == 'assembly':
                built[spec.name] = _build_assembly(client, document, spec, built)
            else:
                built[spec.name] = _build_part_studio(client, document, spec)
        _write_documents_json(built)      # after each document, so a failure loses no ids
    _write_documents_json(built)
    return built

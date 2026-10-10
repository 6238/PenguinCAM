"""Local mode: a stand-in Onshape Part Studio made of uploaded STL files.

With PENGUINCAM_TESTBED=local the development server needs no Onshape account and makes
no network call to Onshape. The stand-in page (/testbed/local) frames the real panel and
plays Onshape's side of its messages; LocalAdapter, mounted on every OnshapeClient's
session in place of the network, answers the API calls the print path makes from the
uploaded files:

- GET /users/sessioninfo                         the stand-in user
- GET /companies                                 one stand-in classroom
- POST /documents/search, GET /documents/<did>, the config document's elements and its
  blob: the team config, settings.local_config_file() (TESTBED_LOCAL_CONFIG, or the test
  bed's team config fixture)
- GET /documents/d/<did>/w/<wid>/elements        the stand-in Part Studio
- GET /elements/d/<did>/w/<wid>/e/<eid>/configuration   no configuration parameters
- GET /parts/d/<did>/w/<wid>/e/<eid>             the uploaded parts
- GET /parts/d/<did>/w/<wid>/e/<eid>/partid/<pid>/stl   a 307 to the stand-in storage
  host, which then serves the uploaded binary STL

Anything else gets HTTP 599 with a body starting `not answered in local mode: `, never a
real call. Uploads are kept per stand-in Part Studio (its document id, one per browser
session) under settings.local_parts_dir(). An uploaded STL is taken to be in millimetres,
the unit the print path asks Onshape for; an ASCII STL is converted to binary on upload,
since Onshape always answers binary.
"""
import json
import re
import secrets
import struct
import threading
from http.client import responses as reason_phrases
from io import BytesIO
from urllib.parse import parse_qs, urlsplit

from requests.adapters import HTTPAdapter
from urllib3.response import HTTPResponse

from testbed import settings
from testbed.scrub import STANDIN_EMAIL, STANDIN_NAME, STANDIN_USER_ID, standin_company_id

MISS_STATUS = 599
MISS_PREFIX = 'not answered in local mode: '
STORAGE_HOST = 'testbed-local.invalid'      # never resolves, so nothing could escape to it
API_PREFIX = '/api/v13'
PART_STUDIO_NAME = 'Part Studio 1'
CONFIG_ELEMENT_NAME = 'PenguinCAM-config.yaml'
CONFIG_DOCUMENT_ID = '0000000000000000000000f1'
CONFIG_WORKSPACE_ID = '0000000000000000000000f2'
CONFIG_ELEMENT_ID = '0000000000000000000000f3'
STANDIN_CLASSROOM = 'Test Bed Classroom 1'
CLASSROOM_ID = standin_company_id(1)
NAME_MAX_CHARS = 100

_HEX = r'[0-9a-f]{24}'
_ID_RE = re.compile(_HEX)
_PART_ID_RE = re.compile(r'L[0-9A-Z]{1,8}')
_ELEMENTS_RE = re.compile(rf'/documents/d/({_HEX})/w/({_HEX})/elements')
_CONFIGURATION_RE = re.compile(rf'/elements/d/({_HEX})/w/({_HEX})/e/({_HEX})/configuration')
_PARTS_RE = re.compile(rf'/parts/d/({_HEX})/w/({_HEX})/e/({_HEX})')
_STL_RE = re.compile(rf'/parts/d/({_HEX})/w/({_HEX})/e/({_HEX})/partid/([^/]+)/stl')
_STORAGE_RE = re.compile(rf'/stl/({_HEX})/([^/]+)')
_DOCUMENT_RE = re.compile(rf'/documents/({_HEX})')
_BLOB_RE = re.compile(rf'/blobelements/d/({_HEX})/w/({_HEX})/e/({_HEX})')

_lock = threading.Lock()


class UploadRefused(ValueError):
    """An uploaded file that is not an STL; the message says why, for the stand-in page."""


# ---------------------------------------------------------------------------
# The stand-in Part Studio: ids and uploaded parts
# ---------------------------------------------------------------------------

def new_part_studio_ids():
    """Fresh made-up ids for one stand-in Part Studio: {did, wid, eid}, 24 hex each."""
    return {key: secrets.token_hex(12) for key in ('did', 'wid', 'eid')}


def valid_ids(ids):
    return (isinstance(ids, dict)
            and all(isinstance(ids.get(k), str) and _ID_RE.fullmatch(ids[k])
                    for k in ('did', 'wid', 'eid')))


def _studio_dir(did):
    if not _ID_RE.fullmatch(did or ''):
        raise ValueError('not a stand-in document id')
    return settings.local_parts_dir() / did


def _manifest_path(did):
    return _studio_dir(did) / 'manifest.json'


def load_manifest(did):
    """{'wid', 'eid', 'next', 'parts': [{partId, name, bytes}]} of a stand-in Part Studio,
    or None when nothing was ever uploaded to it."""
    try:
        return json.loads(_manifest_path(did).read_text())
    except (FileNotFoundError, ValueError):
        return None


def list_parts(ids):
    """The parts uploaded to the stand-in Part Studio `ids`, in upload order."""
    manifest = load_manifest(ids['did'])
    if not manifest or manifest.get('wid') != ids['wid'] or manifest.get('eid') != ids['eid']:
        return []
    return list(manifest.get('parts', []))


def _part_id(number):
    digits = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ'
    text = ''
    while True:
        number, rest = divmod(number, 36)
        text = digits[rest] + text
        if not number:
            return 'L' + text


def _part_name(filename, taken):
    """The part's name: the file name without its .stl, made unique with ' (2)', ' (3)'."""
    stem = (filename or '').replace('\\', '/').rsplit('/', 1)[-1]
    if stem.lower().endswith('.stl'):
        stem = stem[:-4]
    stem = ''.join(ch for ch in stem if ch >= ' ' and ch != '\x7f').strip()[:NAME_MAX_CHARS] or 'Part'
    name, n = stem, 1
    while name in taken:
        n += 1
        name = f'{stem} ({n})'
    return name


def _ascii_to_binary(data):
    """A binary STL from an ASCII one; UploadRefused when it has no facets."""
    try:
        text = data.decode('ascii')
    except UnicodeDecodeError:
        raise UploadRefused('not an STL file') from None
    triangles, vertices = [], []
    for line in text.splitlines():
        words = line.split()
        if len(words) == 4 and words[0] == 'vertex':
            try:
                vertices.append(tuple(float(w) for w in words[1:]))
            except ValueError:
                raise UploadRefused('an ASCII STL vertex could not be read') from None
        elif words[:1] == ['endfacet']:
            if len(vertices) != 3:
                raise UploadRefused('an ASCII STL facet does not have three vertices')
            triangles.append(vertices)
            vertices = []
    if not triangles:
        raise UploadRefused('not an STL file: no facets')
    out = [b'converted from ASCII by the PenguinCAM test bed'.ljust(80, b' '),
           struct.pack('<I', len(triangles))]
    for a, b, c in triangles:
        out.append(struct.pack('<12fH', 0.0, 0.0, 0.0, *a, *b, *c, 0))
    return b''.join(out)


def stl_as_binary(data):
    """`data` as a binary STL: kept when its size matches a binary STL's triangle count,
    converted when it is ASCII, refused otherwise. No size or triangle cap here: the print
    path's own export and mesh checks apply them when the panel adds the part."""
    if len(data) >= 84:
        count = struct.unpack_from('<I', data, 80)[0]
        if len(data) == 84 + 50 * count and count > 0:
            return data
    if data.lstrip()[:5].lower() == b'solid':
        return _ascii_to_binary(data)
    raise UploadRefused('not an STL file')


def add_part(ids, filename, data):
    """Store one uploaded STL as a new part; returns its {partId, name, bytes}."""
    stl = stl_as_binary(data)
    with _lock:
        directory = _studio_dir(ids['did'])
        directory.mkdir(parents=True, exist_ok=True)
        manifest = load_manifest(ids['did'])
        if not manifest or manifest.get('wid') != ids['wid'] or manifest.get('eid') != ids['eid']:
            manifest = {'wid': ids['wid'], 'eid': ids['eid'], 'next': 1, 'parts': []}
        part_id = _part_id(manifest['next'])
        manifest['next'] += 1
        part = {'partId': part_id,
                'name': _part_name(filename, {p['name'] for p in manifest['parts']}),
                'bytes': len(stl)}
        (directory / f'{part_id}.stl').write_bytes(stl)
        manifest['parts'].append(part)
        _manifest_path(ids['did']).write_text(json.dumps(manifest, indent=1))
    return part


def clear_parts(ids):
    """Remove every part of the stand-in Part Studio `ids`. Part ids are never reused."""
    with _lock:
        manifest = load_manifest(ids['did'])
        if not manifest:
            return
        directory = _studio_dir(ids['did'])
        for part in manifest.get('parts', []):
            if _PART_ID_RE.fullmatch(part.get('partId', '')):
                (directory / f"{part['partId']}.stl").unlink(missing_ok=True)
        manifest['parts'] = []
        _manifest_path(ids['did']).write_text(json.dumps(manifest, indent=1))


def _part_stl(did, part_id):
    manifest = load_manifest(did)
    if not manifest or not _PART_ID_RE.fullmatch(part_id):
        return None
    if not any(p['partId'] == part_id for p in manifest.get('parts', [])):
        return None
    try:
        return (_studio_dir(did) / f'{part_id}.stl').read_bytes()
    except FileNotFoundError:
        return None


# ---------------------------------------------------------------------------
# The adapter
# ---------------------------------------------------------------------------

def _json(status, value):
    return status, {'Content-Type': 'application/json;charset=UTF-8'}, json.dumps(value).encode()


def _not_found(what):
    return _json(404, {'message': f'{what} not found', 'status': 404})


def _studio_matches(did, wid, eid=None):
    manifest = load_manifest(did)
    return bool(manifest and manifest.get('wid') == wid and (eid is None or manifest.get('eid') == eid))


def _config_yaml():
    path = settings.local_config_file()
    if path is None:
        return None
    return path.read_text()


def _answer_api(method, path, query, body):
    """(status, headers, body) for an API path under /api/v13, or None when not answered."""
    if method == 'GET' and path == '/users/sessioninfo':
        return _json(200, {'id': STANDIN_USER_ID, 'name': STANDIN_NAME, 'email': STANDIN_EMAIL,
                           'firstName': STANDIN_NAME, 'lastName': STANDIN_NAME})
    if method == 'GET' and path == '/companies':
        return _json(200, {'items': [{'id': CLASSROOM_ID, 'name': STANDIN_CLASSROOM}]})
    if method == 'POST' and path == '/documents/search':
        items = []
        if _config_yaml() is not None:
            items = [{'id': CONFIG_DOCUMENT_ID, 'name': CONFIG_ELEMENT_NAME,
                      'defaultWorkspace': {'id': CONFIG_WORKSPACE_ID}}]
        return _json(200, {'items': items})
    if method != 'GET':
        return None
    match = _DOCUMENT_RE.fullmatch(path)
    if match:
        did = match.group(1)
        if did == CONFIG_DOCUMENT_ID or load_manifest(did) is not None:
            return _json(200, {'id': did, 'name': 'Test Bed Document',
                               'owner': {'type': 1, 'id': CLASSROOM_ID,
                                         'name': STANDIN_CLASSROOM},
                               'defaultWorkspace': {'id': CONFIG_WORKSPACE_ID
                                                    if did == CONFIG_DOCUMENT_ID
                                                    else load_manifest(did)['wid']}})
        return _not_found('document')
    match = _ELEMENTS_RE.fullmatch(path)
    if match:
        did, wid = match.groups()
        if did == CONFIG_DOCUMENT_ID and wid == CONFIG_WORKSPACE_ID and _config_yaml() is not None:
            return _json(200, [{'id': CONFIG_ELEMENT_ID, 'name': CONFIG_ELEMENT_NAME,
                                'type': 'Blob', 'elementType': 'BLOB'}])
        manifest = load_manifest(did)
        if not manifest or manifest.get('wid') != wid:
            return _not_found('workspace')
        return _json(200, [{'id': manifest['eid'], 'name': PART_STUDIO_NAME,
                            'type': 'Part Studio', 'elementType': 'PARTSTUDIO'}])
    match = _BLOB_RE.fullmatch(path)
    if match:
        if match.groups() == (CONFIG_DOCUMENT_ID, CONFIG_WORKSPACE_ID, CONFIG_ELEMENT_ID):
            text = _config_yaml()
            if text is not None:
                return 200, {'Content-Type': 'application/octet-stream'}, text.encode()
        return _not_found('blob element')
    match = _CONFIGURATION_RE.fullmatch(path)
    if match:
        if not _studio_matches(*match.groups()):
            return _not_found('element')
        return _json(200, {'configurationParameters': [], 'currentConfiguration': []})
    match = _STL_RE.fullmatch(path)
    if match:
        did, wid, eid, part_id = match.groups()
        if not _studio_matches(did, wid, eid) or _part_stl(did, part_id) is None:
            return _not_found('part')
        units, mode = query.get('units', [''])[0], query.get('mode', [''])[0]
        if units != 'millimeter' or mode != 'binary':
            # The print path always asks for binary millimetres; anything else is a change
            # this stand-in does not model, so it says so rather than guess.
            return (MISS_STATUS, {'Content-Type': 'text/plain'},
                    f'{MISS_PREFIX}an STL export in units={units!r} mode={mode!r}; local mode '
                    'serves uploaded files as binary millimetres only'.encode())
        return 307, {'Location': f'https://{STORAGE_HOST}/stl/{did}/{part_id}',
                     'Content-Length': '0'}, b''
    match = _PARTS_RE.fullmatch(path)
    if match:
        did, wid, eid = match.groups()
        if not _studio_matches(did, wid, eid):
            return _not_found('element')
        return _json(200, [{'partId': p['partId'], 'name': p['name'], 'bodyType': 'solid',
                            'elementId': eid, 'isHidden': False, 'state': 'IN_CONTEXT'}
                           for p in load_manifest(did).get('parts', [])])
    return None


def answer(method, url, body=None):
    """(status, headers, body bytes) for one request in local mode; a miss is a 599."""
    parts = urlsplit(url)
    host = (parts.hostname or '').lower()
    query = parse_qs(parts.query, keep_blank_values=True)
    result = None
    if host == STORAGE_HOST and method == 'GET':
        match = _STORAGE_RE.fullmatch(parts.path)
        if match:
            stl = _part_stl(*match.groups())
            result = (200, {'Content-Type': 'application/octet-stream'}, stl) if stl is not None \
                else _not_found('stored part')
    elif host.endswith('onshape.com') and parts.path.startswith(API_PREFIX + '/'):
        result = _answer_api(method.upper(), parts.path[len(API_PREFIX):], query, body)
    if result is None:
        q = f'?{parts.query}' if parts.query else ''
        result = (MISS_STATUS, {'Content-Type': 'text/plain'},
                  f'{MISS_PREFIX}{method} {parts.netloc}{parts.path}{q}; the local test bed '
                  'answers only the print path\'s calls and never calls Onshape'.encode())
    return result


class LocalAdapter(HTTPAdapter):
    """Answers every request from the uploaded parts; never opens a socket."""

    def send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):
        status, headers, body = answer(request.method, request.url, request.body)
        raw = HTTPResponse(body=BytesIO(body), headers=headers, status=status,
                           reason=reason_phrases.get(status, ''), preload_content=False,
                           decode_content=False, request_method=request.method)
        return self.build_response(request, raw)

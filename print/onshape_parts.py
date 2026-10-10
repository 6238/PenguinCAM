"""The print path's Onshape module: panel context, REST address and the export transport.

Never imports frc_cam_gui_app. The client arrives from the caller (init_print_routes hands
the routes a getter); authentication stays inside OnshapeClient.request_absolute.
"""
import re
import threading
import time
from dataclasses import dataclass
from urllib.parse import parse_qs, quote, urljoin, urlsplit

from onshape_integration import OnshapeClient

from .limits import STL_ANGLE_TOLERANCE_RAD, STL_CHORD_TOLERANCE_M

NO_CONTEXT_MESSAGE = "Open this panel from a Part Studio or Assembly tab."
DEFAULT_SERVER = 'https://cad.onshape.com'
STL_ACCEPT = 'application/vnd.onshape.v1+octet-stream'
CONFIGURATION_MAX_CHARS = 2000
# The explicit print tessellation every export asks for (Onshape's defaults are undocumented).
STL_PARAMS = {'mode': 'binary', 'units': 'millimeter',
              'chordTolerance': f'{STL_CHORD_TOLERANCE_M:.5f}',
              'angleTolerance': f'{STL_ANGLE_TOLERANCE_RAD:.4f}'}

CONTEXT_IDS = ('documentId', 'workspaceId', 'versionId', 'microversionId', 'elementId')
_ID_RE = re.compile(r'[0-9a-fA-F]{24}')
_PLACEHOLDER_RE = re.compile(r'\{\$\w+\}')
_REDIRECTS = (301, 302, 303, 307, 308)
_CHUNK = 65536


def is_onshape_host(host):
    """True for onshape.com and any *.onshape.com host."""
    host = (host or '').lower()
    return host == 'onshape.com' or host.endswith('.onshape.com')


def clean_onshape_id(value):
    """'' for an empty value or an unsubstituted placeholder (anything with '{' or '$').

    The rule of the app's _clean_onshape_id, written here so the blueprint never imports
    the app: opened on a version, the panel URL carries a literal {$workspaceId}.
    """
    if not value:
        return ''
    text = str(value).strip()
    if '{' in text or '$' in text:
        return ''
    return text


def _server_origin(value):
    """`value` as an https origin on an Onshape host, else DEFAULT_SERVER."""
    try:
        parts = urlsplit(value or '')
        port = parts.port
    except ValueError:
        return DEFAULT_SERVER
    if (parts.scheme != 'https' or not is_onshape_host(parts.hostname) or parts.username
            or parts.password or parts.path not in ('', '/') or parts.query or parts.fragment):
        return DEFAULT_SERVER
    return f'https://{parts.hostname}' + (f':{port}' if port else '')


def panel_context_from_return(return_url):
    """The Onshape panel's ids, server and configuration, read from the return URL's query.

    Ids are kept raw when they are 24 hex characters or a `{$name}` placeholder (Onshape's
    messages tolerate a literal placeholder but not an empty id), otherwise ''.
    """
    query = parse_qs(urlsplit(return_url or '').query, keep_blank_values=True)

    def first(name):
        values = query.get(name)
        return values[0] if values else None

    context = {}
    for name in CONTEXT_IDS:
        value = first(name) or ''
        context[name] = value if _ID_RE.fullmatch(value) or _PLACEHOLDER_RE.fullmatch(value) else ''
    context['server'] = _server_origin(first('server'))
    configuration = first('configuration')
    if configuration is not None:
        context['configuration'] = configuration[:CONFIGURATION_MAX_CHARS]
    return context


class NoOnshapeContext(Exception):
    message = NO_CONTEXT_MESSAGE

    def __init__(self):
        super().__init__(NO_CONTEXT_MESSAGE)


@dataclass(frozen=True)
class OnshapeAddress:
    document_id: str
    wvm: str                    # 'w', 'v' or 'm'
    wvm_id: str
    element_id: str
    configuration: str | None = None

    @property
    def document_path(self):
        kind = {'w': 'workspace_id', 'v': 'version_id', 'm': 'microversion_id'}[self.wvm]
        return OnshapeClient._wvm_path(self.document_id, **{kind: self.wvm_id})

    @property
    def element_path(self):
        return f'{self.document_path}/e/{self.element_id}'


def rest_address(context):
    """The REST address of a panel context, exactly as the CNC export resolves it.

    Prefers the workspace, then the version, then the microversion (OnshapeClient._wvm_path).
    Raises NoOnshapeContext without a document, an element or any of the three.
    """
    did = clean_onshape_id(context.get('documentId'))
    eid = clean_onshape_id(context.get('elementId'))
    if not did or not eid:
        raise NoOnshapeContext()
    try:
        path = OnshapeClient._wvm_path(did, clean_onshape_id(context.get('workspaceId')),
                                       clean_onshape_id(context.get('versionId')),
                                       clean_onshape_id(context.get('microversionId')))
    except ValueError:
        raise NoOnshapeContext() from None
    _d, _did, wvm, wvm_id = path.split('/')
    configuration = context.get('configuration') or None
    if configuration is not None and _PLACEHOLDER_RE.fullmatch(configuration):
        configuration = None        # {$configuration} left unsubstituted: no configuration
    return OnshapeAddress(did, wvm, wvm_id, eid, configuration)


class DownloadTooLarge(Exception):
    """The body passed the byte cap; the response was closed."""


class ExportHTTPError(RuntimeError):
    """An export that did not end in a 2xx answer. A RuntimeError, as the test bed expects.

    `calls` is how many HTTP requests the export sent before it failed.
    """

    def __init__(self, status, message, calls=1):
        super().__init__(message)
        self.status = status
        self.calls = calls


def _read_capped(response, byte_cap):
    chunks, size = [], 0
    for chunk in response.iter_content(_CHUNK):
        size += len(chunk)
        if byte_cap is not None and size > byte_cap:
            response.close()
            raise DownloadTooLarge(f'download passed {byte_cap} bytes')
        chunks.append(chunk)
    return b''.join(chunks)


def fetch_following_redirects(client, path, params=None, *, headers=None, byte_cap=None, max_hops=3):
    """GET an API path, following its redirects by hand: (body, the URL of each hop).

    requests would drop the authentication on a hop to another host, and Onshape requires
    it on its own hosts, so the hops are followed here: an onshape.com host is fetched
    through client.request_absolute (the client's credentials), any other host with no
    authentication at all. `headers` go on the request and on every hop.
    """
    response = client._make_api_request('GET', path, params=params, headers=headers,
                                        allow_redirects=False, stream=True)
    hops = []
    while response.status_code in _REDIRECTS:
        if len(hops) == max_hops:
            response.close()
            raise ExportHTTPError(310, f'export still redirects after {max_hops} hops', len(hops) + 1)
        location = response.headers.get('Location')
        if not location:
            response.close()
            raise ExportHTTPError(response.status_code,
                                  f'export answered {response.status_code} without Location', len(hops) + 1)
        url = urljoin(response.url, location)
        response.close()
        hops.append(url)
        if is_onshape_host(urlsplit(url).hostname):
            response = client.request_absolute('GET', url, headers=headers,
                                               allow_redirects=False, stream=True)
        else:
            response = client.session.get(url, headers=headers, allow_redirects=False, stream=True)
    if not 200 <= response.status_code < 300:
        response.close()
        raise ExportHTTPError(response.status_code, f'export answered HTTP {response.status_code}',
                              len(hops) + 1)
    try:
        return _read_capped(response, byte_cap), hops
    except DownloadTooLarge as exc:
        exc.calls = len(hops) + 1
        raise


# ---------------------------------------------------------------------------
# Resolving selections and exporting parts (spec 3.3)
# ---------------------------------------------------------------------------

ELEMENT_CACHE_TTL_S = 600

NOT_SOLID_MESSAGE = "Only solid parts can be printed: {name} was skipped."
STANDARD_CONTENT_MESSAGE = "{name} is standard content (hardware) and is not printed."
CONFIGURED_MESSAGE = ("This Part Studio has configurations. Use Add from another document\u2026 "
                      "to pick the configuration to print.")
DUPLICATE_NAME_MESSAGE = "Two parts are named {name}; rename one in Onshape."
NOT_IN_ASSEMBLY_MESSAGE = ("Could not find the selected part in this assembly; "
                           "use Add from another document\u2026")
NOT_IN_PART_STUDIO_MESSAGE = ("Could not find the selected part in this Part Studio; "
                              "use Add from another document\u2026")
EXPORT_FAILED_MESSAGE = "Onshape could not export {name}. Try again, or Refresh from Onshape."
LIMIT_REACHED_MESSAGE = "Onshape refused the export (API limit reached)."

VIAS = ('selection', 'dialog', 'refresh')
_PART_ID_RE = re.compile(r'[A-Za-z0-9_.:+=-]{1,100}')
_SELECTION_ID_KEYS = ('selectionId', 'bodyId', 'entityId')
_NOT_SOLID_FLAGS = ('isSurface', 'isComposite', 'isSketch', 'isFlattenedBody')
_INSTANCE_NUMBER_RE = re.compile(r' <\d+>$')


@dataclass(frozen=True)
class PartSource:
    """Where one part is exported from: the durable identity the browser holds for refresh."""
    document_id: str
    wvm: str
    wvm_id: str
    element_id: str
    part_id: str
    configuration: str | None = None
    link_document_id: str | None = None

    @property
    def address(self):
        return OnshapeAddress(self.document_id, self.wvm, self.wvm_id, self.element_id,
                              self.configuration)

    def to_json(self):
        data = {'documentId': self.document_id, 'wvm': self.wvm, 'wvmId': self.wvm_id,
                'elementId': self.element_id, 'partId': self.part_id}
        if self.configuration:
            data['configuration'] = self.configuration
        if self.link_document_id:
            data['linkDocumentId'] = self.link_document_id
        return data

    @classmethod
    def from_json(cls, data):
        """A PartSource from browser-held JSON; ValueError unless every field is valid."""
        if not isinstance(data, dict):
            raise ValueError('a part source is an object')

        def hex_id(key, required=True):
            value = data.get(key)
            if value in (None, '') and not required:
                return None
            if not isinstance(value, str) or not _ID_RE.fullmatch(value):
                raise ValueError(f'{key} is not an Onshape id')
            return value

        wvm, part_id = data.get('wvm'), data.get('partId')
        if wvm not in ('w', 'v', 'm'):
            raise ValueError('wvm is not w, v or m')
        if not isinstance(part_id, str) or not _PART_ID_RE.fullmatch(part_id):
            raise ValueError('partId is not a part id')
        configuration = data.get('configuration')
        if configuration is not None and not isinstance(configuration, str):
            raise ValueError('configuration is not a string')
        return cls(hex_id('documentId'), wvm, hex_id('wvmId'), hex_id('elementId'), part_id,
                   configuration[:CONFIGURATION_MAX_CHARS] if configuration else None,
                   hex_id('linkDocumentId', required=False))

    def key(self):
        """What makes two sources the same part, for de-duplication."""
        return (self.document_id, self.wvm, self.wvm_id, self.element_id, self.part_id,
                self.configuration or '')


@dataclass(frozen=True)
class ResolvedPart:
    name: str
    source: PartSource
    fallback_name: str | None = None        # the dialog's partName, for the idTag fallback


class PartRefused(Exception):
    def __init__(self, message, name=None):
        super().__init__(message)
        self.message = message
        self.name = name


class OnshapeCallFailed(RuntimeError):
    """A resolution call (elements, configuration, parts, assembly) did not answer 200."""

    def __init__(self, what, status):
        super().__init__(f'{what} answered HTTP {status}')
        self.status = status


class ExportFailed(Exception):
    def __init__(self, name, status, reason):
        super().__init__(f'export of {name} failed: {reason}')
        self.name, self.status, self.reason = name, status, reason


class PartTooLarge(Exception):
    def __init__(self, name):
        super().__init__(f'{name} passed the download cap')
        self.name = name


class OnshapeLimitReached(Exception):
    def __init__(self, name=None):
        super().__init__('Onshape answered 402: the API allowance is used up')
        self.name = name


def _get_json(client, what, path, params=None):
    response = client._make_api_request('GET', path, params=params or None)
    if response.status_code == 402:
        raise OnshapeLimitReached()
    if response.status_code != 200:
        raise OnshapeCallFailed(what, response.status_code)
    return response.json()


class ElementCache:
    """Element answers kept for ten minutes, keyed (document, wvm id, element, configuration).

    In process memory, never in the Flask session cookie; it assumes the one-worker
    Procfile, like the part store. Thread-safe. `fresh=True` fetches past the cache and
    stores the new answer.
    """

    def __init__(self, ttl_s=ELEMENT_CACHE_TTL_S, clock=time.time):
        self.ttl_s = ttl_s
        self.clock = clock
        self._entries = {}
        self._lock = threading.Lock()

    def _get(self, kind, address, fetch, fresh, extra=()):
        key = (kind, address.document_id, address.wvm_id, address.element_id,
               address.configuration, *extra)
        if not fresh:
            with self._lock:
                entry = self._entries.get(key)
                if entry and self.clock() - entry[0] < self.ttl_s:
                    return entry[1]
        value = fetch()
        with self._lock:
            self._entries[key] = (self.clock(), value)
        return value

    def element_type(self, client, address, fresh=False):
        def fetch():
            items = _get_json(client, 'elements', f'/documents/{address.document_path}/elements',
                              {'elementId': address.element_id})
            items = items if isinstance(items, list) else []
            match = next((e for e in items if e.get('id') == address.element_id), None)
            return (match or (items[0] if items else {})).get('elementType', '')
        return self._get('type', address, fetch, fresh)

    def configuration_parameters(self, client, address, fresh=False):
        def fetch():
            answer = _get_json(client, 'configuration', f'/elements/{address.element_path}/configuration')
            return list(answer.get('configurationParameters') or [])
        return self._get('configuration', address, fetch, fresh)

    def assembly_definition(self, client, address, fresh=False):
        def fetch():
            params = {'configuration': address.configuration} if address.configuration else None
            return _get_json(client, 'assembly', f'/assemblies/{address.element_path}', params)
        return self._get('assembly', address, fetch, fresh)

    def parts_list(self, client, address, fresh=False, link_document_id=None):
        def fetch():
            params = {}
            if address.configuration:
                params['configuration'] = address.configuration
            if link_document_id:
                params['linkDocumentId'] = link_document_id
            answer = _get_json(client, 'parts', f'/parts/{address.element_path}', params)
            return answer if isinstance(answer, list) else []
        return self._get('parts', address, fetch, fresh, (link_document_id,))


ELEMENT_CACHE = ElementCache()


def _link_for(address, document_id):
    """The open document as linkDocumentId when the part lives in another document."""
    return address.document_id if document_id != address.document_id else None


def _find_part(parts, part_id):
    return next((p for p in parts if p.get('partId') == part_id), None)


def _resolve_partstudio(client, address, selections, cache):
    if address.configuration is None and cache.configuration_parameters(client, address):
        return [], [{'message': CONFIGURED_MESSAGE} for _ in selections]
    parts_list = cache.parts_list(client, address)
    refetched = False
    resolved, errors = [], []
    for selection in selections:
        part_id = selection.get('partId') or next(
            (selection.get(k) for k in _SELECTION_ID_KEYS if selection.get(k)), None)
        part = _find_part(parts_list, part_id) if isinstance(part_id, str) else None
        if part is None and isinstance(part_id, str) and not refetched:
            parts_list, refetched = cache.parts_list(client, address, fresh=True), True
            part = _find_part(parts_list, part_id)
        if part is None:
            errors.append({'message': NOT_IN_PART_STUDIO_MESSAGE})
            continue
        name = str(part.get('name') or part_id)
        if part.get('bodyType', 'solid') != 'solid':
            errors.append({'name': name, 'message': NOT_SOLID_MESSAGE.format(name=name)})
            continue
        resolved.append(ResolvedPart(name, PartSource(address.document_id, address.wvm, address.wvm_id,
                                                      address.element_id, part_id, address.configuration)))
    return resolved, errors


def _walk_occurrence(definition, path):
    """The part instance an occurrence path names, or None."""
    if not isinstance(path, list) or not path:
        return None
    instances = (definition.get('rootAssembly') or {}).get('instances') or []
    subs = definition.get('subAssemblies') or []
    instance = None
    for depth, instance_id in enumerate(path):
        instance = next((i for i in instances if i.get('id') == instance_id), None)
        if instance is None:
            return None
        if depth == len(path) - 1:
            break
        sub = next((s for s in subs if s.get('documentId') == instance.get('documentId')
                    and s.get('elementId') == instance.get('elementId')
                    and s.get('documentMicroversion', instance.get('documentMicroversion'))
                    == instance.get('documentMicroversion')
                    and (s.get('configuration') or '') in ('', instance.get('configuration') or '')),
                   None)
        if sub is None:
            return None
        instances = sub.get('instances') or []
    return instance if instance and instance.get('partId') else None


def _resolve_assembly(client, address, selections, cache):
    definition = cache.assembly_definition(client, address)
    refetched = False
    resolved, errors = [], []
    for selection in selections:
        path = selection.get('occurrencePath')
        instance = _walk_occurrence(definition, path)
        if instance is None and path and not refetched:
            definition, refetched = cache.assembly_definition(client, address, fresh=True), True
            instance = _walk_occurrence(definition, path)
        if instance is None:
            errors.append({'message': NOT_IN_ASSEMBLY_MESSAGE})
            continue
        name = _INSTANCE_NUMBER_RE.sub('', str(instance.get('name') or instance['partId']))
        if instance.get('isStandardContent'):
            errors.append({'name': name, 'message': STANDARD_CONTENT_MESSAGE.format(name=name)})
            continue
        document_id = instance.get('documentId') or address.document_id
        if instance.get('documentVersion'):
            wvm, wvm_id = 'v', instance['documentVersion']
        elif document_id == address.document_id:
            wvm, wvm_id = address.wvm, address.wvm_id
        else:
            wvm, wvm_id = 'm', instance.get('documentMicroversion')
        configuration = (instance.get('configuration') or '')[:CONFIGURATION_MAX_CHARS] or None
        try:
            source = PartSource.from_json({
                'documentId': document_id, 'wvm': wvm, 'wvmId': wvm_id,
                'elementId': instance.get('elementId'), 'partId': instance['partId'],
                'configuration': configuration,
                'linkDocumentId': _link_for(address, document_id)})
        except ValueError:
            errors.append({'name': name, 'message': EXPORT_FAILED_MESSAGE.format(name=name)})
            continue
        resolved.append(ResolvedPart(name, source))
    return resolved, errors


def _resolve_dialog(address, items):
    resolved, errors = [], []
    for item in items:
        name = str(item.get('partName') or 'part')
        if any(item.get(flag) for flag in _NOT_SOLID_FLAGS):
            errors.append({'name': name, 'message': NOT_SOLID_MESSAGE.format(name=name)})
            continue
        document_id = item.get('documentId')
        wvm, wvm_id = ('w', item.get('workspaceId')) if item.get('workspaceId') else ('v', item.get('versionId'))
        try:
            source = PartSource.from_json({
                'documentId': document_id, 'wvm': wvm, 'wvmId': wvm_id,
                'elementId': item.get('elementId'), 'partId': item.get('idTag'),
                'configuration': item.get('elementConfiguration') or None,
                'linkDocumentId': _link_for(address, document_id)})
        except ValueError:
            errors.append({'name': name, 'message': EXPORT_FAILED_MESSAGE.format(name=name)})
            continue
        resolved.append(ResolvedPart(name, source, fallback_name=item.get('partName') or None))
    return resolved, errors


def _resolve_refresh(selections):
    resolved, errors = [], []
    for selection in selections:
        name = str(selection.get('name') or 'part')
        try:
            resolved.append(ResolvedPart(name, PartSource.from_json(selection.get('source'))))
        except ValueError:
            errors.append({'name': name, 'message': EXPORT_FAILED_MESSAGE.format(name=name)})
    return resolved, errors


def resolve_selections(client, address, selections, via, cache=ELEMENT_CACHE):
    """(resolved parts, errors) for the browser's selections; errors are {'name'?, 'message'}.

    `via` is 'selection' (a click in the open element), 'dialog' (Add from another
    document) or 'refresh' (sources the browser already holds). Only 'selection' makes
    calls: the element type, then the Part Studio's configuration and parts list or the
    assembly definition, all cached. A part id or occurrence missing from a cached answer
    refetches that answer once per request before the selection is refused (R16).
    """
    if via not in VIAS:
        raise ValueError(f'via is not one of {VIAS}')
    selections = [s for s in selections if isinstance(s, dict)] if isinstance(selections, list) else []
    if via == 'dialog':
        return _resolve_dialog(address, selections)
    if via == 'refresh':
        return _resolve_refresh(selections)
    element_type = cache.element_type(client, address)
    if element_type == 'PARTSTUDIO':
        return _resolve_partstudio(client, address, selections, cache)
    if element_type == 'ASSEMBLY':
        return _resolve_assembly(client, address, selections, cache)
    return [], [{'message': NO_CONTEXT_MESSAGE} for _ in selections]


@dataclass
class ExportResult:
    stl: bytes
    source: PartSource
    calls: int
    ms: int
    hops: list


def _export_source(client, source, byte_cap):
    path = (f'/parts/{source.address.element_path}/partid/{quote(source.part_id, safe="")}/stl')
    params = dict(STL_PARAMS)
    if source.configuration:
        params['configuration'] = source.configuration
    if source.link_document_id:
        params['linkDocumentId'] = source.link_document_id
    return fetch_following_redirects(client, path, params, headers={'Accept': STL_ACCEPT},
                                     byte_cap=byte_cap)


def export_part_mesh(client, part, *, byte_cap, cache=ELEMENT_CACHE):
    """Export one part's binary STL in millimetres with the print tessellation.

    A 402 raises OnshapeLimitReached and a body past `byte_cap` PartTooLarge. A dialog
    pick whose idTag export fails is matched by name in its element's parts list: one
    match is exported instead (and its source returned), several raise PartRefused, none
    ExportFailed. `calls` counts every HTTP request, redirect hops included.
    """
    started = time.monotonic()
    calls = 0
    source = part.source

    def export(src):
        nonlocal calls
        try:
            stl, hops = _export_source(client, src, byte_cap)
        except ExportHTTPError as exc:
            calls += exc.calls
            if exc.status == 402:
                raise OnshapeLimitReached(part.name) from None
            raise
        except DownloadTooLarge as exc:
            calls += getattr(exc, 'calls', 1)
            raise PartTooLarge(part.name) from None
        calls += len(hops) + 1
        return stl, hops

    try:
        stl, hops = export(source)
    except ExportHTTPError as exc:
        if not part.fallback_name:
            raise ExportFailed(part.name, exc.status, str(exc)) from None
        try:
            listed = cache.parts_list(client, source.address, fresh=True,
                                      link_document_id=source.link_document_id)
        except OnshapeCallFailed as failed:
            calls += 1
            raise ExportFailed(part.name, failed.status, str(failed)) from None
        calls += 1
        matches = [p for p in listed if p.get('name') == part.fallback_name and p.get('partId')]
        if len(matches) > 1:
            raise PartRefused(DUPLICATE_NAME_MESSAGE.format(name=part.fallback_name),
                              part.fallback_name) from None
        if not matches:
            raise ExportFailed(part.name, exc.status, f'{exc}; no part named so') from None
        source = PartSource(source.document_id, source.wvm, source.wvm_id, source.element_id,
                            str(matches[0]['partId']), source.configuration, source.link_document_id)
        try:
            stl, hops = export(source)
        except ExportHTTPError as retry:
            raise ExportFailed(part.name, retry.status, str(retry)) from None
    return ExportResult(stl, source, calls, round((time.monotonic() - started) * 1000), hops)

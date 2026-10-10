"""The print path's Onshape module: panel context, REST address and the export transport.

Never imports frc_cam_gui_app. The client arrives from the caller (init_print_routes hands
the routes a getter); authentication stays inside OnshapeClient.request_absolute.
"""
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urljoin, urlsplit

from onshape_integration import OnshapeClient

NO_CONTEXT_MESSAGE = "Open this panel from a Part Studio or Assembly tab."
DEFAULT_SERVER = 'https://cad.onshape.com'
STL_ACCEPT = 'application/vnd.onshape.v1+octet-stream'
CONFIGURATION_MAX_CHARS = 2000

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
    """An export that did not end in a 2xx answer. A RuntimeError, as the test bed expects."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


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
            raise ExportHTTPError(310, f'export still redirects after {max_hops} hops')
        location = response.headers.get('Location')
        if not location:
            response.close()
            raise ExportHTTPError(response.status_code,
                                  f'export answered {response.status_code} without Location')
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
        raise ExportHTTPError(response.status_code, f'export answered HTTP {response.status_code}')
    return _read_capped(response, byte_cap), hops

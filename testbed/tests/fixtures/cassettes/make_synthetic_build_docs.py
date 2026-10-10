"""Regenerate synthetic-build-docs.json: a full build into an empty test folder.

    uv run python testbed/tests/fixtures/cassettes/make_synthetic_build_docs.py

Runs the real builder in replay mode with the replay adapter swapped for a fake Onshape
that answers with made-up ids, and saves what was asked and answered as the cassette.
Run it after any change to a request body the builder sends.
"""
import json
import os
import re
import sys
import tempfile
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[3]))          # the repository root
os.environ['PENGUINCAM_TESTBED'] = 'replay'
os.environ['XDG_STATE_HOME'] = tempfile.mkdtemp()

from urllib3.response import HTTPResponse  # noqa: E402

from onshape_integration import OnshapeClient  # noqa: E402
from testbed import documents, settings  # noqa: E402
from testbed.adapters import ReplayAdapter  # noqa: E402
from testbed.cassette import Cassette, Exchange  # noqa: E402

SCENARIO = 'synthetic-build-docs'
DIDS = {spec.name: f'd{i:023d}' for i, spec in enumerate(documents.TEST_DOCUMENTS, start=1)}
SPECS = {spec.name: spec for spec in documents.TEST_DOCUMENTS}
created, exchanges, features = [], [], {}


def _wid(did):
    return 'w' + did[1:]


def _eid(did, kind='p'):
    return 'e' + did[1:-1] + kind


def _name_of(did):
    return next(name for name, value in DIDS.items() if value == did)


def answer(method, path, body):
    p = path[len('/api/v13'):]
    if method == 'GET' and p == '/documents':
        return {'items': [{'id': DIDS[n], 'name': n, 'description': documents.TEST_DESCRIPTION,
                           'defaultWorkspace': {'id': _wid(DIDS[n])}} for n in created]}
    if method == 'POST' and p == '/documents':
        created.append(body['name'])
        did = DIDS[body['name']]
        return {'id': did, 'name': body['name'], 'description': body['description'],
                'defaultWorkspace': {'id': _wid(did)}}
    if m := re.fullmatch(r'/documents/d/(\w+)/w/\w+/elements', p):
        return [{'id': _eid(m[1]), 'name': 'Part Studio 1', 'elementType': 'PARTSTUDIO'},
                {'id': _eid(m[1], 'a'), 'name': 'Assembly 1', 'elementType': 'ASSEMBLY'}]
    if m := re.fullmatch(r'/partstudios/d/(\w+)/w/\w+/e/\w+/features', p):
        n = features[m[1]] = features.get(m[1], 0) + 1
        return {'feature': dict(body['feature'], featureId=f'F{m[1][-2:]}f{n}'),
                'featureState': {'featureStatus': 'OK'}}
    if m := re.fullmatch(r'/parts/d/(\w+)/w/\w+/e/\w+', p):
        count = len(SPECS[_name_of(m[1])].part_names)
        return [{'partId': f'J{m[1][-2:]}P{i}', 'name': f'Part {i}'} for i in range(1, count + 1)]
    if re.fullmatch(r'/elements/d/\w+/w/\w+/e/\w+/configuration', p):
        return {'btType': 'BTConfigurationResponse-2019',
                'configurationParameters': body['configurationParameters'],
                'currentConfiguration': body['currentConfiguration']}
    if p.startswith('/metadata/') or p.endswith('/instances'):
        return {}
    if m := re.fullmatch(r'/documents/d/(\w+)/versions', p):
        return {'id': 'v' + m[1][1:], 'name': body['name']}
    if m := re.fullmatch(r'/assemblies/d/(\w+)/w/\w+', p):
        return {'id': _eid(m[1], 'b'), 'name': body['name']}
    raise SystemExit(f'the fake Onshape has no answer for {method} {p}')


def send(adapter, request, **kwargs):
    parts = urlsplit(request.url)
    sent = request.body.decode() if isinstance(request.body, bytes) else request.body
    text = json.dumps(answer(request.method, parts.path, json.loads(sent) if sent else None))
    exchanges.append(Exchange(
        method=request.method, host=parts.netloc, path=parts.path,
        query=parse_qs(parts.query, keep_blank_values=True), request_body=sent, status=200,
        headers={'Content-Type': 'application/json;charset=UTF-8'}, body_text=text,
        body_file=None))
    raw = HTTPResponse(body=BytesIO(text.encode()), headers={'Content-Type': 'application/json'},
                       status=200, preload_content=False, request_method=request.method)
    return adapter.build_response(request, raw)


if __name__ == '__main__':
    ReplayAdapter.send = send
    settings.DOCUMENTS_FILE = Path(tempfile.mkdtemp()) / 'documents.json'
    documents.build_test_documents(OnshapeClient.from_api_keys(), 'f00000000000000000000f01')
    path = Cassette(SCENARIO, exchanges, synthetic=True).save(HERE)
    print(f'{path}: {len(exchanges)} exchanges')

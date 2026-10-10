"""A replay client for the print path's Onshape tests: an API-key client answered from a cassette.

No network: the replay adapter answers every request, on any host, from the current
scenario's cassette, and a request it has no answer for gets a 599.
"""
import contextlib
import socket
from pathlib import Path
from unittest import mock

from onshape_integration import OnshapeClient
from testbed import adapters, settings
from testbed.adapters import ReplayAdapter, set_current_scenario
from testbed.cassette import Exchange

FIXTURE_CASSETTES = Path(__file__).resolve().parents[2] / 'testbed' / 'tests' / 'fixtures' / 'cassettes'


def exchange(path, body='', status=200, host='cad.onshape.com', query=None, headers=None,
             body_bytes=None):
    return Exchange(method='GET', host=host, path=path, query=query or {}, request_body=None,
                    status=status, headers=headers or {}, body_text=None if body_bytes else body,
                    body_file=None, body_bytes=body_bytes)


@contextlib.contextmanager
def replay_client(scenario, cassette_dir=FIXTURE_CASSETTES):
    """Yield (client, sent): `sent` lists (url, headers) of every request the adapter saw."""
    sent = []
    real_send = ReplayAdapter.send

    def spy(adapter, request, **kwargs):
        sent.append((request.url, dict(request.headers)))
        return real_send(adapter, request, **kwargs)

    with mock.patch.object(settings, 'CASSETTE_DIR', Path(cassette_dir)), \
            mock.patch.object(ReplayAdapter, 'send', spy), \
            mock.patch.object(socket.socket, 'connect', side_effect=AssertionError('network used')):
        adapters.reset_testbed_adapters_for_tests()
        try:
            client = OnshapeClient.from_api_keys('k', 's')
            client.session.mount('https://', ReplayAdapter())
            client.session.mount('http://', ReplayAdapter())
            set_current_scenario(scenario)
            yield client, sent
        finally:
            adapters.reset_testbed_adapters_for_tests()

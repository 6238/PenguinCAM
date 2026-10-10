"""OnshapeClient.request_absolute: one place for authentication, for API paths and absolute URLs.

The print path follows an export's redirect to an absolute URL with the client's own
credentials, so the token never leaves the client; _make_api_request is that call with the
API base prefixed.
"""
import unittest
from datetime import datetime, timedelta
from unittest import mock

from onshape_integration import OnshapeClient

URL = 'https://cad.onshape.com/api/v13/blob/part.stl'


def _response(status):
    response = mock.MagicMock()
    response.status_code = status
    return response


def _oauth_client(token='old-token'):
    config = {'client_id': 'test-client', 'client_secret': 'test-secret'}
    with mock.patch.object(OnshapeClient, '_load_config', return_value=config):
        client = OnshapeClient()
    client.access_token = token
    client.refresh_token = 'refresh-token'
    client.token_expires = datetime.now() + timedelta(hours=5)
    return client


class RequestAbsoluteTest(unittest.TestCase):
    def test_apikey_absolute_url_gets_basic_auth(self):
        client = OnshapeClient.from_api_keys('k', 's')
        with mock.patch.object(client.session, 'request', return_value=_response(200)) as request:
            response = client.request_absolute('GET', URL, allow_redirects=False)
        self.assertEqual(response.status_code, 200)
        args, kwargs = request.call_args
        self.assertEqual(args, ('GET', URL))
        self.assertEqual(kwargs['auth'], ('k', 's'))
        self.assertNotIn('Authorization', kwargs['headers'])
        self.assertIs(kwargs['allow_redirects'], False)

    def test_oauth_absolute_url_gets_bearer(self):
        client = _oauth_client()
        headers = {'Accept': 'application/vnd.onshape.v1+octet-stream'}
        with mock.patch.object(client.session, 'request', return_value=_response(200)) as request:
            client.request_absolute('GET', URL, headers=headers)
        args, kwargs = request.call_args
        self.assertEqual(args, ('GET', URL))
        self.assertEqual(kwargs['headers']['Authorization'], 'Bearer old-token')
        self.assertEqual(kwargs['headers']['Accept'], headers['Accept'])
        # The caller's dict is not written to: it is reused on hops to other hosts.
        self.assertEqual(headers, {'Accept': 'application/vnd.onshape.v1+octet-stream'})

    def test_401_refreshes_once_and_retries(self):
        client = _oauth_client()

        def refresh():
            client.access_token = 'new-token'
            return True
        with mock.patch.object(client.session, 'request',
                               side_effect=[_response(401), _response(200)]) as request, \
                mock.patch.object(client, 'refresh_access_token', side_effect=refresh) as refreshed:
            response = client.request_absolute('GET', URL)
        self.assertEqual(response.status_code, 200)
        refreshed.assert_called_once_with()
        self.assertEqual(request.call_count, 2)
        self.assertEqual(request.call_args_list[1].args, ('GET', URL))
        self.assertEqual(request.call_args_list[1].kwargs['headers']['Authorization'],
                         'Bearer new-token')

    def test_make_api_request_prefixes_api_base(self):
        client = OnshapeClient.from_api_keys('k', 's')
        with mock.patch.object(client, 'request_absolute', return_value=_response(200)) as absolute:
            client._make_api_request('GET', '/users/sessioninfo', params={'a': 1})
        absolute.assert_called_once_with('GET', 'https://cad.onshape.com/api/v13/users/sessioninfo',
                                         params={'a': 1})


if __name__ == '__main__':
    unittest.main()

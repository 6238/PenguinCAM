"""
Tests for Onshape 401 handling in OnshapeClient._make_api_request.

Background: refresh used to be driven only by a local expiry timestamp, so a token
Onshape retired early (e.g. because the user re-authorized elsewhere) was never
reconsidered -- the client replayed it forever and every call 401'd until the user
manually re-authorized. These pin the 401-driven refresh-and-retry.
"""

import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from onshape_integration import OnshapeClient, OnshapeAuthError


def _response(status):
    r = MagicMock()
    r.status_code = status
    return r


def _client(access_token='dead-token', refresh_token='refresh-token'):
    config = {'client_id': 'test-client', 'client_secret': 'test-secret'}
    with patch.object(OnshapeClient, '_load_config', return_value=config):
        c = OnshapeClient()
    c.auth_mode = 'oauth'
    c.access_token = access_token
    c.refresh_token = refresh_token
    # Far future: the clock-based check must NOT be what triggers the refresh.
    c.token_expires = datetime.now() + timedelta(hours=5)
    c.session = MagicMock()
    return c


class TestUnauthorizedRetry(unittest.TestCase):

    def test_401_triggers_refresh_and_retry(self):
        c = _client()
        c.session.request.side_effect = [_response(401), _response(200)]

        def fake_refresh():
            c.access_token = 'fresh-token'
            c.tokens_refreshed = True
            return True

        with patch.object(c, 'refresh_access_token', side_effect=fake_refresh) as refresh:
            resp = c._make_api_request('GET', '/documents')

        self.assertEqual(resp.status_code, 200)
        refresh.assert_called_once()
        self.assertEqual(c.session.request.call_count, 2)

    def test_retry_uses_the_new_token(self):
        """The replay must carry the refreshed token, not the dead one."""
        c = _client()
        c.session.request.side_effect = [_response(401), _response(200)]

        def fake_refresh():
            c.access_token = 'fresh-token'
            return True

        with patch.object(c, 'refresh_access_token', side_effect=fake_refresh):
            c._make_api_request('GET', '/documents')

        second_call = c.session.request.call_args_list[1]
        self.assertEqual(second_call.kwargs['headers']['Authorization'], 'Bearer fresh-token')

    def test_failed_refresh_raises_auth_error(self):
        c = _client()
        c.session.request.return_value = _response(401)

        with patch.object(c, 'refresh_access_token', return_value=False):
            with self.assertRaises(OnshapeAuthError):
                c._make_api_request('GET', '/documents')

    def test_persistent_401_after_refresh_raises_auth_error(self):
        """Refresh succeeded but Onshape still says no -- don't loop, surface it."""
        c = _client()
        c.session.request.side_effect = [_response(401), _response(401)]

        with patch.object(c, 'refresh_access_token', return_value=True):
            with self.assertRaises(OnshapeAuthError):
                c._make_api_request('GET', '/documents')

        self.assertEqual(c.session.request.call_count, 2)  # exactly one retry

    def test_success_does_not_refresh(self):
        c = _client()
        c.session.request.return_value = _response(200)

        with patch.object(c, 'refresh_access_token') as refresh:
            resp = c._make_api_request('GET', '/documents')

        self.assertEqual(resp.status_code, 200)
        refresh.assert_not_called()

    def test_other_errors_pass_through_untouched(self):
        """A 404 is a real answer, not an auth problem."""
        c = _client()
        c.session.request.return_value = _response(404)

        with patch.object(c, 'refresh_access_token') as refresh:
            resp = c._make_api_request('GET', '/documents')

        self.assertEqual(resp.status_code, 404)
        refresh.assert_not_called()

    def test_missing_token_raises_auth_error(self):
        c = _client(access_token=None)
        with self.assertRaises(OnshapeAuthError):
            c._make_api_request('GET', '/documents')


class TestRefreshMarksTokensForPersistence(unittest.TestCase):

    def test_successful_refresh_sets_tokens_refreshed(self):
        """The after_request hook keys off this flag to write tokens back."""
        c = _client()
        payload = {'access_token': 'new-access', 'refresh_token': 'new-refresh',
                   'expires_in': 3600}
        ok = _response(200)
        ok.json.return_value = payload

        with patch('onshape_integration.requests.post', return_value=ok):
            result = c.refresh_access_token()

        self.assertTrue(result)
        self.assertTrue(c.tokens_refreshed)
        self.assertEqual(c.access_token, 'new-access')
        # A rotated refresh token must be kept, or the NEXT refresh fails.
        self.assertEqual(c.refresh_token, 'new-refresh')

    def test_refresh_keeps_old_refresh_token_when_not_rotated(self):
        c = _client(refresh_token='original-refresh')
        ok = _response(200)
        ok.json.return_value = {'access_token': 'new-access', 'expires_in': 3600}

        with patch('onshape_integration.requests.post', return_value=ok):
            c.refresh_access_token()

        self.assertEqual(c.refresh_token, 'original-refresh')


if __name__ == '__main__':
    unittest.main()

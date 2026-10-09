"""
Tests for PenguinCAMAuth._check_authorization -- the gate deciding who may sign in.

Deny-by-default matters here: an unset or empty ALLOWED_DOMAINS must never fall open,
and the '*' wildcard must be the only way to admit arbitrary domains.
"""

import unittest
from unittest.mock import patch

from penguincam_auth import PenguinCAMAuth


def _auth_with(**env):
    """Build a PenguinCAMAuth with only its config populated (no Flask app needed)."""
    auth = PenguinCAMAuth.__new__(PenguinCAMAuth)  # bypass __init__/route registration
    with patch.dict('os.environ', env, clear=False):
        auth.config = PenguinCAMAuth._load_config(auth)
    return auth


def _check(auth, email):
    domain = email.split('@')[1] if '@' in email else None
    return auth._check_authorization(email, domain)


class TestDomainGate(unittest.TestCase):

    def test_listed_domain_allowed(self):
        auth = _auth_with(ALLOWED_DOMAINS='popcornpenguins.com', ALLOWED_EMAILS='')
        self.assertTrue(_check(auth, 'student@popcornpenguins.com'))

    def test_unlisted_domain_denied(self):
        auth = _auth_with(ALLOWED_DOMAINS='popcornpenguins.com', ALLOWED_EMAILS='')
        self.assertFalse(_check(auth, 'steven.smitka@oakland.k12.mi.us'))

    def test_multiple_domains(self):
        auth = _auth_with(ALLOWED_DOMAINS='popcornpenguins.com, oakland.k12.mi.us',
                          ALLOWED_EMAILS='')
        self.assertTrue(_check(auth, 'steven.smitka@oakland.k12.mi.us'))
        self.assertTrue(_check(auth, 'student@popcornpenguins.com'))
        self.assertFalse(_check(auth, 'someone@example.com'))

    def test_empty_allowlist_denies_everyone(self):
        """Deny-by-default: a missing config must not fall open."""
        auth = _auth_with(ALLOWED_DOMAINS='', ALLOWED_EMAILS='')
        self.assertFalse(_check(auth, 'student@popcornpenguins.com'))
        self.assertFalse(_check(auth, 'anyone@gmail.com'))

    def test_wildcard_allows_any_domain(self):
        auth = _auth_with(ALLOWED_DOMAINS='*', ALLOWED_EMAILS='')
        self.assertTrue(_check(auth, 'anyone@gmail.com'))
        self.assertTrue(_check(auth, 'steven.smitka@oakland.k12.mi.us'))
        self.assertTrue(_check(auth, 'someone@some-random-school.edu'))

    def test_wildcard_among_other_entries_still_opens(self):
        auth = _auth_with(ALLOWED_DOMAINS='popcornpenguins.com,*', ALLOWED_EMAILS='')
        self.assertTrue(_check(auth, 'anyone@gmail.com'))

    def test_wildcard_is_not_matched_as_a_literal_domain(self):
        """A user whose domain is literally '*' is not a thing, but be explicit."""
        auth = _auth_with(ALLOWED_DOMAINS='popcornpenguins.com', ALLOWED_EMAILS='')
        self.assertFalse(_check(auth, 'weird@*'))

    def test_allowed_email_overrides_domain_gate(self):
        """One-off testers are admitted by email without opening their whole domain."""
        auth = _auth_with(ALLOWED_DOMAINS='popcornpenguins.com',
                          ALLOWED_EMAILS='jsirota@gmail.com')
        self.assertTrue(_check(auth, 'jsirota@gmail.com'))
        self.assertFalse(_check(auth, 'someone-else@gmail.com'))

    def test_allowed_email_works_with_empty_domain_list(self):
        auth = _auth_with(ALLOWED_DOMAINS='', ALLOWED_EMAILS='jsirota@gmail.com')
        self.assertTrue(_check(auth, 'jsirota@gmail.com'))
        self.assertFalse(_check(auth, 'someone-else@gmail.com'))

    def test_whitespace_in_list_is_tolerated(self):
        auth = _auth_with(ALLOWED_DOMAINS='  popcornpenguins.com ,  oakland.k12.mi.us  ',
                          ALLOWED_EMAILS='')
        self.assertTrue(_check(auth, 'student@popcornpenguins.com'))
        self.assertTrue(_check(auth, 'steven.smitka@oakland.k12.mi.us'))


if __name__ == '__main__':
    unittest.main()

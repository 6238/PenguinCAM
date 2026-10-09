"""Scrubbing: what a recording loses before it reaches the public repository.

The owner's name, email address, user id and company ids are replaced by fixed stand-ins,
the same in requests and responses so a replayed request still matches its recording.
Credentials are dropped, a foreign redirect loses its (signed) query, and a /blobelements/
download, which is how the team config is read, is replaced by the fixture.
find_secrets() is the last line of defence: the scrub test runs it over every committed
recording.
"""
import base64
import dataclasses
import re
from urllib.parse import quote, urlsplit, urlunsplit

from testbed.settings import FIXTURE_DIR

STANDIN_EMAIL = 'testbed@example.invalid'
STANDIN_NAME = 'Test Bed Owner'
STANDIN_USER_ID = '000000000000000000000001'

TEAM_CONFIG_FIXTURE = FIXTURE_DIR / 'team-config-fixture.yaml'

_DROPPED_HEADERS = {'authorization', 'cookie', 'set-cookie'}
_BEARER = re.compile(r'Bearer \S+')
_JWT = re.compile(r'eyJ[\w-]+\.[\w-]+\.[\w-]+')
_SECRET_PATTERNS = (
    ('email', re.compile(r'[\w.+-]+@[\w-]+(?:\.[\w-]+)+')),
    ('jwt', _JWT),
    ('bearer', _BEARER),
    ('basic', re.compile(r'Basic [A-Za-z0-9+/=]{8,}')),
)
_ALLOWED_EMAIL_DOMAIN = '@example.invalid'


def standin_company_id(n: int) -> str:
    """The stand-in for the owner's n-th company (from 1): 0000000000000000000000c1, ..."""
    return f'c{n}'.rjust(24, '0')


class Scrubber:
    def __init__(self, replacements: dict):
        # Longest first, so a value that contains another is replaced whole.
        self._replacements = sorted(
            ((real, standin) for real, standin in replacements.items() if real),
            key=lambda pair: len(pair[0]), reverse=True)

    @classmethod
    def from_owner(cls, sessioninfo: dict, companies: list) -> 'Scrubber':
        """Map each real value from /users/sessioninfo and /companies to its stand-in."""
        pairs = {
            sessioninfo.get('email'): STANDIN_EMAIL,
            sessioninfo.get('name'): STANDIN_NAME,
            sessioninfo.get('id'): STANDIN_USER_ID,
        }
        for n, company in enumerate(companies, start=1):
            pairs[company.get('id')] = standin_company_id(n)
        # The same values as they appear inside a URL, e.g. an email with %40.
        for real, standin in list(pairs.items()):
            if real and quote(real, safe='') != real:
                pairs[quote(real, safe='')] = quote(standin, safe='')
        return cls(pairs)

    def _replace_known(self, text):
        if text is None:
            return None
        for real, standin in self._replacements:
            text = text.replace(real, standin)
        return text

    def scrub_text(self, text: str) -> str:
        """For message logs: known values replaced, bearer tokens and JWTs removed."""
        text = self._replace_known(text)
        text = _BEARER.sub('[authorization removed]', text)
        return _JWT.sub('[token removed]', text)

    def _scrub_location(self, location):
        parts = urlsplit(location)
        host = (parts.hostname or '').lower()
        if host and not host.endswith('.onshape.com'):
            parts = parts._replace(query='', fragment='')
        return self._replace_known(urlunsplit(parts))

    def scrub_exchange(self, ex):
        headers = {}
        for name, value in ex.headers.items():
            if name.lower() in _DROPPED_HEADERS:
                continue
            headers[name] = self._scrub_location(value) if name.lower() == 'location' else value
        query = {self._replace_known(key): [self._replace_known(v) for v in values]
                 for key, values in ex.query.items()}
        body_text, body_bytes, body_file = self._replace_known(ex.body_text), ex.body_bytes, ex.body_file
        if '/blobelements/' in ex.path:
            body_text, body_bytes, body_file = TEAM_CONFIG_FIXTURE.read_text(), None, None
        return dataclasses.replace(
            ex, path=self._replace_known(ex.path), query=query,
            request_body=self._replace_known(ex.request_body), headers=headers,
            body_text=body_text, body_bytes=body_bytes, body_file=body_file)


def find_secrets(text: str, extra_literals: list) -> list:
    """(line number, pattern name) for each line that looks like it holds a secret.

    Never returns the matched text, so a failing test cannot print the secret it found.
    """
    literals = []
    for literal in extra_literals:
        if literal:
            literals.append(('literal', literal))
            literals.append(('literal-base64', base64.b64encode(literal.encode()).decode()))
    found = []
    for number, line in enumerate(text.splitlines(), start=1):
        for name, pattern in _SECRET_PATTERNS:
            matches = pattern.findall(line)
            if name == 'email':
                matches = [m for m in matches if not m.endswith(_ALLOWED_EMAIL_DOMAIN)]
            if matches:
                found.append((number, name))
        for name, literal in literals:
            if literal in line:
                found.append((number, name))
    return found

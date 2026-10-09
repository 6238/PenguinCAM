"""Scrubbing: what a recording loses before it reaches the public repository.

The owner's name, email address, user id and company ids are replaced by fixed stand-ins,
the same in requests and responses so a replayed request still matches its recording.
Credentials are dropped, a foreign redirect loses its (signed) query, and a /blobelements/
download, which is how the team config is read, is replaced by the fixture.
find_secrets() is the last line of defence: the scrub test runs it over every committed
recording.
"""
import base64
import binascii
import dataclasses
import json
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
_BASIC = re.compile(r'Basic ([A-Za-z0-9+/=]{8,})')
_SECRET_PATTERNS = (
    ('email', re.compile(r'[\w.+-]+@[\w-]+(?:\.[\w-]+)+')),
    ('jwt', _JWT),
    ('bearer', _BEARER),
    ('presigned', re.compile(r'X-Amz-(?:Signature|Security-Token|Credential)'
                             r'|\bSignature=|[?&]sig=')),
)
_ALLOWED_EMAIL_DOMAIN = '@example.invalid'


def standin_company_id(n: int) -> str:
    """The stand-in for the owner's n-th company (from 1): 0000000000000000000000c1, ..."""
    return f'c{n}'.rjust(24, '0')


def standin_company_name(n: int) -> str:
    return f'Test Bed Classroom {n}'


_STANDIN_COMPANY_ID = re.compile(r'0+c\d+')
_STANDIN_COMPANY_NAME = re.compile(r'Test Bed Classroom \d+')


def host_is_onshape(host: str) -> bool:
    """True for *.onshape.com. Any other host's query is never kept, as it may be signed."""
    return host.split(':')[0].lower().endswith('.onshape.com')


def _is_basic_credential(token):
    """True when `token` decodes to `user:secret`, so "Basic Settings" is not one."""
    try:
        return ':' in base64.b64decode(token, validate=True).decode('utf-8')
    except (binascii.Error, ValueError):
        return False


def _value_forms(real, standin):
    """`real` as it appears raw, inside a URL (%40) and JSON-escaped (\\u00e9)."""
    forms = {real: standin, quote(real, safe=''): quote(standin, safe='')}
    forms[json.dumps(real)[1:-1]] = json.dumps(standin)[1:-1]
    return forms.items()


class Scrubber:
    def __init__(self, replacements: dict, whole_words=()):
        """`replacements` maps real values to stand-ins. Values in `whole_words` (names)
        are replaced only as whole words, so a short first name cannot clip other words.
        Emails are matched without regard to case."""
        rules = []
        for real, standin in replacements.items():
            if not real:
                continue
            for form, standin_form in _value_forms(real, standin):
                pattern = re.escape(form)
                if real in whole_words:
                    pattern = rf'(?<!\w){pattern}(?!\w)'
                flags = re.IGNORECASE if '@' in real else 0
                rules.append((len(form), re.compile(pattern, flags), standin_form))
        # Longest first, so a value that contains another is replaced whole.
        rules.sort(key=lambda rule: rule[0], reverse=True)
        self._rules = [(pattern, standin) for _, pattern, standin in rules]

    @classmethod
    def from_owner(cls, sessioninfo: dict, companies: list) -> 'Scrubber':
        """Map each real value from /users/sessioninfo and /companies to its stand-in.

        The owner's name, first name, last name and "first last" all become STANDIN_NAME.
        """
        pairs = {sessioninfo.get('email'): STANDIN_EMAIL, sessioninfo.get('id'): STANDIN_USER_ID}
        first, last = sessioninfo.get('firstName'), sessioninfo.get('lastName')
        names = [sessioninfo.get('name'), first, last]
        if first and last:
            names.append(f'{first} {last}')
        for name in names:
            pairs[name] = STANDIN_NAME
        for n, company in enumerate(companies, start=1):
            pairs[company.get('id')] = standin_company_id(n)
            pairs[company.get('name')] = standin_company_name(n)
            names.append(company.get('name'))
        return cls(pairs, whole_words={name for name in names if name})

    def _replace_known(self, text):
        if text is None:
            return None
        for pattern, standin in self._rules:
            text = pattern.sub(lambda _match, standin=standin: standin, text)
        return text

    def scrub_text(self, text: str) -> str:
        """For message logs: known values replaced, credentials and JWTs removed."""
        text = self._replace_known(text)
        text = _BEARER.sub('[authorization removed]', text)
        text = _BASIC.sub(lambda m: '[authorization removed]'
                          if _is_basic_credential(m.group(1)) else m.group(0), text)
        return _JWT.sub('[token removed]', text)

    def _scrub_location(self, location):
        parts = urlsplit(location)
        if parts.hostname and not host_is_onshape(parts.hostname):
            parts = parts._replace(query='', fragment='')
        return self._replace_known(urlunsplit(parts))

    def scrub_exchange(self, ex):
        headers = {}
        for name, value in ex.headers.items():
            if name.lower() in _DROPPED_HEADERS:
                continue
            headers[name] = self._scrub_location(value) if name.lower() == 'location' else value
        query = {}
        if host_is_onshape(ex.host):        # a foreign host's query may be a signature
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
        if any(_is_basic_credential(token) for token in _BASIC.findall(line)):
            found.append((number, 'basic'))
        for name, literal in literals:
            if literal in line:
                found.append((number, name))
    return found


_PERSON_KEYS = {'owner', 'createdBy', 'modifiedBy', 'sessioninfo'}
_PERSON_NAME_FIELDS = ('name', 'firstName', 'lastName')
_HEX_ID = re.compile(r'[0-9a-f]{24}')


def _is_standin_id(value):
    return value == STANDIN_USER_ID or bool(_STANDIN_COMPANY_ID.fullmatch(value))


def _is_standin_name(value):
    return value == STANDIN_NAME or bool(_STANDIN_COMPANY_NAME.fullmatch(value))


def _identity_leaks(value, parent_key, leaks):
    """Append (kind, key, value) for each real user id or person's name inside `value`."""
    if isinstance(value, str):
        if value[:1] in '{[':                  # a body recorded as text: look inside it
            try:
                _identity_leaks(json.loads(value), parent_key, leaks)
            except ValueError:
                pass
    elif isinstance(value, list):
        for item in value:
            _identity_leaks(item, parent_key, leaks)
    elif isinstance(value, dict):
        person = parent_key in _PERSON_KEYS
        is_sessioninfo = str(value.get('path', '')).endswith('/users/sessioninfo')
        for key, item in value.items():
            if isinstance(item, str):
                if ((key == 'userId' or (person and key == 'id'))
                        and _HEX_ID.fullmatch(item) and not _is_standin_id(item)):
                    leaks.append(('user-id', key, item))
                elif person and key in _PERSON_NAME_FIELDS and item and not _is_standin_name(item):
                    leaks.append(('owner-name', key, item))
            child_key = 'sessioninfo' if is_sessioninfo and key == 'body_text' else key
            _identity_leaks(item, child_key, leaks)


def find_identity_leaks(text: str) -> list:
    """(line number, kind) for each real user id or name in a JSON or JSON-lines recording.

    Kinds: 'user-id' (24-hex userId, or id of an owner, createdBy, modifiedBy or
    sessioninfo object, that is not a stand-in) and 'owner-name' (a name, firstName or
    lastName in one of those objects that is not a stand-in). Text that is not JSON is
    skipped. Like find_secrets, never returns the value.
    """
    lines = text.splitlines()
    try:
        documents = [(None, json.loads(text))]
    except ValueError:
        documents = []
        for number, line in enumerate(lines, start=1):
            try:
                documents.append((number, json.loads(line)))
            except ValueError:
                pass
    found = set()
    for number, document in documents:
        leaks = []
        _identity_leaks(document, None, leaks)
        for kind, key, value in leaks:
            line_number = number
            if line_number is None:            # the first line holding both key and value
                escaped = json.dumps(value)[1:-1]
                line_number = next((n for n, line in enumerate(lines, start=1)
                                    if key in line and (value in line or escaped in line)), 1)
            found.add((line_number, kind))
    return sorted(found)

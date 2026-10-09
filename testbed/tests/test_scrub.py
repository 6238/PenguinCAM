"""Tests for testbed/scrub.py: what a recording loses before it reaches disk."""
import base64
import os
import subprocess
import unittest
from pathlib import Path

from testbed.cassette import Exchange
from testbed.scrub import (STANDIN_EMAIL, STANDIN_NAME, STANDIN_USER_ID, Scrubber,
                           find_secrets, standin_company_id)
from testbed.settings import FIXTURE_DIR

REPO = Path(__file__).resolve().parents[2]
REAL_EMAIL = 'real.owner@school.example.org'
REAL_NAME = 'Real Owner'
REAL_USER_ID = '5f0e1d2c3b4a59687766aabb'
REAL_COMPANY_IDS = ['6a1b2c3d4e5f60718293a4b5', '6a1b2c3d4e5f60718293a4b6']


def _scrubber():
    sessioninfo = {'id': REAL_USER_ID, 'email': REAL_EMAIL, 'name': REAL_NAME}
    companies = [{'id': cid, 'name': 'Some Classroom'} for cid in REAL_COMPANY_IDS]
    return Scrubber.from_owner(sessioninfo, companies)


def _exchange(**fields):
    base = dict(method='GET', host='cad.onshape.com', path='/api/v13/users/sessioninfo',
                query={}, request_body=None, status=200, headers={}, body_text=None,
                body_file=None)
    base.update(fields)
    return Exchange(**base)


class ScrubExchangeTest(unittest.TestCase):
    def test_authorization_removed_both_forms(self):
        for value in ('Bearer eyJhbGciOi.eyJzdWIi.c2lnbmF0dXJl', 'Basic YWNjZXNzOnNlY3JldA=='):
            with self.subTest(form=value.split()[0]):
                ex = _exchange(headers={'Authorization': value, 'Cookie': 'a=b',
                                        'Set-Cookie': 'c=d', 'Content-Type': 'application/json'})
                scrubbed = _scrubber().scrub_exchange(ex)
                self.assertEqual(scrubbed.headers, {'Content-Type': 'application/json'})
                self.assertNotIn(value.split()[1], repr(scrubbed))

    def test_known_values_replaced_in_any_field(self):
        body = ('{"owner": {"id": "%s", "email": "%s", "name": "%s",'
                ' "href": "https://cad.onshape.com/api/v13/users/%s"},'
                ' "company": {"id": "%s"}}'
                % (REAL_USER_ID, REAL_EMAIL, REAL_NAME, REAL_USER_ID, REAL_COMPANY_IDS[1]))
        ex = _exchange(method='POST', path=f'/api/v13/companies/{REAL_COMPANY_IDS[0]}/users',
                       query={'uid': [REAL_USER_ID]},
                       request_body=f'{{"email": "{REAL_EMAIL}"}}', body_text=body,
                       headers={'Location': f'https://cad.onshape.com/users/{REAL_USER_ID}'})
        scrubbed = _scrubber().scrub_exchange(ex)
        everything = repr(scrubbed)
        for real in (REAL_EMAIL, REAL_NAME, REAL_USER_ID, *REAL_COMPANY_IDS):
            self.assertNotIn(real, everything)
        self.assertIn(STANDIN_EMAIL, scrubbed.request_body)
        self.assertIn(f'/users/{STANDIN_USER_ID}"', scrubbed.body_text)
        self.assertIn(STANDIN_NAME, scrubbed.body_text)
        self.assertEqual(scrubbed.query, {'uid': [STANDIN_USER_ID]})
        self.assertEqual(scrubbed.path, f'/api/v13/companies/{standin_company_id(1)}/users')
        self.assertIn(standin_company_id(2), scrubbed.body_text)
        self.assertEqual(scrubbed.headers['Location'],
                         f'https://cad.onshape.com/users/{STANDIN_USER_ID}')

    def test_same_standin_both_directions(self):
        scrubber = _scrubber()
        request = _exchange(path=f'/api/v13/companies/{REAL_COMPANY_IDS[0]}')
        response = _exchange(body_text=f'{{"items": [{{"id": "{REAL_COMPANY_IDS[0]}"}}]}}')
        self.assertIn(standin_company_id(1), scrubber.scrub_exchange(request).path)
        self.assertIn(standin_company_id(1), scrubber.scrub_exchange(response).body_text)
        self.assertEqual(standin_company_id(1), '0000000000000000000000c1')
        self.assertEqual(standin_company_id(2), '0000000000000000000000c2')

    def test_blobelements_body_replaced_by_fixture(self):
        ex = _exchange(path='/api/v13/blobelements/d/abc/w/def/e/ghi',
                       body_text='team:\n  number: 6238\nprinting:\n  pairing_code: SECRET123\n')
        scrubbed = _scrubber().scrub_exchange(ex)
        self.assertNotIn('SECRET123', repr(scrubbed))
        fixture = (FIXTURE_DIR / 'team-config-fixture.yaml').read_text()
        self.assertEqual(scrubbed.body_text, fixture)

    def test_foreign_redirect_query_dropped(self):
        foreign = _exchange(status=307, headers={
            'Location': 'https://s3.amazonaws.com/bucket/part.stl?X-Amz-Signature=abc&X-Amz-Credential=k'})
        local = _exchange(status=307, headers={
            'Location': 'https://cad.onshape.com/api/v13/x?a=1'})
        self.assertEqual(_scrubber().scrub_exchange(foreign).headers['Location'],
                         'https://s3.amazonaws.com/bucket/part.stl')
        self.assertEqual(_scrubber().scrub_exchange(local).headers['Location'],
                         'https://cad.onshape.com/api/v13/x?a=1')

    def test_scrub_text_replaces_known_values(self):
        text = f'{{"user": "{REAL_EMAIL}", "auth": "Bearer abc.def.ghi"}}'
        scrubbed = _scrubber().scrub_text(text)
        self.assertNotIn(REAL_EMAIL, scrubbed)
        self.assertNotIn('abc.def.ghi', scrubbed)


class FindSecretsTest(unittest.TestCase):
    def test_find_secrets_reports_line_and_kind(self):
        literal = 'myaccesskey123'
        text = '\n'.join([
            'clean line',
            f'mail {REAL_EMAIL}',
            f'fine {STANDIN_EMAIL}',
            'token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl',
            'header Bearer abcdef',
            'header Basic YWJjZGVmZ2hpams=',
            f'key {literal}',
            'b64 ' + base64.b64encode(literal.encode()).decode(),
        ])
        found = find_secrets(text, [literal])
        kinds = {line: kind for line, kind in found}
        self.assertNotIn(1, kinds)
        self.assertEqual(kinds[2], 'email')
        self.assertNotIn(3, kinds)
        self.assertEqual(kinds[4], 'jwt')
        self.assertEqual(kinds[5], 'bearer')
        self.assertEqual(kinds[6], 'basic')
        self.assertEqual(kinds[7], 'literal')
        self.assertEqual(kinds[8], 'literal-base64')


class FixtureTest(unittest.TestCase):
    def test_fixture_is_valid_team_config(self):
        from config_validation import validate_and_sanitize_config
        from team_config import TeamConfig
        text = (FIXTURE_DIR / 'team-config-fixture.yaml').read_text()
        data, warnings = validate_and_sanitize_config(text, strict=True)
        self.assertEqual(warnings, [])
        config = TeamConfig.from_dict(data)
        self.assertEqual(config.team_number, 0)
        self.assertEqual(config.team_name, 'Test Bed Team')
        self.assertIsNone(config.pairing_code)

    def test_fixture_is_tracked(self):
        listed = subprocess.run(
            ['git', 'ls-files', 'testbed/fixtures/team-config-fixture.yaml'],
            cwd=REPO, capture_output=True, text=True, check=True).stdout
        self.assertTrue(listed.strip(), 'the fixture must be committed')


RECORDING_DIRS = ('testbed/cassettes', 'testbed/messages', 'testbed/tests/fixtures')
SECRET_VARIABLES = ('ONSHAPE_ACCESS_KEY', 'ONSHAPE_SECRET_KEY', 'ONSHAPE_PASSWORD')


class CommittedRecordingsTest(unittest.TestCase):
    def test_committed_recordings_are_clean(self):
        extras = [os.environ[name] for name in SECRET_VARIABLES if os.environ.get(name)]
        access, secret = os.environ.get('ONSHAPE_ACCESS_KEY'), os.environ.get('ONSHAPE_SECRET_KEY')
        if access and secret:
            extras.append(f'{access}:{secret}')     # its base64 form is the Basic header
        problems = []
        for directory in RECORDING_DIRS:
            for path in sorted((REPO / directory).rglob('*')):
                if not path.is_file() or path.suffix == '.bin' or path.name == '.gitkeep':
                    continue
                text = path.read_text(encoding='utf-8', errors='replace')
                for line, kind in find_secrets(text, extras):
                    problems.append(f'{path.relative_to(REPO)}:{line} {kind}')
        self.assertEqual(problems, [], 'secret-like strings in committed recordings:\n'
                         + '\n'.join(problems))


if __name__ == '__main__':
    unittest.main()

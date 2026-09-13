"""Flask test-client coverage for the printer relay blueprint.

The relay is swapped for a fresh PrinterRelay on a fake clock in every test, so nothing
leaks between tests and the job deadlines are reachable without sleeping."""
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import printer_relay
import printer_routes
import frc_cam_gui_app as gui
from frc_cam_gui_app import app

CODE = '7K3MQ9XW'
TEAM_YAML_DATA = {'team': {'number': 6238, 'name': 'Popcorn Penguins'},
                  'printing': {'pairing_code': CODE}}

IDLE = {'state': 'idle', 'file': '', 'percent': None, 'remaining_min': None,
        'layer': 0, 'layers': 0, 'error': None,
        'printer': {'model': 'H2S', 'name': 'Shop H2S', 'serial': '01P00A'}}


class FakeClock:
    def __init__(self, now=1_789_300_000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class PrinterRouteTestCase(unittest.TestCase):
    """Shared fixture. The rate limiter is off by default -- the limits get their own
    tests, and leaving them on would make every other test order-dependent."""

    def setUp(self):
        app.config['TESTING'] = True
        app.secret_key = 'test-secret-key'
        self.clock = FakeClock()
        self.relay = printer_relay.PrinterRelay(clock=self.clock)
        self._relay_patch = mock.patch.object(printer_relay, 'RELAY', self.relay)
        self._relay_patch.start()
        self.addCleanup(self._relay_patch.stop)
        self._limiter_was = gui.limiter.enabled
        gui.limiter.enabled = False
        self.addCleanup(setattr, gui.limiter, 'enabled', self._limiter_was)
        self._debug_was = app.debug
        self.addCleanup(setattr, app, 'debug', self._debug_was)
        self.client = app.test_client()

    def onshape_session(self, config_data=TEAM_YAML_DATA, team_number=6238):
        """Give the test client an Onshape session carrying a team config."""
        with self.client.session_transaction() as sess:
            sess['team_config_data'] = config_data
            sess['team_number'] = team_number
            sess['user_email'] = 'student@example.com'
        return mock.patch.object(printer_routes, '_HAS_ONSHAPE_SESSION',
                                 lambda: True, create=True)


class TestStatusRoute(PrinterRouteTestCase):
    def test_requires_an_onshape_session(self):
        resp = self.client.get('/printer/status')
        self.assertEqual(resp.status_code, 401)
        self.assertTrue(resp.get_json()['need_verification'])

    def test_an_uploaded_config_is_never_read(self):
        """The anonymous upload flow must not reach any team's printer, even in a browser
        that still carries an Onshape cookie."""
        with self.client.session_transaction() as sess:
            sess['app_verified'] = True
            sess['upload_config_data'] = TEAM_YAML_DATA
        resp = self.client.get('/printer/status')
        self.assertEqual(resp.status_code, 401)

    def test_unpaired_when_the_yaml_has_no_code(self):
        with self.onshape_session(config_data={'team': {'number': 6238}}):
            resp = self.client.get('/printer/status')
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.get_json()['paired'])

    def test_waiting_when_no_daemon_has_reported(self):
        with self.onshape_session():
            body = self.client.get('/printer/status').get_json()
        self.assertTrue(body['paired'])
        self.assertTrue(body['waiting'])
        self.assertEqual(body['code'], '7K3M-Q9XW')

    def test_online_running_snapshot_is_served(self):
        self.relay.note_code(CODE)
        self.relay.note_daemon(CODE)
        snap = dict(IDLE, state='running', file='sample_part-j8f3a2c1.gcode.3mf',
                    percent=42, remaining_min=31)
        self.relay.store_status(CODE, snap)
        with self.onshape_session():
            body = self.client.get('/printer/status').get_json()
        self.assertTrue(body['online'])
        self.assertEqual(body['status']['state'], 'running')
        self.assertEqual(body['age_seconds'], 0)

    def test_the_display_name_reaches_the_wizard(self):
        """The wizard shows the file without the job-id suffix the daemon baked in."""
        self.relay.note_code(CODE)
        self.relay.note_daemon(CODE)
        self.relay.store_status(CODE, dict(IDLE, state='running',
                                           file='sample_part-j8f3a2c1.gcode.3mf'))
        with self.onshape_session():
            body = self.client.get('/printer/status').get_json()
        self.assertEqual(body['status']['file'], 'sample_part-j8f3a2c1.gcode.3mf')
        self.assertEqual(body['status']['display_name'], 'sample_part.gcode.3mf')

    def test_offline_after_two_minutes_of_silence(self):
        self.relay.note_code(CODE)
        self.relay.note_daemon(CODE)
        self.relay.store_status(CODE, IDLE)
        self.clock.advance(420)
        with self.onshape_session():
            body = self.client.get('/printer/status').get_json()
        self.assertFalse(body['online'])
        self.assertEqual(body['age_seconds'], 420)

    def test_a_corrupt_session_config_reads_as_unpaired(self):
        """A stale cookie can hold anything -- TeamConfig raises on a list. The status line
        must say "not paired", never 500 at a browser whose only fault is an old cookie."""
        with self.onshape_session(config_data=['not', 'a', 'mapping']):
            resp = self.client.get('/printer/status')
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.get_json()['paired'])

    def test_the_route_refreshes_the_session_config_first(self):
        """A YAML edit must reach the relay within the TTL even with no page render."""
        refresh = mock.Mock()
        with self.onshape_session(), \
             mock.patch.object(printer_routes, '_REFRESH_TEAM_CONFIG', refresh, create=True):
            self.client.get('/printer/status')
        refresh.assert_called_once_with()

    def test_a_failing_refresh_does_not_break_the_route(self):
        boom = mock.Mock(side_effect=RuntimeError('onshape down'))
        with self.onshape_session(), \
             mock.patch.object(printer_routes, '_REFRESH_TEAM_CONFIG', boom, create=True):
            resp = self.client.get('/printer/status')
        self.assertEqual(resp.status_code, 200)


class FakeTokenManager:
    """Stands in for FileTokenManager: token -> {'filepath','filename','created'}."""

    def __init__(self):
        self.files = {}

    def register(self, token, filename='sample_part.gcode.3mf', created=None):
        self.files[token] = {'filepath': f'/tmp/{filename}', 'filename': filename,
                             'created': time.time() if created is None else created}
        return token

    def get_file(self, token):
        return self.files.get(token)


class TestJobsRoute(PrinterRouteTestCase):
    def setUp(self):
        super().setUp()
        self.tokens = FakeTokenManager()
        patch = mock.patch.object(printer_routes, '_TOKEN_MANAGER', self.tokens,
                                  create=True)
        patch.start()
        self.addCleanup(patch.stop)
        self.relay.note_code(CODE)
        self.relay.note_daemon(CODE)
        self.relay.store_status(CODE, IDLE)

    def test_requires_an_onshape_session(self):
        self.assertEqual(self.client.post('/printer/jobs', json={'token': 'x'}).status_code,
                         401)

    def test_refused_when_the_yaml_has_no_code(self):
        with self.onshape_session(config_data={'team': {'number': 6238}}):
            resp = self.client.post('/printer/jobs', json={'token': 'x'})
        self.assertEqual(resp.status_code, 409)

    def test_a_corrupt_session_config_is_refused_like_an_unpaired_one(self):
        with self.onshape_session(config_data=['not', 'a', 'mapping']):
            resp = self.client.post('/printer/jobs', json={'token': 'tok'})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.get_json()['error'], 'No printer paired')

    def test_unknown_token_is_not_found(self):
        with self.onshape_session():
            resp = self.client.post('/printer/jobs', json={'token': 'nope'})
        self.assertEqual(resp.status_code, 404)

    def test_a_slice_older_than_forty_minutes_must_be_sliced_again(self):
        self.tokens.register(
            'tok', created=time.time() - printer_routes.SLICE_MAX_AGE_SECONDS - 60)
        with self.onshape_session():
            resp = self.client.post('/printer/jobs', json={'token': 'tok'})
        self.assertEqual(resp.status_code, 409)
        self.assertIn('slice again', resp.get_json()['error'])

    def test_success_stores_a_pending_job_and_answers_202(self):
        self.tokens.register('tok')
        with self.onshape_session():
            resp = self.client.post('/printer/jobs', json={'token': 'tok'})
        self.assertEqual(resp.status_code, 202)
        job_id = resp.get_json()['job_id']
        stored = self.relay.status_for(CODE)['job']
        self.assertEqual(stored['id'], job_id)
        self.assertEqual(stored['state'], 'pending')

    def test_the_job_carries_a_path_that_the_real_download_route_resolves(self):
        """The daemon joins this path with its own backend URL, so the relay never needs to
        know the name it is reached by."""
        self.tokens.register('tok')
        with self.onshape_session():
            self.client.post('/printer/jobs', json={'token': 'tok'})
        job = self.relay.store_status(CODE, IDLE)
        self.assertEqual(job['download_path'], '/download/tok')
        self.assertIn('/download/<token>',
                      [str(rule) for rule in app.url_map.iter_rules()])

    def test_a_send_logs_a_metrics_event_with_the_team_number(self):
        self.tokens.register('tok')
        fake_metrics = mock.Mock()
        with self.onshape_session(), \
             mock.patch.object(printer_routes, '_METRICS', fake_metrics, create=True):
            self.client.post('/printer/jobs', json={'token': 'tok'})
        kind, kwargs = fake_metrics.log_event.call_args[0], fake_metrics.log_event.call_args[1]
        self.assertEqual(kind[0], 'printer_send')
        self.assertEqual(kwargs['team_number'], 6238)

    def test_a_second_send_while_one_is_in_flight_is_refused(self):
        self.tokens.register('tok')
        with self.onshape_session():
            self.assertEqual(
                self.client.post('/printer/jobs', json={'token': 'tok'}).status_code, 202)
            second = self.client.post('/printer/jobs', json={'token': 'tok'})
        self.assertEqual(second.status_code, 409)
        self.assertIn('already on its way', second.get_json()['error'])


class TestDaemonRoutes(PrinterRouteTestCase):
    def setUp(self):
        super().setUp()
        app.debug = True            # development mode, so plain HTTP is allowed
        self.relay.note_code(CODE)

    def auth(self, code=CODE):
        token = printer_relay.issue_token(code, app.secret_key)
        return {'Authorization': f'Bearer {token}'}

    def test_exchange_answers_a_token_then_refuses_the_second_pi(self):
        first = self.client.post('/printer/daemon/pair', json={'code': '7k3m-q9xw'})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(printer_relay.read_token(first.get_json()['token'],
                                                  app.secret_key)[0], CODE)
        second = self.client.post('/printer/daemon/pair', json={'code': CODE})
        self.assertEqual(second.status_code, 404)

    def test_exchange_for_an_unpresented_code_answers_the_same_404(self):
        resp = self.client.post('/printer/daemon/pair', json={'code': 'ABCDEFGH'})
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json(), {'error': 'not paired'})

    def test_sync_with_a_bad_token_says_repair(self):
        resp = self.client.post('/printer/daemon/sync', json=IDLE,
                                headers={'Authorization': 'Bearer garbage'})
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.get_json(), {'error': 'repair'})

    def test_a_token_signed_with_another_key_is_rejected(self):
        token = printer_relay.issue_token(CODE, 'some-other-key')
        resp = self.client.post('/printer/daemon/sync', json=IDLE,
                                headers={'Authorization': f'Bearer {token}'})
        self.assertEqual(resp.status_code, 401)

    def test_an_unauthenticated_sync_never_touches_the_relay(self):
        """The token is checked BEFORE note_daemon, so a guessed code cannot be marked
        alive -- which would otherwise block the real Pi's next exchange."""
        self.client.post('/printer/daemon/sync', json=IDLE,
                         headers={'Authorization': 'Bearer garbage'})
        self.assertTrue(self.relay.status_for(CODE)['waiting'])
        self.assertTrue(self.relay.begin_exchange(CODE))

    def test_sync_stores_the_snapshot_and_hands_out_a_job(self):
        self.relay.note_daemon(CODE)
        self.relay.store_status(CODE, IDLE)
        self.relay.claim_job(CODE, job_id='j8f3a2c1', download_path='/download/tok',
                             filename='sample_part.gcode.3mf', team_number=6238)
        body = self.client.post('/printer/daemon/sync', json=IDLE,
                                headers=self.auth()).get_json()
        self.assertEqual(body['job']['id'], 'j8f3a2c1')
        self.assertEqual(self.relay.status_for(CODE)['job']['state'], 'handed_out')
        self.assertEqual(self.relay.status_for(CODE)['status']['state'], 'idle')

    def test_the_job_on_the_wire_carries_only_the_four_documented_fields(self):
        """Section 7's wire shape. The relay's state -- team_number, the job state, the
        hand-out time -- is the relay's bookkeeping and is never sent to the Pi."""
        self.relay.note_daemon(CODE)
        self.relay.store_status(CODE, IDLE)
        self.relay.claim_job(CODE, job_id='j8f3a2c1', download_path='/download/tok',
                             filename='sample_part.gcode.3mf', team_number=6238)
        job = self.client.post('/printer/daemon/sync', json=IDLE,
                               headers=self.auth()).get_json()['job']
        self.assertEqual(set(job), {'id', 'download_path', 'filename', 'created_at'})
        self.assertEqual(job['download_path'], '/download/tok')
        self.assertEqual(job['filename'], 'sample_part.gcode.3mf')

    def test_sync_answers_a_null_job_when_there_is_none(self):
        body = self.client.post('/printer/daemon/sync', json=IDLE,
                                headers=self.auth()).get_json()
        self.assertIsNone(body['job'])

    def test_ack_records_the_outcome_and_logs_metrics(self):
        self.relay.note_daemon(CODE)
        self.relay.store_status(CODE, IDLE)
        self.relay.claim_job(CODE, job_id='j8f3a2c1', download_path='/download/tok',
                             filename='sample_part.gcode.3mf', team_number=6238)
        self.relay.store_status(CODE, IDLE)
        fake_metrics = mock.Mock()
        with mock.patch.object(printer_routes, '_METRICS', fake_metrics, create=True):
            resp = self.client.post('/printer/daemon/jobs/j8f3a2c1/ack',
                                    json={'ok': True}, headers=self.auth())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(fake_metrics.log_event.call_args[0][0], 'printer_ack')
        self.assertEqual(fake_metrics.log_event.call_args[1]['team_number'], 6238)
        self.assertTrue(self.relay.status_for(CODE)['last_result']['ok'])

    def test_ack_for_an_unknown_job_is_404(self):
        resp = self.client.post('/printer/daemon/jobs/jffffffff/ack',
                                json={'ok': True}, headers=self.auth())
        self.assertEqual(resp.status_code, 404)

    def test_a_job_that_died_on_a_deadline_still_logs_a_failed_ack_event(self):
        """Lazy expiry: the sync that reads the record is what kills the overdue job, so
        the route drains the events AFTER that read, not before."""
        self.relay.note_daemon(CODE)
        self.relay.store_status(CODE, IDLE)
        self.relay.claim_job(CODE, job_id='j8f3a2c1', download_path='/download/tok',
                             filename='sample_part.gcode.3mf', team_number=6238)
        self.clock.advance(4 * 60)      # past the three-minute pick-up deadline
        fake_metrics = mock.Mock()
        with mock.patch.object(printer_routes, '_METRICS', fake_metrics, create=True):
            body = self.client.post('/printer/daemon/sync', json=IDLE,
                                    headers=self.auth()).get_json()
        self.assertIsNone(body['job'])
        args, kwargs = fake_metrics.log_event.call_args
        self.assertEqual(args[0], 'printer_ack')
        self.assertEqual(kwargs['team_number'], 6238)
        self.assertFalse(kwargs['metadata']['ok'])

    def test_ping_reports_flags_and_stores_nothing(self):
        resp = self.client.get('/printer/daemon/ping', headers=self.auth())
        self.assertEqual(resp.get_json(), {'ok': True, 'waiting': True, 'online': False})
        # Storing nothing means a real daemon can still exchange right afterwards.
        self.assertTrue(self.relay.begin_exchange(CODE))


class TestHttpsAndDevMode(PrinterRouteTestCase):
    def setUp(self):
        super().setUp()
        self.relay.note_code(CODE)

    def test_daemon_routes_refuse_plain_http_outside_development_mode(self):
        app.debug = False
        for path, method in (('/printer/daemon/pair', 'post'),
                             ('/printer/daemon/sync', 'post'),
                             ('/printer/daemon/jobs/j8f3a2c1/ack', 'post'),
                             ('/printer/daemon/ping', 'get'),
                             ('/install-printer.sh', 'get')):
            resp = getattr(self.client, method)(path, json={})
            self.assertEqual(resp.status_code, 403, path)

    def test_https_is_accepted_outside_development_mode(self):
        app.debug = False
        resp = self.client.post('/printer/daemon/pair', json={'code': CODE},
                                base_url='https://localhost')
        self.assertEqual(resp.status_code, 200)

    def test_development_mode_allows_plain_http(self):
        app.debug = True
        resp = self.client.post('/printer/daemon/pair', json={'code': CODE})
        self.assertEqual(resp.status_code, 200)

    def test_railway_environment_cancels_development_mode(self):
        app.debug = True
        with mock.patch.dict(os.environ, {'RAILWAY_ENVIRONMENT': 'production'}):
            resp = self.client.post('/printer/daemon/pair', json={'code': CODE})
        self.assertEqual(resp.status_code, 403)

    def test_dev_seed_code_works_in_development_mode(self):
        fresh = printer_relay.PrinterRelay(clock=self.clock)
        app.debug = True
        with mock.patch.object(printer_relay, 'RELAY', fresh), \
             mock.patch.dict(os.environ, {'PRINTER_DEV_PAIRING_CODE': 'ABCDEFGH'}):
            resp = self.client.post('/printer/daemon/pair', json={'code': 'ABCDEFGH'})
        self.assertEqual(resp.status_code, 200)

    def test_dev_seed_code_is_ignored_outside_development_mode(self):
        fresh = printer_relay.PrinterRelay(clock=self.clock)
        app.debug = False
        with mock.patch.object(printer_relay, 'RELAY', fresh), \
             mock.patch.dict(os.environ, {'PRINTER_DEV_PAIRING_CODE': 'ABCDEFGH'}):
            resp = self.client.post('/printer/daemon/pair', json={'code': 'ABCDEFGH'},
                                    base_url='https://localhost')
        self.assertEqual(resp.status_code, 404)

    def test_the_installer_route_serves_the_script(self):
        app.debug = True
        resp = self.client.get('/install-printer.sh')
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_data(as_text=True).startswith('#!/usr/bin/env bash'))

    def test_the_installer_refusal_is_plain_text_a_shell_can_swallow(self):
        """This one is fetched by `curl ... | bash`, so its refusal must not be JSON: a
        comment line is inert in a shell and readable in a terminal."""
        app.debug = False
        resp = self.client.get('/install-printer.sh')
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.mimetype, 'text/plain')
        self.assertEqual(resp.get_data(as_text=True).strip(), '# HTTPS required')
        # The JSON routes are unchanged.
        self.assertEqual(self.client.post('/printer/daemon/pair', json={'code': CODE})
                         .get_json(), {'error': 'HTTPS required'})


class TestRateLimitKeys(PrinterRouteTestCase):
    def test_session_key_is_the_code_not_the_address(self):
        # Exercise the key function the way flask-limiter does: inside a request context
        # whose session carries the Onshape copy of the team config.
        with app.test_request_context('/printer/status'):
            from flask import session
            session['team_config_data'] = TEAM_YAML_DATA
            self.assertEqual(printer_routes._session_code_key(), f'printer-code:{CODE}')

    def test_session_key_falls_back_when_the_session_is_corrupt(self):
        """flask-limiter runs the key function before the view, so it must never raise."""
        with app.test_request_context('/printer/status'):
            from flask import session
            session['team_config_data'] = ['not', 'a', 'mapping']
            key = printer_routes._session_code_key()
        self.assertNotIn('printer-code', key)

    def test_token_key_is_the_code_from_the_bearer_token(self):
        token = printer_relay.issue_token(CODE, app.secret_key)
        with app.test_request_context('/printer/daemon/sync',
                                      headers={'Authorization': f'Bearer {token}'}):
            self.assertEqual(printer_routes._token_code_key(), f'printer-code:{CODE}')

    def test_token_key_falls_back_on_a_garbage_header(self):
        with app.test_request_context('/printer/daemon/sync',
                                      headers={'Authorization': 'Bearer !!!'}):
            key = printer_routes._token_code_key()
        self.assertNotIn('printer-code', key)

    def test_sync_is_not_subject_to_the_app_wide_two_hundred_per_hour_default(self):
        """A five-second sync is 720 requests an hour. Without override_defaults the daemon
        would start taking 429s about seventeen minutes after boot."""
        app.debug = True
        gui.limiter.enabled = True
        self.addCleanup(setattr, gui.limiter, 'enabled', False)
        gui.limiter.reset()
        self.relay.note_code(CODE)
        headers = {'Authorization':
                   f'Bearer {printer_relay.issue_token(CODE, app.secret_key)}'}
        codes = set()
        for _ in range(60):
            codes.add(self.client.post('/printer/daemon/sync', json=IDLE,
                                       headers=headers).status_code)
        self.assertEqual(codes, {200})
        # The 61st in the same minute hits this route's OWN 60/minute limit, proving the
        # route's limit is in force and the 200/hour default is not.
        self.assertEqual(self.client.post('/printer/daemon/sync', json=IDLE,
                                          headers=headers).status_code, 429)

    def test_the_exchange_limit_is_shared_by_every_address(self):
        """Keying the exchange on the address would let a guesser spread the attempts over a
        botnet, so its budget is service-wide: a second address finds it already spent."""
        app.debug = True
        gui.limiter.enabled = True
        self.addCleanup(setattr, gui.limiter, 'enabled', False)
        gui.limiter.reset()
        codes = set()
        for _ in range(60):
            codes.add(self.client.post('/printer/daemon/pair', json={'code': 'ABCDEFGH'},
                                       headers={'X-Forwarded-For': '203.0.113.7'},
                                       environ_overrides={'REMOTE_ADDR': '10.0.0.1'})
                      .status_code)
        self.assertEqual(codes, {404})      # an unpresented code, refused sixty times
        other = self.client.post('/printer/daemon/pair', json={'code': 'ABCDEFGH'},
                                 headers={'X-Forwarded-For': '198.51.100.4'},
                                 environ_overrides={'REMOTE_ADDR': '10.0.0.1'})
        self.assertEqual(other.status_code, 429)


class TestVerificationEightLimiterAndProxy(PrinterRouteTestCase):
    """Spec verification task 8, the parts that need no production traffic.

    What is pinned here: flask-limiter evaluates a route's key_func BEFORE the view, so a
    key_func that raised would be a 500 the view never sees -- hence the try/except in
    every one of ours; override_defaults strips the app-wide 200-per-hour limit from every
    printer route; and ProxyFix decides, for a request that arrives through Cloudflare and
    Railway, both whether the HTTPS gate opens and what address the limiter keys on.

    Still open (needs a live probe, see docs/DEPLOYMENT_GUIDE.md): what address the app
    actually sees in production for a browser and for a Pi."""

    PRINTER_ENDPOINTS = ('printer.printer_status', 'printer.printer_jobs',
                         'printer.printer_daemon_pair', 'printer.printer_daemon_sync',
                         'printer.printer_daemon_ack', 'printer.printer_daemon_ping',
                         'printer.install_printer_sh')

    def setUp(self):
        super().setUp()
        self.relay.note_code(CODE)
        app.debug = False           # production shape: the HTTPS gate is live
        self.headers = {'Authorization':
                        f'Bearer {printer_relay.issue_token(CODE, app.secret_key)}'}

    def enable_limiter(self):
        """Turn the limiter on for one test. The base fixture's cleanup puts it back."""
        gui.limiter.enabled = True
        gui.limiter.reset()

    def capture_remote_addr(self):
        """Record request.remote_addr as the shipped /printer/status route sees it. The
        session gate is the first thing that route calls, so it is the earliest hook into a
        real request; it answers False, and the 401 that follows is beside the point."""
        seen = []

        def gate():
            from flask import request
            seen.append(request.remote_addr)
            return False

        patch = mock.patch.object(printer_routes, '_HAS_ONSHAPE_SESSION', gate,
                                  create=True)
        patch.start()
        self.addCleanup(patch.stop)
        return seen

    # --- the key_func runs before the view --------------------------------------------

    def test_the_shipped_key_func_runs_before_the_view(self):
        """Ordering, observed through the shipped stack: the route's key_func reads the
        bearer token once before the view runs and reads it again itself.

        The key_func the limiter holds was captured when the route was decorated, so this
        watches printer_relay.read_token -- which both callers reach through the module --
        rather than patching printer_routes._token_code_key, which the limiter would
        never see."""
        events = []
        real_read_token = printer_relay.read_token
        real_store_status = self.relay.store_status

        def spy_read_token(token, secret_key):
            events.append('token read')
            return real_read_token(token, secret_key)

        def spy_store_status(*args, **kwargs):
            events.append('view')
            return real_store_status(*args, **kwargs)

        self.enable_limiter()
        with mock.patch.object(printer_relay, 'read_token', spy_read_token), \
             mock.patch.object(self.relay, 'store_status', spy_store_status):
            resp = self.client.post('/printer/daemon/sync', json=IDLE,
                                    headers=self.headers, base_url='https://localhost')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(events, ['token read', 'token read', 'view'])

    def test_a_corrupt_session_does_not_break_the_limited_status_route(self):
        """_session_code_key runs before the view on a session it cannot parse. Its
        try/except is what keeps that a 401 from the session gate instead of a 500."""
        self.enable_limiter()
        with self.client.session_transaction() as sess:
            sess['team_config_data'] = ['not', 'a', 'mapping']   # TeamConfig raises on it
        resp = self.client.get('/printer/status', base_url='https://localhost')
        self.assertEqual(resp.status_code, 401)

    def test_a_malformed_bearer_token_does_not_break_the_limited_sync_route(self):
        """The same for _token_code_key: a garbage credential is the daemon's 'repair'
        answer from the view, never a 500 from the limiter."""
        self.enable_limiter()
        resp = self.client.post('/printer/daemon/sync', json=IDLE,
                                headers={'Authorization': 'Bearer !!! not a token'},
                                base_url='https://localhost')
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.get_json()['error'], 'repair')

    # --- override_defaults ------------------------------------------------------------

    def test_no_printer_route_carries_the_app_wide_default_limit(self):
        """Read out of the limiter's own route map, which is what it consults per request:
        resolve_limits answers (default limits, decorated limits) for an endpoint. Every
        printer route must have an EMPTY default group -- a five-second sync is 720 an hour
        and the app-wide default is 200 -- while an ordinary route still carries it."""
        from flask_limiter.util import get_qualified_name
        manager = gui.limiter.limit_manager

        def limits_for(endpoint):
            view = app.view_functions[endpoint]
            blueprint = endpoint.rpartition('.')[0] or None
            with app.test_request_context('/'):
                defaults, decorated = manager.resolve_limits(
                    app, endpoint=endpoint, blueprint=blueprint,
                    callable_name=get_qualified_name(view))
            return [str(limit.limit) for limit in defaults], \
                   [str(limit.limit) for limit in decorated]

        for endpoint in self.PRINTER_ENDPOINTS:
            defaults, decorated = limits_for(endpoint)
            self.assertEqual(defaults, [], endpoint)
            self.assertEqual(len(decorated), 1, endpoint)
        # The control: a route that did not pass override_defaults does carry it, so the
        # empty lists above are this flag's doing and not an inert assertion.
        self.assertEqual(limits_for('index')[0], ['200 per 1 hour'])

    # --- ProxyFix ---------------------------------------------------------------------

    def test_proxyfix_is_configured_to_honour_one_forwarded_hop(self):
        from werkzeug.middleware.proxy_fix import ProxyFix
        self.assertIsInstance(gui.app.wsgi_app, ProxyFix)
        self.assertEqual(gui.app.wsgi_app.x_for, 1)
        self.assertEqual(gui.app.wsgi_app.x_proto, 1)

    def test_a_forwarded_https_request_passes_the_https_gate(self):
        """Railway terminates TLS and forwards plain HTTP with X-Forwarded-Proto: https.
        Without x_proto=1 every daemon route would answer 403 in production."""
        resp = self.client.post('/printer/daemon/sync', json=IDLE, headers=dict(
            self.headers, **{'X-Forwarded-Proto': 'https'}))
        self.assertEqual(resp.status_code, 200)

    def test_the_same_request_without_the_forwarded_proto_header_is_refused(self):
        resp = self.client.post('/printer/daemon/sync', json=IDLE, headers=self.headers)
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.get_json()['error'], 'HTTPS required')

    def test_a_forwarded_address_becomes_the_request_address(self):
        """One forwarded address reaches the app as request.remote_addr, which is the
        address the limiter falls back to. The test client goes through app.wsgi_app, so
        ProxyFix really runs here -- a bare test_request_context would not apply it."""
        seen = self.capture_remote_addr()
        self.client.get('/printer/status', base_url='https://localhost',
                        headers={'X-Forwarded-For': '203.0.113.7'},
                        environ_overrides={'REMOTE_ADDR': '10.0.0.1'})
        self.assertEqual(seen, ['203.0.113.7'])

    def test_with_two_forwarded_hops_the_nearest_proxy_wins(self):
        """x_for=1 trusts ONE hop, and ProxyFix counts hops from the right. So a header
        that already carries a chain gives the app the address the LAST proxy reported,
        not the browser's -- 10.0.0.9 here, not 203.0.113.7.

        Harmless for the printer routes, whose real limits key on the pairing code. It is
        the open half of verification 8 for anything that keys on the address: what the
        production chain actually looks like needs a live probe."""
        seen = self.capture_remote_addr()
        self.client.get('/printer/status', base_url='https://localhost',
                        headers={'X-Forwarded-For': '203.0.113.7, 10.0.0.9'},
                        environ_overrides={'REMOTE_ADDR': '10.0.0.1'})
        self.assertEqual(seen, ['10.0.0.9'])


class TestNoteCodeWiring(PrinterRouteTestCase):
    """Loading a YAML is what tells the relay a code exists. No wizard step, no route."""

    class _FakeClient:
        last_config_error = None
        last_config_url = None

        def __init__(self, yaml_text):
            self._yaml = yaml_text

        def fetch_config_file(self):
            return self._yaml

    def test_loading_a_config_registers_its_pairing_code(self):
        yaml_text = ('team:\n  number: 6238\n  name: "Popcorn Penguins"\n'
                     'printing:\n  pairing_code: "7k3m-q9xw"\n')
        with app.test_request_context('/'), \
             mock.patch.object(gui.session_manager, 'update_session_tokens',
                               return_value=None):
            gui._load_team_config_into_session(self._FakeClient(yaml_text))
        self.assertTrue(self.relay.status_for(CODE)['paired'])

    def test_loading_a_config_without_a_code_registers_nothing(self):
        with app.test_request_context('/'), \
             mock.patch.object(gui.session_manager, 'update_session_tokens',
                               return_value=None):
            gui._load_team_config_into_session(
                self._FakeClient('team:\n  number: 6238\n'))
        self.assertFalse(self.relay.status_for(CODE)['paired'])


if __name__ == '__main__':
    unittest.main()


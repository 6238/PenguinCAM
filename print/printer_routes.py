"""Flask blueprint for the Bambu printer relay.

Three audiences share one blueprint:
  * the print wizard, through session routes gated on an Onshape session;
  * the Pi daemon, through /printer/daemon/* routes gated on a signed bearer token;
  * the installer fetch, /install-printer.sh.

All relay state lives in printer_relay (no Flask imports there, no printer protocol here).
Wiring follows the slicing branch's pattern: init_printer_routes receives the Flask
module's globals rather than importing them, so there is no import cycle and tests can
substitute any of them.
"""

import os
import time

from flask import Blueprint, current_app, jsonify, request, send_from_directory, session
from flask_limiter.util import get_remote_address

from team_config import TeamConfig

from . import printer_relay

# A sliced file is deleted an hour after it is registered. Refuse a send once it is forty
# minutes old, which keeps both job deadlines (3 min to pick up, 15 to confirm) clear of
# the deletion, so a job can never be handed a download that dies mid-upload.
SLICE_MAX_AGE_SECONDS = 40 * 60

# Rate limits. The app-wide default is 200/hour, and a five-second sync is 720/hour, so
# EVERY route here passes override_defaults=True or the daemon starts taking 429s about
# seventeen minutes after boot.
STATUS_LIMIT = "120 per minute"        # ten open Previews per team, polling every 5 s
JOBS_LIMIT = "10 per minute"
DAEMON_LIMIT = "60 per minute"
EXCHANGE_LIMIT = "60 per minute"       # service-wide; 40 bits is out of reach at that rate
INSTALLER_LIMIT = "30 per minute"

# Assigned by init_printer_routes. Module-level rather than closed over, so tests can
# substitute them.
_HAS_ONSHAPE_SESSION = None
_REFRESH_TEAM_CONFIG = None
_TOKEN_MANAGER = None
_METRICS = None
_LOG = None


def _log(*args):
    if _LOG:
        _LOG(*args)


def _is_dev_mode():
    """Development mode: Flask's debug flag on AND no RAILWAY_ENVIRONMENT.

    The development server started as the project's CLAUDE.md describes meets both;
    gunicorn on Railway meets neither. Two things hang off this: the HTTPS requirement
    below, and PRINTER_DEV_PAIRING_CODE."""
    try:
        return bool(current_app.debug) and not os.environ.get('RAILWAY_ENVIRONMENT')
    except Exception:
        return False


def _require_https(*, plain_text=False):
    """403 unless the request arrived over HTTPS, judged after ProxyFix.

    On Railway, therefore, nothing in this feature is ever served over plain HTTP, whatever
    a proxy forwards. Returns a response tuple to return, or None to proceed.

    `plain_text` is for the installer route, whose caller is `curl ... | bash`: a JSON body
    piped into a shell is noise, while a comment line is inert and readable in a terminal."""
    if request.is_secure or _is_dev_mode():
        return None
    if plain_text:
        return '# HTTPS required\n', 403, {'Content-Type': 'text/plain; charset=utf-8'}
    return jsonify({'error': 'HTTPS required'}), 403


def _maybe_seed_dev_code():
    """Seed the record for PRINTER_DEV_PAIRING_CODE so a daemon can pair against a server
    nobody has opened in Onshape. Read on each request rather than at init, because
    app.debug is still False when this module is imported -- app.run(debug=...) sets it
    afterwards. Ignored outside development mode; seeding twice is harmless."""
    if not _is_dev_mode():
        return
    seed = os.environ.get('PRINTER_DEV_PAIRING_CODE')
    if seed:
        printer_relay.RELAY.seed_code(seed)


def _unauthorized_session():
    """The same JSON shape the other session-gated routes return."""
    return jsonify({'error': 'Session verification required.',
                    'need_verification': True}), 401


def _session_pairing_code():
    """The pairing code from the session's ONSHAPE copy of the team YAML, or None.

    Never `upload_config_data`: a browser that used the Onshape panel earlier may still
    carry an Onshape session, and the anonymous upload flow must not act on that team's
    printer."""
    data = session.get('team_config_data') or {}
    return TeamConfig.from_dict(data).pairing_code


def _safe_session_pairing_code():
    """The session's pairing code, or None when the session cannot be read at all.

    `team_config_data` is whatever an old cookie happens to hold, so TeamConfig can raise on
    it (a list, say). The views must treat that as "no code" -- an unpaired answer from the
    status route and a refusal from the jobs route -- rather than 500 on a browser whose only
    fault is a stale cookie. The key functions guard the same call for the same reason."""
    try:
        return _session_pairing_code()
    except Exception as e:
        _log(f"⚠️  Printer: unreadable team config in the session: {e}")
        return None


def _bearer_token():
    header = request.headers.get('Authorization', '')
    if header.startswith('Bearer '):
        return header[len('Bearer '):].strip()
    return None


def _authenticated_code():
    """The pairing code carried by a correctly signed bearer token, or None.

    Verified by signature alone: the relay never compares the token's code with any YAML,
    because it may not have seen one since it started."""
    payload = printer_relay.read_token(_bearer_token(), current_app.secret_key)
    return payload[0] if payload else None


def _wire_job(job):
    """The job as the daemon receives it: the four fields of the spec's wire shape.

    The relay's own dict also carries `state`, `team_number` and `handed_out_at`, which are
    the relay's bookkeeping and no business of the Pi's."""
    if not job:
        return None
    return {'id': job['id'], 'download_path': job['download_path'],
            'filename': job['filename'], 'created_at': job['created_at']}


def _session_code_key():
    """Rate-limit key for the session routes: the team's pairing code, so one team's
    Previews cannot exhaust another's budget.

    flask-limiter runs this BEFORE the view, so an exception here would be a 500 the view
    never sees. Everything is inside the try, and a session with no usable code just gets
    the address key -- for /printer/jobs that request is refused by the view anyway."""
    try:
        code = _session_pairing_code()
        if code:
            return f'printer-code:{code}'
    except Exception:
        pass
    return get_remote_address()


def _token_code_key():
    """Rate-limit key for the daemon routes: the code inside the bearer token. Same
    try/except rule as _session_code_key, for the same reason."""
    try:
        code = _authenticated_code()
        if code:
            return f'printer-code:{code}'
    except Exception:
        pass
    return get_remote_address()


def _exchange_key():
    """The exchange is limited service-wide on a constant key: forty bits at sixty guesses
    a minute is out of reach, and keying on the address would let a guesser spread the
    attempts over a botnet."""
    return 'printer-exchange'


def _drain_expiry_metrics():
    """Log a printer_ack metrics event for every job that died on a deadline, so the
    metrics show every job's end and not only the acknowledged ones.

    Always called AFTER the read that applies the lazy deadlines (status_for, store_status,
    claim_job, finish_job) -- expiry happens inside that read, so draining first would
    leave the event for the next request to find."""
    for event in printer_relay.RELAY.drain_expiry_events():
        _log(f"🖨️  Printer job {event.job_id} expired: {event.reason}")
        if _METRICS:
            _METRICS.log_event('printer_ack', team_number=event.team_number,
                               metadata={'job_id': event.job_id, 'ok': False,
                                         'error': event.reason, 'expired': True})


def init_printer_routes(app, *, limiter, has_onshape_session, refresh_team_config,
                        token_manager, metrics, log):
    """Register the printer relay blueprint on `app`.

    Receives the Flask module's globals instead of importing them, which is the slicing
    branch's pattern and keeps frc_cam_gui_app a leaf as far as this module is concerned.
    Must be called after limiter, file_token_manager, _has_onshape_session and
    _maybe_refresh_team_config exist."""
    global _HAS_ONSHAPE_SESSION, _REFRESH_TEAM_CONFIG, _TOKEN_MANAGER, _METRICS, _LOG
    _HAS_ONSHAPE_SESSION = has_onshape_session
    _REFRESH_TEAM_CONFIG = refresh_team_config
    _TOKEN_MANAGER = token_manager
    _METRICS = metrics
    _LOG = log

    # The Blueprint is created HERE, not at module scope: Flask refuses to add routes to a
    # blueprint that is already registered, so a module-level one would make this function
    # callable exactly once per process and blow up in any test that re-wires the app.
    bp = Blueprint('printer', __name__)

    # --- session routes: the print wizard ----------------------------------------------

    @bp.route('/printer/status')
    @limiter.limit(STATUS_LIMIT, key_func=_session_code_key, override_defaults=True)
    def printer_status():
        if not _HAS_ONSHAPE_SESSION():
            return _unauthorized_session()
        try:
            _REFRESH_TEAM_CONFIG()
        except Exception as e:      # a transient Onshape failure must not break polling
            _log(f"⚠️  Printer status: team config refresh failed: {e}")
        _maybe_seed_dev_code()
        code = _safe_session_pairing_code()
        if code:
            printer_relay.RELAY.note_code(code)
        payload = printer_relay.RELAY.status_for(code)
        _drain_expiry_metrics()
        return jsonify(payload)

    @bp.route('/printer/jobs', methods=['POST'])
    @limiter.limit(JOBS_LIMIT, key_func=_session_code_key, override_defaults=True)
    def printer_jobs():
        if not _HAS_ONSHAPE_SESSION():
            return _unauthorized_session()
        code = _safe_session_pairing_code()
        if not code:
            return jsonify({'error': 'No printer paired'}), 409
        printer_relay.RELAY.note_code(code)

        body = request.get_json(silent=True) or {}
        token = body.get('token')
        info = _TOKEN_MANAGER.get_file(token) if token else None
        if not info:
            return jsonify({'error': 'File not found or expired'}), 404
        if time.time() - info.get('created', 0) > SLICE_MAX_AGE_SECONDS:
            return jsonify({'error': 'slice again'}), 409

        team_number = session.get('team_number')
        job_id = printer_relay.new_job_id()
        ok, reason, job = printer_relay.RELAY.claim_job(
            code, job_id=job_id, download_path=f'/download/{token}',
            filename=info['filename'], team_number=team_number)
        _drain_expiry_metrics()
        if not ok:
            return jsonify({'error': reason}), 409

        _log(f"🖨️  Printer job {job_id} queued: {info['filename']}")
        if _METRICS:
            _METRICS.log_event('printer_send', team_number=team_number,
                               user_email=session.get('user_email'),
                               metadata={'job_id': job_id, 'filename': info['filename']})
        return jsonify({'ok': True, 'job_id': job_id}), 202

    # --- daemon routes: the Pi ---------------------------------------------------------
    # All require HTTPS outside development mode. All but the exchange and the installer
    # carry Authorization: Bearer <token>. None of them ever calls the session gate, and
    # every one of them validates the token BEFORE it touches the relay, so an unverified
    # code never reaches relay state.

    @bp.route('/printer/daemon/pair', methods=['POST'])
    @limiter.limit(EXCHANGE_LIMIT, key_func=_exchange_key, override_defaults=True)
    def printer_daemon_pair():
        refusal = _require_https()
        if refusal:
            return refusal
        _maybe_seed_dev_code()
        body = request.get_json(silent=True) or {}
        code = printer_relay.normalize_code(body.get('code'))
        if code and printer_relay.RELAY.begin_exchange(code):
            token = printer_relay.issue_token(code, current_app.secret_key)
            _log(f"🖨️  Printer daemon paired for code {printer_relay.format_code(code)}")
            return jsonify({'token': token})
        # The SAME body for an unknown code, a code no session has presented yet, and a
        # code whose daemon is already alive -- so a guesser learns nothing from the reply.
        return jsonify({'error': 'not paired'}), 404

    @bp.route('/printer/daemon/sync', methods=['POST'])
    @limiter.limit(DAEMON_LIMIT, key_func=_token_code_key, override_defaults=True)
    def printer_daemon_sync():
        refusal = _require_https()
        if refusal:
            return refusal
        code = _authenticated_code()
        if not code:
            return jsonify({'error': 'repair'}), 401
        printer_relay.RELAY.note_daemon(code)
        snapshot = request.get_json(silent=True) or {}
        job = printer_relay.RELAY.store_status(code, snapshot)
        _drain_expiry_metrics()
        return jsonify({'job': _wire_job(job)})

    @bp.route('/printer/daemon/jobs/<job_id>/ack', methods=['POST'])
    @limiter.limit(DAEMON_LIMIT, key_func=_token_code_key, override_defaults=True)
    def printer_daemon_ack(job_id):
        refusal = _require_https()
        if refusal:
            return refusal
        code = _authenticated_code()
        if not code:
            return jsonify({'error': 'repair'}), 401
        printer_relay.RELAY.note_daemon(code)
        body = request.get_json(silent=True) or {}
        ok = bool(body.get('ok'))
        reason = body.get('error') or None
        found, team_number = printer_relay.RELAY.finish_job(code, job_id, ok=ok,
                                                            reason=reason)
        _drain_expiry_metrics()
        if not found:
            # The daemon treats this as final: the relay no longer holds that job.
            return jsonify({'error': 'unknown job'}), 404
        _log(f"🖨️  Printer job {job_id} acknowledged: {'ok' if ok else reason}")
        if _METRICS:
            _METRICS.log_event('printer_ack', team_number=team_number,
                               metadata={'job_id': job_id, 'ok': ok, 'error': reason})
        return jsonify({'ok': True})

    @bp.route('/printer/daemon/ping')
    @limiter.limit(DAEMON_LIMIT, key_func=_token_code_key, override_defaults=True)
    def printer_daemon_ping():
        refusal = _require_https()
        if refusal:
            return refusal
        code = _authenticated_code()
        if not code:
            return jsonify({'error': 'repair'}), 401
        # Read-only on purpose: `penguincam-printer status` must not be able to disturb the
        # running service, so this does NOT record daemon_seen_at and hands out no job.
        return jsonify(printer_relay.RELAY.ping(code))

    # --- the installer -----------------------------------------------------------------

    @bp.route('/install-printer.sh')
    @limiter.limit(INSTALLER_LIMIT, key_func=get_remote_address, override_defaults=True)
    def install_printer_sh():
        """A convenience alias for the installer. The DOCUMENTED command fetches the same
        script from raw.githubusercontent.com pinned to a commit, because free-plan
        Cloudflare Bot Fight Mode cannot be skipped per path and a challenge page piped
        into bash fails confusingly. See docs/PRINTER_SETUP.md."""
        refusal = _require_https(plain_text=True)
        if refusal:
            return refusal
        # Beside this module inside the print package, not under app.root_path: the
        # daemon moved with the rest of the print path and the app's root is the
        # repository.
        return send_from_directory(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                'printer_daemon'),
                                   'install.sh', mimetype='text/x-shellscript')

    app.register_blueprint(bp)
    return bp

"""Flask blueprint for the 3D print wizard: page, part info, slice jobs, events.

Never imports frc_cam_gui_app. Everything shared (session gate, token manager,
limiter, folders, metrics, logging, template context) arrives through
init_print_routes(...). See docs/3D_PRINTING.md.
"""
import contextlib
import json
import os
import shutil
import tempfile
import time
import types

from flask import (Blueprint, Response, g, jsonify, make_response, redirect, render_template, request,
                   send_file, session)

from .events import configure_print_events, print_event, valid_page_id
from .jobs import GENERIC_FAILURE, JobFailure, JobPool, QueueFull
from .slicer import SAMPLE_STL, SliceError, part_info, slice_stl

DELIVERED_NAME = "sample_part.gcode.3mf"
BUSY_MESSAGE = "The slicer is busy, try again in a minute."
EXPIRED_MESSAGE = "This slice has expired, go back and slice again."
PAGE_ID_MESSAGE = "This page is out of date; reload it."
BAD_REPORT_MESSAGE = "This report could not be read."
CLIENT_EVENT_KEYS = ("step", "message", "code")
CLIENT_EVENT_MAX_CHARS = 300
DELIVER_ACTIONS = ("download", "drive", "printer")
DELIVER_OUTCOME_MAX_CHARS = 40
PAGE_FIELD_MAX_CHARS = 200
FRAME_ANCESTORS = "frame-ancestors https://*.onshape.com"

# The blueprint carries its own templates and static files, so everything the print
# path needs lives under print/. static_url_path must not be the default "/static":
# that would collide with the application's own static endpoint.
print_bp = Blueprint("print", __name__, template_folder="templates",
                     static_folder="static", static_url_path="/print/static")

# Filled by init_print_routes; routes read it at request time (tests swap ctx.pool).
ctx = types.SimpleNamespace(pool=None, token_manager=None, upload_folder=None, output_folder=None,
                            metrics=None, log=None, template_context=None, require_session=None)


def safe_return_path(value):
    """Only a same-origin path is allowed as the return target; anything else is /app."""
    if not isinstance(value, str) or not value or value[0] != "/":
        return "/app"
    if len(value) > 1 and value[1] in "/\\":     # //host and /\host are off-origin
        return "/app"
    if any(ch < " " or ch == "\x7f" for ch in value):
        # A control character would reach the redirect's Location header, which
        # Werkzeug refuses with a 500 rather than emitting; fall back instead.
        return "/app"
    return value


def _with_verify(path):
    """Append verify=1 to the query, keeping any #fragment at the end where it belongs."""
    path, sep, fragment = path.partition("#")
    return path + ("&" if "?" in path else "?") + "verify=1" + sep + fragment


def _page_id():
    """The request's page id: the X-Print-Page header, or for a GET (an EventSource or
    a poll cannot set headers) the pid query parameter. None unless it is valid."""
    value = request.headers.get("X-Print-Page")
    if value is None and request.method == "GET":
        value = request.args.get("pid")
    return valid_page_id(value)


def _page_id_gate():
    if _page_id() is None:
        return jsonify({"error": PAGE_ID_MESSAGE}), 400
    return None


def _team_for_events():
    """The team number for print events; None (logged as -) on the default config,
    which reports team 6238 without being that team."""
    if session.get("using_default_config"):
        return None
    try:
        return int(session.get("team_number"))
    except (TypeError, ValueError):
        return None


@print_bp.before_request
def _start_request_clock():
    g.print_request_started = time.monotonic()


@print_bp.after_request
def _log_request_event(response):
    # The route rule, never the path: a full job id in /print-job/<id> is a bearer
    # of the download and must not reach a log.
    if request.endpoint != "print.static":
        started = g.get("print_request_started", time.monotonic())
        print_event("request", pid=_page_id(), team=_team_for_events(), method=request.method,
                    rule=request.url_rule.rule if request.url_rule else None,
                    status=response.status_code, ms=round((time.monotonic() - started) * 1000))
    return response


def _short_text(value, limit):
    """A browser-supplied field as a print event value: strings cut to `limit`, numbers
    and booleans as they are, anything else (an object, a list) dropped to None."""
    if isinstance(value, str):
        return value[:limit]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return None


def _json_object():
    body = request.get_json(silent=True)
    return body if isinstance(body, dict) else None


def make_worker(token_manager, upload_folder, output_folder, log):
    """Worker for JobPool: slice the sample part, deliver the archive, clean up."""

    def run(job_id):
        started = time.monotonic()
        scratch = tempfile.mkdtemp(prefix=f"print_{job_id[:8]}_", dir=upload_folder)

        def seconds():
            return round(time.monotonic() - started, 3)

        try:
            result = slice_stl(SAMPLE_STL, scratch)
            final_path = os.path.join(output_folder, f"print_{job_id}.gcode.3mf")
            shutil.move(str(result.output_path), final_path)
            try:
                token = token_manager.register_file(final_path, DELIVERED_NAME)
            except Exception:
                # No token means nothing can ever fetch or expire this archive.
                with contextlib.suppress(OSError):
                    os.unlink(final_path)
                raise
            print_event("slice_done", job=job_id[:8], seconds=seconds(), code=0)
            return {"token": token, "summary": result.summary(), "part": part_info()}
        except SliceError as exc:
            print_event("slice_failed", job=job_id[:8], seconds=seconds(), code=exc.code, reason=exc.message)
            raise JobFailure(exc.message, exc.details) from exc
        except Exception:
            # The pool logs the traceback; the event records that the slice failed.
            print_event("slice_failed", job=job_id[:8], seconds=seconds(), code=None, reason=GENERIC_FAILURE)
            raise
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    return run


def _sse(event, record):
    return f"event: {event}\ndata: {json.dumps(record)}\n\n"


@print_bp.route("/print")
def print_page():
    source = request.args.get("source", "upload")
    if source not in ("upload", "onshape"):
        source = "upload"
    theme = request.args.get("theme", "dark")
    if theme not in ("dark", "light"):
        theme = "dark"
    return_url = safe_return_path(request.args.get("return", "/app"))
    if ctx.require_session():
        return redirect(_with_verify(return_url))
    context = ctx.template_context(force_defaults=(source == "upload"))
    resp = make_response(render_template("print_wizard.html", source=source, theme=theme,
                                         return_url=return_url, **context))
    if source == "onshape":
        resp.headers["Content-Security-Policy"] = FRAME_ANCESTORS
        resp.headers.pop("X-Frame-Options", None)
    return resp


@print_bp.route("/print/part")
def print_part():
    if request.args.get("stl") == "1":
        return send_file(str(SAMPLE_STL), mimetype="model/stl", download_name=SAMPLE_STL.name)
    return jsonify(part_info())


def print_job_submit():
    """Queue a slice. Rate limited to 3 per minute; the rule is added in
    init_print_routes because the limiter does not exist at import time."""
    gate = ctx.require_session() or _page_id_gate()
    if gate:
        return gate
    try:
        job_id = ctx.pool.submit()
    except QueueFull:
        return jsonify({"error": BUSY_MESSAGE}), 503
    print_event("slice_queued", pid=_page_id(), team=_team_for_events(), job=job_id[:8])
    return jsonify({"job_id": job_id}), 202


@print_bp.route("/print-job/<job_id>")
def print_job_status(job_id):
    gate = ctx.require_session() or _page_id_gate()
    if gate:
        return gate
    record = ctx.pool.get(job_id)
    if record is None:
        return jsonify({"error": EXPIRED_MESSAGE}), 404
    return jsonify(record)


@print_bp.route("/print-job/<job_id>/events")
def print_job_events(job_id):
    gate = ctx.require_session() or _page_id_gate()
    if gate:
        return gate
    if ctx.pool.get(job_id) is None:
        return jsonify({"error": EXPIRED_MESSAGE}), 404
    pool = ctx.pool

    def stream():
        for event, record in pool.follow(job_id):
            yield _sse(event, record)

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def print_page_event():
    """The print page's first call: one `page` event describing how it was opened.
    10 per minute, added in init_print_routes."""
    gate = ctx.require_session() or _page_id_gate()
    if gate:
        return gate
    body = _json_object() or {}
    default_config = bool(session.get("using_default_config"))
    team_config = {} if default_config else (session.get("team_config_data") or {})
    fields = {key: _short_text(body.get(key), PAGE_FIELD_MAX_CHARS)
              for key in ("source", "theme", "has_onshape_context", "wvm", "server")}
    print_event("page", pid=_page_id(), team=_team_for_events(), **fields,
                configured=isinstance(team_config.get("printing"), dict),
                config="default" if default_config or not team_config else "team")
    return jsonify({}), 200


def print_client_event():
    """An error the browser showed the student (showError). Only step, message and code,
    each cut to 300 characters. 30 per minute, added in init_print_routes."""
    gate = ctx.require_session() or _page_id_gate()
    if gate:
        return gate
    body = _json_object()
    if body is None or any(key not in CLIENT_EVENT_KEYS for key in body):
        return jsonify({"error": BAD_REPORT_MESSAGE}), 400
    print_event("client_error", pid=_page_id(), team=_team_for_events(),
                **{key: _short_text(body.get(key), CLIENT_EVENT_MAX_CHARS) for key in CLIENT_EVENT_KEYS})
    return "", 204


def print_deliver_event():
    """Download, Drive or Send to Printer, as the browser saw it. Only the first eight
    characters of the job id are logged. 30 per minute, added in init_print_routes."""
    gate = ctx.require_session() or _page_id_gate()
    if gate:
        return gate
    body = _json_object() or {}
    job_id, action = body.get("job_id"), body.get("action")
    if not isinstance(job_id, str) or action not in DELIVER_ACTIONS:
        return jsonify({"error": BAD_REPORT_MESSAGE}), 400
    try:
        print_event("deliver", pid=_page_id(), team=_team_for_events(), job=job_id[:8], action=action,
                    outcome=_short_text(body.get("outcome"), DELIVER_OUTCOME_MAX_CHARS))
    except ValueError:      # not a job id: refused rather than logged
        return jsonify({"error": BAD_REPORT_MESSAGE}), 400
    return "", 204


def init_print_routes(app, *, limiter, require_session, token_manager, upload_folder, output_folder,
                      template_context, metrics, log, pool=None):
    """Register the print blueprint. Called once by frc_cam_gui_app after the
    limiter and temp folders exist. Returns the job pool in use."""
    ctx.require_session = require_session
    ctx.token_manager = token_manager
    ctx.upload_folder = upload_folder
    ctx.output_folder = output_folder
    ctx.template_context = template_context
    ctx.metrics = metrics
    ctx.log = log
    configure_print_events(log, metrics)
    if pool is None:
        pool = JobPool(make_worker(token_manager, upload_folder, output_folder, log), log=log)
        pool.start_sweeper()
    ctx.pool = pool
    # flask-limiter evaluates a decorated limit only through the wrapper its
    # decorator returns, so that wrapper — not the bare view — has to be the
    # registered view function. The limiter does not exist when this module is
    # imported, so the rule is added here rather than with a decorator at
    # definition time.
    print_bp.add_url_rule("/print-job", "print_job_submit",
                          limiter.limit("3 per minute")(print_job_submit), methods=["POST"])
    print_bp.add_url_rule("/print/page", "print_page_event",
                          limiter.limit("10 per minute")(print_page_event), methods=["POST"])
    print_bp.add_url_rule("/print/client-event", "print_client_event",
                          limiter.limit("30 per minute")(print_client_event), methods=["POST"])
    print_bp.add_url_rule("/print/deliver", "print_deliver_event",
                          limiter.limit("30 per minute")(print_deliver_event), methods=["POST"])
    app.register_blueprint(print_bp)
    return pool

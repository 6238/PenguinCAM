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
import traceback
import types
from pathlib import Path

from flask import (Blueprint, Response, g, jsonify, make_response, redirect, render_template, request,
                   send_file, session)

from onshape_integration import OnshapeAuthError

from . import limits
from .events import configure_print_events, print_event, valid_page_id
from .jobs import GENERIC_FAILURE, JobFailure, JobPool, QueueFull
from .mesh import MeshError, orientation_footprints, part_size_mm, read_binary_stl
from .onshape_parts import (ELEMENT_CACHE, LIMIT_REACHED_MESSAGE, NO_CONTEXT_MESSAGE, VIAS, ExportFailed,
                            NoOnshapeContext, OnshapeCallFailed, OnshapeLimitReached, PartRefused,
                            PartTooLarge, export_part_mesh, panel_context_from_return, resolve_selections,
                            rest_address)
from .part_store import PartLimit, PartStore
from .plate import plate_from_printer
from .slicer import SAMPLE_STL, SliceError, _load_profile, part_info, slice_stl

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
# The parts route's sentences (spec 3.5). LIMIT_REACHED_MESSAGE comes from onshape_parts.
SIGNIN_EXPIRED_MESSAGE = "Your Onshape sign-in expired. Go back, Connect, and return to 3D Printing."
EXPORT_FAILED_MESSAGE = limits.EXPORT_FAILED_MESSAGE
# A resolution call failed before any part had a name: the export sentence, unnamed.
RESOLVE_FAILED_MESSAGE = EXPORT_FAILED_MESSAGE.format(name="the selected part")
BAD_PARTS_REQUEST_MESSAGE = "This request could not be read."
PART_EXPIRED_MESSAGE = "This part has expired; Refresh from Onshape."
PART_STORE_FOLDER = "print_parts"

# The blueprint carries its own templates and static files, so everything the print
# path needs lives under print/. static_url_path must not be the default "/static":
# that would collide with the application's own static endpoint.
print_bp = Blueprint("print", __name__, template_folder="templates",
                     static_folder="static", static_url_path="/print/static")

# Filled by init_print_routes; routes read it at request time (tests swap ctx.pool).
ctx = types.SimpleNamespace(pool=None, token_manager=None, upload_folder=None, output_folder=None,
                            metrics=None, log=None, template_context=None, require_session=None,
                            has_onshape_session=None, onshape_client=None, part_store=None,
                            element_cache=ELEMENT_CACHE)


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
    # The Onshape ids arrive only inside the return link (the CNC panel's full query).
    onshape = panel_context_from_return(return_url)
    onshape_error = None
    if source == "onshape":
        try:
            rest_address(onshape)
        except NoOnshapeContext:
            onshape_error = NO_CONTEXT_MESSAGE
    resp = make_response(render_template("print_wizard.html", source=source, theme=theme,
                                         return_url=return_url, onshape=onshape,
                                         onshape_error=onshape_error, **context))
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
    """The print page's first call: one `page` event describing how it was opened. Answers
    the plate (the default printer's, with the margin and spacing) and the limits the
    browser checks. 10 per minute, added in init_print_routes."""
    gate = ctx.require_session() or _page_id_gate()
    if gate:
        return gate
    body = _json_object() or {}
    fields = {key: _short_text(body.get(key), PAGE_FIELD_MAX_CHARS)
              for key in ("source", "theme", "has_onshape_context", "wvm", "server")}
    if body.get("source") == "onshape":
        default_config = bool(session.get("using_default_config"))
        team_config = {} if default_config else (session.get("team_config_data") or {})
        team = _team_for_events()
    else:
        # The upload page never reads the Onshape sign-in's team config, as the app's
        # template context does not (force_defaults): it prints with the defaults.
        default_config, team_config, team = True, {}, None
    print_event("page", pid=_page_id(), team=team, **fields,
                configured=isinstance(team_config.get("printing"), dict),
                config="default" if default_config or not team_config else "team")
    return jsonify({"plate": plate_from_printer(_load_profile("printer.json")),
                    "limits": limits.limits_for_browser()}), 200


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


# ---------------------------------------------------------------------------
# Parts from Onshape (spec 3.3, 3.4)
# ---------------------------------------------------------------------------

def _signin_expired():
    return jsonify({"error": SIGNIN_EXPIRED_MESSAGE, "code": "signin_expired"}), 401


def _size_text(size_mm):
    return f"{size_mm['x']:g}x{size_mm['y']:g}x{size_mm['z']:g}"


class _PartsAnswer:
    """The parts and error sentences of one parts request, and its events."""

    def __init__(self, pid, team):
        self.pid, self.team = pid, team
        self.parts, self.errors = [], []

    def error(self, message, name=None):
        self.errors.append({"name": name, "message": message} if name else {"message": message})

    def failed(self, part, status, reason, message):
        print_event("export_failed", pid=self.pid, team=self.team, part=part.source.part_id,
                    status=status, reason=reason)
        self.error(message, part.name)

    def json(self):
        return jsonify({"parts": [entry.to_json() for entry in self.parts], "errors": self.errors}), 200


def _export_one(client, part, pid, answer):
    """Export, read and store one resolved part. Raises OnshapeLimitReached and
    OnshapeAuthError for the caller; every other failure becomes a sentence."""
    try:
        result = export_part_mesh(client, part, byte_cap=limits.MAX_PART_BYTES, cache=ctx.element_cache)
    except PartTooLarge:
        answer.failed(part, None, "too_large", limits.TOO_DETAILED_MESSAGE.format(name=part.name))
        return
    except PartRefused as exc:
        answer.failed(part, None, "refused", exc.message)
        return
    except ExportFailed as exc:
        answer.failed(part, exc.status, "export", EXPORT_FAILED_MESSAGE.format(name=part.name))
        return
    except (OnshapeLimitReached, OnshapeAuthError):
        raise
    except Exception:       # a connection error or worse: logged here, a sentence for the student
        if ctx.log:
            ctx.log(traceback.format_exc())
        answer.failed(part, None, "error", EXPORT_FAILED_MESSAGE.format(name=part.name))
        return
    source = result.source
    try:
        mesh = read_binary_stl(result.stl, part.name)
        info = {"triangles": mesh.triangles, "size_mm": part_size_mm(mesh),
                "footprints": orientation_footprints(mesh)}
    except MeshError as exc:
        answer.failed(part, 200, "mesh", exc.message)
        return
    try:
        entry = ctx.part_store.add(pid, part.name, source.to_json(), source.key(), result.stl, info)
    except PartLimit as exc:
        answer.failed(part, 200, "store", exc.message)
        return
    print_event("export", pid=pid, team=answer.team, part=source.part_id, element=source.element_id,
                bytes=len(result.stl), triangles=mesh.triangles, size_mm=_size_text(info["size_mm"]),
                calls=result.calls, ms=result.ms, outcome="ok")
    answer.parts.append(entry)


def print_onshape_parts():
    """Resolve the browser's Onshape selections, export each new part once and store it.
    The Onshape sign-in gate, then the page id. 20 per minute, added in init_print_routes."""
    if not ctx.has_onshape_session():
        return _signin_expired()
    gate = _page_id_gate()
    if gate:
        return gate
    pid, team = _page_id(), _team_for_events()
    body = _json_object()
    if (body is None or body.get("via") not in VIAS or not isinstance(body.get("selections"), list)
            or not isinstance(body.get("replace", []), list)):
        return jsonify({"error": BAD_PARTS_REQUEST_MESSAGE}), 400
    via, selections = body["via"], body["selections"]
    try:
        address = rest_address(body.get("context"))
    except NoOnshapeContext:
        return jsonify({"error": NO_CONTEXT_MESSAGE}), 400
    client = ctx.onshape_client()
    if client is None or getattr(client, "credentials_dead", False):
        return _signin_expired()
    answer = _PartsAnswer(pid, team)
    if via != "refresh":
        try:
            ctx.part_store.check_room(pid, len(selections))
        except PartLimit as exc:
            answer.error(exc.message)
            return answer.json()
    try:
        resolved, errors = resolve_selections(client, address, selections, via, cache=ctx.element_cache)
        element_type = ctx.element_cache.element_type(client, address) if via == "selection" else None
    except OnshapeAuthError:
        return _signin_expired()
    except OnshapeLimitReached:
        answer.error(LIMIT_REACHED_MESSAGE)
        return answer.json()
    except Exception as exc:    # OnshapeCallFailed, a connection error: never the upstream body
        if ctx.log and not isinstance(exc, OnshapeCallFailed):
            ctx.log(traceback.format_exc())
        print_event("select", pid=pid, team=team, via=via, count=len(selections), element_type=None)
        answer.error(RESOLVE_FAILED_MESSAGE)
        return answer.json()
    print_event("select", pid=pid, team=team, via=via, count=len(selections), element_type=element_type)
    answer.errors.extend(errors)
    for part in resolved:
        existing = None if via == "refresh" else ctx.part_store.find_source(pid, part.source.key())
        if existing is not None:
            answer.parts.append(existing)
            continue
        try:
            _export_one(client, part, pid, answer)
        except OnshapeLimitReached:
            # The allowance is spent: no later part can be exported either.
            answer.failed(part, 402, "limit_reached", LIMIT_REACHED_MESSAGE)
            break
        except OnshapeAuthError:
            return _signin_expired()
    if via == "refresh":
        ctx.part_store.supersede(body.get("replace", []), pid)
    return answer.json()


def print_part_stl(ref):
    """One stored part's STL, for the page id that owns it. 120 per minute, added in
    init_print_routes."""
    gate = ctx.require_session() or _page_id_gate()
    if gate:
        return gate
    entry = ctx.part_store.get(ref, _page_id())
    if entry is None:       # unknown, expired or another page's: the same answer
        return jsonify({"error": PART_EXPIRED_MESSAGE}), 404
    return send_file(entry.stl_path, mimetype="model/stl")


def init_print_routes(app, *, limiter, require_session, has_onshape_session, onshape_client, token_manager,
                      upload_folder, output_folder, template_context, metrics, log, pool=None, part_store=None):
    """Register the print blueprint. Called once by frc_cam_gui_app after the
    limiter and temp folders exist. `has_onshape_session()` is the Onshape sign-in gate and
    `onshape_client()` returns this browser's OnshapeClient or None. Without `part_store`
    (tests pass their own), the part store gets its own folder under the upload folder and
    its sweeper is started. Returns the job pool in use."""
    ctx.require_session = require_session
    ctx.has_onshape_session = has_onshape_session
    ctx.onshape_client = onshape_client
    if part_store is None:
        part_store = PartStore(Path(upload_folder) / PART_STORE_FOLDER, log=log)
        part_store.start_sweeper()
    ctx.part_store = part_store
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
    print_bp.add_url_rule("/print/onshape/parts", "print_onshape_parts",
                          limiter.limit("20 per minute")(print_onshape_parts), methods=["POST"])
    print_bp.add_url_rule("/print/parts/<ref>.stl", "print_part_stl",
                          limiter.limit("120 per minute")(print_part_stl))
    app.register_blueprint(print_bp)
    return pool

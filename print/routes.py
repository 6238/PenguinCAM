"""Flask blueprint for the 3D print wizard: page, part info, slice jobs, events.

Never imports frc_cam_gui_app. Everything shared (session gate, token manager,
limiter, folders, metrics, logging, template context) arrives through
init_print_routes(...). See docs/3D_PRINTING.md.
"""
import contextlib
import json
import math
import os
import shutil
import tempfile
import time
import traceback
import types
from pathlib import Path

from flask import (Blueprint, Response, g, jsonify, make_response, redirect, render_template, request,
                   send_file, session)
from shapely.geometry import Polygon

from onshape_integration import OnshapeAuthError

from . import limits
from .events import configure_print_events, print_event, valid_page_id
from .jobs import GENERIC_FAILURE, JobFailure, JobPool, QueueFull
from .mesh import MeshError, orientation_footprints, part_size_mm, read_binary_stl
from .onshape_parts import (ELEMENT_CACHE, LIMIT_REACHED_MESSAGE, NO_CONTEXT_MESSAGE, VIAS, ExportFailed,
                            NoOnshapeContext, OnshapeCallFailed, OnshapeLimitReached, PartRefused,
                            PartTooLarge, export_part_mesh, panel_context_from_return, resolve_selections,
                            rest_address)
from .part_store import REF_RE, PartLimit, PartStore
from .plate import ORIENTATIONS, check_placement, plate_from_printer
from .plate_3mf import delivered_file_name
from .plate_job import JobPart, PlateJob, run_plate_job
from .print_config import OVERRIDE_CHOICES, OverrideError, choices_for_request, validate_overrides
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
# The parts route's sentences (spec 3.5). LIMIT_REACHED_MESSAGE comes from onshape_parts.
SIGNIN_EXPIRED_MESSAGE = "Your Onshape sign-in expired. Go back, Connect, and return to 3D Printing."
EXPORT_FAILED_MESSAGE = limits.EXPORT_FAILED_MESSAGE
# A resolution call failed before any part had a name: the export sentence, unnamed, without
# "Refresh from Onshape", which cannot help a click that did not resolve.
RESOLVE_FAILED_MESSAGE = "Onshape could not export the selected part. Try again."
BAD_PARTS_REQUEST_MESSAGE = "This request could not be read."
PART_EXPIRED_MESSAGE = "This part has expired; Refresh from Onshape."
PART_STORE_FOLDER = "print_parts"
# The plate job's sentences (spec 4.6). The limits' sentences come from print/limits.py.
REFRESH_PARTS_MESSAGE = "A part is no longer on the server; press Refresh from Onshape on the Parts step."
BAD_JOB_MESSAGE = "This request could not be read."
CHOOSE_PROFILES_MESSAGE = "Choose a filament and print settings from the list."

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
    """Worker for JobPool: slice the sample part (no payload) or a PlateJob, deliver the
    archive, clean up."""

    def deliver(job_id, result, name):
        final_path = os.path.join(output_folder, f"print_{job_id}.gcode.3mf")
        shutil.move(str(result.output_path), final_path)
        try:
            return token_manager.register_file(final_path, name)
        except Exception:
            # No token means nothing can ever fetch or expire this archive.
            with contextlib.suppress(OSError):
                os.unlink(final_path)
            raise

    def run(job_id, payload=None):
        started = time.monotonic()
        scratch = tempfile.mkdtemp(prefix=f"print_{job_id[:8]}_", dir=upload_folder)
        job8 = job_id[:8]
        who = {} if payload is None else {"pid": payload.pid, "team": payload.team}

        def seconds():
            return round(time.monotonic() - started, 3)

        try:
            if payload is None:
                result = slice_stl(SAMPLE_STL, scratch)
                token = deliver(job_id, result, DELIVERED_NAME)
                print_event("slice_done", job=job8, seconds=seconds(), code=0)
                return {"token": token, "summary": result.summary(), "part": part_info()}
            result = run_plate_job(payload, Path(scratch))
            token = deliver(job_id, result, payload.delivered_name)
            print_event("slice_done", **who, job=job8, copies=len(payload.copies),
                        triangles=payload.triangles(), seconds=seconds())
            return {"token": token, "summary": result.summary(),
                    "part": {"name": payload.delivered_name, "copies": len(payload.copies)}}
        except SliceError as exc:
            print_event("slice_failed", **who, job=job8, seconds=seconds(), code=exc.code, reason=exc.message)
            raise JobFailure(exc.message, exc.details) from exc
        except Exception:
            # The pool logs the traceback; the event records that the slice failed.
            print_event("slice_failed", **who, job=job8, seconds=seconds(), code=None, reason=GENERIC_FAILURE)
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


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _valid_copy(copy):
    return (isinstance(copy, dict) and isinstance(copy.get("ref"), str) and REF_RE.fullmatch(copy["ref"]) is not None
            and copy.get("orientation") in ORIENTATIONS
            and isinstance(copy.get("angle"), int) and not isinstance(copy["angle"], bool)
            and 0 <= copy["angle"] <= 359
            and _is_number(copy.get("x")) and _is_number(copy.get("y")))


def _print_choices(onshape):
    """The request's PrintChoices: the team's for the Onshape flow, the defaults for the
    upload mode (R8) and for a session on the default config."""
    session_data = None if session.get("using_default_config") else session.get("team_config_data")
    return choices_for_request(session_data, force_defaults=not onshape)


def _plate_job(body, pid, team):
    """The PlateJob for a body with `copies`, or the (response, status) that refuses it.
    Every check runs here, before anything reaches the pool or Orca."""
    copies = body.get("copies")
    if not isinstance(copies, list) or not copies or not all(_valid_copy(c) for c in copies):
        return None, (jsonify({"error": BAD_JOB_MESSAGE}), 400)
    if len(copies) > limits.MAX_COPIES:
        return None, (jsonify({"error": limits.TOO_MANY_COPIES_MESSAGE}), 400)
    copies = [{key: c[key] for key in ("ref", "orientation", "angle", "x", "y")} for c in copies]
    entries = {}
    for c in copies:
        if c["ref"] not in entries:
            entry = ctx.part_store.get(c["ref"], pid)
            if entry is None:       # unknown, expired or another page's: the same answer
                return None, (jsonify({"error": REFRESH_PARTS_MESSAGE}), 400)
            entries[c["ref"]] = entry
    triangles = sum(entries[c["ref"]].triangles for c in copies)
    if triangles > limits.MAX_JOB_TRIANGLES:
        return None, (jsonify({"error": limits.JOB_TRIANGLES_MESSAGE}), 400)
    choices = _print_choices(onshape=True)
    if choices.error:
        return None, (jsonify({"error": choices.error}), 400)
    filament = body.get("filament", choices.filaments[0])
    process = body.get("process", choices.processes[0])
    try:
        profiles = choices.slice_profiles(filament, process)
        overrides = validate_overrides(body.get("overrides"), choices.allow_overrides)
    except ValueError:
        return None, (jsonify({"error": CHOOSE_PROFILES_MESSAGE}), 400)
    except OverrideError as exc:
        return None, (jsonify({"error": exc.message}), 400)
    plate = plate_from_printer(choices.printer_profile())
    parts = {ref: {"name": e.name, "footprints": e.footprints} for ref, e in entries.items()}
    problems = check_placement(copies, parts, plate)
    if problems:
        return None, (jsonify({"error": problems[0]["message"], "rule": problems[0]["rule"]}), 400)
    hull_area = sum(Polygon(parts[c["ref"]]["footprints"][c["orientation"]]["hull"]).area for c in copies)
    plate_area = (plate["max"][0] - plate["min"][0]) * (plate["max"][1] - plate["min"][1])
    print_event("layout", pid=pid, team=team, copies=len(copies), parts=len(entries),
                fill=round(100 * hull_area / plate_area), free_angles=sum(1 for c in copies if c["angle"] % 90))
    job = PlateJob(copies=copies,
                   parts={ref: JobPart(name=e.name, stl_path=Path(e.stl_path), footprints=e.footprints,
                                       triangles=e.triangles) for ref, e in entries.items()},
                   profiles=profiles, overrides=overrides, filament=filament, process=process,
                   delivered_name=delivered_file_name([e.name for e in entries.values()], body.get("local_time")),
                   pid=pid, team=team)
    return job, None


def print_job_submit():
    """Queue a slice: the sample part when the body has no `copies`, otherwise a plate job
    whose placement the server checks first. Rate limited to 3 per minute; the rule is
    added in init_print_routes because the limiter does not exist at import time."""
    gate = ctx.require_session() or _page_id_gate()
    if gate:
        return gate
    pid, team = _page_id(), _team_for_events()
    body = _json_object() or {}
    job = None
    if "copies" in body:
        job, refusal = _plate_job(body, pid, team)
        if refusal:
            return refusal
    try:
        job_id = ctx.pool.submit(payload=job)
    except QueueFull:
        return jsonify({"error": BUSY_MESSAGE}), 503
    if job is None:
        print_event("slice_queued", pid=pid, team=team, job=job_id[:8])
    else:
        print_event("slice_queued", pid=pid, team=team, job=job_id[:8], copies=len(job.copies),
                    triangles=job.triangles(), filament=job.filament, process=job.process)
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
    """The print page's first call: one `page` event describing how it was opened, and one
    `config` event with the printing configuration it resolved. Answers that configuration,
    the override choices, the plate (the configured printer's, with the margin and spacing)
    and the limits the browser checks. 10 per minute, added in init_print_routes."""
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
    choices = _print_choices(onshape=body.get("source") == "onshape")
    print_event("config", pid=_page_id(), team=team, printer=choices.printer, filaments=choices.filaments,
                processes=choices.processes, skipped=choices.skipped, error=choices.error)
    return jsonify({"plate": plate_from_printer(choices.printer_profile()),
                    "limits": limits.limits_for_browser(),
                    "config": choices.to_json(), "overrides": OVERRIDE_CHOICES}), 200


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


def _export_one(client, part, pid, answer, replaces=None):
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
        entry = ctx.part_store.add(pid, part.name, source.to_json(), source.key(), result.stl, info,
                                   replaces=replaces)
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
    replaced = {}       # a refresh: source key -> the ref it replaces
    if via == "refresh":
        for ref in body.get("replace", []):
            entry = ctx.part_store.get(ref, pid)
            if entry is not None:
                replaced[entry.source_key] = ref
        new_keys = {part.source.key() for part in resolved} - set(replaced)
    else:
        # A re-click of a listed part costs nothing: only parts the store does not hold
        # count against the page's limit, checked after resolving and before any export.
        new_keys = {part.source.key() for part in resolved
                    if ctx.part_store.find_source(pid, part.source.key()) is None}
    try:
        if new_keys:
            ctx.part_store.check_room(pid, len(new_keys))
    except PartLimit as exc:
        answer.error(exc.message)
        return answer.json()
    for part in resolved:
        existing = None if via == "refresh" else ctx.part_store.find_source(pid, part.source.key())
        if existing is not None:
            if existing not in answer.parts:
                answer.parts.append(existing)
            continue
        try:
            _export_one(client, part, pid, answer, replaces=replaced.get(part.source.key()))
        except OnshapeLimitReached:
            # The allowance is spent: no later part can be exported either.
            answer.failed(part, 402, "limit_reached", LIMIT_REACHED_MESSAGE)
            break
        except OnshapeAuthError:
            return _signin_expired()
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

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

from flask import Blueprint, Response, jsonify, make_response, redirect, render_template, request, send_file

from print_jobs import JobFailure, JobPool, QueueFull
from print_slicer import SAMPLE_STL, SliceError, part_info, slice_stl

DELIVERED_NAME = "sample_part.gcode.3mf"
BUSY_MESSAGE = "The slicer is busy, try again in a minute."
EXPIRED_MESSAGE = "This slice has expired, go back and slice again."
FRAME_ANCESTORS = "frame-ancestors https://*.onshape.com"

print_bp = Blueprint("print", __name__)

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


def make_worker(token_manager, upload_folder, output_folder, metrics, log):
    """Worker for JobPool: slice the sample part, deliver the archive, clean up."""

    def run(job_id):
        started = time.monotonic()
        scratch = tempfile.mkdtemp(prefix=f"print_{job_id[:8]}_", dir=upload_folder)
        outcome = "failed"
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
            log(f"🖨️ Print job {job_id[:8]} sliced: {DELIVERED_NAME}")
            outcome = "done"
            return {"token": token, "summary": result.summary(), "part": part_info()}
        except SliceError as exc:
            raise JobFailure(exc.message, exc.details) from exc
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
            metrics.log_event("print_job", metadata={"outcome": outcome,
                                                      "duration_s": round(time.monotonic() - started, 3)})

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
    gate = ctx.require_session()
    if gate:
        return gate
    try:
        job_id = ctx.pool.submit()
    except QueueFull:
        return jsonify({"error": BUSY_MESSAGE}), 503
    ctx.log(f"🖨️ Print job {job_id[:8]} queued")
    return jsonify({"job_id": job_id}), 202


@print_bp.route("/print-job/<job_id>")
def print_job_status(job_id):
    gate = ctx.require_session()
    if gate:
        return gate
    record = ctx.pool.get(job_id)
    if record is None:
        return jsonify({"error": EXPIRED_MESSAGE}), 404
    return jsonify(record)


@print_bp.route("/print-job/<job_id>/events")
def print_job_events(job_id):
    gate = ctx.require_session()
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
    if pool is None:
        pool = JobPool(make_worker(token_manager, upload_folder, output_folder, metrics, log), log=log)
        pool.start_sweeper()
    ctx.pool = pool
    # flask-limiter evaluates a decorated limit only through the wrapper its
    # decorator returns, so that wrapper — not the bare view — has to be the
    # registered view function. The limiter does not exist when this module is
    # imported, so the rule is added here rather than with a decorator at
    # definition time.
    print_bp.add_url_rule("/print-job", "print_job_submit",
                          limiter.limit("3 per minute")(print_job_submit), methods=["POST"])
    app.register_blueprint(print_bp)
    return pool

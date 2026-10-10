"""Print events: one log line per event, and a metrics row for all but `request`.

Flask-free. The line format is

    [PRINT] <event> pid=<page id|-> team=<n|-> job=<id8|-> key=value ...

so Railway's log search can filter on `[PRINT]` and then on one `pid=`. Values follow
one quoting rule (quote_value), so no value can end a line or forge a `[PRINT]` line.
Tokens, cookies, keys, download tokens, full job ids, pairing codes and Onshape
identities are never passed here; the job guard refuses anything longer than eight
characters. See docs/3D_PRINTING.md, "Support: reading the print logs".
"""
import json
import re

EVENT_NAMES = frozenset({"page", "config", "select", "export", "export_failed", "layout",
                         "slice_queued", "slice_done", "slice_failed", "deliver", "client_error",
                         "request"})
# Always used with fullmatch: `$` with match would also accept a trailing newline,
# which is exactly the character that must never reach a log line unquoted.
PAGE_ID_RE = re.compile(r"[0-9a-f]{12}")
_JOB_RE = re.compile(r"[A-Za-z0-9_-]{1,8}")
_BARE_RE = re.compile(r"[A-Za-z0-9._:/@+-]+")
_KEY_RE = re.compile(r"[a-z][a-z0-9_]*")

# Set by configure_print_events (init_print_routes); quiet until then.
_log = None
_metrics = None


def valid_page_id(value):
    """The page id if it is 12 lowercase hex characters, else None."""
    if isinstance(value, str) and PAGE_ID_RE.fullmatch(value):
        return value
    return None


def _scalar(value):
    if value is None:
        return "-"
    if isinstance(value, bool):          # before int: bool is an int
        return "yes" if value else "no"
    if isinstance(value, (int, float, str)):
        return str(value)
    raise TypeError(f"print event values are explicit fields, not {type(value).__name__}")


def quote_value(value):
    """A value as written in a print event line.

    None is `-`, booleans are yes/no, numbers are str(). A list or tuple is joined
    with commas first. The result is written bare when it is made only of letters,
    digits and ._:/@+- and JSON-encoded (ASCII only) otherwise, so "" stays visible.
    """
    if isinstance(value, (list, tuple)):
        text = ",".join(_scalar(item) for item in value)
    elif isinstance(value, str):
        text = value
    else:
        return _scalar(value)
    return text if _BARE_RE.fullmatch(text) else json.dumps(text, ensure_ascii=True)


def format_print_line(event, pid, team, job, fields):
    if event not in EVENT_NAMES:
        raise ValueError(f"unknown print event {event!r}")
    if pid is not None and not valid_page_id(pid):
        raise ValueError("pid is not a page id")
    if job is not None and not (isinstance(job, str) and _JOB_RE.fullmatch(job)):
        raise ValueError("job must be a short job id (at most 8 characters)")
    if team is not None and (isinstance(team, bool) or not isinstance(team, int)):
        raise ValueError("team must be a team number")
    parts = [f"[PRINT] {event}", f"pid={pid or '-'}", f"team={quote_value(team)}", f"job={job or '-'}"]
    for key, value in fields.items():
        if not _KEY_RE.fullmatch(key):
            raise ValueError(f"bad print event field name {key!r}")
        parts.append(f"{key}={quote_value(value)}")
    return " ".join(parts)


def configure_print_events(log, metrics):
    """Route print events to the app's log() and its metrics module."""
    global _log, _metrics
    _log = log
    _metrics = metrics


def print_event(event, *, pid=None, team=None, job=None, **fields):
    """Write one print event line; every event but `request` also gets a metrics row."""
    line = format_print_line(event, pid, team, job, fields)
    if _log is not None:
        _log(line)
    if event != "request" and _metrics is not None:
        _metrics.log_event("print_" + event, team_number=team,
                           metadata={"pid": pid, "job": job, **fields})
    return line

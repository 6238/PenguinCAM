"""In-process job pool for print slices. No Flask, no app imports.

A job table in process memory plus a thread pool. The pool size is the number
of Orca processes allowed at once; the queue beyond it is deliberately small,
because queued work nobody will wait for is worse than an honest refusal.

Sizing basis (2026-09-13): one slice of print/sample_part.stl peaked at 93 MB
resident and took 0.23 s wall clock on an aarch64 Linux machine.

Plates at the print path's limits (print/limits.py), sliced by
print/scripts/measure_plate.py on the same kind of machine (2026-10-10). The part
is a 40 x 20 mm hub with a bore and 3 mm fillets, revolved into near-square
facets, as a finely tessellated exported part is; each plate ran twice and the
worse peak and the worse time of the two runs are given:
- 30 copies of a 9,882-triangle hub (296,460 triangles): 886 MB peak
  (907,524 KB), 16.2 s. Copies, not triangles, drive this one: it sits just
  inside the 900 MB stop rule (MEASURE_MAX_PEAK_KB), 1.5% under it, so
  MAX_COPIES was lowered to 25 for memory headroom.
- 25 copies of an 11,968-triangle hub (299,200 triangles), the plate at the
  lowered cap: 779 MB peak (797,408 KB), 14.1 s.
- 2 copies of a 149,760-triangle hub (299,520 triangles): 214 MB peak
  (219,136 KB), 3.2 s.
- 3 copies of 124,830 (374,490 triangles): 233 MB (238,280 KB), 4.2 s.
An earlier run with sliver-faceted cylinders (37,500-segment cap fans) peaked
at 1,113 MB for two 150,000-triangle copies; that came from the slivers, not the
triangle count, so the per-part cap stays at 150,000.

The production Railway plan's vCPU and memory allotment has not been measured
yet, so MAX_RUNNING stays at its default of one until it is measured on the
production plan. The rule when revisiting:
min(vCPU count, memory / peak resident memory of one slice), never below one.
See docs/DEPLOYMENT_GUIDE.md.
"""
import secrets
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

MAX_RUNNING = 1          # Orca processes at once
MAX_QUEUED = 2           # jobs waiting beyond the running ones
RECORD_TTL_S = 3600      # a record expires this long after its last update
WAIT_S = 5.0             # subscriber wake-up cadence when nothing changes
SWEEP_INTERVAL_S = 600   # same cadence as the file token manager

TERMINAL = ("done", "failed")
GENERIC_FAILURE = "The slicer failed unexpectedly."


class QueueFull(Exception):
    """The pool is running MAX_RUNNING jobs and MAX_QUEUED more are waiting."""


class JobFailure(Exception):
    """A worker failure with a student-facing message and log-only details."""

    def __init__(self, message, details=""):
        super().__init__(message)
        self.message = message
        self.details = details


class JobPool:
    def __init__(self, worker, *, executor=None, max_running=MAX_RUNNING, max_queued=MAX_QUEUED,
                 ttl_s=RECORD_TTL_S, clock=time.time, log=None):
        self._worker = worker
        self._executor = executor or ThreadPoolExecutor(max_workers=max_running, thread_name_prefix="print-slice")
        self._max_queued = max_queued
        self._ttl_s = ttl_s
        self._clock = clock
        self._log = log or (lambda *args: None)
        self._lock = threading.Lock()
        self._jobs = {}        # id -> record dict
        self._conds = {}       # id -> threading.Condition (shares self._lock)

    # -- public ---------------------------------------------------------

    def submit(self):
        now = self._clock()
        job_id = secrets.token_urlsafe(32)
        with self._lock:
            if sum(1 for r in self._jobs.values() if r["state"] == "queued") >= self._max_queued:
                raise QueueFull()
            self._jobs[job_id] = {"id": job_id, "state": "queued", "created": now, "updated": now,
                                  "token": None, "summary": None, "part": None, "error": None}
            self._conds[job_id] = threading.Condition(self._lock)
        self._executor.submit(self._run, job_id)
        return job_id

    def get(self, job_id):
        with self._lock:
            return self._snapshot(job_id)

    def wait(self, job_id, since_updated, timeout=WAIT_S):
        with self._lock:
            cond = self._conds.get(job_id)
            if cond is None:
                return None
            record = self._jobs[job_id]
            if record["updated"] <= since_updated:
                cond.wait(timeout)
            return self._snapshot(job_id)

    def follow(self, job_id, wait_s=WAIT_S):
        record = self.get(job_id)
        if record is None:
            return
        yield ("state", record)
        while record["state"] not in TERMINAL:
            record = self.wait(job_id, record["updated"], timeout=wait_s)
            if record is None:
                return
            if record["state"] not in TERMINAL:
                yield ("state", record)
        yield (record["state"], record)

    def expire(self, now=None):
        now = self._clock() if now is None else now
        with self._lock:
            dead = [job_id for job_id, r in self._jobs.items() if r["updated"] + self._ttl_s <= now]
            for job_id in dead:
                cond = self._conds.pop(job_id)
                del self._jobs[job_id]
                cond.notify_all()
        return len(dead)

    def start_sweeper(self, interval_s=SWEEP_INTERVAL_S):
        def loop():
            while True:
                time.sleep(interval_s)
                try:
                    self.expire()
                except Exception:   # never let the sweeper die
                    self._log(traceback.format_exc())
        thread = threading.Thread(target=loop, name="print-jobs-sweeper", daemon=True)
        thread.start()
        return thread

    def counts(self):
        with self._lock:
            states = [r["state"] for r in self._jobs.values()]
        return {"queued": states.count("queued"), "running": states.count("running")}

    # -- internals ------------------------------------------------------

    def _snapshot(self, job_id):
        record = self._jobs.get(job_id)
        if record is None:
            return None
        copy = dict(record)
        copy["elapsed_s"] = round(self._clock() - record["created"], 3)
        return copy

    def _update(self, job_id, **fields):
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None:      # expired while running
                return
            record.update(fields)
            record["updated"] = self._clock()
            self._conds[job_id].notify_all()

    def _run(self, job_id):
        self._update(job_id, state="running")
        try:
            result = self._worker(job_id)
            fields = {"token": result.get("token"), "summary": result.get("summary"),
                      "part": result.get("part")}
        except JobFailure as exc:
            self._log(f"print job {job_id[:8]} failed: {exc.message}\n{exc.details}")
            self._update(job_id, state="failed", error=exc.message)
        except Exception:
            self._log(f"print job {job_id[:8]} crashed:\n{traceback.format_exc()}")
            self._update(job_id, state="failed", error=GENERIC_FAILURE)
        else:
            self._update(job_id, state="done", **fields)

"""Tests for print_jobs.JobPool with synchronous stand-ins for the executor."""
import sys
import threading
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import print_jobs  # noqa: E402
from print_jobs import JobFailure, JobPool, QueueFull  # noqa: E402


class ImmediateExecutor:
    """Runs the submitted function on the calling thread, like a pool with a free worker."""
    def submit(self, fn, *args):
        fn(*args)


class NeverExecutor:
    """Accepts work and never runs it: every job stays queued."""
    def __init__(self):
        self.pending = []

    def submit(self, fn, *args):
        self.pending.append((fn, args))

    def run_next(self):
        fn, args = self.pending.pop(0)
        fn(*args)


class FakeClock:
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now


def good_worker(job_id):
    return {"token": "tok-" + job_id[:4], "summary": {"layer_count": 3}, "part": {"name": "sample_part"}}


class SubmitAndRunTest(unittest.TestCase):
    def test_submit_returns_long_id_and_runs_worker_to_done(self):
        pool = JobPool(good_worker, executor=ImmediateExecutor())
        job_id = pool.submit()
        self.assertGreaterEqual(len(job_id), 32)
        rec = pool.get(job_id)
        self.assertEqual(rec["state"], "done")
        self.assertEqual(rec["token"], "tok-" + job_id[:4])
        self.assertEqual(rec["summary"], {"layer_count": 3})
        self.assertEqual(rec["part"], {"name": "sample_part"})
        self.assertIsNone(rec["error"])

    def test_states_go_queued_running_done_in_order(self):
        seen = []
        holder = {}

        def worker(job_id):
            seen.append(holder["pool"].get(job_id)["state"])
            return good_worker(job_id)

        executor = NeverExecutor()
        pool = JobPool(worker, executor=executor)
        holder["pool"] = pool
        job_id = pool.submit()
        seen.append(pool.get(job_id)["state"])
        executor.run_next()
        seen.append(pool.get(job_id)["state"])
        self.assertEqual(seen, ["queued", "running", "done"])

    def test_unknown_id_is_none(self):
        pool = JobPool(good_worker, executor=ImmediateExecutor())
        self.assertIsNone(pool.get("nope"))
        self.assertIsNone(pool.wait("nope", 0, timeout=0.01))

    def test_get_returns_a_copy_with_elapsed(self):
        clock = FakeClock()
        pool = JobPool(good_worker, executor=NeverExecutor(), clock=clock)
        job_id = pool.submit()
        clock.now += 12.5
        rec = pool.get(job_id)
        self.assertEqual(rec["elapsed_s"], 12.5)
        rec["state"] = "hacked"
        self.assertEqual(pool.get(job_id)["state"], "queued")


class FailureTest(unittest.TestCase):
    def test_job_failure_ends_failed_with_message_only(self):
        def worker(job_id):
            raise JobFailure("The slicer could not process this part.", "exit code 255\nsecret details")
        pool = JobPool(worker, executor=ImmediateExecutor())
        rec = pool.get(pool.submit())
        self.assertEqual(rec["state"], "failed")
        self.assertEqual(rec["error"], "The slicer could not process this part.")
        self.assertNotIn("secret details", str(rec))

    def test_unexpected_exception_ends_failed_with_generic_message(self):
        def worker(job_id):
            raise RuntimeError("boom with a path /tmp/x")
        pool = JobPool(worker, executor=ImmediateExecutor())
        rec = pool.get(pool.submit())
        self.assertEqual(rec["state"], "failed")
        self.assertEqual(rec["error"], "The slicer failed unexpectedly.")
        self.assertNotIn("/tmp/x", str(rec))


class QueueCapTest(unittest.TestCase):
    def test_third_waiting_job_is_refused(self):
        executor = NeverExecutor()
        pool = JobPool(good_worker, executor=executor, max_queued=2)
        pool.submit()
        pool.submit()
        self.assertEqual(pool.counts(), {"queued": 2, "running": 0})
        with self.assertRaises(QueueFull):
            pool.submit()
        executor.run_next()
        pool.submit()   # room again after one finished

    def test_running_jobs_do_not_count_as_queued(self):
        started = threading.Event()
        release = threading.Event()

        def slow_worker(job_id):
            started.set()
            release.wait(5)
            return good_worker(job_id)

        pool = JobPool(slow_worker, max_running=1, max_queued=1)   # real thread pool
        first = pool.submit()
        self.assertTrue(started.wait(2))
        self.assertEqual(pool.get(first)["state"], "running")
        pool.submit()   # one queued behind the running one
        with self.assertRaises(QueueFull):
            pool.submit()
        release.set()
        deadline = time.time() + 5
        while pool.counts()["queued"] or pool.counts()["running"]:
            self.assertLess(time.time(), deadline)
            time.sleep(0.02)


class SubscriberTest(unittest.TestCase):
    def test_wait_returns_early_on_state_change(self):
        executor = NeverExecutor()
        pool = JobPool(good_worker, executor=executor)
        job_id = pool.submit()
        since = pool.get(job_id)["updated"]
        threading.Timer(0.05, executor.run_next).start()
        started = time.monotonic()
        rec = pool.wait(job_id, since, timeout=5)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(rec["state"], "done")

    def test_wait_wakes_on_timeout_with_unchanged_state(self):
        pool = JobPool(good_worker, executor=NeverExecutor())
        job_id = pool.submit()
        since = pool.get(job_id)["updated"]
        started = time.monotonic()
        rec = pool.wait(job_id, since, timeout=0.05)
        self.assertGreaterEqual(time.monotonic() - started, 0.04)
        self.assertEqual(rec["state"], "queued")

    def test_follow_yields_state_then_terminal(self):
        pool = JobPool(good_worker, executor=ImmediateExecutor())
        job_id = pool.submit()
        events = list(pool.follow(job_id, wait_s=0.01))
        self.assertEqual([e for e, _ in events], ["state", "done"])
        self.assertEqual(events[-1][1]["token"], "tok-" + job_id[:4])

    def test_follow_repeats_state_on_cadence_until_change(self):
        executor = NeverExecutor()
        pool = JobPool(good_worker, executor=executor)
        job_id = pool.submit()
        gen = pool.follow(job_id, wait_s=0.02)
        first = next(gen)
        second = next(gen)
        self.assertEqual((first[0], first[1]["state"]), ("state", "queued"))
        self.assertEqual((second[0], second[1]["state"]), ("state", "queued"))
        executor.run_next()
        rest = list(gen)
        self.assertEqual(rest[-1][0], "done")
        self.assertTrue(all(e == "state" for e, _ in rest[:-1]))

    def test_follow_failed_job_ends_with_failed(self):
        def worker(job_id):
            raise JobFailure("The slicer could not process this part.", "x")
        pool = JobPool(worker, executor=ImmediateExecutor())
        events = list(pool.follow(pool.submit(), wait_s=0.01))
        self.assertEqual([e for e, _ in events], ["state", "failed"])
        self.assertEqual(events[-1][1]["error"], "The slicer could not process this part.")

    def test_follow_unknown_id_yields_nothing(self):
        pool = JobPool(good_worker, executor=ImmediateExecutor())
        self.assertEqual(list(pool.follow("nope", wait_s=0.01)), [])


class ExpiryTest(unittest.TestCase):
    def test_records_expire_one_hour_after_last_update_even_if_unread(self):
        clock = FakeClock()
        pool = JobPool(good_worker, executor=ImmediateExecutor(), clock=clock, ttl_s=3600)
        old = pool.submit()
        clock.now += 1800
        young = pool.submit()
        clock.now += 1801
        self.assertEqual(pool.expire(), 1)
        self.assertIsNone(pool.get(old))
        self.assertIsNotNone(pool.get(young))

    def test_follow_stops_when_record_expires(self):
        clock = FakeClock()
        pool = JobPool(good_worker, executor=NeverExecutor(), clock=clock, ttl_s=10)
        job_id = pool.submit()
        gen = pool.follow(job_id, wait_s=0.01)
        next(gen)
        clock.now += 11
        pool.expire()
        self.assertEqual(list(gen), [])

    def test_constants_are_the_documented_defaults(self):
        self.assertEqual(print_jobs.MAX_RUNNING, 1)
        self.assertEqual(print_jobs.MAX_QUEUED, 2)
        self.assertEqual(print_jobs.RECORD_TTL_S, 3600)
        self.assertEqual(print_jobs.WAIT_S, 5.0)
        self.assertEqual(print_jobs.SWEEP_INTERVAL_S, 600)


if __name__ == "__main__":
    unittest.main()

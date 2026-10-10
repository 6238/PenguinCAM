"""Print events and the page id gate through the Flask test client. No Orca runs."""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[2]        # the repository root
sys.path.insert(0, str(REPO))

from frc_cam_gui_app import app, limiter  # noqa: E402
from print import events  # noqa: E402
from print import routes as print_routes  # noqa: E402
from print.jobs import JobPool  # noqa: E402
from print.slicer import SliceResult  # noqa: E402
from print.tests.test_routes import ImmediateExecutor, good_worker  # noqa: E402

PID = "abcdef012345"


class EventRouteBase(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        self.client = app.test_client()
        with self.client.session_transaction() as sess:
            sess["app_verified"] = True
        limiter.reset()
        limiter.enabled = False
        self._real_pool = print_routes.ctx.pool
        print_routes.ctx.pool = JobPool(good_worker, executor=ImmediateExecutor())
        self.lines = []
        self.metrics = mock.Mock()
        log_patch = mock.patch.object(print_routes.ctx, "log", self.lines.append)
        log_patch.start()
        self.addCleanup(log_patch.stop)
        self.addCleanup(events.configure_print_events, events._log, events._metrics)
        events.configure_print_events(self.lines.append, self.metrics)

    def tearDown(self):
        print_routes.ctx.pool = self._real_pool
        limiter.enabled = True

    def request_lines(self):
        return [line for line in self.lines if line.startswith("[PRINT] request ")]


class PageIdGateTest(EventRouteBase):
    def test_print_api_without_page_id_is_400(self):
        for method, url in (("get", "/print-job/x"), ("post", "/print-job"), ("get", "/print-job/x/events")):
            with self.subTest(url=url):
                resp = getattr(self.client, method)(url)
                self.assertEqual(resp.status_code, 400)
                self.assertEqual(resp.get_json()["error"], print_routes.PAGE_ID_MESSAGE)
        resp = self.client.post("/print-job", headers={"X-Print-Page": "ABCDEF012345"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"], "This page is out of date; reload it.")

    def test_session_gate_comes_first(self):
        resp = app.test_client().post("/print-job")
        self.assertEqual(resp.status_code, 401)

    def test_get_accepts_pid_query(self):
        resp = self.client.get(f"/print-job/nope?pid={PID}")
        self.assertEqual(resp.status_code, 404)
        # A POST does not take the query parameter: the header is required.
        resp = self.client.post(f"/print-job?pid={PID}")
        self.assertEqual(resp.status_code, 400)

    def test_print_page_needs_no_page_id(self):
        self.assertEqual(self.client.get("/print").status_code, 200)

    def test_sample_part_route_stays_ungated(self):
        bare = app.test_client()
        self.assertEqual(bare.get("/print/part").status_code, 200)
        self.assertEqual(bare.get("/print/part?stl=1").status_code, 200)


class RequestLineTest(EventRouteBase):
    def test_request_line_logs_rule_not_path(self):
        job_id = self.client.post("/print-job", headers={"X-Print-Page": PID}).get_json()["job_id"]
        resp = self.client.get(f"/print-job/{job_id}?pid={PID}")
        self.assertEqual(resp.status_code, 200)
        lines = self.request_lines()
        self.assertEqual(len(lines), 2)
        self.assertIn('rule="/print-job/<job_id>"', lines[1])
        self.assertIn(f"pid={PID}", lines[1])
        self.assertIn("method=GET", lines[1])
        self.assertIn("status=200", lines[1])
        self.assertRegex(lines[1], r" ms=\d+$")
        self.assertTrue(all(job_id not in line for line in self.lines))
        # request lines write no metrics row
        names = [c.args[0] for c in self.metrics.log_event.call_args_list]
        self.assertNotIn("print_request", names)

    def test_rejected_request_is_logged_with_pid_dash(self):
        self.client.post("/print-job")
        self.assertIn("[PRINT] request pid=- ", self.request_lines()[0])
        self.assertIn("status=400", self.request_lines()[0])

    def test_static_files_write_no_request_line(self):
        self.assertEqual(self.client.get("/print/static/print_wizard.js").status_code, 200)
        self.assertEqual(self.request_lines(), [])

    def test_team_dash_on_default_config(self):
        with self.client.session_transaction() as sess:
            sess["using_default_config"] = True
            sess["team_number"] = 6238
        self.client.get("/print")
        self.assertIn(" team=- ", self.request_lines()[-1])
        with self.client.session_transaction() as sess:
            sess["using_default_config"] = False
            sess["team_number"] = 1234
        self.client.get("/print")
        self.assertIn(" team=1234 ", self.request_lines()[-1])


class SliceEventsTest(EventRouteBase):
    def setUp(self):
        super().setUp()
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.upload, self.output = tmp / "uploads", tmp / "outputs"
        self.upload.mkdir()
        self.output.mkdir()
        tokens = mock.Mock()
        tokens.register_file.return_value = "tok123"
        worker = print_routes.make_worker(tokens, str(self.upload), str(self.output), self.lines.append)
        print_routes.ctx.pool = JobPool(worker, executor=ImmediateExecutor())

    def metric_names(self):
        return [c.args[0] for c in self.metrics.log_event.call_args_list]

    def test_slice_events_replace_print_job_metric(self):
        def fake_slice(stl_path, output_dir, timeout_s=120):
            archive = Path(output_dir) / "sample_part.gcode.3mf"
            archive.write_bytes(b"PK")
            return SliceResult(output_path=archive, print_time_s=1.0, filament_m=0.1, filament_g=0.2,
                               layer_count=3, warnings=[])

        with mock.patch.object(print_routes, "slice_stl", fake_slice):
            resp = self.client.post("/print-job", headers={"X-Print-Page": PID})
        self.assertEqual(resp.status_code, 202)
        job_id = resp.get_json()["job_id"]
        names = self.metric_names()
        self.assertIn("print_slice_queued", names)
        self.assertIn("print_slice_done", names)
        self.assertNotIn("print_job", names)
        queued = next(c for c in self.metrics.log_event.call_args_list if c.args[0] == "print_slice_queued")
        self.assertEqual(queued.kwargs["metadata"]["pid"], PID)
        self.assertEqual(queued.kwargs["metadata"]["job"], job_id[:8])
        done = [line for line in self.lines if line.startswith("[PRINT] slice_done ")]
        self.assertEqual(len(done), 1)
        self.assertIn(f"job={job_id[:8]}", done[0])
        self.assertIn(" code=0", done[0])
        self.assertRegex(done[0], r" seconds=[0-9.]+")
        self.assertTrue(all(job_id not in line for line in self.lines))

    def test_slice_failure_logs_code_and_reason(self):
        from print.slicer import SliceError

        def fake_slice(stl_path, output_dir, timeout_s=120):
            raise SliceError("The slicer could not process this part.", "exit code -101\nboom", code=-101)

        with mock.patch.object(print_routes, "slice_stl", fake_slice):
            job_id = self.client.post("/print-job", headers={"X-Print-Page": PID}).get_json()["job_id"]
        failed = [line for line in self.lines if line.startswith("[PRINT] slice_failed ")]
        self.assertEqual(len(failed), 1)
        self.assertIn(f"job={job_id[:8]}", failed[0])
        self.assertIn(" code=-101 ", failed[0])
        self.assertIn('reason="The slicer could not process this part."', failed[0])
        self.assertIn("print_slice_failed", self.metric_names())


if __name__ == "__main__":
    unittest.main()

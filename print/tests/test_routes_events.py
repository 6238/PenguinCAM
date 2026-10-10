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


class BrowserEventRoutesTest(EventRouteBase):
    def post(self, url, body, pid=PID):
        headers = {"X-Print-Page": pid} if pid else {}
        return self.client.post(url, json=body, headers=headers)

    def lines_for(self, event):
        return [line for line in self.lines if line.startswith(f"[PRINT] {event} ")]

    def test_browser_routes_are_gated(self):
        for url in ("/print/page", "/print/client-event", "/print/deliver"):
            with self.subTest(url=url):
                self.assertEqual(app.test_client().post(url, json={}).status_code, 401)
                resp = self.post(url, {}, pid=None)
                self.assertEqual(resp.status_code, 400)
                self.assertEqual(resp.get_json()["error"], print_routes.PAGE_ID_MESSAGE)

    def test_client_event_logs_quoted_message(self):
        resp = self.post("/print/client-event", {"step": "preview", "message": "Bad\nthing", "code": "network"})
        self.assertEqual(resp.status_code, 204)
        lines = self.lines_for("client_error")
        self.assertEqual(len(lines), 1)
        self.assertNotIn("\n", lines[0])
        self.assertEqual(lines[0], f'[PRINT] client_error pid={PID} team=- job=- step=preview '
                                   f'message="Bad\\nthing" code=network')
        self.metrics.log_event.assert_any_call("print_client_error", team_number=None, metadata=mock.ANY)

    def test_trailing_newlines_stay_inside_one_line(self):
        resp = self.client.get(f"/print-job/nope?pid={PID}%0A")
        self.assertEqual(resp.status_code, 400)            # not a page id
        self.post("/print/client-event", {"step": "s\n", "message": "m\n", "code": "c"})
        self.assertEqual(self.lines_for("client_error"),
                         [f'[PRINT] client_error pid={PID} team=- job=- step="s\\n" message="m\\n" code=c'])
        resp = self.post("/print/deliver", {"job_id": "abcdefg\n", "action": "drive", "outcome": "x\n"})
        self.assertEqual(resp.status_code, 400)            # a job value with a newline is refused
        resp = self.post("/print/deliver", {"job_id": "abcdefgh\n", "action": "drive", "outcome": "x\n"})
        self.assertEqual(resp.status_code, 204)            # cut to eight characters, outcome quoted
        self.assertEqual(self.lines_for("deliver"),
                         [f'[PRINT] deliver pid={PID} team=- job=abcdefgh action=drive outcome="x\\n"'])
        for line in self.lines:
            self.assertNotIn("\n", line)
        self.assertIn("[PRINT] request pid=- ", self.request_lines()[0])

    def test_client_event_rejects_extra_keys(self):
        resp = self.post("/print/client-event", {"step": "preview", "message": "x", "token": "secret"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.lines_for("client_error"), [])
        self.assertTrue(all("secret" not in line for line in self.lines))
        self.assertEqual(self.post("/print/client-event", ["not", "an", "object"]).status_code, 400)

    def test_client_event_cuts_to_300(self):
        self.post("/print/client-event", {"step": "s" * 400, "message": "m" * 400, "code": "c" * 400})
        line = self.lines_for("client_error")[0]
        self.assertIn(" step=" + "s" * 300 + " ", line)
        self.assertIn(" message=" + "m" * 300 + " ", line)
        self.assertTrue(line.endswith(" code=" + "c" * 300))

    def test_page_event_fields(self):
        with self.client.session_transaction() as sess:
            sess["team_number"] = 1234
            sess["using_default_config"] = False
            sess["team_config_data"] = {"team": {"number": 1234}, "printing": {"pairing_code": "x"}}
        resp = self.post("/print/page", {"source": "onshape", "theme": "dark", "has_onshape_context": True,
                                         "wvm": "w", "server": "https://cad.onshape.com"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {})
        self.assertEqual(self.lines_for("page"), [
            f"[PRINT] page pid={PID} team=1234 job=- source=onshape theme=dark has_onshape_context=yes "
            "wvm=w server=https://cad.onshape.com configured=yes config=team"])
        # Upload mode in the same browser: the Onshape sign-in's team config is never read.
        self.post("/print/page", {"source": "upload", "theme": "dark", "has_onshape_context": False})
        self.assertEqual(self.lines_for("page")[1],
                         f"[PRINT] page pid={PID} team=- job=- source=upload theme=dark has_onshape_context=no "
                         "wvm=- server=- configured=no config=default")
        with self.client.session_transaction() as sess:
            sess["using_default_config"] = True
            sess["team_number"] = 6238
            sess.pop("team_config_data")
        self.post("/print/page", {"source": "onshape", "theme": {"x": 1}, "has_onshape_context": False})
        self.assertEqual(self.lines_for("page")[2],
                         f"[PRINT] page pid={PID} team=- job=- source=onshape theme=- has_onshape_context=no "
                         "wvm=- server=- configured=no config=default")

    def test_deliver_logs_short_job_id(self):
        job_id = "AbCdEfGh" + "x" * 35
        resp = self.post("/print/deliver", {"job_id": job_id, "action": "drive", "outcome": "ok" + "!" * 60})
        self.assertEqual(resp.status_code, 204)
        line = self.lines_for("deliver")[0]
        self.assertTrue(line.startswith(f"[PRINT] deliver pid={PID} team=- job=AbCdEfGh action=drive outcome="))
        self.assertIn('outcome="ok' + "!" * 38 + '"', line)
        self.assertTrue(all(job_id not in entry for entry in self.lines))
        self.assertEqual(self.post("/print/deliver", {"job_id": job_id, "action": "email", "outcome": "ok"}).status_code, 400)
        self.assertEqual(self.post("/print/deliver", {"job_id": "a b", "action": "drive", "outcome": "ok"}).status_code, 400)


if __name__ == "__main__":
    unittest.main()

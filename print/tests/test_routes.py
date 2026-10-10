"""Route tests for the print blueprint through the Flask test client. No Orca runs."""
import os
import shutil
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[2]        # the repository root
sys.path.insert(0, str(REPO))

from frc_cam_gui_app import app, limiter, file_token_manager  # noqa: E402
from print import events as events_module  # noqa: E402
from print import routes as print_routes  # noqa: E402
from print.jobs import JobFailure, JobPool, QueueFull  # noqa: E402


PAGE_ID = "abcdef012345"


def with_page_id(client):
    """Every print API call from this client sends a page id, as the print page does."""
    client.environ_base["HTTP_X_PRINT_PAGE"] = PAGE_ID
    return client


class ImmediateExecutor:
    def submit(self, fn, *args):
        fn(*args)


def good_worker(job_id, payload):
    return {"token": "tok-" + job_id[:4], "summary": {"print_time_s": 790.0, "filament_g": 3.31,
                                                      "filament_m": 1.09, "layer_count": 60, "warnings": []},
            "part": {"name": "sample_part"}}


class PrintRouteBase(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        self.client = with_page_id(app.test_client())
        with self.client.session_transaction() as sess:
            sess["app_verified"] = True
        limiter.reset()
        limiter.enabled = False
        self._real_pool = print_routes.ctx.pool
        print_routes.ctx.pool = JobPool(good_worker, executor=ImmediateExecutor())

    def tearDown(self):
        print_routes.ctx.pool = self._real_pool
        limiter.enabled = True


class PrintPageTest(PrintRouteBase):
    def test_page_renders_with_source_theme_and_return(self):
        resp = self.client.get("/print?source=upload&theme=light&return=/app%3Fx%3D1")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn('data-source="upload"', html)
        self.assertIn('data-theme="light"', html)
        self.assertIn("/app?x=1", html)

    def test_defaults_when_parameters_missing(self):
        resp = self.client.get("/print")
        html = resp.get_data(as_text=True)
        self.assertIn('data-source="upload"', html)
        self.assertIn('data-theme="dark"', html)
        self.assertIn('data-return="/app"', html)

    def test_foreign_return_is_replaced(self):
        for bad in ("https://evil.example/x", "//evil.example", "/\\evil", "app", "", "/app\r\nSet-Cookie: x=1"):
            with self.subTest(bad=bad):
                resp = self.client.get("/print", query_string={"return": bad})
                self.assertIn('data-return="/app"', resp.get_data(as_text=True))
        self.assertEqual(print_routes.safe_return_path("/onshape-panel?d=1&theme=dark"), "/onshape-panel?d=1&theme=dark")

    def test_redirects_to_return_with_verify_when_gate_fails(self):
        bare = app.test_client()
        resp = bare.get("/print?return=/app")
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.headers["Location"].endswith("/app?verify=1"))
        resp = bare.get("/print?return=/onshape-panel%3Fd%3D1")
        self.assertTrue(resp.headers["Location"].endswith("/onshape-panel?d=1&verify=1"))
        # A #fragment stays last: verify=1 belongs to the query, not the fragment.
        self.assertEqual(print_routes._with_verify("/app?x=1#frag"), "/app?x=1&verify=1#frag")
        self.assertEqual(print_routes._with_verify("/app#frag"), "/app?verify=1#frag")
        self.assertEqual(print_routes._with_verify("/app?x=1"), "/app?x=1&verify=1")

    def test_onshape_source_sets_frame_ancestors(self):
        resp = self.client.get("/print?source=onshape")
        self.assertEqual(resp.headers.get("Content-Security-Policy"), "frame-ancestors https://*.onshape.com")
        self.assertNotIn("X-Frame-Options", resp.headers)
        resp = self.client.get("/print?source=upload")
        self.assertIsNone(resp.headers.get("Content-Security-Policy"))


class PartRouteTest(PrintRouteBase):
    def test_part_json(self):
        resp = self.client.get("/print/part")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["part"]["size_mm"], {"x": 40.0, "y": 30.0, "z": 12.0})
        self.assertEqual(data["printer"]["bed_mm"], {"x": 340.0, "y": 320.0})
        self.assertIn("name", data["filament"])

    def test_part_stl_bytes(self):
        resp = self.client.get("/print/part?stl=1")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, "model/stl")
        self.assertEqual(resp.data, (REPO / "print" / "sample_part.stl").read_bytes())


class JobRoutesTest(PrintRouteBase):
    def test_submit_returns_202_with_id(self):
        resp = self.client.post("/print-job")
        self.assertEqual(resp.status_code, 202)
        job_id = resp.get_json()["job_id"]
        self.assertGreaterEqual(len(job_id), 32)

    def test_submit_requires_session(self):
        resp = app.test_client().post("/print-job")
        self.assertEqual(resp.status_code, 401)
        self.assertTrue(resp.get_json()["need_verification"])

    def test_submit_503_when_queue_full(self):
        class FullPool:
            def submit(self, payload=None):
                raise QueueFull()
        print_routes.ctx.pool = FullPool()
        resp = self.client.post("/print-job")
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.get_json()["error"], "The slicer is busy, try again in a minute.")

    def test_job_json_and_404(self):
        job_id = self.client.post("/print-job").get_json()["job_id"]
        resp = self.client.get(f"/print-job/{job_id}")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["state"], "done")
        self.assertEqual(data["token"], "tok-" + job_id[:4])
        self.assertEqual(data["summary"]["layer_count"], 60)
        self.assertEqual(data["part"], {"name": "sample_part"})
        self.assertIsNone(data["error"])
        self.assertIn("elapsed_s", data)
        resp = self.client.get("/print-job/doesnotexist")
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json()["error"], "This slice has expired, go back and slice again.")

    def test_failed_job_carries_message_not_details(self):
        def bad_worker(job_id, payload):
            raise JobFailure("The slicer could not process this part.", "exit code 255 secret")
        print_routes.ctx.pool = JobPool(bad_worker, executor=ImmediateExecutor())
        job_id = self.client.post("/print-job").get_json()["job_id"]
        data = self.client.get(f"/print-job/{job_id}").get_json()
        self.assertEqual(data["state"], "failed")
        self.assertEqual(data["error"], "The slicer could not process this part.")
        self.assertNotIn("secret", str(data))

    def test_events_stream_state_then_done(self):
        job_id = self.client.post("/print-job").get_json()["job_id"]
        resp = self.client.get(f"/print-job/{job_id}/events")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, "text/event-stream")
        self.assertEqual(resp.headers.get("Cache-Control"), "no-cache")
        body = resp.get_data(as_text=True)
        blocks = [b for b in body.split("\n\n") if b.strip()]
        self.assertTrue(blocks[0].startswith("event: state\ndata: "))
        self.assertIn('"elapsed_s"', blocks[0])
        self.assertTrue(blocks[-1].startswith("event: done\ndata: "))
        self.assertIn('"token": "tok-' + job_id[:4], blocks[-1])

    def test_events_404_for_unknown_id(self):
        resp = self.client.get("/print-job/nope/events")
        self.assertEqual(resp.status_code, 404)

    def test_events_require_session(self):
        resp = app.test_client().get("/print-job/nope/events")
        self.assertEqual(resp.status_code, 401)


class RateLimitTest(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        self.client = with_page_id(app.test_client())
        with self.client.session_transaction() as sess:
            sess["app_verified"] = True
        limiter.reset()
        limiter.enabled = True
        self._real_pool = print_routes.ctx.pool
        print_routes.ctx.pool = JobPool(good_worker, executor=ImmediateExecutor())

    def tearDown(self):
        print_routes.ctx.pool = self._real_pool
        limiter.reset()

    def test_fourth_submit_in_a_minute_is_429(self):
        codes = [self.client.post("/print-job").status_code for _ in range(4)]
        self.assertEqual(codes, [202, 202, 202, 429])


class WorkerTest(unittest.TestCase):
    def test_worker_moves_archive_registers_token_and_cleans_scratch(self):
        import tempfile
        from print.slicer import SliceResult
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        upload, output = tmp / "uploads", tmp / "outputs"
        upload.mkdir()
        output.mkdir()
        registered = {}

        class Tokens:
            def register_file(self, path, name):
                registered["path"], registered["name"] = path, name
                return "tok123"

        events = []

        def fake_slice(stl_path, output_dir, timeout_s=120):
            archive = Path(output_dir) / "sample_part.gcode.3mf"
            archive.write_bytes(b"PK")
            return SliceResult(output_path=archive, print_time_s=1.0, filament_m=0.1, filament_g=0.2, layer_count=3, warnings=["w"])

        class Metrics:
            @staticmethod
            def log_event(name, **kw):
                events.append((name, kw))

        lines = []
        self.addCleanup(events_module.configure_print_events, events_module._log, events_module._metrics)
        events_module.configure_print_events(lines.append, Metrics())
        worker = print_routes.make_worker(Tokens(), str(upload), str(output), lines.append)
        with mock.patch.object(print_routes, "slice_stl", fake_slice):
            result = worker("abcdefgh12345678")
        self.assertEqual(result["token"], "tok123")
        self.assertEqual(registered["name"], "sample_part.gcode.3mf")
        self.assertEqual(Path(registered["path"]).parent, output)
        self.assertTrue(Path(registered["path"]).name.endswith(".gcode.3mf"))
        self.assertTrue(Path(registered["path"]).is_file())
        self.assertEqual(result["summary"]["warnings"], ["w"])
        self.assertEqual(result["part"]["part"]["name"], "sample_part")
        self.assertEqual(os.listdir(upload), [])          # scratch removed
        self.assertTrue(lines[0].startswith("[PRINT] slice_done pid=- team=- job=abcdefgh seconds="))
        self.assertEqual(events[0][0], "print_slice_done")
        self.assertEqual(events[0][1]["metadata"]["code"], 0)

    def test_worker_turns_slice_error_into_job_failure_and_cleans_scratch(self):
        import tempfile
        from print.slicer import SliceError
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        upload, output = tmp / "uploads", tmp / "outputs"
        upload.mkdir()
        output.mkdir()
        logged = []

        def fake_slice(stl_path, output_dir, timeout_s=120):
            raise SliceError("The slicer could not process this part.", "exit code 255")

        class Metrics:
            @staticmethod
            def log_event(name, **kw):
                logged.append((name, kw))

        self.addCleanup(events_module.configure_print_events, events_module._log, events_module._metrics)
        events_module.configure_print_events(lambda *a: None, Metrics())
        worker = print_routes.make_worker(mock.Mock(), str(upload), str(output), lambda *a: logged.append(a))
        with mock.patch.object(print_routes, "slice_stl", fake_slice):
            with self.assertRaises(JobFailure) as ctx:
                worker("abcdefgh12345678")
        self.assertEqual(ctx.exception.message, "The slicer could not process this part.")
        self.assertEqual(ctx.exception.details, "exit code 255")
        self.assertEqual(os.listdir(upload), [])
        self.assertEqual([name for name, _kw in logged], ["print_slice_failed"])
        self.assertEqual(logged[0][1]["metadata"]["reason"], "The slicer could not process this part.")


class DownloadMimeTest(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        self.client = app.test_client()
        limiter.reset()
        limiter.enabled = False

    def tearDown(self):
        limiter.enabled = True

    def test_3mf_downloads_as_binary_and_nc_as_text(self):
        import tempfile
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        archive = tmp / "x.gcode.3mf"
        archive.write_bytes(b"PK\x03\x04")
        nc = tmp / "x.nc"
        nc.write_text("G0 X0\n")
        t1 = file_token_manager.register_file(str(archive), "sample_part.gcode.3mf")
        t2 = file_token_manager.register_file(str(nc), "part.nc")
        r1 = self.client.get(f"/download/{t1}")
        self.assertEqual(r1.mimetype, "application/octet-stream")
        self.assertIn("sample_part.gcode.3mf", r1.headers["Content-Disposition"])
        r2 = self.client.get(f"/download/{t2}")
        self.assertEqual(r2.mimetype, "text/plain")


if __name__ == "__main__":
    unittest.main()

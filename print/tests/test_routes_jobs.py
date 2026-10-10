"""POST /print-job with a placement: the server's checks, the plate job it queues, the worker
and Send to Printer. No Orca run: the worker is a recorder, or run_plate_job is replaced."""
import json
import math
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[2]        # the repository root
sys.path.insert(0, str(REPO))

from frc_cam_gui_app import app, file_token_manager, limiter  # noqa: E402
from print import events  # noqa: E402
from print import limits  # noqa: E402
from print import plate_job  # noqa: E402
from print import printer_relay, printer_routes  # noqa: E402
from print import routes as print_routes  # noqa: E402
from print.jobs import JobPool  # noqa: E402
from print.limits import PLATE_SLICE_TIMEOUT_S  # noqa: E402
from print.mesh import orientation_footprints, part_size_mm, read_binary_stl  # noqa: E402
from print.part_store import PartStore  # noqa: E402
from print.plate_job import PlateJob  # noqa: E402
from print.slicer import DEFAULT_PROFILES, SliceResult  # noqa: E402
from print.tests.test_plate_3mf import box_stl, write_archive  # noqa: E402
from print.tests.test_printer_routes import CODE, IDLE, TEAM_YAML_DATA, FakeClock  # noqa: E402
from print.tests.test_routes import ImmediateExecutor  # noqa: E402

PID, OTHER = "abcdef012345", "0123456789ab"
DELIVERED = "Box_plus1-20261009-1432.gcode.3mf"


def store_part(store, pid, name, size, triangles=None):
    stl = box_stl(*size)
    mesh = read_binary_stl(stl, name)
    info = {"triangles": triangles or mesh.triangles, "size_mm": part_size_mm(mesh),
            "footprints": orientation_footprints(mesh)}
    return store.add(pid, name, {"partId": name}, ("d", "e", name), stl, info)


def copy(ref, x, y, orientation="+z", angle=0):
    return {"ref": ref, "orientation": orientation, "angle": angle, "x": x, "y": y}


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, job_id, payload):
        self.calls.append((job_id, payload))
        return {"token": "tok", "summary": {}, "part": {"name": "x"}}


class JobRouteBase(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        self.client = app.test_client()
        self.client.environ_base["HTTP_X_PRINT_PAGE"] = PID
        with self.client.session_transaction() as sess:
            sess["app_verified"] = True
        limiter.reset()
        limiter.enabled = False
        self.addCleanup(setattr, limiter, "enabled", True)
        self.tmp = Path(tempfile.mkdtemp(prefix="routes-jobs-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = PartStore(self.tmp / "print_parts")
        self.box = store_part(self.store, PID, "Box", (20, 20, 10))
        self.peg = store_part(self.store, PID, "Peg", (8, 8, 30))
        self.recorder = Recorder()
        for name, value in (("part_store", self.store),
                            ("pool", JobPool(self.recorder, executor=ImmediateExecutor()))):
            patch = mock.patch.object(print_routes.ctx, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.lines = []
        self.addCleanup(events.configure_print_events, events._log, events._metrics)
        events.configure_print_events(self.lines.append, mock.Mock())

    def valid_copies(self):
        return [copy(self.box.ref, 50, 50), copy(self.box.ref, 100, 50, angle=30), copy(self.peg.ref, 150, 50)]

    def post(self, copies, **extra):
        return self.client.post("/print-job", json={"copies": copies, "local_time": "20261009-1432", **extra})

    def assert_refused(self, resp, message):
        self.assertEqual(resp.status_code, 400, resp.get_json())
        self.assertEqual(resp.get_json()["error"], message)
        self.assertEqual(self.recorder.calls, [])


class PlacementChecksTest(JobRouteBase):
    def test_overlapping_placement_refused_before_orca(self):
        resp = self.post([copy(self.box.ref, 50, 50), copy(self.box.ref, 60, 50)])
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json(), {"error": "Box #1 and Box #2 are closer than 5 mm.", "rule": "too_close"})
        self.assertEqual(self.recorder.calls, [])

    def test_unknown_ref_asks_for_refresh(self):
        self.assert_refused(self.post([copy("A" * 22, 50, 50)]), print_routes.REFRESH_PARTS_MESSAGE)
        self.assertEqual(print_routes.REFRESH_PARTS_MESSAGE,
                         "A part is no longer on the server; press Refresh from Onshape on the Parts step.")

    def test_other_page_ref_refused(self):
        theirs = store_part(self.store, OTHER, "Box", (20, 20, 10))
        self.assert_refused(self.post([copy(self.box.ref, 50, 50), copy(theirs.ref, 100, 50)]),
                            print_routes.REFRESH_PARTS_MESSAGE)

    def test_one_over_max_copies_refused(self):
        # The cap is MAX_COPIES (25 by ruling); one over it is refused, the cap itself is not.
        too_many = [copy(self.peg.ref, 20 + 15 * (n % 20), 20 + 40 * (n // 20)) for n in range(limits.MAX_COPIES + 1)]
        self.assert_refused(self.post(too_many), limits.TOO_MANY_COPIES_MESSAGE)
        self.assertEqual(self.post(too_many[:limits.MAX_COPIES]).status_code, 202)

    def test_job_triangle_cap_refused(self):
        big = store_part(self.store, PID, "Gear", (30, 30, 5), triangles=limits.MAX_PART_TRIANGLES)
        copies = [copy(big.ref, 50, 50), copy(big.ref, 100, 50), copy(big.ref, 150, 50)]
        self.assert_refused(self.post(copies), limits.JOB_TRIANGLES_MESSAGE)
        self.assertEqual(self.post(copies[:2]).status_code, 202)

    def test_bad_copy_fields_400(self):
        good = copy(self.box.ref, 50, 50)
        bad_copies = [dict(good, angle=360), dict(good, angle=-1), dict(good, angle=12.5), dict(good, angle=True),
                      dict(good, orientation="z"), dict(good, ref="../etc"), dict(good, ref=7),
                      {k: v for k, v in good.items() if k != "x"}, dict(good, y="10")]
        for bad in bad_copies:
            with self.subTest(bad=bad):
                self.assert_refused(self.post([bad]), print_routes.BAD_JOB_MESSAGE)
        for body in ({"copies": "nope"}, {"copies": []}, {"copies": [7]}):
            with self.subTest(body=body):
                self.assert_refused(self.client.post("/print-job", json=body), print_routes.BAD_JOB_MESSAGE)
        # NaN and Infinity reach the server only as raw JSON.
        for raw in ("NaN", "Infinity"):
            body = json.dumps({"copies": [good]}).replace('"x": 50', f'"x": {raw}')
            with self.subTest(raw=raw):
                resp = self.client.post("/print-job", data=body, content_type="application/json")
                self.assert_refused(resp, print_routes.BAD_JOB_MESSAGE)


class QueueTest(JobRouteBase):
    def test_valid_plate_queues_with_delivered_name(self):
        resp = self.post(self.valid_copies(), filament="Generic PETG @BBL H2S",
                         process="0.24mm Balanced Quality @BBL H2S 0.6 nozzle", overrides={})
        self.assertEqual(resp.status_code, 202, resp.get_json())
        self.assertEqual(len(self.recorder.calls), 1)
        job_id, payload = self.recorder.calls[0]
        self.assertEqual(resp.get_json()["job_id"], job_id)
        self.assertIsInstance(payload, PlateJob)
        self.assertEqual(payload.delivered_name, DELIVERED)
        self.assertEqual(payload.copies, self.valid_copies())
        self.assertEqual(set(payload.parts), {self.box.ref, self.peg.ref})
        self.assertEqual(payload.parts[self.box.ref].stl_path, self.box.stl_path)
        self.assertEqual(payload.parts[self.box.ref].triangles, 12)
        self.assertEqual(payload.profiles, DEFAULT_PROFILES)
        self.assertEqual(payload.overrides, {})
        self.assertEqual(payload.pid, PID)

    def test_layout_and_slice_queued_events(self):
        resp = self.post(self.valid_copies())
        job8 = resp.get_json()["job_id"][:8]
        layout = [line for line in self.lines if line.startswith("[PRINT] layout ")]
        queued = [line for line in self.lines if line.startswith("[PRINT] slice_queued ")]
        self.assertEqual(len(layout), 1, self.lines)
        # Two 20 x 20 boxes and an 8 x 8 peg on the 340 x 320 plate: 864 / 108800 rounds to 1%.
        self.assertEqual(layout[0], f"[PRINT] layout pid={PID} team=- job=- copies=3 parts=2 fill=1 free_angles=1")
        self.assertEqual(len(queued), 1)
        self.assertTrue(queued[0].startswith(f"[PRINT] slice_queued pid={PID} team=- job={job8} copies=3 "
                                             "triangles=36 filament="), queued[0])
        self.assertIn(" process=", queued[0])

    def test_body_without_copies_keeps_sample_job(self):
        for kwargs in ({}, {"json": {}}, {"json": {"local_time": "20261009-1432"}}):
            with self.subTest(kwargs=kwargs):
                self.recorder.calls.clear()
                resp = self.client.post("/print-job", **kwargs)
                self.assertEqual(resp.status_code, 202)
                self.assertEqual(self.recorder.calls[0][1], None)


def fake_run_plate_job(names, calls):
    def fake(job, scratch, *args, **kwargs):
        calls.append((job, Path(scratch)))
        out = Path(scratch) / "slice"
        out.mkdir(parents=True, exist_ok=True)
        archive = out / "plate.gcode.3mf"
        write_archive(archive, names)
        return SliceResult(output_path=archive, print_time_s=60.0, filament_m=1.0, filament_g=3.0,
                           layer_count=40, warnings=[])
    return fake


class PlateWorkerTest(JobRouteBase):
    def setUp(self):
        super().setUp()
        self.upload, self.output = self.tmp / "uploads", self.tmp / "outputs"
        self.upload.mkdir()
        self.output.mkdir()
        worker = print_routes.make_worker(file_token_manager, str(self.upload), str(self.output), self.lines.append)
        patch = mock.patch.object(print_routes.ctx, "pool", JobPool(worker, executor=ImmediateExecutor()))
        patch.start()
        self.addCleanup(patch.stop)

    def slice_job(self, run=None):
        calls = []
        run = run or fake_run_plate_job(["Box #1", "Box #2", "Peg #1"], calls)
        with mock.patch.object(print_routes, "run_plate_job", run):
            resp = self.post(self.valid_copies())
        self.assertEqual(resp.status_code, 202, resp.get_json())
        record = print_routes.ctx.pool.get(resp.get_json()["job_id"])
        return record, calls

    def test_plate_worker_delivers_under_delivered_name(self):
        record, calls = self.slice_job()
        self.assertEqual(record["state"], "done", record)
        self.assertEqual(record["part"], {"name": DELIVERED, "copies": 3})
        self.assertEqual(record["summary"]["layer_count"], 40)
        info = file_token_manager.get_file(record["token"])
        self.addCleanup(lambda: Path(info["filepath"]).unlink(missing_ok=True))
        self.assertEqual(info["filename"], DELIVERED)
        self.assertEqual(Path(info["filepath"]).name, f"print_{record['id']}.gcode.3mf")
        self.assertEqual(Path(info["filepath"]).parent, self.output)
        self.assertFalse(calls[0][1].exists())                          # the scratch is gone
        done = [line for line in self.lines if line.startswith("[PRINT] slice_done ")]
        self.assertEqual(len(done), 1)
        self.assertIn(f"pid={PID} team=- job={record['id'][:8]} copies=3 triangles=36 seconds=", done[0])

    def test_plate_failure_logs_code_and_reason(self):
        from print.slicer import SliceError

        def fails(job, scratch):
            raise SliceError("Parts are too close together; spread them out.", "exit code 155", -101)

        record, _calls = self.slice_job(run=fails)
        self.assertEqual(record["state"], "failed")
        self.assertEqual(record["error"], "Parts are too close together; spread them out.")
        failed = [line for line in self.lines if line.startswith("[PRINT] slice_failed ")]
        self.assertEqual(len(failed), 1)
        self.assertTrue(failed[0].startswith(f"[PRINT] slice_failed pid={PID} team=- job={record['id'][:8]} "
                                             "seconds="), failed[0])
        self.assertTrue(failed[0].endswith(' code=-101 reason="Parts are too close together; spread them out."'),
                        failed[0])

    def test_plate_job_uses_plate_timeout(self):
        seen = []

        def fake_slice_plate(model_3mf, profiles, output_dir, timeout_s=PLATE_SLICE_TIMEOUT_S):
            seen.append(timeout_s)
            Path(output_dir).mkdir(parents=True, exist_ok=True)
            archive = Path(output_dir) / "plate.gcode.3mf"
            write_archive(archive, ["Box #1", "Box #2", "Peg #1"])
            return SliceResult(output_path=archive, print_time_s=1.0, filament_m=1.0, filament_g=1.0,
                               layer_count=4, warnings=[])

        with mock.patch.object(plate_job, "slice_plate", fake_slice_plate):
            resp = self.post(self.valid_copies())
        record = print_routes.ctx.pool.get(resp.get_json()["job_id"])
        self.assertEqual(record["state"], "done", record)
        self.addCleanup(lambda: Path(file_token_manager.get_file(record["token"])["filepath"]).unlink(missing_ok=True))
        self.assertEqual(seen, [240])

    def test_send_to_printer_carries_delivered_name(self):
        record, _calls = self.slice_job()
        token = record["token"]
        self.addCleanup(lambda: Path(file_token_manager.get_file(token)["filepath"]).unlink(missing_ok=True))
        relay = printer_relay.PrinterRelay(clock=FakeClock())
        for target, name, value in ((printer_relay, "RELAY", relay),
                                    (printer_routes, "_TOKEN_MANAGER", file_token_manager),
                                    (printer_routes, "_HAS_ONSHAPE_SESSION", lambda: True)):
            patch = mock.patch.object(target, name, value, create=True)
            patch.start()
            self.addCleanup(patch.stop)
        relay.note_code(CODE)
        relay.note_daemon(CODE)
        relay.store_status(CODE, IDLE)
        with self.client.session_transaction() as sess:
            sess["team_config_data"] = TEAM_YAML_DATA
            sess["team_number"] = 6238
        resp = self.client.post("/printer/jobs", json={"token": token})
        self.assertEqual(resp.status_code, 202, resp.get_json())
        job = relay.status_for(CODE)["job"]
        self.assertEqual(job["filename"], DELIVERED)
        self.assertEqual(printer_relay.strip_job_suffix(job["filename"]), DELIVERED)
        self.assertEqual(job["display_name"], DELIVERED)


if __name__ == "__main__":
    unittest.main()

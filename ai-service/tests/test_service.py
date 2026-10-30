"""MOCK-BASED unit tests: a fake ONNX session returns hand-made YOLOX output. They prove decoding, filtering,
tracking, confirmation and the HTTP contract, NOT model accuracy (see tools/evaluate.py for that).
Run: python -m unittest discover -s tests"""
import json
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import cv2
import numpy as np

from sentinel_ai.detector import ModelError, YoloxDetector
from sentinel_ai.pipeline import CameraPipeline, fire_area_percent, fire_severity
from sentinel_ai.service import STREAM_TIMEOUT_MS, CameraWorker, Engine, make_handler, mask, sync_cameras
from sentinel_ai.tracker import IouTracker

N = 52 * 52 + 26 * 26 + 13 * 13  # YOLOX anchors for a 416x416 input


class FakeSession:
    def __init__(self, raw_frames):
        self.raw_frames, self.calls = list(raw_frames), 0

    def get_inputs(self):
        return [type("I", (), {"name": "images"})()]

    def run(self, _, feeds):
        assert feeds["images"].shape == (1, 3, 416, 416)
        raw = self.raw_frames[min(self.calls, len(self.raw_frames) - 1)]
        self.calls += 1
        return [raw]


def raw(boxes, classes=2):
    """boxes: (cx, cy, w, h, score, cls) in model pixels; placed on the stride-8 grid cell under (cx, cy)."""
    out = np.zeros((1, N, 5 + classes), dtype=np.float32)
    for cx, cy, w, h, score, cls in boxes:
        gx, gy = int(cx // 8), int(cy // 8)
        i = gy * 52 + gx
        out[0, i, :4] = [cx / 8 - gx, cy / 8 - gy, np.log(w / 8), np.log(h / 8)]
        out[0, i, 4], out[0, i, 5 + cls] = 1.0, score
    return out


SPEC_PERSON = {"name": "person", "file": "p.onnx", "input": [416, 416], "categories": {"0": "Human"},
               "labels": {"0": "person"}, "threshold": 0.45, "nms": 0.45}
SPEC_FIRE = {"name": "fire_smoke", "file": "f.onnx", "input": [416, 416], "categories": {"0": "Smoke", "1": "Fire"},
             "labels": {"0": "smoke", "1": "fire"}, "threshold": 0.40, "nms": 0.45}


def detector(spec, *frames, classes=2):
    return YoloxDetector(spec, Path("."), session=FakeSession([raw(f, classes) for f in frames]))


class DetectorTests(unittest.TestCase):
    def test_decodes_box_back_to_original_image_coordinates(self):
        d = detector(SPEC_PERSON, [(100, 200, 40, 80, 0.9, 0)])
        img = np.zeros((832, 832, 3), np.uint8)  # scale 0.5 -> boxes double
        [det] = d.detect(img)
        self.assertEqual(det.category, "Human")
        self.assertEqual(det.label, "person")
        self.assertAlmostEqual(det.confidence, 0.9, places=3)
        np.testing.assert_allclose(det.box, (160, 320, 80, 160), atol=0.2)

    def test_filters_low_confidence_and_unmapped_classes(self):
        d = detector(SPEC_PERSON, [(100, 100, 40, 40, 0.3, 0), (300, 300, 40, 40, 0.95, 1)])
        self.assertEqual(d.detect(np.zeros((416, 416, 3), np.uint8)), [])

    def test_nms_merges_duplicates_per_class(self):
        d = detector(SPEC_FIRE, [(100, 100, 60, 60, 0.9, 1), (104, 100, 60, 60, 0.8, 1), (112, 100, 60, 60, 0.7, 0)])
        dets = d.detect(np.zeros((416, 416, 3), np.uint8))
        self.assertEqual(sorted(x.category for x in dets), ["Fire", "Smoke"])

    def test_non_finite_or_empty_boxes_and_scores_are_dropped(self):
        frame = raw([(40, 40, 30, 30, 0.9, 1), (120, 40, 30, 30, 0.9, 1), (200, 40, 30, 30, 0.9, 1),
                     (280, 40, 30, 30, 0.9, 1), (360, 40, 30, 30, 0.9, 1), (40, 200, 30, 30, 0.9, 1)])
        row = lambda cx, cy: int(cy // 8) * 52 + int(cx // 8)  # noqa: E731
        frame[0, row(40, 40), 0] = np.nan       # NaN coordinate
        frame[0, row(120, 40), 2] = np.inf      # infinite width
        frame[0, row(200, 40), 4] = np.inf      # infinite objectness -> infinite confidence
        frame[0, row(280, 40), 6] = np.nan      # NaN class score
        frame[0, row(360, 40), 3] = -np.inf     # exp(-inf) = zero height
        d = YoloxDetector(SPEC_FIRE, Path("."), session=FakeSession([frame]))
        [det] = d.detect(np.zeros((416, 416, 3), np.uint8))  # only the healthy box at (40, 200) survives
        np.testing.assert_allclose(det.box, (25, 185, 30, 30), atol=0.2)
        self.assertTrue(np.isfinite(det.confidence))

    def test_malformed_output_raises_model_error(self):
        bad = YoloxDetector(SPEC_PERSON, Path("."), session=FakeSession([np.zeros((1, 10, 6), np.float32)]))
        with self.assertRaises(ModelError):
            bad.detect(np.zeros((416, 416, 3), np.uint8))

    def test_missing_or_tampered_weights_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ModelError, "not found"):
                YoloxDetector(SPEC_PERSON, Path(tmp))
            (Path(tmp) / "p.onnx").write_bytes(b"tampered")
            with self.assertRaisesRegex(ModelError, "sha256 mismatch"):
                YoloxDetector({**SPEC_PERSON, "sha256": "0" * 64}, Path(tmp))


class TrackerTests(unittest.TestCase):
    def test_keeps_id_for_overlapping_box_and_creates_new_id_far_away(self):
        from sentinel_ai.detector import Det
        t = IouTracker()
        mk = lambda box: Det("Human", "person", 0.9, box, "person", 0.45)
        a = t.update([mk((10, 10, 50, 100))])[0]
        b, c = t.update([mk((14, 12, 50, 100)), mk((300, 10, 50, 100))])
        self.assertEqual(a.track_id, "P1")
        self.assertEqual(b.track_id, "P1")
        self.assertEqual((b.hits, b.age), (2, 2))
        self.assertEqual(c.track_id, "P2")


class PipelineTests(unittest.TestCase):
    FIRE = [(200, 200, 100, 100, 0.8, 1)]

    def test_fire_is_reported_only_after_confirmation_frames(self):
        pipe = CameraPipeline([detector(SPEC_FIRE, self.FIRE)], "cam-1", simulated=False)
        img = np.zeros((416, 416, 3), np.uint8)
        first, second, third = (pipe.process(img) for _ in range(3))
        self.assertFalse(first["fireDetected"])
        self.assertEqual(first["detections"], [])
        self.assertFalse(second["fireDetected"])
        self.assertTrue(third["fireDetected"])
        self.assertEqual(third["fireSeverity"], "Medium")  # 100x100 of 416x416 = 5.8% of the frame
        self.assertIn("confirmed: track F1 seen 3/3 frames", third["detections"][0]["explanation"])

    def test_people_carry_the_tracker_hits_and_a_single_image_carries_none(self):
        person = [(100, 200, 40, 120, 0.9, 0)]
        pipe = CameraPipeline([detector(SPEC_PERSON, person, classes=1)], "cam-1", simulated=False)
        img = np.zeros((416, 416, 3), np.uint8)
        seen = [pipe.process(img)["detections"][0] for _ in range(3)]
        self.assertEqual([(d["trackId"], d["hits"]) for d in seen], [("P1", 1), ("P1", 2), ("P1", 3)])  # reported from frame 1
        single = CameraPipeline([detector(SPEC_PERSON, person, classes=1)], "upload-test", simulated=True).process(img, temporal=False)
        self.assertNotIn("hits", single["detections"][0])  # no tracker: no frame count to report

    def test_single_image_reports_immediately_and_says_so(self):
        pipe = CameraPipeline([detector(SPEC_FIRE, self.FIRE)], "upload-test", simulated=True)
        p = pipe.process(np.zeros((416, 416, 3), np.uint8), temporal=False)
        self.assertTrue(p["fireDetected"])
        self.assertIn("single image", p["detections"][0]["explanation"])
        self.assertTrue(p["simulated"])

    def test_payload_matches_backend_contract(self):
        pipe = CameraPipeline([detector(SPEC_FIRE, self.FIRE)], "cam-1", simulated=False, missing=["person"])
        p = pipe.process(np.zeros((416, 416, 3), np.uint8), temporal=False)
        self.assertEqual(set(p), {"cameraId", "timestamp", "frameWidth", "frameHeight", "normalized", "detections",
                                  "dangerZones", "heatmap", "fireDetected", "fireSeverity", "simulated", "model"})
        self.assertEqual(p["model"], "fire_smoke:f person:missing")
        self.assertEqual(set(p["detections"][0]), {"label", "category", "confidence", "bbox", "trackId", "explanation"})
        self.assertLessEqual(len(p["detections"][0]["explanation"]), 240)
        zone = p["dangerZones"][0]
        self.assertEqual(zone["level"], "Danger")
        self.assertEqual(len(zone["polygon"]), 4)
        hm = p["heatmap"]
        self.assertEqual(len(hm["values"]), hm["rows"] * hm["cols"])
        self.assertTrue(all(0 <= v <= 1 for v in hm["values"]))
        json.dumps(p)  # serialisable

    def test_severity_thresholds(self):
        t = {"Medium": 2.0, "High": 10.0, "Critical": 25.0}
        self.assertEqual([fire_severity(a, t) for a in (0.5, 2, 9.9, 10, 30)], ["Low", "Medium", "Medium", "High", "Critical"])

    def test_fire_area_is_the_union_of_boxes_inside_the_frame(self):
        box = (100, 100, 100, 100)
        self.assertAlmostEqual(fire_area_percent([box], 1000, 1000), 1.0)
        self.assertAlmostEqual(fire_area_percent([box, box], 1000, 1000), 1.0)                    # duplicate counted once (sum gave 2.0)
        self.assertAlmostEqual(fire_area_percent([box, (150, 100, 100, 100)], 1000, 1000), 1.5)   # 50% overlap
        self.assertAlmostEqual(fire_area_percent([(-50, 0, 100, 100)], 1000, 1000), 0.5)           # half outside the frame
        self.assertAlmostEqual(fire_area_percent([(990, 990, 100, 100)], 1000, 1000), 0.01)         # only 10x10 inside
        self.assertAlmostEqual(fire_area_percent([box], 500, 500), 4.0)                             # same box, smaller frame
        self.assertEqual(fire_area_percent([], 640, 480), 0.0)

    def test_partial_nested_settings_keep_the_other_defaults(self):
        pipe = CameraPipeline([], "cam-1", True, {"severity_area_percent": {"High": 8.0}})
        self.assertEqual(pipe.s["severity_area_percent"], {"Medium": 2.0, "High": 8.0, "Critical": 25.0})
        self.assertEqual(pipe.s["confirm_frames"], 3)


class ServiceTests(unittest.TestCase):
    def start(self, engine, cameras=()):
        from http.server import ThreadingHTTPServer
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(engine, list(cameras)))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}"

    def engine(self, tmp, factory):
        (Path(tmp) / "models.json").write_text(json.dumps({"models": [SPEC_PERSON]}))
        return Engine(Path(tmp), detector_factory=factory)

    def post(self, url, body):
        try:
            with urllib.request.urlopen(urllib.request.Request(url + "/infer", data=body, method="POST")) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_no_model_gives_503_and_health_says_so(self):
        with tempfile.TemporaryDirectory() as tmp:
            url = self.start(self.engine(tmp, YoloxDetector))  # real factory, no weights in tmp
            health = json.loads(urllib.request.urlopen(url + "/health").read())
            self.assertEqual(health["status"], "no-model")
            self.assertFalse(health["models"][0]["loaded"])
            ok, png = cv2.imencode(".png", np.zeros((20, 20, 3), np.uint8))
            code, body = self.post(url, png.tobytes())
            self.assertEqual(code, 503)
            self.assertIn("no AI model loaded", body["error"])

    def test_infer_rejects_undecodable_image_and_returns_payload_for_valid_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            factory = lambda spec, d: detector(spec, [(100, 100, 40, 80, 0.9, 0)])
            url = self.start(self.engine(tmp, factory))
            self.assertEqual(self.post(url, b"definitely not an image")[0], 400)
            ok, jpg = cv2.imencode(".jpg", np.zeros((416, 416, 3), np.uint8))
            code, body = self.post(url, jpg.tobytes())
            self.assertEqual(code, 200)
            self.assertEqual(body["payload"]["detections"][0]["category"], "Human")
            self.assertGreaterEqual(body["inferenceMs"], 0)

    def test_file_source_is_always_simulated_and_credentials_are_masked(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "clip.mp4"
            video.write_bytes(b"x")
            e = self.engine(tmp, lambda spec, d: detector(spec, []))
            w = CameraWorker(e, {"cameraId": "cam-1", "source": str(video), "simulated": False}, "http://api", "k")
            self.assertEqual(w.kind, "file")
            self.assertTrue(w.simulated)
        self.assertEqual(mask("rtsp://admin:secret@10.0.0.5/live"), "rtsp://***@10.0.0.5/live")

    def test_deleted_dashboard_camera_stops_on_404_but_config_camera_keeps_running(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class Gone(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b'{"detail": "Camera was deleted"}')

            def log_message(self, *a):
                pass

        api = ThreadingHTTPServer(("127.0.0.1", 0), Gone)
        threading.Thread(target=api.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                e = self.engine(tmp, lambda spec, d: detector(spec, []))
                url = f"http://127.0.0.1:{api.server_address[1]}"
                dashboard = CameraWorker(e, {"cameraId": "cam-1", "source": "rtsp://x/1", "managed": True}, url, "k")
                config = CameraWorker(e, {"cameraId": "cam-2", "source": "rtsp://x/2"}, url, "k")
                dashboard.post({"cameraId": "cam-1"})
                config.post({"cameraId": "cam-2"})
        finally:
            api.shutdown()
        self.assertTrue(dashboard.stop_event.is_set())
        self.assertEqual(dashboard.state, "stopped")
        self.assertFalse(config.stop_event.is_set())  # config.json cameras are the user's own choice: never stopped by the API

    def test_a_worker_stopped_mid_stream_reports_stopped_not_reconnecting(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            video = str(Path(tmp) / "clip.avi")
            out = cv2.VideoWriter(video, cv2.VideoWriter_fourcc(*"MJPG"), 50, (64, 48))
            for _ in range(5):
                out.write(np.zeros((48, 64, 3), np.uint8))
            out.release()
            e = self.engine(tmp, lambda spec, d: detector(spec, []))
            w = CameraWorker(e, {"cameraId": "cam-1", "source": video, "fps": 50, "loop": True}, "http://api", "k")

            def deleted(_payload):  # what post() does on a 404 for a dashboard camera
                w.state = "stopped"
                w.stop_event.set()
            w.post = deleted
            w.run()
        self.assertEqual(w.state, "stopped")

    def test_camera_worker_survives_a_failing_frame(self):
        class Flaky:  # first inference raises, the rest find nothing
            name, file, calls = "fire_smoke", "f.onnx", 0

            def detect(self, bgr):
                Flaky.calls += 1
                if Flaky.calls == 1:
                    raise ModelError("bad output")
                return []

        with tempfile.TemporaryDirectory() as tmp:
            video = str(Path(tmp) / "clip.avi")
            out = cv2.VideoWriter(video, cv2.VideoWriter_fourcc(*"MJPG"), 50, (64, 48))
            for _ in range(3):
                out.write(np.zeros((48, 64, 3), np.uint8))
            out.release()
            e = self.engine(tmp, lambda spec, d: Flaky())
            w = CameraWorker(e, {"cameraId": "cam-1", "source": video, "fps": 50}, "http://127.0.0.1:9", "k")
            w.run()  # file source without loop: returns when the clip ends
            self.assertEqual(Flaky.calls, 3)  # kept going after the failing frame
            self.assertEqual(w.state, "finished")


class FakeCapture:
    """Scripted cv2.VideoCapture: `frames` grabs succeed (each moves `clock` on by `step` s), then the stream drops, raises
    `error`, or blocks on `hang` (a stalled camera)."""

    def __init__(self, opened=True, frames=0, error=None, hang=None, clock=None, step=0.0):
        self.opened, self.frames, self.error, self.hang, self.clock, self.step = opened, frames, error, hang, clock, step
        self.grabs, self.analysed = 0, []  # analysed: when each frame was converted for the model
        self.released = False

    def isOpened(self):
        return self.opened

    def get(self, prop):
        return 0.0

    def grab(self):
        if self.grabs >= self.frames:
            if self.hang:
                self.hang.wait()
            if self.error:
                raise self.error
            return False
        self.grabs += 1
        if self.clock:
            self.clock[0] += self.step
        time.sleep(0.005 if self.frames == float("inf") else 0)  # an endless live stream delivers at its own pace, not in a busy loop
        return True

    def retrieve(self):
        self.analysed.append(time.monotonic())
        return True, np.zeros((48, 64, 3), np.uint8)

    def release(self):
        self.released = True


class Stopper:
    """stop_event stand-in: records every backoff wait and reports "stopped" after `n` of them."""

    def __init__(self, n):
        self.n, self.waits = n, []

    def is_set(self):
        return len(self.waits) >= self.n

    def wait(self, seconds):
        self.waits.append(seconds)

    def set(self):
        self.n = 0


class RtspReliabilityTests(unittest.TestCase):
    """CameraWorker against scripted captures and local fake servers: no real camera is ever opened."""

    def engine(self, detect=list):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        (Path(tmp.name) / "models.json").write_text(json.dumps({"models": [SPEC_FIRE]}))
        Detector = type("D", (), {"name": "fire_smoke", "file": "f.onnx", "detect": lambda self, bgr: detect() or []})
        return Engine(Path(tmp.name), detector_factory=lambda spec, d: Detector())

    def worker(self, caps, cam_id="cam-1", engine=None, fps=2):
        """A stream worker whose open() hands out `caps` in order (kept in w.opened) and whose posts are only counted."""
        w = CameraWorker(engine or self.engine(), {"cameraId": cam_id, "source": "rtsp://127.0.0.1:9/live", "fps": fps}, "http://api", "k")
        self.assertEqual(w.kind, "stream")
        caps, w.opened, w.posts = iter(caps), [], []
        w.open = lambda: w.opened.append(next(caps)) or w.opened[-1]
        w.post = w.posts.append
        return w

    def test_stream_capture_uses_ffmpeg_with_bounded_open_and_read_waits_and_other_sources_do_not(self):
        with mock.patch("sentinel_ai.service.cv2.VideoCapture") as vc:
            CameraWorker.open(self.worker([]))  # the real open(), not the test double
            CameraWorker.open(type("W", (), {"kind": "webcam", "source": 0})())
        self.assertEqual(vc.call_args_list[0], mock.call("rtsp://127.0.0.1:9/live", cv2.CAP_FFMPEG,
                                                         [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, STREAM_TIMEOUT_MS,
                                                          cv2.CAP_PROP_READ_TIMEOUT_MSEC, STREAM_TIMEOUT_MS]))
        self.assertEqual(vc.call_args_list[1], mock.call(0))  # webcams keep OpenCV's own backend and settings

    def test_opencv_really_gives_up_on_a_hung_open_and_a_stalled_stream(self):
        """Real OpenCV/FFmpeg against local sockets, with the timeout cut to 1 s (its default is 30 s)."""
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        silent = socket.socket()  # accepts the TCP connection (backlog) and never answers: a hung camera
        silent.bind(("127.0.0.1", 0))
        silent.listen(4)
        self.addCleanup(silent.close)
        stall = threading.Event()
        self.addCleanup(stall.set)
        jpg = cv2.imencode(".jpg", np.zeros((48, 64, 3), np.uint8))[1].tobytes()

        class Stalling(BaseHTTPRequestHandler):  # a burst of MJPEG frames, then silence on an open connection
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                self.end_headers()
                for _ in range(30):
                    self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(jpg) + jpg + b"\r\n")
                self.wfile.flush()
                stall.wait(60)

            def log_message(self, *a):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Stalling)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with mock.patch("sentinel_ai.service.STREAM_TIMEOUT_MS", 1000):
            w = self.worker([])
            w.source = f"rtsp://127.0.0.1:{silent.getsockname()[1]}/live"
            t0 = time.monotonic()
            cap = CameraWorker.open(w)
            self.assertFalse(cap.isOpened())
            self.assertLess(time.monotonic() - t0, 5)
            w.source = f"http://127.0.0.1:{server.server_address[1]}/video"
            cap = CameraWorker.open(w)
            try:
                self.assertTrue(cap.isOpened())
                while cap.grab():
                    pass
                t0 = time.monotonic()
                self.assertFalse(cap.grab())  # the stalled read returns instead of blocking
                self.assertLess(time.monotonic() - t0, 5)
            finally:
                cap.release()

    def test_open_failures_back_off_exponentially_to_30_s_and_release_every_capture(self):
        caps = [FakeCapture(opened=False) for _ in range(7)]
        w = self.worker(caps)
        w.stop_event = Stopper(7)
        w.run()
        self.assertEqual(w.stop_event.waits, [1, 2, 4, 8, 16, 30, 30])
        self.assertTrue(all(c.released for c in caps))
        self.assertEqual((w.state, w.error), ("error", "cannot open stream source"))

    def test_disconnect_reconnects_after_1_s_but_a_stream_that_opens_without_frames_backs_off(self):
        clock = [0.0]  # frozen: only the first frame of each connection is due
        caps = [FakeCapture(frames=3), FakeCapture(opened=False), FakeCapture(frames=2), FakeCapture(), FakeCapture()]
        w = self.worker(caps)
        w.stop_event = Stopper(5)
        with mock.patch("sentinel_ai.service.time.monotonic", lambda: clock[0]):
            w.run()
        self.assertEqual(w.stop_event.waits, [1, 2, 1, 2, 4])  # frames reset the backoff; open-then-nothing does not
        self.assertEqual(len(w.posts), 2)  # one analysed frame per connection that delivered
        self.assertTrue(all(c.released for c in caps))
        self.assertEqual((w.state, w.error), ("reconnecting", "source ended or dropped"))

    def test_a_capture_that_raises_is_released_and_reconnected_without_leaking_the_link(self):
        caps = [FakeCapture(frames=1, error=cv2.error("rtsp://admin:secret@cam/1 broke")), FakeCapture(frames=1)]
        w = self.worker(caps)
        w.stop_event = Stopper(2)
        with self.assertLogs("sentinel_ai", "WARNING") as logs:
            w.run()
        self.assertTrue(all(c.released for c in caps))
        self.assertEqual(len(w.posts), 2)  # the thread lived on and the second connection delivered
        self.assertNotIn("secret", " ".join(logs.output))

    def test_fps_limit_analyses_one_frame_per_interval_and_never_converts_the_skipped_ones(self):
        clock = [0.0]
        cap = FakeCapture(frames=20, clock=clock, step=0.25)  # a 4 fps stream for 5 s
        w = self.worker([cap], fps=1)
        with mock.patch("sentinel_ai.service.time.monotonic", lambda: clock[0]):
            self.assertTrue(w.read_frames(cap))
        self.assertEqual((cap.grabs, cap.analysed, len(w.posts)), (20, [0.25, 1.25, 2.25, 3.25, 4.25], 5))  # every frame drained, 1 per second analysed

    def test_a_stalled_and_a_dead_camera_do_not_hold_up_a_healthy_one(self):
        stalled = threading.Event()
        hung = FakeCapture(hang=stalled)
        a = self.worker([hung, *[FakeCapture() for _ in range(50)]], "cam-a")
        b = self.worker([FakeCapture(opened=False) for _ in range(50)], "cam-b")
        c = self.worker([FakeCapture(frames=float("inf"))], "cam-c", fps=20)
        for w in (a, b, c):
            w.start()
        try:
            deadline = time.monotonic() + 10
            while len(c.posts) < 5 and time.monotonic() < deadline:
                c.stop_event.wait(0.05)
            self.assertGreaterEqual(len(c.posts), 5)
            self.assertEqual(a.state, "running")  # still inside its stalled read
            self.assertEqual(b.state, "error")
        finally:
            for w in (a, b, c):
                w.stop_event.set()
            stalled.set()  # the stalled read returns (as the read timeout makes it do)
            for w in (a, b, c):
                w.join(5)
        self.assertFalse(any(w.is_alive() for w in (a, b, c)))
        self.assertTrue(hung.released)
        self.assertTrue(all(cap.released for w in (a, b, c) for cap in w.opened))

    def test_overload_slows_every_camera_evenly_but_never_beyond_its_fps_limit(self):
        """2 cameras x 5 fps x 150 ms inference = 1.5 s of work a second for one lock: each camera gets about half."""
        engine = self.engine(lambda: time.sleep(0.15))  # one engine, one inference lock, as in the service
        cams = [self.worker([FakeCapture(frames=float("inf"))], f"cam-{i}", engine, fps=5) for i in range(2)]
        for w in cams:
            w.start()
        try:
            deadline = time.monotonic() + 15
            while min(len(w.posts) for w in cams) < 5 and time.monotonic() < deadline:
                cams[0].stop_event.wait(0.05)
        finally:
            for w in cams:
                w.stop_event.set()
            for w in cams:
                w.join(5)
        for w in cams:
            self.assertGreaterEqual(len(w.posts), 5, w.cam["cameraId"])  # neither camera starves
            times = w.opened[0].analysed
            self.assertGreaterEqual(min(b - a for a, b in zip(times, times[1:])), 0.2 - 0.01)  # never above 5 fps
        self.assertEqual(engine.count, sum(len(w.posts) for w in cams))  # frames are skipped, never queued for later
        self.assertGreaterEqual(engine.last_ms, 150)


class CameraSyncTests(unittest.TestCase):
    def test_starts_new_cameras_and_stops_removed_or_changed_ones(self):
        class FakeWorker:
            def __init__(self, cam):
                self.cam, self.started, self.stop_event = cam, False, threading.Event()

            def start(self):
                self.started = True

        workers, managed = [], {}
        sync_cameras(workers, managed, {"cam-a": "rtsp://a", "cam-b": "rtsp://b"}, FakeWorker)
        a, b = managed["cam-a"], managed["cam-b"]
        self.assertTrue(a.started and b.started)
        sync_cameras(workers, managed, {"cam-a": "rtsp://a", "cam-b": "rtsp://b2"}, FakeWorker)  # b's link changed
        self.assertIs(managed["cam-a"], a)  # unchanged camera keeps running
        self.assertTrue(b.stop_event.is_set())
        self.assertEqual(managed["cam-b"].cam["source"], "rtsp://b2")
        a.stop_event.set()  # stopped after a 404, but the camera is in the list again (re-added): restarted
        sync_cameras(workers, managed, {"cam-a": "rtsp://a", "cam-b": "rtsp://b2"}, FakeWorker)
        self.assertIsNot(managed["cam-a"], a)
        self.assertTrue(managed["cam-a"].started and managed["cam-a"].cam["managed"])
        sync_cameras(workers, managed, {}, FakeWorker)  # all removed in the dashboard
        self.assertEqual((workers, managed), ([], {}))


class AveragePrecisionTests(unittest.TestCase):
    def test_coco_101_point_ap_on_a_hand_computed_example(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("evaluate", Path(__file__).parents[1] / "tools" / "evaluate.py")
        ev = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ev)
        self.assertEqual(ev.average_precision([(0.9, True), (0.8, True)], 2), 1.0)
        self.assertEqual(ev.average_precision([(0.9, False)], 2), 0.0)
        # TP, FP, TP of 2 boxes: precision envelope 1.0 up to recall 0.5 (51 points), then 2/3 (50 points)
        self.assertAlmostEqual(ev.average_precision([(0.7, True), (0.9, True), (0.8, False)], 2), (51 + 50 * 2 / 3) / 101, places=4)
        self.assertIsNone(ev.average_precision([], 0))


class CocoToYoloTests(unittest.TestCase):
    def test_person_boxes_become_normalised_yolo_lines_and_empty_images_become_negatives(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("coco_to_yolo", Path(__file__).parents[1] / "tools" / "coco_to_yolo.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        coco = {"categories": [{"id": 1, "name": "person"}, {"id": 3, "name": "car"}],
                "images": [{"id": 7, "file_name": "a.jpg", "width": 200, "height": 100},
                           {"id": 8, "file_name": "b.jpg", "width": 100, "height": 100}],
                "annotations": [{"image_id": 7, "category_id": 1, "bbox": [50, 25, 100, 50], "iscrowd": 0},
                                {"image_id": 7, "category_id": 1, "bbox": [0, 0, 200, 100], "iscrowd": 1},
                                {"image_id": 7, "category_id": 3, "bbox": [0, 0, 10, 10], "iscrowd": 0},
                                {"image_id": 8, "category_id": 3, "bbox": [0, 0, 10, 10], "iscrowd": 0}]}
        labels, crowd = mod.convert(coco)
        self.assertEqual(labels, {"a.jpg": ["0 0.500000 0.500000 0.500000 0.500000"], "b.jpg": []})
        self.assertEqual(crowd, 1)


class DegradeTests(unittest.TestCase):
    def test_dark_lowers_brightness_and_fog_lowers_contrast(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("degrade", Path(__file__).parents[1] / "tools" / "degrade.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        img = np.zeros((40, 40, 3), np.uint8)
        img[:, 20:] = 200  # half black, half bright
        rng = np.random.default_rng(0)
        dark, fog = mod.degrade(img, ["dark"], rng), mod.degrade(img, ["fog"], rng)
        self.assertLess(dark.mean(), img.mean() * 0.4)
        self.assertLess(int(fog[:, 20:].mean()) - int(fog[:, :20].mean()), 0.5 * 200)  # contrast shrinks
        self.assertGreater(fog[:, :20].mean(), 100)                                      # black turns grey
        np.testing.assert_array_equal(mod.degrade(img, [], rng), img)                    # none = unchanged


class EnhanceTests(unittest.TestCase):
    def test_both_methods_restore_contrast_of_a_hazy_image(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("enhance", Path(__file__).parents[1] / "tools" / "enhance.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        ramp = np.linspace(140, 180, 64, dtype=np.float32)  # a scene squeezed into 40 grey levels, as haze does
        hazy = np.repeat(np.tile(ramp, (64, 1))[:, :, None], 3, axis=2).astype(np.uint8)
        for method in ("clahe", "stretch"):
            out = mod.enhance(hazy, method)
            self.assertEqual(out.shape, hazy.shape)
            self.assertGreater(out.std(), hazy.std() * 1.1, method)  # values spread further apart = more contrast (CLAHE is the gentle one)
        with self.assertRaises(ValueError):
            mod.enhance(hazy, "magic")


class TestCameraTests(unittest.TestCase):
    def test_video_file_is_served_as_mjpeg_that_opencv_can_read(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("mjpeg_camera", Path(__file__).parents[1] / "tools" / "mjpeg_camera.py")
        mjpeg_camera = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mjpeg_camera)
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:  # the camera thread keeps the file open
            video = str(Path(tmp) / "clip.avi")
            out = cv2.VideoWriter(video, cv2.VideoWriter_fourcc(*"MJPG"), 10, (64, 48))
            for i in range(10):
                out.write(np.full((48, 64, 3), i * 20, np.uint8))
            out.release()
            server = mjpeg_camera.serve(video, port=0)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                cap = cv2.VideoCapture(f"http://127.0.0.1:{server.server_address[1]}/video")  # what the AI service does
                ok, frame = cap.read()
                cap.release()
            finally:
                server.shutdown()
        self.assertTrue(ok)
        self.assertEqual(frame.shape, (48, 64, 3))


if __name__ == "__main__":
    unittest.main()

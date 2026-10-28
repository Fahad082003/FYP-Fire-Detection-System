"""AI service integration against a real local HTTP stub (ports of AiServiceTests.cs and AiRateLimitTests.cs).
MOCK-BASED: proves the integration contract, not model accuracy (the AI service's own suite covers the models)."""
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from rest_framework.settings import api_settings
from rest_framework.test import APIClient

from sentinel.models import IncidentAlert, SmartSprinkler

from .base import SentinelTestCase

PNG = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A, 1, 2, 3, 4])
INFER_RESULT = {"inferenceMs": 412.7, "payload": {
    "cameraId": "x", "frameWidth": 640, "frameHeight": 480, "model": "person:yolox_tiny fire:fire_smoke", "fireSeverity": "Medium",
    "detections": [
        {"label": "person", "category": "Human", "confidence": 0.88, "bbox": [10, 20, 50, 120], "trackId": "P1", "explanation": "person 0.88 >= 0.45"},
        {"label": "fire", "category": "Fire", "confidence": 0.71, "bbox": [300, 200, 80, 60], "explanation": "fire 0.71 >= 0.40"},
        {"label": "smoke", "category": "Smoke", "confidence": 0.52, "bbox": [280, 100, 150, 90]}]}}


class StubAi:
    """A local HTTP server standing in for ai-service. `reply` returns (status, body text, delay seconds)."""

    def __init__(self):
        self.reply = lambda path: (200, "{}", 0)
        self.calls = 0
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def _answer(self):
                stub.calls += 1
                if self.headers.get("Content-Length"):
                    self.rfile.read(int(self.headers["Content-Length"]))
                status, text, delay = stub.reply(self.path)
                time.sleep(delay)
                data = text.encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = _answer

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.handle_error = lambda request, address: None  # the client hung up (timeout tests): not a failure
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def closed_port_url() -> str:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{s.getsockname()[1]}"  # nothing listens once the socket is closed


class AiTests(SentinelTestCase):
    def setUp(self):
        super().setUp()
        self.ai = StubAi()
        self.addCleanup(self.ai.close)
        patcher = mock.patch.dict(settings.SENTINEL, AI_BASE_URL=self.ai.url, AI_TIMEOUT_SECONDS=1)
        patcher.start()
        self.addCleanup(patcher.stop)

    def analyze(self, client=None, data=PNG):
        return (client or self.op).post("/api/ai/analyze", {"file": SimpleUploadedFile("test.png", data, "image/png")}, format="multipart")

    def test_status_reports_online_service_and_its_health_json(self):
        self.ai.reply = lambda path: (200, json.dumps({"status": "ok", "models": [{"name": "person", "loaded": True}]}), 0)
        s = self.op.get("/api/ai/status").json()
        self.assertTrue(s["configured"] and s["online"])
        self.assertEqual(s["service"]["status"], "ok")

    def test_status_reports_offline_and_malformed_health_without_failing(self):
        with mock.patch.dict(settings.SENTINEL, AI_BASE_URL=closed_port_url()):
            down = self.op.get("/api/ai/status").json()
        self.assertFalse(down["online"])
        self.assertIn("unreachable", down["error"])
        self.ai.reply = lambda path: (200, "<html>", 0)
        bad = self.op.get("/api/ai/status").json()
        self.assertFalse(bad["online"])
        self.assertIn("invalid JSON", bad["error"])
        with mock.patch.dict(settings.SENTINEL, AI_BASE_URL=""):
            self.assertFalse(self.op.get("/api/ai/status").json()["configured"])

    def test_status_never_blocks_longer_than_three_seconds(self):
        self.ai.reply = lambda path: (200, "{}", 5)
        started = time.perf_counter()
        self.assertFalse(self.op.get("/api/ai/status").json()["online"])
        self.assertLess(time.perf_counter() - started, 4.5)

    def test_analyze_maps_model_output_and_never_opens_an_incident(self):
        self.ai.reply = lambda path: (200, json.dumps(INFER_RESULT), 0)
        r = self.analyze()
        self.assertEqual(r.status_code, 200, r.content)
        a = r.json()
        f = a["frame"]
        self.assertEqual((f["cameraId"], a["inferenceMs"], f["model"]), ("upload-test", 412.7, "person:yolox_tiny fire:fire_smoke"))
        self.assertTrue(f["fireDetected"] and f["smokeDetected"])
        self.assertEqual((f["fireSeverity"], f["humanCount"]), ("Medium", 1))
        self.assertEqual((f["detections"][0]["trackId"], f["detections"][0]["explanation"]), ("P1", "person 0.88 >= 0.45"))
        self.assertEqual(IncidentAlert.objects.count(), 0)  # preview only

    def test_analyze_rejects_fake_and_oversized_images_before_calling_the_service(self):
        self.assertEqual(self.analyze(data=b"not an image").status_code, 400)
        r = self.analyze(data=PNG + b"x" * (7 * 1024 * 1024))  # refused from Content-Length, before the body is stored
        self.assertEqual((r.status_code, r.json()["errors"]["file"]), (400, ["Image is larger than 5 MB."]))
        self.assertEqual(self.ai.calls, 0)

    def test_analyze_turns_service_failures_into_explicit_errors(self):
        cases = {
            "model-missing": ((503, '{"error":"fire model not loaded"}', 0), 503),
            "timeout": ((200, json.dumps(INFER_RESULT), 3), 504),
            "not-json": ((200, "<html>oops</html>", 0), 502),
            "no-payload": ((200, '{"inferenceMs": 3}', 0), 502),
            "invalid-confidence": ((200, '{"payload":{"detections":[{"category":"Fire","confidence":7,"bbox":[0,0,1,1]}]}}', 0), 502),
            "server-error": ((500, '{"error":"boom"}', 0), 502),
            "undecodable": ((400, '{"error":"cannot decode image"}', 0), 400),
        }
        for name, (answer, expected) in cases.items():
            with self.subTest(name):
                self.ai.reply = lambda path, answer=answer: answer
                r = self.analyze()
                self.assertEqual(r.status_code, expected, r.content)
                self.assertEqual(r["Content-Type"], "application/problem+json")
        # Windows retries a refused localhost connect for ~2 s, so give it the time a real deployment has (20 s default).
        with self.subTest("down"), mock.patch.dict(settings.SENTINEL, AI_BASE_URL=closed_port_url(), AI_TIMEOUT_SECONDS=10):
            self.assertEqual(self.analyze().status_code, 503)

    def test_ai_endpoints_require_an_operator(self):
        self.assertEqual(APIClient().get("/api/ai/status").status_code, 401)
        self.assertEqual(self.device.get("/api/ai/status").status_code, 401)  # a device key is not an operator
        self.assertEqual(self.analyze(client=APIClient()).status_code, 401)

    def test_analyze_is_limited_per_user_recovers_after_the_window_and_never_blocks_live_frames(self):
        self.ai.reply = lambda path: (200, json.dumps(INFER_RESULT), 0)
        rates = {**api_settings.DEFAULT_THROTTLE_RATES, "ai-analyze": "3/2"}
        with override_settings(REST_FRAMEWORK={**settings.REST_FRAMEWORK, "DEFAULT_THROTTLE_RATES": rates}), \
                mock.patch("rest_framework.throttling.SimpleRateThrottle.THROTTLE_RATES", rates):
            for _ in range(3):
                self.assertNotEqual(self.analyze().status_code, 429)
            self.assertEqual(self.analyze().status_code, 429)
            self.assertNotEqual(self.analyze(client=self.admin_client).status_code, 429)  # per user
            frame = self.post_frame(cameraId="cam-lobby", frameWidth=640, frameHeight=480,
                                    detections=[{"category": "Human", "confidence": 0.9, "bbox": [100, 100, 50, 150]}])
            self.assertEqual(frame.status_code, 202)  # live frames use another endpoint and are never limited
            self.assertEqual(self.analyze(client=APIClient()).status_code, 401)  # authentication first: 401, not 429
            time.sleep(2.5)  # window over
            self.assertNotEqual(self.analyze().status_code, 429)

    def test_smoke_only_frame_opens_a_medium_non_emergency_incident_tagged_with_the_model(self):
        r = self.post_frame(cameraId="cam-server", frameWidth=640, frameHeight=480, model="fire:fire_smoke", detections=[
            {"category": "Smoke", "confidence": 0.6, "bbox": [10, 10, 200, 100], "explanation": "smoke 0.60 >= 0.40, seen 3/4 frames"}])
        self.assertEqual(r.status_code, 202)
        incident = IncidentAlert.objects.filter(zone="First Floor - Server Hall", is_resolved=False).latest("id")
        self.assertEqual(incident.severity, "Medium")
        self.assertIn("Smoke detected", incident.message)
        self.assertIn("[AI fire:fire_smoke]", incident.message)
        self.assertFalse(SmartSprinkler.objects.filter(zone="First Floor - Server Hall", is_active=True).exists())  # not an emergency

    def test_simulated_emergency_frames_never_switch_sprinklers(self):
        for camera, zone, simulated, expect_active in (("cam-lobby", "Ground Floor - Lobby", True, False),
                                                       ("drone-01", "Ground Floor - Warehouse", False, True)):
            with self.subTest(camera):
                r = self.post_frame(cameraId=camera, frameWidth=640, frameHeight=480, simulated=simulated,
                                    detections=[{"category": "Human", "confidence": 0.9, "bbox": [100, 100, 50, 150], "trackId": "P1"}],
                                    dangerZones=[{"label": "Fuel store", "level": "Danger", "polygon": [[0, 200], [300, 200], [300, 300], [0, 300]]}])
                self.assertEqual(r.status_code, 202)
                self.assertTrue(IncidentAlert.objects.filter(zone=zone, is_resolved=False, danger_count__gt=0).exists())
                self.assertEqual(SmartSprinkler.objects.filter(zone=zone, is_active=True).exists(), expect_active)

    def test_detections_need_a_finite_confidence_and_a_box_that_touches_the_frame(self):
        for confidence, bbox, accepted in (('"NaN"', "[1, 2, 3, 4]", False), ('"Infinity"', "[1, 2, 3, 4]", False),
                                           ("0.9", "[-5000, 9000, 10, 10]", False), ("0.9", "[640, 10, 10, 10]", False),
                                           ("0.9", "[-5, -5, 10, 10]", True), ("0.9", "[630, 470, 50, 50]", True),
                                           ("NaN", "[1, 2, 3, 4]", False), ("1e999", "[1, 2, 3, 4]", False)):
            with self.subTest(confidence=confidence, bbox=bbox):
                body = (f'{{"cameraId":"cam-lobby","frameWidth":640,"frameHeight":480,'
                        f'"detections":[{{"category":"Human","confidence":{confidence},"bbox":{bbox}}}]}}')
                r = self.device.post("/api/detections", body, content_type="application/json")
                self.assertEqual(r.status_code, 202 if accepted else 400)

    def test_overlong_explanation_is_rejected(self):
        r = self.post_frame(cameraId="cam-lobby", detections=[{"category": "Human", "bbox": [1, 2, 3, 4], "explanation": "x" * 241}])
        self.assertEqual(r.status_code, 400)

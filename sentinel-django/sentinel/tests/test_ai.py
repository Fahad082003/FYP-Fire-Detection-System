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

from sentinel.models import IncidentAlert, IncidentLog, IncidentLogType, SmartSprinkler

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
                body = self.rfile.read(int(self.headers["Content-Length"])) if self.headers.get("Content-Length") else b""
                stub.last = (self.command, self.path, body, self.headers.get("X-Device-Key"))
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


class SensitivityTests(SentinelTestCase):
    """GET/PUT /api/ai/sensitivity against the AI service stub. The AI service's own suite proves the value reaches inference."""

    def setUp(self):
        super().setUp()
        self.ai = StubAi()
        self.addCleanup(self.ai.close)
        patcher = mock.patch.dict(settings.SENTINEL, AI_BASE_URL=self.ai.url, AI_TIMEOUT_SECONDS=1)
        patcher.start()
        self.addCleanup(patcher.stop)
    CONFIG = {"min": 0.25, "max": 0.55, "models": [{"name": "fire_smoke", "loaded": True, "threshold": 0.4, "defaultThreshold": 0.4},
                                                   {"name": "person", "loaded": True, "threshold": 0.45, "defaultThreshold": 0.45}]}

    def reply_with(self, applied=None, status=200, error=None):
        def reply(path):
            if self.ai.last[0] == "POST":
                return (status, json.dumps({"error": error}) if error else json.dumps(
                    {"model": "fire_smoke", "threshold": applied, **self.CONFIG}), 0)
            return 200, json.dumps(self.CONFIG), 0
        self.ai.reply = reply

    def put(self, client, threshold=0.45):
        return client.put("/api/ai/sensitivity", {"model": "fire_smoke", "threshold": threshold}, format="json")

    def test_an_admin_change_returns_what_the_ai_service_applied_and_is_audited(self):
        self.reply_with(applied=0.45)
        r = self.put(self.admin_client)
        self.assertEqual((r.status_code, r.json()["threshold"]), (200, 0.45))
        method, path, body, key = self.ai.last
        self.assertEqual((method, path, json.loads(body), key), ("POST", "/config", {"model": "fire_smoke", "threshold": 0.45}, "test-device-key"))
        [entry] = IncidentLog.objects.filter(type=IncidentLogType.SETTING_CHANGED)
        self.assertIn("set the fire_smoke confidence threshold to 0.45 (was 0.4); applied by the AI service", entry.details)

    def test_the_active_value_is_the_ai_services_answer_not_the_requested_one(self):
        self.reply_with(applied=0.5)  # whatever the service reports is what is shown as active
        self.assertEqual(self.put(self.admin_client, 0.45).json()["threshold"], 0.5)
        self.assertEqual(self.op.get("/api/ai/sensitivity").json()["models"][0]["threshold"], 0.4)  # GET = the service's view

    def test_operators_devices_and_bad_values_cannot_change_it(self):
        self.reply_with(applied=0.45)
        self.assertEqual(self.put(self.op).status_code, 403)
        self.assertEqual(self.put(self.device).status_code, 401)
        self.assertEqual(self.put(APIClient()).status_code, 401)
        self.assertEqual(self.admin_client.put("/api/ai/sensitivity", {"model": "fire_smoke", "threshold": "high"}, format="json").status_code, 400)
        self.assertEqual(self.ai.calls, 0)  # refused before the AI service is asked
        self.assertFalse(IncidentLog.objects.filter(type=IncidentLogType.SETTING_CHANGED).exists())

    def test_a_refused_or_unreachable_ai_service_changes_nothing(self):
        self.reply_with(status=400, error="threshold must be a number from 0.25 to 0.55")
        r = self.put(self.admin_client, 0.9)
        self.assertEqual(r.status_code, 400)
        self.assertIn("0.25 to 0.55", r.json()["detail"])
        with mock.patch.dict(settings.SENTINEL, AI_BASE_URL=closed_port_url()):
            r = self.put(self.admin_client)
            self.assertEqual(r.status_code, 503)
            self.assertIn("nothing was changed", r.json()["detail"])
            self.assertEqual(self.op.get("/api/ai/sensitivity").status_code, 503)
        self.assertFalse(IncidentLog.objects.filter(type=IncidentLogType.SETTING_CHANGED).exists())

    def test_the_ai_page_shows_the_active_threshold_and_only_admins_get_the_control(self):
        self.ai.reply = lambda path: (200, json.dumps({"status": "ok", "models": [{"name": "fire_smoke", "loaded": True, "threshold": 0.45,
                                                       "defaultThreshold": 0.4, "categories": ["Fire"], "file": "f", "license": "x"}],
                                                       "cameras": [], "inference": {"count": 0, "avgMs": None}}), 0)
        self.client.force_login(self.admin)
        html = self.client.get("/ai").content.decode()
        self.assertIn('data-sensitivity="fire_smoke"', html)
        self.assertIn("Active in the AI service: 0.45", html)
        self.assertIn("(default 0.4)", html)
        self.assertIn(">Apply</button>", html)
        self.client.force_login(self.operator)
        html = self.client.get("/ai").content.decode()
        self.assertNotIn(">Apply</button>", html)
        self.assertIn("Only an Admin can change it.", html)

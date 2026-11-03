"""GET /api/system/health: every component from a real check (sentinel/health.py). The AI service answer is mocked here
(test_ai.py covers the real HTTP client); database, watchdog, sensors and the live stream are the real ones."""
import asyncio
import contextlib
import json
import threading
import time
from datetime import timedelta
from unittest import mock

from asgiref.sync import sync_to_async
from django.conf import settings
from django.db import connection
from django.db.utils import OperationalError
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from sentinel import events, health
from sentinel.models import CameraFeed, IncidentAlert, IncidentSource, SensorNode
from sentinel.watchdog import watchdog

from .base import SentinelTestCase

MODELS = [{"name": "person", "file": "yolox_tiny.onnx", "loaded": True, "error": None},
          {"name": "fire_smoke", "file": "fire_smoke_yolox.onnx", "loaded": True, "error": None}]


def ai(status="ok", models=MODELS, cameras=()):
    return {"configured": True, "online": True, "error": None,
            "service": {"status": status, "models": list(models), "cameras": list(cameras)}}


AI_OFFLINE = {"configured": True, "online": False, "error": "AI service unreachable at http://127.0.0.1:8001.", "service": None}


class HealthTests(SentinelTestCase):
    def setUp(self):
        super().setUp()
        self.ai_answer = ai()
        patcher = mock.patch("sentinel.health.ai_client.status", side_effect=lambda: self.ai_answer)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.supervisor_file = mock.patch.object(health, "SUPERVISOR_STATE", settings.BASE_DIR / "no-such-dir" / "supervisor.json")
        self.supervisor_file.start()
        self.addCleanup(self.supervisor_file.stop)
        events.last_delivery = 0.0

    def get(self) -> dict:
        r = self.op.get("/api/system/health")
        self.assertEqual(r.status_code, 200)
        return r.json()

    def component(self, key) -> dict:
        return self.get()["components"][key]

    def workers(self):
        """The server's background threads, as sentinel.startup starts them (tests do not run it)."""
        stop = threading.Event()
        threads = [threading.Thread(target=stop.wait, name=name, daemon=True) for name in health.WORKERS]
        for t in threads:
            t.start()
        self.addCleanup(stop.set)

    def fresh_sensors(self):
        SensorNode.objects.update(last_reading_at=timezone.now())

    def live_dashboard(self):
        patcher = mock.patch.object(events, "subscriber_count", return_value=1)
        patcher.start()
        self.addCleanup(patcher.stop)
        events.delivered()

    # ---------------------------------------------------------------- endpoint and overall status
    def test_operators_only_and_every_component_has_status_message_and_check_time(self):
        self.assertEqual(APIClient().get("/api/system/health").status_code, 401)
        self.assertEqual(self.device.get("/api/system/health").status_code, 401)  # it names cameras and sensors
        h = self.get()
        self.assertEqual(list(h["components"]), ["database", "django", "ai", "models", "cameras", "sensors", "liveUpdates", "simulator"])
        for c in h["components"].values():
            self.assertIn(c["status"], ("healthy", "loading", "degraded", "offline", "unknown"))
            self.assertTrue(c["message"] and c["lastChecked"] and c["name"])
        self.assertIn(h["status"], ("healthy", "degraded", "critical"))

    def test_overall_healthy_only_when_every_required_component_is(self):
        self.workers()
        self.fresh_sensors()
        self.live_dashboard()
        h = self.get()
        self.assertEqual((h["status"], h["message"]), ("healthy", "All required components healthy."), h)
        self.assertEqual(h["components"]["simulator"]["status"], "unknown")  # optional: not running is not a failure
        self.assertEqual(h["components"]["cameras"]["status"], "unknown")  # demo feeds have no links and sent nothing: optional

    def test_overall_degraded_while_the_ai_loads_and_critical_without_the_database(self):
        self.workers()
        self.fresh_sensors()
        self.live_dashboard()
        self.ai_answer = ai("loading", models=[])
        h = self.get()
        self.assertEqual((h["status"], h["message"]), ("degraded", "AI Service loading; AI Models loading"))
        self.ai_answer = ai()
        with mock.patch("sentinel.health.connection.cursor", side_effect=OperationalError("08001", "server not found")):
            h = self.get()
        self.assertEqual(h["status"], "critical")
        self.assertEqual(h["components"]["database"]["status"], "offline")
        self.assertNotIn("server not found", h["components"]["database"]["message"])  # no driver text (server names) leaks
        self.assertEqual([h["components"][k]["status"] for k in ("cameras", "sensors")], ["unknown", "unknown"])  # not queried again

    def test_the_check_only_reads(self):
        with CaptureQueriesContext(connection) as queries:
            self.get()
        self.assertTrue(queries.captured_queries)
        self.assertTrue(all(q["sql"].lstrip().upper().startswith("SELECT") for q in queries.captured_queries), queries.captured_queries)

    def test_a_failing_check_is_reported_unknown_and_the_others_still_answer(self):
        with mock.patch.object(health, "sensors", side_effect=RuntimeError("bug")), self.assertLogs("sentinel.health", "ERROR"):
            h = self.get()
        self.assertEqual((h["components"]["sensors"]["status"], h["components"]["sensors"]["message"]), ("unknown", "Check failed (RuntimeError)."))
        self.assertEqual(h["components"]["database"]["status"], "healthy")
        self.assertEqual(h["status"], "degraded")

    # ---------------------------------------------------------------- database and Django
    def test_database_connected(self):
        c = self.component("database")
        self.assertEqual((c["status"], c["message"]), ("healthy", "Connected."))

    def test_django_needs_its_background_workers(self):
        c = self.component("django")
        self.assertEqual(c["status"], "degraded")
        self.assertIn("camera-watchdog", c["message"])
        self.workers()
        c = self.component("django")
        self.assertEqual((c["status"], c["details"]["workers"]), ("healthy", {"camera-watchdog": True, "notification-worker": True}))

    # ---------------------------------------------------------------- AI service and models
    def test_ai_status_mapping_never_calls_loading_or_no_model_healthy(self):
        for answer, expected in [(ai("ok"), "healthy"), (ai("loading", models=[]), "loading"), (ai("degraded"), "degraded"),
                                 (ai("no-model"), "degraded"), (ai("strange"), "unknown"), (AI_OFFLINE, "offline"),
                                 ({"configured": False, "online": False, "error": "AiService:BaseUrl is not set.", "service": None}, "offline")]:
            self.ai_answer = answer
            c = self.component("ai")
            self.assertEqual(c["status"], expected, answer)
            self.assertEqual({k: c["details"][k] for k in answer}, answer)  # the banner reads the /api/ai/status shape from here

    def test_ai_offline_explains_the_supervisor_restart(self):
        self.ai_answer = AI_OFFLINE
        path = settings.BASE_DIR / "sentinel" / "tests" / "supervisor-health-test.json"
        self.addCleanup(path.unlink, missing_ok=True)
        with mock.patch.object(health, "SUPERVISOR_STATE", path):
            for heartbeat, ai_entry, text in [
                (timezone.now(), {"state": "backoff", "exitCode": 15, "restarts": 1, "nextRestartAt": "2026-11-02T10:00:02+00:00"},
                 "Restarting: it exited (code 15); the supervisor restarts it at 2026-11-02T10:00:02+00:00."),  # no lastFailure: older file
                (timezone.now(), {"state": "failed", "exitCode": 1, "restarts": 5}, "The supervisor stopped restarting it after 5 restarts."),
                (timezone.now(), {"state": "unresponsive", "restarts": 0, "unresponsiveSince": "2026-11-02T10:00:00+00:00"},
                 "Unresponsive since 2026-11-02T10:00:00+00:00: the supervisor restarts it if it stays silent."),
                (timezone.now(), {"state": "backoff", "exitCode": 1, "restarts": 0, "nextRestartAt": "2026-11-02T10:00:32+00:00",
                                  "lastFailure": "hung (3 health checks in a row got no answer, none for 30 s (last: no answer (TimeoutError)))"},
                 "Restarting: it hung (3 health checks in a row got no answer, none for 30 s (last: no answer (TimeoutError))); "
                 "the supervisor restarts it at 2026-11-02T10:00:32+00:00."),
                (timezone.now(), {"state": "starting", "restarts": 1, "lastRestartAt": "2026-11-02T10:00:32+00:00", "lastFailure": "hung (x)"},
                 "Recovering: restarted at 2026-11-02T10:00:32+00:00 after it hung (x); loading its models."),
                (timezone.now(), {"state": "starting", "restarts": 0}, "The supervisor is starting it."),
                (timezone.now(), {"state": "failed", "exitCode": 1, "restarts": 5, "lastFailure": "hung (x)"},
                 "Recovery failed: it hung (x). The supervisor stopped restarting it after 5 restarts."),
                (timezone.now() - timedelta(minutes=5), {"state": "backoff", "exitCode": 15}, None)]:  # an old file: no supervisor
                path.write_text(json.dumps({"heartbeat": heartbeat.isoformat(), "children": {"ai": ai_entry}}), encoding="utf-8")
                c = self.component("ai")
                self.assertEqual(c["status"], "offline")
                if text:
                    self.assertTrue(c["message"].endswith(text), c["message"])
                    self.assertEqual(c["details"]["supervisor"]["state"], ai_entry["state"])
                else:
                    self.assertEqual((c["message"], c["details"]["supervisor"]), (AI_OFFLINE["error"], None))

            self.ai_answer = ai()  # recovered: ready again, the last failure and restart time stay visible
            path.write_text(json.dumps({"heartbeat": timezone.now().isoformat(), "children": {"ai": {
                "state": "ready", "restarts": 1, "lastRestartAt": "2026-11-02T10:00:32+00:00", "lastFailure": "hung (x)"}}}), encoding="utf-8")
            c = self.component("ai")
            self.assertEqual((c["status"], c["message"]), ("healthy", "Ready: all models loaded. Restarted 1 time(s), last at "
                                                                      "2026-11-02T10:00:32+00:00 after it hung (x)."))
            self.assertEqual(c["details"]["supervisor"]["lastFailure"], "hung (x)")

    def test_models_loaded_missing_warm_up_failure_and_unknown_while_the_ai_is_offline(self):
        self.assertEqual(self.component("models")["status"], "healthy")
        missing = {**MODELS[1], "loaded": False, "error": "file not found"}
        self.ai_answer = ai("degraded", models=[MODELS[0], missing])
        c = self.component("models")
        self.assertEqual((c["status"], c["message"]), ("degraded", "Not loaded: fire_smoke: file not found"))
        warm_up = {**MODELS[0], "loaded": False, "error": "yolox_tiny.onnx: warm-up failed, the model does not accept a 416x416 input"}
        self.ai_answer = ai("no-model", models=[warm_up])
        c = self.component("models")
        self.assertEqual(c["status"], "offline")
        self.assertIn("warm-up failed", c["message"])
        self.assertEqual(c["details"]["models"][0]["loaded"], False)
        self.ai_answer = ai("loading", models=[])
        self.assertEqual(self.component("models")["status"], "loading")
        self.ai_answer = AI_OFFLINE
        self.assertEqual(self.component("models")["status"], "unknown")

    # ---------------------------------------------------------------- cameras
    def cameras(self) -> dict:
        c = self.component("cameras")
        return c, {e["id"]: (e["state"], e["status"]) for e in c["details"]["cameras"]}

    def test_each_camera_has_its_own_state(self):
        now = timezone.now()
        CameraFeed.objects.filter(pk__in=["cam-lobby", "cam-warehouse", "cam-server"]).update(stream_url="rtsp://10.0.0.5/live")
        watchdog.frame_received("cam-lobby", now - timedelta(seconds=2))
        watchdog.frame_received("cam-warehouse", now - timedelta(seconds=9))  # late, not lost yet (lost after 10 s)
        watchdog.frame_received("cam-server", now - timedelta(seconds=60))
        self.ai_answer = ai(cameras=[{"cameraId": "cam-server", "state": "error", "error": "cannot open stream source"}])
        c, states = self.cameras()
        self.assertEqual(states, {"cam-lobby": ("connected", "healthy"), "cam-warehouse": ("stale", "degraded"),
                                  "cam-server": ("disconnected", "offline"), "drone-01": ("not-monitored", "unknown")})
        self.assertEqual(c["status"], "degraded")
        self.assertIn("1 of 3 camera(s) connected.", c["message"])
        server = next(e for e in c["details"]["cameras"] if e["id"] == "cam-server")
        self.assertEqual(server["message"], "AI cannot read the stream: cannot open stream source")
        self.assertEqual(self.get()["status"], "degraded")

    def test_camera_disconnect_then_recovery_after_an_ai_restart(self):
        CameraFeed.objects.filter(pk="cam-lobby").update(stream_url="rtsp://10.0.0.5/live")
        t0 = timezone.now() - timedelta(seconds=30)
        watchdog.frame_received("cam-lobby", t0)
        with mock.patch("sentinel.alerts.camera_lost"), mock.patch.object(watchdog, "check_sensors"):
            watchdog.check(t0 + timedelta(seconds=20))  # the real watchdog marks it lost
        self.ai_answer = AI_OFFLINE  # the AI service died
        self.assertEqual(self.cameras()[1]["cam-lobby"], ("disconnected", "offline"))
        self.ai_answer = ai("loading", models=[])  # restarted, loading its models
        self.assertEqual(self.cameras()[1]["cam-lobby"], ("recovering", "loading"))
        self.ai_answer = ai(cameras=[{"cameraId": "cam-lobby", "state": "starting", "error": None}])
        self.assertEqual(self.cameras()[1]["cam-lobby"], ("recovering", "loading"))
        watchdog.frame_received("cam-lobby")  # frames resume
        c, states = self.cameras()
        self.assertEqual((states["cam-lobby"], c["status"]), (("connected", "healthy"), "healthy"))

    def test_a_new_stream_link_is_recovering_until_its_first_frame(self):
        CameraFeed.objects.filter(pk="cam-lobby").update(stream_url="rtsp://10.0.0.5/live")
        watchdog.watch_streaming_cameras(timezone.now())  # the server's start-up reconciliation
        self.assertEqual(self.cameras()[1]["cam-lobby"], ("recovering", "loading"))
        with mock.patch("sentinel.alerts.camera_lost"), mock.patch.object(watchdog, "check_sensors"):
            watchdog.check(timezone.now() + timedelta(seconds=settings.SENTINEL["CAMERA_STARTUP_GRACE_SECONDS"] + 5))
        self.assertEqual(self.cameras()[1]["cam-lobby"], ("disconnected", "offline"))  # the grace passed without a frame

    # ---------------------------------------------------------------- sensors
    def test_sensor_reporting_stale_lost_and_never_reported(self):
        now = timezone.now()
        stale = settings.SENTINEL["SENSOR_STALE_SECONDS"]
        SensorNode.objects.update(last_reading_at=now)
        SensorNode.objects.filter(pk="smoke-b1").update(last_reading_at=now - timedelta(seconds=stale + 5))
        SensorNode.objects.filter(pk="temp-b1").update(last_reading_at=now - timedelta(seconds=stale + 5))
        IncidentAlert.objects.create(source=IncidentSource.SENSOR_LOST, zone="x", severity="Medium", sensor_id="temp-b1", message="lost")
        SensorNode.objects.filter(pk="temp-c1").update(last_reading_at=None)
        before = list(SensorNode.objects.values_list("id", "current_value", "last_reading_at").order_by("id"))
        c = self.component("sensors")
        states = {e["id"]: (e["state"], e["status"]) for e in c["details"]["sensors"]}
        self.assertEqual(states, {"smoke-a1": ("reporting", "healthy"), "temp-a1": ("reporting", "healthy"), "smoke-b1": ("stale", "degraded"),
                                  "temp-b1": ("offline", "offline"), "smoke-c1": ("reporting", "healthy"), "temp-c1": ("offline", "offline")})
        self.assertEqual(c["status"], "degraded")
        lobby = next(e for e in c["details"]["sensors"] if e["id"] == "smoke-a1")
        self.assertEqual((lobby["value"], lobby["reading"], lobby["unit"]), (18, "Normal", "ppm"))
        self.assertEqual(list(SensorNode.objects.values_list("id", "current_value", "last_reading_at").order_by("id")), before)  # no reading made
        SensorNode.objects.update(last_reading_at=None)
        self.assertEqual(self.component("sensors")["status"], "offline")

    # ---------------------------------------------------------------- live updates (SSE)
    async def test_live_updates_follow_a_real_dashboard_stream(self):
        c = await sync_to_async(health.live_updates)()
        self.assertEqual(c["status"], "unknown")  # no dashboard connected: delivery cannot be confirmed
        await self.async_client.aforce_login(self.operator)
        response = await self.async_client.get("/api/events")
        stream = aiter(response.streaming_content)
        self.assertTrue((await asyncio.wait_for(anext(stream), 5)).startswith(b"event: snapshot"))
        events.activity("info", "health test")
        self.assertIn(b"health test", await asyncio.wait_for(anext(stream), 5))  # the stream resumed: the snapshot went out
        c = await sync_to_async(health.live_updates)()
        self.assertEqual((c["status"], c["details"]["subscribers"]), ("healthy", 1))

        events.last_delivery = time.monotonic() - health.DELIVERY_STALE_SECONDS - 1  # a stuck stream
        self.assertEqual((await sync_to_async(health.live_updates)())["status"], "degraded")

        pending = asyncio.ensure_future(anext(stream))  # the tab closes
        await asyncio.sleep(0.05)
        pending.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pending
        self.assertEqual((await sync_to_async(health.live_updates)())["status"], "unknown")

    # ---------------------------------------------------------------- simulator (optional)
    def test_simulator_disabled_running_stopped_and_unavailable_never_change_the_overall_status(self):
        self.workers()
        self.fresh_sensors()
        self.live_dashboard()
        c = self.component("simulator")
        self.assertEqual((c["status"], c["optional"]), ("unknown", True))
        r = self.sensor.post("/api/iot/telemetry", {"readings": [{"sensorId": "temp-a1", "value": 23.0}], "simulated": True}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.component("simulator")["status"], "healthy")
        watchdog.simulated_at = timezone.now() - timedelta(seconds=health.SIMULATED_RUNNING_SECONDS + 1)
        h = self.get()
        self.assertEqual((h["components"]["simulator"]["status"], h["status"]), ("offline", "healthy"))
        self.assertTrue(h["components"]["simulator"]["message"].startswith("Stopped"))
        with override_settings(BASE_DIR=settings.BASE_DIR / "no-such-dir"):
            h = self.get()
        self.assertEqual((h["components"]["simulator"]["message"], h["status"]), ("Unavailable: tools/simulator.py is missing.", "healthy"))
        self.assertEqual(self.post_frame(cameraId="cam-lobby", normalized=True, simulated=True, detections=[]).status_code, 202)
        self.assertEqual(self.component("simulator")["status"], "healthy")

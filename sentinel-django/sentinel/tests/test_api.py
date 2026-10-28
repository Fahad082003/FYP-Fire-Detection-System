"""API behaviour on SQL Server. Ports of ApiIntegrationTests, CameraLostAndSimulatedTests, CameraDeletionTests,
CameraStatusTests and FloorPlanAndLogTests (.NET)."""
import base64
import shutil
import tempfile
import threading
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import DatabaseError
from django.http import Http404
from django.utils import timezone
from rest_framework.test import APIClient

from sentinel import alerts, events, notifications, services
from sentinel.models import CameraFeed, FloorPlan, FloorPlanElement, IncidentAlert, IncidentLog, IncidentLogType, SmartSprinkler
from sentinel.watchdog import Watchdog, watchdog

from .base import ADMIN_EMAIL, PASSWORD, SentinelTestCase, fire

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


class ApiTests(SentinelTestCase):
    def sprinklers(self, zone):
        return [s for s in self.device.get("/api/iot/sprinklers").json() if s["zone"] == zone]

    def test_login_returns_a_bearer_token_and_wrong_password_returns_401(self):
        anonymous = APIClient()
        self.assertEqual(anonymous.post("/api/auth/login", {"email": ADMIN_EMAIL, "password": "wrong-password"}, format="json").status_code, 401)
        r = anonymous.post("/api/auth/login", {"email": ADMIN_EMAIL.upper(), "password": PASSWORD}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["user"]["role"], "Admin")
        bearer = APIClient(HTTP_AUTHORIZATION=f"Bearer {r.json()['token']}")
        self.assertEqual(bearer.get("/api/auth/me").json()["email"], ADMIN_EMAIL)
        self.assertEqual(APIClient(HTTP_AUTHORIZATION="Bearer forged").get("/api/auth/me").status_code, 401)

    def test_snapshot_requires_login_and_returns_seeded_devices(self):
        self.assertEqual(APIClient().get("/api/dashboard/snapshot").status_code, 401)
        snapshot = self.op.get("/api/dashboard/snapshot").json()
        self.assertEqual((len(snapshot["cameras"]), len(snapshot["sensors"]), len(snapshot["sprinklers"])), (4, 6, 5))
        self.assertEqual(snapshot["system"], {"level": "nominal", "openIncidents": 0, "activeSprinklers": 0})

    def test_snapshot_keeps_an_old_open_incident_behind_50_newer_ones(self):
        """The dashboard derives its banner, KPIs and alarm from the snapshot's list; an open incident must never drop out."""
        old = IncidentAlert.objects.create(zone="Ground Floor - Lobby", severity="High", message="old open",
                                           timestamp=timezone.now() - timedelta(days=1))
        IncidentAlert.objects.bulk_create([IncidentAlert(zone="Ground Floor - Lobby", message=f"newer {n}", is_resolved=True)
                                           for n in range(55)])
        ids = [i["id"] for i in self.op.get("/api/dashboard/snapshot").json()["incidents"]]
        self.assertIn(old.id, ids)
        self.assertEqual(len(ids), 51)  # the 50 most recent plus the open one

    def test_wrong_device_key_is_rejected(self):
        self.assertEqual(APIClient(HTTP_X_DEVICE_KEY="nope").get("/api/iot/sensors").status_code, 401)

    def test_critical_temperature_opens_incident_and_auto_sprinklers(self):
        r = self.device.post("/api/iot/sensors/temp-a1/readings", {"value": 80.0}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["state"], "Critical")
        self.assertTrue(any(i["sensorId"] == "temp-a1" and i["severity"] == "Critical" for i in self.incidents(resolved="false")))
        self.assertTrue(all(s["isActive"] for s in self.sprinklers("Ground Floor - Lobby")))

    def test_detection_with_humans_in_danger_creates_critical_incident(self):
        r = self.post_frame(cameraId="cam-server", frameWidth=1280, frameHeight=720, fireSeverity="Medium", detections=[
            {"category": "Fire", "confidence": 0.9, "bbox": [100, 100, 200, 200]},
            {"category": "Human", "confidence": 0.95, "bbox": [150, 150, 40, 120], "atRisk": True}])
        self.assertEqual(r.status_code, 202)
        [incident] = [i for i in self.incidents() if i["cameraId"] == "cam-server"]
        self.assertEqual(incident["severity"], "Critical")
        self.assertEqual((incident["humanCount"], incident["dangerCount"]), (1, 1))
        self.assertTrue(SmartSprinkler.objects.get(pk="spr-c1").is_active)
        self.assertEqual(notifications.jobs.qsize(), 1)  # emergency: responders are notified

    def test_invalid_detection_returns_validation_problem(self):
        r = self.post_frame(cameraId="cam-lobby", detections=[{"bbox": [1.0, 2]}])
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r["Content-Type"], "application/problem+json")
        self.assertIn("detections[0].bbox", r.json()["errors"])

    def test_sprinkler_toggle_and_mode_change(self):
        on = self.device.post("/api/iot/sprinklers/spr-b2/toggle", {"isActive": True}, format="json").json()
        self.assertTrue(on["isActive"])
        self.assertIsNotNone(on["lastTriggeredAt"])
        self.assertEqual(self.device.post("/api/iot/sprinklers/spr-b2/toggle", {"mode": "Manual"}, format="json").json()["mode"], "Manual")
        self.assertFalse(self.device.post("/api/iot/sprinklers/spr-b2/toggle").json()["isActive"])  # empty body flips
        self.assertEqual(self.device.post("/api/iot/sprinklers/nope/toggle", {}, format="json").status_code, 404)

    def test_master_switch_is_for_operators_only(self):
        self.assertEqual(self.device.post("/api/iot/sprinklers/master", {"isActive": True}, format="json").status_code, 401)
        r = self.op.post("/api/iot/sprinklers/master", {"isActive": True, "zone": "Ground Floor - Lobby"}, format="json").json()
        self.assertEqual(sorted(r["changed"]), ["spr-a1", "spr-a2"])

    def test_manual_dispatch_requires_operator_and_can_be_resolved(self):
        self.assertEqual(self.device.post("/api/incidents/dispatch", {"zone": "Ground Floor - Lobby"}, format="json").status_code, 401)
        r = self.op.post("/api/incidents/dispatch", {"zone": "Ground Floor - Lobby", "message": "Evacuate via east exit"}, format="json")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["source"], "Manual")
        self.assertEqual(notifications.jobs.get_nowait(), (r.json()["id"], True))  # forced: the cooldown never applies
        resolved = self.op.post(f"/api/incidents/{r.json()['id']}/resolve").json()
        self.assertTrue(resolved["isResolved"])
        self.assertEqual(self.op.post("/api/incidents/dispatch", {"zone": ""}, format="json").status_code, 400)
        # A number is not a severity: 9 would rank above Critical yet skip the Critical emergency check.
        self.assertEqual(self.op.post("/api/incidents/dispatch", {"zone": "Ground Floor - Lobby", "severity": 9}, format="json").status_code, 400)

    def test_emergency_notifications_respect_the_cooldown(self):
        self.post_frame(cameraId="cam-server", frameWidth=1280, frameHeight=720, fireSeverity="Critical",
                        detections=[{"category": "Fire", "bbox": [100, 100, 200, 200]}])
        self.op.post(f"/api/incidents/{IncidentAlert.objects.get().id}/resolve")
        cache.delete("vision-eval:cam-server")  # the per-camera 2 s evaluation throttle has passed
        self.post_frame(cameraId="cam-server", frameWidth=1280, frameHeight=720, fireSeverity="Critical",
                        detections=[{"category": "Fire", "bbox": [100, 100, 200, 200]}])
        self.assertEqual(IncidentAlert.objects.count(), 2)
        self.assertEqual(notifications.jobs.qsize(), 1)  # same source, zone and severity within 120 s: one notification

    def test_a_low_fire_right_after_smoke_still_marks_the_incident_as_fire(self):
        """D-18 (fixed; inherited from the .NET AlertEngine). Smoke alone is Medium; smoke + a Low fire is classified Low, so
        the 2 s evaluation throttle used to drop the fire frame: the incident never got FireDetected and no fire measurement
        was logged. Seen live with ai-service/samples/fire-test.avi. The first fire frame of a camera now passes the throttle."""
        smoke = {"category": "Smoke", "confidence": 0.6, "bbox": [10, 10, 200, 100]}
        self.post_frame(cameraId="cam-lobby", frameWidth=640, frameHeight=480, detections=[smoke])
        self.post_frame(cameraId="cam-lobby", frameWidth=640, frameHeight=480, fireSeverity="Low",
                        detections=[smoke, {"category": "Fire", "confidence": 0.7, "bbox": [300, 200, 40, 40]}])
        self.assertTrue(IncidentAlert.objects.get(camera_id="cam-lobby").fire_detected)

    @mock.patch.dict(settings.SENTINEL, VISION_EVALUATION_INTERVAL_SECONDS=60)  # a slow test database must not outlast the window
    def test_after_the_first_fire_frame_later_frames_follow_the_normal_throttle(self):
        """D-18 bypass is bounded: smoke -> fire -> fire -> smoke inside one throttle window evaluates only the first two frames."""
        smoke = {"category": "Smoke", "confidence": 0.6, "bbox": [10, 10, 200, 100]}

        def low_fire(width):
            self.post_frame(cameraId="cam-lobby", frameWidth=640, frameHeight=480, fireSeverity="Low",
                            detections=[smoke, {"category": "Fire", "confidence": 0.7, "bbox": [300, 200, width, 40]}])

        self.post_frame(cameraId="cam-lobby", frameWidth=640, frameHeight=480, detections=[smoke])
        low_fire(40)
        incident = IncidentAlert.objects.get(camera_id="cam-lobby")
        self.assertEqual((incident.fire_detected, incident.severity, incident.fire_severity), (True, "Medium", "Low"))

        low_fire(80)  # a bigger fire box would log a new measurement if it were evaluated
        self.post_frame(cameraId="cam-lobby", frameWidth=640, frameHeight=480, detections=[smoke])
        self.assertEqual(IncidentAlert.objects.get(pk=incident.pk).updated_at, incident.updated_at)  # both frames throttled
        self.assertEqual(IncidentLog.objects.filter(incident=incident, type=IncidentLogType.FIRE_MEASUREMENT).count(), 1)
        self.assertEqual(IncidentAlert.objects.count(), 1)
        self.assertEqual(notifications.jobs.qsize(), 0)  # a Low fire is no emergency: nothing is sent

    def test_resolving_a_stale_copy_keeps_the_escalation_that_happened_since(self):
        """resolve_incident re-reads the incident under the alert engine's lock instead of saving an old copy over it."""
        self.post_frame(**fire("cam-server", "Medium"))
        stale = IncidentAlert.objects.get()
        self.post_frame(**fire("cam-server", "Critical"))  # escalates the same open incident
        services.resolve_incident(stale, "test")
        incident = IncidentAlert.objects.get()
        self.assertEqual((incident.is_resolved, incident.severity), (True, "Critical"))

    def _measurements(self, incident, points, now):
        IncidentLog.objects.bulk_create([IncidentLog(incident=incident, type=IncidentLogType.FIRE_MEASUREMENT, zone=incident.zone,
                                                     fire_area_percent=v, timestamp=now - timedelta(seconds=s)) for s, v in points])

    def test_fire_trend_from_noisy_unevenly_spaced_measurements(self):
        now = timezone.now()
        cases = {  # (seconds ago, % of frame): jumpy boxes, uneven gaps
            "Increasing": [(80, 1.0), (71, 1.6), (69, 1.1), (40, 2.0), (12, 2.4), (1, 3.0)],
            "Stable": [(80, 2.0), (71, 2.3), (69, 1.8), (40, 2.1), (12, 2.0), (1, 2.2)],
            "Decreasing": [(80, 4.0), (71, 3.5), (69, 3.8), (40, 3.0), (12, 2.2), (1, 2.0)],
            "Insufficient data": [(30, 1.0), (20, 2.0), (10, 3.0)],  # 3 points only
        }
        for expected, points in cases.items():
            incident = IncidentAlert.objects.create(zone="Ground Floor - Lobby", severity="Medium", message=expected, fire_detected=True)
            self._measurements(incident, points, now)
            with self.subTest(expected):
                self.assertEqual(alerts.fire_trend(incident, now)["trend"], expected)
        old = IncidentAlert.objects.create(zone="Ground Floor - Lobby", severity="Medium", message="old", fire_detected=True)
        self._measurements(old, [(400, 1.0), (380, 2.0), (360, 3.0), (340, 4.0)], now)  # no recent detection: not "Decreasing"
        self.assertEqual(alerts.fire_trend(old, now)["trend"], "Insufficient data")
        quick = IncidentAlert.objects.create(zone="Ground Floor - Lobby", severity="Medium", message="quick", fire_detected=True)
        self._measurements(quick, [(8, 1.0), (6, 2.0), (4, 3.0), (2, 4.0)], now)  # 4 points but only 6 s apart
        self.assertEqual(alerts.fire_trend(quick, now)["trend"], "Insufficient data")

    def test_sustained_fire_growth_warns_once_and_reading_the_trend_never_warns(self):
        def box(w):
            return {"cameraId": "cam-lobby", "frameWidth": 640, "frameHeight": 480, "fireSeverity": "Low",
                    "detections": [{"category": "Fire", "confidence": 0.9, "bbox": [100, 100, w, w]}]}

        self.post_frame(**box(60))  # 1.17 % of the frame
        incident = IncidentAlert.objects.get()
        self._measurements(incident, [(70, 0.5), (50, 0.7), (30, 0.9)], timezone.now())  # earlier, smaller boxes
        growth = IncidentLog.objects.filter(incident=incident, type=IncidentLogType.FIRE_GROWTH)
        for w in (150, 170):  # 7.3 %, then 9.4 %: a sustained increase
            cache.delete("vision-eval:cam-lobby")  # the 2 s evaluation throttle has passed
            self.post_frame(**box(w))
        self.assertEqual(growth.count(), 1)  # deduplicated: one warning per incident
        self.assertIn("not physical size", growth.get().details)
        for _ in range(3):  # the dashboard polls the trend
            self.assertEqual(self.op.get(f"/api/incidents/{incident.id}").json()["fireTrend"]["trend"], "Increasing")
        self.assertEqual(growth.count(), 1)
        self.assertEqual(notifications.jobs.qsize(), 0)  # a warning, not an emergency notification

    def test_simulated_sensor_alarm_is_labelled_and_never_opens_sprinklers(self):
        r = self.device.post("/api/iot/telemetry", {"readings": [{"sensorId": "temp-b1", "value": 80.0}], "simulated": True}, format="json")
        self.assertEqual(r.status_code, 200)
        [incident] = [i for i in self.incidents() if i["sensorId"] == "temp-b1"]
        self.assertEqual(incident["severity"], "Critical")
        self.assertIn("[SIMULATED DATA]", incident["message"])
        self.assertFalse(any(s["isActive"] for s in self.sprinklers("Ground Floor - Warehouse")))
        # Control: the same reading without the flag is real data and opens the zone's Auto relays.
        self.device.post("/api/iot/telemetry", {"readings": [{"sensorId": "temp-b1", "value": 81.0}]}, format="json")
        self.assertTrue(any(s["isActive"] for s in self.sprinklers("Ground Floor - Warehouse")))

    def test_extreme_smoke_reading_raises_a_critical_alert_instead_of_being_rejected(self):
        self.device.post("/api/iot/telemetry", {"readings": [{"sensorId": "smoke-a1", "value": 250_000.0}]}, format="json")
        self.assertEqual([i["severity"] for i in self.incidents() if i["sensorId"] == "smoke-a1"], ["Critical"])
        bad = self.device.post("/api/iot/telemetry", {"readings": [{"sensorId": "smoke-a1", "value": 2_000_000.0}]}, format="json")
        self.assertEqual(bad.status_code, 400)
        self.assertIn("must be between", bad.content.decode())

    def test_sensor_history_returns_readings_oldest_first(self):
        for v in (20, 30, 40):
            self.device.post("/api/iot/sensors/temp-c1/readings", {"value": v}, format="json")
        h = self.device.get("/api/iot/sensors/temp-c1/history?limit=2").json()
        self.assertEqual([r["value"] for r in h["readings"]], [30, 40])
        self.assertEqual(h["unit"], "°C")


class CameraTests(SentinelTestCase):
    def lost(self) -> list[dict]:
        return [i for i in self.incidents() if i["source"] == "CameraLost"]

    def status(self, camera_id) -> str:
        return self.op.get(f"/api/cameras/{camera_id}").json()["status"]

    def test_admin_manages_cameras_and_stream_credentials_reach_only_devices(self):
        body = {"id": "cam-gate", "name": "Main Gate", "feedType": "CCTV", "streamUrl": "rtsp://admin:s3cret@10.0.0.20/live",
                "streamFormat": "Simulated", "locationZone": "Ground Floor - Gate"}
        admin = self.admin_client
        self.assertEqual(self.device.post("/api/cameras", body, format="json").status_code, 401)  # devices cannot add cameras
        self.assertEqual(self.op.post("/api/cameras", body, format="json").status_code, 403)       # operators cannot either
        self.assertEqual(admin.post("/api/cameras", body, format="json").status_code, 201)
        self.assertEqual(admin.post("/api/cameras", body, format="json").status_code, 400)          # same id twice
        for bad in ({"id": "bad id!", "name": "x", "locationZone": "A"}, {"id": "cam-x", "name": "x", "locationZone": "Block A"},
                    {"id": "cam-x", "name": "x", "locationZone": "A", "streamUrl": "ftp://x"}):
            self.assertEqual(admin.post("/api/cameras", bad, format="json").status_code, 400, bad)

        seen = next(c for c in admin.get("/api/cameras").json() if c["id"] == "cam-gate")
        self.assertEqual(seen["streamUrl"], "rtsp://***@10.0.0.20/live")
        self.assertEqual(next(c for c in self.device.get("/api/cameras").json() if c["id"] == "cam-gate")["streamUrl"],
                         "rtsp://admin:s3cret@10.0.0.20/live")
        # Renaming with the masked link sent back keeps the saved credentials.
        renamed = admin.put("/api/cameras/cam-gate", {**body, "name": "Front Gate", "streamUrl": seen["streamUrl"]}, format="json").json()
        self.assertEqual(renamed["name"], "Front Gate")
        self.assertEqual(self.device.get("/api/cameras/cam-gate").json()["streamUrl"], "rtsp://admin:s3cret@10.0.0.20/live")

        self.assertEqual(admin.delete("/api/cameras/cam-gate").status_code, 204)
        self.assertEqual(admin.get("/api/cameras/cam-gate").status_code, 404)

    def test_deleting_a_camera_keeps_its_incidents_in_the_history(self):
        self.post_frame(**fire("cam-warehouse"))
        self.assertEqual(self.admin_client.delete("/api/cameras/cam-warehouse").status_code, 204)
        incident = IncidentAlert.objects.get()
        self.assertIsNone(incident.camera_id)  # ON DELETE SET NULL in the database, as with EF Core
        self.assertEqual(incident.zone, "Ground Floor - Warehouse")

    def test_late_frames_of_a_deleted_camera_are_refused_but_unknown_cameras_still_alert(self):
        camera = {"id": "cam-gone", "name": "Gone", "feedType": "CCTV", "streamFormat": "Simulated", "locationZone": "Ground Floor - Store"}
        self.assertEqual(self.admin_client.post("/api/cameras", camera, format="json").status_code, 201)
        self.assertEqual(self.admin_client.delete("/api/cameras/cam-gone").status_code, 204)
        self.assertEqual(self.post_frame(**fire("cam-gone")).status_code, 404)  # 404 makes the AI service stop the camera
        self.assertEqual(self.post_frame(**fire("cam-never-added")).status_code, 202)  # fail-safe: never registered still alerts

        incidents = self.incidents(resolved="false")
        [unassigned] = [i for i in incidents if i["zone"] == "Unassigned"]
        self.assertIn("cam-never-added", unassigned["message"])
        self.assertFalse(any("cam-gone" in i["message"] for i in incidents))
        self.assertEqual(self.admin_client.post("/api/cameras", camera, format="json").status_code, 201)  # added again: frames count
        self.assertEqual(self.post_frame(**fire("cam-gone")).status_code, 202)

    # Delete/frame races (lifecycle check 11b). The threads touch no database: the frames carry no detections (nothing to
    # evaluate) and the deleted "camera" is a stub, so ordering is all that is tested.
    QUIET = {"cameraId": "cam-race", "frameWidth": 640, "frameHeight": 480, "detections": []}

    def test_a_delete_waits_for_the_frame_of_that_camera_being_handled(self):
        order, inside, release = [], threading.Event(), threading.Event()
        real_publish = events.publish

        def publish(kind, data):  # the frame has passed the deleted-camera check and is about to be evaluated
            if kind == "detection":
                inside.set()
                release.wait(10)
                order.append("frame")
            real_publish(kind, data)

        camera = mock.Mock(id="cam-race", delete=lambda: order.append("deleted"))
        with mock.patch.object(events, "publish", publish):
            frame = threading.Thread(target=services.process_detection, args=(self.QUIET,))
            frame.start()
            self.assertTrue(inside.wait(10))
            delete = threading.Thread(target=services.delete_camera, args=(camera,))
            delete.start()
            delete.join(0.5)
            self.assertTrue(delete.is_alive())  # blocked: the row cannot vanish under the frame
            release.set()
            frame.join(10)
            delete.join(10)
        self.assertEqual(order, ["frame", "deleted"])

    def test_a_frame_that_arrives_during_a_delete_is_refused_after_it(self):
        deleting, release, result = threading.Event(), threading.Event(), {}

        def slow_delete():
            deleting.set()
            release.wait(10)

        def post():
            try:
                result["frame"] = services.process_detection(self.QUIET)
            except Http404:
                result["frame"] = 404

        delete = threading.Thread(target=services.delete_camera, args=(mock.Mock(id="cam-race", delete=slow_delete),))
        delete.start()
        self.assertTrue(deleting.wait(10))
        frame = threading.Thread(target=post)
        frame.start()
        frame.join(0.5)
        self.assertTrue(frame.is_alive())  # waits for the delete instead of passing the check before the marker exists
        release.set()
        delete.join(10)
        frame.join(10)
        self.assertEqual(result["frame"], 404)  # never evaluated without its camera row ("Unassigned" incident)

    def test_a_failed_delete_leaves_the_camera_working(self):
        camera = CameraFeed.objects.get(pk="cam-lobby")
        with mock.patch.object(CameraFeed, "delete", side_effect=DatabaseError("boom")):
            with self.assertRaises(DatabaseError):
                services.delete_camera(camera)
        self.assertEqual(self.post_frame(**fire("cam-lobby")).status_code, 202)  # no stale marker, lock released
        self.assertEqual(IncidentAlert.objects.get().zone, "Ground Floor - Lobby")

    def test_a_delete_waits_for_camera_lost_of_that_camera(self):
        """Watchdog race: a delete between camera_lost's lookup and its insert failed with an FK violation. Stubs only."""
        order, inside, release = [], threading.Event(), threading.Event()
        found = mock.Mock(id="cam-race", location_zone="Ground Floor - Lobby")
        found.name = "Race"

        def upsert(draft):  # camera_lost has found the camera and is about to insert its incident
            inside.set()
            release.wait(10)
            order.append("incident")
            return mock.Mock(), "created"

        lost = threading.Thread(target=alerts.camera_lost, args=("cam-race", 11))
        delete = threading.Thread(target=services.delete_camera, args=(mock.Mock(id="cam-race", delete=lambda: order.append("deleted")),))
        with mock.patch.object(alerts, "CameraFeed") as feeds, mock.patch.object(alerts, "IncidentAlert") as incidents, \
                mock.patch.object(alerts, "upsert_incident", upsert), mock.patch.object(alerts, "publish_incident"):
            feeds.objects.filter.return_value.first.return_value = found
            incidents.objects.filter.return_value.exists.return_value = False
            try:
                lost.start()
                self.assertTrue(inside.wait(10))
                delete.start()
                delete.join(0.5)
                self.assertTrue(delete.is_alive())  # blocked: the row cannot vanish under camera_lost
            finally:  # a failed assertion must not leave threads waiting or the patches gone under them
                release.set()
                lost.join(10)
                if delete.ident:  # started
                    delete.join(10)
        self.assertFalse(lost.is_alive() or delete.is_alive())
        self.assertEqual(order, ["incident", "deleted"])

    def test_silent_camera_raises_one_camera_lost_alert_high_while_its_zone_burns(self):
        self.post_frame(**fire("cam-server"))
        self.post_frame(cameraId="cam-lobby", frameWidth=1280, frameHeight=720, detections=[])
        now = timezone.now()
        watchdog.check(now + timedelta(seconds=5))  # not silent long enough
        self.assertEqual(self.lost(), [])
        watchdog.check(now + timedelta(seconds=31))
        watchdog.check(now + timedelta(seconds=40))  # still silent: no second alert
        lost = self.lost()
        self.assertEqual([i["severity"] for i in lost if i["cameraId"] == "cam-server"], ["High"])  # a fire is open in its zone
        self.assertEqual([i["severity"] for i in lost if i["cameraId"] == "cam-lobby"], ["Medium"])
        self.assertFalse(any(i["cameraId"] == "drone-01" for i in lost))  # never connected: no alarm
        self.assertIn("Camera lost", lost[0]["message"])
        # Back online, then silent again: the open camera-lost incident is updated, not duplicated.
        watchdog.frame_received("cam-server", now + timedelta(seconds=41))
        watchdog.check(now + timedelta(seconds=42))
        watchdog.check(now + timedelta(seconds=80))
        self.assertEqual(len([i for i in self.lost() if i["cameraId"] == "cam-server"]), 1)

    def test_status_goes_online_on_frames_offline_after_the_lost_timeout_and_back_online_on_reconnect(self):
        CameraFeed.objects.filter(pk="cam-lobby").update(status="Offline")  # e.g. left over from before
        self.assertEqual(self.post_frame(cameraId="cam-lobby", frameWidth=640, frameHeight=480).status_code, 202)
        now = timezone.now()
        watchdog.check(now)
        self.assertEqual(self.status("cam-lobby"), "Online")  # valid frames -> Online
        watchdog.check(now + timedelta(seconds=5))
        self.assertEqual(self.status("cam-lobby"), "Online")  # one late frame is not a lost camera
        watchdog.check(now + timedelta(seconds=31))
        self.assertEqual(self.status("cam-lobby"), "Offline")  # past CAMERA_LOST_SECONDS -> Offline (with the incident)
        self.post_frame(cameraId="cam-lobby", frameWidth=640, frameHeight=480)
        watchdog.check(timezone.now())
        self.assertEqual(self.status("cam-lobby"), "Online")  # reconnect -> Online
        self.assertTrue(any(i["cameraId"] == "cam-lobby" for i in self.lost() if not i["isResolved"]))  # reconnect does not close it

    def test_after_a_restart_a_stale_offline_status_is_corrected_by_the_first_frames(self):
        CameraFeed.objects.filter(pk="cam-server").update(status="Offline")
        restarted = Watchdog()  # a new process: nothing remembered
        restarted.check(timezone.now())
        self.assertEqual(self.status("cam-server"), "Offline")  # no frames yet: last known status kept
        restarted.frame_received("cam-server")
        restarted.check(timezone.now())
        self.assertEqual(self.status("cam-server"), "Online")

    def test_after_a_restart_a_streaming_camera_that_never_returns_is_reported_lost_after_the_grace_period(self):
        for cid, url in (("cam-dead", "rtsp://10.0.0.99/live"), ("cam-alive", "rtsp://10.0.0.98/live")):
            self.assertEqual(self.admin_client.post("/api/cameras", {
                "id": cid, "name": cid, "feedType": "CCTV", "streamUrl": url, "streamFormat": "Simulated",
                "locationZone": f"Ground Floor - {cid}"}, format="json").status_code, 201)
        restarted = Watchdog()
        restarted.frame_received("cam-alive")  # its stream came back before reconciliation: the real frame wins
        start = timezone.now()
        restarted.watch_streaming_cameras(start)
        restarted.check(start + timedelta(seconds=5))
        restarted.check(start + timedelta(seconds=30))
        self.assertNotIn("cam-dead", [i["cameraId"] for i in self.lost()])  # inside the 60 s grace period
        restarted.check(start + timedelta(seconds=61))
        lost = [i["cameraId"] for i in self.lost()]
        self.assertIn("cam-dead", lost)
        self.assertEqual(self.status("cam-dead"), "Offline")
        self.assertNotIn("cam-lobby", lost)  # demo camera without a stream link: never watched at start-up

    def test_after_a_restart_a_watched_stream_camera_stays_offline_until_its_first_frame(self):
        self.assertEqual(self.admin_client.post("/api/cameras", {
            "id": "cam-dead", "name": "cam-dead", "feedType": "CCTV", "streamUrl": "rtsp://10.0.0.99/live", "streamFormat": "Simulated",
            "locationZone": "Ground Floor - Store"}, format="json").status_code, 201)
        CameraFeed.objects.filter(pk="cam-dead").update(status="Offline")
        restarted = Watchdog()
        start = timezone.now()
        restarted.watch_streaming_cameras(start)
        restarted.check(start + timedelta(seconds=5))  # inside the grace period, no frame yet
        self.assertEqual(self.status("cam-dead"), "Offline")
        restarted.frame_received("cam-dead")
        restarted.check(timezone.now())
        self.assertEqual(self.status("cam-dead"), "Online")

    def test_frames_from_an_unknown_camera_change_nothing_and_do_not_fail(self):
        watchdog.frame_received("cam-not-registered")
        watchdog.check(timezone.now())
        watchdog.check(timezone.now() + timedelta(seconds=31))
        self.assertFalse(CameraFeed.objects.filter(pk="cam-not-registered").exists())
        self.assertEqual(self.lost(), [])


class FloorPlanTests(SentinelTestCase):
    def setUp(self):
        super().setUp()
        folder = Path(tempfile.mkdtemp(prefix="sentinel-tests-"))
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        patcher = mock.patch.dict(settings.SENTINEL, FLOORPLAN_IMAGE_DIR=folder)
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def plan(danger_x=17) -> dict:
        return {"name": "Ground floor", "building": "Warehouse", "floorNumber": 0, "widthMeters": 20, "heightMeters": 10, "elements": [
            {"type": "Corridor", "name": "Main corridor", "x": 0, "y": 4, "width": 20, "height": 2},
            {"type": "Exit", "name": "East", "x": 19, "y": 4, "width": 1, "height": 2},
            {"type": "Exit", "name": "West", "x": 0, "y": 4, "width": 1, "height": 2},
            {"type": "SafeZone", "name": "Assembly point", "x": 0, "y": 7, "width": 3, "height": 3},
            # Not marked active: it becomes active only while First Floor - Server Hall has an open incident.
            {"type": "DangerZone", "name": "Boiler room", "x": danger_x, "y": 4, "width": 1, "height": 2, "zone": "First Floor - Server Hall"}]}

    def route(self, plan_id, x, y) -> dict:
        r = self.op.post(f"/api/floorplans/{plan_id}/route", {"x": x, "y": y}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()

    def upload(self, plan_id, data, name):
        return self.op.put(f"/api/floorplans/{plan_id}/image", {"file": SimpleUploadedFile(name, data, "image/png")}, format="multipart")

    def test_floor_plan_crud_image_route_and_route_log(self):
        created = self.op.post("/api/floorplans", self.plan(), format="json")
        self.assertEqual(created.status_code, 201)
        plan = created.json()
        self.assertEqual(len(plan["elements"]), 5)
        self.assertTrue(any(e["type"] == "SafeZone" and e["name"] == "Assembly point" for e in plan["elements"]))
        self.assertEqual(self.op.post("/api/floorplans", self.plan(danger_x=25), format="json").status_code, 400)  # outside 20 x 10 m

        self.assertEqual(self.upload(plan["id"], PNG, "plan.png").status_code, 200)
        self.assertEqual(self.upload(plan["id"], b"not an image", "fake.png").status_code, 400)  # magic bytes checked
        image = self.op.get(f"/api/floorplans/{plan['id']}/image")
        self.assertEqual(image["Content-Type"], "image/png")
        self.assertEqual(b"".join(image.streaming_content), PNG)

        r1 = self.route(plan["id"], 16, 5)
        self.assertTrue(r1["found"])
        self.assertEqual(r1["exitName"], "East")
        self.assertEqual(r1["estimatedSeconds"], round(r1["distanceMeters"] / 1.2, 1))
        self.assertEqual(r1["activeDangerZones"], [])

        # An open incident in First Floor - Server Hall activates the linked danger zone -> the route switches to West.
        self.assertEqual(self.op.post("/api/incidents/dispatch", {"zone": "First Floor - Server Hall", "message": "test"}, format="json").status_code, 201)
        r2 = self.route(plan["id"], 16, 5)
        self.assertEqual(r2["exitName"], "West")
        self.assertIn("Boiler room", r2["activeDangerZones"])
        risk = self.op.get(f"/api/floorplans/{plan['id']}/risk").json()
        self.assertTrue(risk["active"])
        self.assertEqual(next(a for a in risk["areas"] if a["name"] == "Main corridor")["level"], "Burning")

        self.assertEqual(self.op.post(f"/api/floorplans/{plan['id']}/route", {"x": 50, "y": 5}, format="json").status_code, 400)
        self.route(plan["id"], 16, 5)  # identical repeat: not logged again
        logs = [entry for entry in self.op.get("/api/incidents/logs?type=EvacuationRoute").json()
                if entry["zone"].startswith(f"Floor plan #{plan['id']}")]
        self.assertEqual(len(logs), 2)
        self.assertTrue(any(entry["details"].startswith("ESTIMATE") and "exit 'East'" in entry["details"] for entry in logs))
        self.assertTrue(any(entry["details"].startswith("ESTIMATE") and "exit 'West'" in entry["details"] for entry in logs))

        updated = self.op.put(f"/api/floorplans/{plan['id']}", {**self.plan(), "name": "Renamed", "elements": self.plan()["elements"][:2]},
                              format="json").json()
        self.assertEqual((updated["name"], len(updated["elements"])), ("Renamed", 2))  # elements are replaced
        self.assertEqual(self.op.delete(f"/api/floorplans/{plan['id']}").status_code, 204)
        self.assertEqual(self.op.get(f"/api/floorplans/{plan['id']}").status_code, 404)

    def test_person_in_danger_zone_is_detected_by_backend_and_logged_once(self):
        def frame(with_fire):
            detections = [{"category": "Human", "bbox": [300, 200, 40, 120], "trackId": "p9"}]  # feet at (320, 320)
            if with_fire:
                detections.append({"category": "Fire", "bbox": [400, 200, 100, 100]})
            return {"cameraId": "cam-lobby", "frameWidth": 1280, "frameHeight": 720, "simulated": True,
                    "fireSeverity": "Critical" if with_fire else None, "detections": detections,
                    "dangerZones": [{"label": "Lobby fire area", "level": "Danger",
                                     "polygon": [[250, 150], [600, 150], [600, 400], [250, 400]]}]}

        self.assertEqual(self.post_frame(**frame(False)).json()["dangerCount"], 1)
        self.post_frame(**frame(True))  # escalates High -> Critical, same person + zone
        [incident] = [i for i in self.incidents() if i["cameraId"] == "cam-lobby"]
        self.assertEqual(incident["severity"], "Critical")
        self.assertIn("Lobby fire area", incident["message"])
        self.assertIn("[SIMULATED DATA]", incident["message"])

        logs = self.op.get(f"/api/incidents/logs?incidentId={incident['id']}").json()
        [person] = [entry for entry in logs if entry["type"] == "PersonInDanger"]  # not duplicated by frame 2
        self.assertEqual((person["personTrackId"], person["dangerZone"]), ("p9", "Lobby fire area"))
        self.assertEqual((person["personX"], person["personY"]), (320.0, 320.0))
        self.assertTrue(person["simulated"])
        self.assertEqual({"Opened", "Escalated"} <= {entry["type"] for entry in logs}, True)
        [measurement] = [entry for entry in logs if entry["type"] == "FireMeasurement"]
        self.assertEqual(measurement["fireAreaPercent"], 1.09)  # 100*100 / (1280*720) = 1.085 %
        filtered = self.op.get(f"/api/incidents/logs?incidentId={incident['id']}&type=FireMeasurement").json()
        self.assertTrue(filtered and all(entry["type"] == "FireMeasurement" for entry in filtered))
        # Simulated input never switches relays (Alerts__AutoSprinklerOnSimulatedData is off by default).
        self.assertFalse(SmartSprinkler.objects.filter(zone="Ground Floor - Lobby", is_active=True).exists())

    def test_camera_calibration_is_validated_saved_and_cleared(self):
        plan = self.op.post("/api/floorplans", self.plan(), format="json").json()
        url = "/api/cameras/cam-lobby/calibration"
        image, floor = [[100, 600], [1100, 600], [900, 200], [300, 200]], [[0, 0], [20, 0], [20, 10], [0, 10]]

        def body(points, plan_id=None):
            return {"floorPlanId": plan_id or plan["id"], "imagePoints": image, "planPoints": points}

        self.assertEqual(self.op.put(url, body([[0, 0], [10, 0], [20, 0], [0, 10]]), format="json").status_code, 400)  # 3 on one line
        self.assertEqual(self.op.put(url, body([[0, 0], [25, 0], [20, 10], [0, 10]]), format="json").status_code, 400)  # outside
        self.assertEqual(self.op.put(url, body(floor, plan_id=999_999), format="json").status_code, 400)
        self.assertEqual(self.device.put(url, body(floor), format="json").status_code, 401)

        saved = self.op.put(url, body(floor), format="json").json()
        self.assertEqual(saved["calibration"]["floorPlanId"], plan["id"])
        self.assertTrue(CameraFeed.objects.get(pk="cam-lobby").calibration.startswith('{"FloorPlanId"'))  # .NET's stored format
        listed = next(c for c in self.op.get("/api/cameras").json() if c["id"] == "cam-lobby")
        self.assertEqual(listed["calibration"]["planPoints"], floor)
        self.assertIsNone(self.op.delete(url).json()["calibration"])


class RiskWarningTests(SentinelTestCase):
    """Proximity-based risk warning: rooms/corridors within 5 m of a danger zone linked to an open AI-confirmed fire."""
    LOBBY = "Ground Floor - Lobby"
    FIRE = {"cameraId": "cam-lobby", "frameWidth": 640, "frameHeight": 480, "fireSeverity": "Low",
            "detections": [{"category": "Fire", "confidence": 0.9, "bbox": [100, 100, 60, 60]}]}

    def plan_body(self, zone=LOBBY):  # distances from the fire zone: Hall 0 m, Near room 5.0 m, Far room 6.0 m
        def room(name, x):
            return {"type": "Room", "name": name, "x": x, "y": 0, "width": 1, "height": 2}

        return {"name": "Risk floor", "building": "B", "floorNumber": 0, "widthMeters": 20, "heightMeters": 4, "elements": [
            {"type": "Corridor", "name": "Hall", "x": 0, "y": 0, "width": 20, "height": 2},
            {"type": "DangerZone", "name": "Fire zone", "x": 0, "y": 0, "width": 1, "height": 2, "zone": zone},
            room("Near room", 5), room("Far room", 6)]}

    def save_plan(self, zone=LOBBY):
        r = self.op.post("/api/floorplans", self.plan_body(zone), format="json")
        self.assertEqual(r.status_code, 201)
        return r.json()

    def warnings(self) -> list[str]:
        return list(IncidentLog.objects.filter(type=IncidentLogType.RISK_WARNING).order_by("id").values_list("details", flat=True))

    def test_warns_up_to_5_m_and_not_beyond(self):
        self.save_plan()
        self.post_frame(**self.FIRE)
        warned = self.warnings()
        self.assertEqual(len(warned), 2)
        self.assertTrue(all(w.startswith("Proximity-based risk warning (not a fire-spread prediction)") for w in warned))
        self.assertIn("corridor 'Hall' overlaps danger zone 'Fire zone'", warned[0])
        self.assertTrue(any("room 'Near room' is 5 m from" in w for w in warned))  # the 5 m boundary counts
        self.assertFalse(any("Far room" in w for w in warned))                     # 6 m: safe distance, no warning
        self.assertEqual(notifications.jobs.qsize(), 0)  # a warning, never an SMS/e-mail/push

    def test_no_duplicates_from_more_frames_polling_or_saving_the_plan_again(self):
        plan = self.save_plan()
        self.post_frame(**self.FIRE)
        cache.delete("vision-eval:cam-lobby")  # the 2 s evaluation throttle has passed
        self.post_frame(**{**self.FIRE, "detections": [{**self.FIRE["detections"][0], "bbox": [100, 100, 80, 80]}]})
        for _ in range(3):
            self.assertEqual(self.op.get(f"/api/floorplans/{plan['id']}/risk").status_code, 200)  # dashboard polling
        self.assertEqual(self.op.put(f"/api/floorplans/{plan['id']}", self.plan_body(), format="json").status_code, 200)
        self.assertEqual(alerts.warn_fire_proximity(), 0)  # elements got new ids on save; the area key did not change
        self.assertEqual(len(self.warnings()), 2)

    def test_a_plan_saved_while_the_fire_is_open_warns_server_side(self):
        self.post_frame(**self.FIRE)  # fire first, no plan yet: nothing to measure
        self.assertEqual(self.warnings(), [])
        self.save_plan()
        self.assertEqual(len(self.warnings()), 2)

    def test_camera_lost_dispatch_sensor_alarm_and_marked_zones_are_not_fire(self):
        self.save_plan()
        IncidentAlert.objects.create(source="CameraLost", zone=self.LOBBY, severity="High", message="camera lost")
        IncidentAlert.objects.create(source="Sensor", zone=self.LOBBY, severity="Critical", message="temperature alarm")
        self.assertEqual(self.op.post("/api/incidents/dispatch", {"zone": self.LOBBY, "severity": "High"}, format="json").status_code, 201)
        body = self.plan_body("Other zone")
        body["elements"][1]["isActive"] = True  # marked dangerous by hand, linked to no fire
        self.op.post("/api/floorplans", {**body, "name": "Marked floor"}, format="json")
        self.assertEqual(alerts.warn_fire_proximity(), 0)
        self.assertEqual(self.warnings(), [])

    def test_unknown_fire_position_or_unusable_plan_gives_no_warning(self):
        self.save_plan(zone="Ground Floor - Warehouse")  # this plan does not draw the lobby
        broken = FloorPlan.objects.create(name="No scale", width_meters=0, height_meters=0)
        FloorPlanElement.objects.create(floor_plan=broken, type="DangerZone", name="z", x=0, y=0, width=1, height=1, zone=self.LOBBY)
        flat = FloorPlan.objects.create(name="Flat zone", width_meters=20, height_meters=4)
        FloorPlanElement.objects.create(floor_plan=flat, type="Corridor", name="Hall", x=0, y=0, width=20, height=2)
        FloorPlanElement.objects.create(floor_plan=flat, type="DangerZone", name="z", x=0, y=0, width=0, height=2, zone=self.LOBBY)
        self.post_frame(**self.FIRE)
        self.assertEqual(self.warnings(), [])

    def test_concurrent_checks_for_one_fire_create_one_warning(self):
        """A frame and a plan save at the same moment: the check-then-insert runs under one lock per fire. Stubs only."""
        made, inside, release = [], threading.Event(), threading.Event()

        def warn_plan(fire, plan):
            if made:
                return 0  # already warned
            inside.set()
            release.wait(10)  # between the duplicate check and the insert
            made.append(plan)
            return 1

        fire = mock.Mock(id=999, is_resolved=False, fire_detected=True, zone=self.LOBBY)
        first = threading.Thread(target=alerts.warn_fire_proximity, args=(fire,), kwargs={"plans": ["plan"]})
        second = threading.Thread(target=alerts.warn_fire_proximity, args=(fire,), kwargs={"plans": ["plan"]})
        with mock.patch.object(alerts, "_warn_plan", warn_plan):
            try:
                first.start()
                self.assertTrue(inside.wait(10))
                second.start()
                second.join(0.5)
                self.assertTrue(second.is_alive())  # waits for the first check-and-insert
            finally:
                release.set()
                first.join(10)
                if second.ident:
                    second.join(10)
        self.assertEqual(len(made), 1)


class RouteComparisonApiTests(SentinelTestCase):
    """POST /api/floorplans/{id}/routes: per-exit comparison and rescue list (Improvement 1)."""
    LOBBY = "Ground Floor - Lobby"

    def plan(self, danger_zone=None):
        elements = [{"type": "Corridor", "name": "Hall", "x": 0, "y": 4, "width": 20, "height": 2},
                    {"type": "Exit", "name": "East", "x": 19, "y": 4, "width": 1, "height": 2},
                    {"type": "Exit", "name": "West", "x": 0, "y": 4, "width": 1, "height": 2},
                    {"type": "Exit", "name": "North", "x": 9, "y": 3, "width": 2, "height": 1, "isBlocked": True}]
        if danger_zone:
            elements.append(danger_zone)
        r = self.op.post("/api/floorplans", {"name": "Compare", "building": "", "floorNumber": 0, "widthMeters": 20, "heightMeters": 10,
                                             "elements": elements}, format="json")
        self.assertEqual(r.status_code, 201)
        return r.json()["id"]

    def routes(self, plan_id, starts):
        return self.op.post(f"/api/floorplans/{plan_id}/routes", {"starts": starts}, format="json")

    def test_every_exit_with_distance_time_and_status_and_nothing_is_logged(self):
        plan_id = self.plan()
        logs_before = IncidentLog.objects.count()
        for _ in range(3):  # the dashboard refreshes it
            r = self.routes(plan_id, [[16, 5], [3, 5]])
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(len(body["results"]), 2)
        exits = {e["exitName"]: e for e in body["results"][0]["exits"]}
        self.assertEqual({k: v["status"] for k, v in exits.items()}, {"East": "Available", "West": "Available", "North": "Blocked"})
        speed = body["walkingSpeedMetersPerSecond"]
        self.assertEqual(exits["East"]["estimatedSeconds"], round(exits["East"]["distanceMeters"] / speed, 1))
        self.assertIsNone(exits["North"]["estimatedSeconds"])  # no route: no time, never 0
        self.assertIsNone(exits["North"]["distanceMeters"])
        self.assertEqual(body["results"][1]["exits"][0]["exitName"], "West")  # each start has its own nearest exit
        self.assertEqual(IncidentLog.objects.count(), logs_before)  # read-only: no EvacuationRoute log, unlike /route

    def test_danger_status_comes_from_the_plan_position(self):
        plan_id = self.plan({"type": "DangerZone", "name": "Lobby fire", "x": 8, "y": 4, "width": 3, "height": 2, "zone": self.LOBBY})
        self.post_frame(cameraId="cam-lobby", fireSeverity="Low", detections=[{"category": "Fire", "confidence": 0.9, "bbox": [100, 100, 60, 60]}])
        inside, outside = self.routes(plan_id, [[9, 5], [16, 5]]).json()["results"]
        self.assertTrue(inside["startInDangerZone"])
        self.assertFalse(outside["startInDangerZone"])
        self.assertTrue(inside["found"])  # a person inside can still walk out
        self.assertNotIn("atRisk", str(inside))  # the camera's at-risk flag is not part of the plan answer

    def test_malformed_requests_are_rejected_without_crashing(self):
        plan_id = self.plan()
        for starts in (None, [], "1,2", [[1]], [["a", "b"]], [[True, 2]], [[-1, 5]], [[25, 5]], [[1, 1]] * 51):
            with self.subTest(starts=starts):
                r = self.routes(plan_id, starts)
                self.assertEqual(r.status_code, 400)
                self.assertEqual(r["Content-Type"], "application/problem+json")
        self.assertEqual(self.routes(99999, [[1, 1]]).status_code, 404)
        self.assertEqual(self.device.post(f"/api/floorplans/{plan_id}/routes", {"starts": [[1, 1]]}, format="json").status_code, 401)
        flat = FloorPlan.objects.create(name="No size", width_meters=0, height_meters=0)
        self.assertEqual(self.routes(flat.id, [[0, 0]]).status_code, 400)

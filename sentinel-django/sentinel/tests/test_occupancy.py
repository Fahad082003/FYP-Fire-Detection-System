"""Room-wise people counts (GET /api/floorplans/<id>/occupancy), sensor positions on the plan, and fire and smoke kept apart."""
from unittest import mock

from django.conf import settings
from django.core.cache import cache

from sentinel import events, occupancy
from sentinel.models import CameraFeed, CameraStatus, FloorPlan, IncidentAlert, IncidentLog, IncidentLogType

from .base import SentinelTestCase, fire

LOBBY_PX = [[0, 0], [640, 0], [640, 480], [0, 480]]  # the whole camera frame (640 x 480) ...
LOBBY_M = [[0.5, 0.5], [9.5, 0.5], [9.5, 9.5], [0.5, 9.5]]  # ... is this part of the Lobby room on the plan


class OccupancyTests(SentinelTestCase):
    def setUp(self):
        super().setUp()
        self.plan = self.op.post("/api/floorplans", {"name": "Ground", "widthMeters": 30, "heightMeters": 10, "elements": [
            {"type": "Room", "name": "Lobby", "x": 0, "y": 0, "width": 10, "height": 10},
            {"type": "Room", "name": "Office", "x": 10, "y": 0, "width": 10, "height": 10},
            {"type": "Room", "name": "Store", "x": 20, "y": 0, "width": 10, "height": 10},
            {"type": "Sensor", "name": "smoke-a1", "x": 4, "y": 4, "width": 0.6, "height": 0.6}]}, format="json").json()
        self.calibrate("cam-lobby", LOBBY_M)
        self.calibrate("cam-warehouse", [[10.5, 0.5], [19.5, 0.5], [19.5, 9.5], [10.5, 9.5]])  # Office

    def calibrate(self, camera, plan_points):
        r = self.op.put(f"/api/cameras/{camera}/calibration", {"floorPlanId": self.plan["id"], "imagePoints": LOBBY_PX,
                                                               "planPoints": plan_points}, format="json")
        self.assertEqual(r.status_code, 200, r.content)

    def people(self, camera, *hits, x=300):
        """One frame: a person per hits value (None = an untracked producer), feet at pixel (x + 20, 400)."""
        r = self.post_frame(cameraId=camera, frameWidth=640, frameHeight=480, simulated=True, detections=[
            {"category": "Human", "confidence": 0.9, "trackId": f"P{i}", "bbox": [x + i * 50, 250, 40, 150],
             **({} if h is None else {"hits": h})} for i, h in enumerate(hits)])
        self.assertEqual(r.status_code, 202, r.content)

    def rooms(self) -> dict:
        r = self.op.get(f"/api/floorplans/{self.plan['id']}/occupancy")
        self.assertEqual(r.status_code, 200, r.content)
        return {room["name"]: room for room in r.json()["rooms"]}

    def test_before_any_frame_rooms_are_unknown_never_zero(self):
        rooms = self.rooms()
        self.assertEqual({n: (r["state"], r["count"]) for n, r in rooms.items()},
                         {"Lobby": ("unknown", None), "Office": ("unknown", None), "Store": ("unknown", None)})
        self.assertEqual(rooms["Store"]["reason"], "No calibrated camera sees this room.")
        self.assertEqual(rooms["Lobby"]["reason"], "No frame from its camera yet.")
        self.assertEqual(rooms["Lobby"]["cameras"], ["cam-lobby"])

    def test_enter_stay_and_leave_count_one_tracked_person_once(self):
        self.people("cam-lobby", 1)  # seen once: not yet a tracked person
        self.assertEqual((self.rooms()["Lobby"]["state"], self.rooms()["Lobby"]["count"]), ("live", 0))
        for hits in (3, 4, 5, 6):  # tracked from the 3rd frame; standing still is still one person
            self.people("cam-lobby", hits)
            self.assertEqual(self.rooms()["Lobby"]["count"], 1)
        self.people("cam-lobby")  # walked out of view
        rooms = self.rooms()
        self.assertEqual((rooms["Lobby"]["state"], rooms["Lobby"]["count"]), ("live", 0))  # seen empty: a real 0
        self.assertEqual((rooms["Office"]["state"], rooms["Store"]["state"]), ("unknown", "unknown"))  # other rooms: no data, not 0

    def test_untracked_people_count_and_feet_outside_every_room_do_not(self):
        self.people("cam-lobby", None, None)
        self.assertEqual(self.rooms()["Lobby"]["count"], 2)
        h = occupancy.homography(LOBBY_PX, LOBBY_M)
        self.assertEqual([round(v, 6) for v in occupancy.project(h, 320, 240)], [5.0, 5.0])
        self.assertEqual([round(v, 6) for v in occupancy.project(h, 0, 480)], [0.5, 9.5])

    def test_a_person_walking_into_the_next_room_moves_the_count(self):
        self.calibrate("cam-lobby", [[0.5, 0.5], [19.5, 0.5], [19.5, 9.5], [0.5, 9.5]])  # one wide camera over Lobby and Office
        self.people("cam-lobby", 3, x=100)  # feet at pixel 120 -> 4.1 m: Lobby
        self.assertEqual((self.rooms()["Lobby"]["count"], self.rooms()["Office"]["count"]), (1, 0))
        self.people("cam-lobby", 4, x=500)  # feet at pixel 520 -> 15.9 m: Office
        self.assertEqual((self.rooms()["Lobby"]["count"], self.rooms()["Office"]["count"]), (0, 1))

    def test_two_cameras_on_one_room_are_not_added_up(self):
        self.calibrate("cam-server", LOBBY_M)  # a second camera over the same room
        self.people("cam-lobby", 3)
        self.people("cam-server", 3, 3)
        lobby = self.rooms()["Lobby"]
        self.assertEqual((lobby["count"], lobby["maxPossible"]), (2, 3))  # 2 for sure; 3 only if the cameras saw different people
        self.assertEqual(sorted(lobby["cameras"]), ["cam-lobby", "cam-server"])

    def test_old_frames_make_the_count_stale_not_zero_and_a_recovering_camera_is_live_at_once(self):
        self.people("cam-lobby", 3)
        with mock.patch.dict(settings.SENTINEL, OCCUPANCY_STALE_SECONDS=-1):  # every frame is already too old
            lobby = self.rooms()["Lobby"]
        self.assertEqual((lobby["state"], lobby["count"]), ("stale", 1))
        self.assertTrue(lobby["reason"].startswith("Camera data is old"))
        self.assertEqual(self.rooms()["Lobby"]["state"], "live")
        CameraFeed.objects.filter(pk="cam-lobby").update(status=CameraStatus.OFFLINE)  # left by an earlier outage ...
        self.people("cam-lobby", 4)  # ... and a new frame came before the watchdog writes Online back
        self.assertEqual((self.rooms()["Lobby"]["state"], self.rooms()["Lobby"]["count"]), ("live", 1))

    def test_uncalibrating_or_deleting_the_camera_forgets_its_people(self):
        self.people("cam-lobby", 3)
        self.op.delete("/api/cameras/cam-lobby/calibration")
        self.people("cam-lobby", 4)
        self.assertEqual(self.rooms()["Lobby"]["state"], "unknown")
        self.calibrate("cam-warehouse", LOBBY_M)
        self.people("cam-warehouse", 3)
        self.assertEqual(self.rooms()["Lobby"]["count"], 1)
        self.admin_client.delete("/api/cameras/cam-warehouse")
        self.assertEqual(self.rooms()["Lobby"]["state"], "unknown")

    def test_dashboards_hear_about_a_changed_count_not_every_frame(self):
        with mock.patch.object(events, "publish", wraps=events.publish) as publish:
            for hits in (3, 4, 5):
                self.people("cam-lobby", hits)
            self.people("cam-lobby")
        sent = [args[1] for args, _ in publish.call_args_list if args[0] == "occupancy"]
        self.assertEqual(sent, [{"floorPlanId": self.plan["id"], "cameraId": "cam-lobby"}] * 2)  # 1 person, then 0

    def test_only_signed_in_people_read_it_and_an_unknown_plan_is_404(self):
        self.assertEqual(self.device.get(f"/api/floorplans/{self.plan['id']}/occupancy").status_code, 401)
        self.assertEqual(self.op.get("/api/floorplans/999999/occupancy").status_code, 404)


class SensorPositionTests(SentinelTestCase):
    def save(self, elements, plan_id=None):
        body = {"name": "Ground", "widthMeters": 20, "heightMeters": 10, "elements": [
            {"type": "Corridor", "name": "Hall", "x": 0, "y": 4, "width": 20, "height": 2},
            {"type": "Exit", "name": "East", "x": 19, "y": 4, "width": 1, "height": 2}, *elements]}
        return (self.op.put(f"/api/floorplans/{plan_id}", body, format="json") if plan_id
                else self.op.post("/api/floorplans", body, format="json"))

    def sensor(self, sensor_id, x, y):
        return {"type": "Sensor", "name": sensor_id, "x": x, "y": y, "width": 0.6, "height": 0.6}

    def test_a_sensor_position_is_saved_and_moved_with_the_plan(self):
        r = self.save([self.sensor("smoke-b1", 5, 4.5)])
        self.assertEqual(r.status_code, 201, r.content)
        placed = [e for e in r.json()["elements"] if e["type"] == "Sensor"]
        self.assertEqual([(e["name"], e["x"], e["y"]) for e in placed], [("smoke-b1", 5, 4.5)])
        moved = self.save([self.sensor("smoke-b1", 12, 4.5)], r.json()["id"]).json()
        self.assertEqual([(e["name"], e["x"]) for e in moved["elements"] if e["type"] == "Sensor"], [("smoke-b1", 12)])
        self.assertEqual(self.op.get("/api/iot/sensors/smoke-b1").json()["id"], "smoke-b1")  # the sensor API is unchanged

    def test_an_unknown_or_twice_placed_sensor_is_refused(self):
        unknown = self.save([self.sensor("smoke-zz", 5, 4.5)])
        self.assertEqual(unknown.status_code, 400)
        self.assertIn("elements[2].name", unknown.json()["errors"])
        twice = self.save([self.sensor("smoke-b1", 5, 4.5), self.sensor("smoke-b1", 8, 4.5)])
        self.assertEqual(twice.status_code, 400)
        self.assertIn("placed twice", twice.json()["errors"]["elements[3].name"][0])
        outside = self.save([self.sensor("smoke-b1", 19.8, 4.5)])
        self.assertEqual(outside.status_code, 400)

    def test_a_sensor_marker_is_not_a_wall_for_the_route(self):
        plan = self.save([]).json()
        before = self.op.post(f"/api/floorplans/{plan['id']}/route", {"x": 1, "y": 5}, format="json").json()
        self.save([self.sensor("temp-b1", 10, 4.7)], plan["id"])  # in the middle of the corridor
        after = self.op.post(f"/api/floorplans/{plan['id']}/route", {"x": 1, "y": 5}, format="json").json()
        self.assertTrue(after["found"])
        self.assertEqual(after["distanceMeters"], before["distanceMeters"])


class FireAndSmokeTests(SentinelTestCase):
    def smoke(self, camera, confidence=0.77):
        return {"cameraId": camera, "frameWidth": 1280, "frameHeight": 720, "simulated": True,
                "detections": [{"category": "Smoke", "confidence": confidence, "bbox": [0, 0, 640, 360]}]}

    def incident(self, camera):
        return next(i for i in self.incidents() if i["cameraId"] == camera)

    def logs(self, kind):
        return list(IncidentLog.objects.filter(type=kind).order_by("id"))

    def test_smoke_alone_and_fire_alone_open_their_own_incidents(self):
        self.assertEqual(self.post_frame(**self.smoke("cam-lobby")).status_code, 202)
        self.assertEqual(self.post_frame(**fire("cam-warehouse", "High")).status_code, 202)
        smoke, flame = self.incident("cam-lobby"), self.incident("cam-warehouse")
        self.assertNotEqual(smoke["id"], flame["id"])
        self.assertEqual((smoke["severity"], smoke["fireDetected"], smoke["fireSeverity"], smoke["smokeDetected"]), ("Medium", False, None, True))
        self.assertEqual((flame["fireDetected"], flame["fireSeverity"], flame["smokeDetected"]), (True, "High", False))

        [s] = self.logs(IncidentLogType.SMOKE_MEASUREMENT)
        self.assertEqual((s.incident_id, s.fire_area_percent, s.fire_severity), (smoke["id"], 25.0, None))
        self.assertIn("Smoke boxes cover 25% of the frame on Main Lobby, top confidence 0.77 [SIMULATED DATA]", s.details)
        [f] = self.logs(IncidentLogType.FIRE_MEASUREMENT)
        self.assertEqual(f.incident_id, flame["id"])
        self.assertIn("AI fire severity High, top confidence 0.90", f.details)

    def test_smoke_appearing_after_fire_is_recorded_at_once_on_the_same_incident(self):
        with mock.patch.dict(settings.SENTINEL, VISION_EVALUATION_INTERVAL_SECONDS=60):  # every frame inside one throttle window
            self.post_frame(**fire("cam-lobby"))
            both = fire("cam-lobby")
            both["detections"].append(self.smoke("cam-lobby", 0.66)["detections"][0])
            self.post_frame(**both)
        [i] = self.incidents()
        self.assertEqual((i["fireDetected"], i["smokeDetected"]), (True, True))
        [s] = self.logs(IncidentLogType.SMOKE_MEASUREMENT)
        self.assertIn("top confidence 0.66", s.details)
        first_fire = self.logs(IncidentLogType.FIRE_MEASUREMENT)[0]
        self.assertLessEqual(first_fire.timestamp, s.timestamp)  # each has its own first-seen time
        self.assertLess(first_fire.id, s.id)

    def test_the_live_event_and_the_snapshot_carry_the_smoke_flag(self):
        published = []
        with mock.patch.object(events, "publish", side_effect=lambda kind, data: published.append((kind, data))):
            self.post_frame(**self.smoke("cam-server"))
        [(_, opened)] = [(k, d) for k, d in published if k == "incident"]
        self.assertEqual((opened["action"], opened["incident"]["smokeDetected"], opened["incident"]["fireDetected"]), ("created", True, False))
        snap = next(i for i in self.op.get("/api/dashboard/snapshot").json()["incidents"] if i["cameraId"] == "cam-server")
        self.assertTrue(snap["smokeDetected"])
        detail = self.op.get(f"/api/incidents/{snap['id']}").json()
        self.assertTrue(detail["smokeDetected"])


class CrowdDensityTests(SentinelTestCase):
    """Room and exit limits on the occupancy counts, the safe alternative exit, and one Crowd incident per lasting condition."""
    WHOLE = [[0.5, 0.5], [19.5, 0.5], [19.5, 9.5], [0.5, 9.5]]  # cam-lobby's 640 x 480 frame covers the whole floor

    def setUp(self):
        super().setUp()
        self.plan = self.op.post("/api/floorplans", {"name": "Crowd", "widthMeters": 20, "heightMeters": 10, "elements": [
            {"type": "Corridor", "name": "Hall", "x": 0, "y": 0, "width": 20, "height": 10},
            {"type": "Exit", "name": "East", "x": 19, "y": 4, "width": 1, "height": 2},
            {"type": "Exit", "name": "West", "x": 0, "y": 4, "width": 1, "height": 2},
            {"type": "DangerZone", "name": "West fire", "x": 0, "y": 3, "width": 2, "height": 4, "zone": "First Floor - Server Hall"},
            {"type": "DangerZone", "name": "East fire", "x": 18, "y": 3, "width": 2, "height": 4, "zone": "Ground Floor - Warehouse"},
        ]}, format="json").json()
        self.op.put("/api/cameras/cam-lobby/calibration", {"floorPlanId": self.plan["id"], "imagePoints": LOBBY_PX, "planPoints": self.WHOLE},
                    format="json")
        self.limits({"Exit:East": 3, "Exit:West": 10, "Corridor:Hall": 5})

    def limits(self, values):
        r = self.op.put(f"/api/floorplans/{self.plan['id']}/capacities", values, format="json")
        self.assertEqual(r.status_code, 200, r.content)

    def people(self, east=0, west=0):
        """Tracked people standing near the east (x about 15 m) or west (about 5 m) end."""
        cache.delete(f"crowd-check:{self.plan['id']}")  # the 2 s check throttle
        xs = [470 + 3 * i for i in range(east)] + [130 + 3 * i for i in range(west)]
        self.post_frame(cameraId="cam-lobby", frameWidth=640, frameHeight=480, simulated=True, detections=[
            {"category": "Human", "confidence": 0.9, "trackId": f"P{i}", "hits": 5, "bbox": [x, 250, 20, 150]} for i, x in enumerate(xs)])

    def state(self):
        o = self.op.get(f"/api/floorplans/{self.plan['id']}/occupancy").json()
        return {x["name"]: x for x in o["exits"]}, o["rooms"][0], o

    def crowd(self, open_only=True):
        q = IncidentAlert.objects.filter(source="Crowd")
        return list((q.filter(is_resolved=False) if open_only else q).order_by("id").values_list("zone", flat=True))

    def test_normal_and_at_capacity_are_not_alerts(self):
        self.people(east=2)
        exits, hall, _ = self.state()
        self.assertEqual((exits["East"]["load"], exits["East"]["density"], exits["West"]["load"]), (2, "OK", 0))
        self.assertEqual((hall["count"], hall["capacity"], hall["density"]), (2, 5, "OK"))
        self.people(east=3)
        self.assertEqual(self.state()[0]["East"]["density"], "At capacity")
        self.assertEqual(self.crowd(), [])

    def test_an_overcrowded_exit_gets_the_safe_alternative_and_one_alert_until_it_clears(self):
        self.people(east=4)
        exits, _, _ = self.state()
        east = exits["East"]
        self.assertEqual((east["load"], east["density"], east["alternative"]["exitName"]), (4, "Overcrowded", "West"))
        self.assertIn("Send people to exit 'West'", east["message"])
        for _ in range(3):
            self.people(east=4)  # the same condition goes on: no new alert
        self.assertEqual(self.crowd(), ["Crowd - Exit 'East'"])
        [incident] = IncidentAlert.objects.filter(source="Crowd")
        self.assertEqual((incident.severity, incident.human_count), ("Medium", 4))
        self.assertIn("Overcrowded: 4 people heading there, limit 3. Send people to exit 'West'", incident.message)
        self.people(east=2)
        self.assertEqual(self.crowd(), [])  # back within the limit: resolved by the monitor
        self.assertTrue(IncidentLog.objects.filter(incident=incident, type=IncidentLogType.RESOLVED,
                                                   details__contains="crowd monitor").exists())

    def test_an_overcrowded_room_is_an_alert_too(self):
        self.people(east=3, west=3)
        _, hall, _ = self.state()
        self.assertEqual((hall["count"], hall["density"]), (6, "Overcrowded"))
        self.assertEqual(self.crowd(), ["Crowd - Corridor 'Hall'"])

    def test_an_exit_in_a_fire_zone_is_never_recommended(self):
        self.post_frame(**fire("cam-server"))  # opens a fire incident in "First Floor - Server Hall": the West fire zone is active
        self.people(east=4)
        east = self.state()[0]["East"]
        self.assertEqual((east["density"], east["alternative"]), ("Overcrowded", None))
        self.assertEqual(self.state()[0]["West"]["exitStatus"], "Danger")
        self.assertIn("No safe alternative route", east["message"])

    def test_with_every_exit_in_danger_the_people_have_no_safe_route(self):
        self.post_frame(**fire("cam-server"))
        self.post_frame(**fire("cam-warehouse"))  # both exits now lie in active fire zones
        self.people(east=2)
        exits, _, o = self.state()
        self.assertEqual((exits["East"]["load"], exits["West"]["load"], o["peopleWithoutSafeRoute"]), (0, 0, 2))
        [zone] = self.crowd()
        self.assertEqual(zone, "Crowd - no safe exit")
        self.assertEqual(IncidentAlert.objects.get(source="Crowd").severity, "High")

    def test_stale_or_unknown_counts_neither_alert_nor_clear(self):
        self.people(east=4)
        with mock.patch.dict(settings.SENTINEL, OCCUPANCY_STALE_SECONDS=-1):
            exits, hall, o = self.state()
            self.assertEqual((exits["East"]["load"], exits["East"]["density"], hall["density"], o["peopleWithoutSafeRoute"]),
                             (None, "Unknown", "Unknown", None))
            occupancy.evaluate_crowd(FloorPlan.objects.get(pk=self.plan["id"]))
        self.assertEqual(self.crowd(), ["Crowd - Exit 'East'"])  # still open: unknown is not "back to normal"

    def test_limits_are_validated_and_kept_per_plan(self):
        bad = self.op.put(f"/api/floorplans/{self.plan['id']}/capacities", {"Exit:North": 5, "Exit:East": 0, "Corridor:Hall": "many"}, format="json")
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(set(bad.json()["errors"]), {"Exit:North", "Exit:East", "Corridor:Hall"})
        self.assertEqual(self.op.get(f"/api/floorplans/{self.plan['id']}/capacities").json(),
                         {"Exit:East": 3, "Exit:West": 10, "Corridor:Hall": 5})
        self.limits({})
        self.people(east=9)
        self.assertEqual(self.state()[0]["East"]["density"], "No limit set")
        self.assertEqual(self.crowd(), [])

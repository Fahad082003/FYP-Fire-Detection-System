"""Pure rules, no database. Ports of AlertRulesTests.cs and EvacuationRouterTests.cs."""
from django.test import SimpleTestCase

from sentinel import geometry
from sentinel.models import FloorPlanElement, SensorNode, SensorType
from sentinel.rules import classify_detection, is_emergency, sensor_state
from sentinel.validation import PayloadError, validate


def frame(fire=False, fire_severity=None, humans=0, danger=0, smoke=False) -> dict:
    return {"fireDetected": fire, "fireSeverity": fire_severity, "humanCount": humans, "dangerCount": danger, "smokeDetected": smoke}


class AlertRulesTests(SimpleTestCase):
    def test_smoke_without_flames_is_medium_but_not_an_emergency(self):
        self.assertEqual(classify_detection(frame(smoke=True)), "Medium")
        self.assertFalse(is_emergency(frame(smoke=True)))
        self.assertEqual(classify_detection(frame(fire=True, fire_severity="High", smoke=True)), "High")

    def test_nothing_alertworthy_returns_none(self):
        self.assertIsNone(classify_detection(frame(humans=5)))

    def test_fire_without_people_uses_ai_fire_severity(self):
        for given, expected in (("Low", "Low"), ("High", "High"), (None, "Medium")):
            with self.subTest(given=given):
                self.assertEqual(classify_detection(frame(fire=True, fire_severity=given)), expected)

    def test_fire_with_people_in_view_is_at_least_high(self):
        self.assertEqual(classify_detection(frame(fire=True, fire_severity="Low", humans=2)), "High")

    def test_fire_with_humans_in_danger_is_critical(self):
        self.assertEqual(classify_detection(frame(fire=True, fire_severity="Low", humans=2, danger=1)), "Critical")

    def test_danger_without_fire_scales_with_count(self):
        self.assertEqual(classify_detection(frame(danger=1)), "High")
        self.assertEqual(classify_detection(frame(danger=3)), "Critical")

    def test_emergency_when_danger_or_critical_fire(self):
        self.assertTrue(is_emergency(frame(danger=1)))
        self.assertTrue(is_emergency(frame(fire=True, fire_severity="Critical")))
        self.assertFalse(is_emergency(frame(fire=True, fire_severity="High", humans=3)))

    def test_temperature_states(self):
        for value, expected in ((10, "Normal"), (46, "Warning"), (57, "Alarm"), (71.25, "Critical")):
            with self.subTest(value=value):
                self.assertEqual(sensor_state(SensorNode(type=SensorType.TEMPERATURE, threshold=57, current_value=value)), expected)

    def test_validator_derives_counts_from_detections(self):
        f = validate({"cameraId": "cam-1", "detections": [
            {"category": "Fire", "bbox": [1, 1, 10, 10]},
            {"category": "Human", "bbox": [5, 5, 2, 6], "atRisk": True},
            {"category": "Human", "bbox": [50, 5, 2, 6]}]})
        self.assertTrue(f["fireDetected"])
        self.assertEqual((f["humanCount"], f["dangerCount"]), (2, 1))

    def test_validator_rejects_bad_bbox_and_heatmap(self):
        with self.assertRaises(PayloadError) as ctx:
            validate({"cameraId": "cam 1!", "detections": [{"bbox": [1, 2]}], "heatmap": {"rows": 2, "cols": 2, "values": [0.1]}})
        self.assertTrue({"cameraId", "detections[0].bbox", "heatmap.values"} <= ctx.exception.errors.keys())

    def test_backend_marks_person_inside_danger_zone_but_not_inside_safe_zone(self):
        f = validate({"cameraId": "cam-1", "dangerCount": 0, "detections": [  # the producer says nobody is in danger
            {"category": "Human", "bbox": [100, 100, 20, 50], "trackId": "p1"},  # feet (110, 150) in danger
            {"category": "Human", "bbox": [500, 100, 20, 50], "trackId": "p2"}],  # feet (510, 150) in the safe zone
            "dangerZones": [
                {"label": "Fire area", "level": "Danger", "polygon": [[50, 50], [200, 50], [200, 200], [50, 200]]},
                {"label": "Exit B", "level": "Safe", "polygon": [[450, 50], [600, 50], [600, 200], [450, 200]]}]})
        self.assertEqual(f["dangerCount"], 1)
        self.assertTrue(f["detections"][0]["atRisk"])
        self.assertEqual(f["detections"][0]["dangerZone"], "Fire area")
        self.assertFalse(f["detections"][1]["atRisk"])
        self.assertIn("Safe", [z["level"] for z in f["dangerZones"]])

    def test_nan_literal_and_bool_numbers_are_rejected(self):
        for payload in ({"cameraId": "c", "frameWidth": True}, {"cameraId": "c", "detections": [{"bbox": [1, 2, 3, float("nan")]}]},
                        {"cameraId": "c", "detections": [{"bbox": [1, 2, 3, 4], "confidence": float("inf")}]}):
            with self.subTest(payload=payload), self.assertRaises(PayloadError):
                validate(payload)


def el(type_, x, y, w, h, name="", blocked=False) -> FloorPlanElement:
    return FloorPlanElement(type=type_, x=x, y=y, width=w, height=h, name=name, is_blocked=blocked)


def corridor():
    """20 x 10 m: one corridor, exit "East" at the right end and exit "West" at the left end."""
    return [el("Corridor", 0, 4, 20, 2), el("Exit", 19, 4, 1, 2, "East"), el("Exit", 0, 4, 1, 2, "West")]


def three_rooms():
    """30 x 10 m: Hall A | wall with a door | Hall B | solid wall | Store (no door: fire cannot reach it this way)."""
    return [el("Room", 0, 0, 10, 10, "Hall A"), el("Room", 10, 0, 10, 10, "Hall B"), el("Room", 20, 0, 10, 10, "Store"),
            el("Wall", 9.9, 0, 0.2, 10), el("Door", 9.8, 4, 0.4, 2, "Door AB"), el("Wall", 19.9, 0, 0.2, 10)]


class EvacuationTests(SimpleTestCase):
    def area(self, risk, name):
        return next(a for a in risk["areas"] if a["name"] == name)

    def test_fire_risk_ranks_rooms_by_distance_through_doors_and_walls_block_it(self):
        fire_zone = el("DangerZone", 1, 1, 2, 2, "Fire")
        risk = geometry.fire_risk(30, 10, [*three_rooms(), fire_zone], [fire_zone])
        self.assertTrue(risk["active"])
        self.assertEqual(self.area(risk, "Hall A")["level"], "Burning")
        hall_b = self.area(risk, "Hall B")
        self.assertTrue(6 <= hall_b["distanceMeters"] <= 10)  # (3, 3) -> door at x~10, y 4-6
        self.assertEqual(hall_b["level"], "Medium")
        self.assertEqual(self.area(risk, "Store")["level"], "None")  # behind a solid wall
        self.assertIsNone(self.area(risk, "Store")["distanceMeters"])
        self.assertIn("Hall B", risk["message"])
        self.assertEqual(len(risk["cells"]), risk["cols"] * risk["rows"])
        self.assertIn(4, risk["cells"])  # burning cells exist for the heatmap

    def test_fire_risk_is_inactive_without_an_active_danger_zone(self):
        risk = geometry.fire_risk(30, 10, three_rooms(), [])
        self.assertFalse(risk["active"])
        self.assertEqual(risk["areas"], [])

    def test_finds_nearest_exit_with_plausible_distance(self):
        r = geometry.find_route(20, 10, corridor(), [], 16, 5)
        self.assertTrue(r["found"])
        self.assertEqual(r["exitName"], "East")
        self.assertTrue(2.5 <= r["distanceMeters"] <= 4)

    def test_avoids_active_danger_zone_by_taking_the_other_exit(self):
        danger = el("DangerZone", 17, 4, 1, 2, "Fire")
        r = geometry.find_route(20, 10, [*corridor(), danger], [danger], 16, 5)
        self.assertTrue(r["found"])
        self.assertEqual(r["exitName"], "West")
        self.assertFalse(any(17 <= p[0] <= 18 for p in r["path"]))

    def test_wall_with_blocked_door_makes_exit_unreachable_but_open_door_lets_route_through(self):
        def plan(blocked):
            return [el("Room", 0, 0, 10, 10), el("Room", 10, 0, 10, 10), el("Wall", 9.9, 0, 0.2, 10),
                    el("Door", 9.8, 4, 0.4, 2, "D1", blocked), el("Exit", 19, 4, 1, 2, "Main")]
        closed = geometry.find_route(20, 10, plan(True), [], 2, 5)
        self.assertFalse(closed["found"])
        self.assertIn("No safe route", closed["message"])
        opened = geometry.find_route(20, 10, plan(False), [], 2, 5)
        self.assertTrue(opened["found"])
        self.assertTrue(all(4 <= p[1] <= 6 for p in opened["path"]))  # through the door gap, never the solid wall
        self.assertTrue(17 <= opened["distanceMeters"] <= 18.5)

    def test_reports_missing_exit_and_unwalkable_start(self):
        self.assertIn("no open emergency exit", geometry.find_route(20, 10, [el("Corridor", 0, 4, 20, 2)], [], 5, 5)["message"])
        outside = geometry.find_route(20, 10, corridor(), [], 5, 1)
        self.assertFalse(outside["found"])
        self.assertIn("not inside a room", outside["message"])

    def test_clearance_keeps_route_away_from_danger_zone_when_room_allows_it(self):
        danger = el("DangerZone", 10, 4, 2, 3, "Fire")
        r = geometry.find_route(20, 10, [el("Corridor", 0, 4, 20, 6), el("Exit", 19, 4, 1, 6, "East"), danger], [danger], 5, 5, 1)
        self.assertTrue(r["found"])
        self.assertFalse(r["clearanceViolated"])
        self.assertGreaterEqual(r["minDangerGapMeters"], 1)

    def test_narrow_strip_next_to_danger_is_flagged_not_reported_as_safe(self):
        danger = el("DangerZone", 10, 4, 2, 1, "Fire")
        plan = [el("Corridor", 0, 4, 20, 2), el("Exit", 19, 4, 1, 2, "East"), danger]
        self.assertFalse(geometry.find_route(20, 10, plan, [danger], 2, 5.5)["clearanceViolated"])
        r = geometry.find_route(20, 10, plan, [danger], 2, 5.5, 1)
        self.assertTrue(r["found"])
        self.assertTrue(r["clearanceViolated"])
        self.assertLess(r["minDangerGapMeters"], 1)
        self.assertIn("NO route keeps the required 1 m clearance", r["message"])
        self.assertIn("NOT a safe route", r["message"])

    def test_clearance_prefers_far_door_and_flags_near_door_when_far_door_is_closed(self):
        def plan(far_closed):
            return [el("Room", 0, 0, 10, 10), el("Room", 10, 0, 10, 10), el("Wall", 9.9, 0, 0.2, 10),
                    el("Door", 9.8, 1, 0.4, 1.5, "Near"), el("Door", 9.8, 8, 0.4, 1.5, "Far", far_closed), el("Exit", 19, 0, 1, 10, "Main")]
        danger = el("DangerZone", 11, 0, 2, 3, "Fire")
        opened = geometry.find_route(20, 10, [*plan(False), danger], [danger], 2, 2, 1)
        self.assertTrue(opened["found"])
        self.assertFalse(opened["clearanceViolated"])
        self.assertTrue(any(p[1] >= 8 for p in opened["path"]))  # through the far door
        closed = geometry.find_route(20, 10, [*plan(True), danger], [danger], 2, 2, 1)
        self.assertTrue(closed["found"])
        self.assertTrue(closed["clearanceViolated"])

    def test_danger_zone_across_the_whole_corridor_gives_no_route(self):
        danger = el("DangerZone", 10, 4, 2, 2, "Fire")
        r = geometry.find_route(20, 10, [el("Corridor", 0, 4, 20, 2), el("Exit", 19, 4, 1, 2, "East"), danger], [danger], 2, 5, 1)
        self.assertFalse(r["found"])
        self.assertIn("No safe route", r["message"])

    def test_person_inside_danger_zone_can_still_walk_out(self):
        danger = el("DangerZone", 14, 4, 4, 2, "Fire")
        r = geometry.find_route(20, 10, [*corridor(), danger], [danger], 15, 5)
        self.assertTrue(r["found"])
        self.assertTrue(r["startInDangerZone"])

    def test_large_plan_stays_fast_enough(self):
        """Python is slower than C#: a 200 x 100 m floor must still answer quickly (rasterising per element, not per cell)."""
        import time
        rooms = [el("Room", x, 0, 20, 100, f"R{x}") for x in range(0, 200, 20)]
        walls = [el("Wall", x - 0.1, 0, 0.2, 100) for x in range(20, 200, 20)]
        doors = [el("Door", x - 0.2, 45, 0.4, 2) for x in range(20, 200, 20)]
        started = time.perf_counter()
        r = geometry.find_route(200, 100, [*rooms, *walls, *doors, el("Exit", 199, 40, 1, 20, "Far")], [], 2, 50)
        self.assertTrue(r["found"])
        self.assertLess(time.perf_counter() - started, 5)

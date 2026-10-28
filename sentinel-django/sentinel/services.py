"""Business operations behind the API and the pages (ports of CameraService, DetectionService, SensorService,
IncidentService, DashboardService, FloorPlanService and DbSeeder)."""
import logging
import math
import secrets
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models import QuerySet
from django.http import Http404
from django.utils import timezone

from . import alerts, dto, events, geometry, sprinklers, validation
from .alerts import _num
from .models import (AppUser, CameraFeed, FeedType, FloorElementType, FloorPlan, FloorPlanElement, IncidentAlert, IncidentLog,
                     IncidentLogType, IncidentSource, SensorNode, SensorReading, SensorType, SmartSprinkler, UserRole, rank)
from .rules import sensor_state
from .watchdog import watchdog

log = logging.getLogger("sentinel")


class Invalid(Exception):
    """Business-rule validation failure (400) with errors per field, like ASP.NET's ValidationProblemDetails."""

    def __init__(self, errors: dict[str, list[str]]):
        super().__init__("One or more validation errors occurred.")
        self.errors = errors


def invalid(field: str, message: str) -> Invalid:
    return Invalid({field: [message]})


def get_or_404(source, message: str, **lookup):
    """source: a model or a queryset (e.g. with select_related)."""
    query = source if isinstance(source, QuerySet) else source.objects
    obj = query.filter(**lookup).first()
    if obj is None:
        raise Http404(message)
    return obj


def _opt(key):
    return settings.SENTINEL[key]


# ---------------------------------------------------------------- cameras
# A deleted camera's id is remembered so its late frames (the AI drops it only at its next sync) are refused instead of
# opening "Unassigned" incidents. Ids that were never registered still alert (fail-safe).
# shortcut: in memory only, so a restart forgets it (audit D-12); the AI's 60 s camera sync is then the fallback.
DELETED_CAMERA_SECONDS = 600


def deleted_key(camera_id: str) -> str:
    return f"camera-deleted:{camera_id}"


def cameras():
    return CameraFeed.objects.order_by("feed_type", "name")


def save_camera(form, camera: CameraFeed | None = None) -> CameraFeed:
    """form: a valid CameraForm. Creates the camera when `camera` is None."""
    data = form.cleaned_data
    creating = camera is None
    camera = camera or CameraFeed(id=data["id"])
    camera.name, camera.location_zone, camera.stream_url = data["name"], data["location_zone"], data["stream_url"]
    camera.feed_type, camera.stream_format = data["feed_type"], data["stream_format"]
    camera.updated_at = timezone.now()
    camera.save(force_insert=creating)
    if creating:
        cache.delete(deleted_key(camera.id))  # re-added under the same id: accept its frames again
    return camera


def delete_camera(camera: CameraFeed) -> None:
    camera_id = camera.id  # delete() clears the instance's primary key
    with alerts._lock_for(f"camera:{camera_id}"):  # see process_detection; a failed delete raises before the marker is set
        camera.delete()  # the database sets IncidentAlerts.CameraId to NULL; old incidents stay in the history
        cache.set(deleted_key(camera_id), True, DELETED_CAMERA_SECONDS)


def _check_points(field: str, points, max_x: float, max_y: float) -> None:
    """4 point pairs fix the camera-to-plan mapping only if no 3 points of a set lie on one line."""
    ok = isinstance(points, list) and len(points) == 4 and all(
        isinstance(p, list) and len(p) == 2 and all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in p)
        for p in points)
    if not ok:
        raise invalid(field, "Give exactly 4 points, each [x, y].")
    if any(p[0] < 0 or p[1] < 0 or p[0] > max_x or p[1] > max_y for p in points):
        raise invalid(field, "A point lies outside the camera frame or the floor plan.")
    for skip in range(4):
        a, b, c = [p for k, p in enumerate(points) if k != skip]
        if abs((b[0] - a[0]) * (c[1] - a[1]) - (c[0] - a[0]) * (b[1] - a[1])) / 2 < 1e-6:
            raise invalid(field, "Three of the 4 points lie on one line. Pick 4 corners of a floor area.")


def save_calibration(camera: CameraFeed, calibration: dict | None) -> CameraFeed:
    if calibration is not None:
        plan_id = calibration.get("floorPlanId")
        plan = FloorPlan.objects.filter(pk=plan_id).first() if isinstance(plan_id, int) and not isinstance(plan_id, bool) else None
        if plan is None:
            raise invalid("floorPlanId", "Floor plan not found.")
        _check_points("imagePoints", calibration.get("imagePoints"), math.inf, math.inf)
        _check_points("planPoints", calibration.get("planPoints"), plan.width_meters, plan.height_meters)
        calibration = {"floorPlanId": plan.id, "imagePoints": calibration["imagePoints"], "planPoints": calibration["planPoints"]}
    camera.calibration = dto.store_calibration(calibration)
    camera.updated_at = timezone.now()
    camera.save(update_fields=["calibration", "updated_at"])
    return camera


# ---------------------------------------------------------------- detections (the AI service posts here)
def process_detection(payload) -> tuple[dict, int | None]:
    frame = validation.validate(payload)
    # delete_camera holds the same lock, so a frame is handled wholly before a delete or refused after it: never evaluated
    # without its camera row ("Unassigned" incident) or with a row that is gone by the insert.
    with alerts._lock_for(f"camera:{frame['cameraId']}"):
        if cache.get(deleted_key(frame["cameraId"])):  # 404 tells the AI service to stop this camera
            raise Http404(f"Camera '{frame['cameraId']}' was deleted; frame ignored.")
        watchdog.frame_received(frame["cameraId"])
        events.publish("detection", frame)  # overlay first: dashboards draw boxes before any database work
        incident = alerts.evaluate_detection(frame)
    return frame, incident.id if incident else None


# ---------------------------------------------------------------- sensors
MIN_READING, MAX_READING = -100, 1_000_000  # 1 000 000 ppm = 100 %, the physical maximum


def sensors():
    return SensorNode.objects.order_by("location_zone", "type", "id")


def ingest_reading(sensor_id: str, value, recorded_at=None, simulated: bool = False) -> dict:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not MIN_READING <= value <= MAX_READING:
        raise invalid("value", f"Value must be between {MIN_READING} and {MAX_READING}.")
    sensor = get_or_404(SensorNode, f"Sensor '{sensor_id}' not found.", pk=sensor_id)
    previous = sensor_state(sensor)
    at = recorded_at or timezone.now()
    sensor.current_value = round(float(value), 2)
    sensor.last_reading_at = at
    sensor.updated_at = timezone.now()
    sensor.save()
    SensorReading.objects.create(sensor=sensor, value=sensor.current_value, recorded_at=at)
    _prune_old_readings()

    result = dto.sensor(sensor)
    events.publish("telemetry", result)
    alerts.evaluate_sensor(sensor, previous, simulated)
    return result


def ingest_bulk(readings: list[dict], simulated: bool) -> dict:
    updated, errors = [], []
    for r in readings:
        try:
            ingest_reading(r["sensorId"], r["value"], None, simulated)
            updated.append(r["sensorId"])
        except Invalid as e:
            errors.append({"sensorId": r["sensorId"], "error": " ".join(m for ms in e.errors.values() for m in ms)})
        except Http404 as e:
            errors.append({"sensorId": r["sensorId"], "error": str(e)})
    return {"updated": updated, "errors": errors}


def _prune_old_readings() -> None:
    """Keeps the readings table small: deletes history older than the retention window, at most every 10 minutes."""
    if not cache.add("prune-readings", True, 600):
        return
    cutoff = timezone.now() - timedelta(hours=_opt("SENSOR_HISTORY_RETENTION_HOURS"))
    deleted, _ = SensorReading.objects.filter(recorded_at__lt=cutoff).delete()
    if deleted:
        log.info("Pruned %s sensor readings older than %s", deleted, cutoff)


def sensor_history(sensor: SensorNode, limit: int, since) -> dict:
    query = SensorReading.objects.filter(sensor=sensor)
    if since is not None:
        query = query.filter(recorded_at__gte=since)
    rows = list(query.order_by("-recorded_at")[:max(1, min(limit, 2000))])
    return {"sensorId": sensor.id, "unit": sensor.unit,
            "readings": [{"value": r.value, "recordedAt": r.recorded_at} for r in reversed(rows)]}


# ---------------------------------------------------------------- incidents
def list_incidents(resolved: bool | None, min_severity: str | None, limit: int) -> list[dict]:
    query = IncidentAlert.objects.select_related("camera")
    if resolved is not None:
        query = query.filter(is_resolved=resolved)
    if min_severity is not None:
        query = query.filter(severity__in=[s for s in ("Low", "Medium", "High", "Critical") if rank(s) >= rank(min_severity)])
    return [dto.incident(i) for i in query.order_by("-timestamp")[:max(1, min(limit, 500))]]


def list_logs(incident_id: int | None, log_type: str | None, limit: int) -> list[dict]:
    query = IncidentLog.objects.all()
    if incident_id is not None:
        query = query.filter(incident_id=incident_id)
    if log_type is not None:
        query = query.filter(type=log_type)
    return [dto.log(entry) for entry in query.order_by("-timestamp", "-id")[:max(1, min(limit, 1000))]]


def resolve_incident(incident: IncidentAlert, actor: str) -> IncidentAlert:
    # The alert engine updates open incidents under this lock with a full save: without it (and a fresh copy) a frame
    # arriving during the resolve wrote is_resolved=False back, and a stale copy undid the frame's escalation.
    with alerts._lock_for(f"{incident.source}:{incident.zone}:{incident.camera_id or incident.sensor_id or '-'}"):
        incident.refresh_from_db()
        if incident.is_resolved:
            return incident
        incident.is_resolved = True
        incident.resolved_at = incident.updated_at = timezone.now()
        incident.save()
    events.publish("incident", {"action": "resolved", "incident": dto.incident(incident)})
    events.activity("success", f"Incident #{incident.id} resolved by {actor}", incident.zone)
    alerts.add_log(IncidentLog(incident=incident, type=IncidentLogType.RESOLVED, zone=incident.zone, details=f"Resolved by {actor}"))
    return incident


def dispatch(zone: str, message: str | None, severity: str, activate_sprinklers: bool, actor: str) -> IncidentAlert:
    """Manual emergency dispatch: opens an incident and notifies responders immediately (the cooldown is bypassed)."""
    incident = IncidentAlert.objects.create(
        source=IncidentSource.MANUAL, zone=zone.strip(), severity=severity,
        message=message.strip() if message and message.strip() else f"Manual emergency dispatch initiated by {actor}")
    alerts.publish_incident(incident, "created", emergency=False, force_notify=True)
    if activate_sprinklers:
        sprinklers.set_all(True, incident.zone, actor, f"dispatch #{incident.id}")
    return incident


# ---------------------------------------------------------------- dashboard
def snapshot() -> dict:
    """The whole dashboard state in one call (first load and every reconnect)."""
    sensor_list = list(sensors())
    sensor_dtos = []
    for s in sensor_list:
        history = list(SensorReading.objects.filter(sensor=s).order_by("-recorded_at")
                       .values_list("value", flat=True)[:_opt("SENSOR_HISTORY_POINTS")])
        sensor_dtos.append(dto.sensor(s, history[::-1]))
    sprinkler_list = list(SmartSprinkler.objects.order_by("zone", "id"))
    open_severities = list(IncidentAlert.objects.filter(is_resolved=False).values_list("severity", flat=True))
    states = {sensor_state(s) for s in sensor_list}
    level = ("critical" if any(rank(s) >= rank("High") for s in open_severities) or states & {"Alarm", "Critical"}
             else "elevated" if open_severities or "Warning" in states else "nominal")
    # Every open incident (the dashboard derives its banner, KPIs and alarm from this list) plus the 50 most recent ones.
    q = IncidentAlert.objects.select_related("camera").order_by("-timestamp")
    incidents = {i.id: i for i in [*q.filter(is_resolved=False), *q[:50]]}
    return {
        "cameras": [dto.camera(c) for c in cameras()],
        "sensors": sensor_dtos,
        "sprinklers": [dto.sprinkler(s) for s in sprinkler_list],
        "incidents": [dto.incident(i) for i in sorted(incidents.values(), key=lambda i: i.timestamp, reverse=True)],
        "system": {"level": level, "openIncidents": len(open_severities), "activeSprinklers": sum(s.is_active for s in sprinkler_list)},
        "serverTime": timezone.now(),
    }


# ---------------------------------------------------------------- floor plans
def floor_plans():
    return FloorPlan.objects.prefetch_related("elements").order_by("building", "floor_number", "name")


@transaction.atomic
def save_floor_plan(plan: FloorPlan | None, data: dict) -> FloorPlan:
    """data: a validated SaveFloorPlan body (camelCase). Elements replace the plan's current elements."""
    width, height = data["widthMeters"], data["heightMeters"]
    errors = {}
    for i, e in enumerate(data["elements"]):
        if e["type"] not in FloorElementType.values:
            errors[f"elements[{i}].type"] = ["Unknown element type."]
        elif not all(math.isfinite(e[k]) for k in ("x", "y", "width", "height")) or e["width"] <= 0 or e["height"] <= 0:
            errors[f"elements[{i}]"] = ["Width and height must be positive numbers."]
        elif e["x"] < 0 or e["y"] < 0 or e["x"] + e["width"] > width + 0.001 or e["y"] + e["height"] > height + 0.001:
            errors[f"elements[{i}]"] = [f"'{e['name']}' lies outside the {_num(width)} x {_num(height)} m plan."]
    if errors:
        raise Invalid(errors)

    creating = plan is None
    plan = plan or FloorPlan()
    plan.name, plan.building = data["name"].strip(), (data.get("building") or "").strip()
    plan.floor_number, plan.width_meters, plan.height_meters = data["floorNumber"], width, height
    plan.updated_at = timezone.now()
    plan.save(force_insert=creating)
    if not creating:
        FloorPlanElement.objects.filter(floor_plan=plan).delete()
    FloorPlanElement.objects.bulk_create([FloorPlanElement(
        floor_plan=plan, type=e["type"], name=(e.get("name") or "").strip(), x=e["x"], y=e["y"], width=e["width"],
        height=e["height"], is_blocked=e.get("isBlocked", False), is_active=e.get("isActive", False),
        zone=(e.get("zone") or "").strip() or None) for e in data["elements"]])
    alerts.warn_fire_proximity(plans=[plan])  # a changed plan changes the distances to an open fire
    return plan


def image_dir():
    return settings.SENTINEL["FLOORPLAN_IMAGE_DIR"]


def delete_floor_plan(plan: FloorPlan) -> None:
    _delete_image(plan.image_file_name)
    plan.delete()  # elements are removed by the database (ON DELETE CASCADE)


def _delete_image(name: str | None) -> None:
    if name:
        (image_dir() / name).unlink(missing_ok=True)


def save_floor_plan_image(plan: FloorPlan, data: bytes, content_type: str) -> FloorPlan:
    """data was checked by ai_client.check_image. The file name is chosen by the server: no user input reaches the path."""
    name = f"{plan.id}{'.png' if content_type == 'image/png' else '.jpg'}"
    image_dir().mkdir(parents=True, exist_ok=True)
    (image_dir() / name).write_bytes(data)
    if plan.image_file_name and plan.image_file_name != name:
        _delete_image(plan.image_file_name)
    plan.image_file_name, plan.updated_at = name, timezone.now()
    plan.save(update_fields=["image_file_name", "updated_at"])
    return plan


def _active_danger(elements: list[FloorPlanElement]) -> list[FloorPlanElement]:
    """A danger zone is active when marked so, or when its linked camera/sensor zone has an open incident."""
    open_zones = {z.lower() for z in IncidentAlert.objects.filter(is_resolved=False).values_list("zone", flat=True)}
    return [e for e in elements if e.type == "DangerZone" and (e.is_active or (e.zone and e.zone.strip() and e.zone.lower() in open_zones))]


def _zone_names(zones) -> list[str]:
    return [e.name if e.name and e.name.strip() else "Danger zone" for e in zones]


def route(plan: FloorPlan, x, y) -> dict:
    """Estimated evacuation route from a known start point (meters) to the nearest reachable exit; logged (requirement 5.3)."""
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in (x, y)) \
            or x < 0 or y < 0 or x > plan.width_meters or y > plan.height_meters:
        raise invalid("start", "Start point must be inside the floor plan.")
    elements = list(plan.elements.all())
    active = _active_danger(elements)
    clearance, speed = _opt("DANGER_CLEARANCE_METERS"), _opt("WALKING_SPEED")
    r = geometry.find_route(plan.width_meters, plan.height_meters, elements, active, x, y, clearance)
    seconds = round(r["distanceMeters"] / speed, 1) if r["found"] else 0

    zone = f"Floor plan #{plan.id} {plan.name}"
    f = geometry.fmt
    details = (f"ESTIMATE: route from ({f(x)}, {f(y)}) m to exit '{r['exitName']}': {f(r['distanceMeters'])} m, "
               f"~{f(seconds, 0)} s at {_num(speed)} m/s"
               + (f" | CLEARANCE VIOLATED: passes within {f(r['minDangerGapMeters'])} m of an active danger zone "
                  f"(required {f(clearance)} m)" if r["clearanceViolated"] else "")
               if r["found"] else f"No safe route from ({f(x)}, {f(y)}) m: {r['message']}")
    last = IncidentLog.objects.filter(type=IncidentLogType.EVACUATION_ROUTE, zone=zone).order_by("-id").values_list("details", flat=True).first()
    if last != details[:500]:  # skip an exact repeat of the previous estimate for this plan
        alerts.add_log(IncidentLog(type=IncidentLogType.EVACUATION_ROUTE, zone=zone, details=details))
    return {**r, "estimatedSeconds": seconds, "walkingSpeedMetersPerSecond": speed, "activeDangerZones": _zone_names(active),
            "requiredClearanceMeters": clearance}


MAX_ROUTE_STARTS = 50


def routes(plan: FloorPlan, starts) -> dict:
    """Every exit compared from each start point (meters): the dashboard's route comparison (one start) and rescue list (the
    current camera person markers). Read-only: no log, no incident, so polling it changes nothing."""
    if not (plan.width_meters > 0 and plan.height_meters > 0):
        raise invalid("plan", "The floor plan has no valid size.")
    if not isinstance(starts, list) or not 1 <= len(starts) <= MAX_ROUTE_STARTS:
        raise invalid("starts", f"Give 1 to {MAX_ROUTE_STARTS} start points, each [x, y] in meters.")
    for i, s in enumerate(starts):
        ok = isinstance(s, list) and len(s) == 2 and all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in s)
        if not ok or not (0 <= s[0] <= plan.width_meters and 0 <= s[1] <= plan.height_meters):
            raise invalid(f"starts[{i}]", "Start point must be [x, y] inside the floor plan.")
    elements = list(plan.elements.all())
    active = _active_danger(elements)
    clearance, speed = _opt("DANGER_CLEARANCE_METERS"), _opt("WALKING_SPEED")
    results = []
    for x, y in starts:
        r = geometry.find_routes(plan.width_meters, plan.height_meters, elements, active, x, y, clearance)
        for e in r["exits"]:
            e["estimatedSeconds"] = round(e["distanceMeters"] / speed, 1) if e["found"] else None  # no route: no time
        results.append({"start": [x, y], **r})
    return {"results": results, "walkingSpeedMetersPerSecond": speed, "requiredClearanceMeters": clearance,
            "activeDangerZones": _zone_names(active)}


def risk(plan: FloorPlan) -> dict:
    elements = list(plan.elements.all())
    return geometry.fire_risk(plan.width_meters, plan.height_meters, elements, _active_danger(elements))


# ---------------------------------------------------------------- first-run data
SMOKE_THRESHOLD_PPM, TEMPERATURE_THRESHOLD_C = 300, 57  # 57 °C: standard glass-bulb sprinkler activation temperature


def seed() -> None:
    """The first admin account (when Users is empty) and, if enabled, demo cameras, sensors and sprinklers."""
    if not AppUser.objects.exists():
        password = _opt("SEED_ADMIN_PASSWORD") or secrets.token_urlsafe(12)
        admin = AppUser(email=_opt("SEED_ADMIN_EMAIL").strip().lower(), display_name="Administrator", role=UserRole.ADMIN)
        admin.set_password(password)
        admin.save(force_insert=True)
        if _opt("SEED_ADMIN_PASSWORD"):
            log.info("Created admin account %s", admin.email)
        else:
            log.warning("Created admin %s with GENERATED password: %s  (set Seed__AdminPassword to choose your own; shown only once)",
                        admin.email, password)
    if not _opt("SEED_DEMO_DATA") or CameraFeed.objects.exists() or SensorNode.objects.exists() or SmartSprinkler.objects.exists():
        return
    # Zones are "Floor - Area" so an alert says where to go; cameras, sensors, sprinklers and plan danger zones share the name.
    with transaction.atomic():
        CameraFeed.objects.bulk_create([
            CameraFeed(id="cam-lobby", name="Main Lobby", location_zone="Ground Floor - Lobby"),
            CameraFeed(id="cam-warehouse", name="Warehouse Floor", location_zone="Ground Floor - Warehouse"),
            CameraFeed(id="cam-server", name="Server Hall", location_zone="First Floor - Server Hall"),
            CameraFeed(id="drone-01", name="Perimeter Drone 01", feed_type=FeedType.DRONE, location_zone="Ground Floor - Warehouse")])
        zones = [("Ground Floor - Lobby", "a"), ("Ground Floor - Warehouse", "b"), ("First Floor - Server Hall", "c")]
        SensorNode.objects.bulk_create([node for zone, s in zones for node in (
            SensorNode(id=f"smoke-{s}1", type=SensorType.SMOKE, threshold=SMOKE_THRESHOLD_PPM, current_value=18, location_zone=zone),
            SensorNode(id=f"temp-{s}1", type=SensorType.TEMPERATURE, threshold=TEMPERATURE_THRESHOLD_C, current_value=22, location_zone=zone))])
        SmartSprinkler.objects.bulk_create([SmartSprinkler(id=i, zone=z) for i, z in (
            ("spr-a1", "Ground Floor - Lobby"), ("spr-a2", "Ground Floor - Lobby"), ("spr-b1", "Ground Floor - Warehouse"),
            ("spr-b2", "Ground Floor - Warehouse"), ("spr-c1", "First Floor - Server Hall"))])
    log.info("Seeded demo data: 4 cameras, 6 sensors, 5 sprinklers")

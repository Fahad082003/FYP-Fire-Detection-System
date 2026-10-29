"""The alert engine (port of AlertEngine.cs): decides severity, opens/merges/escalates incidents and triggers the response
(notifications and automatic sprinkler activation)."""
import math
import statistics
import threading
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from . import dto, events, geometry, notifications, sprinklers
from .models import (CameraFeed, CameraStatus, FloorPlan, IncidentAlert, IncidentLog, IncidentLogType, IncidentSource, SensorNode,
                     SensorReading, SensorType, rank)
from .rules import classify_detection, is_emergency, is_stale, max_severity, sensor_state

# Per-origin locks so two frames from the same camera never create duplicate incidents.
# shortcut: in-process locks; with several server processes a database lock (UPDLOCK) would be needed.
_locks: defaultdict[str, threading.Lock] = defaultdict(threading.Lock)
_locks_guard = threading.Lock()


def _lock_for(key: str) -> threading.Lock:
    with _locks_guard:
        return _locks[key]


@dataclass
class Draft:
    source: str
    zone: str
    severity: str
    message: str
    camera_id: str | None = None
    sensor_id: str | None = None
    fire_detected: bool = False
    fire_severity: str | None = None
    human_count: int = 0
    danger_count: int = 0


def _opt(key):
    return settings.SENTINEL[key]


# ---------------------------------------------------------------- vision
def evaluate_detection(frame: dict) -> IncidentAlert | None:
    severity = classify_detection(frame)
    if severity is None:
        return None

    # Frames arrive several times a second per camera: re-evaluate at most every N seconds unless severity rises
    # or fire first appears (D-18: smoke + a Low fire ranks below smoke alone, yet the incident must learn it is a fire).
    # A person newly in danger also passes, or a second person entering within the window would never be logged.
    throttle_key = f"vision-eval:{frame['cameraId']}"
    at_risk = _people_at_risk(frame)
    last_severity, last_fire, last_risk = cache.get(throttle_key) or (None, False, frozenset())
    if last_severity is not None and rank(severity) <= rank(last_severity) and (last_fire or not frame["fireDetected"]) \
            and at_risk <= last_risk:
        return None
    cache.set(throttle_key, (max_severity(severity, last_severity), last_fire or frame["fireDetected"], last_risk | at_risk),
              _opt("VISION_EVALUATION_INTERVAL_SECONDS"))

    camera = CameraFeed.objects.filter(pk=frame["cameraId"]).first()
    zone = camera.location_zone if camera else "Unassigned"
    parts = []
    if frame["fireDetected"]:
        parts.append(f"Fire detected ({frame['fireSeverity']})" if frame["fireSeverity"] else "Fire detected")
    if frame["smokeDetected"]:
        parts.append("Smoke detected")
    danger_zones = list(dict.fromkeys(d["dangerZone"] for d in frame["detections"] if d["dangerZone"]))
    if frame["dangerCount"] > 0:
        quoted = "', '".join(danger_zones)
        where = f" '{quoted}'" if danger_zones else ""
        parts.append(f"{frame['dangerCount']} human(s) in danger zone{where}")
    if frame["humanCount"] > 0:
        parts.append(f"{frame['humanCount']} human(s) in view")
    camera_name = camera.name if camera else frame["cameraId"]
    tags = (f" [AI {frame['model']}]" if frame["model"] else "") + (" [SIMULATED DATA]" if frame["simulated"] else "")

    incident, action = upsert_incident(Draft(
        IncidentSource.VISION, zone, severity, f"{', '.join(parts)} on {camera_name}{tags}", camera.id if camera else None,
        None, frame["fireDetected"], frame["fireSeverity"], frame["humanCount"], frame["dangerCount"]))
    emergency = is_emergency(frame) or severity == "Critical"
    publish_incident(incident, action, emergency, simulated=frame["simulated"])
    _log_frame(incident, frame, camera_name)
    if incident.fire_detected:
        warn_fire_proximity(incident)
    if frame["fireDetected"]:
        corroborate(incident)
    return incident


def _people_at_risk(f: dict) -> frozenset:
    """(trackId, danger zone, n): n numbers people with the same key in this frame, so two untracked people are two entries."""
    seen = Counter()
    keys = set()
    for d in f["detections"]:
        if d["category"] == "Human" and d["atRisk"]:
            k = (d["trackId"], d["dangerZone"])
            keys.add((*k, seen[k]))
            seen[k] += 1
    return frozenset(keys)


def _union_area(boxes, fw: float, fh: float) -> float:
    """Area of [x, y, w, h] boxes inside the frame, overlaps counted once (the AI's fire_area_percent does the same)."""
    rects = [(max(0, x), max(0, y), min(fw, x + w), min(fh, y + h)) for x, y, w, h in boxes]
    rects = [r for r in rects if r[2] > r[0] and r[3] > r[1]]
    xs = sorted({v for r in rects for v in (r[0], r[2])})
    area = 0.0
    for x0, x1 in zip(xs, xs[1:]):  # vertical strips: inside one, the covered height is a union of intervals
        top, covered = -math.inf, 0.0
        for y0, y1 in sorted((r[1], r[3]) for r in rects if r[0] <= x0 and r[2] >= x1):
            if y1 > top:
                covered += y1 - max(y0, top)
                top = y1
        area += covered * (x1 - x0)
    return area


def _log_frame(incident: IncidentAlert, f: dict, camera_name: str) -> None:
    """Requirement 3.2 / 2.3: fire-measurement history and one alert per person per danger zone per incident."""
    sim = " [SIMULATED DATA]" if f["simulated"] else ""
    if f["fireDetected"]:
        # Measured value = AI fire bounding boxes as % of the frame. It is not a physical fire size.
        fw, fh = (1.0, 1.0) if f["normalized"] else (f["frameWidth"], f["frameHeight"])
        boxes = _union_area([d["bbox"] for d in f["detections"] if d["category"] == "Fire"], fw, fh)
        area = round(min(100.0, boxes / (fw * fh) * 100), 2)
        last = (IncidentLog.objects.filter(incident=incident, type=IncidentLogType.FIRE_MEASUREMENT).order_by("-id").first())
        if last is None or (last.fire_area_percent, last.fire_severity, last.human_count, last.danger_count) != (
                area, f["fireSeverity"], f["humanCount"], f["dangerCount"]):
            add_log(IncidentLog(
                incident=incident, type=IncidentLogType.FIRE_MEASUREMENT, zone=incident.zone, fire_area_percent=area,
                fire_severity=f["fireSeverity"], human_count=f["humanCount"], danger_count=f["dangerCount"],
                simulated=f["simulated"],
                details=f"Fire boxes cover {_num(area)}% of the frame on {camera_name}, AI fire severity {f['fireSeverity'] or 'n/a'}{sim}"))
            _warn_if_growing(incident, camera_name, f["simulated"])

    untracked = Counter()
    for d in f["detections"]:
        if d["category"] != "Human" or not d["atRisk"]:
            continue
        logged = IncidentLog.objects.filter(incident=incident, type=IncidentLogType.PERSON_IN_DANGER,
                                            person_track_id=d["trackId"], danger_zone=d["dangerZone"])
        if d["trackId"] is None:
            # No identity across frames: one alert per person beyond the most untracked people logged in this zone.
            untracked[d["dangerZone"]] += 1
            if logged.count() >= untracked[d["dangerZone"]]:
                continue
        elif logged.exists():
            continue  # same person + same zone + same incident = one alert
        fx, fy = round(d["bbox"][0] + d["bbox"][2] / 2, 1), round(d["bbox"][1] + d["bbox"][3], 1)
        who = f"Person #{d['trackId']}" if d["trackId"] is not None else "Untracked person"
        where = f"danger zone '{d['dangerZone']}'" if d["dangerZone"] else "a danger zone (flagged by AI)"
        details = f"{who} in {where} at ({_num(fx)}, {_num(fy)}) on {camera_name}{sim}"
        add_log(IncidentLog(incident=incident, type=IncidentLogType.PERSON_IN_DANGER, zone=incident.zone, details=details,
                            person_track_id=d["trackId"], person_x=fx, person_y=fy, danger_zone=d["dangerZone"],
                            simulated=f["simulated"]))
        events.activity("critical", f"Incident #{incident.id}: {details}", incident.zone)


# ---------------------------------------------------------------- fire growth trend
TREND_WINDOW_SECONDS = 180      # recent measurements only
TREND_MIN_POINTS, TREND_MIN_SPAN_SECONDS = 4, 20
TREND_MIN_CHANGE_POINTS, TREND_MIN_CHANGE_RATIO = 0.5, 0.2  # a change counts only if >= 0.5 %-points and >= 20 % of the level


def fire_trend(incident: IncidentAlert, now=None) -> dict:
    """Trend of the AI fire bounding-box share of the camera frame over the last TREND_WINDOW_SECONDS of FireMeasurement logs.
    Not a physical fire size and not a spread forecast. Theil-Sen slope (median of all pairwise slopes): robust to jumping
    boxes and to uneven sampling. Too few recent measurements = "Insufficient data", never "Decreasing": a missed detection
    is no proof that the fire is out."""
    since = (now or timezone.now()) - timedelta(seconds=TREND_WINDOW_SECONDS)
    points = [(m.timestamp.timestamp(), m.fire_area_percent) for m in IncidentLog.objects.filter(
        incident=incident, type=IncidentLogType.FIRE_MEASUREMENT, timestamp__gte=since, fire_area_percent__isnull=False).order_by("timestamp", "id")]
    result = {"trend": "Insufficient data", "measurements": len(points), "windowSeconds": TREND_WINDOW_SECONDS,
              "basis": "AI fire bounding-box area, % of the camera frame (not a physical size)"}
    span = points[-1][0] - points[0][0] if points else 0
    slopes = [(v2 - v1) / (t2 - t1) for i, (t1, v1) in enumerate(points) for t2, v2 in points[i + 1:] if t2 > t1]
    if len(points) < TREND_MIN_POINTS or span < TREND_MIN_SPAN_SECONDS or not slopes:
        return result
    slope = statistics.median(slopes)  # %-points per second
    change = slope * span
    threshold = max(TREND_MIN_CHANGE_POINTS, TREND_MIN_CHANGE_RATIO * statistics.median(v for _, v in points))
    trend = "Increasing" if change >= threshold else "Decreasing" if change <= -threshold else "Stable"
    return {**result, "trend": trend, "spanSeconds": round(span), "changePercentPoints": round(change, 2),
            "percentPointsPerMinute": round(slope * 60, 2), "fromPercent": points[0][1], "toPercent": points[-1][1]}


def _warn_if_growing(incident: IncidentAlert, camera_name: str, simulated: bool) -> None:
    """One FireGrowth warning per incident (runs under the camera lock, so two frames cannot both create it). Only new
    measurements call this; reading the trend (GET) never warns."""
    if IncidentLog.objects.filter(incident=incident, type=IncidentLogType.FIRE_GROWTH).exists():
        return
    t = fire_trend(incident)
    if t["trend"] != "Increasing":
        return
    details = (f"Fire growth warning on {camera_name}: AI fire box area {_num(t['fromPercent'])}% -> {_num(t['toPercent'])}% of the "
               f"frame in {t['spanSeconds']} s (+{_num(t['percentPointsPerMinute'])} %-points/min). Share of the camera image, "
               f"not physical size or a spread forecast{' [SIMULATED DATA]' if simulated else ''}")
    add_log(IncidentLog(incident=incident, type=IncidentLogType.FIRE_GROWTH, zone=incident.zone, details=details,
                        fire_area_percent=t["toPercent"], fire_severity=incident.fire_severity, simulated=simulated))
    events.activity("warning", f"Incident #{incident.id}: {details}", incident.zone)


# ---------------------------------------------------------------- proximity-based risk warning
def warn_fire_proximity(incident: IncidentAlert | None = None, plans=None) -> int:
    """Proximity-based risk warning, NOT a fire-spread prediction: a room or corridor within geometry.HIGH_METERS (5 m, through
    rooms and doors) of a floor-plan danger zone linked to the zone of an open incident with AI-confirmed fire. Only such
    incidents count; camera-lost, manual dispatch, sensor alarms and manually marked zones never do. One warning per
    (fire incident, plan, area); runs on fire frames and plan saves, never on reads. Returns the number of new warnings."""
    incidents = [incident] if incident else list(IncidentAlert.objects.filter(is_resolved=False, fire_detected=True))
    plans = list(plans if plans is not None else FloorPlan.objects.prefetch_related("elements"))
    created = 0
    for fire in incidents:
        if fire.is_resolved or not fire.fire_detected or not fire.zone.strip():
            continue
        # The check and the insert share this lock: a frame and a plan save at the same time cannot both warn.
        with _lock_for(f"risk:{fire.id}"):
            for plan in plans:
                created += _warn_plan(fire, plan)
    return created


def _warn_plan(fire: IncidentAlert, plan: FloorPlan) -> int:
    if not (plan.width_meters > 0 and plan.height_meters > 0):
        return 0  # no reliable scale: no distance, no warning
    elements = list(plan.elements.all())
    zones = [e for e in elements if e.type == "DangerZone" and (e.zone or "").strip().lower() == fire.zone.strip().lower()
             and e.width > 0 and e.height > 0]
    if not zones:
        return 0  # the fire's zone is not drawn on this plan: its position is unknown here
    risk = geometry.fire_risk(plan.width_meters, plan.height_meters, elements, zones)
    created = 0
    for area in risk["areas"]:
        if area["distanceMeters"] is None or area["distanceMeters"] > geometry.HIGH_METERS:
            continue
        x, y, w, h = area["bounds"]
        key = f"p{plan.id}:{area['type'][0]}{x:g},{y:g},{w:g},{h:g}"[:48]  # stable across plan saves (element ids are not)
        if IncidentLog.objects.filter(incident=fire, type=IncidentLogType.RISK_WARNING, danger_zone=key).exists():
            continue
        where = "overlaps" if area["distanceMeters"] <= 0 else f"is {geometry.fmt(area['distanceMeters'])} m from"
        details = (f"Proximity-based risk warning (not a fire-spread prediction): {area['type'].lower()} '{area['name']}' {where} "
                   f"danger zone '{', '.join(risk['activeDangerZones'])}' on floor plan '{plan.name}' (fire incident #{fire.id}, "
                   f"distance through rooms and doors on the drawn plan)")
        add_log(IncidentLog(incident=fire, type=IncidentLogType.RISK_WARNING, zone=fire.zone, details=details, danger_zone=key,
                            fire_severity=fire.fire_severity))
        events.activity("warning", f"Incident #{fire.id}: {details}", fire.zone)
        created += 1
    return created


def _num(v: float) -> str:
    """Number as .NET printed it: 12.5 -> "12.5", 12.0 -> "12" (shortest round-trip form, never scientific for readings)."""
    return str(int(v)) if float(v).is_integer() else str(v)


def add_log(entry: IncidentLog) -> None:
    entry.details = entry.details[:500]
    entry.save()
    events.publish("incidentLog", dto.log(entry))


# ---------------------------------------------------------------- sensors
def evaluate_sensor(sensor, previous_state: str, simulated: bool = False) -> None:
    state = sensor_state(sensor)
    if state != previous_state:
        level = {"Normal": "success", "Warning": "warning"}.get(state, "critical")
        events.activity(level, f"{sensor.type} sensor {sensor.id}: {previous_state} → {state} "
                               f"({_num(sensor.current_value)}{sensor.unit})", sensor.location_zone)
    if state not in ("Alarm", "Critical"):
        return

    severity = "Critical" if state == "Critical" else "High"
    incident, action = upsert_incident(Draft(
        IncidentSource.SENSOR, sensor.location_zone, severity,
        f"{sensor.type} {_num(sensor.current_value)}{sensor.unit} exceeds safety limit {_num(sensor.threshold)}{sensor.unit} "
        f"({sensor.id})" + (" [SIMULATED DATA]" if simulated else ""), sensor_id=sensor.id))
    publish_incident(incident, action, emergency=state == "Critical", simulated=simulated)
    # A reading over the safety limit opens the zone's Auto relays even before it becomes Critical, but test data never does.
    if not simulated or _opt("AUTO_SPRINKLER_ON_SIMULATED_DATA"):
        sprinklers.auto_activate_zone(sensor.location_zone, f"{sensor.id} {state}")
    elif action in ("created", "escalated") and state != "Critical":  # Critical: publish_incident already said so
        events.activity("warning", f"Incident #{incident.id}: sprinkler auto-activation skipped, the triggering data is "
                                   "SIMULATED (Alerts:AutoSprinklerOnSimulatedData=false)", incident.zone)


# ---------------------------------------------------------------- sensor-camera fusion
def corroborate(incident: IncidentAlert, now=None) -> bool:
    """Marks an open AI fire incident, once, as corroborated by a smoke sensor of the same zone that read at least its Warning
    level within FUSION_WINDOW_SECONDS. A record for the operator only: severity, sprinklers and notifications are untouched."""
    now = now or timezone.now()
    if incident.source != IncidentSource.VISION or not incident.fire_detected or incident.is_resolved:
        return False
    with _lock_for(f"corroborate:{incident.id}"):  # a fire frame and a smoke reading at once still give one record
        if IncidentLog.objects.filter(incident=incident, type=IncidentLogType.CORROBORATED).exists():
            return False
        evidence = _smoke_evidence(incident.zone, now)
        if evidence is None:
            return False
        sensor, reading = evidence
        add_log(IncidentLog(
            incident=incident, type=IncidentLogType.CORROBORATED, zone=incident.zone,
            details=f"Smoke sensor {sensor.id} in the same zone read {_num(reading.value)}{sensor.unit} (warning level "
                    f"{_num(sensor.threshold * _opt('WARNING_RATIO'))}{sensor.unit}) at {reading.recorded_at:%H:%M:%S} UTC, "
                    f"within {_num(_opt('FUSION_WINDOW_SECONDS'))} s of the camera fire"))
    events.activity("warning", f"Incident #{incident.id}: camera fire corroborated by smoke sensor {sensor.id}", incident.zone)
    incident.corroborated = True  # dto.incident reads it
    events.publish("incident", {"action": "updated", "incident": dto.incident(incident)})
    return True


def corroborate_zone(zone: str, now=None) -> None:
    """A smoke reading at Warning or above: the zone's open AI fire incidents that saw fire within the window."""
    now = now or timezone.now()
    since = now - timedelta(seconds=_opt("FUSION_WINDOW_SECONDS"))
    # shortcut: updated_at is the incident's last evaluated frame, which is a fire frame unless only people or smoke followed
    # the fire; a per-frame fire timestamp would need a new column.
    for incident in IncidentAlert.objects.select_related("camera").filter(
            source=IncidentSource.VISION, zone=zone, fire_detected=True, is_resolved=False, updated_at__gte=since):
        corroborate(incident, now)


def _smoke_evidence(zone: str, now):
    """(sensor, reading): a smoke sensor of exactly this zone, not stale, with a Warning-level reading within the window."""
    since = now - timedelta(seconds=_opt("FUSION_WINDOW_SECONDS"))
    for sensor in SensorNode.objects.filter(type=SensorType.SMOKE, location_zone=zone).order_by("id"):
        if is_stale(sensor, now):
            continue  # a silent sensor's old reading says nothing about the room now
        reading = (SensorReading.objects.filter(sensor=sensor, recorded_at__range=(since, now),
                                                value__gte=sensor.threshold * _opt("WARNING_RATIO"))
                   .order_by("-recorded_at").first())
        if reading:
            return sensor, reading
    return None


# ---------------------------------------------------------------- camera health
def camera_lost(camera_id: str, silent_seconds: int | None) -> None:
    """silent_seconds None: a watched stream camera that never sent a first frame (wrong link, AI not reading it)."""
    # services.delete_camera holds the same lock: the row cannot vanish between the lookup and the insert (FK violation).
    with _lock_for(f"camera:{camera_id}"):
        camera = CameraFeed.objects.filter(pk=camera_id).first()
        if camera is None:
            return
        CameraFeed.objects.filter(pk=camera_id).update(status=CameraStatus.OFFLINE)
        # Losing a camera matters most while there is a fire in its zone: that is when power cuts happen.
        fire_open = IncidentAlert.objects.filter(is_resolved=False, zone=camera.location_zone, fire_detected=True).exists()
        incident, action = upsert_incident(Draft(
            IncidentSource.CAMERA_LOST, camera.location_zone, "High" if fire_open else "Medium",
            (f"Camera lost: no frames from {camera.name} for {silent_seconds} s" if silent_seconds is not None
             else f"Camera lost: no first frame from {camera.name}; check its stream link")
            + (" while a fire incident is open in this zone" if fire_open else ""), camera_id=camera.id))
    publish_incident(incident, action, emergency=False)


# ---------------------------------------------------------------- sensor health
def stale_episode(sensor) -> IncidentAlert | None:
    """The sensor-lost incident of the current outage: one opened after the sensor's last valid reading."""
    return (IncidentAlert.objects.filter(source=IncidentSource.SENSOR_LOST, sensor_id=sensor.id,
                                         timestamp__gt=sensor.last_reading_at).order_by("-timestamp").first())


def sensor_lost(sensor_id: str, now) -> None:
    """One incident per outage of a sensor that has reported before. Never an emergency: no notification, no sprinkler."""
    # services.ingest_reading holds the same lock: a reading cannot land between the staleness check and the insert.
    with _lock_for(f"sensor:{sensor_id}"):
        sensor = SensorNode.objects.filter(pk=sensor_id).first()
        if sensor is None or sensor.last_reading_at is None or not is_stale(sensor, now) or stale_episode(sensor):
            return
        was_alarm = sensor_state(sensor) in ("Alarm", "Critical")  # went silent mid-alarm, e.g. burnt: keep it High
        incident = IncidentAlert.objects.create(
            source=IncidentSource.SENSOR_LOST, zone=sensor.location_zone, severity="High" if was_alarm else "Medium",
            sensor_id=sensor.id, message=(
                f"Sensor unavailable: no valid reading from {sensor.type} sensor {sensor.id} since "
                f"{sensor.last_reading_at:%Y-%m-%d %H:%M:%S} UTC" + (" (last reading was over the limit)" if was_alarm else ""))[:500])
    publish_incident(IncidentAlert.objects.select_related("camera").get(pk=incident.pk), "created", emergency=False)
    events.publish("telemetry", dto.sensor(sensor))  # dashboards show the Stale state without waiting for a reading


def sensor_recovered(sensor, episode: IncidentAlert) -> None:
    """Called once per outage, by the first valid reading after it. The incident stays for the operator to resolve."""
    add_log(IncidentLog(incident=episode, type=IncidentLogType.SENSOR_RECOVERED, zone=sensor.location_zone,
                        details=f"{sensor.id} reports again: {_num(sensor.current_value)}{sensor.unit}"))
    events.activity("success", f"{sensor.type} sensor {sensor.id} is reporting again.", sensor.location_zone)


# ---------------------------------------------------------------- incidents
def upsert_incident(d: Draft) -> tuple[IncidentAlert, str]:
    """Merge into the open incident of the same origin touched within the merge window, else create one.
    Returns the incident and "created" | "escalated" | "updated"."""
    with _lock_for(f"{d.source}:{d.zone}:{d.camera_id or d.sensor_id or '-'}"):
        window_start = timezone.now() - timedelta(seconds=_opt("INCIDENT_MERGE_WINDOW_SECONDS"))
        query = IncidentAlert.objects.select_related("camera").filter(
            is_resolved=False, source=d.source, zone=d.zone, updated_at__gte=window_start)
        if d.camera_id is not None:
            query = query.filter(camera_id=d.camera_id)
        if d.sensor_id is not None:
            query = query.filter(sensor_id=d.sensor_id)
        incident = query.order_by("-updated_at").first()

        message = d.message[:500]
        if incident is None:
            incident = IncidentAlert.objects.create(
                source=d.source, zone=d.zone, severity=d.severity, camera_id=d.camera_id, sensor_id=d.sensor_id,
                fire_detected=d.fire_detected, fire_severity=d.fire_severity, human_count=d.human_count,
                danger_count=d.danger_count, message=message)
            return IncidentAlert.objects.select_related("camera").get(pk=incident.pk), "created"

        escalated = rank(d.severity) > rank(incident.severity)
        if escalated:
            incident.severity = d.severity
            incident.message = message
        incident.fire_detected = incident.fire_detected or d.fire_detected
        if d.fire_severity and rank(d.fire_severity) > rank(incident.fire_severity):
            incident.fire_severity = d.fire_severity
        incident.human_count = max(incident.human_count, d.human_count)
        incident.danger_count = max(incident.danger_count, d.danger_count)
        incident.updated_at = timezone.now()
        incident.save()
        return incident, "escalated" if escalated else "updated"


def publish_incident(incident: IncidentAlert, action: str, emergency: bool, force_notify: bool = False,
                     simulated: bool = False) -> None:
    events.publish("incident", {"action": action, "incident": dto.incident(incident)})
    if action not in ("created", "escalated"):
        return

    add_log(IncidentLog(
        incident=incident, type=IncidentLogType.OPENED if action == "created" else IncidentLogType.ESCALATED,
        zone=incident.zone, details=f"{incident.severity}: {incident.message}", fire_severity=incident.fire_severity,
        human_count=incident.human_count, danger_count=incident.danger_count, simulated=simulated))
    level = "critical" if rank(incident.severity) >= rank("High") else "warning"
    events.activity(level, f"Incident #{incident.id} {'opened' if action == 'created' else 'escalated to'} "
                           f"{incident.severity}: {incident.message}", incident.zone)

    if emergency or force_notify:
        notifications.enqueue(incident, force_notify)
    if emergency and simulated and not _opt("AUTO_SPRINKLER_ON_SIMULATED_DATA"):
        events.activity("warning", f"Incident #{incident.id}: sprinkler auto-activation skipped, the triggering data is "
                                   "SIMULATED (Alerts:AutoSprinklerOnSimulatedData=false)", incident.zone)
    elif emergency:
        sprinklers.auto_activate_zone(incident.zone, f"emergency incident #{incident.id}")

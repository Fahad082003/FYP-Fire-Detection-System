"""Model -> JSON shapes. Field names and enum strings are exactly the ASP.NET DTOs (camelCase), so existing clients keep working."""
import json

from django.utils import timezone

from .models import IncidentLogType
from .rules import critical_threshold, is_stale, mask_credentials, sensor_state


def calibration(stored: str | None) -> dict | None:
    """The column holds .NET's default (PascalCase) JSON; the API speaks camelCase. Either casing is read."""
    if not stored:
        return None
    data = {k.lower(): v for k, v in json.loads(stored).items()}
    return {"floorPlanId": data["floorplanid"], "imagePoints": data["imagepoints"], "planPoints": data["planpoints"]}


def store_calibration(value: dict | None) -> str | None:
    if value is None:
        return None
    return json.dumps({"FloorPlanId": value["floorPlanId"], "ImagePoints": value["imagePoints"], "PlanPoints": value["planPoints"]},
                      separators=(",", ":"))


def camera(c, reveal_credentials=False) -> dict:
    return {
        "id": c.id, "name": c.name, "feedType": c.feed_type,
        "streamUrl": c.stream_url if reveal_credentials else mask_credentials(c.stream_url),
        "streamFormat": c.stream_format, "status": c.status, "locationZone": c.location_zone, "updatedAt": c.updated_at,
        "calibration": calibration(c.calibration),
    }


def sensor(s, history=None) -> dict:
    return {
        "id": s.id, "type": s.type, "currentValue": s.current_value, "threshold": s.threshold,
        "criticalThreshold": round(critical_threshold(s), 2), "locationZone": s.location_zone, "unit": s.unit,
        "state": "Stale" if is_stale(s, timezone.now()) else sensor_state(s), "lastReadingAt": s.last_reading_at, "history": history,
    }


def sprinkler(s) -> dict:
    return {"id": s.id, "zone": s.zone, "isActive": s.is_active, "mode": s.mode,
            "lastTriggeredAt": s.last_triggered_at, "updatedAt": s.updated_at}


def incident(i) -> dict:
    """Needs select_related("camera") for cameraName and the false_alarm annotation of services.incident_query() (one query
    per resolved incident otherwise; an open incident is never a false alarm)."""
    false_alarm = i.false_alarm if hasattr(i, "false_alarm") else (
        i.is_resolved and i.incidentlog_set.filter(type=IncidentLogType.FALSE_ALARM).exists())
    corroborated = i.corroborated if hasattr(i, "corroborated") else (
        i.fire_detected and i.incidentlog_set.filter(type=IncidentLogType.CORROBORATED).exists())
    return {
        "id": i.id, "timestamp": i.timestamp, "updatedAt": i.updated_at, "cameraId": i.camera_id,
        "cameraName": i.camera.name if i.camera_id and i.camera else None, "sensorId": i.sensor_id,
        "source": i.source, "zone": i.zone, "severity": i.severity, "fireDetected": i.fire_detected,
        "fireSeverity": i.fire_severity, "humanCount": i.human_count, "dangerCount": i.danger_count,
        "message": i.message, "isResolved": i.is_resolved, "resolvedAt": i.resolved_at, "falseAlarm": false_alarm,
        "corroborated": corroborated,
    }


def user(u) -> dict:
    return {"id": u.id, "email": u.email, "displayName": u.display_name, "role": u.role}


def log(entry) -> dict:
    return {
        "id": entry.id, "incidentId": entry.incident_id, "type": entry.type, "timestamp": entry.timestamp,
        "zone": entry.zone, "details": entry.details, "fireAreaPercent": entry.fire_area_percent,
        "fireSeverity": entry.fire_severity, "humanCount": entry.human_count, "dangerCount": entry.danger_count,
        "personTrackId": entry.person_track_id, "personX": entry.person_x, "personY": entry.person_y,
        "dangerZone": entry.danger_zone, "simulated": entry.simulated,
    }


def element(e) -> dict:
    return {"id": e.id, "type": e.type, "name": e.name, "x": e.x, "y": e.y, "width": e.width, "height": e.height,
            "isBlocked": e.is_blocked, "isActive": e.is_active, "zone": e.zone}


def floor_plan(p, elements=None) -> dict:
    items = elements if elements is not None else sorted(p.elements.all(), key=lambda e: e.id)
    return {
        "id": p.id, "name": p.name, "building": p.building, "floorNumber": p.floor_number,
        "widthMeters": p.width_meters, "heightMeters": p.height_meters, "hasImage": p.image_file_name is not None,
        "updatedAt": p.updated_at, "elements": [element(e) for e in items],
    }

"""Single source of truth for AI detection payloads (port of DetectionPayloadValidator.cs).

Rules, limits and error keys are the ASP.NET ones. Numbers may arrive as JSON numbers or numeric strings (ASP.NET's
web defaults allowed both), so "NaN" as a string must still be caught by the finite checks (audit defect D-13).
"""
import math
import re

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .models import SEVERITIES

MAX_DETECTIONS, MAX_ZONES, MAX_POLYGON_POINTS, MAX_HEATMAP_SIDE = 200, 32, 64, 64
ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
CATEGORIES = ["Human", "Fire", "Smoke", "Danger", "Vehicle", "Other"]
ZONE_LEVELS = ["Warning", "Danger", "Critical", "Safe"]


class PayloadError(Exception):
    def __init__(self, errors: dict[str, list[str]]):
        super().__init__("One or more validation errors occurred.")
        self.errors = errors


def _number(value):
    """float for a JSON number or numeric string; None for anything else (bools included). May be NaN/inf."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _finite(value) -> bool:
    return value is not None and math.isfinite(value)


def _enum(value, names, default):
    """Case-insensitive enum name (as System.Text.Json read them); None when unknown."""
    if value is None:
        return default
    if isinstance(value, str):
        return next((n for n in names if n.lower() == value.lower()), None)
    return None


def _point(p) -> list[float] | None:
    if not isinstance(p, list) or len(p) != 2:
        return None
    xy = [_number(v) for v in p]
    return xy if all(_finite(v) for v in xy) else None


def contains(polygon, x: float, y: float) -> bool:
    """Ray-casting point-in-polygon test (polygon given as [x, y] points)."""
    inside, j = False, len(polygon) - 1
    for i in range(len(polygon)):
        (xi, yi), (xj, yj) = polygon[i], polygon[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def validate(payload) -> dict:
    """Validated frame (DetectionFrameDto) with derived counts, or PayloadError."""
    errors: dict[str, list[str]] = {}

    def add(field, message):
        errors.setdefault(field, []).append(message)

    if not isinstance(payload, dict):
        raise PayloadError({"payload": ["Payload is required."]})

    def as_list(key):
        value = payload.get(key)
        if value is None:
            return []
        if not isinstance(value, list):
            add(key, "Must be a list.")
            return []
        return value

    detections_in, zones_in, points_in = as_list("detections"), as_list("dangerZones"), as_list("humanCoordinates")

    camera_id = payload.get("cameraId")
    if not isinstance(camera_id, str) or not ID_PATTERN.match(camera_id):
        add("cameraId", "cameraId is required (letters, digits, '-' or '_', max 64).")
    sizes = {}
    for key, default in (("frameWidth", 1920), ("frameHeight", 1080)):
        n = _number(payload.get(key, default))
        if not _finite(n) or n != int(n) or not 1 <= n <= 16384:
            add(key, "Must be between 1 and 16384.")
            n = default
        sizes[key] = int(n)
    normalized = payload.get("normalized", False)
    if not isinstance(normalized, bool):
        add("normalized", "Must be true or false.")
        normalized = False
    simulated = payload.get("simulated", False)
    if not isinstance(simulated, bool):
        add("simulated", "Must be true or false.")
        simulated = False

    timestamp = timezone.now()
    if payload.get("timestamp") is not None:
        parsed = parse_datetime(payload["timestamp"]) if isinstance(payload["timestamp"], str) else None
        if parsed is None:
            add("timestamp", "Must be an ISO 8601 date and time.")
        else:
            timestamp = parsed if timezone.is_aware(parsed) else timezone.make_aware(parsed, timezone.utc)

    if len(detections_in) > MAX_DETECTIONS:
        add("detections", f"At most {MAX_DETECTIONS} detections per frame.")
    detections = []
    fw, fh = (1.0, 1.0) if normalized else (sizes["frameWidth"], sizes["frameHeight"])
    for i, d in enumerate(detections_in[:MAX_DETECTIONS]):
        key = f"detections[{i}]"
        if not isinstance(d, dict):
            add(key, "Must not be null.")
            continue
        bbox = d.get("bbox")
        box = [_number(v) for v in bbox] if isinstance(bbox, list) else None
        if box is None or len(box) != 4:
            add(f"{key}.bbox", "bbox must be [x, y, width, height].")
        elif not all(_finite(v) for v in box) or box[2] < 0 or box[3] < 0:
            add(f"{key}.bbox", "bbox values must be finite and width/height non-negative.")
        elif box[0] >= fw or box[1] >= fh or box[0] + box[2] <= 0 or box[1] + box[3] <= 0:
            add(f"{key}.bbox", "bbox must overlap the frame.")  # a box wholly outside the frame is not a sighting (D-14)
        confidence = _number(d.get("confidence", 1.0))
        if not _finite(confidence) or not 0 <= confidence <= 1:
            add(f"{key}.confidence", "Must be between 0 and 1.")
        category = _enum(d.get("category"), CATEGORIES, "Other")
        if category is None:
            add(f"{key}.category", "Unknown category.")
        texts = {}
        for field, limit in (("label", 48), ("trackId", 48), ("explanation", 240)):
            value = d.get(field)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and field == "trackId":
                value = str(value)
            if value is not None and not isinstance(value, str):
                add(f"{key}.{field}", "Must be text.")
                value = None
            elif value is not None and len(value) > limit:
                add(f"{key}.{field}", f"Max {limit} characters.")
            texts[field] = value
        detections.append({"label": texts["label"], "category": category, "confidence": confidence, "bbox": box,
                           "trackId": texts["trackId"], "atRisk": d.get("atRisk") is True, "dangerZone": None,
                           "explanation": texts["explanation"]})

    if len(zones_in) > MAX_ZONES:
        add("dangerZones", f"At most {MAX_ZONES} zones per frame.")
    zones = []
    for i, z in enumerate(zones_in[:MAX_ZONES]):
        key = f"dangerZones[{i}]"
        polygon = z.get("polygon") if isinstance(z, dict) else None
        if not isinstance(polygon, list) or not 3 <= len(polygon) <= MAX_POLYGON_POINTS:
            add(f"{key}.polygon", f"Polygon needs 3 to {MAX_POLYGON_POINTS} points.")
            polygon = []
        else:
            polygon = [_point(p) for p in polygon]
            if any(p is None for p in polygon):
                add(f"{key}.polygon", "Every point must be [x, y].")
        label = z.get("label") if isinstance(z, dict) else None
        if label is not None and (not isinstance(label, str) or len(label) > 48):
            add(f"{key}.label", "Max 48 characters.")
        level = _enum(z.get("level") if isinstance(z, dict) else None, ZONE_LEVELS, "Danger")
        if level is None:
            add(f"{key}.level", "Unknown zone level.")
        zones.append({"label": label, "level": level, "polygon": polygon})

    heatmap = payload.get("heatmap")
    if heatmap is not None:
        rows, cols = (_number(heatmap.get(k)) if isinstance(heatmap, dict) else None for k in ("rows", "cols"))
        values = heatmap.get("values") if isinstance(heatmap, dict) else None
        if not all(_finite(v) and v == int(v) and 1 <= v <= MAX_HEATMAP_SIDE for v in (rows, cols)):
            add("heatmap", f"rows and cols must be between 1 and {MAX_HEATMAP_SIDE}.")
        elif not isinstance(values, list) or len(values) != rows * cols:
            add("heatmap.values", "values length must equal rows * cols (row-major).")
        else:
            values = [_number(v) for v in values]
            if any(v is None or math.isnan(v) or not 0 <= v <= 1 for v in values):
                add("heatmap.values", "Every value must be between 0 and 1.")
            heatmap = {"rows": int(rows), "cols": int(cols), "values": values}

    points = []
    if len(points_in) > MAX_DETECTIONS:
        add("humanCoordinates", f"At most {MAX_DETECTIONS} points.")
    else:
        points = [_point(p) for p in points_in]
        if any(p is None for p in points):
            add("humanCoordinates", "Every point must be [x, y].")

    model = payload.get("model")
    if model is not None and (not isinstance(model, str) or len(model) > 64):
        add("model", "Max 64 characters.")
    counts = {}
    for key in ("humanCount", "dangerCount"):
        value = payload.get(key)
        n = None if value is None else _number(value)
        if value is not None and (not _finite(n) or n != int(n) or n < 0):
            add(key, "Must be zero or more.")
        counts[key] = None if n is None or not _finite(n) else int(n)
    fire_flag = payload.get("fireDetected")
    if fire_flag is not None and not isinstance(fire_flag, bool):
        add("fireDetected", "Must be true or false.")
    fire_severity = _enum(payload.get("fireSeverity"), SEVERITIES, None)
    if payload.get("fireSeverity") is not None and fire_severity is None:
        add("fireSeverity", "Unknown severity.")

    if errors:
        raise PayloadError(errors)

    # Requirement 2.3: the backend itself decides who is in danger. A person whose feet (bottom-centre of the box)
    # stand inside a Danger/Critical zone is at risk, even if the AI did not say so.
    for d in detections:
        if d["category"] != "Human":
            continue
        fx, fy = d["bbox"][0] + d["bbox"][2] / 2, d["bbox"][1] + d["bbox"][3]
        zone = next((z for z in zones if z["level"] in ("Danger", "Critical") and contains(z["polygon"], fx, fy)), None)
        if zone:
            d["atRisk"] = True
            d["dangerZone"] = zone["label"] if zone["label"] and zone["label"].strip() else f"{zone['level']} zone"

    # Derive what the producer omitted; never report fewer people in danger than the zone check found.
    fire = fire_flag if fire_flag is not None else any(d["category"] == "Fire" for d in detections)
    humans = counts["humanCount"]
    if humans is None:
        humans = sum(d["category"] == "Human" for d in detections) or len(points)
    danger = max(counts["dangerCount"] or 0, sum(d["category"] == "Danger" or d["atRisk"] for d in detections))
    return {
        "cameraId": camera_id, "timestamp": timestamp, "frameWidth": sizes["frameWidth"], "frameHeight": sizes["frameHeight"],
        "normalized": normalized, "detections": detections, "dangerZones": zones, "heatmap": heatmap,
        "humanCoordinates": points, "fireDetected": fire, "fireSeverity": fire_severity if fire else None,
        "humanCount": humans, "dangerCount": danger, "simulated": simulated,
        "smokeDetected": any(d["category"] == "Smoke" for d in detections), "model": model,
    }

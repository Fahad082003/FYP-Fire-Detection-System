"""Pure business rules shared by the alert engine, the JSON mapping and the tests (no database access)."""
import re

from django.conf import settings

from .models import SensorType, rank


def max_severity(a: str, b: str) -> str:
    return a if rank(a) >= rank(b) else b


def classify_detection(f: dict) -> str | None:
    """Severity of a validated AI frame, or None when nothing is alert-worthy."""
    if not f["fireDetected"] and f["dangerCount"] == 0 and not f["smokeDetected"]:
        return None
    severity = "Medium" if f["smokeDetected"] else "Low"  # smoke without flames still needs an operator
    if f["fireDetected"]:
        severity = f["fireSeverity"] or "Medium"
        if f["humanCount"] > 0:
            severity = max_severity(severity, "High")  # people near an active fire
    if f["dangerCount"] > 0:
        severity = max_severity(severity, "Critical" if f["dangerCount"] >= 3 else "High")
    if f["fireDetected"] and f["dangerCount"] > 0:
        severity = "Critical"  # humans inside a fire danger zone
    return severity


def is_emergency(f: dict) -> bool:
    """The emergency trigger: humans in danger, or the AI rates the fire Critical."""
    return f["dangerCount"] > 0 or (f["fireDetected"] and f["fireSeverity"] == "Critical")


def critical_threshold(sensor) -> float:
    o = settings.SENTINEL
    factor = o["SMOKE_CRITICAL_MULTIPLIER"] if sensor.type == SensorType.SMOKE else o["TEMPERATURE_CRITICAL_MULTIPLIER"]
    return sensor.threshold * factor


def sensor_state(sensor) -> str:
    value = sensor.current_value
    if value >= critical_threshold(sensor):
        return "Critical"
    if value >= sensor.threshold:
        return "Alarm"
    if value >= sensor.threshold * settings.SENTINEL["WARNING_RATIO"]:
        return "Warning"
    return "Normal"


def is_stale(sensor, now) -> bool:
    """No valid reading for SENSOR_STALE_SECONDS, or none ever: its value says nothing about the room now."""
    last = sensor.last_reading_at
    return last is None or (now - last).total_seconds() >= settings.SENTINEL["SENSOR_STALE_SECONDS"]


def mask_credentials(url: str) -> str:
    """rtsp://user:pass@host -> rtsp://***@host. Only devices (the AI service) ever see the real link."""
    return re.sub(r"(?<=://)[^/@]+@", "***@", url or "")


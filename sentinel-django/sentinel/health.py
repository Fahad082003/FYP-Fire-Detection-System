"""Central system health (GET /api/system/health): every component from a real check, never a stored "healthy".

Component status: healthy, loading, degraded, offline or unknown. Overall: critical when the database is offline (Django is
proven up by answering at all; the dashboard shows critical when it gets no answer), degraded when any required component is
not healthy (loading and unknown included), else healthy. Optional components (the simulator; cameras or sensors when none is
monitored) never change the overall status. Nothing here writes to the database, starts a device or creates a reading.
"""
import json
import logging
import threading
import time

from django.conf import settings
from django.db import connection
from django.db.utils import Error as DatabaseError
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from . import ai_client, events, services
from .alerts import stale_episode
from .rules import is_stale, sensor_state
from .watchdog import watchdog

log = logging.getLogger("sentinel.health")
FRAME_STALE_SECONDS = 8  # shell.js FRAMES_STALE_MS: normal gaps between AI frames measured up to 5.4 s
DELIVERY_STALE_SECONDS = 2 * events.KEEPALIVE_SECONDS + 5  # every open stream sends at least a keep-alive per KEEPALIVE_SECONDS
SIMULATED_RUNNING_SECONDS = 30  # the simulator posts every ~2 s
SUPERVISOR_STATE = settings.BASE_DIR.parent / "run" / "supervisor.json"
SUPERVISOR_HEARTBEAT_SECONDS = 10  # tools/supervisor.py rewrites its state every second
WORKERS = ("camera-watchdog", "notification-worker")  # started by sentinel.startup in the server process


def _seconds(now, then) -> int:
    return round((now - then).total_seconds())


def database() -> dict:
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except DatabaseError as e:
        return {"status": "offline", "message": f"Cannot reach the database ({type(e).__name__})."}
    return {"status": "healthy", "message": "Connected."}


def django_backend() -> dict:
    running = {t.name for t in threading.enumerate() if t.is_alive()}
    workers = {name: name in running for name in WORKERS}
    missing = [name for name, alive in workers.items() if not alive]
    if missing:
        return {"status": "degraded", "details": {"workers": workers},
                "message": f"Answering, but not running: {', '.join(missing)} (camera/sensor-lost alarms or notifications stop)."}
    return {"status": "healthy", "message": "Answering; camera watchdog and notification worker running.", "details": {"workers": workers}}


def supervisor_ai(now) -> dict | None:
    """The AI entry of tools/supervisor.py's state, or None when no supervisor is running (no file, or an old heartbeat)."""
    try:
        state = json.loads(SUPERVISOR_STATE.read_text(encoding="utf-8"))
        heartbeat = parse_datetime(state["heartbeat"])
        ai = state["children"]["ai"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if heartbeat is None or abs(_seconds(now, heartbeat)) > SUPERVISOR_HEARTBEAT_SECONDS or not isinstance(ai, dict):
        return None
    return {k: ai.get(k) for k in ("state", "restarts", "exitCode", "lastExitAt", "lastReadyAt", "nextRestartAt", "lastRestartAt",
                                   "lastFailure", "unresponsiveSince")}


AI_STATUS = {
    "ok": ("healthy", "Ready: all models loaded."),
    "loading": ("loading", "Running, models still loading: nothing is detected yet."),
    "degraded": ("degraded", "Running, but some models failed to load."),
    "no-model": ("degraded", "Running, but no model is loaded: nothing can be detected."),
}


def ai_service(ai: dict, supervisor: dict | None) -> dict:
    details = {**ai, "supervisor": supervisor}  # ai is GET /api/ai/status; the dashboard's banner reads it from here
    sv = supervisor or {}
    failure = sv.get("lastFailure") or f"exited (code {sv.get('exitCode')})"  # older state files have no lastFailure
    if not ai["online"]:
        message = ai["error"]
        state = sv.get("state")
        if state == "unresponsive":
            message += f" Unresponsive since {sv['unresponsiveSince']}: the supervisor restarts it if it stays silent."
        elif state == "backoff":
            message += f" Restarting: it {failure}; the supervisor restarts it at {sv['nextRestartAt']}."
        elif state == "failed":
            message += f" Recovery failed: it {failure}. The supervisor stopped restarting it after {sv['restarts']} restarts."
        elif state == "starting":
            message += (f" Recovering: restarted at {sv['lastRestartAt']} after it {failure}; loading its models."
                        if sv.get("restarts") else " The supervisor is starting it.")
        return {"status": "offline", "message": message, "details": details}
    reported = (ai["service"] or {}).get("status") if isinstance(ai["service"], dict) else None
    status, message = AI_STATUS.get(reported, ("unknown", f"Unexpected AI status {reported!r}."))
    if sv.get("restarts"):
        message += f" Restarted {sv['restarts']} time(s), last at {sv['lastRestartAt']} after it {failure}."
    return {"status": status, "message": message, "details": details}


def models(ai: dict) -> dict:
    service = ai["service"] if ai["online"] and isinstance(ai["service"], dict) else None
    if service is None:
        return {"status": "unknown", "message": "AI service not answering: model state unknown."}
    if service.get("status") == "loading":
        return {"status": "loading", "message": "Loading and warming up the models."}
    entries = [{k: m.get(k) for k in ("name", "file", "loaded", "error")} for m in service.get("models") or []]
    failed = [m for m in entries if not m["loaded"]]
    details = {"models": entries}
    if not entries:
        return {"status": "offline", "message": "The AI manifest lists no model.", "details": details}
    if failed:
        reasons = "; ".join(f"{m['name']}: {m['error']}" for m in failed)
        return {"status": "offline" if len(failed) == len(entries) else "degraded", "message": f"Not loaded: {reasons}", "details": details}
    return {"status": "healthy", "message": f"{len(entries)} model(s) loaded and warmed up.", "details": details}


def camera(c, now, last, lost: bool, seeded: bool, ai_camera: dict | None, ai_loading: bool) -> dict:
    """connected, recovering, stale, disconnected, or not-monitored (a simulated feed without a link that never sent frames)."""
    age = None if last is None or seeded else _seconds(now, last)  # a seeded "last frame" is only the watchdog's grace deadline
    since = "no frame yet" if age is None else f"no frame for {age} s"
    ai_state = (ai_camera or {}).get("state")

    def out(state, status, message):
        return {"id": c.id, "name": c.name, "zone": c.location_zone, "lastFrameAt": None if age is None else last,
                "state": state, "status": status, "message": message}

    if ai_state == "error":
        return out("disconnected", "offline", f"AI cannot read the stream: {ai_camera.get('error')}")
    if age is not None and age <= FRAME_STALE_SECONDS:
        return out("connected", "healthy", f"Frames arriving (last {age} s ago).")
    if ai_state in ("starting", "reconnecting") or (ai_loading and c.stream_url) or (seeded and not lost):  # the AI reads linked ones
        return out("recovering", "loading", f"Recovering: {since}, " + (
            "AI is reconnecting to the stream." if ai_state == "reconnecting" else "waiting for the AI service's first frames."))
    if age is None and not c.stream_url:
        return out("not-monitored", "unknown", "Simulated feed without a stream link: not monitored.")
    if age is None or lost or age > settings.SENTINEL["CAMERA_LOST_SECONDS"]:
        return out("disconnected", "offline", f"Disconnected: {since}.")
    return out("stale", "degraded", f"Stale: {since}.")


def _group(entries: list[dict], noun: str, good: str) -> dict:
    """One status for cameras or sensors: offline if every monitored one is, loading if all problems are, else degraded."""
    monitored = [e for e in entries if e["state"] != "not-monitored"]
    if not monitored:
        return {"status": "unknown", "optional": True, "details": {f"{noun}s": entries},
                "message": f"No {noun} is monitored." if entries else f"No {noun} configured."}
    bad = [e for e in monitored if e["status"] != "healthy"]
    status = ("healthy" if not bad else "offline" if len(bad) == len(monitored) and all(e["status"] == "offline" for e in bad)
              else "loading" if all(e["status"] == "loading" for e in bad) else "degraded")
    message = f"{len(monitored) - len(bad)} of {len(monitored)} {noun}(s) {good}." + (
        " " + "; ".join(f"{e.get('name') or e['id']}: {e['state']}" for e in bad) if bad else "")
    return {"status": status, "message": message, "details": {f"{noun}s": entries}}


def cameras(now, ai: dict) -> dict:
    frames, lost, seeded = watchdog.snapshot()
    service = ai["service"] if ai["online"] and isinstance(ai["service"], dict) else {}
    ai_cameras = {c.get("cameraId"): c for c in service.get("cameras") or [] if isinstance(c, dict)}
    entries = [camera(c, now, frames.get(c.id), c.id in lost, c.id in seeded, ai_cameras.get(c.id), service.get("status") == "loading")
               for c in services.cameras()]
    return _group(entries, "camera", "connected")


def sensors(now) -> dict:
    entries = []
    for s in services.sensors():
        entry = {"id": s.id, "type": s.type, "zone": s.location_zone, "value": s.current_value, "unit": s.unit,
                 "reading": sensor_state(s), "lastReadingAt": s.last_reading_at}
        if s.last_reading_at is None:
            entry.update(state="offline", status="offline", message="Never reported.")
        elif not is_stale(s, now):
            entry.update(state="reporting", status="healthy", message=f"Reporting ({s.current_value:g} {s.unit}, {entry['reading']}).")
        elif stale_episode(s):  # the watchdog has raised its "sensor lost" incident
            entry.update(state="offline", status="offline", message=f"Lost: no reading for {_seconds(now, s.last_reading_at)} s.")
        else:
            entry.update(state="stale", status="degraded", message=f"Stale: no reading for {_seconds(now, s.last_reading_at)} s.")
        entries.append(entry)
    return _group(entries, "sensor", "reporting")


def live_updates() -> dict:
    subscribers = events.subscriber_count()
    if not subscribers:
        return {"status": "unknown", "message": "No dashboard is connected to the live stream, so delivery cannot be confirmed.",
                "details": {"subscribers": 0}}
    age = round(time.monotonic() - events.last_delivery) if events.last_delivery else None
    details = {"subscribers": subscribers, "lastDeliverySeconds": age}
    if age is None or age > DELIVERY_STALE_SECONDS:
        return {"status": "degraded", "details": details,
                "message": f"{subscribers} dashboard(s) connected, but nothing delivered for over {DELIVERY_STALE_SECONDS} s."}
    return {"status": "healthy", "message": f"{subscribers} dashboard(s) connected; last delivery {age} s ago.", "details": details}


def simulator(now) -> dict:
    if not (settings.BASE_DIR / "tools" / "simulator.py").exists():
        return {"status": "offline", "optional": True, "message": "Unavailable: tools/simulator.py is missing."}
    at = watchdog.simulated_at
    if at is None:
        return {"status": "unknown", "optional": True, "message": "Not running (optional): no simulated data since the server started."}
    age = _seconds(now, at)
    if age <= SIMULATED_RUNNING_SECONDS:
        return {"status": "healthy", "optional": True, "message": f"Running: simulated data {age} s ago.", "details": {"lastDataAt": at}}
    return {"status": "offline", "optional": True, "message": f"Stopped: last simulated data {age} s ago.", "details": {"lastDataAt": at}}


NAMES = {"database": "Database", "django": "Django Backend", "ai": "AI Service", "models": "AI Models", "cameras": "Cameras",
         "sensors": "Sensors", "liveUpdates": "Live Updates", "simulator": "Simulator"}


def _check(key: str, fn, *args) -> dict:
    """A failing check makes its own component unknown; the others are still reported."""
    try:
        result = fn(*args)
    except Exception as e:  # noqa: BLE001 - reported in the answer and logged
        log.exception("health check %s failed", key)
        result = {"status": "unknown", "message": f"Check failed ({type(e).__name__})."}
    return {"name": NAMES[key], "optional": False, "details": {}, **result, "lastChecked": timezone.now()}


def system_health() -> dict:
    now = timezone.now()
    c = {"database": _check("database", database), "django": _check("django", django_backend)}
    try:
        ai = ai_client.status()
    except Exception:  # noqa: BLE001 - ai_client.status turns every network failure into an answer; this is a bug guard
        log.exception("AI status check failed")
        ai = {"configured": True, "online": False, "error": "AI status check failed.", "service": None}
    c["ai"] = _check("ai", ai_service, ai, supervisor_ai(now))
    c["models"] = _check("models", models, ai)
    if c["database"]["status"] == "healthy":
        c["cameras"] = _check("cameras", cameras, now, ai)
        c["sensors"] = _check("sensors", sensors, now)
    else:  # each query would wait for the database again
        for key in ("cameras", "sensors"):
            c[key] = _check(key, lambda: {"status": "unknown", "message": "Database unavailable: cannot be read."})
    c["liveUpdates"] = _check("liveUpdates", live_updates)
    c["simulator"] = _check("simulator", simulator, now)

    problems = [f"{x['name']} {x['status']}" for x in c.values() if not x["optional"] and x["status"] != "healthy"]
    overall = "critical" if c["database"]["status"] == "offline" else "degraded" if problems else "healthy"
    return {"status": overall, "message": "; ".join(problems) or "All required components healthy.", "checkedAt": now, "components": c}

"""Room-wise people counts on a floor plan, from the cameras calibrated to it (room occupancy and the human heatmap).

Each camera's newest frame puts its confirmed people on the plan (the feet point, through the camera's 4-point calibration).
A room's count is a snapshot of those newest frames, so someone standing still is one person, not one per frame, and someone
who walks out stops being counted by the next frame. There is no identity across cameras: when two cameras see one room, the
count is the largest single-camera count (nobody counted twice) and maxPossible is the sum (in case they see different people).
A room no live camera sees is "stale" (last count, with its age) or "unknown", never 0.
"""
import json
import threading

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from . import dto, events, geometry
from .models import CameraFeed, FloorPlan, IncidentAlert, IncidentSource
from .validation import PERSON_CONFIRM_HITS

ROOM_TYPES = ("Room", "Corridor", "SafeZone")
# shortcut: in this process's memory, like the event stream; a restart shows every room as unknown until frames arrive.
_latest: dict[str, dict] = {}  # camera id -> {"planId", "at", "points": [(x, y) in plan meters]}
_lock = threading.Lock()


def homography(src, dst) -> list[float] | None:
    """Perspective transform mapping 4 camera-frame points onto 4 plan points (same maths as dashboard.js); None if degenerate."""
    a = []
    for (x, y), (u, v) in zip(src, dst):
        a += [[x, y, 1, 0, 0, 0, -u * x, -u * y, u], [0, 0, 0, x, y, 1, -v * x, -v * y, v]]
    for c in range(8):  # Gauss-Jordan elimination with partial pivoting
        p = max(range(c, 8), key=lambda r: abs(a[r][c]))
        if abs(a[p][c]) < 1e-12:
            return None
        a[c], a[p] = a[p], a[c]
        for r in range(8):
            if r != c:
                f = a[r][c] / a[c][c]
                a[r] = [a[r][k] - f * a[c][k] for k in range(9)]
    return [a[i][8] / a[i][i] for i in range(8)] + [1.0]


def project(h, x, y) -> tuple[float, float]:
    w = h[6] * x + h[7] * y + h[8]
    return (h[0] * x + h[1] * y + h[2]) / w, (h[3] * x + h[4] * y + h[5]) / w


def record(frame: dict) -> None:
    """Called for every accepted AI frame: keeps the camera's people on its plan and tells dashboards when the count changes."""
    camera = CameraFeed.objects.filter(pk=frame["cameraId"]).only("calibration").first()
    cal = dto.calibration(camera.calibration) if camera else None
    h = homography(cal["imagePoints"], cal["planPoints"]) if cal else None
    if h is None:
        forget(frame["cameraId"])
        return
    sx, sy = (frame["frameWidth"], frame["frameHeight"]) if frame["normalized"] else (1, 1)
    points = [project(h, (d["bbox"][0] + d["bbox"][2] / 2) * sx, (d["bbox"][1] + d["bbox"][3]) * sy)  # the feet: on the floor plane
              for d in frame["detections"]
              if d["category"] == "Human" and (d["hits"] is None or d["hits"] >= PERSON_CONFIRM_HITS)]  # tracked people only
    with _lock:
        previous = _latest.get(frame["cameraId"])
        _latest[frame["cameraId"]] = {"planId": cal["floorPlanId"], "at": timezone.now(), "points": points}
    if previous is None or (previous["planId"], len(previous["points"])) != (cal["floorPlanId"], len(points)):
        events.publish("occupancy", {"floorPlanId": cal["floorPlanId"], "cameraId": frame["cameraId"]})
    if points or previous and previous["points"]:  # people moving between rooms change exits too: checked at most every 2 s
        if cache.add(f"crowd-check:{cal['floorPlanId']}", True, 2):
            plan = FloorPlan.objects.filter(pk=cal["floorPlanId"]).first()
            if plan is not None:
                evaluate_crowd(plan)


def forget(camera_id: str) -> None:
    with _lock:
        _latest.pop(camera_id, None)


def _inside(px, py, poly) -> bool:
    """Ray casting: is the point inside the polygon?"""
    hit = False
    for (x1, y1), (x2, y2) in zip(poly, poly[1:] + poly[:1]):
        if (y1 > py) != (y2 > py) and px < x1 + (py - y1) * (x2 - x1) / (y2 - y1):
            hit = not hit
    return hit


def _in_rect(x, y, e) -> bool:
    return e.x <= x <= e.x + e.width and e.y <= y <= e.y + e.height


def _sees(poly, e) -> bool:
    """Does the camera's calibrated floor area overlap the room?
    shortcut: corner tests only, so a quad crossing a room without a corner inside either is missed; fine for 4 floor corners."""
    corners = [(e.x, e.y), (e.x + e.width, e.y), (e.x + e.width, e.y + e.height), (e.x, e.y + e.height)]
    return any(_in_rect(x, y, e) for x, y in poly) or any(_inside(x, y, poly) for x, y in corners)


def plan_occupancy(plan) -> dict:
    now = timezone.now()
    stale_after = settings.SENTINEL["OCCUPANCY_STALE_SECONDS"]
    rooms = [e for e in sorted(plan.elements.all(), key=lambda e: e.id) if e.type in ROOM_TYPES]
    cameras = [(c, dto.calibration(c.calibration)) for c in CameraFeed.objects.exclude(calibration=None)]
    cameras = [(c, cal) for c, cal in cameras if cal["floorPlanId"] == plan.id]
    with _lock:
        latest = {c.id: _latest[c.id] for c, _ in cameras if c.id in _latest and _latest[c.id]["planId"] == plan.id}

    limits = capacities(plan.id)
    counts = {c.id: {} for c, _ in cameras}  # camera -> {room id: people}; a point goes to the smallest room around it
    for c, _ in cameras:
        for x, y in latest.get(c.id, {}).get("points", []):
            room = min((e for e in rooms if _in_rect(x, y, e)), key=lambda e: e.width * e.height, default=None)
            if room is not None:
                counts[c.id][room.id] = counts[c.id].get(room.id, 0) + 1

    out = []
    for e in rooms:
        seeing = [c for c, cal in cameras if _sees(cal["planPoints"], e) or counts[c.id].get(e.id)]
        # Time alone decides: a camera is marked Offline only after 10 s without frames, and the watchdog writes it back
        # to Online a moment after it recovers, so the stored status would wrongly hide a fresh frame.
        fresh = [c for c in seeing if c.id in latest and (now - latest[c.id]["at"]).total_seconds() <= stale_after]
        old = [c for c in seeing if c.id in latest and c not in fresh]
        use = fresh or old
        n = [counts[c.id].get(e.id, 0) for c in use]
        state = "live" if fresh else "stale" if old else "unknown"
        updated = max((latest[c.id]["at"] for c in use), default=None)
        reason = ("" if fresh else
                  "No calibrated camera sees this room." if not seeing else
                  "No frame from its camera yet." if not old else
                  f"Camera data is old: last frame {round((now - updated).total_seconds())} s ago.")
        count, limit = (max(n) if n else None), limits.get(f"{e.type}:{e.name}")
        out.append({"elementId": e.id, "name": e.name or e.type, "type": e.type, "bounds": [e.x, e.y, e.width, e.height],
                    "state": state, "count": count, "maxPossible": sum(n) if n else None,
                    "cameras": [c.id for c in seeing], "updatedAt": updated, "reason": reason,
                    "capacity": limit, "density": _density(state, count, limit)})
    fresh_ids = {c.id for c, _ in cameras if c.id in latest and (now - latest[c.id]["at"]).total_seconds() <= stale_after}
    exits, stranded = _exit_loads(plan, [p for i in fresh_ids for p in latest[i]["points"]], limits,
                                  "unknown" if not fresh_ids else "partial" if len(fresh_ids) < len(cameras) else "live")
    return {"floorPlanId": plan.id, "rooms": out, "exits": exits, "peopleWithoutSafeRoute": stranded,
            "staleAfterSeconds": stale_after, "serverTime": now,
            "note": "People tracked for at least 3 frames, placed by camera calibration. One person seen by two cameras is not "
                    "recognised as the same person: count = largest single-camera count, maxPossible = the sum."}


# ---------------------------------------------------------------- crowd density
def _density(state: str, count, limit) -> str:
    """Overcrowded only above the limit, from a count the cameras really saw; stale or unknown data is never "OK"."""
    if limit is None:
        return "No limit set"
    if state != "live" or count is None:
        return "Unknown"
    return "Overcrowded" if count > limit else "At capacity" if count == limit else "OK"


def _exit_loads(plan, points, limits, state) -> tuple[list[dict], int | None]:
    """People heading to each exit = those whose nearest SAFE exit it is (the evacuation routing, which avoids fire and smoke
    zones). An overcrowded exit gets the safe exit that is next best for those people, or an explicit "no safe route"."""
    from .services import _active_danger  # services imports this module
    elements = list(plan.elements.all())
    exits = [e for e in elements if e.type == "Exit"]
    if not exits:
        return [], None
    active, clearance = _active_danger(elements), settings.SENTINEL["DANGER_CLEARANCE_METERS"]
    inside = [(x, y) for x, y in points if 0 <= x <= plan.width_meters and 0 <= y <= plan.height_meters][:50]
    routes = [geometry.find_routes(plan.width_meters, plan.height_meters, elements, active, x, y, clearance) for x, y in inside]
    heading, stranded = {e.name: [] for e in exits}, 0  # exit -> the route results of the people using it
    for r in routes:
        safe = [x for x in r["exits"] if x["status"] == "Available"]
        if safe:
            heading[safe[0]["exitName"]].append(r)
        else:
            stranded += 1  # nobody is counted at an exit they cannot safely reach
    status = {x["exitName"]: x["status"] for r in routes for x in r["exits"]} if routes else {}
    out = []
    for e in exits:
        limit, load = limits.get(f"Exit:{e.name}"), len(heading[e.name]) if state != "unknown" else None
        density = _density("live" if state != "unknown" else "unknown", load, limit)
        if state == "partial" and density != "Overcrowded":  # a stale camera may see more people: only "too many" is certain
            density = "Unknown" if limit is not None else density
        alternative, message = None, ""
        if density == "Overcrowded":
            options = {}  # other safe exits, by total walking distance for the people at this one
            for r in heading[e.name]:
                for x in r["exits"]:
                    if x["status"] == "Available" and x["exitName"] != e.name:
                        options.setdefault(x["exitName"], []).append(x["distanceMeters"])
            options = {k: v for k, v in options.items() if len(v) == load}  # safe for every one of them
            if options:
                name = min(options, key=lambda k: sum(options[k]))
                alternative = {"exitName": name, "distanceMeters": round(sum(options[name]) / load, 1)}
                message = f"Send people to exit '{name}' (about {alternative['distanceMeters']} m)."
            else:
                message = "No safe alternative route: every other exit is blocked, in danger or unreachable."
        out.append({"name": e.name, "exitStatus": status.get(e.name, "Blocked" if e.is_blocked else "Unknown"), "load": load,
                    "capacity": limit, "density": density, "state": state, "alternative": alternative, "message": message})
    return out, (stranded if state != "unknown" else None)


def evaluate_crowd(plan) -> None:
    """One open Crowd incident per overcrowded room or exit while it lasts; resolved once a live count is back within the
    limit. Unknown or stale data neither opens nor resolves one (it is not a count)."""
    from . import alerts, services  # both import this module
    o = plan_occupancy(plan)
    places = [(f"{r['type']} '{r['name']}'", r["density"], r["count"], r["capacity"], "") for r in o["rooms"]] + \
             [(f"Exit '{x['name']}'", x["density"], x["load"], x["capacity"], x["message"]) for x in o["exits"]]
    if o["peopleWithoutSafeRoute"] is not None and o["exits"]:  # people the cameras see with no safe exit at all
        stranded = o["peopleWithoutSafeRoute"]
        cleared = "OK" if o["exits"][0]["state"] == "live" else "Unknown"  # 0 with a stale camera is not "nobody stranded"
        places.append(("no safe exit", "Overcrowded" if stranded else cleared, stranded, 0,
                       "No safe route available: every exit is blocked, in danger or unreachable from where they are."))
    for what, density, count, limit, advice in places:
        zone = f"{plan.name} - {what}"[:64]
        with alerts._lock_for(f"crowd:{zone}"):
            current = IncidentAlert.objects.select_related("camera").filter(source=IncidentSource.CROWD, zone=zone, is_resolved=False).first()
            if density == "Overcrowded" and current is None:
                text = (f"{count} people have no safe exit. {advice}" if what == "no safe exit" else
                        f"Overcrowded: {count} {'people heading there' if what.startswith('Exit') else 'people inside'}, limit {limit}. {advice}")
                incident = IncidentAlert.objects.create(source=IncidentSource.CROWD, zone=zone, human_count=count, message=text.strip()[:500],
                                                        severity="High" if what == "no safe exit" else "Medium")
                alerts.publish_incident(IncidentAlert.objects.select_related("camera").get(pk=incident.pk), "created", emergency=False)
            elif current is not None and density in ("OK", "At capacity", "No limit set"):
                services.resolve_incident(current, "the crowd monitor (back within the limit)")


def capacities(plan_id: int) -> dict:
    """{"<type>:<name>": max people} of one plan. Kept in a JSON file (FloorPlans__CapacityFile): no database column exists."""
    try:
        data = json.loads(settings.SENTINEL["CAPACITY_FILE"].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    plan = data.get(str(plan_id)) if isinstance(data, dict) else None
    return {k: v for k, v in (plan or {}).items() if isinstance(v, int) and not isinstance(v, bool)}


def save_capacities(plan, values) -> dict:
    """Replaces the plan's limits. Keys are "Room:<name>", "Corridor:<name>", "SafeZone:<name>" or "Exit:<name>" of this plan."""
    from .services import Invalid
    names = {f"{e.type}:{e.name}" for e in plan.elements.all() if e.type in (*ROOM_TYPES, "Exit") and e.name}
    if not isinstance(values, dict):
        raise Invalid({"capacities": ["Give an object like {\"Exit:East\": 30}."]})
    errors = {k: ["No named room, corridor, safe zone or exit with this name on the plan."] for k in values if k not in names}
    errors.update({k: ["Must be a whole number from 1 to 10000."] for k, v in values.items() if k in names
                   and not (isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 10000)})
    if errors:
        raise Invalid(errors)
    path = settings.SENTINEL["CAPACITY_FILE"]
    with _lock:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        data[str(plan.id)] = values
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(path)
    evaluate_crowd(plan)
    return values

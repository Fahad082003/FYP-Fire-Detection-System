"""Evacuation routing and the fire-risk heatmap on a floor plan (ports of EvacuationRouter.cs and FireRiskEstimator.cs).

Pure functions on elements with .type .name .x .y .width .height .is_blocked (unsaved FloorPlanElement objects work).
Units are meters. The plan is rasterised to a grid and searched with Dijkstra (8 neighbours, no corner cutting).
"""
import heapq
import math

NEAR_DANGER_COST_FACTOR = 1000  # any detour through free space beats a step next to a danger zone
WALKABLE = ("Room", "Corridor", "Door", "Exit", "SafeZone")
HIGH_METERS, MEDIUM_METERS, LOW_METERS = 5, 10, 20
LEVEL_NAMES = ["None", "Low", "Medium", "High", "Burning"]


def inside(e, x: float, y: float) -> bool:
    return e.x <= x <= e.x + e.width and e.y <= y <= e.y + e.height


def overlaps(e, x0: float, y0: float, size: float) -> bool:
    return e.x < x0 + size and e.x + e.width > x0 and e.y < y0 + size and e.y + e.height > y0


def _gap(e, x0: float, y0: float, size: float) -> float:
    """Shortest distance between element e and the square cell at (x0, y0); 0 when they touch."""
    dx = max(0.0, e.x - (x0 + size), x0 - (e.x + e.width))
    dy = max(0.0, e.y - (y0 + size), y0 - (e.y + e.height))
    return math.hypot(dx, dy)


def fmt(v: float, decimals: int = 1) -> str:
    """.NET "0.#" format: at most `decimals` decimals, no trailing zeros."""
    text = f"{v:.{decimals}f}".rstrip("0").rstrip(".") if decimals else f"{v:.0f}"
    return "0" if text in ("-0", "") else text


class Grid:
    def __init__(self, width: float, height: float, base_cell: float, max_cells: int):
        self.cell = max(base_cell, math.sqrt(width * height / max_cells))
        self.cols, self.rows = math.ceil(width / self.cell), math.ceil(height / self.cell)
        self.n = self.cols * self.rows

    def covered(self, e, test) -> set[int]:
        """Indexes of cells for which test(e, x0, y0) holds; only the cells near e are tried (same result as all cells)."""
        c = self.cell
        c0, c1 = max(0, int(e.x / c) - 1), min(self.cols - 1, int((e.x + e.width) / c) + 1)
        r0, r1 = max(0, int(e.y / c) - 1), min(self.rows - 1, int((e.y + e.height) / c) + 1)
        return {r * self.cols + col for r in range(r0, r1 + 1) for col in range(c0, c1 + 1) if test(e, col * c, r * c)}

    def centre_in(self, elements) -> set[int]:
        half = self.cell / 2
        return set().union(*(self.covered(e, lambda e, x0, y0: inside(e, x0 + half, y0 + half)) for e in elements))

    def overlapping(self, elements) -> set[int]:
        return set().union(*(self.covered(e, lambda e, x0, y0: overlaps(e, x0, y0, self.cell)) for e in elements))

    def neighbours(self, u: int, open_cells):
        cols, rows = self.cols, self.rows
        ur, uc = divmod(u, cols)
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                nr, nc = ur + dr, uc + dc
                if (dr == 0 and dc == 0) or nr < 0 or nc < 0 or nr >= rows or nc >= cols:
                    continue
                v = nr * cols + nc
                diagonal = dr != 0 and dc != 0
                if v not in open_cells or (diagonal and (ur * cols + nc not in open_cells or nr * cols + uc not in open_cells)):
                    continue  # no corner cutting
                yield v, math.sqrt(2) if diagonal else 1.0


def _fail(message: str, start_in_danger: bool = False) -> dict:
    return {"found": False, "message": message, "path": [], "distanceMeters": 0, "exitName": None,
            "startInDangerZone": start_in_danger, "clearanceViolated": False, "minDangerGapMeters": None}


def find_route(width, height, elements, active_danger, start_x, start_y, clearance=0.0) -> dict:
    """Requirement 2.4: shortest safe walking route to the nearest reachable open exit.
    Walls (unless an open door/exit overlaps them), closed doors/exits and active danger zones are obstacles. Cells closer than
    `clearance` to an active danger zone cost NEAR_DANGER_COST_FACTOR more; a route that still enters that band is returned
    with clearanceViolated and an explicit warning, never as a normal route."""
    if not any(e.type in ("Room", "Corridor") for e in elements):
        return _fail("Floor plan has no rooms or corridors, so no walkable area is defined.")
    exits = [e for e in elements if e.type == "Exit" and not e.is_blocked]
    if not exits:
        return _fail("Floor plan has no open emergency exit.")
    p = _prepare(width, height, elements, active_danger, start_x, start_y)
    if "found" in p:
        return p
    _, prev, reached = _search(p, clearance, p["free"] & p["g"].centre_in(exits))
    if reached < 0:
        return _fail("No safe route: every exit is cut off by walls, closed doors or active danger zones.", p["start_danger"])
    return _route(p, prev, reached, exits, clearance, start_x, start_y)


def find_routes(width, height, elements, active_danger, start_x, start_y, clearance=0.0) -> dict:
    """Every exit compared from one start point, with the same rules as find_route (one Dijkstra over the whole plan, so each
    exit gets its own shortest route). Status per exit: Available (a safe route), Blocked (marked closed), Danger (the exit
    lies in an active danger zone, or every route to it breaks the clearance), Unreachable (cut off), Unknown (start unusable).
    No route = no path, no distance: nothing is invented."""
    exits = [e for e in elements if e.type == "Exit"]
    if not any(e.type in ("Room", "Corridor") for e in elements) or not exits:
        reason = ("Floor plan has no rooms or corridors, so no walkable area is defined." if exits
                  else "Floor plan has no emergency exit.")
        return {"found": False, "message": reason, "startInDangerZone": False, "exits": []}
    p = _prepare(width, height, elements, active_danger, start_x, start_y)
    if "found" in p:
        return {"found": False, "message": p["message"], "startInDangerZone": p["startInDangerZone"],
                "exits": [_exit(e, "Blocked" if e.is_blocked else "Unknown", p["message"]) for e in exits]}
    dist, prev, _ = _search(p, clearance)
    results = []
    for e in exits:
        if e.is_blocked:
            results.append(_exit(e, "Blocked", "The exit is marked blocked or closed."))
            continue
        cells = [c for c in p["free"] & p["g"].centre_in([e]) if c in dist]
        if not cells:
            in_danger = any(z.x < e.x + e.width and e.x < z.x + z.width and z.y < e.y + e.height and e.y < z.y + z.height
                            for z in p["dangers"])
            results.append(_exit(e, "Danger", "The exit lies inside an active danger zone.") if in_danger
                           else _exit(e, "Unreachable", "No route: cut off by walls, closed doors or active danger zones."))
            continue
        r = _route(p, prev, min(cells, key=dist.get), [e], clearance, start_x, start_y)
        results.append({**_exit(e, "Danger" if r["clearanceViolated"] else "Available", r["message"] if r["clearanceViolated"] else None),
                        "found": True, "path": r["path"], "distanceMeters": r["distanceMeters"],
                        "clearanceViolated": r["clearanceViolated"], "minDangerGapMeters": r["minDangerGapMeters"]})
    order = {"Available": 0, "Danger": 1, "Unreachable": 2, "Blocked": 3}
    results.sort(key=lambda x: (order[x["status"]], x["distanceMeters"] if x["found"] else math.inf))
    safe = [x for x in results if x["status"] == "Available"]
    return {"found": bool(safe), "message": f"Nearest safe exit: '{safe[0]['exitName']}'." if safe else "No safe route available.",
            "startInDangerZone": p["startInDangerZone"], "exits": results}


def _exit(e, status: str, reason: str | None) -> dict:
    return {"exitName": e.name, "status": status, "found": False, "path": [], "distanceMeters": None, "clearanceViolated": False,
            "minDangerGapMeters": None, "reason": reason}


def _prepare(width, height, elements, active_danger, start_x, start_y) -> dict:
    """The walkable grid, the distance of each free cell to the active danger zones and the start cell (or a _fail dict)."""
    start_danger = [z for z in active_danger if inside(z, start_x, start_y)]  # a person inside one must be able to walk out
    dangers = [z for z in active_danger if z not in start_danger]
    walk = [e for e in elements if not e.is_blocked and e.type in WALKABLE]
    walls = [e for e in elements if e.type == "Wall"]
    openings = [e for e in elements if not e.is_blocked and e.type in ("Door", "Exit")]
    closed = [e for e in elements if e.is_blocked and e.type in ("Door", "Exit")]

    g = Grid(width, height, 0.5, 200_000)
    cell = g.cell
    # Walls are thin, so test overlap with the whole cell (a centre test would let routes slip through).
    free = (g.centre_in(walk) - g.overlapping(closed) - (g.overlapping(walls) - g.overlapping(openings))
            - g.overlapping(dangers))
    gap = {}
    for i in free:
        r, c = divmod(i, g.cols)
        gap[i] = min((_gap(z, c * cell, r * cell, cell) for z in dangers), default=math.inf)

    sc, sr = min(max(int(start_x / cell), 0), g.cols - 1), min(max(int(start_y / cell), 0), g.rows - 1)
    start = sr * g.cols + sc
    if start not in free:
        return _fail("Start point is on a wall, a closed door or inside an active danger zone."
                     if any(inside(a, start_x, start_y) for a in walk)
                     else "Start point is not inside a room, corridor or other walkable area.", bool(start_danger))
    return {"g": g, "free": free, "gap": gap, "start": start, "dangers": dangers, "start_danger": bool(start_danger),
            "startInDangerZone": bool(start_danger)}


def _search(p: dict, clearance: float, goal: set | None = None):
    """Dijkstra from the start; stops at the first goal cell when a goal is given (find_route), else covers the whole plan."""
    g, free, gap, cell = p["g"], p["free"], p["gap"], p["g"].cell
    dist, prev, heap, reached = {p["start"]: 0.0}, {}, [(0.0, p["start"])], -1
    while heap:
        du, u = heapq.heappop(heap)
        if du > dist.get(u, math.inf):
            continue
        if goal is not None and u in goal:
            reached = u
            break
        for v, step in g.neighbours(u, free):
            nd = du + step * cell * (NEAR_DANGER_COST_FACTOR if gap[v] < clearance else 1)
            if nd < dist.get(v, math.inf):
                dist[v], prev[v] = nd, u
                heapq.heappush(heap, (nd, v))
    return dist, prev, reached


def _route(p: dict, prev: dict, reached: int, exits, clearance: float, start_x: float, start_y: float) -> dict:
    g, gap, cell = p["g"], p["gap"], p["g"].cell
    # Cell path -> points; keep only turning points so the polyline stays small.
    cells = [reached]
    while cells[-1] in prev:
        cells.append(prev[cells[-1]])
    cells.reverse()
    path = [[round(start_x, 2), round(start_y, 2)]]
    for k in range(1, len(cells)):
        last = k == len(cells) - 1
        if not last and cells[k] - cells[k - 1] == cells[k + 1] - cells[k]:
            continue  # same direction: not a turn
        r, c = divmod(cells[k], g.cols)
        path.append([round((c + 0.5) * cell, 2), round((r + 0.5) * cell, 2)])
    distance = sum(math.dist(path[k - 1], path[k]) for k in range(1, len(path)))

    gr, gc = divmod(reached, g.cols)
    exit_name = next(e.name for e in exits if inside(e, gc * cell + cell / 2, gr * cell + cell / 2))  # same test as `goal`
    min_gap = round(min(gap[c] for c in cells), 2) if p["dangers"] else None
    violated = min_gap is not None and min_gap < clearance
    leave = " Start point is inside an active danger zone: leave immediately." if p["start_danger"] else ""
    message = (f"Route to exit '{exit_name}'.{leave}" if not violated else
               f"NO route keeps the required {fmt(clearance)} m clearance from active danger zones. The least-exposed route to "
               f"exit '{exit_name}' passes within {fmt(min_gap)} m of danger: it is NOT a safe route, verify on site.{leave}")
    return {"found": True, "message": message, "path": path, "distanceMeters": round(distance, 2), "exitName": exit_name,
            "startInDangerZone": p["start_danger"], "clearanceViolated": violated, "minDangerGapMeters": min_gap}


def _level(d: float) -> int:
    return 4 if d <= 0 else 3 if d <= HIGH_METERS else 2 if d <= MEDIUM_METERS else 1 if d <= LOW_METERS else 0


# shortcut: distance only, no material, ventilation, smoke or time model; replace with a trained model when data exists.
def fire_risk(width, height, elements, active_danger) -> dict:
    """Requirement 3.3 / 3.4: which areas a fire may reach next, and a risk heatmap. RULE-BASED ESTIMATE, not a fire-spread
    model: shortest path from an active danger zone through rooms, corridors and doors. Walls block it; doors do not (closed
    ones included: they slow a fire, they do not stop it). Cells: -1 outside, 0 none, 1 low, 2 medium, 3 high, 4 burning."""
    g = Grid(width, height, 1.0, 20_000)
    cell, cols = g.cell, g.cols
    danger_names = [z.name if z.name and z.name.strip() else "Danger zone" for z in active_danger]
    cells = [-1] * g.n
    if not active_danger:
        return {"active": False, "message": "No active fire on this floor, so there is nothing to predict.", "cols": cols,
                "rows": g.rows, "cellMeters": cell, "cells": cells, "areas": [], "activeDangerZones": danger_names}

    spaces = [e for e in elements if e.type in ("Room", "Corridor", "Door", "Exit", "SafeZone", "DangerZone")]
    doors = [e for e in elements if e.type in ("Door", "Exit")]
    indoor = g.centre_in(spaces)
    open_cells = indoor - (g.overlapping([e for e in elements if e.type == "Wall"]) - g.overlapping(doors))
    for i in indoor:
        cells[i] = 0
    dist = {i: 0.0 for i in open_cells & g.overlapping(active_danger)}
    heap = [(0.0, i) for i in dist]
    heapq.heapify(heap)
    while heap:  # multi-source Dijkstra
        du, u = heapq.heappop(heap)
        if du > dist[u]:
            continue
        for v, step in g.neighbours(u, open_cells):
            nd = du + step * cell
            if nd < dist.get(v, math.inf):
                dist[v] = nd
                heapq.heappush(heap, (nd, v))
    for i in open_cells:
        cells[i] = _level(dist.get(i, math.inf))

    areas = []
    for e in (e for e in elements if e.type in ("Room", "Corridor")):
        nearest = min((dist.get(i, math.inf) for i in open_cells & g.centre_in([e])), default=math.inf)
        areas.append({"name": e.name if e.name and e.name.strip() else e.type, "type": e.type, "bounds": [e.x, e.y, e.width, e.height],
                      "level": LEVEL_NAMES[_level(nearest)], "distanceMeters": round(nearest, 1) if math.isfinite(nearest) else None})
    areas.sort(key=lambda a: a["distanceMeters"] if a["distanceMeters"] is not None else math.inf)
    nxt = [a for a in areas if a["level"] in ("High", "Medium")][:3]
    message = ("Next at risk: " + ", ".join(f"{a['name']} ({fmt(a['distanceMeters'])} m from the fire, {a['level']})" for a in nxt) + "."
               if nxt else f"No other room or corridor within {fmt(MEDIUM_METERS, 0)} m of the fire.")
    return {"active": True, "message": message, "cols": cols, "rows": g.rows, "cellMeters": cell, "cells": cells,
            "areas": areas, "activeDangerZones": danger_names}

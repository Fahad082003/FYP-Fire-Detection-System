"""Automatic live test: fire video -> test camera -> AI service -> incident -> danger zone -> evacuation route changes -> risk.

Start everything first (.\\start.ps1, no simulator), then in ai-service:
    .\\.venv\\Scripts\\python tools\\live_test.py
It adds camera "cam-live" and floor plan "Live Test", checks each step and prints a pass/fail list.
Watch the dashboard while it runs. Ctrl+C at the end removes both and resolves the test incident.
"""
import argparse
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).parent))
from mjpeg_camera import serve  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
ZONE, CAM, PLAN = "Ground Floor - Lobby", "cam-live", "Live Test"
START = {"x": 6, "y": 5}  # a person in the corridor: West exit is nearer until the fire blocks it


def dotenv(path: Path) -> dict:
    env = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip()
    return env


class Api:
    def __init__(self, base: str):
        self.base, self.token = base.rstrip("/"), None

    def call(self, method: str, path: str, body=None):
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                raw = r.read()
                return r.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(errors="replace")


def up(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=5):
            return True
    except OSError:
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--video", type=Path, default=ROOT / "ai-service" / "samples" / "fire-test.avi")
    ap.add_argument("--api", default="http://localhost:5291")
    ap.add_argument("--ai", default="http://127.0.0.1:8008")
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--env", type=Path, default=ROOT / ".env", help="admin login is read from here")
    ap.add_argument("--wait", type=int, default=150, help="seconds to wait for the AI incident")
    ap.add_argument("--allow-real-alerts", action="store_true", help="run even though SMS/Email are configured")
    ap.add_argument("--no-hold", action="store_true", help="clean up right after the checks")
    args = ap.parse_args()

    env = dotenv(args.env)
    live = [k for k in ("Notifications__Sms__TwilioAccountSid", "Notifications__Email__SmtpHost") if env.get(k)]
    if live and not args.allow_real_alerts:
        print("STOP: .env has real SMS/Email settings (" + ", ".join(live) + "). This test makes a real fire incident.\n"
              "Turn on Safe Test Mode first (user guide, chapter 4), restart the API, then run this again.")
        return 2
    if not up(args.api + "/health"):
        print(f"STOP: the API is not running at {args.api}. Run .\\start.ps1 first.")
        return 2
    if not up(args.ai + "/health"):
        print(f"STOP: the AI service is not running at {args.ai}. Run .\\start.ps1 first.")
        return 2
    cap = cv2.VideoCapture(str(args.video))
    ok, frame = cap.read()
    cap.release()
    if not ok:
        print(f"STOP: cannot read video {args.video}")
        return 2
    h, w = frame.shape[:2]

    api = Api(args.api)
    status, body = api.call("POST", "/api/auth/login", {"email": env.get("Seed__AdminEmail"), "password": env.get("Seed__AdminPassword")})
    if status != 200:
        print(f"STOP: admin login failed ({status}). Check Seed__AdminEmail / Seed__AdminPassword in .env.")
        return 2
    api.token = body["token"]

    results = []

    def check(name, passed, detail=""):
        results.append(passed)
        print(f"  {'PASS' if passed else 'FAIL'}  {name}" + (f": {detail}" if detail else ""))

    server = serve(str(args.video), port=args.port)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    plan_id = None
    try:
        print(f"Test camera: http://127.0.0.1:{args.port}/video  ({args.video.name})")
        camera = {"name": "Live Test", "feedType": "CCTV", "streamUrl": f"http://127.0.0.1:{args.port}/video",
                  "streamFormat": "Mjpeg", "locationZone": ZONE}
        exists = api.call("GET", f"/api/cameras/{CAM}")[0] == 200
        status, _ = api.call("PUT", f"/api/cameras/{CAM}", camera) if exists else api.call("POST", "/api/cameras", {"id": CAM, **camera})
        check("camera cam-live saved (Devices)", status in (200, 201), str(status))

        for p in api.call("GET", "/api/floorplans")[1] or []:
            if p["name"] == PLAN:
                api.call("DELETE", f"/api/floorplans/{p['id']}")
        status, plan = api.call("POST", "/api/floorplans", {
            "name": PLAN, "building": "Test", "floorNumber": 0, "widthMeters": 20, "heightMeters": 10, "elements": [
                {"type": "Corridor", "name": "Lobby corridor", "x": 0, "y": 3, "width": 20, "height": 4},
                {"type": "Exit", "name": "West", "x": 0, "y": 3, "width": 1, "height": 4},
                {"type": "Exit", "name": "East", "x": 19, "y": 3, "width": 1, "height": 4},
                {"type": "DangerZone", "name": "Lobby fire", "x": 3, "y": 3, "width": 1.5, "height": 4, "zone": ZONE}]})
        check("floor plan 'Live Test' saved", status == 201, str(status))
        if status != 201:
            return 1
        plan_id = plan["id"]
        status, _ = api.call("PUT", f"/api/cameras/{CAM}/calibration", {
            "floorPlanId": plan_id, "imagePoints": [[0, h / 2], [w, h / 2], [w, h], [0, h]], "planPoints": [[2, 3], [18, 3], [18, 7], [2, 7]]})
        check("camera calibrated to the plan", status == 200, str(status))

        already = [i for i in api.call("GET", "/api/incidents?resolved=false")[1] if i["zone"] == ZONE]
        if already:
            print(f"  NOTE  {len(already)} open incident(s) in {ZONE} already: resolve them for a clean before/after route.")
        before = api.call("POST", f"/api/floorplans/{plan_id}/route", START)[1]
        check("route before the fire", before["found"], f"{before['exitName']} exit, {before['distanceMeters']} m, ~{before['estimatedSeconds']} s")

        print(f"Waiting for the AI to see the fire (up to {args.wait} s; it picks up new cameras within a minute)...")
        started, incident = time.monotonic(), None
        while time.monotonic() - started < args.wait and not incident:
            time.sleep(3)
            incident = next((i for i in api.call("GET", "/api/incidents?resolved=false")[1] if i["cameraId"] == CAM and i["fireDetected"]), None)
        check("AI opened a fire incident", incident is not None,
              f"#{incident['id']} {incident['severity']} after {time.monotonic() - started:.0f} s" if incident else "none (is the AI service running with its device key?)")
        if incident:
            after = api.call("POST", f"/api/floorplans/{plan_id}/route", START)[1]
            check("danger zone active", "Lobby fire" in after["activeDangerZones"], ", ".join(after["activeDangerZones"]))
            check("route avoids the fire", after["found"] and after["exitName"] != before["exitName"],
                  f"{after['exitName']} exit, {after['distanceMeters']} m")
            risk = api.call("GET", f"/api/floorplans/{plan_id}/risk")[1]
            check("fire risk heatmap active", risk["active"], risk["message"])

        print(f"\n{sum(results)}/{len(results)} checks passed.")
        if not args.no_hold:
            print("Dashboard: Live Test tile, Alert Logs, Evacuation Route -> 'Live Test' plan.  Press Ctrl+C to clean up.")
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        print("Cleaning up: camera, plan, test incident...")
        # Video off first: the AI keeps a deleted camera until its next sync, and frames from it would open an "Unassigned" incident.
        server.camera.stopped.set()
        server.shutdown()
        # OpenCV still drains buffered frames for a few seconds: delete only once the AI stopped sending
        # ("camera lost" needs 10 s of silence, so none is raised).
        sent, quiet_since, deadline = None, time.monotonic(), time.monotonic() + 30
        while time.monotonic() - quiet_since < 3 and time.monotonic() < deadline:
            time.sleep(1)
            try:
                with urllib.request.urlopen(args.ai + "/health", timeout=5) as r:
                    now = next((c["framesSent"] for c in json.loads(r.read())["cameras"] if c["cameraId"] == CAM), None)
            except OSError:
                break
            if now != sent:
                sent, quiet_since = now, time.monotonic()
        for i in api.call("GET", "/api/incidents?resolved=false")[1] or []:
            if i["cameraId"] == CAM:  # every incident this test opened, so the alarm stops
                api.call("POST", f"/api/incidents/{i['id']}/resolve")
        api.call("DELETE", f"/api/cameras/{CAM}")
        if plan_id:
            api.call("DELETE", f"/api/floorplans/{plan_id}")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())

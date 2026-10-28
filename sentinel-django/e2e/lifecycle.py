"""Live camera lifecycle with the REAL AI service and models (no mocks). Usage (from sentinel-django):

    .venv\\Scripts\\python e2e\\lifecycle.py

Starts, on a throwaway database (test_SentinelDb_e2e, dropped at the end): the Django server on :5399, the AI service from
ai-service/.venv on :8018 (unmodified, real models), and a test camera that serves samples/fire-test.avi as MJPEG on :8095.
Then it kills and restarts them to check the camera lifecycle, prints every step with measured times, and finally runs
ai-service/tools/live_test.py unchanged against Django. Nothing touches SentinelDb, a real camera, sprinklers or notifications.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from run import DB, PORT, ROOT, SERVER_ENV, create_db, drop_db, wait_for

AI_DIR = ROOT.parent / "ai-service"
AI_PYTHON = AI_DIR / ".venv" / "Scripts" / "python.exe"
AI_PORT, CAM_PORT, CAM = 8018, 8095, "cam-life"
API = f"http://127.0.0.1:{PORT}"
ENV = {**SERVER_ENV, "AiService__BaseUrl": f"http://127.0.0.1:{AI_PORT}"}
LOGS = ROOT / "e2e" / "lifecycle-logs"
results: list[tuple[str, bool, str]] = []
t0 = time.monotonic()


def log(message: str) -> None:
    print(f"[{time.monotonic() - t0:7.1f}s] {message}", flush=True)


def check(name: str, passed: bool, detail: str = "") -> None:
    results.append((name, passed, detail))
    log(f"{'PASS' if passed else 'FAIL'}  {name}{': ' + detail if detail else ''}")


class Proc:
    """A child process with its output in lifecycle-logs/<name>.log."""

    def __init__(self, name, args, cwd, env=None):
        self.name, self.args, self.cwd, self.env, self.p = name, args, cwd, env, None

    def start(self):
        out = open(LOGS / f"{self.name}.log", "a", encoding="utf-8")
        self.p = subprocess.Popen(self.args, cwd=self.cwd, env={**os.environ, **(self.env or {})}, stdout=out, stderr=subprocess.STDOUT)
        return self

    def kill(self):
        if self.p and self.p.poll() is None:
            self.p.kill()
            self.p.wait(10)


token = None


def api(method, path, body=None, timeout=15):
    headers = {"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})}
    request = urllib.request.Request(API + path, data=None if body is None else json.dumps(body).encode(), method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as r:
            raw = r.read()
            return r.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")


def until(what: str, condition, timeout: float, every: float = 0.5):
    """Seconds until condition() is truthy, or None after `timeout`."""
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        try:
            if condition():
                return time.monotonic() - start
        except OSError:
            pass
        time.sleep(every)
    log(f"timeout after {timeout:.0f} s waiting for: {what}")
    return None


def ai_camera():
    status = api("GET", "/api/ai/status")[1]
    cams = (status.get("service") or {}).get("cameras", []) if isinstance(status, dict) else []
    return next((c for c in cams if c["cameraId"] == CAM), None), status


def ai_stopped_posting() -> bool:
    """The AI worker posts synchronously and reports "reconnecting" (read loop ended) or "cannot open ..." (before the loop)
    only outside its read/post loop, so from then on no frame of the camera is in flight."""
    cam = ai_camera()[0] or {}
    return cam.get("state") == "reconnecting" or str(cam.get("error") or "").startswith("cannot open")


def opened_before_delete(incident: dict) -> bool:
    """A fire incident of the test camera evaluated while the camera still existed: only then does the alert engine give it the
    camera's zone and name (a deleted camera's frame gets "Unassigned"). Unassigned and camera-lost incidents never match."""
    return (incident["source"] == "Vision" and incident["zone"] == "Ground Floor - Lobby"
            and "on Lifecycle Cam" in incident["message"])


def camera_status():
    return api("GET", f"/api/cameras/{CAM}")[1]["status"]


def open_incidents(source=None):
    items = api("GET", "/api/incidents?resolved=false")[1]
    return [i for i in items if i["cameraId"] == CAM and (source is None or i["source"] == source)]


def main() -> int:
    global token
    LOGS.mkdir(exist_ok=True)
    for f in LOGS.glob("*.log"):
        f.unlink()
    create_db()
    workdir = Path(tempfile.mkdtemp(prefix="sentinel-lifecycle-"))
    (workdir / "ai.json").write_text(json.dumps({"listen": f"127.0.0.1:{AI_PORT}", "cameras": []}))
    django = Proc("django", [sys.executable, "-m", "uvicorn", "config.asgi:application", "--host", "127.0.0.1", "--port", str(PORT)], ROOT, ENV)
    ai = Proc("ai", [str(AI_PYTHON), "-m", "sentinel_ai", "--config", str(workdir / "ai.json")], AI_DIR,
              {"SENTINEL_API_URL": API, "SENTINEL_DEVICE_KEY": ENV["Device__ApiKey"]})
    camera = Proc("camera", [str(AI_PYTHON), "tools/mjpeg_camera.py", "samples/fire-test.avi", "--port", str(CAM_PORT)], AI_DIR)
    procs = [django, ai, camera]
    try:
        for p in procs:
            p.start()
        wait_for(f"{API}/health", 60)
        wait_for(f"http://127.0.0.1:{AI_PORT}/health", 120)
        wait_for(f"http://127.0.0.1:{CAM_PORT}/video", 30)
        status, body = api("POST", "/api/auth/login", {"email": ENV["Seed__AdminEmail"], "password": ENV["Seed__AdminPassword"]})
        token = body["token"]
        ai_health = api("GET", "/api/ai/status")[1]
        check("1. services start; Django reports the AI service online with all models", ai_health["online"] and ai_health["service"]["status"] == "ok",
              ", ".join(f"{m['name']}={'loaded' if m['loaded'] else 'MISSING'}" for m in ai_health["service"]["models"]))

        status, _ = api("POST", "/api/cameras", {"id": CAM, "name": "Lifecycle Cam", "feedType": "CCTV", "streamFormat": "Mjpeg",
                                                 "streamUrl": f"http://127.0.0.1:{CAM_PORT}/video", "locationZone": "Ground Floor - Lobby"})
        assert status == 201, status
        took = until("AI posts frames for the new camera", lambda: (ai_camera()[0] or {}).get("framesSent", 0) > 0, 90)
        check("2. camera added on the server -> the AI service picks it up and frames arrive", took is not None,
              f"{took:.1f} s (AI syncs its camera list every 60 s)" if took else "")
        check("   frames mark the camera Online", until("Online", lambda: camera_status() == "Online", 10) is not None)
        took = until("fire incident", lambda: any(i["fireDetected"] for i in open_incidents("Vision")), 150, 1)
        check("   real fire detection (D-Fire test video) opens a fire incident", took is not None, f"{took:.1f} s" if took else "")
        fire = next((i for i in open_incidents("Vision") if i["fireDetected"]), None)

        log("disconnecting the camera (test camera process killed)")
        camera.kill()
        took = until("AI reports the stream error", lambda: (ai_camera()[0] or {}).get("state") in ("error", "reconnecting"), 30)
        check("4. camera drop: AI health shows the camera not running (banner: CAMERA OFFLINE)", took is not None, f"{took:.1f} s" if took else "")
        took = until("camera-lost incident", lambda: open_incidents("CameraLost"), 40)
        check("   camera-lost incident raised by the watchdog", took is not None, f"{took:.1f} s after the disconnect" if took else "")
        lost = open_incidents("CameraLost")
        check("   ...at High severity, because a fire incident is open in its zone", bool(lost) and lost[0]["severity"] == "High",
              lost[0]["severity"] if lost else "")
        check("   database status Offline", camera_status() == "Offline")

        log("reconnecting the camera")
        camera.start()
        took = until("frames again -> Online", lambda: camera_status() == "Online", 60)
        check("5. reconnect: frames again and status Online", took is not None, f"{took:.1f} s" if took else "")
        still = {i["id"] for i in open_incidents()}
        check("6. reconnect does not resolve the open fire incident or the camera-lost incident",
              fire is not None and fire["id"] in still and bool(lost) and lost[0]["id"] in still)

        log("stopping the AI service")
        ai.kill()
        took = until("AI offline", lambda: not api("GET", "/api/ai/status")[1]["online"], 10)
        check("7. AI stopped: /api/ai/status online=false (banner: MONITORING DEGRADED / AI OFFLINE, never nominal)", took is not None,
              f"{took:.1f} s" if took else "")
        took = until("silence -> Offline", lambda: camera_status() == "Offline", 30)
        check("   no frames without the AI: camera Offline again (camera-lost alarm path)", took is not None, f"{took:.1f} s" if took else "")
        log("restarting the AI service")
        ai.start()
        took = until("AI online and frames again", lambda: camera_status() == "Online" and api("GET", "/api/ai/status")[1]["online"], 150)
        check("8. AI restarted: online again, the camera list is re-read, frames resume", took is not None, f"{took:.1f} s" if took else "")

        for i in open_incidents():
            api("POST", f"/api/incidents/{i['id']}/resolve")
        log("D-17: camera dies and the server restarts within a second")
        camera.kill()
        # A late buffered frame reaching the restarted server would replace the start-up grace (seen once: alarm after 12 s).
        drained = until("AI stops posting for the dead camera", ai_stopped_posting, 30)
        django.kill()
        django.start()
        wait_for(f"{API}/health", 60)
        restarted = time.monotonic()
        took = until("camera-lost after restart", lambda: open_incidents("CameraLost"), 100, 1)
        check("10. server restart while the camera is dead: camera-lost raised after the 60 s start-up grace",
              drained is not None and took is not None and took > 45,
              "AI never stopped posting within 30 s of the camera dying, so the restart could not be tested cleanly"
              if drained is None else f"{took:.1f} s after the restart" if took is not None else "no camera-lost incident within 100 s")
        check("    and the database status is Offline", camera_status() == "Offline")
        camera.start()
        until("frames again", lambda: camera_status() == "Online", 60)

        for i in open_incidents():
            api("POST", f"/api/incidents/{i['id']}/resolve")
        log("deleting the camera while it streams")
        status, _ = api("DELETE", f"/api/cameras/{CAM}")
        took = until("AI stops the deleted camera", lambda: (ai_camera()[0] or {}).get("state") == "stopped", 30)
        check("11. delete: 204, and the AI service stops that camera on the first refused frame", status == 204 and took is not None,
              f"{took:.1f} s" if took else "")
        # A frame in flight while the incidents above were resolved can open a new fire incident just before the delete
        # (the delete set its CameraId to NULL). Resolved only now that the AI has stopped, so no later frame can add one.
        before_delete = [i for i in api("GET", "/api/incidents?resolved=false")[1] if opened_before_delete(i)]
        for i in before_delete:
            api("POST", f"/api/incidents/{i['id']}/resolve")
        log(f"resolved {len(before_delete)} fire incident(s) opened by frames evaluated before the delete")
        time.sleep(20)
        unassigned = [i for i in api("GET", "/api/incidents?resolved=false")[1] if i["zone"] == "Unassigned" or "Lifecycle Cam" in i["message"]]
        check("    no camera-lost or Unassigned alarm for the deleted camera (20 s later)", not unassigned, f"{len(unassigned)} found")
        camera.kill()

        env_file = workdir / "live_test.env"  # the admin login live_test.py reads; never the real .env
        env_file.write_text(f"Seed__AdminEmail={ENV['Seed__AdminEmail']}\nSeed__AdminPassword={ENV['Seed__AdminPassword']}\n")
        log("12. running ai-service/tools/live_test.py unchanged against Django (floor plan, calibration, fire, danger zone, route, risk)")
        code = subprocess.call([str(AI_PYTHON), "tools/live_test.py", "--api", API, "--ai", f"http://127.0.0.1:{AI_PORT}", "--env", str(env_file),
                                "--port", "8096", "--no-hold"], cwd=AI_DIR)
        check("12. live_test.py (written for the .NET API) passes against Django", code == 0, f"exit code {code}")
    finally:
        for p in procs:
            p.kill()
        from django.db import connections
        connections.close_all()
        drop_db()
        log(f"dropped {DB}")

    failed = [r for r in results if not r[1]]
    log(f"{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

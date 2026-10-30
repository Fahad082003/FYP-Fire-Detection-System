"""Browser tests against the real Django server, on a throwaway database. Usage (from sentinel-django):

    .venv\\Scripts\\python e2e\\run.py            (first time: cd e2e; npm install --offline)

1. Creates SQL Server database test_SentinelDb_e2e from schema/sentinel.sql (dropped again at the end).
2. Starts a stub AI service on :8019 that the tests switch between healthy, offline, model missing and camera error.
3. Starts the Django server (uvicorn) on :5399 with test keys; .env is never read.
4. Runs the browser tests (Microsoft Edge through playwright-core, no browser download).
SentinelDb and the real AI service are never touched; no sprinkler, SMS, e-mail or push leaves the machine.
"""
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = "test_SentinelDb_e2e"
PORT, AI_PORT = 5399, 8019
SERVER_ENV = {
    "SENTINEL_NO_DOTENV": "1",
    "ConnectionStrings__Sentinel": f"Server=localhost;Database={DB};Trusted_Connection=True;TrustServerCertificate=True",
    "Jwt__SigningKey": "e2e-only-signing-key-0123456789abcdefghij",
    "Device__ApiKey": "e2e-device-key", "Device__SensorApiKey": "e2e-sensor-key",
    "Seed__AdminEmail": "admin@e2e.local", "Seed__AdminPassword": "E2e-Passw0rd!", "Seed__DemoData": "true",
    "AiService__BaseUrl": f"http://127.0.0.1:{AI_PORT}",
    "Notifications__Email__Recipients": "",  # nothing is sent anywhere
    "Alerts__SensorStaleSeconds": "5",  # the sensor-staleness test must not wait a minute
}

HEALTH = {
    "ok": {"status": "ok", "models": [{"name": "person", "file": "yolox_tiny.onnx", "license": "Apache-2.0", "categories": ["Human"], "threshold": 0.45,
                                       "loaded": True, "error": None},
                                      {"name": "fire_smoke", "file": "fire_smoke_yolox.onnx", "license": "own", "categories": ["Fire", "Smoke"],
                                       "threshold": 0.4, "loaded": True, "error": None}],
           "capabilities": ["Fire", "Human", "Smoke"], "inference": {"count": 3, "avgMs": 410.5, "lastMs": 400.1}, "cameras": []},
}
HEALTH["model-missing"] = {**HEALTH["ok"], "status": "degraded",
                           "models": [HEALTH["ok"]["models"][0], {**HEALTH["ok"]["models"][1], "loaded": False, "error": "file not found"}]}
HEALTH["camera-error"] = {**HEALTH["ok"], "cameras": [{"cameraId": "cam-warehouse", "kind": "stream", "source": "rtsp://***@10.0.0.5/live",
                                                       "simulated": False, "state": "error", "error": "cannot open stream source",
                                                       "framesSent": 0, "postFailures": 0}]}
stub = {"mode": "ok"}


class StubAi(BaseHTTPRequestHandler):
    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if stub["mode"] == "offline":
            return self._send(503, {"error": "stub offline"})
        self._send(200, HEALTH[stub["mode"]])

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path == "/__mode":  # test control
            stub["mode"] = json.loads(body)["mode"]
            return self._send(200, stub)
        self._send(503, {"error": "fire model not loaded"})  # /infer: the browser test checks this error reaches the page

    def log_message(self, *args):
        pass


def sql(statement: str) -> None:
    import pyodbc  # the driver the Django backend uses
    with pyodbc.connect("Driver={ODBC Driver 18 for SQL Server};Server=localhost;Database=master;Trusted_Connection=yes;"
                        "TrustServerCertificate=yes", autocommit=True) as c:
        c.execute(statement)


def drop_db() -> None:
    assert DB.startswith("test_")
    sql(f"IF DB_ID('{DB}') IS NOT NULL BEGIN ALTER DATABASE [{DB}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE; DROP DATABASE [{DB}]; END")


def create_db() -> None:
    drop_db()  # a leftover from an interrupted run
    sql(f"CREATE DATABASE [{DB}]")
    os.environ.update(SERVER_ENV)
    os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings"
    sys.path.insert(0, str(ROOT))
    import django
    django.setup()
    from django.db import connection
    from sentinel.schema import run_schema_script
    assert connection.settings_dict["NAME"] == DB
    run_schema_script(connection)
    connection.close()


def require_free_ports(*ports: int) -> None:
    """A leftover server from an interrupted run would answer instead of this run's: uvicorn exits when its port is taken while
    the old one still answers /health, and on Windows the stub AI service (SO_REUSEADDR) silently shares the port."""
    busy = []
    for port in ports:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                busy.append(port)
    if busy:
        raise SystemExit(f"Port(s) {busy} already in use, e.g. by a server left from an interrupted run. Stop it first; nothing was changed.")


def wait_for(url: str, seconds: float = 30) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(url, timeout=2).close()
            return
        except OSError:
            time.sleep(0.3)
    raise RuntimeError(f"{url} did not answer within {seconds} s")


def main() -> int:
    require_free_ports(PORT, AI_PORT)  # before create_db: a refused run changes nothing
    create_db()
    ai = ThreadingHTTPServer(("127.0.0.1", AI_PORT), StubAi)
    threading.Thread(target=ai.serve_forever, daemon=True).start()
    log = open(ROOT / "e2e" / "server.log", "w", encoding="utf-8")
    server = subprocess.Popen([sys.executable, "-m", "uvicorn", "config.asgi:application", "--host", "127.0.0.1", "--port", str(PORT)],
                              cwd=ROOT, env={**os.environ, **SERVER_ENV}, stdout=log, stderr=subprocess.STDOUT)
    try:
        wait_for(f"http://127.0.0.1:{PORT}/health")
        env = {**os.environ, "BASE_URL": f"http://127.0.0.1:{PORT}", "AI_CONTROL": f"http://127.0.0.1:{AI_PORT}/__mode",
               "DEVICE_KEY": SERVER_ENV["Device__ApiKey"], "SENSOR_KEY": SERVER_ENV["Device__SensorApiKey"], "ADMIN_EMAIL": SERVER_ENV["Seed__AdminEmail"],
               "ADMIN_PASSWORD": SERVER_ENV["Seed__AdminPassword"]}
        # Extra arguments go to node's test runner, e.g. --test-name-pattern=devices
        return subprocess.call(["node", "--test", "--test-concurrency=1", "--test-timeout=120000", *sys.argv[1:], "dashboard.test.js"],
                               cwd=ROOT / "e2e", env=env)
    finally:
        server.terminate()
        server.wait(10)
        log.close()
        ai.shutdown()
        from django.db import connections
        connections.close_all()
        drop_db()


if __name__ == "__main__":
    sys.exit(main())

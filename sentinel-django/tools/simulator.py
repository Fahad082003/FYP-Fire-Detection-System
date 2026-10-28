"""FireVision AI field simulator (port of backend/Sentinel.Simulator). It stands in for the systems that feed the server:
  * AI vision pipeline -> POST /api/detections (boxes, danger zones, fire severity); the .NET version used the SignalR hub
  * IoT gateway        -> POST /api/iot/telemetry (smoke ppm / temperature degrees C)
  * Sprinkler relays   -> GET  /api/iot/sprinklers (active relays suppress the simulated fire)
Every frame and reading is marked simulated, so it is labelled SIMULATED and never switches real relays by itself.

Usage (from sentinel-django):  .venv\\Scripts\\python tools\\simulator.py --device-key <Device__ApiKey> --ignite "Ground Floor - Warehouse"
Standard library only.
"""
import argparse
import json
import math
import os
import random
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

HEAT_COLS, HEAT_ROWS = 32, 18


def log(level: str, message: str) -> None:
    print(f"{datetime.now():%H:%M:%S} {level:<5} {message}", flush=True)


class Fire:
    def __init__(self):
        self.intensity, self.center, self.suppressing = 0.0, (0.5, 0.6), False

    @property
    def burning(self) -> bool:
        return self.intensity > 0.05

    @property
    def level(self) -> str:
        """AI-style severity from intensity."""
        i = self.intensity
        return "Low" if i < 0.3 else "Medium" if i < 0.55 else "High" if i < 0.8 else "Critical"


class Walker:
    def __init__(self, track_id, x, y, vx, vy):
        self.track_id, self.x, self.y, self.vx, self.vy = track_id, x, y, vx, vy

    def step(self, dt, flee_from, rng):
        if flee_from:
            dx, dy = self.x - flee_from[0], self.y - flee_from[1]
            dist = max(math.hypot(dx, dy), 1e-3)
            self.vx += dx / dist * 0.25 * dt
            self.vy += dy / dist * 0.12 * dt
        speed = math.hypot(self.vx, self.vy)
        if speed > 0.18:
            self.vx, self.vy = self.vx / speed * 0.18, self.vy / speed * 0.18
        self.vx += (rng.random() - 0.5) * 0.04
        self.vy += (rng.random() - 0.5) * 0.02
        self.x += self.vx * dt
        self.y += self.vy * dt
        if not 0.04 <= self.x <= 0.92:
            self.vx, self.x = -self.vx, min(max(self.x, 0.04), 0.92)
        if not 0.35 <= self.y <= 0.78:
            self.vy, self.y = -self.vy, min(max(self.y, 0.35), 0.78)


class Simulator:
    def __init__(self, args):
        self.args, self.rng, self.lock = args, random.Random(), threading.Lock()
        self.fires: dict[str, Fire] = {}
        self.active_sprinkler_zones: set[str] = set()
        self.cameras, self.sensors = [], []

    def http(self, method, path, body=None):
        request = urllib.request.Request(f"{self.args.base_url}/{path}", method=method, data=None if body is None else json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json", "X-Device-Key": self.args.device_key})
        with urllib.request.urlopen(request, timeout=10) as r:
            return json.loads(r.read() or b"null")

    def fire(self, zone) -> Fire:
        with self.lock:
            return self.fires.setdefault(zone, Fire())

    def ignite(self, zone):
        f = self.fire(zone)
        with self.lock:
            f.intensity = max(f.intensity, 0.12)
            f.center = (0.25 + self.rng.random() * 0.5, 0.5 + self.rng.random() * 0.2)
            f.suppressing = False
        log("WARN", f"FIRE ignited in {zone}")

    def run(self):
        try:
            cameras = self.http("GET", "api/cameras")
            self.sensors = self.http("GET", "api/iot/sensors")
        except urllib.error.HTTPError as e:
            raise SystemExit(f"GET from the server returned {e.code}. Is the device key correct?")
        for c in cameras:
            drone = c["feedType"] == "Drone"
            suffix = c["id"][-3:]
            walkers = [Walker(f"{suffix}-{i}", 0.1 + self.rng.random() * 0.75, 0.4 + self.rng.random() * 0.35,
                              (self.rng.random() - 0.5) * 0.16, (self.rng.random() - 0.5) * 0.06) for i in range(1, self.rng.randint(1, 4) + 1)]
            self.cameras.append({"id": c["id"], "zone": c["locationZone"], "w": 1920 if drone else 1280, "h": 1080 if drone else 720, "walkers": walkers})
        if not self.cameras and not self.sensors:
            raise SystemExit("No cameras or sensors registered. Start the server with Seed__DemoData=true (or add devices).")
        log("INFO", f"Simulating {len(self.cameras)} camera(s) and {len(self.sensors)} sensor(s)")
        for zone in self.args.ignite:
            self.ignite(zone)
        for loop in (self.world_loop, self.sprinkler_loop, self.telemetry_loop):
            threading.Thread(target=loop, daemon=True).start()
        self.vision_loop()

    def zones(self):
        return {c["zone"] for c in self.cameras} | {s["locationZone"] for s in self.sensors}

    def world_loop(self):
        """Fire physics: random ignition, logistic growth, suppression while the zone's sprinklers are on."""
        dt = 0.5
        while True:
            for zone in self.zones():
                f = self.fire(zone)
                if not f.burning and self.rng.random() < self.args.fire_probability * dt:
                    self.ignite(zone)
                with self.lock:
                    if not f.burning:
                        f.intensity = 0
                        continue
                    if zone in self.active_sprinkler_zones:
                        f.intensity = max(0.0, f.intensity - 0.025 * dt)
                        if not f.suppressing:
                            f.suppressing = True
                            log("INFO", f"Sprinklers suppressing fire in {zone}")
                        if not f.burning:
                            log("INFO", f"Fire in {zone} extinguished")
                    else:
                        f.intensity = min(1.0, f.intensity + 0.035 * dt * (1.2 - f.intensity))
            time.sleep(dt)

    def sprinkler_loop(self):
        while True:
            try:
                self.active_sprinkler_zones = {s["zone"] for s in self.http("GET", "api/iot/sprinklers") if s["isActive"]}
            except OSError as e:
                log("WARN", f"Sprinkler poll failed: {e}")
            time.sleep(3)

    def telemetry_loop(self):
        while True:
            readings = []
            for s in self.sensors:
                f, noise = self.fire(s["locationZone"]), self.rng.gauss(0, 1)
                value = (round(22 + f.intensity * 75 + noise * 0.6, 2) if s["type"] == "Temperature"
                         else round(max(0.0, 15 + f.intensity * 620 + noise * 4), 1))
                readings.append({"sensorId": s["id"], "value": value})
            if readings:
                try:
                    self.http("POST", "api/iot/telemetry", {"readings": readings, "simulated": True})
                except urllib.error.HTTPError as e:
                    log("WARN", f"Telemetry rejected ({e.code}): {e.read(300).decode(errors='replace')}")
                except OSError as e:
                    log("WARN", f"Telemetry post failed: {e}")
            time.sleep(self.args.telemetry_interval)

    def vision_loop(self):
        interval = 1 / self.args.fps
        while True:
            for cam in self.cameras:
                try:
                    self.http("POST", "api/detections", self.frame(cam, interval))
                except urllib.error.HTTPError as e:
                    log("WARN", f"Frame from {cam['id']} rejected ({e.code}): {e.read(300).decode(errors='replace')}")
                except OSError as e:
                    log("WARN", f"Frame post failed: {e}")
            time.sleep(interval)

    def frame(self, cam, dt) -> dict:
        """One AI-style inference frame in source-frame pixels."""
        f = self.fire(cam["zone"])
        with self.lock:
            intensity, center, burning, level = f.intensity, f.center, f.burning, f.level
        w, h = cam["w"], cam["h"]
        detections, zones, points = [], [], []
        if burning:
            size = 0.06 + intensity * 0.28
            fw, fh = size * w, size * h * 1.2
            detections.append({"label": "fire", "category": "Fire", "confidence": round(0.7 + intensity * 0.29, 2),
                               "bbox": [center[0] * w - fw / 2, center[1] * h - fh / 2, fw, fh], "trackId": f"{cam['id']}-F"})
            if intensity > 0.25:
                sw, sh = fw * 1.6, fh * 0.9
                detections.append({"label": "smoke", "category": "Smoke", "confidence": round(0.6 + intensity * 0.3, 2),
                                   "bbox": [max(0, center[0] * w - sw / 2), max(0, center[1] * h - fh / 2 - sh), sw, sh], "trackId": f"{cam['id']}-S"})
            radius = size * 1.4
            zones.append({"label": "danger zone", "level": "Critical" if intensity > 0.6 else "Danger",
                          "polygon": [[(center[0] + radius * math.cos(a)) * w, (center[1] + radius * 0.8 * math.sin(a)) * h]
                                      for a in (i * math.pi / 5 for i in range(10))]})
            zones.append({"label": "assembly point", "level": "Safe",
                          "polygon": [[0.80 * w, 0.40 * h], [0.97 * w, 0.40 * h], [0.97 * w, 0.90 * h], [0.80 * w, 0.90 * h]]})
        for walker in cam["walkers"]:
            # Walkers flee when close to the fire. Who is "in danger" is decided by the server (feet inside a danger zone).
            near = burning and math.hypot(walker.x - center[0], walker.y - center[1]) < 0.12 + intensity * 0.3
            walker.step(dt, center if near else None, self.rng)
            bw, bh = 0.045 * w, 0.2 * h
            detections.append({"label": "person", "category": "Human", "confidence": round(0.82 + self.rng.random() * 0.16, 2),
                               "bbox": [walker.x * w - bw / 2, walker.y * h - bh, bw, bh], "trackId": walker.track_id})
            points.append([walker.x * w, walker.y * h])
        heatmap = None
        if burning:
            spread = 0.05 + intensity * 0.2
            values = [round(min(1.0, intensity * 1.1 * math.exp(-((((c + 0.5) / HEAT_COLS - center[0]) ** 2
                                                                   + (((r + 0.5) / HEAT_ROWS - center[1]) * 1.3) ** 2) / (2 * spread * spread)))), 3)
                      for r in range(HEAT_ROWS) for c in range(HEAT_COLS)]
            heatmap = {"rows": HEAT_ROWS, "cols": HEAT_COLS, "values": values}
        return {"cameraId": cam["id"], "timestamp": datetime.now(timezone.utc).isoformat(), "frameWidth": w, "frameHeight": h,
                "normalized": False, "detections": detections, "dangerZones": zones, "heatmap": heatmap, "humanCoordinates": points,
                "fireDetected": burning, "fireSeverity": level if burning else None, "humanCount": len(cam["walkers"]),
                "simulated": True}  # test data: marked as such in alerts and logs


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", default="http://localhost:5291", help="server base URL")
    ap.add_argument("--device-key", default=os.environ.get("Device__ApiKey", ""), help="Device__ApiKey configured on the server")
    ap.add_argument("--fps", type=float, default=5, help="detection frames per second per camera")
    ap.add_argument("--telemetry-interval", type=float, default=2, help="seconds between sensor batches")
    ap.add_argument("--fire-probability", type=float, default=0.004, help="random ignition chance per zone per second")
    ap.add_argument("--ignite", action="append", default=[], metavar="ZONE", help="start a fire in ZONE immediately (repeatable)")
    args = ap.parse_args()
    if not args.device_key:
        raise SystemExit("A device key is required: --device-key <key> (must match Device__ApiKey on the server).")
    args.base_url = args.base_url.rstrip("/")
    try:
        Simulator(args).run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

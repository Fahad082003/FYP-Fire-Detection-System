"""HTTP service (/health, /infer) + one worker thread per configured camera that posts to the Sentinel API."""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import cv2
import numpy as np

from . import VERSION
from .detector import ModelError, YoloxDetector, load_manifest
from .pipeline import CameraPipeline

log = logging.getLogger("sentinel_ai")
MAX_IMAGE_BYTES = 8 * 1024 * 1024
CAMERA_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
CAMERA_SYNC_SECONDS = 60  # how often the camera list is re-read from the API


def mask(source: str) -> str:
    """Hide credentials in stream URLs before they reach logs or /health."""
    return re.sub(r"//[^/@]+@", "//***@", str(source))


class Engine:
    """Loads every model in the manifest; a missing model is reported, never replaced by fake output."""

    def __init__(self, models_dir: Path, settings: dict | None = None, detector_factory=YoloxDetector):
        self.settings = settings or {}
        self.detectors, self.models, self.missing = [], [], []
        for spec in load_manifest(models_dir):
            entry = {"name": spec["name"], "file": spec["file"], "license": spec.get("license"),
                     "categories": sorted(set(spec["categories"].values())), "threshold": spec["threshold"]}
            try:
                self.detectors.append(detector_factory(spec, models_dir))
                entry.update(loaded=True, error=None)
            except ModelError as e:
                self.missing.append(spec["name"])
                entry.update(loaded=False, error=str(e))
                log.warning("model %s not loaded: %s", spec["name"], e)
            self.models.append(entry)
        self.lock = threading.Lock()  # one inference at a time: CPU-bound, parallel runs only add latency
        self.count, self.total_ms, self.last_ms = 0, 0.0, None

    def pipeline(self, camera_id: str, simulated: bool, zones=None) -> CameraPipeline:
        return CameraPipeline(self.detectors, camera_id, simulated, self.settings, zones, self.missing)

    def run(self, pipe: CameraPipeline, bgr: np.ndarray, temporal: bool) -> tuple[dict, float]:
        with self.lock:
            t0 = time.perf_counter()
            payload = pipe.process(bgr, temporal)
            ms = (time.perf_counter() - t0) * 1000
            self.count, self.total_ms, self.last_ms = self.count + 1, self.total_ms + ms, round(ms, 1)
        return payload, ms

    def health(self, cameras) -> dict:
        return {
            "service": "sentinel-ai", "version": VERSION,
            "status": "ok" if self.detectors and not self.missing else ("degraded" if self.detectors else "no-model"),
            "models": self.models,
            "capabilities": sorted({c for m in self.models if m["loaded"] for c in m["categories"]}),
            "inference": {"count": self.count, "avgMs": round(self.total_ms / self.count, 1) if self.count else None,
                          "lastMs": self.last_ms},
            "cameras": [w.status() for w in cameras],
        }


class CameraWorker(threading.Thread):
    """Reads one source (RTSP/HTTP stream, webcam index or local video file) and posts confirmed frames to the API."""

    def __init__(self, engine: Engine, cam: dict, api_url: str, device_key: str):
        super().__init__(daemon=True, name=f"cam-{cam['cameraId']}")
        self.engine, self.cam, self.api_url, self.device_key = engine, cam, api_url.rstrip("/"), device_key
        src = os.path.expandvars(str(cam["source"]))  # credentials come from environment variables, not the config file
        self.kind = "webcam" if src.isdigit() else "file" if Path(src).is_file() else "stream"
        self.source = int(src) if self.kind == "webcam" else src
        # A recorded file is test input, never a live camera: always labelled simulated.
        self.simulated = True if self.kind == "file" else bool(cam.get("simulated", False))
        self.fps = float(cam.get("fps", 2))
        self.pipe = engine.pipeline(cam["cameraId"], self.simulated, cam.get("zones"))
        self.state, self.error, self.sent, self.failed = "starting", None, 0, 0
        self.stop_event = threading.Event()

    def status(self) -> dict:
        return {"cameraId": self.cam["cameraId"], "kind": self.kind, "source": mask(self.source), "simulated": self.simulated,
                "state": self.state, "error": self.error, "framesSent": self.sent, "postFailures": self.failed}

    def run(self):
        backoff = 1.0
        while not self.stop_event.is_set():
            cap = cv2.VideoCapture(self.source)
            if not cap.isOpened():
                self.state, self.error = "error", f"cannot open {self.kind} source"
                self.stop_event.wait(backoff)
                backoff = min(30.0, backoff * 2)
                continue
            self.state, self.error, backoff = "running", None, 1.0
            skip = max(1, round((cap.get(cv2.CAP_PROP_FPS) or self.fps) / self.fps)) if self.kind == "file" else 1
            due = 0.0
            while not self.stop_event.is_set():
                if self.kind == "file":
                    for _ in range(skip - 1):
                        cap.grab()
                ok, frame = cap.read()  # streams: keep reading so the buffer never lags behind real time
                if not ok:
                    break
                if time.monotonic() < due:
                    continue
                due = time.monotonic() + 1 / self.fps
                try:
                    payload, _ = self.engine.run(self.pipe, frame, temporal=True)
                except Exception as e:  # one bad frame must not silently stop this camera; /health shows the error
                    self.state, self.error = "error", f"inference failed: {e}"
                    log.exception("%s: inference failed", self.cam["cameraId"])
                else:
                    if self.state == "error":
                        self.state, self.error = "running", None
                    self.post(payload)
                if self.kind == "file":
                    self.stop_event.wait(max(0.0, due - time.monotonic()))
            cap.release()
            if self.stop_event.is_set():
                return  # stopped (sync or a 404): keep that state in /health instead of "reconnecting"
            if self.kind == "file" and not self.cam.get("loop", False):
                self.state = "finished"
                return
            self.state, self.error = "reconnecting", "source ended or dropped"
            self.stop_event.wait(backoff)

    def post(self, payload: dict):
        req = urllib.request.Request(f"{self.api_url}/api/detections", data=json.dumps(payload).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "X-Device-Key": self.device_key})
        try:
            with urllib.request.urlopen(req, timeout=5):
                self.sent += 1
        except urllib.error.HTTPError as e:
            self.failed += 1
            self.error = f"API HTTP {e.code}: {e.read(300).decode(errors='replace')}"
            log.warning("%s: %s", self.cam["cameraId"], self.error)
            if e.code == 404 and self.cam.get("managed"):  # deleted in the dashboard: stop now, not at the next sync
                self.state = "stopped"
                self.stop_event.set()
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            self.failed += 1
            self.error = f"API unreachable: {e}"
            log.warning("%s: %s", self.cam["cameraId"], self.error)


def api_cameras(api: str, key: str) -> dict[str, str]:
    """Cameras added in the dashboard (Devices page): id -> stream link. Cameras without a link are simulated feeds."""
    req = urllib.request.Request(f"{api.rstrip('/')}/api/cameras", headers={"X-Device-Key": key})
    with urllib.request.urlopen(req, timeout=10) as r:
        cams = json.loads(r.read())
    return {c["id"]: c["streamUrl"] for c in cams if c.get("streamUrl") and CAMERA_ID.match(str(c.get("id", "")))}


def sync_cameras(workers: list, managed: dict, wanted: dict, make_worker) -> None:
    """Start a worker per new camera; stop workers whose camera was removed or whose link changed.
    `managed` holds only the workers started here (cameras from config.json are never touched)."""
    for cam_id, w in list(managed.items()):
        if wanted.get(cam_id) != w.cam["source"] or w.stop_event.is_set():  # stopped by a 404: restart if it is wanted again
            w.stop_event.set()
            workers.remove(w)
            del managed[cam_id]
    for cam_id, url in wanted.items():
        if cam_id not in managed:
            w = make_worker({"cameraId": cam_id, "source": url, "fps": 2, "managed": True})  # 2 fps: a walking person keeps one track id
            managed[cam_id] = w
            workers.append(w)
            w.start()


def make_handler(engine: Engine, cameras: list):
    class Handler(BaseHTTPRequestHandler):
        def _json(self, code: int, body: dict):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if urlparse(self.path).path == "/health":
                return self._json(200, engine.health(cameras))
            self._json(404, {"error": "not found"})

        def do_POST(self):
            url = urlparse(self.path)
            if url.path != "/infer":
                return self._json(404, {"error": "not found"})
            camera_id = parse_qs(url.query).get("cameraId", ["upload-test"])[0]
            if not CAMERA_ID.match(camera_id):
                return self._json(400, {"error": "invalid cameraId"})
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_IMAGE_BYTES:
                return self._json(400, {"error": f"body must be an image of 1..{MAX_IMAGE_BYTES} bytes"})
            img = cv2.imdecode(np.frombuffer(self.rfile.read(length), np.uint8), cv2.IMREAD_COLOR)
            if img is None:
                return self._json(400, {"error": "cannot decode image (PNG or JPG expected)"})
            if not engine.detectors:
                return self._json(503, {"error": "no AI model loaded: " + "; ".join(m["error"] for m in engine.models)})
            try:
                payload, ms = engine.run(engine.pipeline(camera_id, simulated=True), img, temporal=False)
            except ModelError as e:
                log.error("inference failed: %s", e)
                return self._json(500, {"error": str(e)})
            self._json(200, {"payload": payload, "inferenceMs": round(ms, 1)})

        def log_message(self, fmt, *args):
            log.debug("http: " + fmt, *args)

    return Handler


def serve(config: dict, models_dir: Path):
    engine = Engine(models_dir, config.get("settings"))
    api = os.environ.get("SENTINEL_API_URL", config.get("api", "http://localhost:5291"))
    key = os.environ.get("SENTINEL_DEVICE_KEY", "")
    cameras = []
    for cam in config.get("cameras", []):
        if not cam.get("enabled", True):
            continue
        if not CAMERA_ID.match(str(cam.get("cameraId", ""))):
            raise SystemExit(f"invalid cameraId in config: {cam.get('cameraId')!r}")
        if not key:
            raise SystemExit("SENTINEL_DEVICE_KEY is not set: camera workers cannot post to the API")
        cameras.append(CameraWorker(engine, cam, api, key))
    host, port = config.get("listen", "127.0.0.1:8008").rsplit(":", 1)
    server = ThreadingHTTPServer((host, int(port)), make_handler(engine, cameras))
    for w in cameras:
        w.start()
    if key:  # cameras added in the dashboard need no entry in config.json
        fixed = {w.cam["cameraId"] for w in cameras}
        managed: dict = {}

        def keep_in_sync():
            while True:
                try:
                    wanted = {i: u for i, u in api_cameras(api, key).items() if i not in fixed}
                    sync_cameras(cameras, managed, wanted, lambda cam: CameraWorker(engine, cam, api, key))
                except (urllib.error.URLError, OSError, ValueError) as e:
                    log.warning("camera list from the API is unavailable: %s", e)
                time.sleep(CAMERA_SYNC_SECONDS)

        threading.Thread(target=keep_in_sync, daemon=True, name="camera-sync").start()
    else:
        log.info("SENTINEL_DEVICE_KEY not set: only cameras from config.json run (dashboard cameras need the key)")
    log.info("sentinel-ai %s on http://%s:%s, models loaded: %s, missing: %s, cameras: %d",
             VERSION, host, port, [d.name for d in engine.detectors], engine.missing, len(cameras))
    try:
        server.serve_forever()
    finally:
        for w in cameras:
            w.stop_event.set()

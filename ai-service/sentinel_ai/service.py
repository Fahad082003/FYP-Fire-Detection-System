"""HTTP service (/health, /infer, /config) + one worker thread per configured camera that posts to the Sentinel API."""
from __future__ import annotations

import hmac
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
from .pipeline import DEFAULTS, CameraPipeline

log = logging.getLogger("sentinel_ai")
MAX_IMAGE_BYTES = 8 * 1024 * 1024
CAMERA_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
CAMERA_SYNC_SECONDS = 60  # how often the camera list is re-read from the API
# Longest wait for a network stream to open or deliver a frame (OpenCV's FFmpeg default is 30 s). The server already calls a
# camera lost after 10 s without frames, so waiting longer only delays the reconnect.
STREAM_TIMEOUT_MS = 10_000
# Sensitivity controller: the confidence thresholds an admin may choose. Only the range the models were evaluated over
# (reports/: 0.25 to 0.55); a value outside it has no measured precision/recall behind it.
THRESHOLD_MIN, THRESHOLD_MAX = 0.25, 0.55
OVERRIDES_FILE = "thresholds.local.json"  # in the models folder: the admin's choice survives a restart; models.json stays as shipped


def mask(source: str) -> str:
    """Hide credentials in stream URLs before they reach logs or /health. Up to the last "@" before the path, as FFmpeg reads
    it: a password with a raw "@" (admin:p@ss) is hidden whole."""
    return re.sub(r"//[^/]*@", "//***@", str(source))


class Engine:
    """Loads every model in the manifest; a missing model is reported, never replaced by fake output."""

    def __init__(self, models_dir: Path, settings: dict | None = None, detector_factory=YoloxDetector, load: bool = True):
        self.models_dir, self.detector_factory = models_dir, detector_factory
        self.settings = settings or {}
        self.detectors, self.models, self.missing = [], [], []
        self.loading = True  # running is not ready: /health says "loading" until every model was tried
        self.lock = threading.Lock()  # one inference at a time: CPU-bound, parallel runs only add latency
        self.count, self.total_ms, self.last_ms = 0, 0.0, None
        if load:
            self.load()

    def load(self) -> None:
        overrides = self.overrides()
        for spec in load_manifest(self.models_dir):
            entry = {"name": spec["name"], "file": spec["file"], "license": spec.get("license"),
                     "categories": sorted(set(spec["categories"].values())), "threshold": spec.get("threshold"),
                     "defaultThreshold": spec.get("threshold")}
            try:
                detector = self.detector_factory(spec, self.models_dir)
                value = overrides.get(spec["name"])
                if valid_threshold(value):
                    detector.threshold = entry["threshold"] = float(value)
                elif value is not None:
                    log.warning("ignoring saved threshold %r for %s (allowed %s to %s)", value, spec["name"], THRESHOLD_MIN, THRESHOLD_MAX)
                self.detectors.append(detector)
                entry.update(loaded=True, error=None)
            except ModelError as e:
                self.missing.append(spec["name"])
                entry.update(loaded=False, error=str(e))
                log.warning("model %s not loaded: %s", spec["name"], e)
            self.models.append(entry)
        self.loading = False

    def overrides(self) -> dict:
        try:
            data = json.loads((self.models_dir / OVERRIDES_FILE).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            log.warning("ignoring %s: %s", OVERRIDES_FILE, e)
            return {}
        return data if isinstance(data, dict) else {}

    def set_threshold(self, name: str, value) -> float:
        """Applies a new confidence threshold to one loaded model between two inferences and saves it; ValueError if refused."""
        if not valid_threshold(value):
            raise ValueError(f"threshold must be a number from {THRESHOLD_MIN} to {THRESHOLD_MAX}")
        detector = next((d for d in self.detectors if d.name == name), None)
        if detector is None:
            raise ValueError(f"model {name!r} is not loaded")
        with self.lock:  # never in the middle of an inference
            saved = {**self.overrides(), name: float(value)}
            tmp = self.models_dir / (OVERRIDES_FILE + ".tmp")
            tmp.write_text(json.dumps(saved, indent=2), encoding="utf-8")
            os.replace(tmp, self.models_dir / OVERRIDES_FILE)  # saved first: a failed write leaves the old value active
            detector.threshold = float(value)
            next(m for m in self.models if m["name"] == name)["threshold"] = detector.threshold
        log.info("threshold of %s set to %s", name, detector.threshold)
        return detector.threshold

    def config(self) -> dict:
        active = {d.name: d.threshold for d in self.detectors}  # what inference really uses
        return {"min": THRESHOLD_MIN, "max": THRESHOLD_MAX, "models": [
            {"name": m["name"], "loaded": m["loaded"], "threshold": active.get(m["name"]), "defaultThreshold": m["defaultThreshold"]}
            for m in self.models]}

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
            "status": "loading" if self.loading else "ok" if self.detectors and not self.missing
                      else "degraded" if self.detectors else "no-model",
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

    def open(self) -> cv2.VideoCapture:
        """Network streams get bounded open/read waits, so a hung or stalled camera fails and reconnects instead of blocking
        this worker. FFmpeg is OpenCV's first stream backend anyway; pinning it only drops the fallbacks without a timeout.
        RTSP: OpenCV already asks for TCP first and falls back to UDP (OPENCV_FFMPEG_CAPTURE_OPTIONS, set before start, overrides)."""
        if self.kind != "stream":
            return cv2.VideoCapture(self.source)
        return cv2.VideoCapture(self.source, cv2.CAP_FFMPEG, [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, STREAM_TIMEOUT_MS,
                                                               cv2.CAP_PROP_READ_TIMEOUT_MSEC, STREAM_TIMEOUT_MS])

    def run(self):
        backoff = 1.0
        while not self.stop_event.is_set():
            cap, opened, dropped = self.open(), False, "source ended or dropped"
            try:
                opened = cap.isOpened()
                if opened:
                    self.state, self.error = "running", None
                    if self.read_frames(cap):
                        backoff = 1.0  # frames came: retry a drop at once; a source that opens but never delivers backs off
            except Exception as e:  # a broken stream must not end this thread silently: release and reconnect
                dropped = f"capture failed: {mask(e)}"
                log.warning("%s: %s", self.cam["cameraId"], dropped)
            finally:
                cap.release()
            if self.stop_event.is_set():
                return  # stopped (sync or a 404): keep that state in /health instead of "reconnecting"
            if not opened:
                self.state, self.error = "error", f"cannot open {self.kind} source"
            elif self.kind == "file" and not self.cam.get("loop", False):
                self.state = "finished"
                return
            else:
                self.state, self.error = "reconnecting", dropped
            self.stop_event.wait(backoff)
            backoff = min(30.0, backoff * 2)

    def read_frames(self, cap: cv2.VideoCapture) -> bool:
        """Analyses at most `fps` frames a second until the source ends; True if any frame arrived."""
        skip = max(1, round((cap.get(cv2.CAP_PROP_FPS) or self.fps) / self.fps)) if self.kind == "file" else 1
        due, got = 0.0, False
        while not self.stop_event.is_set():
            if self.kind == "file":
                for _ in range(skip - 1):
                    cap.grab()
            if not cap.grab():  # streams: keep grabbing so the buffer never lags behind real time
                return got
            got = True
            if time.monotonic() < due:
                continue  # skipped frames are never converted to BGR: retrieve() only for the ones analysed
            due = time.monotonic() + 1 / self.fps
            ok, frame = cap.retrieve()
            if not ok:
                return got
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
        return got

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


class Server(ThreadingHTTPServer):
    # The default sets SO_REUSEADDR, which on Windows lets a second AI service bind the same port and share its requests.
    allow_reuse_address = False


def valid_threshold(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and THRESHOLD_MIN <= value <= THRESHOLD_MAX


def make_handler(engine: Engine, cameras: list, device_key: str = ""):
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
            if urlparse(self.path).path == "/config":
                return self._json(200, engine.config())
            self._json(404, {"error": "not found"})

        def post_config(self):
            """{"model": name, "threshold": x}. Only the Sentinel server (the device key) may change it."""
            if not device_key or not hmac.compare_digest(self.headers.get("X-Device-Key", ""), device_key):
                return self._json(403, {"error": "config changes need the Sentinel device key"})
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(min(length, 4096))) if 0 < length <= 4096 else None
                active = engine.set_threshold(body["model"], body["threshold"])
            except (ValueError, TypeError, KeyError) as e:
                return self._json(400, {"error": str(e) if isinstance(e, ValueError) and body else "body must be {model, threshold}"})
            except OSError as e:
                return self._json(500, {"error": f"could not save the setting: {e}"})
            self._json(200, {"model": body["model"], "threshold": active, **engine.config()})

        def do_POST(self):
            url = urlparse(self.path)
            if url.path == "/config":
                return self.post_config()
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
            if engine.loading:
                return self._json(503, {"error": "AI models are still loading, try again in a few seconds"})
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
    confirm = (config.get("settings") or {}).get("confirm_frames", DEFAULTS["confirm_frames"])
    if isinstance(confirm, bool) or not isinstance(confirm, int) or confirm < 1:  # 0 would confirm every single-frame detection
        raise SystemExit(f"settings.confirm_frames must be a whole number of 1 or more, got {confirm!r}")
    engine = Engine(models_dir, config.get("settings"), load=False)
    api = os.environ.get("SENTINEL_API_URL", config.get("api", "http://localhost:5291"))
    key = os.environ.get("SENTINEL_DEVICE_KEY", "")
    configured = []
    for cam in config.get("cameras", []):
        if not cam.get("enabled", True):
            continue
        if not CAMERA_ID.match(str(cam.get("cameraId", ""))):
            raise SystemExit(f"invalid cameraId in config: {cam.get('cameraId')!r}")
        if not key:
            raise SystemExit("SENTINEL_DEVICE_KEY is not set: camera workers cannot post to the API")
        configured.append(cam)
    cameras = []
    host, port = config.get("listen", "127.0.0.1:8008").rsplit(":", 1)
    try:
        server = Server((host, int(port)), make_handler(engine, cameras, key))
    except OSError as e:  # never take over a port another service holds
        raise SystemExit(f"cannot listen on {host}:{port}, the port is in use (is another AI service running?): {e}")
    failed = []

    def start_up():
        """Models load while /health already answers "loading"; cameras start only once the models are ready."""
        try:
            engine.load()
        except Exception as e:  # noqa: BLE001 - e.g. no models.json: exit as before instead of staying "loading" forever
            failed.append(e)
            server.shutdown()
            return
        cameras.extend(CameraWorker(engine, cam, api, key) for cam in configured)
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
        log.info("sentinel-ai %s ready, models loaded: %s, missing: %s, cameras: %d",
                 VERSION, [d.name for d in engine.detectors], engine.missing, len(cameras))

    log.info("sentinel-ai %s on http://%s:%s, loading models", VERSION, host, port)
    threading.Thread(target=start_up, daemon=True, name="start-up").start()
    try:
        server.serve_forever()
    finally:
        for w in cameras:
            w.stop_event.set()
    if failed:
        raise failed[0]

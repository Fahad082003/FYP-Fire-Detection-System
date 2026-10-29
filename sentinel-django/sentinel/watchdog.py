"""Camera-lost watchdog (port of CameraWatchdog.cs, including audit fixes D-16 and D-17).

A camera that was sending frames and goes silent (power cut in a fire, cable, crashed AI service) raises one "camera lost"
incident per silent period. Watched: every camera seen since the server started, plus, from start-up, every camera with a
stream link (the cameras the AI service reads), which gets CAMERA_STARTUP_GRACE_SECONDS to send its first frame.
Simulated feeds without a link never alarm. CameraFeed.status follows the same rule: Offline when the silence crosses
CAMERA_LOST_SECONDS, Online on fresh frames.
"""
import logging
import threading
import time
from datetime import datetime, timedelta

from django.conf import settings
from django.db import close_old_connections
from django.utils import timezone

from . import alerts, events
from .models import CameraFeed, CameraStatus, SensorNode

log = logging.getLogger("sentinel.watchdog")
CHECK_SECONDS = 2
AI_CAMERA_SYNC_SECONDS = 60  # ai-service CAMERA_SYNC_SECONDS: a new stream link is picked up at most this late


# shortcut: last-frame times live in this process; with several server processes each one watches only its own frames.
class Watchdog:
    def __init__(self):
        self._lock = threading.Lock()
        self._last_frame: dict[str, datetime] = {}
        self._lost: set[str] = set()
        self._online: set[str] = set()  # status already written as Online by this process
        self._seeded: set[str] = set()  # watched since start-up but no frame yet: its status must not turn Online
        self._started = timezone.now()

    def frame_received(self, camera_id: str, now: datetime | None = None) -> None:
        with self._lock:
            self._last_frame[camera_id] = now or timezone.now()
            self._seeded.discard(camera_id)

    def watch_streaming_cameras(self, now: datetime) -> None:
        """Start-up reconciliation: a camera that died while the server was down is otherwise never watched (D-17)."""
        ids = CameraFeed.objects.exclude(stream_url="").values_list("id", flat=True)
        self._watch(ids, now, settings.SENTINEL["CAMERA_STARTUP_GRACE_SECONDS"])

    def camera_linked(self, camera_id: str, now: datetime | None = None) -> None:
        """A camera given a stream link while the server runs (B-2): the AI service reads it at its next camera sync, then
        gets the start-up grace for a first frame. A wrong link then raises "camera lost" instead of staying Online."""
        self._watch([camera_id], now or timezone.now(), AI_CAMERA_SYNC_SECONDS + settings.SENTINEL["CAMERA_STARTUP_GRACE_SECONDS"])

    def forget(self, camera_id: str) -> None:
        """A deleted camera: re-added under the same id it is watched afresh, not left in a finished silent period."""
        with self._lock:
            self._last_frame.pop(camera_id, None)
            for state in (self._lost, self._online, self._seeded):
                state.discard(camera_id)

    def _watch(self, ids, now: datetime, grace_seconds: float) -> None:
        seen_at = now + timedelta(seconds=grace_seconds - settings.SENTINEL["CAMERA_LOST_SECONDS"])
        with self._lock:
            for camera_id in ids:
                if camera_id not in self._last_frame:  # a real frame already received wins
                    self._last_frame[camera_id] = seen_at
                    self._seeded.add(camera_id)

    def check(self, now: datetime) -> None:
        """Raises one alert per silent period and announces when the camera sends again."""
        limit = settings.SENTINEL["CAMERA_LOST_SECONDS"]
        with self._lock:
            snapshot = list(self._last_frame.items())
        for camera_id, last in snapshot:
            silent = (now - last).total_seconds()
            if silent > limit:
                with self._lock:
                    if camera_id in self._lost:
                        continue  # already reported
                    self._lost.add(camera_id)
                    self._online.discard(camera_id)  # camera_lost() stores Offline
                    never_sent = camera_id in self._seeded  # its "last frame" is only the grace deadline
                alerts.camera_lost(camera_id, None if never_sent else int(silent))
                continue
            with self._lock:
                recovered = camera_id in self._lost
                self._lost.discard(camera_id)
                needs_write = camera_id not in self._online and camera_id not in self._seeded
            if recovered:
                events.activity("success", f"Camera {camera_id} is sending frames again.")
            # Once per process and after every loss, so a status left Offline before a restart is corrected too.
            if needs_write:
                CameraFeed.objects.filter(pk=camera_id).exclude(status=CameraStatus.ONLINE).update(status=CameraStatus.ONLINE)
                with self._lock:
                    self._online.add(camera_id)  # only after the write, so a failed write is retried next check
        self.check_sensors(now)

    def check_sensors(self, now: datetime) -> None:
        """One sensor-lost incident per outage; the episode lives in the database, so a restart does not repeat it."""
        stale_after = timedelta(seconds=settings.SENTINEL["SENSOR_STALE_SECONDS"])
        if now - self._started < stale_after:
            return  # just started: gateways get one full window to report again
        for sensor_id in SensorNode.objects.filter(last_reading_at__lte=now - stale_after).values_list("id", flat=True):
            alerts.sensor_lost(sensor_id, now)

    def run_forever(self) -> None:
        try:
            self.watch_streaming_cameras(timezone.now())
        except Exception:  # noqa: BLE001 - logged; the periodic check still runs
            log.exception("Camera watchdog start-up reconciliation failed")
        while True:
            time.sleep(CHECK_SECONDS)
            try:
                self.check(timezone.now())
            except Exception:  # noqa: BLE001 - logged; the next check retries
                log.exception("Camera watchdog check failed")
            finally:
                close_old_connections()


watchdog = Watchdog()


def start() -> None:
    threading.Thread(target=watchdog.run_forever, daemon=True, name="camera-watchdog").start()

"""In-process event broadcast to every open dashboard (Server-Sent Events). Replaces the SignalR hub's server -> client events.

Event names and payloads are the SignalR ones: detection, telemetry, sprinkler, incident, notification, activity, incidentLog.
Publishing is thread-safe and never raises: a broadcast failure must not break the alert pipeline.
"""
import asyncio
import json
import logging
import threading

from django.utils import timezone
from rest_framework.utils.encoders import JSONEncoder

log = logging.getLogger("sentinel.events")
QUEUE_LIMIT = 500  # a dashboard that stops reading is dropped instead of growing memory

# shortcut: subscribers live in this process; with several server processes each one sees only its own events.
_subscribers: set[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = set()
_lock = threading.Lock()


def subscribe() -> asyncio.Queue:
    queue = asyncio.Queue(QUEUE_LIMIT)
    with _lock:
        _subscribers.add((asyncio.get_running_loop(), queue))
    return queue


def unsubscribe(queue: asyncio.Queue) -> None:
    with _lock:
        _subscribers.difference_update({s for s in _subscribers if s[1] is queue})


def is_subscribed(queue: asyncio.Queue) -> bool:
    with _lock:
        return any(q is queue for _, q in _subscribers)


def _offer(queue: asyncio.Queue, message: str) -> None:
    try:
        queue.put_nowait(message)
    except asyncio.QueueFull:  # the stream closes; the browser reconnects and reloads the full snapshot
        log.warning("dashboard event queue full; dropping that stream")
        unsubscribe(queue)


def publish(event: str, payload) -> None:
    try:
        message = f"event: {event}\ndata: {json.dumps(payload, cls=JSONEncoder, separators=(',', ':'))}\n\n"
        with _lock:
            targets = list(_subscribers)
        for loop, queue in targets:
            try:
                loop.call_soon_threadsafe(_offer, queue, message)
            except RuntimeError:  # that dashboard's event loop is closed: forget it, keep serving the others
                unsubscribe(queue)
    except Exception:  # noqa: BLE001 - see module docstring
        log.warning("broadcast of %s failed", event, exc_info=True)


def activity(level: str, message: str, zone: str | None = None) -> None:
    """Human-readable line for the dashboard's live event log (info, success, warning, critical, notify)."""
    publish("activity", {"level": level, "message": message, "zone": zone, "timestamp": timezone.now()})


def subscriber_count() -> int:
    with _lock:
        return len(_subscribers)

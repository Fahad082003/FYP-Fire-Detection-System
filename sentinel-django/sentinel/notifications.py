"""Emergency notifications (port of NotificationDispatcher.cs, NotificationWorker and Channels.cs).

enqueue() is instant; a background thread does the slow SMS / e-mail / push calls. A cooldown per (source, zone, severity)
stops the same emergency from spamming responders; an operator's manual dispatch (force) bypasses it.
"""
import base64
import html
import json
import logging
import queue
import threading
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings
from django.core.cache import cache
from django.core.mail import EmailMultiAlternatives
from django.db import close_old_connections
from django.utils import timezone

from . import events
from .models import IncidentAlert, IncidentSource, WebPushSubscription, rank

log = logging.getLogger("sentinel.notifications")
jobs: "queue.Queue[tuple[int, bool]]" = queue.Queue()


def _opt(key):
    return settings.SENTINEL[key]


def _split(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def enqueue(incident: IncidentAlert, force: bool = False) -> bool:
    if not force:
        key = f"notify:{incident.source}:{urllib.parse.quote(incident.zone)}:{incident.severity}"
        if not cache.add(key, incident.id, _opt("COOLDOWN_SECONDS")):  # add() is atomic: only the first one wins
            log.info("Notification for incident #%s suppressed by cooldown", incident.id)
            return False
    jobs.put((incident.id, force))
    log.info("Emergency notifications queued for incident #%s", incident.id)
    return True


# ---------------------------------------------------------------- message
class AlertMessage:
    """Everything a channel needs to render an alert."""

    def __init__(self, i: IncidentAlert):
        self.incident_id, self.severity, self.zone = i.id, i.severity, i.zone
        self.location = (f"{i.zone} ({i.camera.name})" if i.camera_id and i.camera
                         else f"{i.zone} (sensor {i.sensor_id})" if i.sensor_id else i.zone)
        self.source, self.fire_detected, self.fire_severity = i.source, i.fire_detected, i.fire_severity
        self.human_count, self.danger_count, self.message, self.timestamp = i.human_count, i.danger_count, i.message, i.timestamp
        self.site, self.url = _opt("SITE_NAME"), _opt("DASHBOARD_URL")

    @property
    def headline(self) -> str:
        if self.source == IncidentSource.MANUAL:
            return "EMERGENCY DISPATCH"
        if self.fire_detected and self.danger_count > 0:
            return "FIRE — HUMANS IN DANGER"
        if self.fire_detected:
            return "FIRE DETECTED"
        if self.danger_count > 0:
            return "HUMANS IN DANGER ZONE"
        if self.source == IncidentSource.SENSOR:
            return "SENSOR SAFETY LIMIT EXCEEDED"
        return "INCIDENT"

    @property
    def subject(self) -> str:
        return f"[{self.site}] {self.severity.upper()}: {self.headline} — {self.zone} (#{self.incident_id})"

    @property
    def text(self) -> str:
        fire = f"Fire severity: {self.fire_severity or 'unknown'}" if self.fire_detected else "No fire reported"
        return (f"[{self.site}] {self.severity.upper()} - {self.headline}\nLocation: {self.location}\n"
                f"Humans in area: {self.human_count} | Humans in danger: {self.danger_count}\n{fire}\n{self.message}\n"
                f"Incident #{self.incident_id} at {self.timestamp:%Y-%m-%d %H:%M:%S} UTC\n{self.url}")

    def html(self) -> str:
        color = "#b91c1c" if rank(self.severity) >= rank("High") else "#b45309" if self.severity == "Medium" else "#047857"
        e = html.escape
        return (f'<div style="font-family:Segoe UI,Arial,sans-serif;max-width:560px;border:1px solid #1f2937;border-radius:10px;overflow:hidden">'
                f'<div style="background:{color};color:#fff;padding:16px 20px"><div style="font-size:12px;letter-spacing:.12em;'
                f'text-transform:uppercase">{e(self.site)} · Incident #{self.incident_id}</div><div style="font-size:20px;'
                f'font-weight:700">{self.severity.upper()} — {e(self.headline)}</div></div><div style="padding:16px 20px;color:#111827">'
                f'<p>{e(self.message)}</p><p><b>Location:</b> {e(self.location)}<br><b>Humans in area:</b> {self.human_count}<br>'
                f'<b>Humans in danger:</b> {self.danger_count}<br><b>Detected:</b> {self.timestamp:%Y-%m-%d %H:%M:%S} UTC</p>'
                f'<a href="{e(self.url)}" style="background:#ef4444;color:#fff;padding:10px 16px;border-radius:8px;'
                f'text-decoration:none">Open Command Center</a></div></div>')


# ---------------------------------------------------------------- channels: each returns (ok, delivered, failed, detail) or None = not configured
def send_sms(m: AlertMessage):
    sid, token, sender, numbers = _opt("SMS_ACCOUNT_SID"), _opt("SMS_AUTH_TOKEN"), _opt("SMS_FROM"), _split(_opt("SMS_RECIPIENTS"))
    if not (sid and token and sender and numbers):
        return None
    url = f"https://api.twilio.com/2010-04-01/Accounts/{urllib.parse.quote(sid)}/Messages.json"
    auth = base64.b64encode(f"{sid}:{token}".encode()).decode()
    delivered, errors = 0, []
    for number in numbers:
        body = urllib.parse.urlencode({"To": number, "From": sender, "Body": m.text[:1500]}).encode()
        request = urllib.request.Request(url, data=body, headers={"Authorization": f"Basic {auth}"})
        try:
            with urllib.request.urlopen(request, timeout=20):
                delivered += 1
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            log.warning("Twilio SMS to %s failed: %s", number, e)
            errors.append(f"{number}: {e}")
    return delivered > 0, delivered, len(errors), "; ".join(errors)


def send_email(m: AlertMessage):
    recipients = _split(_opt("EMAIL_RECIPIENTS"))
    if not recipients:
        return None
    if not settings.EMAIL_HOST:
        log.warning("[EMAIL console mode] To: %s\nSubject: %s\n%s", ", ".join(recipients), m.subject, m.text)
        return True, len(recipients), 0, "console mode (no SMTP host configured)"
    mail = EmailMultiAlternatives(m.subject, m.text, _opt("EMAIL_FROM"), recipients,
                                  headers={"X-Priority": "1"} if rank(m.severity) >= rank("High") else None)
    mail.attach_alternative(m.html(), "text/html")
    mail.send()
    return True, len(recipients), 0, ""


def send_push(m: AlertMessage):
    public, private = _opt("WEBPUSH_PUBLIC_KEY"), _opt("WEBPUSH_PRIVATE_KEY")
    if not (public and private):
        return None
    try:
        from pywebpush import WebPushException, webpush  # optional: only needed when VAPID keys are configured
    except ImportError:
        return False, 0, 0, "pywebpush is not installed (pip install pywebpush)"
    payload = json.dumps({"title": f"{m.severity.upper()} · {m.headline}",
                          "body": f"{m.location} — humans in danger: {m.danger_count}", "tag": f"incident-{m.incident_id}",
                          "url": m.url, "requireInteraction": rank(m.severity) >= rank("High")})
    delivered = failed = 0
    stale = []
    for sub in WebPushSubscription.objects.filter(is_active=True):
        try:
            webpush({"endpoint": sub.endpoint, "keys": {"p256dh": sub.p256dh, "auth": sub.auth}}, payload,
                     vapid_private_key=private, vapid_claims={"sub": _opt("WEBPUSH_SUBJECT")}, timeout=20)
            delivered += 1
        except WebPushException as e:
            failed += 1
            status = getattr(e.response, "status_code", None)
            if status in (404, 410):
                stale.append(sub.id)
            else:
                log.warning("Web push failed (%s): %s", status, e)
    if stale:
        WebPushSubscription.objects.filter(id__in=stale).update(is_active=False)
    detail = (f"{len(stale)} expired subscription(s) removed" if stale
              else "no subscribers" if delivered + failed == 0 else "")
    return delivered > 0 or failed == 0, delivered, failed, detail


CHANNELS = [("sms", send_sms), ("email", send_email), ("push", send_push)]


def deliver(incident_id: int) -> None:
    incident = IncidentAlert.objects.select_related("camera").filter(pk=incident_id).first()
    if incident is None:
        return
    message = AlertMessage(incident)
    for name, send in CHANNELS:
        try:
            result = send(message)
        except Exception as e:  # noqa: BLE001 - one broken channel must not stop the others; reported below
            log.exception("%s notification for incident #%s failed", name, incident_id)
            result = (False, 0, 0, str(e)[:300])
        ok, delivered, failed, detail = result if result is not None else (False, 0, 0, "not configured")
        log.info("%s for incident #%s: ok=%s delivered=%s failed=%s %s", name, incident_id, ok, delivered, failed, detail)
        events.publish("notification", {"incidentId": incident_id, "channel": name, "ok": ok, "skipped": result is None,
                                        "delivered": delivered, "failed": failed, "detail": detail, "timestamp": timezone.now()})


def _worker() -> None:
    while True:
        incident_id, _ = jobs.get()
        try:
            deliver(incident_id)
        except Exception:  # noqa: BLE001 - keep the worker alive; the failure is logged
            log.exception("Notification delivery for incident #%s crashed", incident_id)
        finally:
            close_old_connections()


def start_worker() -> None:
    threading.Thread(target=_worker, daemon=True, name="notification-worker").start()

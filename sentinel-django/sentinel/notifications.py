"""Emergency notifications (port of NotificationDispatcher.cs, NotificationWorker and Channels.cs).

enqueue() is instant; a background thread does the slow SMS / e-mail / push calls. A cooldown per (source, zone, severity)
stops the same emergency from spamming responders; an operator's manual dispatch (force) bypasses it.
"""
import base64
import html
import importlib.util
import json
import logging
import queue
import re
import threading
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings
from django.core.cache import cache
from django.core import signing
from django.core.mail import EmailMultiAlternatives
from django.db import close_old_connections
from django.utils import timezone

from . import dto, events
from .models import FloorPlan, IncidentAlert, IncidentLog, IncidentLogType, IncidentSource, WebPushSubscription, rank

log = logging.getLogger("sentinel.notifications")
jobs: "queue.Queue[tuple[int, bool, tuple | None]]" = queue.Queue()  # (incident id, force, only these channels or None = all)


def _opt(key):
    return settings.SENTINEL[key]


def _split(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def enqueue(incident: IncidentAlert, force: bool = False, only: tuple | None = None) -> bool:
    """only=("fcm",): a new fire or smoke incident that is not an emergency reaches the phones; SMS etc. stay for emergencies."""
    if not force:
        key = f"notify:{','.join(only or ())}:{incident.source}:{urllib.parse.quote(incident.zone)}:{incident.severity}"
        if not cache.add(key, incident.id, _opt("COOLDOWN_SECONDS")):  # add() is atomic: only the first one wins
            log.info("Notification for incident #%s suppressed by cooldown", incident.id)
            return False
    jobs.put((incident.id, force, only))
    log.info("Notifications queued for incident #%s (%s)", incident.id, ", ".join(only) if only else "all channels")
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
        self.camera = i.camera if i.camera_id else None
        self.smoke = IncidentLog.objects.filter(incident_id=i.id, type=IncidentLogType.SMOKE_MEASUREMENT).exists()
        self.kind = ("Fire and smoke" if self.fire_detected and self.smoke else "Fire" if self.fire_detected
                     else "Smoke" if self.smoke else self.headline.title())
        self.building = self._building()
        self.incident_url = f"{self.url.rstrip('/')}/incidents?incident={i.id}"

    def _building(self) -> str:
        """Building and floor plan of the camera's calibration, when it has one ("" otherwise: never guessed)."""
        cal = dto.calibration(self.camera.calibration) if self.camera and self.camera.calibration else None
        plan = FloorPlan.objects.filter(pk=cal["floorPlanId"]).first() if cal else None
        return " · ".join(x for x in (plan.building, plan.name) if x) if plan else ""

    def snapshot(self) -> tuple[bytes | None, str]:
        """(JPEG, "") of this incident: the stored one, else grabbed once from its camera when it is an http(s) MJPEG feed and
        stored (App_Data/snapshots/<id>.jpg), so every channel and retry uses the same picture. Else (None, why not)."""
        path = snapshot_path(self.incident_id)
        if path.is_file():
            return path.read_bytes(), ""
        picture, why = self._grab()
        if picture:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(picture)
        return picture, why

    def snapshot_url(self) -> tuple[str | None, str]:
        """A signed link to the stored snapshot that works without a session for 24 h (for the phone or browser showing the
        push). It only works where the dashboard address is reachable from that device."""
        picture, why = self.snapshot()
        if not picture:
            return None, why
        signature = signing.TimestampSigner(salt=SNAPSHOT_SALT).sign(str(self.incident_id))
        return f"{self.url.rstrip('/')}/api/incidents/{self.incident_id}/snapshot?s={urllib.parse.quote(signature)}", ""

    def _grab(self) -> tuple[bytes | None, str]:
        camera = self.camera
        if camera is None:
            return None, "no camera for this incident"
        if camera.stream_format != "Mjpeg" or urllib.parse.urlsplit(camera.stream_url).scheme not in ("http", "https"):
            return None, "no snapshot for this camera's stream type"
        try:
            with urllib.request.urlopen(camera.stream_url, timeout=5) as r:
                data, started = b"", time.monotonic()
                while len(data) < 2_000_000 and time.monotonic() - started < 5:
                    chunk = r.read1(65536)
                    if not chunk:
                        break
                    data += chunk
                    start = data.find(b"\xff\xd8")
                    end = data.find(b"\xff\xd9", start + 2) if start >= 0 else -1
                    if end > 0:
                        return data[start:end + 2], ""
        except (OSError, ValueError) as e:
            return None, f"snapshot unavailable: {type(e).__name__}"
        return None, "snapshot unavailable: no complete picture from the stream"

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
        if getattr(self, "smoke", False):
            return "SMOKE DETECTED"
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


SNAPSHOT_SALT = "sentinel-incident-snapshot"


def snapshot_path(incident_id: int):
    return _opt("SNAPSHOT_DIR") / f"{int(incident_id)}.jpg"  # an int: no user text reaches the path


# ---------------------------------------------------------------- channels: each returns (ok, delivered, failed, detail[, status]) or None = not configured
PHONE = re.compile(r"^\+[1-9]\d{7,14}$")  # E.164, as Twilio and GSM modems expect


def _twilio(sid, token, sender, numbers, body, prefix="", label="Twilio SMS"):
    """Twilio Messages API for SMS and (prefix "whatsapp:") WhatsApp. A 2xx answer means Twilio queued the message, not that
    the phone received it. Invalid numbers are refused here, never sent."""
    url = f"https://api.twilio.com/2010-04-01/Accounts/{urllib.parse.quote(sid)}/Messages.json"
    auth = base64.b64encode(f"{sid}:{token}".encode()).decode()
    delivered, errors = 0, []
    for number in numbers:
        if not PHONE.match(number):
            errors.append(f"{number}: not an international number like +923001234567")
            continue
        data = urllib.parse.urlencode({"To": prefix + number, "From": prefix + sender, "Body": body[:1500]}).encode()
        request = urllib.request.Request(url, data=data, headers={"Authorization": f"Basic {auth}"})
        try:
            with urllib.request.urlopen(request, timeout=20):
                delivered += 1
        except urllib.error.HTTPError as e:
            reason = _json_error(e, "message")
            log.warning("%s to %s failed: %s %s", label, number, e, reason)
            errors.append(f"{number}: HTTP {e.code} {reason}".strip())
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            log.warning("%s to %s failed: %s", label, number, e)
            errors.append(f"{number}: {e}")
    return delivered > 0, delivered, len(errors), "; ".join(errors)


def _json_error(e: urllib.error.HTTPError, field: str) -> str:
    """The provider's own error text (Twilio "message", Telegram "description", FCM error.status); never the request URL."""
    try:
        body = json.loads(e.read(2000))
    except (ValueError, OSError, AttributeError):  # AttributeError: an error without a body
        return ""
    value = body.get(field) if isinstance(body, dict) else None
    return str(value.get("status") if isinstance(value, dict) else value or "")[:200]


def send_sms(m: AlertMessage):
    sid, token, sender, numbers = _opt("SMS_ACCOUNT_SID"), _opt("SMS_AUTH_TOKEN"), _opt("SMS_FROM"), _split(_opt("SMS_RECIPIENTS"))
    if not (sid and token and sender and numbers):
        return None
    return _twilio(sid, token, sender, numbers, m.text)


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
    for sub in WebPushSubscription.objects.filter(is_active=True).exclude(endpoint__startswith=FCM_PREFIX):
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


def send_whatsapp(m: AlertMessage):
    """WhatsApp through Twilio's WhatsApp sender. Needs its own approved sender; outside a 24 h customer window WhatsApp only
    allows approved templates, which this plain message is not (see the setup notes in the report)."""
    sid, token = _opt("WHATSAPP_ACCOUNT_SID"), _opt("WHATSAPP_AUTH_TOKEN")
    sender, numbers = _opt("WHATSAPP_FROM"), _split(_opt("WHATSAPP_RECIPIENTS"))
    if not (sid and token and sender and numbers):
        return None
    ok, delivered, failed, detail = _twilio(sid, token, sender, numbers, m.text, "whatsapp:", "Twilio WhatsApp")
    # shortcut: no photo, Twilio fetches media from a public URL and the dashboard is not public; add one if it ever is
    return ok, delivered, failed, "; ".join(x for x in (detail, "photo not attached (Twilio needs a public image URL)") if x)


def send_telegram(m: AlertMessage):
    """Telegram Bot API: the camera snapshot as a photo with the alert as caption when one is available, else a text message.
    Telegram answers ok only once the message is in the chat, so this one is reported as Delivered."""
    token, chats = _opt("TELEGRAM_BOT_TOKEN"), _split(_opt("TELEGRAM_CHAT_IDS"))
    if not (token and chats):
        return None
    photo, why = m.snapshot()
    delivered, errors = 0, []
    for chat in chats:
        if photo:
            data, ctype = _multipart({"chat_id": chat, "caption": m.text[:1000]}, "photo", "snapshot.jpg", photo)
            method = "sendPhoto"
        else:
            data, ctype = json.dumps({"chat_id": chat, "text": m.text[:4000]}).encode(), "application/json"
            method = "sendMessage"
        request = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}", data=data, headers={"Content-Type": ctype})
        try:
            with urllib.request.urlopen(request, timeout=20) as r:
                if json.loads(r.read()).get("ok"):
                    delivered += 1
                else:
                    errors.append(f"chat {chat}: Telegram did not confirm")
        except urllib.error.HTTPError as e:  # 401 = wrong bot token, 400 "chat not found" = wrong chat id
            errors.append(f"chat {chat}: HTTP {e.code} {_json_error(e, 'description')}".strip())
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            errors.append(f"chat {chat}: {type(e).__name__}")  # never str(e): it could carry the URL with the bot token
    log.info("Telegram for incident #%s: %s delivered, %s failed", m.incident_id, delivered, len(errors))
    return delivered > 0, delivered, len(errors), "; ".join([*errors, "with photo" if photo else f"text only: {why}"])


def _multipart(fields: dict, file_field: str, filename: str, content: bytes) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode() for k, v in fields.items()]
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
                 f'Content-Type: image/jpeg\r\n\r\n'.encode() + content + f"\r\n--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


FCM_PREFIX = "fcm:"  # FCM device tokens share the push-subscription table: Endpoint = "fcm:<token>"
FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
FCM_INSTALL = r"sentinel-django\.venv\Scripts\python -m pip install google-auth"
_fcm_creds: dict = {"path": None, "value": None}


def google_auth_installed() -> bool:
    return importlib.util.find_spec("google") is not None and importlib.util.find_spec("google.auth") is not None


def _google_request():
    """google-auth's own transport interface over urllib: its bundled transports need requests or urllib3 (not installed)."""
    from google.auth import exceptions, transport

    class Response(transport.Response):
        def __init__(self, status, headers, data):
            self._status, self._headers, self._data = status, headers, data
        status = property(lambda self: self._status)
        headers = property(lambda self: self._headers)
        data = property(lambda self: self._data)

    class Request(transport.Request):
        def __call__(self, url, method="GET", body=None, headers=None, timeout=None, **kwargs):
            request = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
            try:
                with urllib.request.urlopen(request, timeout=timeout or 20) as r:
                    return Response(r.status, dict(r.headers), r.read())
            except urllib.error.HTTPError as e:  # google-auth reads the error body (e.g. invalid_grant) itself
                return Response(e.code, dict(e.headers or {}), e.read())
            except (urllib.error.URLError, OSError) as e:
                raise exceptions.TransportError(f"Firebase unavailable: {type(e).__name__}") from None

    return Request()


def _fcm_credentials():
    """Service-account credentials from google-auth, cached per file: the library signs the RS256 grant and keeps the expiry."""
    from google.oauth2 import service_account
    path = _opt("FCM_CREDENTIALS_FILE")
    if _fcm_creds["path"] != path or _fcm_creds["value"] is None:
        _fcm_creds.update(path=path, value=service_account.Credentials.from_service_account_file(path, scopes=[FCM_SCOPE]))
    return _fcm_creds["value"]


def _fcm_access_token(force: bool = False) -> str:
    credentials = _fcm_credentials()
    if force or not credentials.valid:  # no token yet, expired, or FCM refused it (401): google-auth fetches a new one
        credentials.refresh(_google_request())
    return credentials.token


def _token_problem(e: Exception) -> str:
    """The FCM status word for a failed OAuth step, without any key material."""
    kind = type(e).__name__
    return ("Firebase unavailable" if kind == "TransportError" else
            "Invalid credentials")  # RefreshError (revoked or wrong key), or a file that is no service-account JSON


def fcm_status() -> dict:
    """Configured / Not configured / Missing dependency / Invalid credentials, checked without any network call, plus the
    last runtime problem (Firebase unavailable) from the delivery log."""
    project, path = _opt("FCM_PROJECT_ID"), _opt("FCM_CREDENTIALS_FILE")
    web = all(_opt(k) for k in ("FCM_WEB_API_KEY", "FCM_WEB_APP_ID", "FCM_SENDER_ID", "FCM_VAPID_KEY"))
    status, reason = "Configured", ""
    if not (project and path):
        status, reason = "Not configured", "Set Notifications__Fcm__ProjectId and Notifications__Fcm__CredentialsFile."
    elif not google_auth_installed():
        status, reason = "Missing dependency", f"google-auth is not installed. Run: {FCM_INSTALL}"
    else:
        try:
            _fcm_credentials()
        except Exception as e:  # noqa: BLE001 - any unreadable or wrong file is reported, never raised into the page
            status, reason = "Invalid credentials", f"The credentials file cannot be used as a service account ({type(e).__name__})."
    last = IncidentLog.objects.filter(type=IncidentLogType.NOTIFICATION, details__startswith="fcm: ").order_by("-id").first()
    if status == "Configured" and last and "Firebase unavailable" in last.details:
        status, reason = "Firebase unavailable", "The last delivery could not reach Firebase."
    return {"status": status, "reason": reason, "webPush": web, "lastDelivery": last.details if last else None,
            "lastDeliveryAt": last.timestamp if last else None}


def send_fcm(m: AlertMessage):
    """Firebase Cloud Messaging (HTTP v1) to every active device of an active user. FCM accepting a message ("Sent") is not a
    delivery receipt from the phone or browser."""
    project, path = _opt("FCM_PROJECT_ID"), _opt("FCM_CREDENTIALS_FILE")
    if not (project and path):
        return None
    if not google_auth_installed():
        return f"google-auth is not installed (run: {FCM_INSTALL})"
    devices = list(WebPushSubscription.objects.filter(is_active=True, endpoint__startswith=FCM_PREFIX, user__is_active=True))
    if not devices:
        return True, 0, 0, "no registered device (a browser or app must register its FCM token)"
    try:
        access = _fcm_access_token()
    except Exception as e:  # noqa: BLE001 - google-auth RefreshError / TransportError, or a bad credentials file
        return False, 0, len(devices), f"{_token_problem(e)}: no FCM access token ({type(e).__name__})", "Failed"
    image, snapshot_note = m.snapshot_url()
    link = m.incident_url
    url = f"https://fcm.googleapis.com/v1/projects/{urllib.parse.quote(project)}/messages:send"
    sent, invalid, failed, results = 0, [], 0, []
    for sub in devices:
        message = {"message": {
            "token": sub.endpoint[len(FCM_PREFIX):],
            "notification": {"title": f"{m.kind.upper()} · {m.severity}", "body": m.location, **({"image": image} if image else {})},
            "data": {"incidentId": str(m.incident_id), "type": m.kind, "severity": m.severity, "location": m.location,
                     "building": m.building, "zone": m.zone, "timestamp": m.timestamp.isoformat(), "url": link,
                     "snapshotUrl": image or "", "snapshot": "available" if image else snapshot_note},
            "webpush": {"headers": {"Urgency": "high"}, "fcm_options": {"link": link}},
            "android": {"priority": "high"}}}
        body = json.dumps(message).encode()
        for attempt in (1, 2):  # a 401 means the OAuth token expired early: refresh once and repeat
            request = urllib.request.Request(url, data=body, headers={"Authorization": f"Bearer {access}", "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(request, timeout=20) as r:
                    name = json.loads(r.read() or b"{}").get("name", "")
                sent += 1
                results.append(f"device {sub.id}: Sent {name}".strip())
            except urllib.error.HTTPError as e:
                status = _json_error(e, "error")
                if e.code == 401 and attempt == 1:
                    try:
                        access = _fcm_access_token(force=True)
                        continue
                    except Exception as refresh:  # noqa: BLE001
                        status = _token_problem(refresh)
                if e.code == 404 or status == "UNREGISTERED":  # the app was removed or the token expired: never retried
                    invalid.append(sub.id)
                    results.append(f"device {sub.id}: Invalid token ({status or e.code}), switched off")
                else:  # 429 / 5xx / 400: possibly passing; retried once by the worker
                    failed += 1
                    results.append(f"device {sub.id}: Failed HTTP {e.code} {status}".strip())
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                failed += 1
                results.append(f"device {sub.id}: Failed, Firebase unavailable ({type(e).__name__})")
            break
    if invalid:
        WebPushSubscription.objects.filter(id__in=invalid).update(is_active=False)
    # shortcut: a partly sent alert is not retried for the failed devices; a per-device delivery table would allow that
    word = "Sent" if sent else "Invalid token" if invalid and not failed else "Failed"
    return sent > 0, sent, failed + len(invalid), "; ".join([*results, f"snapshot: {'attached' if image else snapshot_note}"]), word


# ---------------------------------------------------------------- GSM modem (serial, AT commands)
class ModemError(Exception):
    pass


def _open_serial():
    import serial  # optional: pyserial, only needed when a GSM modem is configured
    return serial.Serial(_opt("GSM_PORT"), _opt("GSM_BAUD"), timeout=0.5, write_timeout=5)


def _at(port, command: str, expect: str, seconds: float = 5.0, raw: bytes | None = None) -> str:
    """Sends an AT command (or raw bytes) and reads until `expect` or ERROR appears, at most `seconds`."""
    port.write(raw if raw is not None else (command + "\r").encode("ascii"))
    deadline, answer = time.monotonic() + seconds, ""
    while time.monotonic() < deadline:
        answer += port.read(256).decode("ascii", "replace")
        if expect in answer:
            return answer
        if "ERROR" in answer:
            raise ModemError(f"{command}: {answer.strip()[-80:]}")
    raise ModemError(f"{command}: no answer within {seconds:g} s")


def send_gsm(m: AlertMessage, numbers=None):
    """SMS through a GSM modem on a serial port: no internet needed. Runs in the notification thread, never in a request.
    +CMGS means the network accepted the SMS, not that the phone received it."""
    numbers = numbers or _split(_opt("GSM_RECIPIENTS") or _opt("SMS_RECIPIENTS"))
    if not (_opt("GSM_PORT") and numbers):
        return None
    if importlib.util.find_spec("serial") is None:
        return "pyserial is not installed (pip install pyserial)"
    text = f"[{m.site}] {m.severity.upper()} {m.headline} at {m.location} #{m.incident_id}".encode("ascii", "replace").decode()[:160]
    try:
        port = _open_serial()
    except Exception as e:  # noqa: BLE001 - pyserial raises SerialException (an OSError) or ValueError for a bad port
        return False, 0, len(numbers), f"modem unavailable on {_opt('GSM_PORT')}: {type(e).__name__}"
    delivered, errors = 0, []
    try:
        _at(port, "AT", "OK")
        if "READY" not in _at(port, "AT+CPIN?", "OK"):
            raise ModemError("SIM not ready (missing, PIN locked or blocked)")
        reg = re.search(r"\+CREG: \d,(\d)", _at(port, "AT+CREG?", "OK"))
        if not reg or reg.group(1) not in ("1", "5"):  # 1 = home network, 5 = roaming
            raise ModemError("not registered on the mobile network")
        _at(port, "AT+CMGF=1", "OK")  # text mode
        for number in numbers:
            if not PHONE.match(number):
                errors.append(f"{number}: not an international number")
                continue
            try:
                _at(port, f'AT+CMGS="{number}"', ">")
                _at(port, "message", "+CMGS:", 30, raw=text.encode("ascii") + b"\x1a")  # Ctrl-Z sends it
                delivered += 1
            except ModemError as e:
                errors.append(f"{number}: {e}")
    except ModemError as e:
        errors.append(str(e))
    finally:
        port.close()
    return delivered > 0, delivered, max(len(errors), len(numbers) - delivered), "; ".join(errors)


# ---------------------------------------------------------------- dispatch, status, retries
CHANNELS = [("sms", send_sms), ("gsm", send_gsm), ("whatsapp", send_whatsapp), ("telegram", send_telegram),
            ("fcm", send_fcm), ("email", send_email), ("push", send_push)]
CONFIRMED = {"telegram"}  # providers whose success answer means the message arrived; the others only accepted it
MAX_ATTEMPTS = 2  # first try plus one retry, only for a channel that delivered nothing


def _outcome(name: str, result) -> tuple[str, int, int, str]:
    if result is None:
        return "Not configured", 0, 0, "not configured"
    if isinstance(result, str):
        return "Disabled", 0, 0, result
    ok, delivered, failed, detail, *word = result
    status = word[0] if word else "Failed" if not ok else ("Delivered" if name in CONFIRMED else "Pending") if delivered else "Not sent"
    return status, delivered, failed, detail


def _attempts(incident: IncidentAlert, name: str) -> list[str]:
    """This channel's earlier attempts for this incident at its current severity (an escalation is notified again)."""
    return list(IncidentLog.objects.filter(incident=incident, type=IncidentLogType.NOTIFICATION,
                                           details__startswith=f"{name}: ", details__contains=f"for the {incident.severity} alert")
                .values_list("details", flat=True))


SENT = ("Delivered", "Pending", "Sent")  # the provider took it: never sent again for this incident and severity


def deliver(incident_id: int, only: tuple | None = None) -> None:
    """Every configured channel once per incident severity; a channel that already sent (Delivered or Pending) is skipped, so a
    retry or a repeated job never sends twice. GSM runs only when internet SMS did not get through."""
    incident = IncidentAlert.objects.select_related("camera").filter(pk=incident_id).first()
    if incident is None:
        return
    message = AlertMessage(incident)
    sms_sent, retry = False, False
    for name, send in CHANNELS:
        if only and name not in only:
            continue
        earlier = _attempts(incident, name)
        if any(d.startswith(tuple(f"{name}: {s}" for s in SENT)) for d in earlier):
            sms_sent = sms_sent or name == "sms"
            continue
        if len(earlier) >= MAX_ATTEMPTS or (name == "gsm" and sms_sent):
            continue
        try:
            result = send(message)
        except Exception as e:  # noqa: BLE001 - one broken channel must not stop the others; reported below
            log.exception("%s notification for incident #%s failed", name, incident_id)
            result = (False, 0, 0, type(e).__name__)
        status, delivered, failed, detail = _outcome(name, result)
        sms_sent = sms_sent or (name == "sms" and delivered > 0)
        log.info("%s for incident #%s: %s delivered=%s failed=%s %s", name, incident_id, status, delivered, failed, detail)
        if status != "Not configured":  # the audit trail of what was attempted; an unconfigured channel attempted nothing
            add_log(IncidentLog(incident=incident, type=IncidentLogType.NOTIFICATION, zone=incident.zone,
                                details=f"{name}: {status} for the {incident.severity} alert · {delivered} sent, {failed} failed"
                                        f"{' · ' + detail if detail else ''}"[:500]))
            retry = retry or (status == "Failed" and len(earlier) + 1 < MAX_ATTEMPTS)
        events.publish("notification", {"incidentId": incident_id, "channel": name, "status": status, "ok": status in SENT,
                                        "skipped": result is None, "delivered": delivered, "failed": failed, "detail": detail,
                                        "timestamp": timezone.now()})
    if retry:
        with _retries_lock:
            _retries.append((time.monotonic() + _opt("NOTIFY_RETRY_SECONDS"), incident_id, only))


_retries: list[tuple[float, int, tuple | None]] = []  # (due time, incident id, channels): failed channels are retried once, later
_retries_lock = threading.Lock()


def _due_retries() -> list[tuple[int, tuple | None]]:
    now = time.monotonic()
    with _retries_lock:
        due = [(i, only) for t, i, only in _retries if t <= now]
        _retries[:] = [r for r in _retries if r[0] > now]
    return due


def channel_status() -> list[dict]:
    """Configured / Not configured / Disabled per channel from the settings alone (nothing is sent; no secret is shown)."""
    def has(*keys):
        return all(_opt(k) for k in keys)

    rows = [("SMS (Twilio, internet)", has("SMS_ACCOUNT_SID", "SMS_AUTH_TOKEN", "SMS_FROM", "SMS_RECIPIENTS"), None),
            ("SMS fallback (GSM modem)", has("GSM_PORT") and bool(_opt("GSM_RECIPIENTS") or _opt("SMS_RECIPIENTS")),
             None if importlib.util.find_spec("serial") else "pyserial is not installed"),
            ("WhatsApp (Twilio)", has("WHATSAPP_ACCOUNT_SID", "WHATSAPP_AUTH_TOKEN", "WHATSAPP_FROM", "WHATSAPP_RECIPIENTS"), None),
            ("Telegram", has("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_IDS"), None),
            ("Firebase Cloud Messaging", has("FCM_PROJECT_ID", "FCM_CREDENTIALS_FILE"),
             None if google_auth_installed() else "google-auth is not installed"),
            ("Email", bool(_opt("EMAIL_RECIPIENTS")), None if settings.EMAIL_HOST else "no SMTP host: written to the log only"),
            ("Web push", has("WEBPUSH_PUBLIC_KEY", "WEBPUSH_PRIVATE_KEY"),
             None if importlib.util.find_spec("pywebpush") else "pywebpush is not installed")]
    return [{"name": n, "status": "Not configured" if not ok else "Disabled" if why else "Configured", "reason": why or ""}
            for n, ok, why in rows]


def add_log(entry: IncidentLog) -> None:
    from .alerts import add_log as save  # alerts imports this module: imported late
    save(entry)


def _worker() -> None:
    while True:
        try:
            incident_id, _, only = jobs.get(timeout=1)
            todo = [(incident_id, only)]
        except queue.Empty:
            todo = []
        for incident_id, only in todo + _due_retries():
            try:
                deliver(incident_id, only)
            except Exception:  # noqa: BLE001 - keep the worker alive; the failure is logged
                log.exception("Notification delivery for incident #%s crashed", incident_id)
            finally:
                close_old_connections()


def start_worker() -> None:
    threading.Thread(target=_worker, daemon=True, name="notification-worker").start()

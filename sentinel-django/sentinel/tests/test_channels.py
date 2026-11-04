"""Alert channels: Telegram, WhatsApp (Twilio), Firebase Cloud Messaging and the GSM modem SMS fallback, through deliver().
MOCK-BASED: every provider (HTTP and the serial port) is faked here; nothing can reach Telegram, Twilio, Google or a modem.
That proves our side of each contract, not real delivery."""
import io
import json
import urllib.error
from unittest import mock

from django.conf import settings
from django.core.cache import cache

from sentinel import events, notifications
from sentinel.models import CameraFeed, IncidentAlert, IncidentLog, IncidentLogType

from .base import SentinelTestCase, fire

TELEGRAM = {"TELEGRAM_BOT_TOKEN": "123:BOT-SECRET-TOKEN", "TELEGRAM_CHAT_IDS": "-1001,-1002"}
WHATSAPP = {"WHATSAPP_ACCOUNT_SID": "ACwa", "WHATSAPP_AUTH_TOKEN": "wa-SECRET", "WHATSAPP_FROM": "+14155238886",
            "WHATSAPP_RECIPIENTS": "+923001234567"}
SMS = {"SMS_ACCOUNT_SID": "ACsms", "SMS_AUTH_TOKEN": "sms-SECRET", "SMS_FROM": "+15550000000", "SMS_RECIPIENTS": "+923001234567"}
GSM = {"GSM_PORT": "COM9", "SMS_RECIPIENTS": "+923001234567"}


class Answer(io.BytesIO):
    """What urlopen returns: a context manager with read(), read1(), status and headers."""
    status, headers = 200, {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(code, body):
    return urllib.error.HTTPError("https://provider.test/x", code, "error", {}, io.BytesIO(json.dumps(body).encode()))


class ChannelTestCase(SentinelTestCase):
    def setUp(self):
        super().setUp()
        cache.delete("vision-eval:cam-server")
        self.post_frame(**fire("cam-server", "Critical"))
        self.incident = IncidentAlert.objects.get()
        self.requests = []

    def network(self, *answers):
        """urlopen fake: records every request and answers in order (an Exception instance is raised)."""
        queue = list(answers)

        def urlopen(request, timeout=None):
            self.requests.append(request)
            answer = queue.pop(0) if queue else Answer(b'{"ok": true}')
            if isinstance(answer, Exception):
                raise answer
            return answer
        return mock.patch("urllib.request.urlopen", side_effect=urlopen)

    def deliver(self):
        published = []
        with mock.patch.object(events, "publish", side_effect=lambda kind, data: kind == "notification" and published.append(data)):
            notifications.deliver(self.incident.id)
        return {p["channel"]: p for p in published}

    def attempts(self, channel):
        return list(IncidentLog.objects.filter(incident=self.incident, type=IncidentLogType.NOTIFICATION, details__startswith=f"{channel}: ")
                    .order_by("id").values_list("details", flat=True))


class TelegramTests(ChannelTestCase):
    def test_a_text_alert_with_incident_details_is_delivered_once_per_chat_and_never_twice(self):
        with mock.patch.dict(settings.SENTINEL, TELEGRAM), self.network():
            result = self.deliver()
            self.assertEqual(result["telegram"]["status"], "Delivered")  # Telegram's ok = it is in the chat
            self.deliver()  # the same incident and severity again (a retry or a repeated job)
        self.assertEqual([r.full_url.rsplit("/", 1)[1] for r in self.requests], ["sendMessage", "sendMessage"])  # 2 chats, once
        sent = json.loads(self.requests[0].data)
        self.assertEqual(sent["chat_id"], "-1001")
        for part in ("CRITICAL", "FIRE DETECTED", "First Floor - Server Hall (Server Hall)", f"Incident #{self.incident.id}", "UTC"):
            self.assertIn(part, sent["text"])
        [log] = self.attempts("telegram")
        self.assertTrue(log.startswith("telegram: Delivered for the Critical alert · 2 sent, 0 failed"))
        self.assertIn("text only: no snapshot for this camera's stream type", log)  # a simulated camera has no picture

    def test_the_camera_snapshot_is_sent_as_a_photo_when_the_stream_gives_one(self):
        CameraFeed.objects.filter(pk="cam-server").update(stream_format="Mjpeg", stream_url="http://cam.test/video")
        self.incident.refresh_from_db()
        jpeg = b"\xff\xd8\xff\xe0fake-jpeg-bytes\xff\xd9"
        stream = Answer(b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n--frame")
        with mock.patch.dict(settings.SENTINEL, {**TELEGRAM, "TELEGRAM_CHAT_IDS": "-1001"}), self.network(stream):
            result = self.deliver()
        snapshot, photo = self.requests
        self.assertEqual(snapshot, "http://cam.test/video")  # urlopen got the camera URL itself
        self.assertTrue(photo.full_url.endswith("/sendPhoto"))
        self.assertIn(jpeg, photo.data)
        self.assertIn(b'name="caption"', photo.data)
        self.assertEqual(result["telegram"]["status"], "Delivered")
        self.assertIn("with photo", self.attempts("telegram")[0])

    def test_a_snapshot_that_fails_still_sends_the_text(self):
        CameraFeed.objects.filter(pk="cam-server").update(stream_format="Mjpeg", stream_url="http://cam.test/video")
        with mock.patch.dict(settings.SENTINEL, {**TELEGRAM, "TELEGRAM_CHAT_IDS": "-1001"}), self.network(OSError("camera down")):
            result = self.deliver()
        self.assertTrue(self.requests[1].full_url.endswith("/sendMessage"))
        self.assertEqual(result["telegram"]["status"], "Delivered")
        self.assertIn("text only: snapshot unavailable: OSError", self.attempts("telegram")[0])

    def test_without_a_token_nothing_is_sent_or_logged(self):
        with self.network():
            result = self.deliver()
        self.assertEqual((result["telegram"]["status"], result["telegram"]["skipped"]), ("Not configured", True))
        self.assertEqual((self.requests, self.attempts("telegram")), ([], []))

    def test_a_wrong_chat_id_fails_without_the_bot_token_in_any_log_and_the_other_chat_still_gets_it(self):
        refused = http_error(400, {"ok": False, "description": "Bad Request: chat not found"})
        with mock.patch.dict(settings.SENTINEL, TELEGRAM), self.network(refused), self.assertLogs("sentinel.notifications", "INFO") as logs:
            result = self.deliver()
        self.assertEqual((result["telegram"]["status"], result["telegram"]["delivered"], result["telegram"]["failed"]), ("Delivered", 1, 1))
        log = self.attempts("telegram")[0]
        self.assertIn("chat -1001: HTTP 400 Bad Request: chat not found", log)
        self.assertNotIn("BOT-SECRET-TOKEN", log + "\n".join(logs.output) + json.dumps(result, default=str))

    def test_a_provider_failure_is_retried_once_later_and_then_given_up(self):
        down = urllib.error.URLError(ConnectionRefusedError("refused"))
        with mock.patch.dict(settings.SENTINEL, {**TELEGRAM, "TELEGRAM_CHAT_IDS": "-1001"}), self.network(down, down, down):
            self.assertEqual(self.deliver()["telegram"]["status"], "Failed")
            self.assertEqual([r[1] for r in notifications._retries], [self.incident.id])  # the worker retries it later
            self.deliver()
            self.deliver()  # a third run: the 2 attempts are used up
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(len(self.attempts("telegram")), 2)
        self.assertIn("chat -1001: URLError", self.attempts("telegram")[0])

    def test_an_escalation_is_a_new_alert(self):
        with mock.patch.dict(settings.SENTINEL, {**TELEGRAM, "TELEGRAM_CHAT_IDS": "-1001"}), self.network():
            self.deliver()
            IncidentAlert.objects.filter(pk=self.incident.pk).update(severity="High")
            self.deliver()
            IncidentAlert.objects.filter(pk=self.incident.pk).update(severity="Critical")
            self.deliver()  # Critical was already sent
        self.assertEqual(len(self.requests), 2)


class WhatsAppTests(ChannelTestCase):
    def test_a_message_goes_through_twilios_whatsapp_sender_and_is_pending_not_delivered(self):
        with mock.patch.dict(settings.SENTINEL, WHATSAPP), self.network(Answer(b'{"sid": "SM1", "status": "queued"}')):
            result = self.deliver()
            self.deliver()
        [request] = self.requests  # once, the second run skipped it
        sent = dict(x.split("=", 1) for x in request.data.decode().split("&"))
        self.assertEqual((sent["To"], sent["From"]), ("whatsapp%3A%2B923001234567", "whatsapp%3A%2B14155238886"))
        self.assertIn("/Accounts/ACwa/Messages.json", request.full_url)
        self.assertEqual(result["whatsapp"]["status"], "Pending")  # Twilio queued it; no read receipt
        self.assertIn("photo not attached (Twilio needs a public image URL)", self.attempts("whatsapp")[0])

    def test_sms_credentials_alone_do_not_turn_whatsapp_on(self):
        with mock.patch.dict(settings.SENTINEL, SMS), self.network(Answer(b"{}")):
            result = self.deliver()
        self.assertEqual(result["whatsapp"]["status"], "Not configured")
        self.assertEqual([r.full_url.split("/Accounts/")[1].split("/")[0] for r in self.requests], ["ACsms"])  # only the SMS one

    def test_an_invalid_recipient_is_refused_before_any_call(self):
        with mock.patch.dict(settings.SENTINEL, {**WHATSAPP, "WHATSAPP_RECIPIENTS": "03001234567"}), self.network():
            result = self.deliver()
        self.assertEqual((self.requests, result["whatsapp"]["status"]), ([], "Failed"))
        self.assertIn("03001234567: not an international number", self.attempts("whatsapp")[0])

    def test_a_provider_error_is_logged_without_secrets_and_retried_once(self):
        refused = http_error(400, {"code": 63016, "message": "Freeform message outside the 24 h window, use a template"})
        with mock.patch.dict(settings.SENTINEL, WHATSAPP), self.network(refused, refused, Answer(b"{}")):
            self.assertEqual(self.deliver()["whatsapp"]["status"], "Failed")
            self.assertEqual(self.deliver()["whatsapp"]["status"], "Failed")
            self.deliver()
        self.assertEqual(len(self.requests), 2)
        log = self.attempts("whatsapp")[0]
        self.assertIn("Freeform message outside the 24 h window", log)
        self.assertNotIn("wa-SECRET", "".join(self.attempts("whatsapp")))


class FakeModem:
    """A serial port answering AT commands from `answers` (command prefix -> reply); records what was written."""

    def __init__(self, **answers):
        self.answers = {"AT": "OK", "AT+CPIN?": "+CPIN: READY\r\nOK", "AT+CREG?": "+CREG: 0,1\r\nOK", "AT+CMGF=1": "OK",
                        "AT+CMGS=": ">", "\x1a": "+CMGS: 7\r\nOK", **answers}
        self.written, self.pending, self.closed = [], "", False

    def write(self, data: bytes):
        text = data.decode("ascii", "replace")
        self.written.append(text)
        key = "\x1a" if text.endswith("\x1a") else next(k for k in sorted(self.answers, key=len, reverse=True) if text.startswith(k))
        self.pending = self.answers[key]

    def read(self, n):
        out, self.pending = self.pending.encode(), ""
        return out

    def close(self):
        self.closed = True


class GsmTests(ChannelTestCase):
    def modem(self, port=None, error=None):
        def open_serial():
            if error:
                raise error
            return port
        real = notifications.importlib.util.find_spec
        return mock.patch.multiple(notifications, _open_serial=open_serial,  # pyserial is not installed here: pretend it is
                                   importlib=mock.Mock(util=mock.Mock(find_spec=lambda name: real("json") if name == "serial" else real(name))))

    def test_the_at_command_sequence_sends_the_alert_text_mode(self):
        port = FakeModem()
        with mock.patch.dict(settings.SENTINEL, GSM), self.modem(port), self.network():
            result = self.deliver()
        self.assertEqual([w.split("\r")[0] for w in port.written[:5]], ["AT", "AT+CPIN?", "AT+CREG?", "AT+CMGF=1", 'AT+CMGS="+923001234567"'])
        text = port.written[5]
        self.assertTrue(text.endswith("\x1a") and len(text) <= 161 and text.isascii())
        self.assertIn(f"CRITICAL FIRE DETECTED at First Floor - Server Hall (Server Hall) #{self.incident.id}", text)
        self.assertTrue(port.closed)
        self.assertEqual(result["gsm"]["status"], "Pending")  # +CMGS: the network took it; no delivery report asked
        self.assertEqual(self.requests, [])  # nothing over the internet

    def test_modem_sim_network_and_send_problems_are_explicit_failures(self):
        cases = [({"error": OSError("COM9 not found")}, "modem unavailable on COM9: OSError"),
                 ({"port": FakeModem(**{"AT+CPIN?": "+CPIN: SIM PIN\r\nOK"})}, "SIM not ready"),
                 ({"port": FakeModem(**{"AT+CREG?": "+CREG: 0,2\r\nOK"})}, "not registered on the mobile network"),
                 ({"port": FakeModem(**{"\x1a": "+CMS ERROR: 38"})}, "+923001234567: message: +CMS ERROR: 38")]
        for args, expected in cases:
            IncidentLog.objects.filter(type=IncidentLogType.NOTIFICATION).delete()
            with self.subTest(expected), mock.patch.dict(settings.SENTINEL, GSM), self.modem(**args):
                self.assertEqual(self.deliver()["gsm"]["status"], "Failed")
                self.assertIn(expected, self.attempts("gsm")[0])

    def test_a_silent_modem_times_out(self):
        clock = iter(range(0, 10_000))
        with mock.patch.dict(settings.SENTINEL, GSM), self.modem(FakeModem(AT="")), \
                mock.patch.object(notifications.time, "monotonic", side_effect=lambda: next(clock)):
            self.assertEqual(self.deliver()["gsm"]["status"], "Failed")
        self.assertIn("AT: no answer within 5 s", self.attempts("gsm")[0])

    def test_gsm_is_the_fallback_only_when_internet_sms_fails(self):
        port = FakeModem()
        with mock.patch.dict(settings.SENTINEL, {**GSM, **SMS}), self.modem(port), self.network(Answer(b'{"status": "queued"}')):
            result = self.deliver()
        self.assertEqual((result["sms"]["status"], "gsm" in result, port.written), ("Pending", False, []))  # Twilio took it

        IncidentLog.objects.filter(type=IncidentLogType.NOTIFICATION).delete()
        with mock.patch.dict(settings.SENTINEL, {**GSM, **SMS}), self.modem(port), self.network(http_error(401, {"message": "Authenticate"})):
            result = self.deliver()
            self.deliver()  # the SMS retry fails again; the GSM SMS already went out: not sent twice
        self.assertEqual((result["sms"]["status"], result["gsm"]["status"]), ("Failed", "Pending"))
        self.assertEqual(sum(w.endswith("\x1a") for w in port.written), 1)
        self.assertTrue(self.attempts("sms")[0].startswith("sms: Failed"))

    def test_gsm_disabled_or_unconfigured_says_so(self):
        with self.network():
            self.assertEqual(self.deliver()["gsm"]["status"], "Not configured")
        with mock.patch.dict(settings.SENTINEL, GSM), self.network():  # pyserial is really not installed here
            result = self.deliver()
        self.assertEqual((result["gsm"]["status"], result["gsm"]["detail"]), ("Disabled", "pyserial is not installed (pip install pyserial)"))


class ChannelStatusTests(SentinelTestCase):
    def test_the_notifications_page_lists_every_channel_without_secrets(self):
        self.client.force_login(self.operator)
        with mock.patch.dict(settings.SENTINEL, {**TELEGRAM, **GSM}):
            html = self.client.get("/notifications").content.decode()
            rows = {c["name"]: c["status"] for c in notifications.channel_status()}
        self.assertEqual(rows, {"SMS (Twilio, internet)": "Not configured", "SMS fallback (GSM modem)": "Disabled",
                                "WhatsApp (Twilio)": "Not configured", "Telegram": "Configured",
                                "Firebase Cloud Messaging": "Not configured", "Email": "Not configured", "Web push": "Not configured"})
        self.assertIn('data-channel="Telegram"', html)
        self.assertIn("pyserial is not installed", html)
        self.assertIn("No alert was sent yet.", html)
        self.assertNotIn("BOT-SECRET-TOKEN", html)

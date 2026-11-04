"""Firebase Cloud Messaging: google-auth credentials and token refresh, device tokens, fire/smoke payloads, snapshots, failures.
MOCK-BASED. google-auth is not installed in this venv, so a stand-in with the same interface (service_account.Credentials,
transport.Request/Response, exceptions) is put in sys.modules; it drives our urllib transport like the library would. Every
HTTP request (Google's token endpoint, FCM) is faked: nothing reaches Google. Real RS256 signing and delivery are NOT tested."""
import importlib.machinery
import json
import sys
import types
import urllib.error
from datetime import datetime, timedelta, timezone as dt_timezone
from unittest import mock

from django.conf import settings
from django.core.cache import cache
from rest_framework.test import APIClient

from sentinel import events, notifications
from sentinel.models import AppUser, CameraFeed, IncidentAlert, IncidentLog, IncidentLogType, UserRole, WebPushSubscription

from .base import PASSWORD, SentinelTestCase, fire
from .test_channels import Answer, http_error

FCM = {"FCM_PROJECT_ID": "sentinel-test", "FCM_CREDENTIALS_FILE": "C:/keys/service-account.json"}
TOKEN_URI = "https://oauth2.googleapis.com/token"
SEND_URL = "https://fcm.googleapis.com/v1/projects/sentinel-test/messages:send"


def fake_google_auth(refreshes: list, bad_file=False):
    """sys.modules entries that look like google-auth: Credentials.refresh() POSTs to the token endpoint through the
    transport it is given (ours), as the real library does after signing its JWT grant."""
    def module(name):
        m = types.ModuleType(name)
        m.__spec__ = importlib.machinery.ModuleSpec(name, None)
        return m

    google, auth, transport, exceptions, oauth2, service_account = (module(n) for n in (
        "google", "google.auth", "google.auth.transport", "google.auth.exceptions", "google.oauth2", "google.oauth2.service_account"))
    transport.Request, transport.Response = type("Request", (), {}), type("Response", (), {})
    exceptions.RefreshError = type("RefreshError", (Exception,), {})
    exceptions.TransportError = type("TransportError", (Exception,), {})

    class Credentials:
        def __init__(self, scopes):
            self.scopes, self.token, self.expiry = scopes, None, None

        @classmethod
        def from_service_account_file(cls, path, scopes):
            if bad_file:
                raise ValueError("Service account info was not in the expected format")
            return cls(scopes)

        @property
        def valid(self):
            return self.token is not None and self.expiry > datetime.now(dt_timezone.utc)

        def refresh(self, request):
            response = request(TOKEN_URI, method="POST", body=b"grant_type=jwt-bearer&assertion=signed", headers={})
            refreshes.append(response.status)
            if response.status != 200:
                raise exceptions.RefreshError(f"invalid_grant: {json.loads(response.data).get('error')}")
            answer = json.loads(response.data)
            self.token, self.expiry = answer["access_token"], datetime.now(dt_timezone.utc) + timedelta(seconds=answer["expires_in"])

    service_account.Credentials = Credentials
    google.auth, google.oauth2, auth.transport, auth.exceptions, oauth2.service_account = auth, oauth2, transport, exceptions, service_account
    return {m.__name__: m for m in (google, auth, transport, exceptions, oauth2, service_account)}


class FcmTestCase(SentinelTestCase):
    def setUp(self):
        super().setUp()
        self.refreshes, self.requests = [], []
        self.client.force_login(self.operator)

    def google(self, bad_file=False):
        return mock.patch.dict(sys.modules, fake_google_auth(self.refreshes, bad_file))

    def network(self, *answers, token_expires_in=3600):
        """urlopen fake: the token endpoint always answers with an access token; FCM sends answer in order."""
        queue = list(answers)

        def urlopen(request, timeout=None):
            url = request if isinstance(request, str) else request.full_url
            self.requests.append(request)
            if url == TOKEN_URI:
                return Answer(json.dumps({"access_token": f"oauth-SECRET-{len(self.refreshes)}", "expires_in": token_expires_in}).encode())
            answer = queue.pop(0) if queue else Answer(b'{"name": "projects/sentinel-test/messages/0:1"}')
            if isinstance(answer, Exception):
                raise answer
            return answer
        return mock.patch("urllib.request.urlopen", side_effect=urlopen)

    def sends(self):
        return [r for r in self.requests if not isinstance(r, str) and r.full_url == SEND_URL]

    def register(self, token="ZmlyZWJhc2UtdG9rZW4tYWJjZGVmZ2hpamts", client=None, **extra):
        return (client or self.op).post("/api/push/fcm-token", {"token": token, **extra}, format="json")

    def emergency(self, camera="cam-server"):
        cache.delete(f"vision-eval:{camera}")
        self.post_frame(**fire(camera, "Critical"))
        return IncidentAlert.objects.get(camera_id=camera)

    def deliver(self, incident):
        published = []
        with mock.patch.object(events, "publish", side_effect=lambda kind, data: kind == "notification" and published.append(data)):
            notifications.deliver(incident.id, ("fcm",))
        return next((p for p in published if p["channel"] == "fcm"), None)

    def log(self, incident):
        return list(IncidentLog.objects.filter(incident=incident, type=IncidentLogType.NOTIFICATION, details__startswith="fcm: ")
                    .order_by("id").values_list("details", flat=True))


class FcmAuthTests(FcmTestCase):
    def test_status_says_not_configured_missing_dependency_invalid_credentials_or_configured(self):
        self.assertEqual(notifications.fcm_status()["status"], "Not configured")
        with mock.patch.dict(settings.SENTINEL, FCM):
            missing = notifications.fcm_status()  # google-auth is really not installed here
            self.assertEqual(missing["status"], "Missing dependency")
            self.assertIn(r"sentinel-django\.venv\Scripts\python -m pip install google-auth", missing["reason"])
            with self.google(bad_file=True):
                self.assertEqual(notifications.fcm_status()["status"], "Invalid credentials")
            notifications._fcm_creds.update(path=None, value=None)
            with self.google():
                self.assertEqual(notifications.fcm_status()["status"], "Configured")
        self.assertEqual(self.refreshes, [])  # the status check never calls Google

    def test_without_the_library_nothing_crashes_and_nothing_is_sent(self):
        self.register()
        incident = self.emergency()
        with mock.patch.dict(settings.SENTINEL, FCM), self.network():
            result = self.deliver(incident)
        self.assertEqual((result["status"], self.requests), ("Disabled", []))
        self.assertIn("pip install google-auth", result["detail"])

    def test_the_library_gets_a_token_through_our_transport_reuses_it_and_refreshes_it_when_expired(self):
        self.register()
        with mock.patch.dict(settings.SENTINEL, FCM), self.google(), self.network():
            self.deliver(self.emergency("cam-server"))
            self.deliver(self.emergency("cam-lobby"))  # still valid: no new token
            self.assertEqual(self.refreshes, [200])
            notifications._fcm_creds["value"].expiry = datetime.now(dt_timezone.utc) - timedelta(seconds=1)
            self.deliver(self.emergency("cam-warehouse"))
        self.assertEqual(self.refreshes, [200, 200])
        token_calls = [r for r in self.requests if not isinstance(r, str) and r.full_url == TOKEN_URI]
        self.assertEqual([r.get_method() for r in token_calls], ["POST", "POST"])
        self.assertEqual([r.headers["Authorization"] for r in self.sends()], ["Bearer oauth-SECRET-0"] * 2 + ["Bearer oauth-SECRET-1"])

    def test_an_fcm_401_forces_one_refresh_and_one_resend(self):
        self.register()
        with mock.patch.dict(settings.SENTINEL, FCM), self.google(), self.network(http_error(401, {"error": {"status": "UNAUTHENTICATED"}})):
            result = self.deliver(self.emergency())
        self.assertEqual((result["status"], len(self.sends()), self.refreshes), ("Sent", 2, [200, 200]))

    def test_revoked_credentials_or_an_unreachable_google_fail_clearly_without_secrets(self):
        self.register()
        incident = self.emergency()
        refused = http_error(400, {"error": "invalid_grant", "error_description": "Invalid JWT Signature."})

        def urlopen(request, timeout=None):
            raise refused
        with mock.patch.dict(settings.SENTINEL, FCM), self.google(), mock.patch("urllib.request.urlopen", side_effect=urlopen):
            result = self.deliver(incident)
        self.assertEqual(result["status"], "Failed")
        self.assertIn("Invalid credentials: no FCM access token (RefreshError)", self.log(incident)[0])

        IncidentLog.objects.filter(type=IncidentLogType.NOTIFICATION).delete()
        notifications._fcm_creds.update(path=None, value=None)
        down = urllib.error.URLError(TimeoutError("timed out"))
        with mock.patch.dict(settings.SENTINEL, FCM), self.google(), mock.patch("urllib.request.urlopen", side_effect=down):
            self.deliver(incident)
            self.assertEqual(notifications.fcm_status()["status"], "Firebase unavailable")
        self.assertIn("Firebase unavailable: no FCM access token (TransportError)", self.log(incident)[0])
        self.assertNotIn("SECRET", "".join(self.log(incident)))


class FcmDeviceTests(FcmTestCase):
    def test_register_duplicate_and_a_rotated_token(self):
        first = self.register()
        self.assertEqual((first.status_code, first.json()["created"]), (201, True))
        again = self.register()
        self.assertEqual((again.status_code, again.json()["created"]), (200, False))  # the same device: no second row
        self.assertEqual(WebPushSubscription.objects.count(), 1)
        rotated = self.register("bmV3LXRva2VuLWFmdGVyLXJvdGF0aW9uLTEyMw", replaces="ZmlyZWJhc2UtdG9rZW4tYWJjZGVmZ2hpamts")
        self.assertEqual(rotated.status_code, 201)
        self.assertEqual(sorted(WebPushSubscription.objects.values_list("endpoint", "is_active")),
                         [("fcm:ZmlyZWJhc2UtdG9rZW4tYWJjZGVmZ2hpamts", False), ("fcm:bmV3LXRva2VuLWFmdGVyLXJvdGF0aW9uLTEyMw", True)])

    def test_users_only_see_and_manage_their_own_devices(self):
        self.register("b3BlcmF0b3ItcGhvbmUtdG9rZW4tMTIzNDU2")
        self.register("b3BlcmF0b3ItbGFwdG9wLXRva2VuLTk4NzY1", client=self.op)  # a second device of the same user
        self.register("YWRtaW4tYnJvd3Nlci10b2tlbi0xMjM0NTY3OA", client=self.admin_client)
        mine = self.op.get("/api/push/fcm-token").json()["devices"]
        self.assertEqual(len(mine), 2)
        self.assertEqual(sorted(d["tokenEnd"] for d in mine), ["LTk4NzY1", "MTIzNDU2"])
        self.assertNotIn("b3BlcmF0b3ItcGhvbmUtdG9rZW4tMTIzNDU2", json.dumps(mine))  # never the whole token
        not_mine = self.op.delete("/api/push/fcm-token", {"token": "YWRtaW4tYnJvd3Nlci10b2tlbi0xMjM0NTY3OA"}, format="json")
        self.assertEqual(not_mine.status_code, 404)
        self.assertTrue(WebPushSubscription.objects.get(user=self.admin).is_active)
        stolen = self.register("bmV3LXRva2Vu-LXN0b2xlbi0xMjM0NTY", replaces="YWRtaW4tYnJvd3Nlci10b2tlbi0xMjM0NTY3OA")
        self.assertEqual(stolen.status_code, 404)  # cannot switch off another user's device by "replacing" it
        self.assertEqual(self.op.delete("/api/push/fcm-token", {"token": "b3BlcmF0b3ItcGhvbmUtdG9rZW4tMTIzNDU2"}, format="json").status_code, 204)
        self.assertEqual(sum(d["active"] for d in self.op.get("/api/push/fcm-token").json()["devices"]), 1)

    def test_devices_keys_and_anonymous_callers_cannot_register(self):
        self.assertEqual(self.register(client=APIClient()).status_code, 401)
        self.assertEqual(self.register(client=self.device).status_code, 401)
        self.assertEqual(self.register("too-short").status_code, 400)

    def test_the_page_gets_the_public_web_config_only_when_complete(self):
        self.assertIsNone(self.op.get("/api/push/fcm-token").json()["webConfig"])
        web = {**FCM, "FCM_WEB_API_KEY": "AIza-public", "FCM_WEB_APP_ID": "1:2:web:3", "FCM_SENDER_ID": "2", "FCM_VAPID_KEY": "BPublic"}
        with mock.patch.dict(settings.SENTINEL, web):
            info = self.op.get("/api/push/fcm-token").json()
        self.assertEqual(info["webConfig"], {"apiKey": "AIza-public", "projectId": "sentinel-test", "messagingSenderId": "2",
                                             "appId": "1:2:web:3", "vapidKey": "BPublic"})
        self.assertNotIn("service-account", json.dumps(info))  # the credentials path is never sent to the browser

    def test_a_deactivated_user_gets_no_push(self):
        user = AppUser(email="left@test.local", display_name="Left", role=UserRole.OPERATOR)
        user.set_password(PASSWORD)
        user.save(force_insert=True)
        self.register("bGVmdC10aGUtY29tcGFueS10b2tlbi0xMjM0NQ", client=self.as_user(user))
        AppUser.objects.filter(pk=user.pk).update(is_active=False)
        with mock.patch.dict(settings.SENTINEL, FCM), self.google(), self.network():
            result = self.deliver(self.emergency())
        self.assertEqual((result["status"], self.sends()), ("Not sent", []))
        self.assertIn("no registered device", result["detail"])


class FcmDispatchTests(FcmTestCase):
    def message(self, request):
        return json.loads(request.data)["message"]

    def test_a_fire_push_carries_type_severity_place_time_and_the_incident_link(self):
        plan = self.op.post("/api/floorplans", {"name": "First floor", "building": "Block A", "widthMeters": 20, "heightMeters": 10,
                                                "elements": []}, format="json").json()
        self.op.put("/api/cameras/cam-server/calibration", {"floorPlanId": plan["id"], "imagePoints": [[0, 0], [640, 0], [640, 480], [0, 480]],
                                                            "planPoints": [[1, 1], [9, 1], [9, 9], [1, 9]]}, format="json")
        self.register()
        incident = self.emergency()
        with mock.patch.dict(settings.SENTINEL, FCM), self.google(), self.network():
            result = self.deliver(incident)
            self.deliver(incident)  # the same incident again: not sent twice
        [request] = self.sends()
        m = self.message(request)
        self.assertEqual(m["notification"], {"title": "FIRE · Critical", "body": "First Floor - Server Hall (Server Hall)"})
        self.assertEqual({k: m["data"][k] for k in ("incidentId", "type", "severity", "building", "zone")},
                         {"incidentId": str(incident.id), "type": "Fire", "severity": "Critical", "building": "Block A · First floor",
                          "zone": "First Floor - Server Hall"})
        self.assertEqual(m["data"]["url"], f"http://localhost:5291/incidents?incident={incident.id}")
        self.assertEqual(m["webpush"]["fcm_options"]["link"], m["data"]["url"])
        self.assertTrue(m["data"]["timestamp"].startswith(str(incident.timestamp.date())))
        self.assertTrue(all(isinstance(v, str) for v in m["data"].values()))  # FCM data values must be strings
        self.assertLess(len(request.data), 4096)  # FCM's payload limit
        self.assertEqual((result["status"], m["data"]["snapshot"]), ("Sent", "no snapshot for this camera's stream type"))
        self.assertIn("device 1: Sent projects/sentinel-test/messages/0:1", self.log(incident)[0].replace(
            f"device {WebPushSubscription.objects.get().id}", "device 1"))

    def test_a_new_smoke_incident_is_pushed_and_says_smoke_while_sms_stays_for_emergencies(self):
        self.register()
        self.post_frame(cameraId="cam-lobby", frameWidth=640, frameHeight=480, detections=[
            {"category": "Smoke", "confidence": 0.7, "bbox": [0, 0, 320, 240]}])
        incident = IncidentAlert.objects.get()
        self.assertEqual(incident.severity, "Medium")  # not an emergency
        [(incident_id, force, only)] = list(notifications.jobs.queue)
        self.assertEqual((incident_id, only), (incident.id, ("fcm",)))
        with mock.patch.dict(settings.SENTINEL, {**FCM, "SMS_ACCOUNT_SID": "AC", "SMS_AUTH_TOKEN": "t", "SMS_FROM": "+15550000000",
                                                 "SMS_RECIPIENTS": "+923001234567"}), self.google(), self.network():
            notifications.deliver(incident.id, only)
        [request] = self.sends()
        m = self.message(request)
        self.assertEqual((m["notification"]["title"], m["data"]["type"]), ("SMOKE · Medium", "Smoke"))
        self.assertFalse(any("twilio" in getattr(r, "full_url", "") for r in self.requests))  # no SMS for a non-emergency

    def test_a_snapshot_is_stored_once_and_linked_with_a_signature(self):
        CameraFeed.objects.filter(pk="cam-server").update(stream_format="Mjpeg", stream_url="http://cam.test/video")
        self.register()
        incident = self.emergency()
        jpeg = b"\xff\xd8\xff\xe0snapshot\xff\xd9"
        with mock.patch.dict(settings.SENTINEL, FCM), self.google(), self.network():
            with mock.patch.object(notifications.AlertMessage, "_grab", return_value=(jpeg, "")) as grab:
                self.deliver(incident)
                notifications.AlertMessage(IncidentAlert.objects.get(pk=incident.pk)).snapshot()
        grab.assert_called_once()  # stored: the second use reads the file
        m = self.message(self.sends()[0])
        image = m["notification"]["image"]
        self.assertEqual((m["data"]["snapshot"], m["data"]["snapshotUrl"]), ("available", image))
        path = image.split("localhost:5291", 1)[1]
        anonymous = APIClient()
        self.assertEqual(anonymous.get(path).content, jpeg)  # the signed link works without a session
        self.assertEqual(anonymous.get(f"/api/incidents/{incident.id}/snapshot").status_code, 401)
        self.assertEqual(anonymous.get(path.replace("?s=", "?s=x")).status_code, 401)
        self.assertEqual(anonymous.get(path.replace(f"/{incident.id}/", f"/{incident.id + 1}/")).status_code, 401)  # one incident only
        self.assertEqual(self.op.get(f"/api/incidents/{incident.id}/snapshot").status_code, 200)

    def test_an_invalid_token_is_switched_off_and_never_retried(self):
        self.register()
        gone = http_error(404, {"error": {"code": 404, "status": "UNREGISTERED"}})
        with mock.patch.dict(settings.SENTINEL, FCM), self.google(), self.network(gone):
            result = self.deliver(self.emergency())
        self.assertEqual(result["status"], "Invalid token")
        self.assertFalse(WebPushSubscription.objects.get().is_active)
        self.assertEqual(notifications._retries, [])

    def test_a_firebase_error_or_timeout_is_retried_once_and_then_given_up(self):
        self.register()
        incident = self.emergency()
        with mock.patch.dict(settings.SENTINEL, FCM), self.google(), \
                self.network(http_error(503, {"error": {"status": "UNAVAILABLE"}}), urllib.error.URLError(TimeoutError("timed out"))):
            self.assertEqual(self.deliver(incident)["status"], "Failed")
            self.assertEqual([r[1:] for r in notifications._retries], [(incident.id, ("fcm",))])
            self.assertEqual(self.deliver(incident)["status"], "Failed")
            self.assertIsNone(self.deliver(incident))  # 2 attempts used: no third
        first, second = self.log(incident)
        self.assertIn("Failed HTTP 503 UNAVAILABLE", first)
        self.assertIn("Failed, Firebase unavailable (URLError)", second)
        self.assertTrue(WebPushSubscription.objects.get().is_active)  # a passing outage never removes a device

    def test_incidents_still_open_while_firebase_is_down(self):
        self.register()
        with mock.patch.dict(settings.SENTINEL, FCM), self.google(), mock.patch("urllib.request.urlopen", side_effect=OSError("down")):
            r = self.post_frame(**fire("cam-server", "Critical"))
            self.assertEqual(r.status_code, 202)
            incident = IncidentAlert.objects.get()
            notifications.deliver(incident.id)  # every channel; Firebase fails, nothing raises
        self.assertFalse(IncidentAlert.objects.get(pk=incident.pk).is_resolved)
        self.assertIn("Firebase unavailable", self.log(incident)[0])

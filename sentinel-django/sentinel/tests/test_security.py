"""Sign-in, roles, CSRF and the live event stream (replaces the SignalR hub tests in ApiIntegrationTests.cs)."""
import asyncio
import contextlib
import json
import os
import subprocess
import sys
from unittest import mock

from asgiref.sync import sync_to_async
from django.conf import settings
from django.test import Client
from rest_framework.test import APIClient

from sentinel import events
from sentinel.security import hash_password, verify_password

from sentinel.models import SmartSprinkler

from .base import ADMIN_EMAIL, PASSWORD, SENSOR_KEY, SentinelTestCase

# Made by ASP.NET Core Identity itself (PasswordHasher<T>.HashPassword("Passw0rd!"), .NET 8.0.31, 2026-10-26).
DOTNET_HASH = "AQAAAAIAAYagAAAAEG6EF5pmYYGrmPYD28iAQMCdf9PrgvJI1y+hjJL8nVx5vnYi5+lx7DUneURCSm8wiw=="


class PasswordTests(SentinelTestCase):
    def test_hashes_written_by_aspnet_identity_still_verify(self):
        self.assertTrue(verify_password(DOTNET_HASH, "Passw0rd!"))
        self.assertFalse(verify_password(DOTNET_HASH, "passw0rd!"))
        self.assertFalse(verify_password("not base64 at all", "Passw0rd!"))
        self.assertFalse(verify_password("", "Passw0rd!"))

    def test_new_hashes_use_the_identity_v3_format(self):
        stored = hash_password("s3cret-pass")
        self.assertTrue(stored.startswith("AQAAAAIAAYag"))  # v3, PBKDF2-HMAC-SHA512, 100 000 iterations: .NET accepts it
        self.assertTrue(verify_password(stored, "s3cret-pass"))
        self.assertNotEqual(stored, hash_password("s3cret-pass"))  # random salt


class PageTests(SentinelTestCase):
    def test_pages_need_a_login_and_the_login_page_signs_in(self):
        client = Client(enforce_csrf_checks=True)
        for page in ("/", "/floor-plans", "/ai", "/devices"):
            self.assertRedirects(client.get(page), f"/login?next={page}", fetch_redirect_response=False)
        login = client.get("/login")
        token = login.cookies["csrftoken"].value
        wrong = client.post("/login", {"email": ADMIN_EMAIL, "password": "wrong-pass", "csrfmiddlewaretoken": token})
        self.assertContains(wrong, "Invalid email or password.")
        ok = client.post("/login?next=/floor-plans", {"email": ADMIN_EMAIL, "password": PASSWORD, "csrfmiddlewaretoken": token})
        self.assertRedirects(ok, "/floor-plans", fetch_redirect_response=False)
        # Until live data proves otherwise the banner is never green (audit D-15): the page starts "degraded".
        self.assertContains(client.get("/"), "MONITORING DEGRADED · LIVE LINK DOWN")
        self.admin.refresh_from_db()
        self.assertIsNotNone(self.admin.last_login)  # LastLoginAt is stored, as before

    def test_login_never_redirects_to_another_site(self):
        r = self.client.post("/login?next=https://evil.example/", {"email": ADMIN_EMAIL, "password": PASSWORD})
        self.assertRedirects(r, "/", fetch_redirect_response=False)

    def test_login_page_is_rate_limited_per_client(self):
        for _ in range(10):
            self.client.post("/login", {"email": ADMIN_EMAIL, "password": "wrong-pass"})
        self.assertEqual(self.client.post("/login", {"email": ADMIN_EMAIL, "password": PASSWORD}).status_code, 429)

    def test_api_calls_from_the_browser_session_need_the_csrf_token(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.operator)
        self.assertEqual(client.post("/api/incidents/dispatch", {"zone": "Ground Floor - Lobby"}, content_type="application/json").status_code, 403)
        client.get("/")  # sets the CSRF cookie, as the dashboard page does
        token = client.cookies["csrftoken"].value
        r = client.post("/api/incidents/dispatch", {"zone": "Ground Floor - Lobby"}, content_type="application/json", HTTP_X_CSRFTOKEN=token)
        self.assertEqual(r.status_code, 201)

    def test_only_admins_change_cameras_on_the_devices_page(self):
        self.client.force_login(self.operator)
        self.assertNotContains(self.client.get("/devices"), "+ Camera")
        self.assertEqual(self.client.post("/devices", {"delete": "cam-lobby"}).status_code, 403)
        self.client.force_login(self.admin)
        form = {"id": "cam-gate", "name": "Main Gate", "feed_type": "CCTV", "location_zone": "Ground Floor - Gate",
                "stream_url": "", "stream_format": "Simulated"}
        self.assertContains(self.client.post("/devices?add", {**form, "location_zone": "Gate"}), "Floor - Area")  # validation shown
        self.assertRedirects(self.client.post("/devices?add", form), "/devices", fetch_redirect_response=False)
        self.assertContains(self.client.get("/devices"), '<td class="strong">Main Gate</td>', html=False)
        self.assertRedirects(self.client.post("/devices", {"delete": "cam-gate"}), "/devices", fetch_redirect_response=False)
        page = self.client.get("/devices")
        self.assertNotContains(page, '<td class="strong">Main Gate</td>', html=False)
        self.assertContains(page, "Camera deleted: Main Gate")

    def test_logout_needs_a_post(self):
        self.client.force_login(self.operator)
        self.assertEqual(self.client.get("/logout").status_code, 405)
        self.assertRedirects(self.client.post("/logout"), "/login", fetch_redirect_response=False)
        self.assertEqual(self.client.get("/").status_code, 302)


class LiveEventTests(SentinelTestCase):
    async def read_event(self, stream) -> tuple[str, dict]:
        while True:
            chunk = (await asyncio.wait_for(anext(stream), 5)).decode()
            if chunk.startswith("event: "):
                name, data = chunk.split("\n")[:2]
                return name[7:], json.loads(data[6:])

    async def test_stream_sends_the_snapshot_then_every_device_frame(self):
        self.assertEqual((await self.async_client.get("/api/events")).status_code, 401)
        await self.async_client.aforce_login(self.operator)
        response = await self.async_client.get("/api/events")
        self.assertEqual(response["Content-Type"], "text/event-stream")
        stream = aiter(response.streaming_content)
        name, snapshot = await self.read_event(stream)
        self.assertEqual((name, len(snapshot["cameras"])), ("snapshot", 4))

        r = await sync_to_async(self.post_frame)(cameraId="cam-lobby", normalized=True,
                                                 detections=[{"category": "Human", "bbox": [0.1, 0.1, 0.2, 0.4], "trackId": "p1"}])
        self.assertEqual(r.status_code, 202)
        name, frame = await self.read_event(stream)
        self.assertEqual((name, frame["cameraId"], frame["humanCount"]), ("detection", "cam-lobby", 1))
        # A closed browser tab: the server cancels the read that is waiting for the next event (as Django's ASGI handler does).
        pending = asyncio.ensure_future(anext(stream))
        await asyncio.sleep(0.05)
        pending.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pending
        self.assertEqual(events.subscriber_count(), 0)  # a closed dashboard is forgotten

    def test_device_keys_cannot_open_the_dashboard_stream(self):
        self.assertEqual(APIClient(HTTP_X_DEVICE_KEY="test-device-key").get("/api/events").status_code, 401)


class DeviceRoleTests(SentinelTestCase):
    """Least privilege: the AI service's key and a sensor gateway's key each reach only their own endpoints; no device key
    switches a relay or acts as an operator. Wrong role: 403. Operator-only endpoint, wrong or missing key: 401."""
    QUIET = {"cameraId": "cam-lobby", "frameWidth": 640, "frameHeight": 480, "detections": []}
    READING = {"value": 21.0}
    TELEMETRY = {"readings": [{"sensorId": "temp-b1", "value": 22.0}]}
    # (method, path, body, AI service, sensor gateway)
    MATRIX = [
        ("get", "/api/cameras", None, 200, 403),  # the camera list holds the stream credentials
        ("get", "/api/cameras/cam-lobby", None, 200, 403),
        ("post", "/api/detections", QUIET, 202, 403),
        ("get", "/api/iot/sensors", None, 403, 200),
        ("get", "/api/iot/sensors/temp-a1", None, 403, 200),
        ("post", "/api/iot/sensors/temp-a1/readings", READING, 403, 200),
        ("post", "/api/iot/telemetry", TELEMETRY, 403, 200),
        ("get", "/api/iot/sprinklers", None, 403, 200),  # read-only relay state (the simulator puts its fire out with it)
        ("get", "/api/iot/sensors/temp-a1/history", None, 401, 401),
        ("post", "/api/iot/sprinklers/spr-a1/toggle", {"isActive": True}, 401, 401),
        ("post", "/api/iot/sprinklers/master", {"isActive": True}, 401, 401),
        ("get", "/api/incidents", None, 401, 401),
        ("post", "/api/incidents/dispatch", {"zone": "Ground Floor - Lobby"}, 401, 401),
        ("put", "/api/cameras/cam-lobby/calibration", {}, 401, 401),
    ]

    def call(self, client, method, path, body):
        return getattr(client, method)(path, body, format="json") if body is not None else getattr(client, method)(path)

    def test_each_device_key_reaches_only_its_own_endpoints(self):
        clients = {"AI service": (self.device, 3), "sensor gateway": (self.sensor, 4),
                   "wrong key": (APIClient(HTTP_X_DEVICE_KEY="not-a-key"), None), "no key": (APIClient(), None)}
        for row in self.MATRIX:
            method, path, body = row[:3]
            for name, (client, column) in clients.items():
                with self.subTest(path=path, method=method, caller=name):
                    self.assertEqual(self.call(client, method, path, body).status_code, row[column] if column else 401)
        self.assertEqual(SmartSprinkler.objects.filter(is_active=True).count(), 0)  # no device call switched a relay

    def test_operators_keep_their_access(self):
        self.assertEqual(self.op.post("/api/iot/sensors/temp-a1/readings", self.READING, format="json").status_code, 200)
        self.assertEqual(self.op.get("/api/iot/sensors/temp-a1/history").status_code, 200)
        self.assertEqual(self.op.get("/api/cameras").status_code, 200)
        self.assertTrue(self.op.post("/api/iot/sprinklers/spr-a1/toggle", {"isActive": True}, format="json").json()["isActive"])
        self.assertEqual(self.op.post("/api/iot/sprinklers/master", {"isActive": False}, format="json").status_code, 200)

    def test_a_role_without_a_configured_key_is_disabled_and_never_falls_back_to_the_other_key(self):
        with mock.patch.dict(settings.SENTINEL, SENSOR_API_KEY=""):
            self.assertEqual(self.sensor.post("/api/iot/telemetry", self.TELEMETRY, format="json").status_code, 401)
            self.assertEqual(self.device.post("/api/iot/telemetry", self.TELEMETRY, format="json").status_code, 403)
            self.assertEqual(self.device.post("/api/detections", self.QUIET, format="json").status_code, 202)  # AI unaffected
        with mock.patch.dict(settings.SENTINEL, DEVICE_API_KEY=""):
            self.assertEqual(self.device.post("/api/detections", self.QUIET, format="json").status_code, 401)
            self.assertEqual(self.sensor.post("/api/detections", self.QUIET, format="json").status_code, 403)
            self.assertEqual(self.sensor.post("/api/iot/telemetry", self.TELEMETRY, format="json").status_code, 200)

    def test_the_same_key_for_both_roles_stops_the_server_without_printing_it(self):
        key = "one-key-for-everything-0123456789"
        env = {**os.environ, "SENTINEL_NO_DOTENV": "1", "Jwt__SigningKey": "test-only-signing-key-0123456789abcdef",
               "Device__ApiKey": key, "Device__SensorApiKey": key}
        r = subprocess.run([sys.executable, "-c", "import config.settings"], cwd=settings.BASE_DIR, env=env,
                           capture_output=True, text=True, timeout=60)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("must be different keys", r.stderr)
        self.assertNotIn(key, r.stderr + r.stdout)
        self.assertNotEqual(settings.SENTINEL["DEVICE_API_KEY"], SENSOR_KEY)  # and the test keys themselves differ

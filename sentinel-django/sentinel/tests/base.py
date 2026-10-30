"""Shared test setup: the real first-run seeder (admin + demo devices), an operator, and API clients.
Port of SentinelApiFactory.cs. Runs on the throwaway database built by sentinel.test_runner."""
from unittest import mock

from django.conf import settings
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from sentinel import notifications, services
from sentinel.models import AppUser, UserRole
from sentinel.watchdog import watchdog

DEVICE_KEY, SENSOR_KEY = "test-device-key", "test-sensor-key"  # AI service, sensor gateway (set in manage.py)
ADMIN_EMAIL, PASSWORD = "admin@test.local", "Test@12345"


class SentinelTestCase(TestCase):
    """Demo data as seeded in Development: cameras cam-lobby, cam-warehouse, cam-server, drone-01; sensors smoke/temp-a1, b1, c1;
    sprinklers spr-a1, spr-a2, spr-b1, spr-b2, spr-c1. In-memory state (cache, watchdog, notification queue) is reset per test."""

    @classmethod
    def setUpTestData(cls):
        with mock.patch.dict(settings.SENTINEL, SEED_DEMO_DATA=True, SEED_ADMIN_EMAIL=ADMIN_EMAIL, SEED_ADMIN_PASSWORD=PASSWORD):
            services.seed()
        cls.admin = AppUser.objects.get(email=ADMIN_EMAIL)
        cls.operator = AppUser(email="operator@test.local", display_name="Operator", role=UserRole.OPERATOR)
        cls.operator.set_password(PASSWORD)
        cls.operator.save(force_insert=True)

    def setUp(self):
        cache.clear()
        watchdog.__init__()  # a fresh process: nothing remembered
        while not notifications.jobs.empty():
            notifications.jobs.get_nowait()

    @staticmethod
    def as_user(user) -> APIClient:
        client = APIClient()
        client.force_authenticate(user)
        return client

    @property
    def admin_client(self) -> APIClient:
        return self.as_user(self.admin)

    @property
    def op(self) -> APIClient:
        return self.as_user(self.operator)

    @property
    def device(self) -> APIClient:
        """The AI service's key: camera list and detections."""
        return APIClient(HTTP_X_DEVICE_KEY=DEVICE_KEY)

    @property
    def sensor(self) -> APIClient:
        """A sensor gateway's key: sensor readings and telemetry."""
        return APIClient(HTTP_X_DEVICE_KEY=SENSOR_KEY)

    def post_frame(self, **payload):
        return self.device.post("/api/detections", payload, format="json")

    def incidents(self, **filters) -> list[dict]:
        query = "&".join(f"{k}={v}" for k, v in filters.items())
        return self.op.get(f"/api/incidents?{query}").json()


def fire(camera_id: str, severity: str = "Medium") -> dict:
    return {"cameraId": camera_id, "frameWidth": 1280, "frameHeight": 720, "fireSeverity": severity,
            "detections": [{"category": "Fire", "confidence": 0.9, "bbox": [100, 100, 200, 200]}]}

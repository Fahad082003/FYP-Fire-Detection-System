"""Work the server process does once at start: first-run data, the camera watchdog and the notification worker
(the .NET API's DbSeeder and hosted services). Not run by manage.py commands or tests."""
import logging

from django.conf import settings
from django.db import connection
from django.db.utils import Error as DatabaseError

from . import notifications, services, watchdog

log = logging.getLogger("sentinel")


def start_background() -> None:
    if settings.ENV_FILE:
        log.info("Loaded environment from %s", settings.ENV_FILE)
    if not settings.SENTINEL["DEVICE_API_KEY"]:
        log.warning("Device__ApiKey is not set: the AI service cannot post frames (AI device access is disabled)")
    if not settings.SENTINEL["SENSOR_API_KEY"]:
        log.warning("Device__SensorApiKey is not set: sensor gateways cannot post readings (sensor device access is disabled)")
    try:
        if "CameraFeeds" not in connection.introspection.table_names():
            log.error("The database has no Sentinel tables. Create them with: python manage.py create_schema")
        else:
            services.seed()
    except DatabaseError:
        log.exception("Database check failed. Is SQL Server running and ConnectionStrings__Sentinel correct?")
    finally:
        connection.close()  # this thread's connection must not be reused by request threads
    watchdog.start()
    notifications.start_worker()

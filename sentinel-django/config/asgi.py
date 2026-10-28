"""ASGI entry point: python -m uvicorn config.asgi:application --port 5291

ASGI (not WSGI) because the live event stream and the MJPEG relay are long-lived streaming responses.
Run exactly one server process: the camera watchdog, the notification worker and the event broadcast live in it.
"""
import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django_app = get_asgi_application()

from django.contrib.staticfiles.handlers import ASGIStaticFilesHandler  # noqa: E402 - needs the configured app

from sentinel.startup import start_background  # noqa: E402

start_background()
application = ASGIStaticFilesHandler(django_app)  # serves /static/ itself, so no web server or extra package is needed

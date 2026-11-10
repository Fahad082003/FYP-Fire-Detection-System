"""Server-side validation for the login page and cameras. CameraForm also validates the /api/cameras endpoints,
so the Devices page and API clients get exactly the same rules and messages."""
import re
from datetime import timedelta
from urllib.parse import urlsplit

from django import forms
from django.utils import timezone

from .models import CameraFeed, FeedType, SensorNode, SensorType, SmartSprinkler, SprinklerMode, StreamFormat
from .rules import mask_credentials

CAMERA_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
ZONE_NAME = re.compile(r"^\S.* - \S.*$")  # "Floor - Area", so an alert says where to go
ZONE_RULE = 'Zone must be "Floor - Area", e.g. "Ground Floor - Lobby", or a zone already in use (max 64 characters).'


def existing_zones() -> list[str]:
    """Every zone a camera, sensor or sprinkler already uses (older databases have names like "Zone A")."""
    return sorted({*CameraFeed.objects.values_list("location_zone", flat=True), *SensorNode.objects.values_list("location_zone", flat=True),
                   *SmartSprinkler.objects.values_list("zone", flat=True)})


def clean_zone(value: str) -> str:
    """A new zone must say where to go ("Floor - Area"); a zone already in use is always accepted, so devices can join it."""
    value = value.strip()
    if not value or len(value) > 64 or not (ZONE_NAME.match(value) or value in existing_zones()):
        raise forms.ValidationError(ZONE_RULE)
    return value


def clean_device_id(value: str, model, what: str) -> str:
    if not CAMERA_ID.match(value):
        raise forms.ValidationError(f"Id: 1–64 letters, digits, '-' or '_' (e.g. {what}-a2). It cannot change later.")
    if model.objects.filter(pk=value).exists():
        raise forms.ValidationError(f"A {what} with id '{value}' already exists.")
    return value


class SensorForm(forms.Form):
    """A smoke or temperature sensor: its gateway then posts readings with this id (Device__SensorApiKey)."""
    id = forms.CharField(max_length=200)
    type = forms.ChoiceField(choices=SensorType.choices, error_messages={"invalid_choice": "Unknown sensor type."})
    threshold = forms.FloatField(min_value=0.01, max_value=1_000_000, error_messages={
        "invalid": "Limit must be a number.", "min_value": "Limit must be above 0.", "required": "Limit is required."})
    location_zone = forms.CharField(max_length=1000)

    def clean_id(self):
        return clean_device_id(self.cleaned_data["id"], SensorNode, "sensor")

    def clean_location_zone(self):
        return clean_zone(self.cleaned_data["location_zone"])


class SprinklerForm(forms.Form):
    """A sprinkler relay of a zone: Auto opens by itself in an emergency of that zone, Manual only by a person."""
    id = forms.CharField(max_length=200)
    zone = forms.CharField(max_length=1000)
    mode = forms.ChoiceField(choices=SprinklerMode.choices, error_messages={"invalid_choice": "Unknown mode."})

    def clean_id(self):
        return clean_device_id(self.cleaned_data["id"], SmartSprinkler, "sprinkler")

    def clean_zone(self):
        return clean_zone(self.cleaned_data["zone"])


class LoginForm(forms.Form):
    email = forms.EmailField(max_length=256, error_messages={"required": "Enter a valid email address.",
                                                             "invalid": "Enter a valid email address."})
    password = forms.CharField(min_length=6, max_length=128, strip=False, widget=forms.PasswordInput,
                               error_messages={"required": "Password must be at least 6 characters.",
                                               "min_length": "Password must be at least 6 characters."})


class ReportForm(forms.Form):
    """Reports date range: UTC calendar days, both included. Missing values default to the last 7 days."""
    MAX_DAYS = 366
    start = forms.DateField(input_formats=["%Y-%m-%d"], error_messages={"invalid": "Start: use a date like 2026-10-01.",
                                                                       "required": "Start date is required."})
    end = forms.DateField(input_formats=["%Y-%m-%d"], error_messages={"invalid": "End: use a date like 2026-10-31.",
                                                                     "required": "End date is required."})

    def clean(self):
        data = super().clean()
        start, end = data.get("start"), data.get("end")
        if start and end:
            if start > end:
                raise forms.ValidationError("Start date must be on or before the end date.")
            if (end - start).days + 1 > self.MAX_DAYS:
                raise forms.ValidationError(f"Choose at most {self.MAX_DAYS} days.")
            if end > timezone.now().date() + timedelta(days=1):  # one day of slack: local "today" east of UTC
                raise forms.ValidationError("End date cannot be in the future.")
        return data


class CameraForm(forms.Form):
    id = forms.CharField(required=False, max_length=200)
    name = forms.CharField(required=False, max_length=1000)
    feed_type = forms.ChoiceField(choices=FeedType.choices, initial=FeedType.CCTV,
                                  error_messages={"invalid_choice": "Unknown camera type.", "required": "Unknown camera type."})
    location_zone = forms.CharField(required=False, max_length=1000)
    stream_url = forms.CharField(required=False, max_length=5000)
    stream_format = forms.ChoiceField(choices=StreamFormat.choices, initial=StreamFormat.SIMULATED,
                                      error_messages={"invalid_choice": "Unknown stream format.", "required": "Unknown stream format."})

    def __init__(self, *args, camera: CameraFeed | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.camera = camera  # None = adding a new camera

    def clean_id(self):
        if self.camera is not None:
            return self.camera.id  # the id cannot change: it is what the AI service posts as cameraId
        value = self.cleaned_data["id"]
        if not CAMERA_ID.match(value):
            raise forms.ValidationError("Id: 1–64 letters, digits, '-' or '_' (e.g. cam-gate). The AI service sends frames with this id.")
        if CameraFeed.objects.filter(pk=value).exists():
            raise forms.ValidationError(f"A camera with id '{value}' already exists.")
        return value

    def clean_name(self):
        value = self.cleaned_data["name"]
        if not 0 < len(value) <= 120:
            raise forms.ValidationError("Name is required (max 120 characters).")
        return value

    def clean_location_zone(self):
        return clean_zone(self.cleaned_data["location_zone"])

    def clean_stream_url(self):
        value = self.cleaned_data["stream_url"]
        # The dashboard only ever sees the masked link; sending it back unchanged keeps the saved credentials.
        if self.camera is not None and value == mask_credentials(self.camera.stream_url):
            return self.camera.stream_url
        parts = urlsplit(value) if value else None
        if len(value) > 500 or (parts and not (parts.scheme in ("rtsp", "rtsps", "http", "https") and parts.netloc)):
            raise forms.ValidationError("Stream link must be an rtsp://, http:// or https:// address (max 500), or empty for a simulated feed.")
        return value

"""Server-side validation for the login page and cameras. CameraForm also validates the /api/cameras endpoints,
so the Devices page and API clients get exactly the same rules and messages."""
import re
from urllib.parse import urlsplit

from django import forms

from .models import CameraFeed, FeedType, StreamFormat
from .rules import mask_credentials

CAMERA_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
ZONE_NAME = re.compile(r"^\S.* - \S.*$")  # "Floor - Area", so an alert says where to go


class LoginForm(forms.Form):
    email = forms.EmailField(max_length=256, error_messages={"required": "Enter a valid email address.",
                                                             "invalid": "Enter a valid email address."})
    password = forms.CharField(min_length=6, max_length=128, strip=False, widget=forms.PasswordInput,
                               error_messages={"required": "Password must be at least 6 characters.",
                                               "min_length": "Password must be at least 6 characters."})


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
        value = self.cleaned_data["location_zone"]
        if len(value) > 64 or not ZONE_NAME.match(value):
            raise forms.ValidationError('Zone must be "Floor - Area", e.g. "Ground Floor - Lobby" (max 64 characters).')
        return value

    def clean_stream_url(self):
        value = self.cleaned_data["stream_url"]
        # The dashboard only ever sees the masked link; sending it back unchanged keeps the saved credentials.
        if self.camera is not None and value == mask_credentials(self.camera.stream_url):
            return self.camera.stream_url
        parts = urlsplit(value) if value else None
        if len(value) > 500 or (parts and not (parts.scheme in ("rtsp", "rtsps", "http", "https") and parts.netloc)):
            raise forms.ValidationError("Stream link must be an rtsp://, http:// or https:// address (max 500), or empty for a simulated feed.")
        return value

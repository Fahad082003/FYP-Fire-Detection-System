"""Client for the sentinel-ai inference service in ai-service/ (port of AiInferenceClient.cs).

It never fabricates results: an unconfigured, offline, slow or malformed service becomes an explicit error, never an empty
"all clear". Live camera frames do not pass through here; the AI service posts them to /api/detections itself.
"""
import json
import urllib.error
import urllib.request

from django.conf import settings

from . import validation

STATUS_TIMEOUT_SECONDS = 3  # a health check must stay fast even while inference is busy


class AiError(Exception):
    """An HTTP status, a title and a message for the operator (503 unavailable, 504 timeout, 502 bad answer)."""

    def __init__(self, status: int, title: str, detail: str):
        super().__init__(detail)
        self.status, self.title, self.detail = status, title, detail


class ImageRejected(Exception):
    """The image itself is unacceptable (400)."""


def _url(path: str) -> str:
    return settings.SENTINEL["AI_BASE_URL"].rstrip("/") + "/" + path


def status() -> dict:
    base = settings.SENTINEL["AI_BASE_URL"]
    if not base.strip():
        return {"configured": False, "online": False, "error": "AiService:BaseUrl is not set.", "service": None}
    try:
        with urllib.request.urlopen(_url("health"), timeout=STATUS_TIMEOUT_SECONDS) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        return {"configured": True, "online": False, "error": f"Health check returned HTTP {e.code}.", "service": None}
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return {"configured": True, "online": False, "error": f"AI service unreachable at {base}.", "service": None}
    try:
        return {"configured": True, "online": True, "error": None, "service": json.loads(raw)}
    except ValueError:
        return {"configured": True, "online": False, "error": "Health check returned invalid JSON.", "service": None}


def image_type(head: bytes) -> str | None:
    """'image/png' or 'image/jpeg' from the file's magic bytes; never trust the name or the browser's content type."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    return None


def read_upload(request, max_bytes: int) -> bytes | None:
    """The multipart field "file", refusing oversized bodies before Django spools them to disk (the .NET RequestSizeLimit)."""
    if int(request.META.get("CONTENT_LENGTH") or 0) > max_bytes + 1024 * 1024:  # 1 MB for the multipart wrapping
        raise ImageRejected(f"Image is larger than {max_bytes // (1024 * 1024)} MB.")
    upload = request.FILES.get("file")
    return upload.read(max_bytes + 1) if upload else None


def check_image(data: bytes | None, max_bytes: int) -> str:
    """Content type of an uploaded PNG/JPG, or ImageRejected with the message the operator sees."""
    if not data:
        raise ImageRejected("Choose a PNG or JPG image.")
    if len(data) > max_bytes:
        raise ImageRejected(f"Image is larger than {max_bytes // (1024 * 1024)} MB.")
    content_type = image_type(data[:8])
    if content_type is None:
        raise ImageRejected("Only PNG or JPG images are allowed.")
    return content_type


def analyze(data: bytes | None) -> dict:
    """Runs the real models on one operator-uploaded PNG/JPG. Preview only: no incident is ever opened."""
    o = settings.SENTINEL
    content_type = check_image(data, o["AI_MAX_IMAGE_BYTES"])
    if not o["AI_BASE_URL"].strip():
        raise AiError(503, "AI service not configured", "Set AiService__BaseUrl to the sentinel-ai service URL.")
    request = urllib.request.Request(_url("infer?cameraId=upload-test"), data=data, method="POST",
                                     headers={"Content-Type": content_type})
    try:
        with urllib.request.urlopen(request, timeout=o["AI_TIMEOUT_SECONDS"]) as r:
            body = r.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        text = e.read(300).decode(errors="replace")
        if e.code == 400:
            raise ImageRejected(f"AI service rejected the image: {text}")
        if e.code == 503:
            raise AiError(503, "AI model not available", text)
        raise AiError(502, "AI service error", f"HTTP {e.code}: {text}")
    except (TimeoutError, urllib.error.URLError, OSError) as e:
        if isinstance(e, TimeoutError) or isinstance(getattr(e, "reason", None), TimeoutError):
            raise AiError(504, "AI service timeout", f"No inference result within {o['AI_TIMEOUT_SECONDS']:g} s.")
        raise AiError(503, "AI service unavailable",
                      f"AI service unreachable at {o['AI_BASE_URL']}. Start ai-service (see ai-service/README.md).")

    try:
        result = json.loads(body)
        payload = result["payload"]
        inference_ms = float(result.get("inferenceMs", 0))
    except (ValueError, KeyError, TypeError):
        raise AiError(502, "Malformed AI result", "AI service returned a result without a detection payload.")
    if not isinstance(payload, dict):
        raise AiError(502, "Malformed AI result", "AI service returned a result without a detection payload.")
    payload["cameraId"] = "upload-test"
    try:
        return {"frame": validation.validate(payload), "inferenceMs": round(inference_ms, 1)}
    except validation.PayloadError as e:
        problems = "; ".join(f"{k}: {', '.join(v)}" for k, v in e.errors.items())
        raise AiError(502, "Malformed AI result", f"AI service result failed validation: {problems}")

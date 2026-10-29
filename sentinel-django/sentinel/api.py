"""REST API with the same paths, JSON shapes and status codes as the ASP.NET Core API it replaces.

Request serializers use the JSON wire names (camelCase) as field names, so validated_data matches the contract directly.
"""
import logging

from django.conf import settings
from django.contrib.auth import authenticate
from django.http import FileResponse, Http404
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import exceptions, serializers, status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import SimpleRateThrottle
from rest_framework.views import exception_handler

from . import ai_client, alerts, dto, services, sprinklers
from .forms import CameraForm
from .models import (CameraFeed, FloorPlan, IncidentAlert, IncidentLogType, SensorNode, SmartSprinkler, SprinklerMode,
                     WebPushSubscription)
from .security import IsAdmin, IsOperatorOrDevice, actor, is_device, issue_token
from .validation import PayloadError

log = logging.getLogger("sentinel.api")
SEVERITY_CHOICES = ["Low", "Medium", "High", "Critical"]


# ---------------------------------------------------------------- errors -> ProblemDetails-like JSON
def _flatten(detail, prefix="") -> dict[str, list[str]]:
    if isinstance(detail, dict):
        out = {}
        for key, value in detail.items():
            out.update(_flatten(value, f"{prefix}.{key}" if prefix else str(key)))
        return out
    if isinstance(detail, list) and detail and all(isinstance(d, (dict, list)) for d in detail):
        out = {}
        for i, value in enumerate(detail):
            out.update(_flatten(value, f"{prefix}[{i}]"))
        return out
    items = detail if isinstance(detail, list) else [detail]
    return {prefix or "body": [str(d) for d in items if str(d)]} if any(str(d) for d in items) else {}


def _problem(code: int, title: str, detail: str | None = None, errors: dict | None = None) -> Response:
    body = {"status": code, "title": title}
    if detail:
        body["detail"] = detail
    if errors is not None:
        body["errors"] = errors
    return Response(body, status=code, content_type="application/problem+json")


def problem_details(exc, context):
    if isinstance(exc, (services.Invalid, PayloadError)):
        return _problem(400, "One or more validation errors occurred.", errors=exc.errors)
    if isinstance(exc, ai_client.ImageRejected):
        return _problem(400, "One or more validation errors occurred.", errors={"file": [str(exc)]})
    if isinstance(exc, ai_client.AiError):
        log.warning("%s: %s", exc.title, exc.detail)  # expected operational state (e.g. AI service down): no stack trace
        return _problem(exc.status, exc.title, exc.detail)
    if isinstance(exc, Http404):
        return _problem(404, "Not found", str(exc) or "Not found.")
    if isinstance(exc, exceptions.ValidationError):
        return _problem(400, "One or more validation errors occurred.", errors=_flatten(exc.detail))
    response = exception_handler(exc, context)
    if response is not None:
        titles = {401: "Unauthorized", 403: "Forbidden", 404: "Not found", 405: "Method not allowed", 429: "Too many requests"}
        detail = response.data.get("detail") if isinstance(response.data, dict) else None
        problem = _problem(response.status_code, titles.get(response.status_code, "Bad request"), str(detail) if detail else None)
        for header in ("WWW-Authenticate", "Retry-After"):
            if header in response:
                problem[header] = response[header]
        return problem
    log.exception("Unhandled exception for %s", context["request"].path)
    return _problem(500, "An unexpected error occurred.")


# ---------------------------------------------------------------- request bodies
class LoginBody(serializers.Serializer):
    email = serializers.EmailField(max_length=256)
    password = serializers.CharField(min_length=6, max_length=128, trim_whitespace=False)


class DispatchBody(serializers.Serializer):
    zone = serializers.CharField(max_length=64)
    message = serializers.CharField(max_length=500, required=False, allow_blank=True, allow_null=True, trim_whitespace=False)
    severity = serializers.ChoiceField(SEVERITY_CHOICES, default="Critical")
    activateSprinklers = serializers.BooleanField(default=False)


class ReadingBody(serializers.Serializer):
    value = serializers.FloatField()
    recordedAt = serializers.DateTimeField(required=False, allow_null=True)


class TelemetryItem(serializers.Serializer):
    sensorId = serializers.CharField(max_length=64)
    value = serializers.FloatField()


class TelemetryBody(serializers.Serializer):
    readings = TelemetryItem(many=True, min_length=1, max_length=500)
    simulated = serializers.BooleanField(default=False)


class ToggleBody(serializers.Serializer):
    isActive = serializers.BooleanField(required=False, allow_null=True, default=None)
    mode = serializers.ChoiceField(SprinklerMode.values, required=False, allow_null=True, default=None)


class MasterBody(serializers.Serializer):
    isActive = serializers.BooleanField()
    zone = serializers.CharField(max_length=64, required=False, allow_blank=True, allow_null=True)


class ElementBody(serializers.Serializer):
    type = serializers.CharField(max_length=16)
    name = serializers.CharField(max_length=80, required=False, allow_blank=True, default="", trim_whitespace=False)
    x = serializers.FloatField()
    y = serializers.FloatField()
    width = serializers.FloatField()
    height = serializers.FloatField()
    isBlocked = serializers.BooleanField(default=False)
    isActive = serializers.BooleanField(default=False)
    zone = serializers.CharField(max_length=64, required=False, allow_blank=True, allow_null=True)


class FloorPlanBody(serializers.Serializer):
    name = serializers.CharField(max_length=120)
    building = serializers.CharField(max_length=120, required=False, allow_blank=True, allow_null=True)
    floorNumber = serializers.IntegerField(min_value=-10, max_value=200, default=0)
    widthMeters = serializers.FloatField(min_value=1, max_value=1000)
    heightMeters = serializers.FloatField(min_value=1, max_value=1000)
    elements = ElementBody(many=True, max_length=2000, required=False, default=list)


class PushKeys(serializers.Serializer):
    p256dh = serializers.CharField(max_length=255)
    auth = serializers.CharField(max_length=255)


class PushBody(serializers.Serializer):
    endpoint = serializers.URLField(max_length=500)
    keys = PushKeys()


def body(serializer_class, request) -> dict:
    s = serializer_class(data=request.data)
    s.is_valid(raise_exception=True)
    return s.validated_data


def _int(value, name):
    try:
        return int(value)
    except (TypeError, ValueError):
        raise services.invalid(name, "Must be a whole number.")


# ---------------------------------------------------------------- auth
class WindowThrottle(SimpleRateThrottle):
    """Rate "N/S" = at most N requests per S seconds (the .NET config used arbitrary window seconds)."""

    def parse_rate(self, rate):
        limit, seconds = rate.split("/")
        return int(limit), int(seconds)


class LoginThrottle(WindowThrottle):
    scope = "login"  # per client IP

    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}


class AnalyzeThrottle(WindowThrottle):
    """Per user: test images queue on the single-threaded AI service. Live camera frames are never limited."""
    scope = "ai-analyze"

    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": request.user.pk}


@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([LoginThrottle])
def login(request):
    """For scripts (e.g. ai-service/tools/live_test.py): returns a bearer token. The dashboard signs in on /login."""
    data = body(LoginBody, request)
    user = authenticate(request, username=data["email"], password=data["password"])
    if user is None:  # same message for "no such user" and "wrong password"
        raise exceptions.AuthenticationFailed("Invalid email or password.")
    user.last_login = timezone.now()
    user.save(update_fields=["last_login"])
    token, expires = issue_token(user)
    return Response({"token": token, "expiresAt": expires, "user": dto.user(user)})


@api_view(["GET"])
def me(request):
    return Response(dto.user(request.user))


# ---------------------------------------------------------------- cameras
def _camera_form(request, camera=None) -> CameraForm:
    d = request.data if isinstance(request.data, dict) else {}
    form = CameraForm({"id": d.get("id") or "", "name": d.get("name") or "", "feed_type": d.get("feedType") or "CCTV",
                       "location_zone": d.get("locationZone") or "", "stream_url": d.get("streamUrl") or "",
                       "stream_format": d.get("streamFormat") or "Simulated"}, camera=camera)
    if not form.is_valid():
        names = {"feed_type": "feedType", "location_zone": "locationZone", "stream_url": "streamUrl", "stream_format": "streamFormat"}
        raise services.Invalid({names.get(k, k): list(v) for k, v in form.errors.items()})
    return form


@api_view(["GET", "POST"])
@permission_classes([IsOperatorOrDevice])
def cameras(request):
    if request.method == "GET":  # only devices (the AI service) get the real stream link; people see credentials masked
        return Response([dto.camera(c, reveal_credentials=is_device(request.user)) for c in services.cameras()])
    if not IsAdmin().has_permission(request, None):
        raise exceptions.PermissionDenied()
    return Response(dto.camera(services.save_camera(_camera_form(request))), status=status.HTTP_201_CREATED)


@api_view(["GET", "PUT", "DELETE"])
@permission_classes([IsOperatorOrDevice])
def camera(request, camera_id):
    cam = services.get_or_404(CameraFeed, f"Camera '{camera_id}' not found.", pk=camera_id)
    if request.method == "GET":
        return Response(dto.camera(cam, reveal_credentials=is_device(request.user)))
    if not IsAdmin().has_permission(request, None):
        raise exceptions.PermissionDenied()
    if request.method == "PUT":
        return Response(dto.camera(services.save_camera(_camera_form(request, cam), cam)))
    services.delete_camera(cam)
    return Response(status=status.HTTP_204_NO_CONTENT)


@api_view(["PUT", "DELETE"])
def calibration(request, camera_id):
    """Links the camera to a floor plan with 4 point pairs, so the dashboard can place detected people on the plan."""
    cam = services.get_or_404(CameraFeed, f"Camera '{camera_id}' not found.", pk=camera_id)
    value = request.data if request.method == "PUT" else None
    if request.method == "PUT" and not isinstance(value, dict):
        raise services.invalid("calibration", "A calibration object is required.")
    return Response(dto.camera(services.save_calibration(cam, value)))


# ---------------------------------------------------------------- dashboard, detections
@api_view(["GET"])
def snapshot(request):
    return Response(services.snapshot())


@api_view(["POST"])
@permission_classes([IsOperatorOrDevice])
def detections(request):
    """One AI inference frame. 202 = accepted; 400 = invalid; 404 = the camera was deleted (the AI service stops it)."""
    frame, incident_id = services.process_detection(request.data)
    return Response({"accepted": True, "cameraId": frame["cameraId"], "fireDetected": frame["fireDetected"],
                     "humanCount": frame["humanCount"], "dangerCount": frame["dangerCount"], "incidentId": incident_id},
                    status=status.HTTP_202_ACCEPTED)


# ---------------------------------------------------------------- incidents
def _severity_param(value):
    if value is None:
        return None
    match = next((s for s in SEVERITY_CHOICES if s.lower() == value.lower()), None)
    if match is None:
        raise services.invalid("minSeverity", "Unknown severity.")
    return match


@api_view(["GET"])
def incidents(request):
    q = request.query_params
    resolved = {"true": True, "false": False}.get(q.get("resolved", "").lower()) if "resolved" in q else None
    return Response(services.list_incidents(resolved, _severity_param(q.get("minSeverity")), _int(q.get("limit", 50), "limit")))


@api_view(["GET"])
def incident_logs(request):
    q = request.query_params
    log_type = q.get("type")
    if log_type is not None and log_type not in IncidentLogType.values:
        raise services.invalid("type", "Unknown log type.")
    incident_id = _int(q["incidentId"], "incidentId") if q.get("incidentId") else None
    return Response(services.list_logs(incident_id, log_type, _int(q.get("limit", 200), "limit")))


def _incident(incident_id) -> IncidentAlert:
    return services.get_or_404(services.incident_query(), f"Incident #{incident_id} not found.", pk=incident_id)


@api_view(["GET"])
def incident(request, incident_id):
    found = _incident(incident_id)
    return Response({**dto.incident(found), "fireTrend": alerts.fire_trend(found)})  # read-only: never creates a warning


@api_view(["POST"])
def resolve(request, incident_id):
    return Response(dto.incident(services.resolve_incident(_incident(incident_id), actor(request))))


@api_view(["POST"])
def false_alarm(request, incident_id):
    return Response(dto.incident(services.mark_false_alarm(_incident(incident_id), actor(request))))


@api_view(["POST"])
def dispatch(request):
    d = body(DispatchBody, request)
    created = services.dispatch(d["zone"], d.get("message"), d["severity"], d["activateSprinklers"], actor(request))
    return Response(dto.incident(_incident(created.id)), status=status.HTTP_201_CREATED)


# ---------------------------------------------------------------- IoT: sensors and sprinklers
@api_view(["GET"])
@permission_classes([IsOperatorOrDevice])
def sensors(request):
    return Response([dto.sensor(s) for s in services.sensors()])


@api_view(["GET"])
@permission_classes([IsOperatorOrDevice])
def sensor(request, sensor_id):
    return Response(dto.sensor(services.get_or_404(SensorNode, f"Sensor '{sensor_id}' not found.", pk=sensor_id)))


@api_view(["GET"])
@permission_classes([IsOperatorOrDevice])
def sensor_history(request, sensor_id):
    node = services.get_or_404(SensorNode, f"Sensor '{sensor_id}' not found.", pk=sensor_id)
    since = request.query_params.get("since")
    parsed = parse_datetime(since) if since else None
    if since and parsed is None:
        raise services.invalid("since", "Must be an ISO 8601 date and time.")
    if parsed is not None and timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.utc)
    return Response(services.sensor_history(node, _int(request.query_params.get("limit", 120), "limit"), parsed))


@api_view(["POST"])
@permission_classes([IsOperatorOrDevice])
def sensor_reading(request, sensor_id):
    d = body(ReadingBody, request)
    return Response(services.ingest_reading(sensor_id, d["value"], d.get("recordedAt")))


@api_view(["POST"])
@permission_classes([IsOperatorOrDevice])
def telemetry(request):
    """Gateway batch: many sensors in one request. Unknown sensors are reported, not fatal."""
    d = body(TelemetryBody, request)
    result = services.ingest_bulk(d["readings"], d["simulated"])
    return Response(result, status=400 if result["errors"] and not result["updated"] else 200)


@api_view(["GET"])
@permission_classes([IsOperatorOrDevice])
def sprinkler_list(request):
    return Response([dto.sprinkler(s) for s in SmartSprinkler.objects.order_by("zone", "id")])


@api_view(["POST"])
@permission_classes([IsOperatorOrDevice])
def sprinkler_toggle(request, sprinkler_id):
    """{"isActive": true|false, "mode": "Auto"|"Manual"}, both optional; an empty body flips the relay."""
    relay = services.get_or_404(SmartSprinkler, f"Sprinkler '{sprinkler_id}' not found.", pk=sprinkler_id)
    d = body(ToggleBody, request) if request.data else {"isActive": None, "mode": None}
    sprinklers.toggle(relay, d["isActive"], d["mode"], actor(request))
    return Response(dto.sprinkler(relay))


@api_view(["POST"])
def sprinkler_master(request):
    """System-wide (or per-zone) sprinkler switch. Operators only."""
    d = body(MasterBody, request)
    changed = sprinklers.set_all(d["isActive"], d.get("zone"), actor(request), "master switch")
    return Response({"changed": changed, "sprinklers": [dto.sprinkler(s) for s in SmartSprinkler.objects.order_by("zone", "id")]})


# ---------------------------------------------------------------- floor plans
def _plan(plan_id) -> FloorPlan:
    return services.get_or_404(FloorPlan.objects.prefetch_related("elements"), f"Floor plan #{plan_id} not found.", pk=plan_id)


@api_view(["GET", "POST"])
def floor_plans(request):
    if request.method == "GET":
        return Response([dto.floor_plan(p) for p in services.floor_plans()])
    plan = services.save_floor_plan(None, body(FloorPlanBody, request))
    return Response(dto.floor_plan(_plan(plan.id)), status=status.HTTP_201_CREATED)


@api_view(["GET", "PUT", "DELETE"])
def floor_plan(request, plan_id):
    plan = _plan(plan_id)
    if request.method == "GET":
        return Response(dto.floor_plan(plan))
    if request.method == "PUT":  # replaces name, size and all elements
        services.save_floor_plan(plan, body(FloorPlanBody, request))
        return Response(dto.floor_plan(_plan(plan_id)))
    services.delete_floor_plan(plan)
    return Response(status=status.HTTP_204_NO_CONTENT)


@api_view(["GET", "PUT"])
def floor_plan_image(request, plan_id):
    """PUT: multipart field "file", PNG/JPG up to 5 MB. Only a background: nothing is detected from the image."""
    plan = _plan(plan_id)
    if request.method == "PUT":
        data = ai_client.read_upload(request, settings.SENTINEL["FLOORPLAN_MAX_IMAGE_BYTES"])
        content_type = ai_client.check_image(data, settings.SENTINEL["FLOORPLAN_MAX_IMAGE_BYTES"])
        return Response(dto.floor_plan(services.save_floor_plan_image(plan, data, content_type)))
    path = services.image_dir() / plan.image_file_name if plan.image_file_name else None
    if path is None or not path.is_file():
        raise Http404(f"Floor plan #{plan_id} has no image.")
    return FileResponse(path.open("rb"), content_type="image/png" if path.suffix == ".png" else "image/jpeg")


@api_view(["POST"])
def floor_plan_route(request, plan_id):
    d = request.data if isinstance(request.data, dict) else {}
    return Response(services.route(_plan(plan_id), d.get("x"), d.get("y")))


@api_view(["POST"])
def floor_plan_routes(request, plan_id):
    """Read-only per-exit comparison for one or more start points: {"starts": [[x, y], ...]}."""
    d = request.data if isinstance(request.data, dict) else {}
    return Response(services.routes(_plan(plan_id), d.get("starts")))


@api_view(["GET"])
def floor_plan_risk(request, plan_id):
    return Response(services.risk(_plan(plan_id)))


# ---------------------------------------------------------------- AI service
@api_view(["GET"])
def ai_status(request):
    return Response(ai_client.status())


@api_view(["POST"])
@throttle_classes([AnalyzeThrottle])
def ai_analyze(request):
    """Multipart field "file": PNG/JPG. 503 = service/model unavailable, 504 = timeout, 502 = malformed result."""
    return Response(ai_client.analyze(ai_client.read_upload(request, settings.SENTINEL["AI_MAX_IMAGE_BYTES"])))



# ---------------------------------------------------------------- web push
@api_view(["GET"])
def vapid_key(request):
    key = settings.SENTINEL["WEBPUSH_PUBLIC_KEY"]
    return Response({"publicKey": key or None})


@api_view(["POST"])
def push_subscribe(request):
    d = body(PushBody, request)
    WebPushSubscription.objects.update_or_create(endpoint=d["endpoint"], defaults={
        "p256dh": d["keys"]["p256dh"], "auth": d["keys"]["auth"], "user": request.user,
        "user_agent": request.headers.get("User-Agent", "")[:255], "is_active": True})
    return Response({"subscribed": True}, status=status.HTTP_201_CREATED)


@api_view(["POST"])
def push_unsubscribe(request):
    endpoint = request.data.get("endpoint") if isinstance(request.data, dict) else None
    if not endpoint:
        raise services.invalid("endpoint", "The endpoint is required.")
    WebPushSubscription.objects.filter(endpoint=endpoint).update(is_active=False)
    return Response(status=status.HTTP_204_NO_CONTENT)


@api_view(["GET"])
@permission_classes([AllowAny])
def health(request):
    return Response({"status": "ok", "time": timezone.now()})


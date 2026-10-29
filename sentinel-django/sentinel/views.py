"""Server-rendered pages, the live event stream (SSE) and the MJPEG camera relay."""
import asyncio
import base64
import json
import logging
import urllib.request
from datetime import timedelta
from urllib.parse import urlsplit

from asgiref.sync import sync_to_async
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login as auth_login, logout as auth_logout
from django.contrib.auth.decorators import login_required
from django.contrib.staticfiles import finders
from django.http import FileResponse, HttpResponse, HttpResponseForbidden, StreamingHttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST
from rest_framework.utils.encoders import JSONEncoder

from . import ai_client, events, services
from .api import AnalyzeThrottle, LoginThrottle
from .forms import CameraForm, LoginForm, ReportForm
from .models import CameraFeed, FeedType, SmartSprinkler, StreamFormat
from .rules import mask_credentials
from .security import is_operator

log = logging.getLogger("sentinel")
KEEPALIVE_SECONDS = 15


# ---------------------------------------------------------------- sign in / out
def login(request):
    if request.user.is_authenticated:
        return redirect("dashboard")
    form = LoginForm(request.POST or None)
    error = None
    if request.method == "POST":
        if not LoginThrottle().allow_request(request, None):
            error = "Too many sign-in attempts. Wait a minute and try again."
        elif form.is_valid():
            user = authenticate(request, username=form.cleaned_data["email"], password=form.cleaned_data["password"])
            if user is not None:
                auth_login(request, user)  # also stores LastLoginAt
                target = request.POST.get("next") or request.GET.get("next")
                safe = target and url_has_allowed_host_and_scheme(target, {request.get_host()}, request.is_secure())
                return redirect(target if safe else "dashboard")
            error = "Invalid email or password."  # same message for "no such user" and "wrong password"
    return render(request, "sentinel/login.html", {"form": form, "error": error, "next": request.GET.get("next", "")},
                  status=429 if error and error.startswith("Too many") else 200)


@require_POST
def logout(request):
    auth_logout(request)
    return redirect("login")


# ---------------------------------------------------------------- pages
@login_required
def dashboard(request):
    return render(request, "sentinel/dashboard.html", {"page": "dashboard"})


@login_required
def floor_plans(request):
    return render(request, "sentinel/floor_plans.html", {"page": "floor_plans"})


@login_required
def ai(request):
    """AI service status and a test-image check. Results come from the real models; nothing is shown when they are missing."""
    context = {"page": "ai", "max_mb": settings.SENTINEL["AI_MAX_IMAGE_BYTES"] // (1024 * 1024)}
    if request.method == "POST":
        try:
            data = ai_client.read_upload(request, settings.SENTINEL["AI_MAX_IMAGE_BYTES"])
            if not AnalyzeThrottle().allow_request(request, None):
                raise ai_client.AiError(429, "Too many requests", "Too many test images. Wait a minute and try again.")
            result = ai_client.analyze(data)
            content_type = ai_client.image_type(data[:8])
            context.update(result=result, image=f"data:{content_type};base64,{base64.b64encode(data).decode()}")
        except ai_client.ImageRejected as e:
            context["error"] = str(e)
        except ai_client.AiError as e:
            context["error"] = f"{e.title}: {e.detail}"
    context["status"] = ai_client.status()
    return render(request, "sentinel/ai.html", context)


@login_required
def reports(request):
    """Incident analytics for a date range (GET form, so a report is a shareable link). Read-only."""
    today = timezone.now().date()
    form = ReportForm({"start": str(today - timedelta(days=6)), "end": str(today), **request.GET.dict()})
    report = services.incident_analytics(form.cleaned_data["start"], form.cleaned_data["end"]) if form.is_valid() else None
    if report:  # bar sizes: breakdowns relative to the total, days relative to the busiest day
        report.update(busiest=max(d["count"] for d in report["daily"]),
                      false_alarm_percent=None if report["falseAlarmRate"] is None else round(report["falseAlarmRate"] * 100, 1))
    breakdowns = report and [("By severity", "Severity", report["bySeverity"]), ("By source", "Source", report["bySource"])]
    return render(request, "sentinel/reports.html", {"page": "reports", "form": form, "report": report, "breakdowns": breakdowns},
                  status=200 if report else 400)


@login_required
def devices(request):
    """Admin device setup: a customer's own cameras (name, zone, stream link) instead of the demo list."""
    camera = None
    if request.GET.get("edit"):
        camera = CameraFeed.objects.filter(pk=request.GET["edit"]).first()
    show_form = request.user.is_admin and ("add" in request.GET or camera is not None)
    form = None
    if request.method == "POST":
        if not request.user.is_admin:
            return HttpResponseForbidden("Only an Admin can add, edit or delete cameras.")
        if "delete" in request.POST:
            target = CameraFeed.objects.filter(pk=request.POST["delete"]).first()
            if target:
                services.delete_camera(target)
                messages.success(request, f"Camera deleted: {target.name}")
            return redirect("devices")
        form = CameraForm(request.POST, camera=camera)
        if form.is_valid():
            saved = services.save_camera(form, camera)
            messages.success(request, f"Camera saved: {saved.name} ({saved.location_zone})")
            return redirect("devices")
        show_form = True
    elif show_form:
        initial = {"feed_type": FeedType.CCTV, "stream_format": StreamFormat.SIMULATED}
        if camera:
            initial = {"id": camera.id, "name": camera.name, "feed_type": camera.feed_type, "location_zone": camera.location_zone,
                       "stream_url": mask_credentials(camera.stream_url), "stream_format": camera.stream_format}
        form = CameraForm(initial=initial, camera=camera)
    zones = sorted({*CameraFeed.objects.values_list("location_zone", flat=True),
                    *services.sensors().values_list("location_zone", flat=True),
                    *SmartSprinkler.objects.values_list("zone", flat=True)})
    return render(request, "sentinel/devices.html", {
        "page": "devices", "cameras": [(c, mask_credentials(c.stream_url)) for c in services.cameras()],
        "form": form if show_form else None, "editing": camera, "zones": zones,
        "feed_types": FeedType.choices, "formats": StreamFormat.choices})


def service_worker(request):
    """Web Push service worker. Served from the site root so its scope covers every page."""
    path = finders.find("sentinel/sw.js")
    response = FileResponse(open(path, "rb"), content_type="text/javascript")
    response["Cache-Control"] = "no-cache"
    return response


# ---------------------------------------------------------------- live events (replaces the SignalR hub for dashboards)
async def event_stream(request):
    """Server-Sent Events: first the full snapshot, then every broadcast. The browser's EventSource reconnects by itself."""
    user = await request.auser()
    if not is_operator(user):
        return HttpResponse(status=401)
    queue = events.subscribe()  # before the snapshot, so nothing that happens meanwhile is missed

    async def stream():
        try:
            snapshot = await sync_to_async(services.snapshot)()
            yield f"event: snapshot\ndata: {json.dumps(snapshot, cls=JSONEncoder, separators=(',', ':'))}\n\n"
            while events.is_subscribed(queue):
                try:
                    yield await asyncio.wait_for(queue.get(), KEEPALIVE_SECONDS)
                except TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            events.unsubscribe(queue)

    response = StreamingHttpResponse(stream(), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response


# ---------------------------------------------------------------- MJPEG relay
async def camera_stream(request, camera_id):
    """Same-origin MJPEG relay for CCTV/drone feeds, shown in <img>. The session cookie authenticates it."""
    user = await request.auser()
    if not is_operator(user):
        return HttpResponse(status=401)
    camera = await CameraFeed.objects.filter(pk=camera_id).afirst()
    if camera is None:
        return HttpResponse(f"Camera '{camera_id}' not found.", status=404)
    if camera.stream_format != StreamFormat.MJPEG or urlsplit(camera.stream_url).scheme not in ("http", "https"):
        return HttpResponse("Proxy supports http(s) MJPEG feeds only; play HLS/MP4 directly.", status=400)
    try:
        upstream = await asyncio.to_thread(urllib.request.urlopen, camera.stream_url, timeout=30)
    except OSError as e:
        log.warning("MJPEG upstream for %s failed: %s", camera_id, e)
        return HttpResponse(status=502)

    async def relay():
        try:
            while chunk := await asyncio.to_thread(upstream.read1, 65536):
                yield chunk
        finally:
            await asyncio.to_thread(upstream.close)

    response = StreamingHttpResponse(relay(), content_type=upstream.headers.get("Content-Type", "multipart/x-mixed-replace"))
    response["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response

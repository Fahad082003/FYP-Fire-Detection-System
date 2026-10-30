# Migration ka record: ASP.NET Core + Angular → Django

Purane stack ki har cheez aur woh ab kahan hai. Yeh **historical record** hai.

**Status (2026-10-26):** migration mukammal. Parity verify hone ke baad purane folders `backend/` (ASP.NET Core 8 API, `Sentinel.Simulator`, `Sentinel.Api.Tests`; 82 tracked files) aur `frontend/` (Angular 22; 78 tracked files) delete kar diye gaye. Woh git mein maujood hain: `main` ka aakhri commit (`HEAD`; deletion abhi commit nahi hui) aur branch `feature/dotnet-angular-rebuild`. Deletion se pehle ke checks aur aakhri test nateeje: [../FINAL_DJANGO_MIGRATION_AUDIT.md](../FINAL_DJANGO_MIGRATION_AUDIT.md).

Status ke matlab: **ported** = dobara bana aur tests se cover; **replaced** = wahi kaam doosre tareeqe se; **dropped** = nahi laya gaya (wajah saath likhi).

## Backend (`backend/Sentinel.Api`, ASP.NET Core 8 — delete ho chuka)

| .NET | Django | Status |
|---|---|---|
| `Program.cs` (DI, auth schemes, policies, rate limits, CORS, Swagger, SignalR, seeding) | `config/settings.py`, `config/asgi.py`, `sentinel/startup.py`, `sentinel/security.py` | ported; CORS aur Swagger dropped (ab ek hi origin; API browser nahi) |
| `Configuration/DotEnv.cs`, `Options.cs` | `settings.load_dotenv`, `settings.SENTINEL` (wahi keys aur defaults) | ported |
| `Data/SentinelDbContext.cs`, `Migrations/*` | `sentinel/models.py` (unmanaged), `schema/sentinel.sql` (EF migrations se bana) | ported; schema wahi |
| `Data/DbSeeder.cs` | server start par `services.seed()` | ported |
| `Models/*` (8 files) | `sentinel/models.py` | ported (wahi tables, columns, enum strings) |
| `DTOs/*`, `Mapping.cs` | `sentinel/dto.py` (wahi camelCase shapes), request bodies ke liye `api.py` mein DRF serializers | ported |
| `Validators/DetectionPayloadValidator.cs` | `sentinel/validation.py` | ported (audit fixes D-13, D-14 samet) |
| `Security/DeviceKeyAuthenticationHandler.cs`, `AuthPolicies.cs` | `security.DeviceKeyAuthentication`, `IsOperator`, `IsAdmin`, `operator_or(role)` | ported (sirf-logon wale endpoints par device key → 401, pehle ki tarah). 2026-10-30 (Phase 3 M1): ek shared key ki jagah do roles, `Device__ApiKey` (AI service) aur `Device__SensorApiKey` (sensor gateways); sprinkler toggle ab sirf operator |
| JWT bearer auth, `TokenService`, `AuthService` | Django sessions (browser) + signed bearer token (`POST /api/auth/login`, scripts ke liye) | replaced; ASP.NET Identity password hashes wahi |
| `Middleware/ExceptionHandlingMiddleware.cs` (ProblemDetails) | `api.problem_details` (DRF exception handler, `application/problem+json`) | ported |
| `Controllers/*` (10) | `sentinel/api.py` + `sentinel/urls.py`, wahi paths | ported (2026-10-26 ko har route aur method code se dobara milaya gaya) |
| `Services/AlertEngine.cs` | `sentinel/alerts.py` | ported |
| `Services/CameraWatchdog.cs` (hosted service) | `sentinel/watchdog.py` (ASGI app ka daemon thread) | ported (D-16, D-17 samet) |
| `Services/CameraService.cs`, `DetectionService.cs`, `SensorService.cs`, `IncidentService.cs`, `DashboardService.cs`, `FloorPlanService.cs` | `sentinel/services.py`, `sentinel/forms.py` (camera rules) | ported |
| `Services/SprinklerService.cs` | `sentinel/sprinklers.py` | ported |
| `Services/SensorRules.cs` | `sentinel/rules.py` | ported |
| `Services/EvacuationRouter.cs`, `FireRiskEstimator.cs` | `sentinel/geometry.py` | ported (wahi algorithm; speed ke liye har element rasterise) |
| `Services/AiInferenceClient.cs` | `sentinel/ai_client.py` | ported |
| `Services/Notifications/*` (dispatcher, worker, Twilio, MailKit, WebPush) | `sentinel/notifications.py` (urllib se Twilio REST, Django mail, optional pywebpush) | ported; web push ke liye `pywebpush` |
| `Services/RealtimeNotifier.cs`, `Hubs/EmergencyMonitoringHub.cs` (SignalR) | `sentinel/events.py` + `views.event_stream` (Server-Sent Events) | replaced: dashboards sirf receive karte thay; producers REST istemal karte hain |
| Hub methods `PublishDetection`, `PublishTelemetry` | `POST /api/detections`, `POST /api/iot/sensors/{id}/readings` | replaced (wahi validation) |
| `CamerasController.Stream` (MJPEG relay) | `views.camera_stream` (async) | ported |
| `generate-vapid-keys` CLI | — | dropped: koi bhi VAPID generator (maslan `npx web-push generate-vapid-keys`) |
| `backend/Sentinel.Simulator` | `tools/simulator.py` | ported (frames SignalR ki jagah REST se) |
| `backend/Sentinel.Api.Tests` (74 tests, SQLite in memory) | `sentinel/tests/*` (SQL Server, disposable database) | ported, neeche test map |
| `appsettings.json` defaults | `settings.SENTINEL` | ported; `Cors`, JWT `Issuer`/`Audience` aur ASP.NET logging levels dropped (zaroorat nahi) |

## Frontend (`frontend/`, Angular 22 — delete ho chuka)

| Angular | Django | Status |
|---|---|---|
| Routes `/login`, `/`, `/floor-plans`, `/ai`, `/devices`; `authGuard`, `guestGuard` | `sentinel/urls.py`, `@login_required`, signed-in users ka `/login` se redirect | ported |
| `AuthService` (localStorage mein JWT), interceptors | session cookie + CSRF; `core.js api()` signed-out users ko `/login` bhejta hai | replaced |
| `SignalRService` (live state) | `shell.js` (`EventSource` par `live` store) | replaced |
| `ShellComponent` (banner, AI poll, link state, clock, mute, push, dispatch) | `templates/sentinel/shell.html` + `shell.js` | ported (D-15 aur IL-9 rules samet) |
| `AlarmService`, `ToastService`, `ConfirmService`, dialogs | `shell.js`, `core.js`, `base.html` | ported |
| `PushNotificationService`, `public/sw.js` | `shell.js`, `static/sentinel/sw.js` (`/sw.js` par) | ported |
| Dashboard: video wall, video feed, overlay renderer | `dashboard.js` (Tile), `overlay.js` | ported (hls.js vendored, sirf HLS feeds ke liye load) |
| KPI strip, sensor panel, sparkline, sprinkler controls | `dashboard.js` | ported |
| Alert logs, incident history (growth chart), activity feed, dispatch dialog | `dashboard.js`, `shell.html` | ported |
| Evacuation panel, homography, floor-plan map | `dashboard.js`, `planmap.js` | ported |
| Floor-plan editor + camera calibration | `floor_plans.html` + `floorplans.js` | ported |
| AI analysis page | `templates/sentinel/ai.html` (server-rendered: status, upload, boxes) | ported |
| Devices page | `templates/sentinel/devices.html` + `forms.CameraForm` (server-rendered) | ported |
| Login page | `templates/sentinel/login.html` + `forms.LoginForm` | ported |
| Tailwind CSS 4 | `static/sentinel/css/app.css` (wahi tokens aur look, build nahi) | replaced |
| Vitest unit tests (32), Playwright e2e (9) | Django tests + `e2e/dashboard.test.js` (asli server par 10 browser scenarios) | replaced, neeche dekhein |

## Tests

| Purana suite | Naye tests |
|---|---|
| `AlertRulesTests`, `EvacuationRouterTests` | `tests/test_rules.py` (har case, aur bare plan ka speed check) |
| `ApiIntegrationTests`, `CameraLostAndSimulatedTests`, `CameraDeletionTests`, `CameraStatusTests`, `FloorPlanAndLogTests` | `tests/test_api.py` |
| `AiServiceTests`, `AiRateLimitTests` | `tests/test_ai.py` (fake handler ki jagah asli local HTTP stub) |
| SignalR hub tests | `tests/test_security.py` (live events) |
| — | `tests/test_security.py`: .NET password-hash compatibility, sign-in page, CSRF, roles, login rate limit, safe redirects |
| Angular unit tests (banner ordering, people count, homography, chart, drawing) | asli UI ke zariye browser mein (`e2e/dashboard.test.js`), aur server side API tests se |
| Playwright e2e (mocked API) | `e2e/dashboard.test.js` (asli server, asli database, stub AI) |
| Audit live scripts (`integ.py`, `wd.py`, `qa.py`) | `e2e/lifecycle.py` (asli AI service aur models, kill aur restart) |
| — (migration ke dauran naye) | D-18 ke 2 tests; camera delete/frame race ke 3 tests; watchdog `camera_lost`/delete race ka 1 test (sab `tests/test_api.py`) |

## Faisle

- **Ek Django app** (`sentinel`): ek hi juri hui domain, jaise ek hi .NET project tha.
- **Unmanaged models, SentinelDb mein Django ki koi table nahi**: signed-cookie sessions, permission tables ke baghair `auth`, aur ek router jo `test_*` ke ilawa `migrate` rokta hai. (Migration ke dauran is wajah se Django aur .NET API ek hi data par saath chal sakte thay.)
- **Django Channels ki jagah Server-Sent Events**: browser sirf events receive karta tha; SSE ko koi extra package nahi chahiye, khud reconnect hota hai aur session cookie istemal karta hai.
- **Jahan kuch live nahi, wahan server-rendered** (login, AI page, devices); jahan page live ya interactive hai wahan vanilla JS (dashboard, floor-plan editor).
- **Tailwind ki jagah plain CSS**: run time par Node build nahi.
- **Browser tests Node mein (`playwright-core`)**: machine par pehle se tha (npm cache), installed Edge chalata hai, browser download nahi. Python Playwright (~40 MB) ke liye approval chahiye hoti.

## Migration ke dauran theek hue masle (2026-10-26)

- **D-18** (.NET se aaya): smoke ke foran baad Low fire frame 2 s throttle mein gum ho jata tha. Ab har window ka pehla fire frame guzarta hai (`sentinel/alerts.py`, `evaluate_detection`).
- **Camera delete vs in-flight frame**: frame camera row ke baghair evaluate ho kar "Unassigned" incident (ya FK error) bana sakta tha. Ab `process_detection` aur `delete_camera` ek per-camera lock lete hain (`sentinel/services.py`).
- **Watchdog `camera_lost` vs delete**: lookup aur insert ke beech delete par `FK_IncidentAlerts_CameraFeeds_CameraId` IntegrityError. Ab `camera_lost` bhi wahi lock leta hai (`sentinel/alerts.py`).
- Lifecycle test ki do timing races (D-17 check 10 aur check 11b) `e2e/lifecycle.py` mein theek hui; assertions kamzor nahi ki gayin.

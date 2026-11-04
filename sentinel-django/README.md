# FireVision AI — Django server

Project ka wahid server. Ek Python server dashboard pages, REST API aur live event stream teeno deta hai.
2026-10-26 ko isne purane ASP.NET Core API (`backend/`) aur Angular dashboard (`frontend/`) ki jagah le li; woh dono delete ho chuke hain aur sirf git history mein hain ([MIGRATION.md](MIGRATION.md)).
AI service (`ai-service/`) nahi badli; woh is server se bilkul usi tarah baat karti hai jaise pehle .NET API se karti thi.

```
AI service / IoT gateway ──(X-Device-Key, REST)──► Django (uvicorn, :5291) ──► Django ORM ──► SQL Server (SentinelDb, schema wahi)
  ai-service/, tools/simulator.py                    │  alert engine → notification worker → SMS / GSM / WhatsApp / Telegram / Firebase / e-mail / web push
                                                     │  camera watchdog (camera lost, Online/Offline), occupancy + crowd alerts
                                                     ▼
                         Browser ◄── server-rendered pages + /api/events (Server-Sent Events) ── session cookie
```

| Layer | Technology |
|---|---|
| Pages | Django templates, plain CSS (`static/sentinel/css/app.css`), vanilla JavaScript modules (koi build step nahi, run time par Node nahi) |
| API | Django REST Framework; wahi paths aur JSON jo purane ASP.NET API ke thay |
| Live updates | Server-Sent Events: `/api/events` (purane SignalR hub ki jagah) |
| Database | SQL Server, `mssql-django` + ODBC Driver 18; models purani EF Core tables ko map karte hain |
| Server | `uvicorn` (ASGI), ek process |

## Folder ka naqsha

```
sentinel-django/
  manage.py, requirements.txt
  config/            settings (project ki .env parhta hai), urls, asgi
  sentinel/          ek hi Django app
    models.py        EF Core tables ki unmanaged mappings (naam, columns, enum strings wahi)
    security.py      ASP.NET Identity password hashes, device key, bearer tokens, roles
    validation.py    AI detection payload validator
    rules.py         severity, emergency aur sensor-state rules
    alerts.py        alert engine: incidents, escalation, logs, sprinkler aur notification triggers
    watchdog.py      camera-lost watchdog, Online/Offline status, start-up reconciliation
    notifications.py cooldown queue + background worker; Twilio SMS, GSM modem SMS, WhatsApp, Telegram, Firebase (FCM), SMTP e-mail, web push
    geometry.py      evacuation routing aur fire-risk heatmap
    occupancy.py     har kamre ke log (Live / Stale / Unknown), crowd limits (App_Data/capacities.json) aur Crowd incidents
    health.py        /api/system/health ke asli checks (Notifications page)
    services.py      cameras, detections, sensors, incidents, dashboard snapshot, floor plans, pehli dafa ka seed
    ai_client.py     AI service ka /health, /infer aur /config (sensitivity)
    api.py, urls.py  REST endpoints;  views.py pages, live events, MJPEG relay;  events.py broadcast
    templates/, static/   pages, CSS, JavaScript (shell.js, dashboard.js, floorplans.js, planmap.js, overlay.js, core.js)
    tests/           Django tests (SQL Server, disposable database)
  schema/sentinel.sql   database schema (purani EF Core migrations se bana)
  e2e/               browser tests (run.py) aur asli AI service ke saath lifecycle check (lifecycle.py)
  tools/simulator.py field simulator
  App_Data/          floorplans/ (plan images), capacities.json (crowd limits), snapshots/<id>.jpg (incident ki camera tasveer, sirf MJPEG)
```

Ek hi app jaan boojh kar: incidents, cameras, sensors, sprinklers aur floor plans ek hi juri hui domain hain (alert engine sab ko chhoota hai). Modules alag karte hain; alag apps sirf cross-imports barhatin.

## Pages

Dark UI ("Fire Safety Command Center"). Har page par upar header bar: page ka naam, Live/Connecting, ghari, status chip (maslan "ELEVATED RISK · 2 OPEN"), laal **Emergency dispatch** button, **Enable Alerts**, alarm awaz mute, user aur Logout. Phone par menu ☰ button se drawer mein khulta hai; tablet layout target nahi.

| Group | Page | Path | Kya hai |
|---|---|---|---|
| MONITORING | Overview (pehle Dashboard) | `/` | building status banner, 4 cards (Cameras, Sensors, Sprinklers, Open Incidents), "Needs your attention", camera tiles |
| | Live Cameras (pehle Cameras) | `/cameras` | LIVE / NO AI DATA tiles; aag = laal box, dhuwan = dashed orange box, log = hara; filters All / Problems / CCTV / Drone |
| | Floor Plan (pehle Building Map) | `/map` | cameras, sensors (status ka rang), log, "People per room" aur "Fire risk" layers, click = bahar nikalne ka rasta. "Edit plan" = `/floor-plans` ("+ Sensor", "Max people (crowd alert)", 4-point calibration) |
| | **Occupancy & Heatmap** (naya) | `/occupancy` | 4 cards, logon ka purple heatmap, "People per room" (Live / Stale / Unknown) aur "Crowd at the exits" tables |
| | Incidents | `/incidents` | Fire aur Smoke cards, list (SMOKE tag, Resolve / False alarm), detail, "What happened" history. `/incidents?incident=<id>` woh incident kholta hai (push click) |
| | Sensors | `/sensors` | readings, limits, Normal/Warning/Alarm/Critical/Not reporting/Stale, history |
| | Sprinklers | `/sprinklers` | zones aur relays (Auto/Manual); relay chalana **asli action** hai |
| | Activity Log | `/activity` | live event log, filters |
| | Reports & Analytics (pehle Reports) | `/reports` | totals, per-day chart, severity, source ("Crowd" samet), aur "Model accuracy" (`ai-service/reports/**/report.json` se; incident totals accuracy nahi) |
| CONFIGURATION | AI Settings (pehle AI Service) | `/ai` | service status, har model ka "Detection threshold" slider 0.25–0.55 (Apply sirf Admin), camera sources, "Test with a picture" |
| | **Notifications** (naya) | `/notifications` | "Notifications & System Health": health cards (har 10 s `GET /api/system/health` se), alert channels table, Firebase panel, "Recent notification activity" |
| | Settings (sirf Admin) | `/devices` | cameras add/edit/delete |

System health ab AI page par nahi, Notifications par hai. Alert channels aur Firebase panel bhi Notifications par hain. Kamron ke log aur exit crowding Occupancy & Heatmap par hain (Floor Plan par nahi).

## Setup (Windows PowerShell)

Pehle se chahiye: Python 3.13 64-bit, SQL Server 2022 (Developer ya Express) aur **ODBC Driver 18 for SQL Server**. Naye PC par asaan raasta project folder ka `.\setup.ps1` hai (dekhein [../README.md](../README.md)). Haath se:

```powershell
cd C:\Users\m_fahad\Desktop\Project\sentinel-django
py -3.13 -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
```

Configuration project ki `.env` hai (ek folder upar; `.env.example` se copy karein). Asli environment variables hamesha jeet-te hain.

| Variable | Matlab | Default |
|---|---|---|
| `ConnectionStrings__Sentinel` | SQL Server connection (ADO.NET format) | `Server=localhost;Database=SentinelDb;Trusted_Connection=True;TrustServerCertificate=True` |
| `Jwt__SigningKey` | **Lazmi**, ≥ 32 characters. Session cookies aur API tokens sign karta hai | — |
| `Jwt__ExpiryMinutes` | Session aur token ki umar | 480 |
| `Device__ApiKey` | Sirf AI service ki key (`X-Device-Key`): camera list aur detections | khali = AI device access band |
| `Device__SensorApiKey` | Sirf sensor gateways ki key (`X-Device-Key`): sensor readings, telemetry, sprinkler status parhna. `Device__ApiKey` se **alag** honi chahiye, warna server start nahi hota | khali = sensor gateways band |
| `Seed__AdminEmail`, `Seed__AdminPassword` | Pehla admin, jab Users table khali ho | `admin@sentinel.local`, generated |
| `Seed__DemoData` | Pehle start par demo cameras, sensors, sprinklers | false (`SENTINEL_DEBUG` ke saath true) |
| `Alerts__*` | Cooldown 120 s, merge window 90 s, camera lost 10 s, start-up grace 60 s, sensor multipliers, site name, `Alerts__DashboardUrl` (alerts mein link, `http://localhost:5291/`) | upar wale |
| `Alerts__AutoSprinklerOnSimulatedData` | Simulated data sprinkler relays chala sake | false (`SENTINEL_DEBUG` ke saath true) |
| `AiService__BaseUrl`, `AiService__TimeoutSeconds`, `AiService__MaxImageBytes` | AI service | `http://localhost:8008`, 20, 5 MB |
| `FloorPlans__*` | Chalne ki raftaar 1.2 m/s, danger clearance 1 m, image folder `App_Data/floorplans` | upar wale |
| `RateLimits__AiAnalyze__PermitLimit`, `__WindowSeconds` | Har user ke test-image ki had | 10 per 60 s |
| `Notifications__Sms__*`, `__Email__*`, `__WebPush__*` | Twilio SMS, SMTP (khali host = sirf log), web push (`pip install pywebpush` chahiye) | band |
| `Notifications__WhatsApp__TwilioAccountSid`, `__TwilioAuthToken`, `__FromNumber`, `__Recipients` | Twilio WhatsApp. Numbers E.164 (`+923001234567`); approved WhatsApp sender chahiye; 24 h customer window ke bahar sirf approved templates | band |
| `Notifications__Telegram__BotToken`, `__ChatIds` | **Optional / Legacy — not part of current scope.** Telegram bot; chat ids comma se alag | band |
| `Notifications__Fcm__ProjectId`, `__CredentialsFile` | Firebase Cloud Messaging (HTTP v1); service-account JSON ka path, file project folder ke **bahar** rakhein; `pip install google-auth` chahiye | band |
| `Notifications__Fcm__WebApiKey`, `__WebAppId`, `__MessagingSenderId`, `__VapidKey` | Firebase console ki public web values (browser push) | khali |
| `Notifications__Gsm__Port`, `__BaudRate`, `__Recipients` | **Optional / Legacy — not part of current scope.** USB GSM modem (maslan `COM5`, 115200); recipients khali = SMS recipients; `pip install pyserial` chahiye | band |
| `Notifications__RetrySeconds` | Poori tarah fail hue channel ki ek retry | 30 |
| `Notifications__SnapshotDirectory` | Incident snapshot JPEGs | `App_Data/snapshots` |
| `FloorPlans__OccupancyStaleSeconds` | Camera ka aakhri frame itna purana ho to kamra "Stale" | 10 |
| `FloorPlans__CapacityFile` | Crowd limits ("Max people") ki file | `App_Data/capacities.json` |
| `AiService__EvaluationDirectory` | Reports ke "Model accuracy" ke `report.json` yahan se | `ai-service/reports` |
| `SENTINEL_DEBUG` | Development defaults (upar dekhein); signing key ke baghair bhi chalne deta hai | false |
| `SENTINEL_ALLOWED_HOSTS` | Server kin host names ko jawab de | `localhost,127.0.0.1,[::1]` |
| `SENTINEL_HTTPS` | HTTPS par: secure cookies, HTTPS redirect, HSTS | false |
| `SENTINEL_ODBC_DRIVER` | ODBC driver ka naam | `ODBC Driver 18 for SQL Server` |

Saari nayi settings marzi hain aur unke naam `.env.example` mein hain. Secrets kabhi commit na karein. `Cors__AllowedOrigins__*` ab istemal nahi hota: pages aur API ek hi origin par hain.

### Database

Models **unmanaged** hain: Django Sentinel tables na banata hai, na badalta hai, na migrate karta hai, aur use apni koi table nahi chahiye (sessions signed cookies hain). Woh maujooda `SentinelDb` ko jaisa hai waisa istemal karta hai.

- **`SentinelDb` par `manage.py migrate` kabhi mat chalayein.** Database router waise bhi rok deta hai: migrations sirf `test_*` naam wale databases par chalti hain.
- Naya install (khali database): `.\.venv\Scripts\python manage.py create_schema` `schema/sentinel.sql` chalata hai. Tables pehle se hon to inkaar kar deta hai.
- Database ek PC se doosre par: `database\save.ps1` aur `database\load.ps1` (SQL Server backup).
- Passwords ASP.NET Identity v3 format (PBKDF2-HMAC-SHA512) mein rehte hain; purane logins chalte rehte hain.
- Camera calibration JSON purane format (`{"FloorPlanId":…}`) mein store hota hai.
- Is round mein **koi schema change nahi**. Sirf naye text values: incident source `Crowd`; log types `SmokeMeasurement`, `Notification`, `SettingChanged`; plan element type `Sensor`; FCM tokens maujooda `PushSubscriptions` table mein `fcm:<token>` ki shakal mein.

## Chalana

```powershell
# Django: dashboard + API, http://localhost:5291 (AI service ka default API address)
cd C:\Users\m_fahad\Desktop\Project\sentinel-django
.\.venv\Scripts\python -m uvicorn config.asgi:application --host 127.0.0.1 --port 5291

# AI service (nayi terminal); har 60 s server se cameras parhti hai
cd C:\Users\m_fahad\Desktop\Project\ai-service
$env:SENTINEL_DEVICE_KEY = '<Device__ApiKey>'; .\.venv\Scripts\python -m sentinel_ai

# Simulator (marzi): warehouse mein aag
cd C:\Users\m_fahad\Desktop\Project\sentinel-django
.\.venv\Scripts\python tools\simulator.py --device-key <Device__ApiKey> --sensor-key <Device__SensorApiKey> --ignite "Ground Floor - Warehouse"
```

Ya project folder se `.\start.ps1` (`-Simulator` aag ke demo ke liye; purana `-Django` switch bhi chalta hai) aur band karne ke liye `.\stop.ps1`. http://localhost:5291 kholein aur `Seed__` admin se login karein.

**Sirf ek** server process chalayein (`--workers` nahi): camera watchdog, notification queue, live-event broadcast, rate limits aur camera locks isi process mein rehte hain. `manage.py runserver` support nahi: woh live events stream nahi kar sakta.

## API

Wahi paths, JSON (camelCase, enums text mein) aur status codes jo purane ASP.NET API ke thay. Errors `application/problem+json` hain (`status`, `title`, `detail`, `errors`).
Authentication: browser apni session cookie (CSRF ke saath) istemal karta hai. Scripts `POST /api/auth/login` → `Authorization: Bearer <token>`. Machines `X-Device-Key`: har key sirf apne role ke endpoints khol sakti hai (doosre role ka endpoint → 403, ghalat ya missing key → 401).

| Endpoint | Kaun | |
|---|---|---|
| `POST /api/auth/login`, `GET /api/auth/me` | koi bhi (10/min per IP) / operator | scripts ke liye token |
| `GET /api/cameras`, `GET /api/cameras/{id}` | operator ya AI key | AI service ko stream passwords dikhte hain, logon ko `***` |
| `POST`, `PUT /api/cameras[/{id}]`, `DELETE /api/cameras/{id}` | admin | |
| `PUT`, `DELETE /api/cameras/{id}/calibration` | operator | 4 point pairs, validated |
| `GET /api/cameras/{id}/stream` | operator | MJPEG relay |
| `POST /api/detections` | operator ya AI key | AI frames; 202, 400 invalid, 404 deleted camera |
| `GET /api/dashboard/snapshot` | operator | |
| `GET /api/incidents?resolved&minSeverity&limit`, `GET /api/incidents/{id}`, `POST /api/incidents/{id}/resolve` | operator | |
| `GET /api/incidents/logs?incidentId&type&limit`, `POST /api/incidents/dispatch` | operator | dispatch cooldown ko bypass karta hai |
| `GET /api/iot/sensors[/{id}]`, `POST /api/iot/sensors/{id}/readings`, `POST /api/iot/telemetry` | operator ya sensor key | |
| `GET /api/iot/sensors/{id}/history` | operator | |
| `GET /api/iot/sprinklers` | operator ya sensor key | sirf parhna |
| `POST /api/iot/sprinklers/{id}/toggle` | operator | khali body ulat deti hai; **koi device key sprinkler nahi chala sakti** |
| `POST /api/iot/sprinklers/master` | operator | |
| `GET/POST /api/floorplans`, `GET/PUT/DELETE /api/floorplans/{id}`, `GET/PUT …/image`, `POST …/route`, `GET …/risk` | operator | |
| `GET /api/floorplans/{id}/occupancy` | operator | har kamre ke log: Live / Stale / Unknown, kabhi andaze ka 0 nahi |
| `GET`, `PUT /api/floorplans/{id}/capacities` | operator | "Max people" per kamra/exit, maslan `{"Exit:East": 30}`; PUT sab badal deta hai |
| `GET`, `PUT /api/ai/sensitivity` | operator / PUT sirf admin | `{"model", "threshold"}`; sirf AI service ki confirm ki hui value active dikhti hai; har badlao `SettingChanged` log |
| `GET /api/system/health` | operator | Notifications page ke health cards (asli checks) |
| `GET`, `POST`, `DELETE /api/push/fcm-token` | operator | Firebase device token register/refresh/hatana (browser; mobile app ke liye bhi tayyar, app maujood nahi) |
| `GET /api/incidents/{id}/snapshot` | operator, ya push ka signed link (`?s=…`, 24 h) | incident ki camera JPEG |
| `GET /api/ai/status`, `POST /api/ai/analyze` | operator | analyze: 10/min per user; kabhi incident nahi kholta |
| `GET /api/push/vapid-public-key`, `POST /api/push/subscribe`, `POST /api/push/unsubscribe` | operator | |
| `GET /api/events` | operator (session) | live events: pehle `snapshot`, phir `detection`, `telemetry`, `sprinkler`, `incident`, `notification`, `activity`, `incidentLog`, `occupancy` |
| `GET /health` | koi bhi | |

Purane API se farq: SignalR hub (`/hubs/monitoring`) aur Swagger page nahi. Dashboards `/api/events` istemal karte hain; producers `POST /api/detections`. Login token signed Django token hai (JWT nahi), aur sirf `Authorization: Bearer` se parha jata hai.

Naye pages: `/occupancy` aur `/notifications`.

## Notifications

Background worker (thread) alerts bhejta hai. Har koshish incident history (log type `Notification`) aur Notifications page par likhi jati hai. Ek hi incident + severity ka duplicate nahi jata; poori tarah fail hua channel 30 s baad ek dafa dobara.

| Channel | Kab | Note |
|---|---|---|
| Firebase push (FCM) | har naya confirmed fire ya smoke incident | browser: Notifications → "Enable notifications on this browser" (permission sirf click par; Firebase Web SDK 10.12.2 gstatic se). Click `/incidents?incident=<id>` kholta hai. MJPEG camera ho to snapshot ka signed 24 h link |
| SMS (Twilio), WhatsApp (Twilio), Telegram, email | emergencies (High/Critical ya khatre mein insaan) aur manual dispatch | Telegram MJPEG camera par tasveer bhi bhejta hai |
| GSM modem SMS | sirf jab Twilio SMS fail ho ya configured na ho | AT, AT+CPIN?, AT+CREG?, AT+CMGF=1, AT+CMGS; USB modem (SIM800/SIM7600) + active SIM |
| Web Push (VAPID) | pehle ki tarah | |

Status words: **Delivered** (sirf Telegram confirm karta hai), **Pending** (Twilio/GSM ne qubool kiya), **Sent** (Firebase ne qubool kiya), **Failed**, **Invalid token**, **Disabled**, **Not configured**. "Pending" ya "Sent" ka matlab yeh nahi ke message phone tak pohancha.

Asli delivery (Twilio, WhatsApp, Telegram, Firebase, SMTP) aur GSM hardware **test nahi hue**. Snapshot link sirf wahan khulta hai jahan se dashboard ka address pohanch mein ho.

## Occupancy aur crowd alerts

- 3+ frames tak track hue log camera calibration ke zariye plan par rakhe jate hain. Har kamra: **Live**, **Stale** (camera 10 s se purana, aakhri count rehta hai) ya **Unknown** (koi camera nahi). Ek kamre par do cameras: count = sab se bara, `maxPossible` = jor (cameras ke darmiyan pehchan nahi).
- Limits editor ke "Max people (crowd alert)" field se `App_Data/capacities.json` mein. Had se zyada → ek "Crowd" incident (Medium; koi safe exit na ho to High), jo had mein wapas aane par khud resolve hota hai. Unknown/stale data kabhi incident nahi kholta ya band karta. Mashwara sirf aise exit ka jo aag/dhuwan danger zone mein na ho.

## AI sensitivity

AI Settings par slider (0.25–0.55, step 0.01) `PUT /api/ai/sensitivity` bhejta hai; server AI service ke `POST /config` ko aage deta hai. AI service value `ai-service/models/thresholds.local.json` mein save karti hai (restart ke baad bhi; `models.json` nahi badalta; default fire_smoke 0.40, person 0.45). Kam value = zyada sensitive (zyada alerts, zyada jhoote alarm).

## Camera lifecycle aur status banner

- **Frames**: AI service har analysed frame `/api/detections` par bhejti hai. Delete hue camera ke frames ko 404 milta hai (10 min tak memory mein yaad), is liye AI service us camera ko foran rok deti hai. Kabhi register na hue camera ke frames phir bhi "Unassigned" alert dete hain (fail-safe).
- **Camera delete aur frames**: `process_detection` aur `delete_camera` ek hi per-camera lock (`camera:<id>`) lete hain. Frame ya to delete se pehle poora handle hota hai ya delete ke baad 404 pata hai — kabhi apni camera row ke baghair evaluate nahi hota.
- **Watchdog** (har 2 s): jo camera frames bhej raha tha aur 10 s khamosh rahe, use ek "Camera lost" incident milta hai (us zone mein fire incident khula ho to High, warna Medium) aur status `Offline`. Naye frames phir `Online` karte hain. Reconnect kabhi khule fire ya camera-lost incident ko resolve nahi karta. `camera_lost` bhi wahi `camera:<id>` lock leta hai, is liye delete ke saath race mein FK error nahi aata.
- **Start-up reconciliation** (audit D-17): server start par har stream link wala camera aise dekha jata hai jaise 50 s pehle dekha gaya ho, is liye restart ke dauran mara hua camera 60 s grace ke baad report hota hai.
- **Banner** (`shell.js`, audit D-15 / IL-9): pehle khule incidents ("CRITICAL ALERT · n OPEN" / "ELEVATED RISK · n OPEN"), saath monitoring ka masla. Incident na ho to banner sirf tab hara jab kuch ghalat na ho. Warna is tarteeb se: LIVE LINK DOWN (pehla snapshot aane tak bhi) → AI STATUS UNKNOWN → AI OFFLINE → AI MODEL MISSING → CAMERA OFFLINE → AI DATA STALE (kisi camera ke frames 8–120 s pehle ruke). AI status har 10 s poll hota hai; purane jawab naye ko overwrite nahi karte.

## Tests

Saare tests disposable databases par chalte hain; koi `.env` nahi parhta (us mein asli Twilio/SMTP keys ho sakti hain), aur koi asli camera, relay ya notification service tak nahi pohanchta.

```powershell
cd C:\Users\m_fahad\Desktop\Project\sentinel-django
.\.venv\Scripts\python manage.py test --noinput      # Django: schema/sentinel.sql se test_SentinelDb_django banata, baad mein drop
cd e2e; npm install --offline; cd ..                 # ek dafa: playwright-core (installed Microsoft Edge, koi browser download nahi)
.\.venv\Scripts\python e2e\run.py                    # browser tests: asli server + test_SentinelDb_e2e + stub AI service
.\.venv\Scripts\python e2e\lifecycle.py              # asli AI service, asli models, test camera: camera lifecycle (~6 min)
cd ..\ai-service; .\.venv\Scripts\python -m unittest discover -s tests   # AI service
```

`e2e/lifecycle.py` aakhir mein `ai-service/tools/live_test.py` (bina badle) Django ke khilaf chalata hai.

Taaza nateeje (2026-11-04): Django **288/288**, AI service **57/57** (asli model tests samet), browser e2e **31/31**, lifecycle **18/18** (asli AI, asli models, MJPEG test camera). Yeh ek run ka nateeja hai, reliability ki guarantee nahi.

### Asli AI service ke saath naap (`e2e/lifecycle.py`, 2026-10-26, sab race fixes ke baad ka run, prerecorded MJPEG, asli CCTV nahi)

| Qadam | Nateeja |
|---|---|
| Camera add → AI frames bheje | 59.9 s (AI ka 60 s camera sync) |
| `fire-test.avi` mein asli aag → fire incident | 12.6 s (runs mein 3.6–66 s tak badla) |
| Camera band → AI health `error` / camera-lost incident / `Offline` | 1.1 s / 11.6 s (High: zone mein fire khula) / 11.6 s |
| Camera wapas → `Online` (incidents khule rehte hain) | 4.2 s |
| AI band → `/api/ai/status` offline / camera `Offline` | 2.1 s / 7.9 s |
| AI restart → frames phir | 4.1 s |
| Camera mara hua aur server restart → camera-lost | 60.1 s (start-up grace) |
| Stream ke dauran camera delete → AI roke; jhoote alarms | 0.7 s; 20 s mein koi nahi |
| `ai-service/tools/live_test.py`, bina badle | 8/8 (fire incident 110 s mein; had 150 s) |

Yeh ek run ka nateeja hai (18/18), reliability ki guarantee nahi.

## Maloom hadein

- **Sirf ek server process** (dekhein Chalana). Kai processes mein har ek sirf apne frames dekhega aur apne events bhejega, aur camera locks ki guarantee khatam ho jayegi.
- **Deleted-camera memory** in-process hai (10 min). Server restart ke baad delete hue id ka late frame ek "Unassigned" incident khol sakta hai (audit D-12). AI service ka 60 s camera sync fallback hai.
- **`LocMemCache` 300 entries** tak rakhta hai aur bharne par 1/3 random mita deta hai; bohat saari IPs se login koshishein deleted-camera marker ya notification cooldown ko waqt se pehle mita sakti hain.
- **Fire detection ka waqt** runs mein bohat badalta hai (3.6–110 s); `live_test.py` ki 150 s had ke qareeb pohanch sakta hai.
- Web push ke liye VAPID keys set hon to `pip install pywebpush` chahiye; Firebase ke liye `google-auth`, GSM ke liye `pyserial` (teeno default install nahi).
- Koi mobile app nahi; FCM token API ek app ke liye tayyar hai.
- Production-ready aur safety-certified **nahi**. Test nahi hue: asli RTSP CCTV, asli sensors/relays, asli Twilio/WhatsApp/Telegram/Firebase/SMTP delivery, GSM hardware, asli sunset/steam/vape footage, tablet layout.

D-18 (smoke ke foran baad Low fire frame throttle mein gum) 2026-10-26 ko theek ho gaya: `sentinel/alerts.py`, tests `test_a_low_fire_right_after_smoke_still_marks_the_incident_as_fire` aur `test_after_the_first_fire_frame_later_frames_follow_the_normal_throttle`.

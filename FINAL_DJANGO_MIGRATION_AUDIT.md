# FireVision AI — Django Migration ki Aakhri Audit (2026-10-26)

Yeh report batati hai ke migration ke aakhir mein kya kiya gaya, kya test hua (asal output ke saath), aur kya abhi baqi ya untested hai. Har test nateeja isi tareekh ke asal run ka hai; purane nateeje "history" likh kar alag kiye gaye hain.

## 1. Aakhri architecture

- **Django hi wahid server hai**: `sentinel-django/` (Django 5.2, Django REST Framework, `mssql-django`, `uvicorn`, port 5291). Yahi dashboard pages, REST API (purane paths aur JSON) aur live updates (`GET /api/events`, Server-Sent Events) deta hai.
- **Database**: wahi SQL Server `SentinelDb`, schema bina badle (`sentinel-django/schema/sentinel.sql`). Models unmanaged; `SentinelDb` par `migrate` router se band.
- **AI service**: `ai-service/` (Python, ONNX Runtime, port 8008) — is migration mein bilkul nahi badli.
- **Start**: project folder se `.\start.ps1` (Server + AI service), `-Simulator` aag ke demo ke liye. Purana `-Django` switch ab bhi qubool hai.

Parity check (2026-10-26, code se): purane 10 controllers ke **har route aur HTTP method** ka Django mein `sentinel/urls.py` + `sentinel/api.py` mein milan hua. SignalR hub `/hubs/monitoring` ki jagah `/api/events`. Jaan boojh kar chhoray gaye: Swagger, CORS, `generate-vapid-keys` CLI, JWT issuer/audience (sessions ne jagah li). Tafseel: [sentinel-django/MIGRATION.md](sentinel-django/MIGRATION.md).

## 2. Delete ki gayi legacy directories

| Path | Kya tha | Tracked files | Saath mein mitay (ignored, dobara ban sakte) |
|---|---|---|---|
| `backend/` | ASP.NET Core 8 API (`Sentinel.Api`), `Sentinel.Api.Tests`, `Sentinel.Simulator`, `Sentinel.slnx` | 82 | `bin/`, `obj/` (folder takreeban 308 MB) |
| `frontend/` | Angular 22 dashboard (`angular.json`, `src/`, `e2e/`) | 78 | `node_modules/`, `dist/`, `.angular/`, `test-results/` |

Delete se pehle ki jaanch:
- Pehchaan: `backend/Sentinel.slnx` + `Sentinel.Api/Program.cs`, `frontend/angular.json` maujood thay.
- In folders mein koi untracked user file nahi thi (sirf build artifacts).
- Data loss ka khatra nahi: `backend/Sentinel.Api/App_Data/floorplans` khali tha, aur `SentinelDb` ke wahid floor plan ka `ImageFileName` NULL hai (read-only query).
- Django ya AI code ka koi runtime hissa in folders par depend nahi karta (sirf "kahan se port hua" wale comments).
- `appsettings.json` ke defaults `sentinel-django/config/settings.py` (`SENTINEL`) mein milaye gaye.
- Dono folders git mein mehfooz hain: purane commits aur branch `feature/dotnet-angular-rebuild`.

## 3. Mehfooz rakhi gayi directories aur assets

- `sentinel-django/` (server, tests, schema, simulator, e2e).
- `ai-service/` — koi file nahi badli; model files ke sha256 run se pehle aur baad same.
- `database/` (`SentinelDb.bak`, `save.ps1`, `load.ps1`, `rename-zones.sql`) — sirf `load.ps1` ka ek message badla.
- `docs/` (guides, PDFs, `images/`).
- `.env` (asli secrets) — **nahi chhedi**, commit bhi nahi hoti.
- `docs/PROJECT_GUIDE_ROMAN_URDU.md` — yeh file is migration se pehle hi commit `6bc2190` mein delete ho chuki thi; na chhedi, na dobara banayi.

## 4. Update ki gayi reports aur guides

| File | Kya badla |
|---|---|
| `README.md` | Poora Roman Urdu mein, sirf Django architecture, setup, tests, history alag |
| `sentinel-django/README.md` | Roman Urdu; D-18 "fixed"; race fixes; taaza lifecycle naap; hadein |
| `sentinel-django/MIGRATION.md` | Roman Urdu; historical tables mehfooz; deletion status aur migration ke dauran ke fixes |
| `docs/FIREVISION_AI_GUIDE_ROMAN_URDU_v5.html` | Architecture diagram, tech table, flow, alert engine, setup, tests, API (chapter 23 "API Aur Live Events (SSE)"), file map — sab Django par |
| `docs/FIREVISION_AI_USER_GUIDE_ROMAN_URDU.html` | Prerequisites, setup, start (2 windows, :5291), Test 3 ab PowerShell se (Swagger nahi), web push, tests, troubleshooting |
| `docs/FIREVISION_AI_FEATURES_GUIDE_ROMAN_URDU.html` | Start ki windows, :5291, simulator command, screenshots note |
| `docs/FIREVISION_AI_HARDWARE_GUIDE_ROMAN_URDU.html` | Network par server (`SENTINEL_ALLOWED_HOSTS`, uvicorn `--host 0.0.0.0`), VAPID keys |
| `docs/FIREVISION_AI_GUIDE_ROMAN_URDU_v5.pdf`, `docs/FIREVISION_AI_USER_GUIDE_ROMAN_URDU.pdf` | Updated HTML se Edge headless (Chromium "Skia/PDF", wahi engine jo purane PDFs ka tha) se dobara bane: 55 aur 32 pages |
| `start.ps1`, `.env.example`, `database/load.ps1`, `sentinel-django/config/settings.py` (ek comment) | Sirf-Django; `.env.example` se CORS line hati, VAPID tareeqa naya |
| `FINAL_DJANGO_MIGRATION_AUDIT.md` | Yeh report (naya) |

Jo **nahi** badla (aur kyun): `ai-service/README.md` aur `ai-service/reports/*` — `ai-service/` ko chhedna mana tha; un mein purane .NET API ke hawale hain (audit report historical hai). Guides ki tasveerein (`docs/images/`) purane Angular dashboard ki hain; har guide mein iska note likha hai.

## 5. Implement kiye gaye fixes (is migration ke dauran)

| Masla | Fix | File |
|---|---|---|
| D-18: smoke ke foran baad Low fire frame 2 s throttle mein gum | har throttle window ka pehla fire frame guzarta hai; severity rules nahi badle | `sentinel/alerts.py` (`evaluate_detection`) |
| Camera delete vs in-flight frame: "Unassigned" incident ya FK error | `process_detection` aur `delete_camera` ek per-camera lock (`camera:<id>`) | `sentinel/services.py` |
| Watchdog `camera_lost` vs delete: `FK_IncidentAlerts_CameraFeeds_CameraId` IntegrityError | `camera_lost` bhi wahi lock | `sentinel/alerts.py` |
| Lifecycle test ki timing races (check 10, check 11b) | AI ke frames drain hone ka intezar; delete se pehle bane incident ko AI rukne ke baad resolve; assertions bina badle | `sentinel-django/e2e/lifecycle.py` |

Regression tests: D-18 ke 2, frame/delete race ke 3, watchdog/delete race ka 1 (`sentinel/tests/test_api.py`). Race tests purane code par fail aur fix ke baad pass hue.

## 6. Test commands aur asal nateeje (2026-10-26, legacy delete hone ke **baad**)

Sab tests disposable databases (`test_SentinelDb_django`, `test_SentinelDb_e2e`) par; `SENTINEL_NO_DOTENV=1`; koi env secret set nahi tha.

| Command (folder `sentinel-django`) | Nateeja | Exit |
|---|---|---|
| `.\.venv\Scripts\python manage.py test --noinput` | **76 tests, OK** (40.0 s) | 0 |
| `.\.venv\Scripts\python e2e\run.py` | **10/10 pass**, 0 fail, 0 skip (105.6 s); server log mein koi 5xx nahi | 0 |
| `.\.venv\Scripts\python e2e\lifecycle.py` | **18/18 checks** (310.8 s) | 0 |
| `manage.py check` (dummy key) | no issues | 0 |
| `manage.py check --deploy` (dummy key) | 5 warnings: HSTS, SSL redirect, secure cookies (local HTTP par expected; `SENTINEL_HTTPS=true` inhein on karta hai) aur W009 (sirf dummy key ki wajah se) | — |
| `pip check` | No broken requirements | 0 |
| `start.ps1`, `database/load.ps1` PowerShell parser | 0 parse errors; `start.ps1 [-Simulator] [-Django]` | — |

Deletion se pehle (parity gate) bhi: Django 76/76 aur browser 10/10 pass hue thay.

**Nahi chalaya ("not run")**:
- `.\start.ps1` asal mein — yeh asli `.env` aur asli `SentinelDb` istemal karta (server start par seed bhi likh sakta hai); sirf parse kiya. Server start aur `/health` browser e2e mein test DB par verify hue.
- AI service ke tests (`ai-service`, `unittest`) — aakhri report: 25/25 (2026-10-21), is migration mein dobara nahi.

## 7. Startup, build aur API verification

Browser e2e ne asli `uvicorn` server (test DB par) chala kar yeh verify kiya: `GET /health` 200, login (validation, ghalat password, sign-in), dashboard, live banner (SSE), incidents live + resolve, camera tile overlay, Devices (add/edit/delete), AI page, floor plans + evacuation route, har page ke routes aur phone width. Lifecycle ne asli AI service + models ke saath detections, watchdog, restart, delete aur `ai-service/tools/live_test.py` (8/8) chalaye. Django ko build step ki zaroorat nahi.

Is lifecycle run ke naap: camera add → frames 60.5 s; asli aag → fire incident 3.8 s; camera band → camera-lost 10.7 s (High); restart ke baad camera-lost 60.6 s (start-up grace); delete → AI ruki 0.6 s, 20 s mein koi jhoota alarm nahi; `live_test.py` fire incident 72 s, route West exit 5.26 m → East exit 13.25 m. Is run mein delete wali race bani hi nahi (`resolved 0 …`), is liye race fixes ka saboot unit tests hain, yeh run nahi.

## 8. Database aur AI safety

- Har run se pehle aur baad `sys.databases` mein sirf `SentinelDb`; test databases drop hue.
- `SentinelDb` (sirf read-only queries): test users 0, incidents 1 (pehle jaisa), `sys.dm_db_index_usage_stats` mein kisi table par user write nahi (SQL Server ke aakhri restart se).
- `ai-service/models/fire_smoke_yolox.onnx`, `yolox_tiny.onnx`, `models.json` ke sha256 pehle wale se same. `ai-service/` mein koi file nahi badli.
- Koi asli CCTV, sprinkler, alarm, SMS, email ya push nahi chala. Koi naya dependency install ya download nahi hua.
- Koi chalta hua process band nahi kiya; tests ke baad ports 5291, 5399, 8018, 8019, 8095, 8096 khali.

## 9. Baqi kaam (A/B/C/D)

**A. Must fix:** koi khula nahi. (Startup, primary features aur regression tests sab pass.)

**B. Should fix**

| File / function | Masla | Asar | Agla qadam |
|---|---|---|---|
| `config/settings.py` `CACHES` (`LocMemCache`) | 300 entries ki had; bharne par 1/3 random entries mitti hain — deleted-camera marker ya notification cooldown waqt se pehle mit sakti hai | kuch late "Unassigned" frames ya ek duplicate notification | `OPTIONS: {"MAX_ENTRIES": …}` barhana ya alag cache; approval ke baad |
| `ai-service/tools/live_test.py` fire wait | Fire detection ka waqt runs mein 3.6–110 s; had 150 s | lifecycle check 12 kabhi flaky ho sakta hai | kai runs ka record; zaroorat ho to had (approval se) |
| `ai-service/README.md`, `ai-service/reports/*` | Purane .NET API ka zikr | sirf documentation | `ai-service/` badalne ki ijazat milne par |
| `.env` (local) | Purane comments aur `Cors__AllowedOrigins__0` | koi asar nahi (Django ignore karta hai) | user khud saaf kar sakta hai |
| `docs/images/*` | Angular dashboard ki tasveerein | guide aur asal UI mein chhota farq | Django dashboard ke naye screenshots |
| `e2e/lifecycle.py` | `%TEMP%\sentinel-lifecycle-*` folders nahi mitata | temp folders jama hote hain (sirf test login) | cleanup ka ek line |
| HTTPS | `check --deploy` warnings | network par password saaf jata hai | network par chalane se pehle `SENTINEL_HTTPS` + reverse proxy |

**C. Optional:** `alerts._locks` dictionary har camera id par barhti hai (bohat kam asar); code comments mein ".NET se port" ke hawale (history ke taur par theek); `-Django` switch ab sirf compatibility ke liye.

**D. Not verified**
- Asli RTSP/IP CCTV, fog/steam/IR footage.
- Asli hardware: ESP32 sensors, sprinkler relays.
- Asli Twilio SMS, SMTP email, Web Push delivery.
- Kai server processes (`--workers`) — locks, watchdog aur SSE sirf ek process ke liye.
- Doosre PC se network par dashboard (hardware guide ka qadam) — test nahi hua.
- `.\start.ps1` asli `.env` ke saath end-to-end.
- Naye PDFs page-by-page visually (machine par PDF renderer nahi; HTML render check kiya gaya).

## 10. Asli CCTV, hardware aur multi-process hadein

Saare live tests prerecorded video (`fire-test.avi`) aur local MJPEG test camera par hue. Race fixes in-process locks hain: **ek hi `uvicorn` process** ki shart par sahi. Kai processes mein DB-level locking chahiye hogi (scope se bahar).

## 11. Git ka khulasa

Is migration ki commit mein:
- **Delete**: `backend/` (82 files), `frontend/` (78 files).
- **Naya**: `sentinel-django/` (69 source files; `.venv`, `node_modules`, logs, `__pycache__` ignored), `FINAL_DJANGO_MIGRATION_AUDIT.md`.
- **Modified**: `README.md`, `.env.example`, `start.ps1`, `database/load.ps1`, 4 HTML guides, 2 PDFs.
- `.env` aur `ai-service/.venv` git se bahar (`.git/info/exclude`).

## 12. Kya project tayyar hai?

- **Local development aur FYP demo ke liye: haan.** Ek command (`.\start.ps1`) se server aur AI chalte hain; 76 unit/API tests, 10 browser tests aur 18 live lifecycle checks aaj pass hue.
- **Production ke liye: nahi.** Safety-certified nahi; asli CCTV, hardware aur notification channels untested; sirf ek process; HTTPS ke baghair. Lifecycle ka pass hona reliability ya safety ki guarantee nahi.

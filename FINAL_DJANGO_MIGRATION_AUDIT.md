# FireVision AI — Aakhri Report (Django version)

**Taaza update: 2026-10-28.** Is report mein har nateeja asal mein chalaye gaye test ka hai. Jo test nahi hua, use "nahi hua" likha gaya hai.
Migration ki tafseel (kya kahan gaya) [sentinel-django/MIGRATION.md](sentinel-django/MIGRATION.md) mein hai. Yeh project FYP/demo ke liye hai, production ke liye nahi.

## 1. Project ka current architecture

```
AI service / IoT gateway ──(X-Device-Key, REST)──► Django (uvicorn, :5291) ──► Django ORM ──► SQL Server (SentinelDb)
  ai-service/, tools/simulator.py                    │  alert engine → notification worker → SMS / Email / Push
                                                     │  camera watchdog (camera lost, Online/Offline), sprinklers
                                                     ▼
                         Browser ◄── Django pages + /api/events (Server-Sent Events, live updates) ── session cookie
```

| Hissa | Kya hai | Folder |
|---|---|---|
| Server (dashboard + API + live updates) | Django 5.2, Django REST Framework, `uvicorn` — **ek hi process** | `sentinel-django/` |
| Database | SQL Server `SentinelDb`, schema `sentinel-django/schema/sentinel.sql` (Django schema nahi badalta) | `database/` (backup scripts) |
| AI | Python + ONNX Runtime, port 8008; models `fire_smoke_yolox.onnx`, `yolox_tiny.onnx` | `ai-service/` |
| Docs | Roman Urdu guides (HTML + PDF) | `docs/` |
| Start | `.\start.ps1` (Server + AI), `.\start.ps1 -Simulator` (demo) | project root |

Purana ASP.NET Core backend aur Angular frontend 2026-10-26 ko delete ho chuke hain. Woh sirf purani git history mein hain.

## 2. Update ki gayi documentation (2026-10-28)

| File | Kya badla |
|---|---|
| `ai-service/README.md` | Roman Urdu mein; .NET/SignalR/Angular ki jagah Django; `AiService__BaseUrl`, `start.ps1` aur camera sync ka zikr |
| `ai-service/reports/README.md`, `ai-service/reports/2026-10-19-AUDIT.md` | Upar "Historical note" — yeh purane stack ki reports hain; content nahi badla |
| `docs/images/` | **15 tasveerein** naye Django dashboard ki (neeche list) |
| 3 HTML guides + `README.md` | Tasveeron wala note; tests ke taaza numbers |
| `docs/*_GUIDE_ROMAN_URDU_v5.pdf`, `docs/*_USER_GUIDE_ROMAN_URDU.pdf` | Updated HTML se dobara bane (Edge, 55 aur 32 pages) |
| `FINAL_DJANGO_MIGRATION_AUDIT.md` | Yeh report |

**Nayi tasveerein kaise li gayin:** test database (`test_SentinelDb_e2e`) par Django server, **asli AI service aur asli models**, `fire-test.avi` ka test MJPEG camera, aur simulator. Asli `SentinelDb` ya `.env` istemal nahi hua. Tasveeron mein jo AI detections hain woh asli model ki hain (raat ke video mein kuch "PERSON" ghalat detections bhi hain — yeh model ka asal output hai).

Nayi: `login`, `dashboard-fire`, `video-wall`, `live-mjpeg-tile`, `kpis`, `sensor-panel`, `alert-logs`, `incident-history`, `activity`, `evacuation-after`, `fire-risk-heatmap`, `dispatch-dialog`, `ai-tab-fire`, `floorplan-editor`, `camera-calibration`.
Abhi purani (Angular): `evacuation-before`, `evacuation-camera-people`, `camera-safe-danger-zones`, `swagger` (Swagger ab maujood hi nahi; tasveer sirf hawale ke liye).

## 3. Theek kiye gaye bugs

Code review mein mile; har bug code parh kar confirm kiya, phir chhota sa fix.

| # | Bug (asaan lafzon mein) | Fix | File | Test |
|---|---|---|---|---|
| 1 | 50 se zyada naye incidents ke baad koi **purana khula incident dashboard se ghayab** ho jata (banner "nominal" dikh sakta tha, Resolve button nahi) | Snapshot mein saare khule incidents + aakhri 50 | `sentinel/services.py` (`snapshot`) | `test_snapshot_keeps_an_old_open_incident_behind_50_newer_ones` |
| 2 | Incident **Resolve** karte waqt agar usi waqt naya AI frame aaye to resolve ulta ho sakta tha, aur purani copy save hone se nayi escalation mit jati | Resolve ab alert engine wala lock leta hai aur incident dobara parhta hai | `sentinel/services.py` (`resolve_incident`) | `test_resolving_a_stale_copy_keeps_the_escalation_that_happened_since` |
| 3 | Server restart ke baad stream wala camera **bina frame ke 60 s "Online"** dikhta | Watchdog pehle frame tak status nahi badalta | `sentinel/watchdog.py` | `test_after_a_restart_a_watched_stream_camera_stays_offline_until_its_first_frame` |
| 4 | Dashboard ke floor plan par active danger zone **laal sirf click ke baad**; bina naam wala zone kabhi laal nahi | Risk map ke active zones bhi istemal; naam ke baghair zone = "Danger zone" | `static/.../dashboard.js`, `planmap.js` | browser tests (10/10) |
| 5 | Floor-plan editor mein plan badalne par **pichle plan ka calibration** form mein reh jata (ghalat plan par save ho sakta tha) | Plan badalne par calibration form saaf | `static/.../floorplans.js` (`open`) | browser tests (10/10) |
| 6 | AI page par fire/person ka **box tasveer se bahar** ban raha tha (overlay tasveer se zyada chaura) | Overlay wrapper ab tasveer jitna chaura | `templates/sentinel/ai.html` | nayi `ai-tab-fire.png` mein box aag par (aankh se check) |

Pehle se maujood fixes bhi qaim hain: D-18 (smoke ke baad Low aag), camera delete vs frame race, watchdog `camera_lost` vs delete race. In ke saare tests pass hue (neeche).

Ek apni ghalti bhi theek ki: beech ke git kaam mein `sentinel-django/e2e/node_modules/playwright-core` adhoora reh gaya tha (browser tests nahi chal sakte thay); session backup se wapas rakha (version 1.63.0, lockfile ke mutabiq).

## 4. Test commands aur asal nateeje (2026-10-28)

Saare database tests disposable `test_*` databases par; `SENTINEL_NO_DOTENV=1`; koi asli SMS/email/push, camera ya sprinkler nahi.

| Test | Command (folder) | Nateeja | Waqt |
|---|---|---|---|
| Django unit/API (D-18, delete race, watchdog race aur 3 naye tests samet) | `.\.venv\Scripts\python manage.py test --noinput` (`sentinel-django`) | **79/79 pass** | 38 s |
| Browser (Edge: login, dashboard, live alerts, banner, devices, AI page, floor plans, mobile) | `.\.venv\Scripts\python e2e\run.py` (`sentinel-django`) | **10/10 pass** | 107 s |
| Lifecycle run 1 (asli AI + models + test camera) | `.\.venv\Scripts\python e2e\lifecycle.py` | **15/18** ❌ | 390 s |
| Lifecycle run 2 | wahi | **18/18** ✅ (aag 16.1 s mein) | 307 s |
| Lifecycle run 3 | wahi | **18/18** ✅ (aag 1.2 s mein) | 294 s |
| AI service unit tests (mock-based) | `.\.venv\Scripts\python -m unittest discover -s tests` (`ai-service`) | **25/25 pass** | 9 s |

**Lifecycle run 1 kyun fail hua:** asli AI ne `fire-test.avi` mein aag **150 s ke andar confirm nahi ki**. Is wajah se 3 checks fail hue: "fire incident", "camera-lost High severity" (High ke liye khula fire incident chahiye) aur "reconnect ke baad fire incident khula" — teeno ek hi wajah. Isi run mein server ne 330 frames accept kiye, koi error nahi tha, aur aakhir mein `live_test.py` ne usi video mein aag pakri (8/8). Wajah AI ka 3-frame confirmation hai: D-Fire video ke frames mein aag ke box har frame uchhalte hain, is liye confirmation ka waqt qismat par hai (ab tak 1.2 s se >150 s). Fix ke liye AI ke thresholds/confirmation ya test ki 150 s had badalni parti — dono is kaam mein mana/kamzori hain, is liye **pending** (neeche B-1).

Har lifecycle run mein: D-17 restart ke baad camera-lost 59.5 / 60.6 / 60.8 s (grace sahi), camera delete ke baad koi jhoota alarm nahi, `live_test.py` 8/8.

## 5. Startup aur configuration checks

| Check | Nateeja |
|---|---|
| `manage.py check` (dummy key, DB se connect nahi) | No issues |
| `pip check` — `sentinel-django\.venv` aur `ai-service\.venv` | Dono: no broken requirements |
| `start.ps1`, `database\load.ps1`, `database\save.ps1` (PowerShell parser) | 0 parse errors |
| `start.ps1` ki zaroori cheezen: dono venvs, `tools\simulator.py`, `config\asgi.py` | Sab maujood |
| Imports: `uvicorn` 0.54.0, Django, DRF, `mssql`; AI: `onnxruntime` 1.31.0, `cv2`, `numpy` | OK |
| `.env` mein `Device__ApiKey` aur 32+ characters ki `Jwt__SigningKey` | Maujood (values nahi dekhi) |
| `.env.example` | Sirf khali/default values; Django ke mutabiq |
| Startup command (`uvicorn config.asgi:application --port 5291`) aur AI command asal mein chale | Haan — browser aur lifecycle tests mein, test DB par |

**`.\start.ps1` khud nahi chalaya:** yeh asli `.env` parhta hai aur server start par asli `SentinelDb` se jur kar seed chalata hai. Asli database ko na chhoone ki shart ki wajah se ise chalana mumkin nahi tha. Upar wale checks aur test DB par wahi commands chalne se yeh hissa cover hai.

## 6. Baqi bugs aur hadein

- **Lifecycle ka aag wala check kabhi kabhi fail** (upar dekhein).
- **Runtime par add kiya camera watchdog mein nahi aata** (`sentinel/services.py` `save_camera`): ghalat RTSP link wala naya camera "Online" hi dikhta hai aur camera-lost alert nahi deta, jab tak server restart na ho. Fix pending, kyunke AI ka camera sync khud ~60 s leta hai — 60 s grace dene se naye sahi camera par bhi jhoota camera-lost alarm aa sakta hai.
- **Sirf ek server process:** camera locks, cache, watchdog aur live events ek hi process mein. Kai `uvicorn` workers ya servers par yeh guarantees khatam ho jati hain. Is ke liye database-level locking chahiye (kaam nahi kiya).
- **Cache (`LocMemCache`, 300 entries):** bharne par entries mit sakti hain (deleted-camera marker, notification cooldown). Marker 600 s aur restart par khatam.
- **HTTPS nahi:** network par password aur device key saaf jate hain. Network par chalane se pehle `SENTINEL_HTTPS=true`, reverse proxy aur certificate chahiye; `SENTINEL_ALLOWED_HOSTS` mein PC ka IP.
- **4 purani tasveerein** (upar list).

## 7. Kaam ki list

**A. Zaroori (main functionality tootne wale bugs):** koi khula nahi. Is review mein mile 6 bugs theek ho gaye aur tests pass hain.

**B. Behtar karne wale**

| # | File/path | Masla | Asar | Mashwara |
|---|---|---|---|---|
| B-1 | `sentinel-django/e2e/lifecycle.py` (check 3), `ai-service` confirmation | Aag ki confirmation 1.2 s se >150 s | Lifecycle kabhi 15/18 | Video jisme aag ka box stable ho, ya had ke baare mein faisla (approval ke saath) |
| B-2 | `sentinel/services.py` `save_camera` + `watchdog.py` | Naya camera watchdog mein nahi | Ghalat link wala camera "Online" | AI sync ke saath mila kar grace (jaise 120 s) tay karna, phir test |
| B-3 | `docs/images/` | 4 purani tasveerein | Guide mein thora farq | Calibration + logon wale frames ke saath dobara lena |
| B-4 | `sentinel-django/e2e/lifecycle.py` | `%TEMP%\sentinel-lifecycle-*` folders nahi mitata | Temp folders jama | Cleanup line |
| B-5 | `config/settings.py` `CACHES` | 300 entries ki had | Kabhi duplicate notification | `MAX_ENTRIES` barhana |

**C. Future ke liye optional:** kai server processes ke liye DB-level locking; HTTPS deployment; asli hardware (ESP32, relays) ke saath end-to-end; ghalat "PERSON" detections kam karne ke liye person model ka raat ke footage par evaluation.

## 8. Git status (2026-10-28, koi commit/push nahi kiya)

Branch `main`, GitHub (`origin/main`) ke barabar + yeh **32 files modified** (commit nahi):
- Code: `sentinel-django/sentinel/services.py`, `watchdog.py`, `templates/sentinel/ai.html`, `static/sentinel/js/dashboard.js`, `floorplans.js`, `planmap.js`, `tests/test_api.py`.
- Docs: `README.md`, `FINAL_DJANGO_MIGRATION_AUDIT.md`, `ai-service/README.md`, `ai-service/reports/README.md`, `ai-service/reports/2026-10-19-AUDIT.md`, 3 HTML guides, 2 PDFs, 15 tasveerein (`docs/images/`).
- `docs/PROJECT_GUIDE_ROMAN_URDU.md` maujood nahi aur nahi chheri/banayi. Purana .NET/Angular dobara nahi bana.

Safety: asli `SentinelDb` mein koi write nahi (test users 0, incidents 1, koi table write nahi); saare test databases drop; model hashes (`fire_smoke_yolox.onnx`, `yolox_tiny.onnx`, `models.json`) pehle jaise; koi process ya port khula nahi bacha; koi asli SMS/email/push nahi gaya (logs mein "skipped (not configured)").

## 9. Jo abhi verify nahi hua

- Asli RTSP/IP CCTV camera, raat/dhund/steam/IR footage.
- Asli hardware: ESP32 sensors, sprinkler relays.
- Asli SMS, email aur push ka pahunchna.
- Doosre PC se network par dashboard; HTTPS.
- Kai server processes.
- `.\start.ps1` asli `.env` + asli `SentinelDb` ke saath.
- Fix 4, 5 (JavaScript) ka alag automatic test nahi — sirf browser tests ka 10/10 aur code review.

**Nateeja:** local chalane aur FYP demo ke liye project tayyar hai. Production ya asli building ke liye abhi tayyar nahi (safety-certified nahi).

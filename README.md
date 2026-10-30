# FireVision AI — Emergency Monitoring Command Center

Aag aur khatre ki real-time monitoring. Project ki apni AI service (ya koi bhi producer jo wahi JSON bheje) bounding boxes, danger zones aur fire severity bhejti hai, aur FireVision AI unhein CCTV aur drone feeds par live dikhata hai. Saath mein yeh:
- incidents kholta aur escalate karta hai, aur SMS, email aur web-push alerts bhejta hai;
- smart sprinklers khud chalu karta hai;
- camera khamosh ho jaye to **camera lost** alert deta hai;
- calibrated cameras mein dikhne wale logon ko floor plan par rakhta hai aur unki jagah se evacuation route nikalta hai;
- andaza lagata hai ke aag agle kin kamron tak pohanch sakti hai (rule-based, darwazon ke zariye) aur floor plan par risk heatmap dikhata hai.

> **2026-10-26 se project sirf Django par chalta hai.** Purana ASP.NET Core API (`backend/`) aur Angular dashboard (`frontend/`) delete kar diye gaye hain; woh sirf git history mein hain. Kya kahan gaya: [sentinel-django/MIGRATION.md](sentinel-django/MIGRATION.md). Poori audit report: [FINAL_DJANGO_MIGRATION_AUDIT.md](FINAL_DJANGO_MIGRATION_AUDIT.md).

> AI inference ek alag Python service mein chalti hai, [ai-service/](ai-service/README.md), CPU par ONNX Runtime ke saath.
> - **Person detection** (YOLOX-Tiny, Apache-2.0) installed hai; evaluation 2026-10-13: [ai-service/reports](ai-service/reports/README.md).
> - **Fire/smoke detection** (D-Fire par fine-tuned YOLOX-Tiny) installed hai; evaluation 2026-10-15: [ai-service/training](ai-service/training/README.md).
> - Dono model files (`ai-service/models/*.onnx`, takreeban 20 MB har ek) repo mein hain, is liye naye clone ko download ki zaroorat nahi.
>
> Dashboard ka **AI** tab dikhata hai ke kaun se models asal mein load hain.

| Layer | Technology |
|---|---|
| Server (dashboard + API) | Django 5.2, Django REST Framework, `uvicorn` (ASGI), ek hi process — [sentinel-django/](sentinel-django/README.md) |
| Pages | Django templates, plain CSS, vanilla JavaScript (koi build step nahi, run time par Node nahi) |
| Live updates | Server-Sent Events: `GET /api/events` |
| Database | Microsoft SQL Server (`SentinelDb`, schema `sentinel-django/schema/sentinel.sql`), `mssql-django` + ODBC Driver 18 |
| Notifications | Twilio SMS, SMTP email, Web Push (VAPID, `pywebpush`) |
| AI | `ai-service/` (Python, ONNX Runtime, port 8008) |
| Simulator | `sentinel-django/tools/simulator.py`: smoke/temperature sensors, sprinklers, AI frames |

```
AI service / IoT gateway ──(X-Device-Key, REST)──► Django (uvicorn, :5291) ──► Django ORM ──► SQL Server (SentinelDb)
  ai-service/, tools/simulator.py                    │  alert engine → notification worker → SMS / Email / Push
                                                     │  camera watchdog (camera lost, Online/Offline), sprinklers
                                                     ▼
                         Browser ◄── server-rendered pages + /api/events (Server-Sent Events) ── session cookie
```

## Jaldi shuru karna (Windows PowerShell)

Pehle se chahiye: Python 3.13, SQL Server (Developer, Express ya LocalDB) aur **ODBC Driver 18 for SQL Server**.

```powershell
cd C:\Users\m_fahad\Desktop\Project
Copy-Item .env.example .env        # phir Jwt__SigningKey, Device__ApiKey, Device__SensorApiKey (alag key), Seed__AdminPassword bharein

# Django server (ek dafa setup)
cd sentinel-django
py -3.13 -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
# Sirf bilkul khali naye database ke liye: .\.venv\Scripts\python manage.py create_schema

# AI service (ek dafa setup), dekhein ai-service/README.md
cd ..\ai-service
py -3.13 -m venv .venv; .\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python tools\download_models.py

# Chalana (project folder se): Server + AI service, har ek apni window mein
cd ..
.\start.ps1                 # aag ka demo bhi chahiye to: .\start.ps1 -Simulator
```

Takreeban 30 second baad http://localhost:5291 kholein. `start.ps1` `.env` se `Device__ApiKey` parh kar AI service ko deta hai, aur `-Simulator` ke saath `Device__SensorApiKey` bhi (yeh khali ho to simulator start nahi hota). Purani guides ka `.\start.ps1 -Django` ab bhi chalta hai.

`Seed__AdminEmail` aur `Seed__AdminPassword` se login karein. Admin account server ke pehle start par banta hai, jab Users table khali ho (password khali ho to ek random password server window mein ek dafa print hota hai).
- Admin **Devices** page par customer ke apne cameras add, edit aur delete karta hai. Stream passwords dashboard users ko `***` dikhte hain; asli link sirf AI service ko jata hai.
- `SENTINEL_DEVICE_KEY` set ho to AI service har 60 s server se camera list dobara parhti hai, is liye naya camera ek minute ke andar analyse hone lagta hai.
- IP camera ke baghair live test: `ai-service\tools\mjpeg_camera.py 0` laptop webcam (ya video file) ko MJPEG bana kar `http://127.0.0.1:8090/video` par deta hai; Devices page par format Mjpeg ke saath add karein.
- `ai-service\tools\live_test.py` poori chain khud chalata hai (`samples/fire-test.avi`, D-Fire test frames, CC0): camera, floor plan, calibration, AI fire incident, danger zone, naya evacuation route aur risk heatmap, phir PASS/FAIL. `.env` mein asli SMS ya email settings hon to yeh chalne se inkaar karta hai.

**Zaroori:** `SentinelDb` par kabhi `manage.py migrate` mat chalayein (database router waise bhi rok deta hai). Django models unmanaged hain; schema nahi badalta.

## Do PCs par wahi database (sirf private repo)

Database git ke zariye ek backup file ki shakal mein jata hai: `database/SentinelDb.bak`. Is mein login user aur har record hai, is liye repo private rakhein.

```powershell
# PC 1: kaam khatam karte waqt
.\database\save.ps1
git add database\SentinelDb.bak; git commit -m "db: update"; git push

# PC 2: kaam shuru karte waqt (pehle server band karein; yeh local database ko OVERWRITE karta hai)
git pull
.\database\load.ps1
```

- Aakhri `save` jeetta hai. Do syncs ke darmiyan dono PCs par data mat badlein.
- Doosre PC par SQL Server 2022 ya naya chahiye, kyunke backup purane version par restore nahi hota.
- PowerShell scripts block kare to: `powershell -ExecutionPolicy Bypass -File .\database\save.ps1`.

## Tests

Saare Django tests disposable `test_*` databases par chalte hain, `.env` nahi parhte, aur kisi asli camera, relay ya notification service tak nahi pohanchte.

```powershell
cd C:\Users\m_fahad\Desktop\Project\sentinel-django
.\.venv\Scripts\python manage.py test --noinput      # Django unit/API tests (test_SentinelDb_django, baad mein drop)
cd e2e; npm install --offline; cd ..                 # ek dafa: browser tests ke liye playwright-core (installed Edge use karta hai)
.\.venv\Scripts\python e2e\run.py                    # browser tests: asli server + test_SentinelDb_e2e + stub AI
.\.venv\Scripts\python e2e\lifecycle.py              # asli AI service + models + test camera: camera lifecycle (~6 min)
cd ..\ai-service; .\.venv\Scripts\python -m unittest discover -s tests   # AI service (mock-based)
```

Taaza nateeje (2026-10-28): Django **79/79**, browser **10/10**, lifecycle 3 runs: **15/18, 18/18, 18/18** (pehle run mein asli AI ne 150 s ke andar aag confirm nahi ki; wajah report mein). AI service ke unit tests **25/25**. Tafseel: [FINAL_DJANGO_MIGRATION_AUDIT.md](FINAL_DJANGO_MIGRATION_AUDIT.md).

> Tareekh: 2026-10-21 ko purane stack par yeh pass hue thay: backend 74, frontend 32, AI service 25, browser 9. Woh stack ab delete ho chuka hai.

Model evaluation tools `ai-service/tools` mein hain:
- `evaluate.py`: labelled set par precision, recall, F1, AP50, AP50:95 aur latency.
- `degrade.py`: masnooi andhera aur dhund.
- `enhance.py`: AI se pehle tajurbati contrast enhancement; live service isay istemal nahi karti.
- `coco_to_yolo.py`: COCO labels ko us format mein jo `evaluate.py` parhta hai.

Nateeje aur poori AI audit: [ai-service/reports](ai-service/reports/README.md).

## Documentation

- **[FINAL_DJANGO_MIGRATION_AUDIT.md](FINAL_DJANGO_MIGRATION_AUDIT.md)**: migration ki aakhri audit — kya delete hua, kya test hua, kya baqi hai.
- **[sentinel-django/README.md](sentinel-django/README.md)**: server ka setup, configuration, API, camera lifecycle, tests aur limits.
- **[sentinel-django/MIGRATION.md](sentinel-django/MIGRATION.md)**: purane .NET/Angular stack ki har cheez Django mein kahan gayi (historical record).
- **[docs/FIREVISION_AI_GUIDE_ROMAN_URDU_v5.html](docs/FIREVISION_AI_GUIDE_ROMAN_URDU_v5.html)** (PDF bhi): poori project guide — camera se dashboard tak flow, alerts, sensors, sprinklers, floor plans, evacuation, database, security, API, AI models aur unki training/accuracy. Screenshots dekhne ke liye `docs` folder se kholein.
- **[docs/FIREVISION_AI_USER_GUIDE_ROMAN_URDU.html](docs/FIREVISION_AI_USER_GUIDE_ROMAN_URDU.html)** (PDF bhi): non-technical users ke liye qadam ba qadam — install, start, login, dashboard, hands-on tests, "Safe Test Mode" aur troubleshooting.
- **[docs/FIREVISION_AI_FEATURES_GUIDE_ROMAN_URDU.html](docs/FIREVISION_AI_FEATURES_GUIDE_ROMAN_URDU.html)**: har feature kaise istemal karein.
- **[docs/FIREVISION_AI_HARDWARE_GUIDE_ROMAN_URDU.html](docs/FIREVISION_AI_HARDWARE_GUIDE_ROMAN_URDU.html)**: asli hardware jorna — RTSP cameras, ESP32 sensors (`/api/iot/telemetry`), sprinkler relays (`/api/iot/sprinklers`), Twilio, SMTP aur Web Push.

Guides ki zyada tar tasveerein naye Django dashboard ki hain (2026-10-28); 3 abhi purane Angular dashboard ki hain (list `FINAL_DJANGO_MIGRATION_AUDIT.md` mein).

**Production-ready ya safety-certified nahi.** Asli RTSP CCTV, asli hardware aur notification channels abhi test nahi hue.

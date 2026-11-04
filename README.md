# FireVision AI — Emergency Monitoring Command Center

Aag aur khatre ki real-time monitoring. Project ki apni AI service (ya koi bhi producer jo wahi JSON bheje) bounding boxes, danger zones aur fire severity bhejti hai, aur FireVision AI unhein CCTV aur drone feeds par live dikhata hai. Saath mein yeh:
- aag aur dhuwan (smoke) **alag alag** pehchanta hai: sirf dhuwan bhi apna incident kholta hai (SMOKE tag);
- incidents kholta aur escalate karta hai, aur alerts bhejta hai: SMS (Twilio), GSM modem SMS (fallback), WhatsApp, Telegram, Firebase push, email aur web push;
- smart sprinklers khud chalu karta hai;
- camera khamosh ho jaye to **camera lost** alert deta hai;
- calibrated cameras mein dikhne wale logon ko floor plan par rakhta hai aur unki jagah se evacuation route nikalta hai;
- andaza lagata hai ke aag agle kin kamron tak pohanch sakti hai (rule-based, darwazon ke zariye) aur floor plan par risk heatmap dikhata hai;
- **Occupancy & Heatmap** page: har kamre mein kitne log hain (Live / Stale / Unknown, kabhi nakli 0 nahi) aur logon ka purple heatmap (fire risk layer se alag);
- **crowd alert**: kamre ya exit par "Max people" se zyada log hon to ek "Crowd" incident, aur safe doosre exit ka mashwara;
- sensors floor plan par apni live haalat ke rang mein dikhte hain;
- **Reports & Analytics** par model accuracy: precision, recall, F1, AP50 (labelled images par offline evaluation);
- **AI Settings** par har model ki sensitivity (detection threshold 0.25–0.55) slider se badli ja sakti hai (sirf Admin).

> **2026-10-26 se project sirf Django par chalta hai.** Purana ASP.NET Core API (`backend/`) aur Angular dashboard (`frontend/`) delete kar diye gaye hain; woh sirf git history mein hain. Kya kahan gaya: [sentinel-django/MIGRATION.md](sentinel-django/MIGRATION.md). Poori audit report: [FINAL_DJANGO_MIGRATION_AUDIT.md](FINAL_DJANGO_MIGRATION_AUDIT.md).

> AI inference ek alag Python service mein chalti hai, [ai-service/](ai-service/README.md), CPU par ONNX Runtime ke saath.
> - **Person detection** (YOLOX-Tiny, Apache-2.0) installed hai; evaluation 2026-10-13: [ai-service/reports](ai-service/reports/README.md).
> - **Fire/smoke detection** (D-Fire par fine-tuned YOLOX-Tiny) installed hai; evaluation 2026-10-15: [ai-service/training](ai-service/training/README.md).
> - Dono model files (`ai-service/models/*.onnx`, takreeban 20 MB har ek) repo mein hain, is liye naye clone ko download ki zaroorat nahi.
>
> Dashboard ka **AI Settings** page dikhata hai ke kaun se models asal mein load hain.

| Layer | Technology |
|---|---|
| Server (dashboard + API) | Django 5.2, Django REST Framework, `uvicorn` (ASGI), ek hi process — [sentinel-django/](sentinel-django/README.md) |
| Pages | Django templates, plain CSS, vanilla JavaScript (koi build step nahi, run time par Node nahi) |
| Live updates | Server-Sent Events: `GET /api/events` |
| Database | Microsoft SQL Server (`SentinelDb`, schema `sentinel-django/schema/sentinel.sql`), `mssql-django` + ODBC Driver 18 |
| Notifications | Twilio SMS, GSM modem SMS fallback (`pyserial`), Twilio WhatsApp, Telegram bot, Firebase Cloud Messaging (`google-auth`), SMTP email, Web Push (VAPID, `pywebpush`). Background worker; har koshish incident history aur **Notifications** page par |
| AI | `ai-service/` (Python, ONNX Runtime, port 8008) |
| Simulator | `sentinel-django/tools/simulator.py`: smoke/temperature sensors, sprinklers, AI frames |

```
AI service / IoT gateway ──(X-Device-Key, REST)──► Django (uvicorn, :5291) ──► Django ORM ──► SQL Server (SentinelDb)
  ai-service/, tools/simulator.py                    │  alert engine → notification worker → SMS / GSM / WhatsApp / Telegram / Firebase / Email / Push
                                                     │  camera watchdog (camera lost, Online/Offline), sprinklers, occupancy + crowd alerts
                                                     ▼
                         Browser ◄── server-rendered pages + /api/events (Server-Sent Events) ── session cookie
```

## Jaldi shuru karna (naya Windows PC)

Pehle se chahiye:
- Windows 10/11, **Python 3.13 64-bit** (`py` launcher ke saath)
- **SQL Server 2022** (Developer ya Express); SQL Server Management Studio marzi
- **ODBC Driver 18 for SQL Server**
- Git (ya project ka zip)
- Microsoft Edge aur Node.js sirf browser tests ke liye

```powershell
git clone git@github.com:Fahad082003/FYP-Fire-Detection-System.git   # ya project folder copy karein
cd FYP-Fire-Detection-System                                           # project folder mein PowerShell kholein
.\setup.ps1
```

`setup.ps1` Python check karta hai, dono `.venv` bana kar requirements install karta hai (pehle poochta hai), ODBC check karta hai, `.env.example` se `.env` banata hai (pehle poochta hai), aur SQL Server aur model files check karta hai. Jab tak sab OK na ho, dobara chalayein. Scripts block hon to: `powershell -ExecutionPolicy Bypass -File .\setup.ps1`.

Phir `.env` mein bharein: `Jwt__SigningKey` (32+ characters), `Device__ApiKey`, `Device__SensorApiKey` (alag key), `Seed__AdminEmail`, `Seed__AdminPassword`, `ConnectionStrings__Sentinel`. Secrets kabhi commit na karein.

Database, do mein se ek:

```powershell
.\database\load.ps1     # shared copy restore karta hai (local SentinelDb OVERWRITE hota hai; SQL Server 2022+)
# ya sirf bilkul khali naye database ke liye:
sentinel-django\.venv\Scripts\python sentinel-django\manage.py create_schema
```

Chalana aur band karna:

```powershell
.\start.ps1              # supervisor window kholta hai, Django aur AI models ready hone tak intezar karta hai
.\start.ps1 -Simulator   # saath mein nakli sensors/frames (aag ka demo)
.\stop.ps1               # sab band
```

http://localhost:5291 kholein. `start.ps1` `.env` se `Device__ApiKey` parh kar AI service ko deta hai, aur `-Simulator` ke saath `Device__SensorApiKey` bhi (yeh khali ho to simulator start nahi hota). Purani guides ka `.\start.ps1 -Django` ab bhi chalta hai.

Marzi installs, **sirf** agar woh channel istemal karna ho:

```powershell
sentinel-django\.venv\Scripts\python -m pip install google-auth   # Firebase push
sentinel-django\.venv\Scripts\python -m pip install pyserial      # GSM modem SMS (Optional / Legacy, current scope mein nahi)
sentinel-django\.venv\Scripts\python -m pip install pywebpush     # Web Push
```

Doosra tareeqa: haath se setup (`setup.ps1` ke baghair):

```powershell
Copy-Item .env.example .env        # phir upar wali values bharein

# Django server (ek dafa setup)
cd sentinel-django
py -3.13 -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
# Sirf bilkul khali naye database ke liye: .\.venv\Scripts\python manage.py create_schema

# AI service (ek dafa setup), dekhein ai-service/README.md
cd ..\ai-service
py -3.13 -m venv .venv; .\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python tools\download_models.py   # sirf agar models\ mein koi file na ho

# Chalana (project folder se)
cd ..
.\start.ps1
```

`Seed__AdminEmail` aur `Seed__AdminPassword` se login karein. Admin account server ke pehle start par banta hai, jab Users table khali ho (password khali ho to ek random password server window mein ek dafa print hota hai).
- Admin **Settings** page par customer ke apne cameras add, edit aur delete karta hai. Stream passwords dashboard users ko `***` dikhte hain; asli link sirf AI service ko jata hai.
- `SENTINEL_DEVICE_KEY` set ho to AI service har 60 s server se camera list dobara parhti hai, is liye naya camera ek minute ke andar analyse hone lagta hai.
- IP camera ke baghair live test: `ai-service\tools\mjpeg_camera.py 0` laptop webcam (ya video file) ko MJPEG bana kar `http://127.0.0.1:8090/video` par deta hai; Settings page par format Mjpeg ke saath add karein.
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

Saare tests disposable `test_*` databases par chalte hain, `.env` nahi parhte, aur kisi asli camera, relay ya notification service tak nahi pohanchte (koi asli message nahi jata).

```powershell
cd C:\Users\m_fahad\Desktop\Project\sentinel-django
.\.venv\Scripts\python manage.py test --noinput      # Django unit/API tests (test_SentinelDb_django, baad mein drop)
cd e2e; npm install --offline; cd ..                 # ek dafa: browser tests ke liye playwright-core (installed Edge use karta hai)
.\.venv\Scripts\python e2e\run.py                    # browser tests: asli server + test_SentinelDb_e2e + stub AI
.\.venv\Scripts\python e2e\lifecycle.py              # asli AI service + models + test camera: camera lifecycle (~6 min)
cd ..\ai-service; .\.venv\Scripts\python -m unittest discover -s tests   # AI service (mock tests + asli model tests)
```

Taaza nateeje (2026-11-04): Django **288/288**, AI service **57/57** (asli model tests samet), browser e2e **31/31**, lifecycle **18/18** (asli AI, asli models, MJPEG test camera). Yeh ek run ka nateeja hai, reliability ki guarantee nahi.

> Tareekh: 2026-10-21 ko purane stack par yeh pass hue thay: backend 74, frontend 32, AI service 25, browser 9. Woh stack ab delete ho chuka hai.

Model evaluation tools `ai-service/tools` mein hain:
- `evaluate.py`: labelled set par precision, recall, F1, AP50, AP50:95 aur latency.
- `false_alarm_eval.py`: asli models ke saath false-alarm review (nateeja: `ai-service/reports/2026-11-04-false-alarm-review/review.json`).
- `degrade.py`: masnooi andhera aur dhund.
- `enhance.py`: AI se pehle tajurbati contrast enhancement; live service isay istemal nahi karti.
- `coco_to_yolo.py`: COCO labels ko us format mein jo `evaluate.py` parhta hai.

`evaluate.py` ki nayi run `--out` se `ai-service/reports` ke andar save ho to woh khud **Reports & Analytics** ke "Model accuracy" hisse mein aa jati hai. Incident totals accuracy nahi hain.

Nateeje aur poori AI audit: [ai-service/reports](ai-service/reports/README.md).

## Documentation

- **[FINAL_DJANGO_MIGRATION_AUDIT.md](FINAL_DJANGO_MIGRATION_AUDIT.md)**: migration ki aakhri audit — kya delete hua, kya test hua, kya baqi hai.
- **[sentinel-django/README.md](sentinel-django/README.md)**: server ka setup, configuration, API, camera lifecycle, tests aur limits.
- **[sentinel-django/MIGRATION.md](sentinel-django/MIGRATION.md)**: purane .NET/Angular stack ki har cheez Django mein kahan gayi (historical record).
- **[docs/FIREVISION_AI_GUIDE_ROMAN_URDU_v5.html](docs/FIREVISION_AI_GUIDE_ROMAN_URDU_v5.html)**: poori project guide — camera se dashboard tak flow, alerts, sensors, sprinklers, floor plans, evacuation, database, security, API, AI models aur unki training/accuracy. Screenshots dekhne ke liye `docs` folder se kholein.
- **[docs/FIREVISION_AI_USER_GUIDE_ROMAN_URDU.html](docs/FIREVISION_AI_USER_GUIDE_ROMAN_URDU.html)**: non-technical users ke liye qadam ba qadam — install, start, login, dashboard, hands-on tests, "Safe Test Mode" aur troubleshooting.
- **[docs/FIREVISION_AI_FEATURES_GUIDE_ROMAN_URDU.html](docs/FIREVISION_AI_FEATURES_GUIDE_ROMAN_URDU.html)**: har feature kaise istemal karein.
- **[docs/FIREVISION_AI_HARDWARE_GUIDE_ROMAN_URDU.html](docs/FIREVISION_AI_HARDWARE_GUIDE_ROMAN_URDU.html)**: asli hardware jorna — RTSP cameras, ESP32 sensors (`/api/iot/telemetry`), sprinkler relays (`/api/iot/sprinklers`), sensors ko naqshe par rakhna, aur saare alert channels ka setup: Twilio SMS, GSM modem, WhatsApp, Telegram, SMTP, Web Push aur Firebase.

Guides 2026-11-04 ko naye UI ke mutabiq update hui hain (Overview, Live Cameras, Floor Plan, Occupancy & Heatmap, Incidents, Sensors, Sprinklers, Activity Log, Reports & Analytics, AI Settings, Notifications, Settings). Tasveerein `docs/images` mein hain aur simulated test data dikhati hain.

**Production-ready ya safety-certified nahi.** Abhi test nahi hue: asli RTSP CCTV, asli sensors aur relays, asli Twilio SMS/WhatsApp, Telegram, Firebase aur SMTP delivery, GSM modem hardware, asli sunset/steam/vape footage (false alarms), aur tablet layout. Koi mobile app nahi. Push ke snapshot links sirf wahan chalte hain jahan se dashboard ka address pohanch mein ho.

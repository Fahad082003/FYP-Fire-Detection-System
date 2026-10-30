# sentinel-ai: AI inference service

Ek alag Python process jo CPU par ONNX models chalata hai aur nateeje Django server (`sentinel-django/`) ko bhejta hai. Server khud kabhi inference nahi karta, is liye dheema model kabhi server ki requests ko nahi rokta.

```
camera / video / test image ─► sentinel_ai (ONNX Runtime, CPU)
   preprocess (letterbox) → YOLOX inference → decode + confidence filter + NMS
   → IoU tracking (track ids) → N frames tak fire/smoke confirmation → fire-box area % se severity
   → confirmed aag ke gird AI danger zones → detection-density heatmap → har detection ki explanation
   ─► POST /api/detections (X-Device-Key) ─► Django server: validation, danger-zone mein insaan ka check,
      incidents + dedupe, IncidentLogs, notifications, sprinkler rules, live updates (/api/events, SSE) → dashboard
```

> Tareekh: 2026-10-26 se pehle yahi frames ASP.NET Core API ko jate thay aur dashboard Angular tha (SignalR). Ab dono ki jagah Django hai; is service ka code nahi badla.

## Models
| Naam | File | Kahan se | License | Haalat |
|---|---|---|---|---|
| person | `yolox_tiny.onnx` (COCO, sirf class `person`) | https://github.com/Megvii-BaseDetection/YOLOX/releases/tag/0.1.1rc0 | Apache-2.0 | Repo mein hai. Ghayab ho to `tools/download_models.py` official URL se la kar sha256 check karta hai |
| fire_smoke | `fire_smoke_yolox.onnx` | Khud fine-tune karein: [training/README.md](training/README.md) (D-Fire, CC0) | YOLOX Apache-2.0 + D-Fire CC0 | 2026-10-15 ko install hua (YOLOX-Tiny @ 416, D-Fire par trained). Repo mein hai; checkpoint se `training/export_onnx.py` dobara export kar sakta hai. Metrics: [reports/README.md](reports/README.md). Iska koi download URL nahi |

Aag phailne ka andaza lagane wala koi model nahi (requirement 3.3). Heatmap sirf yeh dikhata hai ke aag/dhuwan **kahan dekha gaya**; yeh nahi batata ke aag kahan jayegi. (Kamron tak aag pohanchne ka rule-based andaza Django server ka "risk map" karta hai.)

## Hardware
- **Sirf CPU.** Xeon W3520 (AVX ke baghair) par check hua: numpy 2.5.4, opencv-python-headless 5.0.0.93 aur onnxruntime 1.31.0 chalte hain.
- Takreeban 0.5 GB RAM.
- Inference ke liye GPU nahi chahiye. Training ke liye CUDA GPU chahiye.

## Install aur chalana (Windows PowerShell)
```powershell
cd C:\Users\m_fahad\Desktop\Project\ai-service
py -3.13 -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python tools\download_models.py         # sirf agar models\ mein koi file na ho (official URLs, sha256 check)
Copy-Item config.example.json config.json               # marzi: config.json mein apne cameras; secrets env vars mein rakhein
$env:SENTINEL_DEVICE_KEY = "<project .env ka Device__ApiKey>"
.\.venv\Scripts\python -m sentinel_ai                   # http://127.0.0.1:8008/health
```
Aam taur par yeh sab project folder ka `.\start.ps1` khud karta hai (Server + AI service; device key `.env` se leta hai).

- Django server AI service ka address `.env` ke `AiService__BaseUrl` se leta hai (default `http://localhost:8008`).
- `SENTINEL_DEVICE_KEY` set ho to service har 60 s server se camera list (`GET /api/cameras`) parhti hai; dashboard ke **Devices** page par add kiya camera ek minute ke andar analyse hone lagta hai. `config.json` ki zaroorat sirf extra cameras ke liye hai.
- Dashboard ke **AI** page aur upar wale banner mein service ki haalat dikhti hai: hara = sab models load, peela = koi model ghayab, laal = offline.

## HTTP endpoints
Service sirf 127.0.0.1 par sunti hai, aur sirf Django server ise call karta hai.

**`GET /health`**
```json
{ "service": "sentinel-ai", "status": "degraded",
  "models": [ { "name": "person", "loaded": true, "error": null },
              { "name": "fire_smoke", "loaded": false, "error": "model file not found: ..." } ],
  "capabilities": ["Human"],
  "inference": { "count": 3, "avgMs": 410.2 },
  "cameras": [] }
```

**`POST /infer?cameraId=upload-test`** (raw PNG/JPG body, zyada se zyada 8 MB)
- Kamyabi:
  ```json
  { "payload": { "cameraId": "upload-test", "detections": [ { "category": "Human", "confidence": 0.87, "bbox": [x, y, w, h], "trackId": null, "explanation": "person 0.87 >= threshold 0.45 (person); box 6.1% of frame; single image: no multi-frame confirmation" } ],
                 "fireDetected": false, "fireSeverity": null, "model": "person:yolox_tiny fire_smoke:missing", "simulated": true, ... },
    "inferenceMs": 412.7 }
  ```
- Errors:
  - `400` jab image decode na ho.
  - `503` jab koi model load na ho.
  - `500` jab model aisa output de jo yeh code parh na sake.

## Camera sources (`config.json`)
| `source` | Kya samjha jata hai | `simulated` |
|---|---|---|
| `rtsp://…`, `http://…` (credentials ke liye `${ENV_VAR}`) | live stream (FFmpeg): open ya frame 10 s mein na aaye to release aur reconnect, backoff 1→30 s. RTSP pehle TCP, phir UDP | jo aap set karein (default `false`) |
| `0`, `1`, … | local webcam | jo aap set karein |
| video file ka path | **test input** | **hamesha `true`**, config kuch bhi kahe |

Har camera ke frames `fps` par process hote hain (default 2), ek waqt mein ek inference. Is CPU par kul takreeban 1 frame/s ka hisaab rakhein. Zyada cameras par har camera ke frames barabar kam hote hain (purane frames skip hote hain, queue nahi). RTSP ko sirf TCP ya UDP par majboor karna ho to service start karne **se pehle** `OPENCV_FFMPEG_CAPTURE_OPTIONS=rtsp_transport;tcp` set karein (chalte process mein set karne ka asar nahi hota).

`cameraId` server ke database ke kisi camera se milna chahiye. Usi camera ka zone tay karta hai ke kaun sa incident, sprinklers aur floor-plan danger zones mutasir honge. Yeh service footage kabhi save nahi karti.

## Safety behaviour
- **Fire/smoke confirmation:** aag aur dhuwan tab report hote hain jab wahi track `confirm_frames` frames mein dikhe (default 3). Akeli test images foran report hoti hain aur un par yeh likha hota hai.
- **Fire severity** fire boxes ke frame mein hisse se: 2% se kam Low, 2% Medium, 10% High, 25% Critical (configurable). Yeh screen par size hai, asli size nahi, is liye har camera ke liye calibrate karein.
- **AI danger zones:** har confirmed aag ek Danger zone ban jati hai (box 25% bara). Server ka apna foot-point check phir aag mein ya qareeb khare logon ko flag karta hai.
- **Ghayab ya badla hua model** (sha256 match na ho): `/health` aur dashboard mein dikhta hai, aur kabhi nakli output se replace nahi hota. Rollback ka matlab `.onnx` file hatana hai.

## Tests aur evaluation
```powershell
.\.venv\Scripts\python -m unittest discover -s tests          # MOCK-BASED: sirf contract/logic, accuracy ka koi daawa nahi
.\.venv\Scripts\python tools\prepare_dataset.py pennfudan <PennFudanPed> ..\..\datasets\pennfudan
.\.venv\Scripts\python tools\evaluate.py --data <yolo dataset> --classes 0=Human --out reports\person   # ASLI model metrics
```
Asli models aur Django server ke saath poora live test: `sentinel-django\e2e\lifecycle.py` (dekhein [../sentinel-django/README.md](../sentinel-django/README.md)).
Acceptance criteria aur fire/smoke fine-tuning pipeline: [training/README.md](training/README.md).

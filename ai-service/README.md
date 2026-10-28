# sentinel-ai: AI inference service

A separate Python process that runs ONNX models on CPU and feeds the existing Sentinel API. The .NET API never runs inference itself, so request handlers are never blocked by slow models.

```
camera / video / test image ─► sentinel_ai (ONNX Runtime, CPU)
   preprocess (letterbox) → YOLOX inference → decode + confidence filter + NMS
   → IoU tracking (track ids) → fire/smoke confirmation over N frames → severity from fire-box area %
   → AI danger zones around confirmed fire → detection-density heatmap → explanation per detection
   ─► POST /api/detections (X-Device-Key)  ─► existing pipeline: validation, danger-zone person check,
      incidents + dedupe, IncidentLogs, notifications, sprinkler rules, SignalR → Angular dashboard
```

## Models
| Name | File | Source | License | Status |
|---|---|---|---|---|
| person | `yolox_tiny.onnx` (COCO, class `person` only) | https://github.com/Megvii-BaseDetection/YOLOX/releases/tag/0.1.1rc0 | Apache-2.0 | In the repo. If it is missing, `tools/download_models.py` fetches it from the official URL and checks its sha256 |
| fire_smoke | `fire_smoke_yolox.onnx` | Fine-tune it yourself: see [training/README.md](training/README.md) (D-Fire, CC0) | YOLOX Apache-2.0 + D-Fire CC0 | Installed 2026-10-15 (YOLOX-Tiny @ 416, trained on D-Fire). In the repo; it can be re-exported from the checkpoint with training/export_onnx.py. Metrics: [reports/README.md](reports/README.md) |

There is no fire-spread prediction model (requirement 3.3). The heatmap is a decaying map of **where fire/smoke was detected**. It is not a prediction of where fire will go.

## Hardware
- **CPU only.** Verified on a Xeon W3520 without AVX: numpy 2.5.4, opencv-python-headless 5.0.0.93 and onnxruntime 1.31.0 all run.
- About 0.5 GB RAM.
- GPU: none needed for inference. Training needs a CUDA GPU.

## Install and run (Windows PowerShell)
```powershell
cd C:\Users\m_fahad\Desktop\Project\ai-service
py -3.13 -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python tools\download_models.py         # only if models\ is missing a file (official URLs, sha256 checked)
Copy-Item config.example.json config.json               # then enable cameras; keep secrets in env vars
$env:SENTINEL_DEVICE_KEY = "<same value as Device__ApiKey in the API .env>"   # only needed when cameras are enabled
.\.venv\Scripts\python -m sentinel_ai                   # http://127.0.0.1:8008/health
```
The API reads `AiService:BaseUrl` (default `http://localhost:8008`). The dashboard shows the service state in the **AI** tab: green means all models are loaded, amber means a model is missing, red means offline.

## HTTP endpoints
The service binds to 127.0.0.1 only, and only the API calls it.

**`GET /health`**
```json
{ "service": "sentinel-ai", "status": "degraded",
  "models": [ { "name": "person", "loaded": true, "error": null },
              { "name": "fire_smoke", "loaded": false, "error": "model file not found: ..." } ],
  "capabilities": ["Human"],
  "inference": { "count": 3, "avgMs": 410.2 },
  "cameras": [] }
```

**`POST /infer?cameraId=upload-test`** (raw PNG/JPG body, at most 8 MB)
- Success:
  ```json
  { "payload": { "cameraId": "upload-test", "detections": [ { "category": "Human", "confidence": 0.87, "bbox": [x, y, w, h], "trackId": null, "explanation": "person 0.87 >= threshold 0.45 (person); box 6.1% of frame; single image: no multi-frame confirmation" } ],
                 "fireDetected": false, "fireSeverity": null, "model": "person:yolox_tiny fire_smoke:missing", "simulated": true, ... },
    "inferenceMs": 412.7 }
  ```
- Errors:
  - `400` when the image cannot be decoded.
  - `503` when no model is loaded.
  - `500` when the model returns output this code cannot read.

## Camera sources (`config.json`)
| `source` | Treated as | `simulated` |
|---|---|---|
| `rtsp://…`, `http://…` (with `${ENV_VAR}` for credentials) | live stream, reconnects with backoff | whatever you set (default `false`) |
| `0`, `1`, … | local webcam | whatever you set |
| path to a video file | **test input** | **always `true`**, whatever the config says |

Frames are processed at `fps` per camera (default 2), with one inference at a time. On this CPU, budget about 1 frame/s in total.

`cameraId` must match a camera in the API database. That camera's zone decides which incident, sprinklers and floor-plan danger zones are affected. Footage is never stored by this service.

## Safety behaviour
- **Fire/smoke confirmation:** fire and smoke are reported only after the same track has been seen in `confirm_frames` frames (default 3). Single test images are reported immediately and labelled as such.
- **Fire severity** comes from the fire boxes' share of the frame: below 2% Low, 2% Medium, 10% High, 25% Critical (configurable). This is on-screen size, not physical size, so calibrate it per camera.
- **AI danger zones:** each confirmed fire becomes a Danger zone (its box grown by 25%). The API's own foot-point check then flags people standing in or near the fire.
- **Missing or tampered model** (sha256 mismatch): it is reported in `/health` and the dashboard and never replaced by fake output. Rollback means removing the `.onnx` file.

## Tests and evaluation
```powershell
.\.venv\Scripts\python -m unittest discover -s tests          # MOCK-BASED: contract/logic only, no accuracy claims
.\.venv\Scripts\python tools\prepare_dataset.py pennfudan <PennFudanPed> ..\..\datasets\pennfudan
.\.venv\Scripts\python tools\evaluate.py --data <yolo dataset> --classes 0=Human --out reports\person   # REAL model metrics
```
See [training/README.md](training/README.md) for the acceptance criteria and the fire/smoke fine-tuning pipeline.

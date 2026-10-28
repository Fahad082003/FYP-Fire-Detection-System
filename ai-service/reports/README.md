# Evaluation reports (measured, not estimated)

> **Historical note (Roman Urdu):** yeh reports 2026-10-13 se 2026-10-19 ke darmiyan bani theen, jab server ASP.NET Core API tha aur dashboard Angular (SignalR). Model ke measurements ab bhi sahi hain (models nahi badle); lekin "API", "SignalR" aur ports ka zikr purane stack ka hai. 2026-10-26 se server Django hai (`sentinel-django/`); taaza end-to-end test: `sentinel-django\e2e\lifecycle.py` aur [../../FINAL_DJANGO_MIGRATION_AUDIT.md](../../FINAL_DJANGO_MIGRATION_AUDIT.md).

**Latest: [2026-10-19 full AI and end-to-end audit](2026-10-19-AUDIT.md)**. It adds AP50 and AP50:95, p99 latency, per-image predictions (predictions.jsonl), 95% confidence intervals, live failure probes, a 1- and 3-camera load test, and a dependency scan. Section I adds a **synthetic** darkness/fog stress test.

## 2026-10-13: person model `yolox_tiny.onnx` (official YOLOX COCO weights, sha256 427cc366…) on Penn-Fudan
- **Data:** Penn-Fudan Pedestrian Database (https://www.cis.upenn.edu/~jshi/ped_html/), 170 images, 423 labelled pedestrians.
  - Every image contains people, so there are **no negative images** and the false-positive rate on empty scenes was **not measured**.
- **Hardware:** Xeon W3520 (no AVX), CPU only, ONNX Runtime 1.31.0. Command:
  `python tools/evaluate.py --data <pennfudan> --classes 0=Human --sweep 0.25,0.35,0.45,0.55`
- Full numbers: [2026-10-13-person-yolox_tiny-pennfudan.json](2026-10-13-person-yolox_tiny-pennfudan.json).

| Threshold | TP | FP | FN | Precision | Recall | F1 |
|---|---|---|---|---|---|---|
| 0.25 | 420 | 260 | 3 | 0.618 | 0.993 | 0.762 |
| 0.35 | 420 | 214 | 3 | 0.663 | 0.993 | 0.795 |
| **0.45 (configured)** | 420 | 174 | 3 | 0.707 | 0.993 | 0.826 |
| 0.55 | 419 | 134 | 4 | 0.758 | 0.991 | 0.859 |

**Latency per image:** mean 129.0 ms, p50 127.1 ms, p95 138.6 ms.

**Failure analysis:** the 6 images with the most false positives were inspected visually. The red boxes are almost all real people that Penn-Fudan does not label: cyclists, background and partly hidden pedestrians. The measured precision is therefore a **lower bound** caused by incomplete labels, not a confirmed model weakness. The threshold was **not** raised, because recall (missed people) matters more here.

**Decision:** *Evaluated: improvement required.*
- Recall (≥ 0.85) and latency (p95 ≤ 1000 ms) **meet** the criteria.
- Precision (≥ 0.80) is **not confirmed**. The next step is a fully labelled set that includes negatives (for example the COCO val2017 person subset, or authorised site CCTV), not retraining.
- Not tested: night/IR footage, fog, heavy crowds, top-down CCTV angles.

## Fire/smoke model
See the 2026-10-15 GPU-trained entry at the end. That model is installed as `fire_smoke_yolox.onnx`.

## 2026-10-13: fire/smoke CPU pilot (YOLOX-Nano @ 320) on the official D-Fire TEST split
**Training actually ran.** It was a CPU pilot, deliberately small, because a full fine-tune on this CPU would take days (see ../training/README.md).

**Data:**
- Official D-Fire.zip (CC0), 3,036,222,313 bytes, sha256 3824fb3ce32cfa8b…
- Validated: train 17,221 images (7,833 negatives), test 4,306 images (2,005 negatives).
  - 0 identical images across the splits.
  - 26 zero-area labels were dropped and counted.
- Pilot subset: 4,000 train and 800 validation images, taken from the train split in groups of 100 consecutive frames. The test split was used only for this final score.

**Training:**
- Start point: official COCO `yolox_nano.pth` (sha256 cd28f55f…, loaded tensors-only).
- Run 1: lr 0.00125, full model, 2 epochs. Loss 9.75 → 9.39 with no detections. Stopped.
- Run 2: lr 0.01, frozen backbone, epochs 1–4 of 8 completed. **Paused** at the user's request and never resumed. The pilot script was later removed; a copy is in the `ai-service.zip` uploaded to Kaggle.
- The best validation checkpoint (epoch 3) was scored. Full numbers: [2026-10-13-fire_smoke-cpu-pilot-dfire-test.json](2026-10-13-fire_smoke-cpu-pilot-dfire-test.json).

| Threshold | Fire P / R / F1 | Fire image-level FPR | Smoke P / R / F1 | Smoke image-level FPR |
|---|---|---|---|---|
| 0.05 | 0.147 / 0.104 / 0.122 | 0.017 | 0.192 / 0.192 / 0.192 | 0.127 |
| 0.10 | 0.219 / 0.017 / 0.032 | 0.001 | 0.246 / 0.130 / 0.170 | 0.056 |
| 0.40 (service default) | — / **0.000** / — | 0.000 | 0.563 / 0.004 / 0.008 | 0.000 |

At 0.4 the hard-case slices detect almost nothing:
- smoke without flames: 1 of 1,186
- small fire: 0 of 831
- low light: 0 fire detections in 559 images

Latency on this CPU: mean 42 ms, p95 89 ms.

**Decision:** *Evaluated: improvement required.* The model fails every acceptance criterion (fire image recall ≥ 0.90). It is **not installed** in the live service, because an installed model that misses fires would read as "all clear". Next step: the GPU notebook ../training/train_fire_smoke_colab.ipynb (full train split, 80 epochs, YOLOX-Tiny @ 416).

## 2026-10-15: fire/smoke YOLOX-Tiny @ 416, GPU-trained, on the official D-Fire TEST split
**Training actually ran.**
- Kaggle T4 notebook (`../training/train_fire_smoke_colab.ipynb`, Version 1), 80/80 epochs, about 4 h 39 min.
- Started from the official COCO `yolox_tiny.pth` (sha256 9de513de…), with the full D-Fire train split.
- Best validation AP50:95 was 0.359, at epoch 80. Validation came from the train split; the test split was not used for selection.
- The notebook's export cell failed because YOLOX 0.3.0 calls `torch.onnx._export`, which current PyTorch has removed. The model was exported locally from `best_ckpt.pth` (sha256 df57126a…) with `../training/export_onnx.py`, loaded tensors-only.
- Output: `fire_smoke_yolox.onnx`, sha256 8d73df91dbb0241cfb04cacf529a8be4b26e68d4fccbb20cd816884bfa914d73. That hash is recorded in models.json.

**Data:** 4,306 test images: 2,005 negatives, 1,115 with fire, 2,081 with smoke. Full numbers: [2026-10-15-fire_smoke-yolox_tiny-kaggle-dfire-test/report.json](2026-10-15-fire_smoke-yolox_tiny-kaggle-dfire-test/report.json).

| Threshold | Fire box P / R | Fire image recall / FPR | Smoke box P / R | Smoke image recall / FPR |
|---|---|---|---|---|
| 0.25 | 0.642 / 0.697 | 0.952 / 0.011 | 0.793 / 0.798 | 0.947 / 0.039 |
| 0.30 | 0.676 / 0.682 | 0.947 / 0.009 | 0.808 / 0.791 | 0.939 / 0.034 |
| 0.35 | 0.706 / 0.668 | 0.940 / 0.007 | 0.821 / 0.784 | 0.932 / 0.032 |
| **0.40 (configured)** | 0.731 / 0.649 | **0.932 / 0.006** | 0.833 / 0.777 | **0.923 / 0.028** |
| 0.50 | 0.783 / 0.602 | 0.904 / 0.004 | 0.856 / 0.754 | 0.891 / 0.024 |

**Hard-case slices at 0.4** (image level):
- Negatives: 4 of 2,005 had a fire detection and 17 of 2,005 a smoke detection.
- Smoke without flames: 1,074 of 1,186 detected as smoke, 15 wrongly flagged as fire.
- Small fire (box under 1% of the image): 769 of 831 detected.
- Low light (mean grey below 60): 2 of 229 negatives had a false fire detection.

**Latency on this CPU** (Xeon W3520, no AVX, fire_smoke model only): mean 182 ms, p50 142 ms, p95 356 ms. With both models, the live service averaged about 460 ms per frame.

**Decision:** *Evaluated: meets the defined development criteria* on D-Fire test.
- Fire image recall is 0.932 (criterion ≥ 0.90), with FPR 0.006 (≤ 0.10).
- Smoke image recall is 0.923 (≥ 0.80), with FPR 0.028 (≤ 0.15).
- Box precision is 0.731 for fire and 0.833 for smoke (≥ 0.60).
- p95 latency is 356 ms (≤ 1000 ms).

Limits:
- Fire box recall is only 0.65. Each image is caught, but not every flame in it. This matters for area-based severity, which can be under-estimated.
- D-Fire test comes from the same sources as train. Site CCTV, fog, steam, dust, reflections and IR footage are **not tested**.
- This is not a safety certification. Keep smoke detectors, the fire panel and human oversight.

**End-to-end (2026-10-15, real services, isolated test DB `SentinelDb_E2E_20261015`, dropped afterwards):**
- Setup:
  - The `.env` was **not loaded**, so the real SMS, email and push credentials were not active.
  - The API ran on :5299 with throwaway keys. sentinel-ai ran with `models loaded: ['person', 'fire_smoke']`, status ok.
- Input: `e2e-fire.avi`, 40 consecutive D-Fire TEST frames, labelled simulated, sent to `cam-warehouse` (Zone B).
- Run 1 at fps 1 (every 4th frame): 10 frames posted with 0 detections. The detector found fire in every frame, but D-Fire "consecutive" frames are separate photos whose boxes jump between samples. No track reached 3 hits, so the 3-frame confirmation correctly reported nothing.
  - The consequence for real use: confirmation needs a camera whose view is stable for at least 3 processed frames.
- Run 2 at fps 4 (all frames): 40 frames posted, 0 failures.
  - Incident #1 opened at Low, then escalated to Medium: "Fire detected (Medium), Smoke detected … [SIMULATED DATA]".
  - There was only **one** incident; repeat frames updated it (dedupe).
  - 6 IncidentLogs were written, all `simulated: true`.
  - SignalR delivered the `incident`, `incidentLog`, `activity` and `detection` events.
  - All 5 sprinklers stayed **off**.
- Not exercised: the emergency path (High or Critical severity, or a person in the danger zone). It would trigger notifications and the sprinkler rule; the sprinkler-skip-on-simulated rule is covered only by unit tests.

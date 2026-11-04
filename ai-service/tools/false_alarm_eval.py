"""False-alarm review of the fire/smoke pipeline with the REAL installed models. Three sources, always reported apart:

1. REAL, labelled: the per-image D-Fire TEST predictions that tools/evaluate.py stored (predictions.jsonl, 4,306 images with
   their ground truth). Image level per class at the configured threshold, and what each candidate extra rule would change.
   These are single photos, so they can judge per-detection rules but not temporal ones.
2. SYNTHETIC, labelled by construction: scenes drawn here (sunset, blinking red lights, kitchen steam, vape plume, red object,
   flickering warm glow). Ground truth: no fire and no smoke in any frame. They are plain drawings, not real footage, so they
   say nothing about real-world accuracy.
3. REAL footage, clip label only: samples/fire-test.avi (D-Fire test photos as a clip) contains fire and smoke; there are no
   per-frame labels, so only "was it confirmed in the clip" is measured.

  python tools/false_alarm_eval.py [--out reports/<date>-false-alarm-review]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from sentinel_ai.detector import load_manifest  # noqa: E402

PREDICTIONS = ROOT / "reports" / "2026-10-19-fire_smoke-yolox_tiny-dfire-test" / "predictions.jsonl"
SAMPLE = ROOT / "samples" / "fire-test.avi"
H, W = 480, 640


# ---------------------------------------------------------------- synthetic scenes (expected: no fire, no smoke)
def sunset(t):
    y = np.linspace(0, 1, H)[:, None]
    img = np.zeros((H, W, 3), np.float32)
    img[..., 2], img[..., 1], img[..., 0] = 255 * (0.6 + 0.4 * y), 255 * (0.2 + 0.45 * (1 - y)), 60 * (1 - y)
    cv2.circle(img, (W // 2, int(H * 0.7) + t), 60, (120, 220, 255), -1)  # the sun sinks a pixel per frame
    return img.astype(np.uint8)


def red_lights(t):
    img = np.full((H, W, 3), 25, np.uint8)
    for i in range(6):
        cv2.circle(img, (80 + i * 95, 200), 18, (40, 40, 255) if (t + i) % 2 == 0 else (20, 20, 90), -1)
    return cv2.GaussianBlur(img, (0, 0), 4)


def _cloud(t, x, y0, rise, spread, density, seed):
    rng = np.random.default_rng(seed + t)
    cloud = np.zeros((H, W), np.float32)
    for k in range(25):
        cv2.circle(cloud, (int(x + rng.normal(0, spread)), int(y0 - k * rise + rng.normal(0, 10))), 10 + k * 2, 1.0, -1)
    return cv2.GaussianBlur(cloud, (0, 0), 20)[..., None] * density


def steam(t):
    img = np.full((H, W, 3), (70, 80, 90), np.uint8)
    cv2.rectangle(img, (200, 330), (440, 480), (160, 160, 160), -1)  # a pot on the stove
    c = _cloud(t, 320, 320, 6, 40, 0.8, 1)
    return (img * (1 - c) + 235 * c).astype(np.uint8)


def vape(t):
    img = np.full((H, W, 3), (120, 110, 100), np.uint8)
    cv2.ellipse(img, (250, 300), (60, 80), 0, 0, 360, (90, 120, 170), -1)  # a face in profile
    cv2.rectangle(img, (300, 300), (370, 312), (40, 40, 40), -1)  # the vape pen
    c = _cloud(t, 400, 300, 4, 25, 0.6, 7)
    return (img * (1 - c) + 225 * c).astype(np.uint8)


def red_object(t):
    img = np.full((H, W, 3), (200, 200, 195), np.uint8)
    cv2.rectangle(img, (270 + t, 140), (370 + t, 420), (20, 20, 200), -1)  # a red extinguisher carried slowly past
    return img


def warm_glow(t):
    img = np.full((H, W, 3), 30, np.uint8)
    level = 0.6 + 0.4 * ((t * 7) % 5) / 4  # an orange lamp flickering
    cv2.circle(img, (320, 220), 70, (30 * level, 140 * level, 255 * level), -1)
    return cv2.GaussianBlur(img, (0, 0), 15)


SCENARIOS = {"sunset": sunset, "red lights (blinking)": red_lights, "kitchen steam": steam, "vape": vape,
             "red object": red_object, "flickering warm glow": warm_glow}


def prf(tp, fp, fn):
    p = tp / (tp + fp) if tp + fp else None
    r = tp / (tp + fn) if tp + fn else None
    return {"precision": p and round(p, 4), "recall": r and round(r, 4),
            "f1": round(2 * p * r / (p + r), 4) if p and r else None}


# ---------------------------------------------------------------- 1. real labelled images: candidate rules
RULES = {  # a candidate extra rule keeps a detection (category, confidence, [x, y, w, h]) when it returns True
    "none (current pipeline)": lambda d, ds: True,
    "box at least 100 px²": lambda d, ds: d[2][2] * d[2][3] >= 100,
    "box at least 400 px²": lambda d, ds: d[2][2] * d[2][3] >= 400,
    "confidence at least 0.45": lambda d, ds: d[1] >= 0.45,
    "confidence at least 0.5": lambda d, ds: d[1] >= 0.5,
    "fire only together with smoke": lambda d, ds: d[0] != "Fire" or any(o[0] == "Smoke" for o in ds),
}


def real_images(path: Path, threshold: float) -> dict:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    out = {"source": path.relative_to(ROOT).as_posix(), "images": len(rows), "threshold": threshold, "rules": {}}
    for name, keep in RULES.items():
        out["rules"][name] = {}
        for cat in ("Fire", "Smoke"):
            tp = fp = fn = tn = 0
            for r in rows:
                dets = [d for d in r["pred"] if d[1] >= threshold]
                labelled, found = any(g[0] == cat for g in r["gt"]), any(d[0] == cat and keep(d, dets) for d in dets)
                tp, fp, fn, tn = tp + (labelled and found), fp + (found and not labelled), fn + (labelled and not found), tn + (not labelled and not found)
            out["rules"][name][cat] = {"tp": tp, "fp": fp, "fn": fn, "tn": tn, **prf(tp, fp, fn)}
    return out


# ---------------------------------------------------------------- 2 and 3: the real models through the live pipeline
def clip(engine, frames) -> list[dict]:
    pipe = engine.pipeline("false-alarm-eval", simulated=True)
    return [engine.run(pipe, f, temporal=True)[0] for f in frames]


def confirmed(payloads, cat) -> int:
    return sum(any(d["category"] == cat for d in p["detections"]) for p in payloads)


def synthetic(engine, frames=20) -> dict:
    out = {}
    for name, draw in SCENARIOS.items():
        payloads = clip(engine, (draw(t) for t in range(frames)))
        out[name] = {"frames": frames, "expected": "no fire, no smoke", "fireFrames": confirmed(payloads, "Fire"),
                     "smokeFrames": confirmed(payloads, "Smoke")}
    return out


def sample(engine, path: Path, frames=40) -> dict:
    cap, images = cv2.VideoCapture(str(path)), []
    while len(images) < frames:
        ok, f = cap.read()
        if not ok:
            break
        images.append(f)
    cap.release()
    payloads = clip(engine, images)
    return {"source": path.relative_to(ROOT).as_posix(), "frames": len(images), "expected": "fire and smoke in the clip (no per-frame labels)",
            "fireFrames": confirmed(payloads, "Fire"), "smokeFrames": confirmed(payloads, "Smoke"),
            "smokeWithoutFireFrames": sum(not p["fireDetected"] and any(d["category"] == "Smoke" for d in p["detections"]) for p in payloads)}


def main() -> int:
    a = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    a.add_argument("--out", type=Path, help="folder for review.json (not report.json: this is not an evaluate.py run)")
    args = a.parse_args()
    from sentinel_ai.service import Engine
    threshold = next(m["threshold"] for m in load_manifest(ROOT / "models") if m["name"] == "fire_smoke")
    engine = Engine(ROOT / "models")
    review = {"evaluatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "realImages": real_images(PREDICTIONS, threshold) if PREDICTIONS.is_file() else None,
              "synthetic": synthetic(engine), "realSample": sample(engine, SAMPLE) if SAMPLE.is_file() else None}
    text = json.dumps(review, indent=2)
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "review.json").write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())

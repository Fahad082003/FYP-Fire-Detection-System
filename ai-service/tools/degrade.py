"""SYNTHETIC stress test: darken and/or fog a labelled YOLO dataset for tools/evaluate.py. Labels are copied unchanged.

This is NOT real night, IR or fog footage. It shows how the score moves when images get darker or hazier, nothing more.
  dark: brightness x0.25, then sensor-like noise (Gaussian, sigma 6 grey levels)
  fog:  homogeneous haze, I = J*t + A*(1 - t) with transmission t = 0.45 and airlight A = 215 (light grey)

  python tools/degrade.py --data <dataset> --out <dir> --kinds dark,fog --sample 1000 --seed 0
  python tools/evaluate.py --data <dir> --classes 0=Smoke,1=Fire --out reports/<name>
--kinds none copies the same sample unchanged (the clean baseline for a fair comparison).
"""
import argparse
import random
import shutil
from pathlib import Path

import cv2
import numpy as np

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".bmp"}


def degrade(img: np.ndarray, kinds: list[str], rng: np.random.Generator) -> np.ndarray:
    out = img.astype(np.float32)
    if "fog" in kinds:
        out = out * 0.45 + 215 * (1 - 0.45)
    if "dark" in kinds:  # after fog: a foggy night is dark haze, not bright haze
        out = out * 0.25 + rng.normal(0, 6, out.shape)
    return np.clip(out, 0, 255).astype(np.uint8)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--kinds", default="dark", help="comma list of dark, fog; or none")
    ap.add_argument("--sample", type=int, default=0, help="random N images (0 = all); the same seed gives the same images")
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()
    kinds = [] if a.kinds == "none" else a.kinds.split(",")
    assert set(kinds) <= {"dark", "fog"}, f"unknown kind in {kinds}"
    images = sorted(p for p in (a.data / "images").iterdir() if p.suffix.lower() in IMAGE_EXT)
    if a.sample:
        images = sorted(random.Random(a.seed).sample(images, min(a.sample, len(images))))
    (a.out / "images").mkdir(parents=True, exist_ok=True)
    (a.out / "labels").mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(a.seed)
    for p in images:
        img = cv2.imread(str(p))
        if img is None:
            continue
        cv2.imwrite(str(a.out / "images" / (p.stem + ".jpg")), degrade(img, kinds, rng), [cv2.IMWRITE_JPEG_QUALITY, 95])
        label = a.data / "labels" / (p.stem + ".txt")
        if label.is_file():
            shutil.copy(label, a.out / "labels" / label.name)
    print(f"{len(images)} images, kinds={kinds or ['none']} -> {a.out}")

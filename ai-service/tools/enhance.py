"""EXPERIMENT: contrast enhancement applied before the AI, to test whether it recovers detections lost in haze.

  clahe:   CLAHE on the lightness channel only (LAB colour space, clip 2.0, 8x8 tiles); colours keep their hue
  stretch: per-channel contrast stretch from the 1st to the 99th percentile to the full 0..255 range

  python tools/enhance.py --data <dataset> --out <dir> --method clahe
  python tools/evaluate.py --data <dir> --classes 0=Smoke,1=Fire --out reports/<name>
Compare against the same dataset without enhancement. Not used by the live service.
"""
import argparse
import shutil
from pathlib import Path

import cv2
import numpy as np

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".bmp"}


def enhance(img: np.ndarray, method: str) -> np.ndarray:
    if method == "clahe":
        lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
        lab[:, :, 0] = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(lab[:, :, 0])
        return cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)
    if method == "stretch":
        lo, hi = np.percentile(img, 1, axis=(0, 1)), np.percentile(img, 99, axis=(0, 1))
        return np.clip((img.astype(np.float32) - lo) * 255 / np.maximum(hi - lo, 1), 0, 255).astype(np.uint8)
    raise ValueError(f"unknown method {method!r}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--method", choices=["clahe", "stretch"], required=True)
    a = ap.parse_args()
    (a.out / "images").mkdir(parents=True, exist_ok=True)
    (a.out / "labels").mkdir(parents=True, exist_ok=True)
    n = 0
    for p in sorted(p for p in (a.data / "images").iterdir() if p.suffix.lower() in IMAGE_EXT):
        img = cv2.imread(str(p))
        if img is None:
            continue
        cv2.imwrite(str(a.out / "images" / (p.stem + ".jpg")), enhance(img, a.method), [cv2.IMWRITE_JPEG_QUALITY, 95])
        label = a.data / "labels" / (p.stem + ".txt")
        if label.is_file():
            shutil.copy(label, a.out / "labels" / label.name)
        n += 1
    print(f"{n} images, method={a.method} -> {a.out}")

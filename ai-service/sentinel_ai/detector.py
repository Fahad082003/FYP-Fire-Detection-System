"""YOLOX ONNX detector (official Megvii export: raw head output, decoder applied here)."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

CATEGORIES = {"Human", "Fire", "Smoke"}


@dataclass
class Det:
    category: str            # Sentinel DetectionCategory: Human | Fire | Smoke
    label: str
    confidence: float
    box: tuple[float, float, float, float]  # x, y, w, h in pixels of the original frame
    model: str
    threshold: float
    track_id: str | None = None
    hits: int = 1            # frames this track was seen in (set by the tracker)
    age: int = 1             # frames since the track started
    explanation: str | None = None


class ModelError(RuntimeError):
    """The model file is missing, broken, or produced output this code cannot interpret."""


def load_manifest(models_dir: Path) -> list[dict]:
    return json.loads((models_dir / "models.json").read_text(encoding="utf-8"))["models"]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class YoloxDetector:
    """One YOLOX model. `session` is injectable so unit tests can run without weights (mock-based)."""

    def __init__(self, spec: dict, models_dir: Path, session=None):
        self.name = spec["name"]
        self.file = spec["file"]
        self.input_h, self.input_w = spec["input"]
        self.threshold = float(spec["threshold"])
        self.nms = float(spec["nms"])
        self.categories = {int(k): v for k, v in spec["categories"].items()}
        self.labels = {int(k): v for k, v in spec.get("labels", {}).items()}
        bad = set(self.categories.values()) - CATEGORIES
        if bad:
            raise ModelError(f"{self.name}: unknown categories {bad}")
        if session is None:
            path = models_dir / self.file
            if not path.is_file():
                raise ModelError(f"model file not found: {path} (run tools/download_models.py or see training/README.md)")
            if spec.get("sha256") and sha256(path) != spec["sha256"]:
                raise ModelError(f"{path.name}: sha256 mismatch, refusing to load an unverified file")
            import onnxruntime as ort  # imported lazily: unit tests do not need it
            session = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
        self.session = session
        self.input_name = session.get_inputs()[0].name

    def preprocess(self, bgr: np.ndarray) -> tuple[np.ndarray, float]:
        """Letterbox to the model size with grey (114) padding; YOLOX 0.1.1rc0 models take raw 0-255 BGR."""
        h, w = bgr.shape[:2]
        r = min(self.input_h / h, self.input_w / w)
        resized = cv2.resize(bgr, (int(w * r), int(h * r)), interpolation=cv2.INTER_LINEAR)
        padded = np.full((self.input_h, self.input_w, 3), 114, dtype=np.uint8)
        padded[: resized.shape[0], : resized.shape[1]] = resized
        return np.ascontiguousarray(padded.transpose(2, 0, 1)[None], dtype=np.float32), r

    def decode(self, raw: np.ndarray) -> np.ndarray:
        """Grid decode of the raw head output [1, N, 5 + classes] -> boxes as cx, cy, w, h in model pixels."""
        out = np.array(raw, dtype=np.float32)
        grids, strides = [], []
        for s in (8, 16, 32):
            hs, ws = self.input_h // s, self.input_w // s
            xv, yv = np.meshgrid(np.arange(ws), np.arange(hs))
            grids.append(np.stack((xv, yv), 2).reshape(1, -1, 2))
            strides.append(np.full((1, hs * ws, 1), s))
        grid, stride = np.concatenate(grids, 1), np.concatenate(strides, 1)
        if out.ndim != 3 or out.shape[1] != grid.shape[1] or out.shape[2] < 6:
            raise ModelError(f"{self.name}: unexpected output shape {out.shape}, expected (1, {grid.shape[1]}, 5+classes)")
        out[..., :2] = (out[..., :2] + grid) * stride
        out[..., 2:4] = np.exp(out[..., 2:4]) * stride
        return out[0]

    def detect(self, bgr: np.ndarray) -> list[Det]:
        if bgr is None or bgr.ndim != 3 or bgr.shape[2] != 3:
            raise ValueError("expected a BGR image")
        blob, r = self.preprocess(bgr)
        pred = self.decode(self.session.run(None, {self.input_name: blob})[0])
        scores = pred[:, 4:5] * pred[:, 5:]
        cls = scores.argmax(1)
        conf = scores[np.arange(len(cls)), cls]
        # NaN/Inf rows must be dropped here: max(0, nan) / min(w, nan) would otherwise clip them into full-frame boxes.
        valid = np.isfinite(pred[:, :4]).all(1) & np.isfinite(conf) & (pred[:, 2] > 0) & (pred[:, 3] > 0)
        keep = valid & np.isin(cls, list(self.categories)) & (conf >= self.threshold)
        if not keep.any():
            return []
        boxes, conf, cls = pred[keep, :4] / r, conf[keep], cls[keep]
        xywh = np.stack([boxes[:, 0] - boxes[:, 2] / 2, boxes[:, 1] - boxes[:, 3] / 2, boxes[:, 2], boxes[:, 3]], 1)
        fh, fw = bgr.shape[:2]
        result = []
        for c in np.unique(cls):  # class-wise NMS
            idx = np.where(cls == c)[0]
            kept = cv2.dnn.NMSBoxes(xywh[idx].tolist(), conf[idx].tolist(), self.threshold, self.nms)
            for k in np.array(kept).reshape(-1):
                x, y, w, h = xywh[idx[k]]
                x0, y0 = max(0.0, float(x)), max(0.0, float(y))
                x1, y1 = min(float(fw), float(x + w)), min(float(fh), float(y + h))
                if x1 <= x0 or y1 <= y0:
                    continue
                result.append(Det(self.categories[int(c)], self.labels.get(int(c), self.categories[int(c)].lower()),
                                  round(float(conf[idx[k]]), 4), (round(x0, 1), round(y0, 1), round(x1 - x0, 1), round(y1 - y0, 1)),
                                  self.name, self.threshold))
        return result

"""Frame -> detections -> tracking -> confirmation -> Sentinel detection payload (DetectionPayloadDto)."""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np

from .detector import Det, YoloxDetector
from .tracker import IouTracker

DEFAULTS = {
    "confirm_frames": 3,          # fire/smoke track must be seen in this many frames before it is reported
    "severity_area_percent": {"Medium": 2.0, "High": 10.0, "Critical": 25.0},  # fire boxes as % of frame; below Medium = Low
    "fire_zone_margin": 0.25,     # AI danger zone around confirmed fire = fire box grown by this fraction per side
    "heatmap": {"rows": 9, "cols": 16, "decay": 0.8},
}


def fire_severity(area_percent: float, thresholds: dict) -> str:
    """shortcut: on-screen area, not physical size; depends on camera distance. Calibrate per camera."""
    for level in ("Critical", "High", "Medium"):
        if area_percent >= thresholds[level]:
            return level
    return "Low"


def fire_area_percent(boxes, fw: int, fh: int) -> float:
    """Union of the fire boxes as % of the frame: overlapping or duplicate boxes are counted once, parts outside the frame not at all."""
    mask = np.zeros((fh, fw), bool)
    for x, y, w, h in boxes:
        mask[max(0, int(y)):max(0, min(fh, int(np.ceil(y + h)))), max(0, int(x)):max(0, min(fw, int(np.ceil(x + w))))] = True
    return float(mask.mean() * 100)


class CameraPipeline:
    def __init__(self, detectors: list[YoloxDetector], camera_id: str, simulated: bool,
                 settings: dict | None = None, zones: list | None = None, missing: list[str] | None = None):
        self.detectors, self.camera_id, self.simulated = detectors, camera_id, simulated
        settings = settings or {}
        # Merge nested dicts too, so a config that sets only {"High": 8} keeps the default Medium/Critical.
        self.s = {k: {**v, **settings.get(k, {})} if isinstance(v, dict) else settings.get(k, v) for k, v in DEFAULTS.items()}
        self.zones = zones or []
        self.tracker = IouTracker()
        hm = self.s["heatmap"]
        self.heat = np.zeros((hm["rows"], hm["cols"]), dtype=np.float32)
        loaded = [f"{d.name}:{d.file.rsplit('.', 1)[0]}" for d in detectors]
        self.model = " ".join(loaded + [f"{m}:missing" for m in (missing or [])])[:64]

    def process(self, bgr: np.ndarray, temporal: bool = True) -> dict:
        """temporal=False for single test images: nothing to confirm against, so every detection above threshold counts."""
        fh, fw = bgr.shape[:2]
        dets: list[Det] = [d for det in self.detectors for d in det.detect(bgr)]
        if temporal:
            dets = self.tracker.update(dets)
        need = self.s["confirm_frames"] if temporal else 1
        reported = [d for d in dets if d.category == "Human" or d.hits >= need]

        fire = [d for d in reported if d.category == "Fire"]
        area = fire_area_percent([d.box for d in fire], fw, fh)
        severity = fire_severity(area, self.s["severity_area_percent"]) if fire else None
        for d in reported:
            d.explanation = self._explain(d, fw, fh, temporal, need)

        zones = list(self.zones)
        m = self.s["fire_zone_margin"]
        for d in fire:  # people whose feet stand in/near a confirmed fire are flagged by the backend's zone check
            x, y, w, h = d.box
            x0, y0, x1, y1 = max(0, x - w * m), max(0, y - h * m), min(fw, x + w * (1 + m)), min(fh, y + h * (1 + m))
            zones.append({"label": f"AI fire area {d.track_id or ''}".strip(), "level": "Danger",
                          "polygon": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]})

        return {
            "cameraId": self.camera_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "frameWidth": int(fw), "frameHeight": int(fh), "normalized": False,
            "detections": [{"label": d.label, "category": d.category, "confidence": d.confidence, "bbox": list(d.box),
                            "trackId": d.track_id, "explanation": d.explanation} for d in reported],
            "dangerZones": zones,
            "heatmap": self._heatmap(reported, fw, fh),
            "fireDetected": bool(fire),
            "fireSeverity": severity,
            "simulated": self.simulated,
            "model": self.model,
        }

    def _explain(self, d: Det, fw: int, fh: int, temporal: bool, need: int) -> str:
        parts = [f"{d.label} {d.confidence:.2f} >= threshold {d.threshold:.2f} ({d.model})",
                 f"box {d.box[2] * d.box[3] / (fw * fh) * 100:.1f}% of frame"]
        if not temporal:
            parts.append("single image: no multi-frame confirmation")
        elif d.category == "Human":
            parts.append(f"track {d.track_id} seen {d.hits}/{d.age} frames")
        else:
            parts.append(f"confirmed: track {d.track_id} seen {d.hits}/{d.age} frames (need {need})")
        return "; ".join(parts)[:240]

    def _heatmap(self, dets: list[Det], fw: int, fh: int) -> dict | None:
        """Decaying density of confirmed fire/smoke boxes (a detection map, NOT a fire-spread prediction)."""
        rows, cols = self.heat.shape
        frame = np.zeros_like(self.heat)
        for d in dets:
            if d.category not in ("Fire", "Smoke"):
                continue
            x, y, w, h = d.box
            c0, c1 = int(x / fw * cols), min(cols - 1, int((x + w) / fw * cols))
            r0, r1 = int(y / fh * rows), min(rows - 1, int((y + h) / fh * rows))
            frame[r0:r1 + 1, c0:c1 + 1] = np.maximum(frame[r0:r1 + 1, c0:c1 + 1], d.confidence)
        self.heat = np.maximum(self.heat * self.s["heatmap"]["decay"], frame)
        if self.heat.max() < 0.05:
            return None
        return {"rows": rows, "cols": cols, "values": [round(float(v), 3) for v in self.heat.clip(0, 1).ravel()]}

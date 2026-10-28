"""Greedy IoU tracker: gives detections a stable track id across frames of one camera."""
from __future__ import annotations

from dataclasses import dataclass

from .detector import Det

PREFIX = {"Human": "P", "Fire": "F", "Smoke": "S"}


def iou(a, b) -> float:
    ax1, ay1, bx1, by1 = a[0] + a[2], a[1] + a[3], b[0] + b[2], b[1] + b[3]
    iw, ih = max(0.0, min(ax1, bx1) - max(a[0], b[0])), max(0.0, min(ay1, by1) - max(a[1], b[1]))
    inter = iw * ih
    union = a[2] * a[3] + b[2] * b[3] - inter
    return inter / union if union > 0 else 0.0


@dataclass
class Track:
    id: str
    category: str
    box: tuple
    hits: int = 1
    age: int = 1
    missed: int = 0


class IouTracker:
    """shortcut: greedy IoU matching, no motion model; people crossing paths can swap ids. Upgrade to ByteTrack/DeepSORT for crowds."""

    def __init__(self, iou_threshold: float = 0.3, max_missed: int = 5):
        self.iou_threshold, self.max_missed = iou_threshold, max_missed
        self.tracks: list[Track] = []
        self._next = 0

    def update(self, dets: list[Det]) -> list[Det]:
        for t in self.tracks:
            t.age += 1
        pairs = sorted(((iou(t.box, d.box), ti, di) for ti, t in enumerate(self.tracks) for di, d in enumerate(dets)
                        if t.category == d.category), reverse=True)
        used_t, used_d = set(), set()
        for score, ti, di in pairs:
            if score < self.iou_threshold or ti in used_t or di in used_d:
                continue
            used_t.add(ti)
            used_d.add(di)
            t = self.tracks[ti]
            t.box, t.hits, t.missed = dets[di].box, t.hits + 1, 0
            dets[di].track_id, dets[di].hits, dets[di].age = t.id, t.hits, t.age
        for ti, t in enumerate(self.tracks):
            if ti not in used_t:
                t.missed += 1
        for di, d in enumerate(dets):
            if di in used_d:
                continue
            self._next += 1
            t = Track(f"{PREFIX.get(d.category, 'T')}{self._next}", d.category, d.box)
            self.tracks.append(t)
            d.track_id, d.hits, d.age = t.id, 1, 1
        self.tracks = [t for t in self.tracks if t.missed <= self.max_missed]
        return dets

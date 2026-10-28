"""Measure the REAL loaded models on a labelled dataset (YOLO format). Nothing here is estimated or invented.

Dataset layout:  <dir>/images/*.jpg|png   <dir>/labels/<same stem>.txt   lines: "<class> <cx> <cy> <w> <h>" (0..1)
Images without a label file (or with an empty one) are NEGATIVES (nothing to detect).

Example:
  python tools/evaluate.py --data ../datasets/pennfudan --classes 0=Human --out reports/person-pennfudan
  python tools/evaluate.py --data ../datasets/dfire-test --classes 0=Smoke,1=Fire --sweep 0.25,0.3,0.4,0.5 --out reports/fire
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sentinel_ai.detector import load_manifest, YoloxDetector, ModelError  # noqa: E402
from sentinel_ai.tracker import iou  # noqa: E402

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".bmp"}
SKIPPED_LABELS: list[str] = []


def read_labels(path: Path, w: int, h: int, class_map: dict) -> list[tuple[str, tuple]]:
    if not path.is_file():
        return []
    out = []
    for line in path.read_text().split("\n"):
        parts = line.split()
        if len(parts) != 5 or int(parts[0]) not in class_map:
            continue
        cx, cy, bw, bh = (float(v) for v in parts[1:])
        if bw <= 0 or bh <= 0:  # degenerate annotation (zero-area box): cannot be detected, so it is not scored
            SKIPPED_LABELS.append(f"{path.name}: {line.strip()}")
            continue
        out.append((class_map[int(parts[0])], ((cx - bw / 2) * w, (cy - bh / 2) * h, bw * w, bh * h)))
    return out


def match(preds, gts, iou_thr):
    """Greedy match by confidence; returns tp, fp, fn counts for one category in one image."""
    used, tp = set(), 0
    for p in sorted(preds, key=lambda d: -d.confidence):
        best, bi = 0.0, -1
        for i, g in enumerate(gts):
            if i not in used and (v := iou(p.box, g)) > best:
                best, bi = v, i
        if best >= iou_thr:
            used.add(bi)
            tp += 1
    return tp, len(preds) - tp, len(gts) - tp


AP_IOUS = [round(0.5 + 0.05 * i, 2) for i in range(10)]  # COCO: 0.50, 0.55, ... 0.95


def match_flags(preds, gts, iou_thr) -> list[bool]:
    """Same greedy matching as match(), but one flag per prediction (highest confidence first): True = true positive."""
    used, flags = set(), []
    for p in sorted(preds, key=lambda d: -d.confidence):
        best, bi = 0.0, -1
        for i, g in enumerate(gts):
            if i not in used and (v := iou(p.box, g)) > best:
                best, bi = v, i
        flags.append(best >= iou_thr)
        if flags[-1]:
            used.add(bi)
    return flags


def average_precision(scored: list[tuple[float, bool]], n_gt: int) -> float | None:
    """COCO-style AP: 101-point interpolated area under the precision-recall curve (all object sizes, no crowd regions)."""
    if not n_gt:
        return None
    scored = sorted(scored, key=lambda x: -x[0])
    tp = np.cumsum([f for _, f in scored], dtype=float)
    fp = np.cumsum([not f for _, f in scored], dtype=float)
    recall = tp / n_gt if len(scored) else np.zeros(0)
    precision = tp / np.maximum(tp + fp, 1e-9) if len(scored) else np.zeros(0)
    precision = np.maximum.accumulate(precision[::-1])[::-1]  # precision envelope
    points = [precision[np.searchsorted(recall, r, side="left")] if r <= (recall[-1] if len(recall) else -1) else 0.0
              for r in np.linspace(0, 1, 101)]
    return round(float(np.mean(points)), 4)


def prf(tp, fp, fn):
    p = tp / (tp + fp) if tp + fp else None
    r = tp / (tp + fn) if tp + fn else None
    f1 = 2 * p * r / (p + r) if p and r else (0.0 if p is not None and r is not None else None)
    return {"tp": tp, "fp": fp, "fn": fn, "precision": p and round(p, 4), "recall": r and round(r, 4), "f1": f1 and round(f1, 4)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--classes", required=True, help="dataset class index -> category, e.g. 0=Smoke,1=Fire")
    ap.add_argument("--models", type=Path, default=Path(__file__).resolve().parent.parent / "models")
    ap.add_argument("--iou", type=float, default=0.5)
    ap.add_argument("--sweep", default="", help="extra confidence thresholds to report, e.g. 0.25,0.35")
    ap.add_argument("--limit", type=int, default=0, help="evaluate only the first N images (0 = all)")
    ap.add_argument("--images-list", type=Path, help="text file of image file names: evaluate only this slice (e.g. night.txt)")
    ap.add_argument("--ap-floor", type=float, default=0.01, help="lowest confidence kept for the AP/mAP precision-recall curve")
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()

    class_map = {int(k): v for k, v in (kv.split("=") for kv in a.classes.split(","))}
    wanted = set(class_map.values())
    detectors, load_ms = [], {}
    for spec in load_manifest(a.models):
        if wanted & set(spec["categories"].values()):
            t0 = time.perf_counter()
            try:
                detectors.append(YoloxDetector(spec, a.models))
            except ModelError as e:
                sys.exit(f"cannot evaluate: {e}")
            load_ms[spec["name"]] = round((time.perf_counter() - t0) * 1000, 1)
    if not detectors:
        sys.exit(f"no model in the manifest produces {wanted}")
    base = {d.name: d.threshold for d in detectors}
    sweep = sorted({float(t) for t in a.sweep.split(",") if t} | set(base.values()))
    for d in detectors:  # infer once at the AP floor, filter per threshold afterwards (greedy NMS, no top-k: same boxes)
        d.threshold = min(min(sweep), a.ap_floor)
    ap_scored = {c: {t: [] for t in AP_IOUS} for c in wanted}
    n_gt = {c: 0 for c in wanted}
    a.out.mkdir(parents=True, exist_ok=True)
    pred_log = (a.out / "predictions.jsonl").open("w", encoding="utf-8")

    images = sorted(p for p in (a.data / "images").iterdir() if p.suffix.lower() in IMAGE_EXT)
    if a.images_list:
        wanted_names = {Path(n.strip()).name for n in a.images_list.read_text().splitlines() if n.strip()}
        images = [p for p in images if p.name in wanted_names]
    if a.limit:
        images = images[: a.limit]
    counts = {t: {c: [0, 0, 0] for c in wanted} for t in sweep}
    img_conf = {t: {c: {"tp": 0, "fp": 0, "fn": 0, "tn": 0} for c in wanted} for t in sweep}
    latency, failures = [], {"false_negatives": [], "false_positives": []}
    # Hard-case slices derivable from the labels alone (no manual tagging); image-level, at the configured threshold.
    slices = {s: {c: {"images": 0, "detected": 0} for c in wanted} for s in ("negative", "smoke_only", "small_fire", "low_light_positive", "low_light_negative")}
    cls_threshold = {c: base[d.name] for d in detectors for c in d.categories.values()}
    for path in images:
        img = cv2.imread(str(path))
        if img is None:
            print(f"skip unreadable {path.name}")
            continue
        h, w = img.shape[:2]
        gts = read_labels(a.data / "labels" / (path.stem + ".txt"), w, h, class_map)
        t0 = time.perf_counter()
        preds = [p for d in detectors for p in d.detect(img) if p.category in wanted]
        latency.append((time.perf_counter() - t0) * 1000)
        for c in wanted:
            pc = sorted((p for p in preds if p.category == c), key=lambda d: -d.confidence)
            gc = [g for cat, g in gts if cat == c]
            n_gt[c] += len(gc)
            for t in AP_IOUS:
                ap_scored[c][t].extend(zip((p.confidence for p in pc), match_flags(pc, gc, t)))
        pred_log.write(json.dumps({"image": path.name, "gt": [[cat, [round(v, 1) for v in g]] for cat, g in gts],
                                   "pred": [[p.category, p.confidence, list(p.box)] for p in preds if p.confidence >= min(sweep)],
                                   "ms": round(latency[-1], 1)}) + "\n")
        cats = {cat for cat, _ in gts}
        tags = ["negative"] if not gts else []
        if "Smoke" in cats and "Fire" not in cats:
            tags.append("smoke_only")
        if any(cat == "Fire" and g[2] * g[3] < 0.01 * w * h for cat, g in gts):
            tags.append("small_fire")
        if float(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).mean()) < 60:  # brightness proxy for night / poor lighting
            tags.append("low_light_negative" if not gts else "low_light_positive")
        for tag in tags:
            for c in wanted:
                slices[tag][c]["images"] += 1
                slices[tag][c]["detected"] += any(p.category == c and p.confidence >= cls_threshold[c] for p in preds)
        for t in sweep:
            for c in wanted:
                pc = [p for p in preds if p.category == c and p.confidence >= t]
                gc = [g for cat, g in gts if cat == c]
                tp, fp, fn = match(pc, gc, a.iou)
                for i, v in enumerate((tp, fp, fn)):
                    counts[t][c][i] += v
                key = ("tp" if pc else "fn") if gc else ("fp" if pc else "tn")
                img_conf[t][c][key] += 1
                if t == base[next(d.name for d in detectors if c in d.categories.values())]:
                    if fn:
                        failures["false_negatives"].append(f"{path.name} ({c}: missed {fn})")
                    if fp:
                        failures["false_positives"].append(f"{path.name} ({c}: {fp} extra, max conf {max(p.confidence for p in pc):.2f})")

    pred_log.close()
    ap = {c: {str(t): average_precision(ap_scored[c][t], n_gt[c]) for t in AP_IOUS} for c in wanted}
    report = {
        "dataset": str(a.data), "slice": a.images_list.name if a.images_list else None, "images": len(latency), "iouThreshold": a.iou, "models": {d.name: d.file for d in detectors},
        "configuredThresholds": base,
        "modelLoadMs": load_ms,
        "modelFileBytes": {d.name: (a.models / d.file).stat().st_size for d in detectors},
        "averagePrecision": {"_meaning": f"COCO-style 101-point AP per IoU threshold from predictions >= {a.ap_floor}; "
                                         "mAP50 = AP at IoU 0.50, mAP50_95 = mean over IoU 0.50..0.95. All object sizes, no crowd regions.",
                             **{c: {"groundTruthBoxes": n_gt[c], "AP50": ap[c]["0.5"],
                                    "AP50_95": round(statistics.mean(v for v in ap[c].values() if v is not None), 4) if n_gt[c] else None,
                                    "byIoU": ap[c]} for c in wanted}},
        "latencyMs": {"mean": round(statistics.mean(latency), 1), "p50": round(float(np.percentile(latency, 50)), 1),
                      "p95": round(float(np.percentile(latency, 95)), 1), "p99": round(float(np.percentile(latency, 99)), 1),
                      "first": round(latency[0], 1), "imagesPerSecond": round(1000 / statistics.mean(latency), 2)} if latency else None,
        "byThreshold": {str(t): {c: {**prf(*counts[t][c]), "imageLevel": {
            **img_conf[t][c],
            "falsePositiveRate": round(img_conf[t][c]["fp"] / (img_conf[t][c]["fp"] + img_conf[t][c]["tn"]), 4)
            if img_conf[t][c]["fp"] + img_conf[t][c]["tn"] else None}} for c in wanted} for t in sweep},
        "slices": {"_meaning": "images in the slice and how many had >= 1 detection of the class at the configured threshold. "
                              "negative: any detection is a false alarm. smoke_only: smoke labelled, no fire labelled. "
                              "small_fire: a fire box under 1% of the image. low_light_*: mean grey level < 60/255 (a brightness proxy, "
                              "not a verified night label). Fog/steam/dust/reflections need human-tagged --images-list files.", **slices},
        "skippedDegenerateLabels": SKIPPED_LABELS,
        "failures": {k: v[:200] for k, v in failures.items()},
    }
    (a.out / "report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps({k: report[k] for k in ("images", "latencyMs", "averagePrecision", "byThreshold", "slices")}, indent=2))
    print(f"full report: {a.out / 'report.json'}")


if __name__ == "__main__":
    main()

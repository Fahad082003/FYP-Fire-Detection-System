"""Dataset preparation: convert, validate, split by scene, and export COCO json for YOLOX training.

  python tools/prepare_dataset.py pennfudan <PennFudanPed dir> <out dir>        # person eval set (YOLO format)
  python tools/prepare_dataset.py validate  <yolo dir> --classes 2              # decode check, label range, exact duplicates
  python tools/prepare_dataset.py split     <yolo dir> <out dir> --group "^([A-Za-z]+_?\\d*?)_" --val 0.15 --test 0.15
  python tools/prepare_dataset.py coco      <yolo dir> <out.json> --names smoke,fire

Split groups images by a filename regex (e.g. the source video id), so near-identical frames of one video never
land in both train and test (leakage). Labels are never invented: images without labels stay negatives.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import shutil
import sys
from collections import defaultdict
from pathlib import Path

import cv2

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".bmp"}


def images(d: Path):
    return sorted(p for p in (d / "images").iterdir() if p.suffix.lower() in IMAGE_EXT)


def pennfudan(src: Path, out: Path):
    (out / "images").mkdir(parents=True, exist_ok=True)
    (out / "labels").mkdir(parents=True, exist_ok=True)
    box = re.compile(r"\((\d+), (\d+)\) - \((\d+), (\d+)\)")
    n = 0
    for ann in sorted((src / "Annotation").glob("*.txt")):
        text = ann.read_text(errors="replace")
        img_path = src / "PNGImages" / (ann.stem + ".png")
        img = cv2.imread(str(img_path))
        if img is None:
            print(f"skip {img_path.name}: unreadable")
            continue
        h, w = img.shape[:2]
        lines = []
        for line in text.splitlines():
            if line.startswith("Bounding box") and (m := box.search(line)):
                x0, y0, x1, y1 = map(int, m.groups())
                lines.append(f"0 {(x0 + x1) / 2 / w:.6f} {(y0 + y1) / 2 / h:.6f} {(x1 - x0) / w:.6f} {(y1 - y0) / h:.6f}")
        shutil.copy(img_path, out / "images" / img_path.name)
        (out / "labels" / (ann.stem + ".txt")).write_text("\n".join(lines))
        n += 1
    print(f"converted {n} images -> {out}")


def validate(d: Path, classes: int):
    problems, hashes = [], defaultdict(list)
    counts = defaultdict(int)
    imgs = images(d)
    for p in imgs:
        data = p.read_bytes()
        hashes[hashlib.sha1(data).hexdigest()].append(p.name)
        if cv2.imread(str(p)) is None:
            problems.append(f"{p.name}: cannot decode")
        lab = d / "labels" / (p.stem + ".txt")
        if not lab.is_file() or not lab.read_text().strip():
            counts["negative_images"] += 1
            continue
        for i, line in enumerate(lab.read_text().splitlines(), 1):
            parts = line.split()
            if not parts:
                continue
            ok = len(parts) == 5 and parts[0].isdigit() and int(parts[0]) < classes
            vals = [float(v) for v in parts[1:]] if ok else []
            if not ok or not all(0 <= v <= 1 for v in vals) or vals[2] <= 0 or vals[3] <= 0:
                problems.append(f"{lab.name}:{i}: bad label '{line}'")
            else:
                counts[f"class_{parts[0]}_boxes"] += 1
    dups = [names for names in hashes.values() if len(names) > 1]
    print(json.dumps({"images": len(imgs), **counts, "exact_duplicate_groups": len(dups), "problems": len(problems)}, indent=2))
    for line in problems[:50] + [f"duplicate: {g}" for g in dups[:20]]:
        print(line)
    return not problems


def split(d: Path, out: Path, group: str, val: float, test: float, seed: int):
    rx = re.compile(group)
    groups = defaultdict(list)
    for p in images(d):
        m = rx.search(p.name)
        groups[m.group(1) if m else p.stem].append(p)
    keys = sorted(groups)
    random.Random(seed).shuffle(keys)
    n_test, n_val = round(len(keys) * test), round(len(keys) * val)
    parts = {"test": keys[:n_test], "val": keys[n_test:n_test + n_val], "train": keys[n_test + n_val:]}
    for name, ks in parts.items():
        for sub in ("images", "labels"):
            (out / name / sub).mkdir(parents=True, exist_ok=True)
        chosen = [p for k in ks for p in groups[k]]
        for p in chosen:
            shutil.copy(p, out / name / "images" / p.name)
            lab = d / "labels" / (p.stem + ".txt")
            if lab.is_file():
                shutil.copy(lab, out / name / "labels" / lab.name)
        print(f"{name}: {len(ks)} groups, {len(chosen)} images copied")


def coco(d: Path, out_json: Path, names: list[str]):
    doc = {"images": [], "annotations": [], "categories": [{"id": i + 1, "name": n} for i, n in enumerate(names)]}
    aid, skipped = 0, 0
    for iid, p in enumerate(images(d), 1):
        img = cv2.imread(str(p))
        if img is None:
            continue
        h, w = img.shape[:2]
        doc["images"].append({"id": iid, "file_name": p.name, "width": w, "height": h})
        lab = d / "labels" / (p.stem + ".txt")
        for line in (lab.read_text().splitlines() if lab.is_file() else []):
            if not line.strip():
                continue
            c, cx, cy, bw, bh = line.split()
            bw_, bh_ = float(bw) * w, float(bh) * h
            if bw_ < 1 or bh_ < 1:  # degenerate (zero-area) annotation: dropped, counted below
                skipped += 1
                continue
            aid += 1
            doc["annotations"].append({"id": aid, "image_id": iid, "category_id": int(c) + 1, "iscrowd": 0, "area": bw_ * bh_,
                                       "bbox": [float(cx) * w - bw_ / 2, float(cy) * h - bh_ / 2, bw_, bh_]})
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(doc))
    print(f"{len(doc['images'])} images, {aid} boxes, {skipped} degenerate boxes dropped -> {out_json}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("pennfudan"); s.add_argument("src", type=Path); s.add_argument("out", type=Path)
    s = sub.add_parser("validate"); s.add_argument("dir", type=Path); s.add_argument("--classes", type=int, required=True)
    s = sub.add_parser("split"); s.add_argument("dir", type=Path); s.add_argument("out", type=Path)
    s.add_argument("--group", default=r"^(.+?)_?\d+\.\w+$"); s.add_argument("--val", type=float, default=0.15)
    s.add_argument("--test", type=float, default=0.15); s.add_argument("--seed", type=int, default=42)
    s = sub.add_parser("coco"); s.add_argument("dir", type=Path); s.add_argument("out", type=Path); s.add_argument("--names", required=True)
    a = ap.parse_args()
    if a.cmd == "pennfudan":
        pennfudan(a.src, a.out)
    elif a.cmd == "validate":
        sys.exit(0 if validate(a.dir, a.classes) else 1)
    elif a.cmd == "split":
        split(a.dir, a.out, a.group, a.val, a.test, a.seed)
    else:
        coco(a.dir, a.out, a.names.split(","))

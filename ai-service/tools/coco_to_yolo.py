"""Turn a COCO instances file into the YOLO label layout tools/evaluate.py reads, for ONE category (default: person).

Why: Penn-Fudan has no empty scenes and misses many people, so its precision is only a lower bound. COCO val2017 labels
every person and has many images without any person (this script prints the exact count), so it also measures false
alarms on empty scenes.

  1. Download (about 1 GB; check the COCO terms of use first): val2017.zip and annotations_trainval2017.zip from cocodataset.org
  2. Unzip the images into <root>/images, then:
       python tools/coco_to_yolo.py --annotations instances_val2017.json --root <root>
  3. python tools/evaluate.py --data <root> --classes 0=Human --sweep 0.25,0.35,0.45,0.55 --out reports/<date>-person-coco-val2017

Every image gets a label file; images without the category get an empty one (= a negative).
"iscrowd" regions are skipped and counted: people inside them that the model finds score as false positives,
so precision stays slightly pessimistic.
"""
import argparse
import json
from collections import defaultdict
from pathlib import Path


def convert(coco: dict, category: str = "person") -> tuple[dict[str, list[str]], int]:
    """Returns {image file name: YOLO lines "0 cx cy w h" (0..1)} and the number of skipped crowd boxes."""
    cat_ids = {c["id"] for c in coco["categories"] if c["name"] == category}
    if not cat_ids:
        raise SystemExit(f"category {category!r} not in this file")
    images = {im["id"]: im for im in coco["images"]}
    lines, crowd = defaultdict(list), 0
    for a in coco["annotations"]:
        if a["category_id"] not in cat_ids:
            continue
        if a.get("iscrowd"):
            crowd += 1
            continue
        im = images[a["image_id"]]
        x, y, w, h = a["bbox"]
        if w <= 0 or h <= 0:
            continue
        lines[im["file_name"]].append(f"0 {(x + w / 2) / im['width']:.6f} {(y + h / 2) / im['height']:.6f} {w / im['width']:.6f} {h / im['height']:.6f}")
    return {im["file_name"]: lines.get(im["file_name"], []) for im in coco["images"]}, crowd


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--annotations", type=Path, required=True)
    ap.add_argument("--root", type=Path, required=True, help="folder that contains images/; labels/ is written next to it")
    ap.add_argument("--category", default="person")
    args = ap.parse_args()
    labels, crowd = convert(json.loads(args.annotations.read_text(encoding="utf-8")), args.category)
    out = args.root / "labels"
    out.mkdir(parents=True, exist_ok=True)
    for name, rows in labels.items():
        (out / (Path(name).stem + ".txt")).write_text("\n".join(rows), encoding="utf-8")
    positives = sum(1 for rows in labels.values() if rows)
    print(f"{len(labels)} images: {positives} with {args.category}, {len(labels) - positives} negatives, "
          f"{sum(map(len, labels.values()))} boxes, {crowd} crowd regions skipped -> {out}")

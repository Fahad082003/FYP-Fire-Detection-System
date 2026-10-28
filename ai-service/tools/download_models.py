"""Download the model weights listed in models/models.json from their official URLs and verify sha256.

  python tools/download_models.py
Entries without a URL (e.g. your own fine-tuned fire_smoke model) are skipped with a note.
"""
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sentinel_ai.detector import load_manifest, sha256  # noqa: E402

MODELS = Path(__file__).resolve().parent.parent / "models"

for spec in load_manifest(MODELS):
    target = MODELS / spec["file"]
    if not spec.get("url"):
        print(f"{spec['name']}: no official download, place your own {spec['file']} here (see training/README.md)")
        continue
    if target.is_file() and sha256(target) == spec["sha256"]:
        print(f"{spec['name']}: {target.name} already present and verified")
        continue
    print(f"{spec['name']}: downloading {spec['url']}")
    tmp = target.with_suffix(".part")
    urllib.request.urlretrieve(spec["url"], tmp)
    digest = sha256(tmp)
    if digest != spec["sha256"]:
        tmp.unlink()
        sys.exit(f"{spec['name']}: sha256 mismatch ({digest}), file deleted")
    tmp.replace(target)
    print(f"{spec['name']}: verified {target.name}")

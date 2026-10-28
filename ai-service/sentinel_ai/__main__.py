import argparse
import json
import logging
from pathlib import Path

from .service import serve

ROOT = Path(__file__).resolve().parent.parent

parser = argparse.ArgumentParser(prog="sentinel_ai", description="Sentinel AI inference service")
parser.add_argument("--config", type=Path, default=ROOT / "config.json", help="cameras/settings JSON (default: ai-service/config.json)")
parser.add_argument("--models", type=Path, default=ROOT / "models", help="folder with models.json and .onnx weights")
args = parser.parse_args()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
config = json.loads(args.config.read_text(encoding="utf-8")) if args.config.is_file() else {}
serve(config, args.models)

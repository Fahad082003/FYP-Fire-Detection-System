"""REAL-WEIGHTS golden test: the installed models (sha256-checked by the normal loader) on samples/fire-test.avi.
No fake session. Skipped with the reason when onnxruntime, a weight file or the sample is missing (nothing is downloaded).
Slow (~20 s on CPU). Run: python -m unittest discover -s tests"""
import importlib.util
import unittest
from pathlib import Path

import cv2

from sentinel_ai.detector import load_manifest
from sentinel_ai.service import Engine

ROOT = Path(__file__).resolve().parent.parent
MODELS, SAMPLE = ROOT / "models", ROOT / "samples" / "fire-test.avi"
FRAMES = 40


def missing() -> str | None:
    if importlib.util.find_spec("onnxruntime") is None:
        return "onnxruntime is not installed"
    if not (MODELS / "models.json").is_file():
        return f"missing {MODELS / 'models.json'}"
    files = [MODELS / m["file"] for m in load_manifest(MODELS)]
    gone = [p.name for p in files + [SAMPLE] if not p.is_file()]
    return f"missing local file(s): {', '.join(gone)}" if gone else None


@unittest.skipIf(missing(), missing())
class RealWeightsTests(unittest.TestCase):
    def test_both_models_load_and_the_sample_fire_is_confirmed_over_frames(self):
        engine = Engine(MODELS)
        self.assertEqual(sorted(d.name for d in engine.detectors), ["fire_smoke", "person"])
        self.assertEqual(engine.missing, [])

        pipe = engine.pipeline("golden-test", simulated=True)
        need = pipe.s["confirm_frames"]
        cap = cv2.VideoCapture(str(SAMPLE))
        payloads = []
        while len(payloads) < FRAMES:
            ok, frame = cap.read()
            if not ok:
                break
            payloads.append(engine.run(pipe, frame, temporal=True)[0])
        cap.release()
        self.assertGreaterEqual(len(payloads), need, "the sample is shorter than the confirmation window")

        self.assertTrue(any(p["fireDetected"] for p in payloads), "no confirmed fire on the known fire sample")
        self.assertFalse(any(p["fireDetected"] for p in payloads[:need - 1]))  # nothing can be confirmed before frame `need`
        fires = [d for p in payloads for d in p["detections"] if d["category"] == "Fire"]
        self.assertTrue(all(d["hits"] >= need and "confirmed:" in d["explanation"] for d in fires))

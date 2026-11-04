"""REAL-WEIGHTS golden tests: the installed models (sha256-checked by the normal loader) on samples/fire-test.avi (fire, and smoke
without fire in some frames) and on synthetic look-alikes.
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
PREDICTIONS = ROOT / "reports" / "2026-10-19-fire_smoke-yolox_tiny-dfire-test" / "predictions.jsonl"
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

    def test_smoke_is_confirmed_on_its_own_with_its_own_track_and_confidence(self):
        engine = Engine(MODELS)
        pipe = engine.pipeline("golden-smoke", simulated=True)
        need = pipe.s["confirm_frames"]
        cap = cv2.VideoCapture(str(SAMPLE))
        payloads = []
        while len(payloads) < FRAMES:
            ok, frame = cap.read()
            if not ok:
                break
            payloads.append(engine.run(pipe, frame, temporal=True)[0])
        cap.release()

        smoke = [d for p in payloads for d in p["detections"] if d["category"] == "Smoke"]
        self.assertTrue(smoke, "no confirmed smoke on the sample (on 2026-11-03 it was confirmed in frames 2-9, 0-based)")
        self.assertTrue(all(d["hits"] >= need and "confirmed:" in d["explanation"] and 0.4 <= d["confidence"] <= 1 for d in smoke))
        self.assertTrue(all(d["trackId"].startswith("S") for d in smoke))  # smoke has its own tracks, never a fire track
        alone = [p for p in payloads if not p["fireDetected"] and any(d["category"] == "Smoke" for d in p["detections"])]
        self.assertTrue(alone, "smoke was only ever reported together with fire")
        self.assertTrue(all(p["fireSeverity"] is None for p in alone))  # smoke alone is never rated as a fire

    def test_synthetic_look_alikes_give_no_confirmed_fire_or_smoke(self):
        """SYNTHETIC scenes from tools/false_alarm_eval.py (sunset, blinking red lights, kitchen steam, vape, red object, flickering
        warm glow); ground truth: no fire, no smoke. The model does not fire on these plain drawings; this is NOT evidence for
        real sunsets, steam or vapes (no labelled footage of those exists in this project)."""
        ev = _tool("false_alarm_eval")
        result = ev.synthetic(Engine(MODELS), frames=10)
        self.assertEqual(sorted(result), sorted(["sunset", "red lights (blinking)", "kitchen steam", "vape", "red object", "flickering warm glow"]))
        self.assertEqual({k: (v["fireFrames"], v["smokeFrames"]) for k, v in result.items()}, {k: (0, 0) for k in result})


class FalseAlarmEvidenceTests(unittest.TestCase):
    """The REAL labelled D-Fire test predictions (4,306 images with ground truth): why no extra false-alarm rule is added."""

    @unittest.skipUnless(PREDICTIONS.is_file(), f"missing {PREDICTIONS}")
    def test_every_candidate_rule_costs_more_real_detections_than_it_saves_false_alarms(self):
        rules = _tool("false_alarm_eval").real_images(PREDICTIONS, 0.4)["rules"]
        base = rules.pop("none (current pipeline)")
        self.assertEqual({c: (m["tp"], m["fp"], m["fn"], m["tn"]) for c, m in base.items()},
                         {"Fire": (1039, 19, 76, 3172), "Smoke": (1920, 63, 161, 2162)})
        for name, rule in rules.items():
            for cat in ("Fire", "Smoke"):
                saved = base[cat]["fp"] - rule[cat]["fp"]
                lost = rule[cat]["fn"] - base[cat]["fn"]
                self.assertGreaterEqual(lost, saved, f"{name} on {cat}: {saved} false alarms fewer for {lost} missed")
            self.assertGreater(rules[name]["Fire"]["fn"] + rules[name]["Smoke"]["fn"], base["Fire"]["fn"] + base["Smoke"]["fn"], name)


def _tool(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

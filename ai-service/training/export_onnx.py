"""Export a trained YOLOX checkpoint to the ONNX format sentinel_ai expects (raw head output, decoded by the service).

YOLOX 0.3.0's tools/export_onnx.py calls torch.onnx._export, which current PyTorch no longer has; this uses torch.onnx.export.
  python export_onnx.py --exp yolox_tiny_dfire.py --ckpt best_ckpt.pth --out fire_smoke_yolox.onnx
Needs: torch, YOLOX 0.3.0 importable (PYTHONPATH or pip -e), onnx.
"""
import argparse

import torch
from torch import nn
from yolox.exp import get_exp
from yolox.models.network_blocks import SiLU
from yolox.utils import replace_module

ap = argparse.ArgumentParser()
ap.add_argument("--exp", required=True)
ap.add_argument("--ckpt", required=True)
ap.add_argument("--out", required=True)
a = ap.parse_args()

exp = get_exp(a.exp, None)
model = exp.get_model().eval()
# weights_only=True: only tensors are unpickled, no code from the checkpoint runs.
# YOLOX stores best_ap as a numpy scalar, so allow just the numpy scalar/dtype types (data, not code).
import numpy as np
safe = [np._core.multiarray.scalar, np.dtype, type(np.dtype(np.float64))]
with torch.serialization.safe_globals(safe):
    ckpt = torch.load(a.ckpt, map_location="cpu", weights_only=True)
model.load_state_dict(ckpt["model"])
model = replace_module(model, nn.SiLU, SiLU)
model.head.decode_in_inference = False
h, w = exp.test_size
torch.onnx.export(model, torch.randn(1, 3, h, w), a.out, input_names=["images"], output_names=["output"], opset_version=11, dynamo=False)
print(f"exported {a.out} (input {h}x{w}, classes {exp.num_classes}, checkpoint epoch {ckpt.get('start_epoch', '?')})")

# Fire/smoke model: dataset, fine-tuning and evaluation

**Status: trained and installed (2026-10-15).**
- Training: Kaggle T4, 80 epochs, 14,621 training and 2,600 validation images.
- The result was exported locally with [export_onnx.py](export_onnx.py), because YOLOX 0.3.0's own exporter fails on current PyTorch.
- Metrics are in [../reports/README.md](../reports/README.md).
- The steps below are how to reproduce it.

## Why fine-tuning is needed
- The person model (YOLOX-Tiny, COCO) has no fire or smoke class.
- No official, permissively licensed, ready-made fire/smoke ONNX model was found.
- The D-Fire authors publish YOLOv5 weights, but they come with two problems:
  - They are `.pt` pickles on OneDrive, and a pickle can run code when it is loaded.
  - YOLOv5 is AGPL-3.0.

The plan is therefore to fine-tune COCO-pretrained YOLOX-Tiny (Apache-2.0) on D-Fire (CC0). Only an ONNX file comes back into this repo.

## Hardware check (development machine, 2026-10-13)
| Item | Found | Consequence |
|---|---|---|
| CPU | Intel Xeon W3520, 4 threads, **no AVX/AVX2** (SSE4.2 only) | ONNX Runtime CPU inference works (verified). Training is impractical: hours per epoch. |
| GPU | NVIDIA Quadro FX 380, 256 MB, no usable CUDA | No GPU training |
| RAM / disk | 16 GB / ~75 GB free | Enough for the dataset |

**Measured on 2026-10-13.**
- PyTorch 2.14.1 (CPU wheel) runs on this non-AVX CPU using generic kernels.
- One YOLOX training step (forward, backward and optimizer), batch 8, takes:

| Model / input size | Seconds per image | One D-Fire epoch (~17k images) | 80 epochs |
|---|---|---|---|
| YOLOX-Tiny @ 416 | 0.867 | 4.1 h | 13.6 days |
| YOLOX-Nano @ 416 | 0.549 | 2.6 h | 8.6 days |
| YOLOX-Nano @ 320 | 0.248 | 1.2 h | 3.9 days |

Data loading and augmentation add more time on top of these figures. **Full training must run on a CUDA GPU:** use [train_fire_smoke_colab.ipynb](train_fire_smoke_colab.ipynb) (Google Colab or Kaggle T4, or a cloud VM).

**CPU pilot (2026-10-13):**
- A small CPU run trained YOLOX-Nano @ 320 on a subset. It failed every acceptance criterion; see [../reports/README.md](../reports/README.md).
- Its script was removed after the GPU model was installed. A copy is in the `ai-service.zip` uploaded to Kaggle.

## Dataset: D-Fire
- Source: https://github.com/gaiasd/DFireDataset (download links are in its README: OneDrive, plus a Kaggle mirror). The official `D-Fire.zip` is 3,036 MB. OneDrive blocks scripted downloads (HTTP 403), so download it in a browser.
- License: **CC0 1.0** (public domain). The authors ask you to cite: de Venâncio, Lisboa, Barbosa, *An automatic fire detection system based on deep convolutional neural networks for low-power, resource-constrained devices*, Neural Computing and Applications, 2022.
- Content (from the dataset README, not verified here): more than 21,000 images with YOLO labels. **Class 0 = smoke, 1 = fire.** 9,838 images contain neither, so they serve as hard negatives.
- Use the dataset's own train/test split. Never train on the test images.

```bash
# 1. Validate: decodes every image, checks label ranges, finds exact duplicates
python tools/prepare_dataset.py validate D-Fire/train --classes 2
python tools/prepare_dataset.py validate D-Fire/test  --classes 2

# 2. Carve a validation set out of TRAIN, grouped by filename prefix, so frames of one source stay together
python tools/prepare_dataset.py split D-Fire/train work/dfire --val 0.15 --test 0

# 3. COCO json for YOLOX (category ids 1 = smoke, 2 = fire; YOLOX index 0 = smoke, 1 = fire, matching models/models.json)
python tools/prepare_dataset.py coco work/dfire/train work/dfire_coco/annotations/train.json --names smoke,fire
python tools/prepare_dataset.py coco work/dfire/val   work/dfire_coco/annotations/val.json   --names smoke,fire
#    then copy images: work/dfire/train/images -> work/dfire_coco/train2017, work/dfire/val/images -> work/dfire_coco/val2017
```

Your own CCTV clips (only with authorisation): extract frames, label them in a tool such as CVAT or Label Studio, and keep each clip in a single split by giving files a `<clipId>_<frame>.jpg` name. Auto-generated labels are **not** ground truth until a person has checked them.

## Fine-tune (on the GPU machine)
```bash
git clone --branch 0.3.0 https://github.com/Megvii-BaseDetection/YOLOX && cd YOLOX
export PYTHONPATH=$PWD   # `pip install -e .` fails: YOLOX's old setup.py needs torch at build time
pip install loguru tabulate pycocotools thop ninja onnx
wget https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_tiny.pth   # COCO-pretrained start point
export DFIRE_COCO=/path/to/work/dfire_coco
python tools/train.py -f /path/to/ai-service/training/yolox_tiny_dfire.py -d 1 -b 32 --fp16 -o -c yolox_tiny.pth
#   checkpoints + logs: YOLOX_outputs/yolox_tiny_dfire/ (best_ckpt.pth = best validation AP)
# YOLOX's own tools/export_onnx.py fails on current PyTorch; use ours:
python /path/to/ai-service/training/export_onnx.py --exp /path/to/ai-service/training/yolox_tiny_dfire.py \
       --ckpt YOLOX_outputs/yolox_tiny_dfire/best_ckpt.pth --out fire_smoke_yolox.onnx
```
Keep alongside the weights: the exp file, `pip freeze`, the training log, and the evaluation report below.

## Install the fine-tuned model
1. Copy `fire_smoke_yolox.onnx` to `ai-service/models/`.
2. Put its sha256 into `models/models.json` (entry `fire_smoke`, field `sha256`). The service refuses a file whose hash does not match.
3. Restart the service. `/health` should now report `fire_smoke: loaded`.

**Rollback:** delete or rename the `.onnx` file. The service then reports the model as missing, and fire/smoke detection stops. Nothing switches silently to fake output.

## Evaluate: identical protocol for the baseline and every candidate
```bash
python tools/evaluate.py --data D-Fire/test --classes 0=Smoke,1=Fire --sweep 0.25,0.3,0.35,0.4,0.5 --out reports/fire-dfire-test
```
The report gives, per threshold and class:
- TP, FP, FN, precision, recall and F1 at IoU 0.5.
- An image-level confusion matrix (tp, fp, fn, tn) and the false-positive rate on negative images.
- Latency (mean, p50, p95).
- Lists of false-negative and false-positive images, to inspect failure cases.

Also slice the test set into night, fog/steam, small-or-distant fire, and crowded scenes. Evaluate each slice separately.

## Acceptance criteria (development)
| Class | Criterion | Why |
|---|---|---|
| Fire | image-level recall ≥ 0.90 **and** false-positive rate on negative images ≤ 0.10 | A missed fire is the worst outcome. False positives are further reduced live by the 3-frame confirmation. |
| Smoke | image-level recall ≥ 0.80, false-positive rate ≤ 0.15 | Smoke is harder: fog, steam and dust look similar. |
| Fire/smoke boxes | precision ≥ 0.60 at IoU 0.5 | The boxes drive the area-%, severity and AI danger zones. |
| Person | box recall ≥ 0.85 and precision ≥ 0.80 at IoU 0.5 | People in danger zones must not be missed. |
| Latency | p95 ≤ 1000 ms on the deployment CPU | Allows 1 frame/s per camera. |

Overall accuracy is not used, because negatives dominate the data and would hide missed fires. Passing these offline numbers does **not** make the system safe for real emergencies: keep human oversight, smoke detectors and standard procedures.

## Final decision record
Fill this in after each evaluation. Use one of:
- *Evaluated: meets the defined development criteria*
- *Evaluated: improvement required*
- *Fine-tuned: improvement verified against baseline*
- *Not evaluated: dependency missing*

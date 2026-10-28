"""YOLOX experiment: fine-tune COCO-pretrained YOLOX-Tiny on D-Fire (2 classes: 0 = smoke, 1 = fire).

Expected layout (made by tools/prepare_dataset.py coco):
  $DFIRE_COCO/annotations/train.json, val.json
  $DFIRE_COCO/train2017/*.jpg   (train images)    $DFIRE_COCO/val2017/*.jpg (val images)
Run inside a clone of https://github.com/Megvii-BaseDetection/YOLOX (Apache-2.0) on a CUDA GPU; see README.md here.
"""
import os

from yolox.exp import Exp as MyExp


class Exp(MyExp):
    def __init__(self):
        super().__init__()
        # YOLOX-Tiny architecture: same size/decoder as the person model, so sentinel_ai runs it unchanged.
        self.depth, self.width = 0.33, 0.375
        self.input_size = self.test_size = (416, 416)
        self.random_size = (10, 20)
        self.mosaic_scale = (0.5, 1.5)
        self.enable_mixup = False
        self.num_classes = 2
        self.data_dir = os.environ.get("DFIRE_COCO", "datasets/dfire_coco")
        self.train_ann, self.val_ann = "train.json", "val.json"
        self.max_epoch = 80
        self.no_aug_epochs = 10
        self.eval_interval = 5  # best_ckpt.pth = best validation AP (checkpoint selection)
        self.exp_name = "yolox_tiny_dfire"

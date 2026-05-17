"""Wahoo Bay v2 inference pipeline.

Replaces the Fishial-vendored 866-class ArcFace + sub-centre + KNN
classifier with a from-scratch DINOv2 ViT-S/14 trained on our
volunteer-labelled crops + iNaturalist supplements (223 classes).
The detector is unchanged — same ultralytics YOLO v26 checkpoint —
only the per-bbox species head is swapped.

Why a separate backend instead of editing fishial.py in place:
* Easy revert. `CLASSIFIER_BACKEND=fishial` (the old default lives on
  in config.py) restores the previous behaviour with zero code changes.
* Different vendoring. Fishial pulls in a third-party `inference`
  module via `sys.path.insert`; v2 is pure timm + torch.
* Clean provenance. The dashboard's `classifier_sha256` should point
  at whichever model actually produced a row; the backend switch lets
  provenance.py hash the active checkpoint without ambiguity.

Performance: `torch.compile(model, mode="reduce-overhead")` builds a
CUDA graph that drops single-crop latency from ~3.7 ms eager to
~0.7 ms (5×). First call after load is a long compile (~30 s); we
warm up at construction so steady-state frame processing isn't paying
that cost. Batched inference fall-through means N detections in a
frame go through one forward pass, not N — meaningful when the reef
is busy.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import cv2
import numpy as np
import timm
import torch
import torch.nn.functional as F
from ultralytics import YOLO

from .pipeline_types import FishDetection, Prediction

log = logging.getLogger(__name__)

# ImageNet stats — must match scripts/train_baseline.py val transform.
_IMAGENET_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
_IMAGENET_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)


def _strip_module_prefix(state_dict: dict) -> dict:
    """DataParallel / DDP wraps params under "module." — drop it so the
    keys line up with a plain timm model."""
    if not state_dict:
        return state_dict
    if all(k.startswith("module.") for k in state_dict.keys()):
        return {k[len("module."):]: v for k, v in state_dict.items()}
    return state_dict


def _preprocess_crop(
    frame_bgr: np.ndarray,
    bbox: tuple[int, int, int, int],
    out_size: int,
) -> np.ndarray:
    """Mirror the training val_tf: pad-to-square crop, resize shorter side
    to int(out_size * 1.14), centre-crop to out_size, BGR→RGB,
    [0,1]/ImageNet normalise. Returns a CHW float32 array — caller stacks
    + moves to device. Inline (no torchvision) so we don't pay the per-
    frame PIL hop."""
    H, W = frame_bgr.shape[:2]
    x1, y1, x2, y2 = bbox
    # Clamp to frame bounds — detector occasionally emits boxes that
    # spill a few pixels past the edge after letterbox un-pad.
    x1 = max(0, min(int(x1), W - 1))
    y1 = max(0, min(int(y1), H - 1))
    x2 = max(x1 + 1, min(int(x2), W))
    y2 = max(y1 + 1, min(int(y2), H))
    crop = frame_bgr[y1:y2, x1:x2]
    if crop.size == 0:
        crop = np.zeros((out_size, out_size, 3), dtype=np.uint8)

    # Resize shorter side to int(out_size * 1.14) then centre-crop, same as
    # val transform. This matches the training-time evaluation exactly so
    # there's no train/inference skew at the resize step.
    resize_side = int(out_size * 1.14)
    h, w = crop.shape[:2]
    if h < w:
        new_h, new_w = resize_side, int(round(w * resize_side / h))
    else:
        new_h, new_w = int(round(h * resize_side / w)), resize_side
    crop = cv2.resize(crop, (new_w, new_h), interpolation=cv2.INTER_AREA)
    # Centre crop to out_size.
    top = (new_h - out_size) // 2
    left = (new_w - out_size) // 2
    crop = crop[top:top + out_size, left:left + out_size]
    if crop.shape[:2] != (out_size, out_size):
        # Edge case (1px rounding): pad-then-trim.
        canvas = np.zeros((out_size, out_size, 3), dtype=crop.dtype)
        h2, w2 = crop.shape[:2]
        canvas[:h2, :w2] = crop
        crop = canvas

    crop = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    crop = (crop - _IMAGENET_MEAN) / _IMAGENET_STD
    return np.transpose(crop, (2, 0, 1))  # HWC → CHW


class ClassifierV2Pipeline:
    """Detector + DINOv2 ViT-S/14 classifier. Same `process_frame` shape
    as FishialPipeline so inference_proc.py can swap them via env var."""

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.device = cfg.device

        # ---- detector (unchanged) ----
        self.detector = YOLO(cfg.detector_path)
        self.detector.to(self.device)
        dummy = np.zeros((cfg.det_imgsz, cfg.det_imgsz, 3), dtype=np.uint8)
        self.detector.predict(
            source=dummy, imgsz=cfg.det_imgsz,
            conf=cfg.det_conf_threshold, iou=cfg.det_iou_threshold,
            device=self.device, verbose=False,
        )
        log.info("v2: detector loaded %s on %s", cfg.detector_path, self.device)

        # ---- classifier ----
        classes_path = Path(cfg.classifier_v2_classes_json)
        with classes_path.open() as f:
            class_names_raw: list[str] = json.load(f)
        # Training emits class names with underscores ("Haemulon_chrysargyreum");
        # the rest of the system speaks binomials with spaces. Convert once.
        self.class_names: list[str] = [c.replace("_", " ") for c in class_names_raw]
        num_classes = len(self.class_names)
        if num_classes < 2:
            raise RuntimeError(f"v2 classes.json has {num_classes} entries; expected >=2")

        self.img_size = int(cfg.classifier_v2_input_size)
        backbone = cfg.classifier_v2_arch
        self.model = timm.create_model(
            backbone,
            pretrained=False,
            num_classes=num_classes,
            img_size=self.img_size,
        )
        ckpt_path = Path(cfg.classifier_v2_checkpoint)
        ckpt = torch.load(ckpt_path, map_location=self.device, weights_only=False)
        sd = ckpt.get("model", ckpt.get("state_dict", ckpt))
        sd = _strip_module_prefix(sd)
        missing, unexpected = self.model.load_state_dict(sd, strict=False)
        if missing or unexpected:
            log.warning(
                "v2: load_state_dict — %d missing, %d unexpected (likely harmless tags)",
                len(missing), len(unexpected),
            )
        self.model = self.model.to(self.device).eval()

        # Cast weights to autocast dtype so memory traffic + matmuls run
        # in bf16 without per-call autocast overhead. fp32 fallback keeps
        # the option for debugging.
        self.autocast_dtype = self._autocast_dtype()
        if self.autocast_dtype is not None:
            self.model = self.model.to(self.autocast_dtype)
        self.model = self.model.to(memory_format=torch.channels_last)

        # torch.compile reduce-overhead = build a CUDA graph on first call.
        # We warm it up below with batch sizes the worker is likely to see
        # so the steady-state path doesn't trigger a recompile.
        if bool(cfg.classifier_v2_compile):
            t_c0 = time.time()
            self.model = torch.compile(self.model, mode="reduce-overhead")
            warmup_batches = [1, 2, 4, 8]
            with torch.no_grad():
                for bs in warmup_batches:
                    dummy_t = torch.zeros(
                        (bs, 3, self.img_size, self.img_size),
                        device=self.device,
                        dtype=self.autocast_dtype or torch.float32,
                    ).to(memory_format=torch.channels_last)
                    for _ in range(3):
                        self.model(dummy_t)
            if str(self.device).startswith("cuda"):
                torch.cuda.synchronize()
            log.info(
                "v2: torch.compile warmup done in %.1fs (batches=%s)",
                time.time() - t_c0, warmup_batches,
            )
        else:
            log.info("v2: torch.compile disabled (CLASSIFIER_V2_COMPILE=0)")

        log.info(
            "v2: classifier ready — arch=%s, classes=%d, img_size=%d, dtype=%s",
            backbone, num_classes, self.img_size, self.autocast_dtype or "fp32",
        )

    def _autocast_dtype(self) -> torch.dtype | None:
        m = (self.cfg.classifier_autocast or "off").strip().lower()
        if m in ("off", "none", "fp32", "float32", ""):
            return None
        if m in ("fp16", "float16", "half"):
            return torch.float16
        if m in ("bf16", "bfloat16"):
            return torch.bfloat16
        log.warning("v2: unknown CLASSIFIER_AUTOCAST=%r; falling back to fp32", m)
        return None

    def _batched_forward(self, crops: np.ndarray) -> torch.Tensor:
        """crops: (N, 3, H, W) float32 ndarray (already normalised).
        Returns (N, num_classes) softmax probabilities as a CPU tensor."""
        dtype = self.autocast_dtype or torch.float32
        x = torch.from_numpy(crops).to(self.device, dtype=dtype, non_blocking=True)
        x = x.to(memory_format=torch.channels_last)
        with torch.no_grad():
            logits = self.model(x)
        # softmax in fp32 to avoid bf16 saturation in the tail of the
        # distribution; the head output is small so the cost is trivial.
        probs = F.softmax(logits.float(), dim=-1)
        return probs.cpu()

    def process_frame(self, frame_bgr: np.ndarray) -> list[FishDetection]:
        t0 = time.time()
        r = self.detector.predict(
            source=frame_bgr,
            imgsz=self.cfg.det_imgsz,
            conf=self.cfg.det_conf_threshold,
            iou=self.cfg.det_iou_threshold,
            device=self.device,
            verbose=False,
        )[0]
        boxes = r.boxes.data.cpu().numpy() if r.boxes is not None else np.empty((0, 6))
        t1 = time.time()

        if not len(boxes):
            log.debug("v2 frame: 0 fish (det=%.1fms)", (t1 - t0) * 1000)
            return []

        # Split detections by det_conf — only run the classifier on the
        # likely-real ones. Same gating as Fishial path so the reviewer
        # queue characteristics don't shift on cutover.
        threshold = float(self.cfg.min_classify_det_conf)
        keep_idx = [i for i, b in enumerate(boxes) if float(b[4]) >= threshold]

        probs_by_idx: dict[int, np.ndarray] = {}
        if keep_idx:
            crops = np.stack(
                [
                    _preprocess_crop(
                        frame_bgr,
                        tuple(boxes[i][:4].tolist()),
                        self.img_size,
                    )
                    for i in keep_idx
                ],
                axis=0,
            )
            probs = self._batched_forward(crops).numpy()
            for j, i in enumerate(keep_idx):
                probs_by_idx[i] = probs[j]
        t2 = time.time()

        topk_n = int(self.cfg.classifier_topk)
        min_acc = float(self.cfg.min_accept_accuracy)
        out: list[FishDetection] = []
        for i, box in enumerate(boxes):
            x1, y1, x2, y2, conf, _cls = box.tolist()
            topk: list[Prediction] = []
            p = probs_by_idx.get(i)
            if p is not None:
                # argsort descending; take top-k that clear min_accept_accuracy
                order = np.argpartition(-p, min(topk_n, len(p) - 1))[:topk_n]
                order = order[np.argsort(-p[order])]
                for k in order:
                    acc = float(p[k])
                    if acc < min_acc:
                        continue
                    name = self.class_names[int(k)]
                    topk.append(Prediction(
                        name=name,
                        # No external taxonomy ID source for v2 yet — use the
                        # binomial as the species_id so detection_events
                        # group/filter queries (best_species_id) stay sane.
                        # Pre-cutover Fishial events keep their UUIDs.
                        species_id=name,
                        accuracy=acc,
                    ))
            out.append(FishDetection(
                bbox=(int(x1), int(y1), int(x2), int(y2)),
                det_conf=float(conf),
                topk=topk,
            ))

        log.debug(
            "v2 frame: %d fish (%d classified, det=%.1fms, cls=%.1fms)",
            len(out), len(keep_idx), (t1 - t0) * 1000, (t2 - t1) * 1000,
        )
        return out

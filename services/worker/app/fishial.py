"""Thin wrapper around Fishial's detector + classifier checkpoints.

Detector: ultralytics YOLO on model.pt (v26 nano).
Classifier: Fishial TorchScript bundle loaded via their FishInferenceEngine
(vendored at runtime by adding CLASSIFIER_INFERENCE_MODULE_DIR to sys.path).
"""
from __future__ import annotations

import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import numpy as np
import torch
from ultralytics import YOLO

log = logging.getLogger(__name__)


@dataclass
class Prediction:
    name: str
    species_id: Optional[str]
    accuracy: float


@dataclass
class FishDetection:
    bbox: tuple[int, int, int, int]
    det_conf: float
    topk: List[Prediction]

    @property
    def best(self) -> Optional[Prediction]:
        return self.topk[0] if self.topk else None


class _AutocastForward:
    """Wraps the classifier's model so its forward() runs under
    torch.autocast and the outputs are cast back to fp32 before the
    vendor's scoring path consumes them. Necessary because the vendor's
    natural-centroid scoring does a scatter_reduce that requires the
    embedding dtype to match the centroid tensor (fp32). Forwarding
    attribute access keeps things like `.arcface_head.weight` reachable."""

    def __init__(self, inner, device: str, dtype: torch.dtype) -> None:
        # bypass __setattr__ so __getattr__ doesn't recurse looking for these
        self.__dict__["_inner"] = inner
        self.__dict__["_device_type"] = "cuda" if str(device).startswith("cuda") else "cpu"
        self.__dict__["_dtype"] = dtype

    def __call__(self, x):
        with torch.autocast(device_type=self._device_type, dtype=self._dtype):
            out = self._inner(x)
        # cast tensor outputs back to fp32 for compatibility with the
        # downstream scoring path (centroid scatter_reduce, KNN)
        if isinstance(out, (list, tuple)):
            return type(out)(o.float() if torch.is_tensor(o) else o for o in out)
        return out.float() if torch.is_tensor(out) else out

    def __getattr__(self, name):
        return getattr(self.__dict__["_inner"], name)


class FishialPipeline:
    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.device = cfg.device

        self.detector = YOLO(cfg.detector_path)
        self.detector.to(self.device)
        # warm up
        dummy = np.zeros((cfg.det_imgsz, cfg.det_imgsz, 3), dtype=np.uint8)
        self.detector.predict(
            source=dummy, imgsz=cfg.det_imgsz,
            conf=cfg.det_conf_threshold, iou=cfg.det_iou_threshold,
            device=self.device, verbose=False,
        )
        log.info("Detector loaded: %s on %s", cfg.detector_path, self.device)

        # Vendor Fishial's classifier inference module at runtime.
        module_dir = Path(cfg.classifier_inference_module_dir)
        if str(module_dir) not in sys.path:
            sys.path.insert(0, str(module_dir))
        from inference import FishInferenceEngine, InferenceConfig  # type: ignore

        self.classifier = FishInferenceEngine.from_bundle(
            cfg.classifier_path,
            input_size=(cfg.classifier_input_h, cfg.classifier_input_w),
            device=self.device,
            config=InferenceConfig(
                max_unique_classes=cfg.classifier_topk,
                return_emb=False,
                k_centers=3,
            ),
        )

        # Wrap the classifier's forward in an autocast shim if requested.
        ac_dtype = self._autocast_dtype()
        if ac_dtype is not None:
            self.classifier.model = _AutocastForward(
                self.classifier.model, self.device, ac_dtype,
            )
            log.info("Classifier autocast: enabled (%s)", ac_dtype)
        else:
            log.info("Classifier autocast: disabled (fp32)")

        log.info(
            "Classifier loaded: %s (%d classes) on %s",
            cfg.classifier_path,
            len(self.classifier.class_mapping),
            self.device,
        )

    def _autocast_dtype(self) -> Optional[torch.dtype]:
        m = (self.cfg.classifier_autocast or "off").strip().lower()
        if m in ("off", "none", "fp32", "float32", ""):
            return None
        if m in ("fp16", "float16", "half"):
            return torch.float16
        if m in ("bf16", "bfloat16"):
            return torch.bfloat16
        log.warning("unknown CLASSIFIER_AUTOCAST=%r; falling back to fp32", m)
        return None

    def process_frame(self, frame_bgr: np.ndarray) -> List[FishDetection]:
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
            log.debug("frame: 0 fish (det=%.1fms)", (t1 - t0) * 1000)
            return []

        # Split detections by det_conf: high-confidence ones go through the
        # classifier (bf16/fp16 autocast if configured); low-confidence ones
        # are still persisted as events but with an empty top-K so the
        # classifier doesn't pay for likely-noise detections.
        threshold = float(self.cfg.min_classify_det_conf)
        keep_idx = [i for i, b in enumerate(boxes) if float(b[4]) >= threshold]

        results_by_idx: dict[int, object] = {}
        if keep_idx:
            bboxes = [boxes[i][:4].tolist() for i in keep_idx]
            frames = [frame_bgr] * len(keep_idx)
            # Autocast is applied inside the classifier's forward via
            # _AutocastForward (set up at init time) — outputs come back
            # in fp32, so the downstream scoring path is unaffected.
            results = self.classifier.predict(
                frames,
                bboxes=bboxes,
                method=self.cfg.classifier_method,
            )
            if not isinstance(results, list):
                results = [results]
            for i, fr in zip(keep_idx, results):
                results_by_idx[i] = fr
        t2 = time.time()

        out: List[FishDetection] = []
        for i, box in enumerate(boxes):
            x1, y1, x2, y2, conf, _cls = box.tolist()
            topk: List[Prediction] = []
            fr = results_by_idx.get(i)
            if fr is not None:
                for p in fr.top_k:
                    if p.accuracy < self.cfg.min_accept_accuracy:
                        continue
                    topk.append(Prediction(
                        name=p.name,
                        species_id=p.species_id,
                        accuracy=float(p.accuracy),
                    ))
            out.append(FishDetection(
                bbox=(int(x1), int(y1), int(x2), int(y2)),
                det_conf=float(conf),
                topk=topk,  # empty if det_conf < min_classify_det_conf
            ))

        log.debug(
            "frame: %d fish (%d classified, det=%.1fms, cls=%.1fms)",
            len(out), len(keep_idx), (t1 - t0) * 1000, (t2 - t1) * 1000,
        )
        return out

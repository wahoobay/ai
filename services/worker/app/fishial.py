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
        log.info(
            "Classifier loaded: %s (%d classes) on %s",
            cfg.classifier_path,
            len(self.classifier.class_mapping),
            self.device,
        )

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

        # classify all detections in a single batched forward pass
        bboxes = [b[:4].tolist() for b in boxes]
        frames = [frame_bgr] * len(boxes)
        results = self.classifier.predict(
            frames,
            bboxes=bboxes,
            method=self.cfg.classifier_method,
        )
        if not isinstance(results, list):
            results = [results]
        t2 = time.time()

        out: List[FishDetection] = []
        for box, fr in zip(boxes, results):
            x1, y1, x2, y2, conf, _cls = box.tolist()
            topk: List[Prediction] = []
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
                topk=topk,
            ))

        log.debug(
            "frame: %d fish (det=%.1fms, cls=%.1fms)",
            len(out), (t1 - t0) * 1000, (t2 - t1) * 1000,
        )
        return out

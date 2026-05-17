"""Shared dataclasses for the inference pipeline. Lives in its own module
so both backends (fishial.py, classifier_v2.py) can use the same types
without one having to import the other — that keeps backend selection
clean and means downstream consumers (smoother, persistence, overlay)
don't care which backend produced the detections."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Prediction:
    name: str
    species_id: str | None
    accuracy: float


@dataclass
class FishDetection:
    bbox: tuple[int, int, int, int]
    det_conf: float
    topk: list[Prediction]

    @property
    def best(self) -> Prediction | None:
        return self.topk[0] if self.topk else None

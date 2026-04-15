"""Draw annotated overlays on frames for the live stream + saved snapshots."""
from __future__ import annotations

from typing import Iterable

import cv2
import numpy as np

# Deterministic palette so the same species gets a consistent colour across frames.
_PALETTE = [
    (66, 135, 245), (245, 158, 66), (66, 245, 111), (245, 66, 167),
    (245, 236, 66), (66, 245, 236), (191, 66, 245), (245, 66, 66),
    (129, 245, 66), (66, 110, 245), (245, 186, 66), (66, 245, 177),
]


def _colour(species_id: str | None) -> tuple[int, int, int]:
    if not species_id:
        return (180, 180, 180)
    return _PALETTE[hash(species_id) % len(_PALETTE)]


def annotate(
    frame_bgr: np.ndarray,
    detections: Iterable,
    show_det_conf: bool = True,
) -> np.ndarray:
    out = frame_bgr.copy()
    for d in detections:
        x1, y1, x2, y2 = d.bbox
        best = d.best
        col = _colour(best.species_id if best else None)
        cv2.rectangle(out, (x1, y1), (x2, y2), col, 2)

        if best:
            label = f"{best.name}  {best.accuracy:.2f}"
        else:
            label = "unknown"
        if show_det_conf:
            label += f"  det={d.det_conf:.2f}"

        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
        pad = 4
        ytxt = max(0, y1 - th - pad * 2)
        cv2.rectangle(out, (x1, ytxt), (x1 + tw + pad * 2, ytxt + th + pad * 2), col, -1)
        cv2.putText(
            out, label, (x1 + pad, ytxt + th + pad),
            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 1, cv2.LINE_AA,
        )
    return out

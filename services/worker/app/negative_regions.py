"""Per-camera "no-fish-zone" polygon filter.

The YOLO detector reliably emits false positives on stationary reef
geometry — most prominently the dark oval cut-outs in the SEAHIVE
artificial reef structure. They never move, the detector boxes them,
the classifier assigns a low-confidence species, and the result ends
up in the reviewer queue as garbage to be rejected.

Critical: a fish that swims THROUGH a hole must still be detected.
So we can't just drop everything whose center lands inside the
polygon — that would also wipe out every fish that happens to pass
across the hole. Instead, we drop a detection only when its bbox
*matches the hole* — i.e., its IoU with the polygon's bounding rect
clears a threshold (default 0.5). The hole's own false-positive
detection is roughly the size and position of the hole → IoU ≈ 0.95
→ drop. A fish swimming through the hole has a much smaller bbox →
IoU well under 0.5 → kept. Single threshold, clean separation.

Config (data/camera_negative_regions.json) keyed by source_name:
  sources.{name}.regions[].polygon  list of [x, y]
  sources.{name}.regions[].min_iou  optional, default 0.5
  sources.{name}.frame_w/frame_h    annotation resolution (auto-rescaled)

Filtering happens post-detector / pre-classifier in inference_proc.py
so suppressed detections never reach the classifier, the database,
the live overlay, or the reviewer queue.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

# Default IoU threshold. Calibration check:
#   hole detection (bbox ≈ polygon)   → ~0.95   → drop
#   fish inside hole (small bbox)     → ~0.05   → keep
#   fish overlapping hole edge        → ~0.05   → keep
#   big detection containing hole     → ~0.25   → keep
# 0.5 cleanly separates the first case from the rest.
_DEFAULT_MIN_IOU = 0.5


@dataclass
class _Region:
    """One polygon, precomputed for fast IoU. Polygon kept around for
    rescaling under a resolution change."""

    polygon_xy: np.ndarray  # (N, 2) float32 — raw vertices
    rect: tuple[int, int, int, int]  # bounding rect (x1, y1, x2, y2)
    rect_area: int  # rect's area, used as the IoU denominator
    min_iou: float  # per-region drop threshold


@dataclass
class _RegionGroup:
    regions: list[_Region]
    src_w: int
    src_h: int


def _bbox_iou(a: tuple, b: tuple) -> float:
    """IoU between two (x1, y1, x2, y2) rects."""
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    iw = max(0, min(ax2, bx2) - max(ax1, bx1))
    ih = max(0, min(ay2, by2) - max(ay1, by1))
    inter = iw * ih
    if inter == 0:
        return 0.0
    a_area = max(0, ax2 - ax1) * max(0, ay2 - ay1)
    b_area = max(0, bx2 - bx1) * max(0, by2 - by1)
    union = a_area + b_area - inter
    return inter / union if union > 0 else 0.0


def _polygon_bounding_rect(pts: np.ndarray) -> tuple[int, int, int, int]:
    """Min/max of polygon vertices → axis-aligned bounding rect.
    Works for any polygon shape; for roughly-convex holes (which is
    what the SEAHIVE cut-outs are) the bounding rect is a good
    stand-in for the polygon's actual area."""
    xs = pts[:, 0]
    ys = pts[:, 1]
    return (int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max()))


class NegativeRegionFilter:
    """Loads the regions config once at construction; cheap to call per
    frame after that. Treat instances as immutable — a config edit
    requires a worker restart (or a future hot-reload, not implemented)."""

    def __init__(self, path: str | Path | None) -> None:
        self._by_source: dict[str, _RegionGroup] = {}
        self._n_regions = 0
        if not path:
            log.info("negative-regions filter: no config path, disabled")
            return
        p = Path(path)
        try:
            data = json.loads(p.read_text())
        except FileNotFoundError:
            log.info("negative-regions filter: config %s not found, disabled", p)
            return
        except Exception as e:
            log.warning(
                "negative-regions filter: failed to parse %s (%s); disabled",
                p,
                e,
            )
            return
        sources = data.get("sources") or {}
        for source_name, cfg in sources.items():
            src_w = int(cfg.get("frame_w") or 0)
            src_h = int(cfg.get("frame_h") or 0)
            regions: list[_Region] = []
            for r in cfg.get("regions") or []:
                pts_raw = r.get("polygon") or []
                if len(pts_raw) < 3:
                    continue
                pts = np.asarray(pts_raw, dtype=np.float32)
                rect = _polygon_bounding_rect(pts)
                rect_area = max(0, rect[2] - rect[0]) * max(0, rect[3] - rect[1])
                if rect_area == 0:
                    continue
                regions.append(
                    _Region(
                        polygon_xy=pts,
                        rect=rect,
                        rect_area=rect_area,
                        min_iou=float(r.get("min_iou", _DEFAULT_MIN_IOU)),
                    )
                )
            if regions:
                self._by_source[source_name] = _RegionGroup(
                    regions=regions,
                    src_w=src_w,
                    src_h=src_h,
                )
                self._n_regions += len(regions)
        if self._by_source:
            log.info(
                "negative-regions filter loaded: %s (path=%s)",
                ", ".join(
                    f"{s}={len(g.regions)}@{g.src_w}x{g.src_h}" for s, g in self._by_source.items()
                ),
                p,
            )
        else:
            log.info(
                "negative-regions filter: config %s has no polygons; pass-through",
                p,
            )

    @property
    def has_regions(self) -> bool:
        return self._n_regions > 0

    def filter_detections(
        self,
        source_name: str,
        detections: list,
        frame_w: int,
        frame_h: int,
    ) -> tuple[list, int]:
        """Drop a detection only when its bbox closely matches one of
        the configured no-fish-zone polygons for `source_name`.
        Matching = IoU(bbox, polygon's bounding rect) >= the region's
        min_iou threshold. Fish that pass through a hole have small
        bboxes vs the hole → low IoU → kept. Returns (kept, n_dropped).
        Pass-through if the source has no configured regions."""
        group = self._by_source.get(source_name)
        if group is None or not group.regions:
            return detections, 0

        # Build per-call rect list, rescaling once if the annotation
        # resolution differs from the current frame resolution. Common
        # path is matching resolution; the rescale branch is cheap.
        if group.src_w and group.src_h and (group.src_w != frame_w or group.src_h != frame_h):
            sx = frame_w / group.src_w
            sy = frame_h / group.src_h
            rects = []
            min_ious = []
            for region in group.regions:
                scaled = region.polygon_xy.copy()
                scaled[:, 0] *= sx
                scaled[:, 1] *= sy
                rect = _polygon_bounding_rect(scaled)
                rects.append(rect)
                min_ious.append(region.min_iou)
        else:
            rects = [r.rect for r in group.regions]
            min_ious = [r.min_iou for r in group.regions]

        kept: list = []
        dropped = 0
        for d in detections:
            bbox = tuple(d.bbox)  # (x1, y1, x2, y2)
            matched = False
            for rect, min_iou in zip(rects, min_ious, strict=False):
                iou = _bbox_iou(bbox, rect)
                if iou >= min_iou:
                    matched = True
                    if log.isEnabledFor(logging.DEBUG):
                        log.debug(
                            "negative-regions: drop bbox=%s vs rect=%s iou=%.2f (>= %.2f)",
                            bbox,
                            rect,
                            iou,
                            min_iou,
                        )
                    break
            if matched:
                dropped += 1
            else:
                kept.append(d)
        return kept, dropped

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

Config (format: data/camera_negative_regions.json) at one storage URL, the
same for the dashboard and the worker: NEGATIVE_REGIONS_URL (s3:// on AWS),
else the file at NEGATIVE_REGIONS_PATH. The dashboard's admin page writes it;
the worker reads it at start and every RELOAD_INTERVAL_S, so a save applies
without a restart. Keyed by source_name:
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
import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from common.storage import as_url, open_read

log = logging.getLogger(__name__)

# Default IoU threshold. Calibration check:
#   hole detection (bbox ≈ polygon)   → ~0.95   → drop
#   fish inside hole (small bbox)     → ~0.05   → keep
#   fish overlapping hole edge        → ~0.05   → keep
#   big detection containing hole     → ~0.25   → keep
# 0.5 cleanly separates the first case from the rest.
_DEFAULT_MIN_IOU = 0.5

# How often the worker re-reads the regions, in seconds: an admin's save applies within this long.
RELOAD_INTERVAL_S = 60.0

_UNREAD = object()  # no read yet


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


def _parse(data: dict) -> dict[str, _RegionGroup]:
    """The config's sources → their usable regions (a polygon needs three vertices and some area)."""
    by_source: dict[str, _RegionGroup] = {}
    for source_name, cfg in (data.get("sources") or {}).items():
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
            by_source[source_name] = _RegionGroup(regions=regions, src_w=src_w, src_h=src_h)
    return by_source


class NegativeRegionFilter:
    """Reads the regions config from a storage URL (s3:// on AWS; a file:// URL or a plain path on a laptop) at
    construction and again on every `reload()`. `watch()` reloads every RELOAD_INTERVAL_S on a thread of its
    own, so a save on the dashboard's admin page applies without a worker restart. Cheap to call per frame: a
    reload swaps in a whole new set of regions, never edits the set a frame is using."""

    def __init__(self, source: str | Path | None) -> None:
        self.url = as_url(str(source or ""))
        self._by_source: dict[str, _RegionGroup] = {}
        self._n_regions = 0
        self._raw: bytes | None | object = _UNREAD  # the config bytes in force; None = no config
        self._last_report = ""
        if not self.url:
            log.info("negative-regions filter: no config path, disabled")
            return
        self.reload()

    def reload(self) -> bool:
        """Read the config again; True when a changed config was applied. One that cannot be read or parsed
        leaves the regions in force as they are (a passing S3 error must not switch the filter off); one that
        is gone switches the filter off, as it is at startup without one."""
        if not self.url:
            return False
        try:
            raw: bytes | None = open_read(self.url)
        except FileNotFoundError:
            raw = None
        except Exception as e:
            self._report(
                logging.WARNING,
                f"negative-regions filter: cannot read {self.url} ({e}); regions unchanged",
            )
            return False
        if raw == self._raw:
            return False
        if raw is None:
            by_source: dict[str, _RegionGroup] = {}
        else:
            try:
                by_source = _parse(json.loads(raw))
            except Exception as e:
                self._report(
                    logging.WARNING,
                    f"negative-regions filter: failed to parse {self.url} ({e}); regions unchanged",
                )
                return False
        self._by_source = by_source
        self._n_regions = sum(len(g.regions) for g in by_source.values())
        self._raw = raw
        if raw is None:
            message = f"negative-regions filter: config {self.url} not found, disabled"
        elif by_source:
            loaded = ", ".join(
                f"{s}={len(g.regions)}@{g.src_w}x{g.src_h}" for s, g in by_source.items()
            )
            message = f"negative-regions filter loaded: {loaded} (url={self.url})"
        else:
            message = f"negative-regions filter: config {self.url} has no polygons; pass-through"
        # Every applied change is logged, even one that reads the same (a polygon moved, the counts did not).
        self._last_report = message
        log.info("%s", message)
        return True

    def watch(self, stop: threading.Event, interval_s: float | None = None) -> None:
        """Reload every interval_s seconds (default RELOAD_INTERVAL_S) until `stop` is set. Never raises."""
        while not stop.wait(RELOAD_INTERVAL_S if interval_s is None else interval_s):
            try:
                self.reload()
            except Exception:
                log.exception("negative-regions filter: reload failed")

    def _report(self, level: int, message: str) -> None:
        # A read or parse failure, polled every minute, logs once until something else happens.
        if message != self._last_report:
            self._last_report = message
            log.log(level, "%s", message)

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

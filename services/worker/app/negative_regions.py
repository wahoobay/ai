"""Per-camera "no-fish-zone" polygon filter.

The YOLO detector reliably emits false positives on stationary reef
geometry — most prominently the dark oval cut-outs in the SEAHIVE
artificial reef structure. They never move, the detector boxes them,
the classifier assigns a low-confidence species, and the result ends
up in the reviewer queue as garbage to be rejected.

This module loads a per-camera list of polygons from
`data/camera_negative_regions.json` (keyed by source_name) and offers
`filter_detections()` which drops any detection whose bbox center
falls inside a polygon for the current camera. Filtering happens
post-detector / pre-classifier in inference_proc.py — so suppressed
detections never reach the classifier, the database, or the live
overlay.

Cheap: cv2.pointPolygonTest, called once per detection per region.
Typical worst case is 8 detections × 5 regions = 40 tests/frame,
sub-millisecond.

Polygons are stored in the resolution they were annotated against
(carried as `frame_w` / `frame_h` per source). At load time they
get rescaled to the current frame resolution if the camera config
has changed since — no re-annotation required for a resolution flip.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

log = logging.getLogger(__name__)


@dataclass
class _RegionGroup:
    """Compiled per-source polygons + the resolution they were drawn at."""
    polygons: list[np.ndarray]  # each (N, 1, 2) int32, cv2.pointPolygonTest-ready
    src_w: int
    src_h: int


class NegativeRegionFilter:
    """Loads the regions config once at construction; cheap to call per
    frame after that. Treat instances as immutable — a config edit
    requires a worker restart (or a future hot-reload, not implemented)."""

    def __init__(self, path: str | Path | None) -> None:
        self._by_source: dict[str, _RegionGroup] = {}
        self._n_polys = 0
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
                p, e,
            )
            return
        sources = data.get("sources") or {}
        for source_name, cfg in sources.items():
            polys: list[np.ndarray] = []
            for r in cfg.get("regions") or []:
                pts = r.get("polygon") or []
                if len(pts) < 3:
                    continue
                arr = np.asarray(pts, dtype=np.int32).reshape(-1, 1, 2)
                polys.append(arr)
            if polys:
                self._by_source[source_name] = _RegionGroup(
                    polygons=polys,
                    src_w=int(cfg.get("frame_w") or 0),
                    src_h=int(cfg.get("frame_h") or 0),
                )
                self._n_polys += len(polys)
        if self._by_source:
            log.info(
                "negative-regions filter loaded: %s (path=%s)",
                ", ".join(
                    f"{s}={len(g.polygons)}@{g.src_w}x{g.src_h}"
                    for s, g in self._by_source.items()
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
        return self._n_polys > 0

    def filter_detections(
        self,
        source_name: str,
        detections: list,
        frame_w: int,
        frame_h: int,
    ) -> tuple[list, int]:
        """Drop detections whose bbox center is inside any polygon for
        `source_name`. Returns (kept, n_dropped). Pass-through if the
        source has no configured regions."""
        group = self._by_source.get(source_name)
        if group is None or not group.polygons:
            return detections, 0

        # Rescale polygons once per call if the annotation resolution
        # differs from the current frame resolution. Common path is no
        # rescale (matching res); the branch is cheap either way.
        polys = group.polygons
        if (
            group.src_w and group.src_h
            and (group.src_w != frame_w or group.src_h != frame_h)
        ):
            sx = frame_w / group.src_w
            sy = frame_h / group.src_h
            polys = []
            for poly in group.polygons:
                scaled = poly.astype(np.float32).copy()
                scaled[:, 0, 0] *= sx
                scaled[:, 0, 1] *= sy
                polys.append(scaled.astype(np.int32))

        kept: list = []
        dropped = 0
        for d in detections:
            x1, y1, x2, y2 = d.bbox
            cx = float((x1 + x2) / 2)
            cy = float((y1 + y2) / 2)
            inside = False
            for poly in polys:
                if cv2.pointPolygonTest(poly, (cx, cy), False) >= 0:
                    inside = True
                    break
            if inside:
                dropped += 1
            else:
                kept.append(d)
        return kept, dropped

"""Frame-to-frame smoothing for the live overlay.

Raw per-frame detector output is accurate but visually noisy: bbox jitters a
few pixels, top-1 species can flip between similar classes. We want a calmer
display *without* the box lagging behind a fish that's actually swimming.

Previous version averaged the last N bboxes. That smooths static targets
nicely but drags behind any moving fish — when the average catches up, the
box appears to "jump". This version uses a **constant-velocity tracker**
for the box centre and a short window for box size:

  - Each track holds centre (cx, cy), velocity (vx, vy), and size (w, h).
  - On a new measurement the velocity is EMA-updated, and the displayed
    centre is a blend of (raw observation) and (predicted = last + velocity).
    Tunable by ``center_alpha`` — higher = trust the raw detection more.
  - Box size is averaged over the last N frames (fish size changes slowly;
    smoothing here kills "breathing").
  - On a missed frame we extrapolate along the velocity vector (for a few
    frames; velocity decays so the box doesn't sail off after a real exit).
  - Association is greedy IoU as before, so two nearby fish can't merge.

Public API (class ``DetectionSmoother``, ``update(frame_id, detections)``)
matches the previous module — no pipeline changes required.

Smoothing is display-only. Raw detections continue to be written to
``detection_events`` / ``frame_stats`` / the COCO saver.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Deque, List, Optional, Tuple

import numpy as np

from .fishial import FishDetection, Prediction


@dataclass
class SmootherUpdate:
    """Output of one tracker tick.

    ``display`` is the smoothed list to draw on the live overlay.
    ``raw_track_ids`` is parallel to the *input* `detections` list; it tells
    the rest of the pipeline which persistent track each raw detection
    belongs to so we can deduplicate counts across frames.
    """
    display: List[FishDetection]
    raw_track_ids: List[Optional[int]]


def _iou(a: Tuple[int, int, int, int], b: Tuple[int, int, int, int]) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    area_a = max(1, (ax2 - ax1) * (ay2 - ay1))
    area_b = max(1, (bx2 - bx1) * (by2 - by1))
    return inter / (area_a + area_b - inter)


@dataclass
class _Track:
    track_id: int
    # kinematic state
    cx: float = 0.0
    cy: float = 0.0
    vx: float = 0.0
    vy: float = 0.0
    w:  float = 0.0
    h:  float = 0.0
    size_history: Deque[Tuple[float, float]] = field(default_factory=deque)
    initialized:  bool = False

    # classifier-output memory
    conf_history: Deque[float] = field(default_factory=deque)
    topk_history: Deque[List[Prediction]] = field(default_factory=deque)

    # bookkeeping
    last_seen_frame: int = -1
    age_since_hit:   int = 0
    total_hits:      int = 0

    def update_with(
        self,
        frame_id: int,
        bbox: Tuple[int, int, int, int],
        det_conf: float,
        topk: List[Prediction],
        *,
        window: int,
        center_alpha: float,
        velocity_alpha: float,
    ) -> None:
        x1, y1, x2, y2 = bbox
        raw_cx = (x1 + x2) / 2.0
        raw_cy = (y1 + y2) / 2.0
        raw_w  = float(max(1, x2 - x1))
        raw_h  = float(max(1, y2 - y1))

        if not self.initialized:
            self.cx, self.cy = raw_cx, raw_cy
            self.w,  self.h  = raw_w,  raw_h
            self.vx = self.vy = 0.0
            self.initialized = True
        else:
            dt = max(1, frame_id - self.last_seen_frame)
            # observed per-frame velocity
            meas_vx = (raw_cx - self.cx) / dt
            meas_vy = (raw_cy - self.cy) / dt
            # EMA-update velocity so estimate is stable
            self.vx = velocity_alpha * meas_vx + (1 - velocity_alpha) * self.vx
            self.vy = velocity_alpha * meas_vy + (1 - velocity_alpha) * self.vy

            # Predicted centre if the fish kept its previous velocity
            pred_cx = self.cx + self.vx * dt
            pred_cy = self.cy + self.vy * dt
            # Blend prediction with raw observation. high center_alpha → lock
            # onto the measurement (responsive, slightly jittery).
            self.cx = center_alpha * raw_cx + (1 - center_alpha) * pred_cx
            self.cy = center_alpha * raw_cy + (1 - center_alpha) * pred_cy

        # size smoothing: window mean (fish grow/shrink slowly)
        self.size_history.append((raw_w, raw_h))
        _trim(self.size_history, window)
        arr = np.asarray(self.size_history, dtype=np.float32)
        self.w = float(arr[:, 0].mean())
        self.h = float(arr[:, 1].mean())

        # class-output memory
        self.conf_history.append(det_conf)
        _trim(self.conf_history, window)
        self.topk_history.append(topk)
        _trim(self.topk_history, window)

        self.last_seen_frame = frame_id
        self.age_since_hit   = 0
        self.total_hits     += 1

    def coast(self, velocity_decay: float) -> None:
        """Advance one frame along the velocity vector without a measurement.

        Applies a decay so a track with no new detections doesn't sail off the
        frame. Called once per smoother tick for each track that wasn't
        matched this frame.
        """
        if not self.initialized:
            return
        self.cx += self.vx
        self.cy += self.vy
        self.vx *= velocity_decay
        self.vy *= velocity_decay
        self.age_since_hit += 1

    def smoothed_bbox(self) -> Tuple[int, int, int, int]:
        x1 = int(round(self.cx - self.w / 2))
        y1 = int(round(self.cy - self.h / 2))
        x2 = int(round(self.cx + self.w / 2))
        y2 = int(round(self.cy + self.h / 2))
        return x1, y1, x2, y2

    def smoothed_conf(self) -> float:
        return float(np.mean(list(self.conf_history))) if self.conf_history else 0.0

    def smoothed_topk(self, k: int) -> List[Prediction]:
        """Sum per-species accuracy across the window, take top-k. Normalised
        by window length so absent species don't get a free ride."""
        agg:  dict[str, float] = defaultdict(float)
        meta: dict[str, Prediction] = {}
        for topk in self.topk_history:
            seen = set()
            for p in topk:
                key = p.species_id or p.name
                if key in seen:
                    continue
                seen.add(key)
                agg[key]  += p.accuracy
                meta[key] = p
        window = max(1, len(self.topk_history))
        ranked = sorted(agg.items(), key=lambda kv: kv[1] / window, reverse=True)
        out: List[Prediction] = []
        for key, total in ranked[:k]:
            proto = meta[key]
            out.append(Prediction(
                name=proto.name,
                species_id=proto.species_id,
                accuracy=round(total / window, 4),
            ))
        return out


def _trim(d: Deque, max_len: int) -> None:
    while len(d) > max_len:
        d.popleft()


class DetectionSmoother:
    def __init__(
        self,
        window: int = 5,
        iou_threshold: float = 0.3,
        max_age: int = 1,
        min_hits: int = 1,
        topk: int = 3,
        center_alpha: float = 0.6,
        velocity_alpha: float = 0.5,
        velocity_decay: float = 0.8,
    ) -> None:
        self.window = max(1, window)
        self.iou_threshold  = iou_threshold
        self.max_age        = max_age
        self.min_hits       = max(1, min_hits)
        self.topk           = topk
        self.center_alpha   = center_alpha
        self.velocity_alpha = velocity_alpha
        self.velocity_decay = velocity_decay
        self._tracks: dict[int, _Track] = {}
        self._next_id = 1

    def reset(self) -> None:
        self._tracks.clear()
        self._next_id = 1

    def update(self, frame_id: int, detections: List[FishDetection]) -> SmootherUpdate:
        existing_ids = list(self._tracks.keys())
        raw_track_ids: List[Optional[int]] = [None] * len(detections)

        # Greedy IoU matching on *predicted* current bboxes, not last-frame
        # bboxes — so a moving fish's track stays associated after a one-frame
        # miss.
        pairs: list[tuple[int, int]] = []
        if existing_ids and detections:
            M = np.full((len(existing_ids), len(detections)), -1.0, dtype=np.float32)
            for i, tid in enumerate(existing_ids):
                t = self._tracks[tid]
                if not t.initialized:
                    continue
                predicted = t.smoothed_bbox()
                for j, d in enumerate(detections):
                    M[i, j] = _iou(predicted, d.bbox)
            while True:
                idx = int(np.argmax(M))
                i, j = np.unravel_index(idx, M.shape)
                if M[i, j] < self.iou_threshold:
                    break
                pairs.append((existing_ids[i], j))
                M[i, :] = -1
                M[:, j] = -1

        matched_track_ids = {tid for tid, _j in pairs}
        matched_det_idxs  = {j   for _tid, j in pairs}

        # Matched: kinematic update
        for tid, j in pairs:
            d = detections[j]
            self._tracks[tid].update_with(
                frame_id=frame_id, bbox=tuple(d.bbox),
                det_conf=d.det_conf, topk=list(d.topk),
                window=self.window,
                center_alpha=self.center_alpha,
                velocity_alpha=self.velocity_alpha,
            )
            raw_track_ids[j] = tid

        # Unmatched tracks: coast along velocity for up to max_age frames.
        to_drop: list[int] = []
        for tid in existing_ids:
            if tid in matched_track_ids:
                continue
            t = self._tracks[tid]
            t.coast(self.velocity_decay)
            if t.age_since_hit > self.max_age:
                to_drop.append(tid)
        for tid in to_drop:
            del self._tracks[tid]

        # Unmatched detections: open new tracks
        for j, d in enumerate(detections):
            if j in matched_det_idxs:
                continue
            new_id = self._next_id
            self._next_id += 1
            t = _Track(track_id=new_id)
            t.update_with(
                frame_id=frame_id, bbox=tuple(d.bbox),
                det_conf=d.det_conf, topk=list(d.topk),
                window=self.window,
                center_alpha=self.center_alpha,
                velocity_alpha=self.velocity_alpha,
            )
            self._tracks[new_id] = t
            raw_track_ids[j] = new_id

        # Emit current state for every track with enough hits
        out: List[FishDetection] = []
        for t in self._tracks.values():
            if t.total_hits < self.min_hits or not t.initialized:
                continue
            out.append(FishDetection(
                bbox=t.smoothed_bbox(),
                det_conf=t.smoothed_conf(),
                topk=t.smoothed_topk(self.topk),
            ))
        return SmootherUpdate(display=out, raw_track_ids=raw_track_ids)

    def stats(self) -> dict:
        return {
            "active_tracks": len(self._tracks),
            "next_id": self._next_id,
            "window": self.window,
            "iou_threshold": self.iou_threshold,
            "max_age": self.max_age,
            "min_hits": self.min_hits,
            "center_alpha": self.center_alpha,
            "velocity_alpha": self.velocity_alpha,
        }

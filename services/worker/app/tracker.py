"""Frame-to-frame smoothing for the live overlay.

Raw per-frame detector output is *accurate* but visually noisy: the bbox
jitters a few pixels each frame, and top-1 species can flip between two
very-similar classes. For the dashboard we want something calmer.

Algorithm (deliberately simple):

  1. Assign each incoming detection to an existing track by greedy IoU match
     (threshold ``iou_threshold``). Unmatched detections open new tracks.
  2. A track holds a sliding window of its last N detections. The smoothed
     bbox is the mean of the window; the smoothed top-K is obtained by
     summing per-species accuracy scores across the window and taking the
     highest-ranked names.
  3. A track not matched in a frame "ages"; it's emitted for up to
     ``max_age`` frames after its last real detection (so a one-frame detector
     miss doesn't cause a flicker), then dropped.
  4. Tracks must accumulate ``min_hits`` observations before they appear in
     the output. Raises the bar for one-off false positives without adding
     perceptible lag.

Output detections reuse the same FishDetection dataclass used elsewhere, so
the rest of the pipeline (overlay, publisher, persistence-of-raw) is
untouched.

Note: this smoother is for display only. Raw detections continue to be
written to ``detection_events`` and ``frame_stats``. Never smooth data that
flows into training or evaluation.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Deque, Iterable, List, Optional, Tuple

import numpy as np

from .fishial import FishDetection, Prediction


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
    bbox_history:     Deque[Tuple[int, int, int, int]] = field(default_factory=deque)
    conf_history:     Deque[float]                     = field(default_factory=deque)
    # each element is a list[Prediction] from one frame
    topk_history:     Deque[List[Prediction]]          = field(default_factory=deque)
    last_seen_frame:  int = -1
    age_since_hit:    int = 0
    total_hits:       int = 0

    def push(
        self,
        frame_id: int,
        bbox: Tuple[int, int, int, int],
        det_conf: float,
        topk: List[Prediction],
        window: int,
    ) -> None:
        self.bbox_history.append(bbox);   _trim(self.bbox_history, window)
        self.conf_history.append(det_conf); _trim(self.conf_history, window)
        self.topk_history.append(topk);   _trim(self.topk_history, window)
        self.last_seen_frame = frame_id
        self.age_since_hit = 0
        self.total_hits += 1

    def smoothed_bbox(self) -> Tuple[int, int, int, int]:
        arr = np.asarray(list(self.bbox_history), dtype=np.float32)
        x1, y1, x2, y2 = arr.mean(axis=0)
        return int(round(x1)), int(round(y1)), int(round(x2)), int(round(y2))

    def smoothed_conf(self) -> float:
        return float(np.mean(list(self.conf_history))) if self.conf_history else 0.0

    def smoothed_topk(self, k: int) -> List[Prediction]:
        """Per-species accuracy summed across the window → rank → take top k.

        Normalises by window length so absent species don't get a free pass.
        """
        agg_sum:   dict[str, float]  = defaultdict(float)
        agg_count: dict[str, int]    = defaultdict(int)
        meta:      dict[str, Prediction] = {}
        for topk in self.topk_history:
            seen_this_frame = set()
            for p in topk:
                key = p.species_id or p.name
                if key in seen_this_frame:
                    continue
                seen_this_frame.add(key)
                agg_sum[key]   += p.accuracy
                agg_count[key] += 1
                meta[key] = p
        # smoothed accuracy = (sum across window) / window_size
        window = max(1, len(self.topk_history))
        ranked = sorted(
            agg_sum.items(),
            key=lambda kv: kv[1] / window,
            reverse=True,
        )
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
        max_age: int = 3,
        min_hits: int = 1,
        topk: int = 3,
    ) -> None:
        self.window = max(1, window)
        self.iou_threshold = iou_threshold
        self.max_age = max_age
        self.min_hits = max(1, min_hits)
        self.topk = topk
        self._tracks: dict[int, _Track] = {}
        self._next_id = 1

    def reset(self) -> None:
        self._tracks.clear()
        self._next_id = 1

    def update(self, frame_id: int, detections: List[FishDetection]) -> List[FishDetection]:
        existing_ids = list(self._tracks.keys())

        # Build IoU matrix between existing tracks' last bbox and new detections
        pairs: list[tuple[int, int]] = []
        if existing_ids and detections:
            M = np.full((len(existing_ids), len(detections)), -1.0, dtype=np.float32)
            for i, tid in enumerate(existing_ids):
                t = self._tracks[tid]
                if not t.bbox_history:
                    continue
                last_bbox = t.bbox_history[-1]
                for j, d in enumerate(detections):
                    M[i, j] = _iou(last_bbox, d.bbox)
            # greedy matching
            while True:
                idx = int(np.argmax(M))
                i, j = np.unravel_index(idx, M.shape)
                if M[i, j] < self.iou_threshold:
                    break
                pairs.append((existing_ids[i], j))
                M[i, :] = -1
                M[:, j] = -1

        matched_track_ids = {tid for tid, _j in pairs}
        matched_det_idxs  = {j   for _t,  j in pairs}

        # Update matched tracks
        for tid, j in pairs:
            d = detections[j]
            self._tracks[tid].push(
                frame_id=frame_id, bbox=tuple(d.bbox),
                det_conf=d.det_conf, topk=list(d.topk),
                window=self.window,
            )

        # Age unmatched tracks; drop the ones past max_age
        to_drop: list[int] = []
        for tid in existing_ids:
            if tid in matched_track_ids:
                continue
            t = self._tracks[tid]
            t.age_since_hit += 1
            if t.age_since_hit > self.max_age:
                to_drop.append(tid)
        for tid in to_drop:
            del self._tracks[tid]

        # Open new tracks for unmatched detections
        for j, d in enumerate(detections):
            if j in matched_det_idxs:
                continue
            new_id = self._next_id
            self._next_id += 1
            t = _Track(track_id=new_id)
            t.push(frame_id=frame_id, bbox=tuple(d.bbox),
                   det_conf=d.det_conf, topk=list(d.topk),
                   window=self.window)
            self._tracks[new_id] = t

        # Emit smoothed detections
        out: List[FishDetection] = []
        for tid, t in self._tracks.items():
            if t.total_hits < self.min_hits:
                continue
            out.append(FishDetection(
                bbox=t.smoothed_bbox(),
                det_conf=t.smoothed_conf(),
                topk=t.smoothed_topk(self.topk),
            ))
        return out

    def stats(self) -> dict:
        return {
            "active_tracks": len(self._tracks),
            "next_id": self._next_id,
            "window": self.window,
            "iou_threshold": self.iou_threshold,
            "max_age": self.max_age,
            "min_hits": self.min_hits,
        }

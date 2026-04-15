"""Inference loop: pull frames, detect+classify, persist, publish.

Runs in a background thread so the worker's FastAPI app can serve the MJPEG
stream and /events endpoints concurrently.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

import cv2
import numpy as np

from .fishial import FishDetection, FishialPipeline
from .overlay import annotate
from .persistence import EventLog, ImageSaver, PgWriter
from .provenance import Provenance
from .sources import VideoSource
from .tracker import DetectionSmoother

log = logging.getLogger(__name__)


@dataclass
class LiveFrame:
    """Latest annotated frame + its detections, served to dashboard clients."""
    jpeg: bytes
    frame_id: int
    ts: datetime
    source_name: str
    detections_summary: list
    infer_ms: float = 0.0

    @classmethod
    def empty(cls) -> "LiveFrame":
        return cls(
            jpeg=b"", frame_id=0, ts=datetime.now(timezone.utc),
            source_name="", detections_summary=[],
        )


class LiveBuffer:
    """Thread-safe one-slot buffer with a condition variable for the MJPEG stream."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._current = LiveFrame.empty()

    def publish(self, f: LiveFrame) -> None:
        with self._cond:
            self._current = f
            self._cond.notify_all()

    def snapshot(self) -> LiveFrame:
        with self._cond:
            return self._current

    def wait_for_next(self, last_frame_id: int, timeout: float = 5.0) -> Optional[LiveFrame]:
        with self._cond:
            if self._current.frame_id == last_frame_id:
                self._cond.wait(timeout=timeout)
            return self._current if self._current.frame_id != last_frame_id else None


@dataclass
class PipelineStats:
    frames_seen: int = 0
    frames_with_fish: int = 0
    detections_total: int = 0
    last_infer_ms: float = 0.0
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_frame_at: Optional[datetime] = None
    current_source: str = ""


class PipelineRunner:
    def __init__(
        self,
        cfg,
        source: VideoSource,
        fishial: FishialPipeline,
        live: LiveBuffer,
        pg: Optional[PgWriter],
        event_log: EventLog,
        saver: ImageSaver,
        provenance: Optional[Provenance] = None,
    ) -> None:
        self.cfg = cfg
        self.source = source
        self.fishial = fishial
        self.live = live
        self.pg = pg
        self.event_log = event_log
        self.saver = saver
        self.provenance = provenance
        self.stats = PipelineStats()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._frame_stats_buf: list[tuple] = []
        self._frame_stats_flush_threshold = 30
        self.smoother: Optional[DetectionSmoother] = None
        if cfg.tracker_enabled:
            self.smoother = DetectionSmoother(
                window=cfg.tracker_window,
                iou_threshold=cfg.tracker_iou_threshold,
                max_age=cfg.tracker_max_age,
                min_hits=cfg.tracker_min_hits,
                topk=cfg.classifier_topk,
            )
            log.info(
                "detection smoother enabled (window=%d iou=%.2f max_age=%d min_hits=%d)",
                cfg.tracker_window, cfg.tracker_iou_threshold,
                cfg.tracker_max_age, cfg.tracker_min_hits,
            )

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="pipeline", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self.source.close()
        if self._thread:
            self._thread.join(timeout=timeout)
        self._flush_frame_stats()
        self.event_log.close()
        if self.pg:
            self.pg.close()

    def _run(self) -> None:
        cfg = self.cfg
        min_period = 1.0 / max(1, cfg.live_stream_max_fps)
        last_publish = 0.0

        try:
            for frame_id, frame, source_name in self.source.frames():
                if self._stop.is_set():
                    break
                now_mono = time.monotonic()
                ts = datetime.now(timezone.utc)
                self.stats.frames_seen += 1
                self.stats.last_frame_at = ts
                self.stats.current_source = source_name

                t0 = time.time()
                detections = self.fishial.process_frame(frame)
                infer_ms = (time.time() - t0) * 1000
                self.stats.last_infer_ms = infer_ms
                if detections:
                    self.stats.frames_with_fish += 1
                    self.stats.detections_total += len(detections)

                # persist raw events (tracker smoothing is display-only; we
                # never smooth data that flows into training or evaluation)
                self._persist_events(ts, frame_id, source_name, detections)

                # frame-stats sampler for drift monitor (sub-sampled)
                if frame_id % max(1, cfg.frame_stats_every_n_frames) == 0:
                    self._sample_frame_stats(ts, frame_id, source_name, frame, detections)

                # Smooth detections for the dashboard overlay
                display = self.smoother.update(frame_id, detections) if self.smoother else detections

                # tunable image save (annotated render uses smoothed detections;
                # the saved COCO annotations use raw)
                annotated = annotate(frame, display) if display else frame
                saved = self.saver.maybe_save(
                    frame_bgr=frame,
                    annotated_bgr=annotated,
                    detections=detections,
                    frame_id=frame_id,
                    ts=ts,
                    source_name=source_name,
                    now_mono=now_mono,
                )
                if saved and self.pg:
                    reason, image_path, coco_path = saved
                    try:
                        self.pg.record_saved_frame(
                            ts, frame_id, source_name, reason, image_path, coco_path,
                            num_fish=len(detections),
                        )
                    except Exception:
                        log.exception("pg: saved_frame insert failed")

                # publish to live buffer, rate-limited (smoothed payload for display)
                if (now_mono - last_publish) >= min_period:
                    self._publish_live(frame_id, ts, source_name, annotated, display, infer_ms)
                    last_publish = now_mono
        except Exception:
            log.exception("pipeline crashed")

    def _persist_events(
        self,
        ts: datetime,
        frame_id: int,
        source_name: str,
        detections: List[FishDetection],
    ) -> None:
        if not detections:
            return
        prov_dict = self.provenance.as_dict() if self.provenance else {}
        # jsonl line per detection
        for d in detections:
            rec = {
                "ts": ts.isoformat(),
                "frame_id": frame_id,
                "source": source_name,
                "det_conf": d.det_conf,
                "bbox": list(d.bbox),
                "topk": [
                    {"name": p.name, "species_id": p.species_id, "accuracy": p.accuracy}
                    for p in d.topk
                ],
                **prov_dict,
            }
            self.event_log.write(rec)

        if self.pg:
            try:
                self.pg.record_detections(ts, frame_id, source_name, detections)
            except Exception:
                log.exception("pg: detection insert failed (will retry on next frame)")

    def _sample_frame_stats(
        self,
        ts: datetime,
        frame_id: int,
        source_name: str,
        frame_bgr: np.ndarray,
        detections: List[FishDetection],
    ) -> None:
        """Downsample and compute cheap per-frame stats for drift monitoring."""
        # Resize for speed; preserves brightness/colour statistics well enough.
        small = cv2.resize(frame_bgr, (160, 90), interpolation=cv2.INTER_AREA)
        b, g, r = small[..., 0], small[..., 1], small[..., 2]
        # BT.601 luma
        luma = 0.114 * b + 0.587 * g + 0.299 * r
        mean_luma = float(luma.mean())
        std_luma  = float(luma.std())
        mean_r, mean_g, mean_b = float(r.mean()), float(g.mean()), float(b.mean())
        num = len(detections)
        mean_conf = float(np.mean([d.det_conf for d in detections])) if num else 0.0
        self._frame_stats_buf.append((
            ts, source_name, frame_id,
            mean_luma, mean_r, mean_g, mean_b, std_luma,
            num, mean_conf,
        ))
        if self.pg and len(self._frame_stats_buf) >= self._frame_stats_flush_threshold:
            self._flush_frame_stats()

    def _flush_frame_stats(self) -> None:
        if not self.pg or not self._frame_stats_buf:
            return
        batch = self._frame_stats_buf
        self._frame_stats_buf = []
        try:
            self.pg.record_frame_stats(batch)
        except Exception:
            log.exception("pg: frame_stats insert failed; dropping %d rows", len(batch))

    def _publish_live(
        self,
        frame_id: int,
        ts: datetime,
        source_name: str,
        annotated: np.ndarray,
        detections: List[FishDetection],
        infer_ms: float,
    ) -> None:
        ok, buf = cv2.imencode(
            ".jpg", annotated,
            [int(cv2.IMWRITE_JPEG_QUALITY), self.cfg.jpeg_quality],
        )
        if not ok:
            return

        summary = [
            {
                "bbox": list(d.bbox),
                "det_conf": d.det_conf,
                "best_name": d.best.name if d.best else None,
                "best_species_id": d.best.species_id if d.best else None,
                "best_accuracy": d.best.accuracy if d.best else None,
                "topk": [
                    {"name": p.name, "species_id": p.species_id, "accuracy": p.accuracy}
                    for p in d.topk
                ],
            }
            for d in detections
        ]
        self.live.publish(LiveFrame(
            jpeg=buf.tobytes(),
            frame_id=frame_id,
            ts=ts,
            source_name=source_name,
            detections_summary=summary,
            infer_ms=infer_ms,
        ))

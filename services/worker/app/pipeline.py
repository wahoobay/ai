"""Inference loop: pull frames, detect+classify, persist, publish.

Two threads:
- `FrameTap`     reads source frames at native fps, publishes raw MJPEG
                 immediately so the live video stays smooth, and hands the
                 latest frame to the inference consumer through a one-slot
                 queue (intermediate frames are dropped if inference is slow).
- `PipelineRunner` pulls the latest raw frame, runs detector + classifier,
                 persists, draws the overlay, and publishes the annotated
                 MJPEG. Annotated stream therefore updates at inference rate;
                 raw stream stays at source rate.

That split is what lets viewers toggle bboxes off and get a silky native-fps
feed even when the model is doing 13-fish-per-frame work.
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
    """Latest annotated + raw frames + detections, served to dashboard clients.

    With the FrameTap split there are two LiveBuffers in play: one populated
    by FrameTap at native fps (only `jpeg_raw` is meaningful) and one
    populated by PipelineRunner at inference rate (only `jpeg` + the
    detection summary are meaningful). We reuse the same dataclass for both
    so the MJPEG/snapshot endpoints can pick out whichever field they need.
    """
    jpeg: bytes              # annotated (with bboxes + labels)
    jpeg_raw: bytes          # source-pixel frame, no overlay
    frame_id: int
    ts: datetime
    source_name: str
    detections_summary: list
    infer_ms: float = 0.0

    @classmethod
    def empty(cls) -> "LiveFrame":
        return cls(
            jpeg=b"", jpeg_raw=b"", frame_id=0,
            ts=datetime.now(timezone.utc),
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
class LatestRaw:
    """One frame in transit between the FrameTap and the inference loop."""
    frame_id: int
    frame_bgr: np.ndarray
    ts: datetime
    source_name: str
    in_fallback: bool


class LatestSlot:
    """Drop-old single-frame queue. The producer (FrameTap) overwrites whatever
    is currently here; the consumer (PipelineRunner) is woken up and pulls
    whichever frame happened to be most recent at consume time. Intermediate
    frames are dropped — the inference loop never queues up a backlog."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._latest: Optional[LatestRaw] = None
        self._consumed_id: int = -1

    def publish(self, item: LatestRaw) -> None:
        with self._cond:
            self._latest = item
            self._cond.notify_all()

    def wait_next(self, timeout: float = 5.0) -> Optional[LatestRaw]:
        with self._cond:
            self._cond.wait_for(
                lambda: self._latest is not None and self._latest.frame_id != self._consumed_id,
                timeout=timeout,
            )
            if self._latest is None or self._latest.frame_id == self._consumed_id:
                return None
            self._consumed_id = self._latest.frame_id
            return self._latest


@dataclass
class PipelineStats:
    frames_seen: int = 0           # frames grabbed from source (native fps)
    frames_inferred: int = 0       # frames the inference loop processed
    frames_with_fish: int = 0
    detections_total: int = 0
    last_infer_ms: float = 0.0
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_frame_at: Optional[datetime] = None
    current_source: str = ""
    # frames consumed during autoswitch fallback (no video data is collected
    # from these — they're a visual placeholder only)
    fallback_frames: int = 0


class FrameTap:
    """Reads frames from the source at native fps in its own thread.
    Publishes raw JPEG to `live_raw` immediately and hands the latest frame
    to `slot` for inference. Owns the source (calls .close on stop)."""

    def __init__(
        self,
        cfg,
        source: VideoSource,
        live_raw: LiveBuffer,
        slot: LatestSlot,
        stats: PipelineStats,
    ) -> None:
        self.cfg = cfg
        self.source = source
        self.live_raw = live_raw
        self.slot = slot
        self.stats = stats
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="frame-tap", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        try:
            self.source.close()
        except Exception:
            pass
        if self._thread:
            self._thread.join(timeout=timeout)

    def _run(self) -> None:
        cfg = self.cfg
        jpeg_q = [int(cv2.IMWRITE_JPEG_QUALITY), cfg.jpeg_quality]
        try:
            for frame_id, frame, source_name in self.source.frames():
                if self._stop.is_set():
                    break
                ts = datetime.now(timezone.utc)
                in_fallback = bool(getattr(self.source, "is_dark", False))

                self.stats.frames_seen += 1
                self.stats.last_frame_at = ts
                self.stats.current_source = source_name
                if in_fallback:
                    self.stats.fallback_frames += 1

                # publish raw JPEG for /stream_raw.mjpeg & /snapshot_raw.jpg
                ok, buf = cv2.imencode(".jpg", frame, jpeg_q)
                if ok:
                    self.live_raw.publish(LiveFrame(
                        jpeg=b"",
                        jpeg_raw=buf.tobytes(),
                        frame_id=frame_id,
                        ts=ts,
                        source_name=source_name,
                        detections_summary=[],
                        infer_ms=0.0,
                    ))

                # hand off to inference; intermediate frames are dropped
                self.slot.publish(LatestRaw(
                    frame_id=frame_id,
                    frame_bgr=frame,
                    ts=ts,
                    source_name=source_name,
                    in_fallback=in_fallback,
                ))
        except Exception:
            log.exception("frame-tap crashed")


class PipelineRunner:
    def __init__(
        self,
        cfg,
        fishial: FishialPipeline,
        live: LiveBuffer,           # annotated buffer (inference rate)
        slot: LatestSlot,           # latest raw frame from FrameTap
        stats: PipelineStats,       # shared with FrameTap
        pg: Optional[PgWriter],
        event_log: EventLog,
        saver: ImageSaver,
        provenance: Optional[Provenance] = None,
    ) -> None:
        self.cfg = cfg
        self.fishial = fishial
        self.live = live
        self.slot = slot
        self.stats = stats
        self.pg = pg
        self.event_log = event_log
        self.saver = saver
        self.provenance = provenance
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
                center_alpha=cfg.tracker_center_alpha,
                velocity_alpha=cfg.tracker_velocity_alpha,
                velocity_decay=cfg.tracker_velocity_decay,
            )
            log.info(
                "detection smoother enabled: velocity model "
                "(size_window=%d iou=%.2f max_age=%d center_alpha=%.2f velocity_alpha=%.2f)",
                cfg.tracker_window, cfg.tracker_iou_threshold,
                cfg.tracker_max_age, cfg.tracker_center_alpha, cfg.tracker_velocity_alpha,
            )

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="pipeline", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
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
        was_fallback = False

        try:
            while not self._stop.is_set():
                item = self.slot.wait_next(timeout=5.0)
                if item is None:
                    continue  # source idle; keep waiting

                frame_id = item.frame_id
                frame = item.frame_bgr
                ts = item.ts
                source_name = item.source_name
                in_fallback = item.in_fallback
                now_mono = time.monotonic()

                # On fallback transitions, reset the smoother so its tracks
                # don't bridge real-camera fish and playlist fish.
                if in_fallback != was_fallback:
                    if self.smoother is not None:
                        self.smoother.reset()
                    was_fallback = in_fallback
                    log.info(
                        "autoswitch transition: in_fallback=%s — %s video-data collection",
                        in_fallback,
                        "PAUSING" if in_fallback else "RESUMING",
                    )

                if in_fallback:
                    # No inference, no DB writes. The raw stream is already
                    # serving the fallback frame at native fps (handled by
                    # FrameTap). Mirror it onto the annotated buffer so a
                    # viewer with bboxes ON also sees the dark-camera image
                    # rather than a frozen prior frame.
                    if (now_mono - last_publish) >= min_period:
                        self._publish_live(
                            frame_id, ts, source_name,
                            annotated=frame, detections=[], infer_ms=0.0,
                        )
                        last_publish = now_mono
                    continue

                # ----- live-camera path: full inference + persist ---------
                t0 = time.time()
                detections = self.fishial.process_frame(frame)
                infer_ms = (time.time() - t0) * 1000
                self.stats.last_infer_ms = infer_ms
                self.stats.frames_inferred += 1
                if detections:
                    self.stats.frames_with_fish += 1
                    self.stats.detections_total += len(detections)

                # smoother first so each raw detection knows its track id
                if self.smoother:
                    upd = self.smoother.update(frame_id, detections)
                    display = upd.display
                    track_ids = upd.raw_track_ids
                else:
                    display = detections
                    track_ids = [None] * len(detections)

                # persist raw events with their track ids
                self._persist_events(ts, frame_id, source_name, detections, track_ids)

                # frame-stats sampler for drift monitor (sub-sampled)
                if frame_id % max(1, cfg.frame_stats_every_n_frames) == 0:
                    self._sample_frame_stats(ts, frame_id, source_name, frame, detections)

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

                # publish annotated (smoothed) frame for /stream.mjpeg
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
        track_ids: List[Optional[int]],
    ) -> None:
        if not detections:
            return
        prov_dict = self.provenance.as_dict() if self.provenance else {}
        for d, tid in zip(detections, track_ids):
            rec = {
                "ts": ts.isoformat(),
                "frame_id": frame_id,
                "source": source_name,
                "track_id": tid,
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
                self.pg.record_detections(ts, frame_id, source_name, detections, track_ids)
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
        small = cv2.resize(frame_bgr, (160, 90), interpolation=cv2.INTER_AREA)
        b, g, r = small[..., 0], small[..., 1], small[..., 2]
        luma = 0.114 * b + 0.587 * g + 0.299 * r
        mean_luma = float(luma.mean())
        std_luma = float(luma.std())
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
        jpeg_q = [int(cv2.IMWRITE_JPEG_QUALITY), self.cfg.jpeg_quality]
        ok_a, buf_a = cv2.imencode(".jpg", annotated, jpeg_q)
        if not ok_a:
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
            jpeg=buf_a.tobytes(),
            jpeg_raw=b"",
            frame_id=frame_id,
            ts=ts,
            source_name=source_name,
            detections_summary=summary,
            infer_ms=infer_ms,
        ))

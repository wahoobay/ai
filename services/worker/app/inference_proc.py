"""Inference subprocess: runs detector + classifier, persistence, and the
overlay encode in a separate OS process from the FastAPI / FrameTap parent.

Why a separate process instead of a thread? The Python GIL. PyTorch and
numpy ops release the GIL for their core math, but plenty of Python-side
work (tensor → numpy conversion, dict construction, list comprehensions
inside the smoother) runs under the GIL. With everything in-process, the
FrameTap thread that reads RTSP frames was spending up to 50 % of its
time blocked behind the inference thread and missing native-fps frames.
Out-of-process means FrameTap gets a whole CPU + GIL to itself.

Communication uses two `multiprocessing.Queue`s under the spawn context
(no fork, no shared CUDA state):

  worker process                 inference subprocess
  ──────────────                 ────────────────────
   FrameTap ──▶ in_queue ─────▶ run_inference_worker
                                       │
                                       ▼
                            detector + classifier
                            smoother + persist
                            annotate + encode JPEG
                                       │
              live_annotated ◀──── out_queue
              (drain thread)

Frames are pickled across the queue (~3 MB / frame at 720p × ~10 ms
serialise + ~10 ms deserialise = ~20 ms round-trip). Cheap enough at
15 fps; a future shared_memory hop is the natural next optimisation if
this becomes the bottleneck.
"""
from __future__ import annotations

import logging
import multiprocessing as mp
import os
import queue
import time
from dataclasses import dataclass
from datetime import datetime

import numpy as np

# NOTE: do NOT import .fishial / .persistence / cv2 at module top — those
# pull torch and CUDA into anyone who imports this file. We keep them
# inside the subprocess entry point so the parent worker never initialises
# CUDA.

log = logging.getLogger("wahoobay.worker.inference_proc")


@dataclass
class FrameIn:
    """Worker → inference. Carries one raw frame."""
    frame_id: int
    frame_bgr: np.ndarray
    ts: datetime
    source_name: str
    reset_smoother: bool = False


@dataclass
class FrameOut:
    """Inference → worker. Carries the annotated JPEG + summary + stats."""
    frame_id: int
    ts: datetime
    source_name: str
    annotated_jpeg: bytes
    detections_summary: list
    infer_ms: float
    n_detections: int
    had_fish: bool


# Sentinel sent by the parent to ask the subprocess to shut down cleanly.
SHUTDOWN = object()


def run_inference_worker(
    cfg,
    in_queue: mp.Queue,
    out_queue: mp.Queue,
    ready_event,
) -> None:
    """Subprocess entry point. Initialises models + persistence, drains
    frames from `in_queue`, emits results to `out_queue`. Returns when it
    receives `SHUTDOWN`."""
    # Delayed imports: keep CUDA + heavy deps out of the parent.
    import threading

    import cv2

    from .fishial import FishialPipeline
    from .overlay import annotate
    from .persistence import EventLog, ImageSaver, PgWriter
    from .provenance import compute as compute_provenance
    from .tracker import DetectionSmoother

    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    log.info("inference subprocess: starting (pid=%d, device=%s)", os.getpid(), cfg.device)
    provenance = compute_provenance(cfg)

    fishial = FishialPipeline(cfg)
    event_log = EventLog(cfg.events_log_dir)
    saver = ImageSaver(cfg)

    pg: PgWriter | None = None
    try:
        pg = PgWriter(cfg.database_url, provenance=provenance)
        log.info("inference subprocess: postgres connected")
    except Exception as e:
        log.warning("postgres unavailable in inference subprocess (%s); jsonl-only", e)

    smoother: DetectionSmoother | None = None
    if cfg.tracker_enabled:
        smoother = DetectionSmoother(
            window=cfg.tracker_window,
            iou_threshold=cfg.tracker_iou_threshold,
            max_age=cfg.tracker_max_age,
            min_hits=cfg.tracker_min_hits,
            topk=cfg.classifier_topk,
            center_alpha=cfg.tracker_center_alpha,
            velocity_alpha=cfg.tracker_velocity_alpha,
            velocity_decay=cfg.tracker_velocity_decay,
        )

    frame_stats_buf: list = []
    frame_stats_flush_threshold = 30
    jpeg_q = [int(cv2.IMWRITE_JPEG_QUALITY), cfg.jpeg_quality]

    # Background save-worker: takes SaveJobs off `save_queue` and does the
    # disk I/O (encode + write JPEGs, write COCO sidecar) plus the
    # `record_saved_frame` DB call. Bounded queue with drop-oldest if the
    # writer falls behind — losing a saved frame is cosmetic, but stalling
    # inference would matter.
    SAVE_QUEUE_MAXSIZE = 8
    save_queue: queue.Queue = queue.Queue(maxsize=SAVE_QUEUE_MAXSIZE)
    save_worker_stop = threading.Event()

    def save_worker() -> None:
        log.info("save worker: started")
        while True:
            try:
                job = save_queue.get(timeout=1.0)
            except queue.Empty:
                if save_worker_stop.is_set():
                    return
                continue
            if job is None:
                return
            try:
                saver.write(job)
                if pg:
                    pg.record_saved_frame(
                        job.ts, job.frame_id, job.source_name,
                        job.reason, str(job.img_path), str(job.coco_path),
                        num_fish=job.n_fish,
                    )
            except Exception:
                log.exception("save worker: write failed for frame_id=%d", job.frame_id)

    save_thread = threading.Thread(target=save_worker, name="save-worker", daemon=True)
    save_thread.start()

    def _flush_frame_stats() -> None:
        nonlocal frame_stats_buf
        if not pg or not frame_stats_buf:
            return
        batch = frame_stats_buf
        frame_stats_buf = []
        try:
            pg.record_frame_stats(batch)
        except Exception:
            log.exception("pg: frame_stats insert failed; dropping %d rows", len(batch))

    ready_event.set()
    log.info("inference subprocess: ready")

    try:
        while True:
            try:
                item = in_queue.get(timeout=5.0)
            except queue.Empty:
                continue
            if item is SHUTDOWN or item is None:
                log.info("inference subprocess: shutdown signal received")
                break
            assert isinstance(item, FrameIn), f"unexpected queue item: {type(item)}"

            if item.reset_smoother and smoother is not None:
                smoother.reset()
                log.info("smoother reset (post-fallback transition)")

            t0 = time.time()
            detections = fishial.process_frame(item.frame_bgr)
            infer_ms = (time.time() - t0) * 1000

            had_fish = bool(detections)
            n_detections = len(detections)

            if smoother:
                upd = smoother.update(item.frame_id, detections)
                display = upd.display
                track_ids = upd.raw_track_ids
            else:
                display = detections
                track_ids = [None] * len(detections)

            # Persist raw events tagged with their track ids
            if detections:
                prov_dict = provenance.as_dict() if provenance else {}
                for d, tid in zip(detections, track_ids, strict=False):
                    rec = {
                        "ts": item.ts.isoformat(),
                        "frame_id": item.frame_id,
                        "source": item.source_name,
                        "track_id": tid,
                        "det_conf": d.det_conf,
                        "bbox": list(d.bbox),
                        "topk": [
                            {"name": p.name, "species_id": p.species_id, "accuracy": p.accuracy}
                            for p in d.topk
                        ],
                        **prov_dict,
                    }
                    event_log.write(rec)
                if pg:
                    try:
                        pg.record_detections(item.ts, item.frame_id, item.source_name,
                                             detections, track_ids)
                    except Exception:
                        log.exception("pg: detection insert failed (will retry)")

            # Sub-sampled frame stats for drift monitoring
            if item.frame_id % max(1, cfg.frame_stats_every_n_frames) == 0:
                small = cv2.resize(item.frame_bgr, (160, 90), interpolation=cv2.INTER_AREA)
                b, g, r = small[..., 0], small[..., 1], small[..., 2]
                luma = 0.114 * b + 0.587 * g + 0.299 * r
                mean_conf = float(np.mean([d.det_conf for d in detections])) if detections else 0.0
                frame_stats_buf.append((
                    item.ts, item.source_name, item.frame_id,
                    float(luma.mean()), float(r.mean()), float(g.mean()), float(b.mean()),
                    float(luma.std()), n_detections, mean_conf,
                ))
                if pg and len(frame_stats_buf) >= frame_stats_flush_threshold:
                    _flush_frame_stats()

            # Image save (annotated render uses smoothed; COCO uses raw).
            # maybe_save does the gating + state updates synchronously and
            # returns a SaveJob; the disk-bound part is handled by the
            # save-worker thread so this loop never blocks on I/O.
            annotated = annotate(item.frame_bgr, display) if display else item.frame_bgr
            job = saver.maybe_save(
                frame_bgr=item.frame_bgr,
                annotated_bgr=annotated,
                detections=detections,
                frame_id=item.frame_id,
                ts=item.ts,
                source_name=item.source_name,
                now_mono=time.monotonic(),
            )
            if job is not None:
                try:
                    save_queue.put_nowait(job)
                except queue.Full:
                    # Drop-oldest: pop and re-put so the freshest save wins.
                    try:
                        save_queue.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        save_queue.put_nowait(job)
                    except queue.Full:
                        pass
                    log.warning("save queue full; dropped a save")

            # Encode the annotated JPEG and ship it back. The parent
            # publishes it onto live_annotated.
            ok, buf = cv2.imencode(".jpg", annotated, jpeg_q)
            if not ok:
                annotated_jpeg = b""
            else:
                annotated_jpeg = buf.tobytes()

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
                for d in display
            ]
            try:
                out_queue.put_nowait(FrameOut(
                    frame_id=item.frame_id,
                    ts=item.ts,
                    source_name=item.source_name,
                    annotated_jpeg=annotated_jpeg,
                    detections_summary=summary,
                    infer_ms=infer_ms,
                    n_detections=n_detections,
                    had_fish=had_fish,
                ))
            except queue.Full:
                # Parent draining slowly — drop. Live overlay will skip a
                # frame, persisted data is unaffected.
                pass
    except Exception:
        log.exception("inference subprocess: unhandled error, exiting")
    finally:
        # Drain pending saves before pg is closed so record_saved_frame
        # rows for queued jobs still land. Sentinel + join with timeout.
        try:
            save_queue.put_nowait(None)
        except Exception:
            save_worker_stop.set()
        save_thread.join(timeout=10.0)
        if save_thread.is_alive():
            log.warning("save worker did not drain in 10s; %d job(s) lost",
                        save_queue.qsize())
        try:
            _flush_frame_stats()
        except Exception:
            log.exception("flush frame_stats failed")
        try:
            event_log.close()
        except Exception:
            pass
        if pg:
            try:
                pg.close()
            except Exception:
                pass
        log.info("inference subprocess: exited")

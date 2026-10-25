"""Inference subprocess: runs detector + classifier, persistence, and
the overlay encode in a separate OS process from the FastAPI /
FrameTap parent.

Why a separate process instead of a thread? The Python GIL. PyTorch
and numpy ops release the GIL for their core math, but plenty of
Python-side work (tensor → numpy conversion, dict construction, list
comprehensions inside the smoother) runs under the GIL. With
everything in-process, the FrameTap thread that reads RTSP frames was
spending up to 50 % of its time blocked behind the inference thread
and missing native-fps frames. Out-of-process means FrameTap gets a
whole CPU + GIL to itself.

Communication uses **shared memory + small metadata queues** under the
spawn context (no fork, no shared CUDA state). The frame bytes never
travel through pickle:

    worker process (parent)                         inference subprocess
    ──────────────────────                          ────────────────────
       FrameTap                                       run_inference_worker
            │                                              │
            │ 1. memcpy frame into slot[i] of shm pool      │
            │ 2. put FrameIn(slot_idx=i, shape, ts)         │
            │    onto filled_q                              │
            │                                ────────▶      │ 3. get FrameIn
            │                                               │ 4. read shm[i] as
            │                                               │    zero-copy view
            │                                               │ 5. detector +
            │                                               │    classifier +
            │                                               │    smoother +
            │                                               │    persistence +
            │                                               │    annotate +
            │                                               │    JPEG encode
            │                                               │ 6. put slot_idx
            │                            ◀──────── empty_q ─┤    back on empty_q
            │ 7. reuse slot                                 │ 7. put FrameOut on
            │                            ◀──── out_queue ──┘    out_queue
            │ 8. drain thread publishes annotated JPEG
            ▼
        live_annotated (LiveBuffer)

3 shm slots × 6.2 MB each → 18 MB /dev/shm per worker. The annotated
JPEG bytes coming back (~100-200 KB) DO go through the small
out_queue via pickle — small enough that the cost is irrelevant.

The classifier backend is selected at subprocess boot from
`CLASSIFIER_BACKEND`:
  * `v2` (default) — DINOv2 ViT-S/14 trained from random init
  * `fishial` — legacy 866-class ArcFace + sub-centre + KNN

Both backends share `pipeline_types.FishDetection`, so the rest of
the loop is backend-agnostic.

`NegativeRegionFilter` runs immediately after the classifier and
drops any detection whose bbox closely matches a configured
no-fish-zone polygon (the SEAHIVE holes, etc.). See
`negative_regions.py`.
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
    """Worker → inference. Metadata-only — the frame bytes themselves
    live in a shared-memory slot referenced by `slot_idx`. The
    inference subprocess attaches via FrameShmReader and reconstructs
    a zero-copy numpy view at `shape`. Slot is returned to the
    parent's empty_q after the subprocess is done reading."""

    frame_id: int
    slot_idx: int
    shape: tuple[int, int, int]  # (H, W, C) of the frame in the slot
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
    frame_shm_descriptor=None,
    empty_q: mp.Queue | None = None,
) -> None:
    """Subprocess entry point. Initialises models + persistence, drains
    `FrameIn` metadata from `in_queue` (the "filled" queue), reads the
    actual frame bytes from the shared-memory slot referenced by each
    metadata, processes it, and returns the slot index to `empty_q` so
    the parent can reuse it. Results are emitted on `out_queue`.
    Returns when it receives `SHUTDOWN`.

    `frame_shm_descriptor` is a FrameShmDescriptor — the picklable handle
    for attaching to the parent-allocated shared-memory segments. It is
    optional only so legacy callers (none in this repo) can still hand
    raw numpy frames via FrameIn.frame_bgr; the modern path always sets
    both descriptor and empty_q."""
    # Delayed imports: keep CUDA + heavy deps out of the parent.
    import threading

    import cv2

    from .frame_shm import FrameShmReader
    from .negative_regions import NegativeRegionFilter
    from .overlay import annotate
    from .persistence import EventLog, ImageSaver, PgWriter
    from .provenance import compute as compute_provenance
    from .tracker import DetectionSmoother

    # Pick the classifier backend at subprocess boot. Default is "v2"
    # (DINOv2 ViT-S/14 from-scratch); "fishial" keeps the legacy
    # 866-class Fishial pipeline available as a one-flag revert.
    backend = (cfg.classifier_backend or "v2").strip().lower()
    if backend == "v2":
        from .classifier_v2 import ClassifierV2Pipeline as _Pipeline
    elif backend == "fishial":
        from .fishial import FishialPipeline as _Pipeline
    else:
        raise RuntimeError(f"unknown CLASSIFIER_BACKEND={backend!r}; expected 'v2' or 'fishial'")

    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    log.info(
        "inference subprocess: starting (pid=%d, device=%s, classifier_backend=%s)",
        os.getpid(),
        cfg.device,
        backend,
    )

    # Attach to the parent's shared-memory frame pool. Done before model
    # init so a misconfigured pool fails fast rather than after a 25 s
    # torch.compile warmup.
    frame_reader: FrameShmReader | None = None
    if frame_shm_descriptor is not None:
        frame_reader = FrameShmReader(frame_shm_descriptor)
        log.info(
            "frame pool attached: %d slot(s), %d bytes each",
            len(frame_reader.shms),
            frame_reader.slot_bytes,
        )

    provenance = compute_provenance(cfg)

    classifier = _Pipeline(cfg)
    negative_filter = NegativeRegionFilter(getattr(cfg, "negative_regions_path", None))
    event_log = EventLog(cfg.events_log_dir)
    saver = ImageSaver(cfg)

    pg: PgWriter | None = None
    try:
        pg = PgWriter(cfg.database_url, provenance=provenance, location_id=cfg.location_id)
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
                        job.ts,
                        job.frame_id,
                        job.source_name,
                        job.reason,
                        job.img_key,
                        job.coco_key,
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

            slot_idx = getattr(item, "slot_idx", None)
            slot_released = False

            def _release_slot(
                slot_idx=slot_idx,
            ) -> None:  # bound per item: the closure outlives the loop variable
                nonlocal slot_released
                if slot_released:
                    return
                slot_released = True
                if slot_idx is None or empty_q is None:
                    return
                try:
                    empty_q.put_nowait(slot_idx)
                except Exception:
                    log.exception("failed to return shm slot %d", slot_idx)

            try:
                if item.reset_smoother and smoother is not None:
                    smoother.reset()
                    log.info("smoother reset (post-fallback transition)")

                # Resolve the actual frame bytes. With the shared-memory
                # pool active, item carries (slot_idx, shape) and the
                # frame lives in /dev/shm; reconstruct a zero-copy numpy
                # view over it. Legacy callers (none in this repo) can
                # still pass the full numpy array on item.frame_bgr.
                if frame_reader is not None and slot_idx is not None:
                    frame_bgr = frame_reader.view(slot_idx, item.shape)
                else:
                    frame_bgr = getattr(item, "frame_bgr", None)
                    if frame_bgr is None:
                        log.error("frame %d has no payload; skipping", item.frame_id)
                        continue

                t0 = time.time()
                detections = classifier.process_frame(frame_bgr)
                # Drop detections that fall inside a configured no-fish
                # zone for this camera (stationary reef geometry —
                # SEAHIVE holes etc.). Applied here, post-classifier,
                # so the filter sees the exact same bboxes the rest of
                # the pipeline would have persisted.
                if negative_filter.has_regions and detections:
                    h, w = frame_bgr.shape[:2]
                    detections, _n_filtered = negative_filter.filter_detections(
                        item.source_name,
                        detections,
                        w,
                        h,
                    )
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
                            pg.record_detections(
                                item.ts, item.frame_id, item.source_name, detections, track_ids
                            )
                        except Exception:
                            log.exception("pg: detection insert failed (will retry)")

                # Sub-sampled frame stats for drift monitoring
                if item.frame_id % max(1, cfg.frame_stats_every_n_frames) == 0:
                    small = cv2.resize(frame_bgr, (160, 90), interpolation=cv2.INTER_AREA)
                    b, g, r = small[..., 0], small[..., 1], small[..., 2]
                    luma = 0.114 * b + 0.587 * g + 0.299 * r
                    mean_conf = (
                        float(np.mean([d.det_conf for d in detections])) if detections else 0.0
                    )
                    frame_stats_buf.append(
                        (
                            item.ts,
                            item.source_name,
                            item.frame_id,
                            float(luma.mean()),
                            float(r.mean()),
                            float(g.mean()),
                            float(b.mean()),
                            float(luma.std()),
                            n_detections,
                            mean_conf,
                        )
                    )
                    if pg and len(frame_stats_buf) >= frame_stats_flush_threshold:
                        _flush_frame_stats()

                # Image save. `annotate()` copies internally so the
                # returned buffer survives the slot release. The raw
                # frame is only copied when a save job actually
                # materialises — most frames don't trigger a save, so
                # the common path stays zero-copy.
                annotated = annotate(frame_bgr, display) if display else frame_bgr
                job = saver.maybe_save(
                    frame_bgr=frame_bgr,
                    annotated_bgr=annotated,
                    detections=detections,
                    frame_id=item.frame_id,
                    ts=item.ts,
                    source_name=item.source_name,
                    now_mono=time.monotonic(),
                    track_ids=track_ids,
                )
                if job is not None:
                    # The save-worker thread writes after the slot is
                    # recycled, so the SaveJob mustn't hold a shm view.
                    # Annotated is already a separate buffer when
                    # annotate() ran; only copy when display was empty
                    # and annotated aliases frame_bgr.
                    if frame_reader is not None and slot_idx is not None:
                        job.frame_bgr = np.ascontiguousarray(job.frame_bgr).copy()
                        if job.annotated_bgr is frame_bgr or job.annotated_bgr is job.frame_bgr:
                            job.annotated_bgr = job.frame_bgr
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
                # publishes it onto live_annotated. `annotated` is a
                # standalone buffer at this point so the slot can be
                # released as soon as imencode is done.
                ok, buf = cv2.imencode(".jpg", annotated, jpeg_q)
                annotated_jpeg = buf.tobytes() if ok else b""

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
                    out_queue.put_nowait(
                        FrameOut(
                            frame_id=item.frame_id,
                            ts=item.ts,
                            source_name=item.source_name,
                            annotated_jpeg=annotated_jpeg,
                            detections_summary=summary,
                            infer_ms=infer_ms,
                            n_detections=n_detections,
                            had_fish=had_fish,
                        )
                    )
                except queue.Full:
                    # Parent draining slowly — drop. Live overlay will
                    # skip a frame, persisted data is unaffected.
                    pass
            finally:
                # Always return the slot — even on mid-iteration crash —
                # so the parent's empty_q doesn't bleed out.
                _release_slot()
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
            log.warning("save worker did not drain in 10s; %d job(s) lost", save_queue.qsize())
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
        if frame_reader is not None:
            try:
                frame_reader.close()
            except Exception:
                pass
        log.info("inference subprocess: exited")

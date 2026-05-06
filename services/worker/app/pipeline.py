"""Inference loop: pull frames, dispatch to the inference subprocess,
publish annotated overlays.

Three threads + one subprocess:

- `FrameTap` (thread)              reads source frames at native fps,
                                   publishes raw MJPEG immediately, hands
                                   off the latest non-fallback frame to
                                   the inference subprocess.
- `inference_proc.run_inference_worker` (subprocess)
                                   detector + classifier + persistence +
                                   overlay encode. See inference_proc.py.
- `InferenceClient.drain_thread`   pulls FrameOut results from the subprocess
                                   and publishes the annotated JPEG to the
                                   `live_annotated` buffer.

Putting inference in a separate OS process means the FrameTap thread is
not blocked behind it on the GIL. Raw stream therefore tracks the source
fps regardless of how heavy inference is.
"""
from __future__ import annotations

import logging
import multiprocessing as mp
import queue
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime

import cv2

from .inference_proc import SHUTDOWN, FrameIn, FrameOut, run_inference_worker
from .sources import VideoSource

log = logging.getLogger(__name__)


@dataclass
class LiveFrame:
    """Latest annotated + raw frames + detections, served to dashboard clients.

    With the FrameTap split there are two LiveBuffers in play: one populated
    by FrameTap at native fps (only `jpeg_raw` is meaningful) and one
    populated by the InferenceClient drain thread at inference rate (only
    `jpeg` + the detection summary are meaningful). We reuse the same
    dataclass for both so the MJPEG/snapshot endpoints can pick out
    whichever field they need.
    """
    jpeg: bytes              # annotated (with bboxes + labels)
    jpeg_raw: bytes          # source-pixel frame, no overlay
    frame_id: int
    ts: datetime
    source_name: str
    detections_summary: list
    infer_ms: float = 0.0

    @classmethod
    def empty(cls) -> LiveFrame:
        return cls(
            jpeg=b"", jpeg_raw=b"", frame_id=0,
            ts=datetime.now(UTC),
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

    def wait_for_next(self, last_frame_id: int, timeout: float = 5.0) -> LiveFrame | None:
        with self._cond:
            if self._current.frame_id == last_frame_id:
                self._cond.wait(timeout=timeout)
            return self._current if self._current.frame_id != last_frame_id else None


@dataclass
class PipelineStats:
    frames_seen: int = 0           # frames grabbed from source (native fps)
    frames_inferred: int = 0       # frames the inference subprocess processed
    frames_with_fish: int = 0
    detections_total: int = 0
    last_infer_ms: float = 0.0
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_frame_at: datetime | None = None
    current_source: str = ""
    # frames consumed during autoswitch fallback (no video data is collected
    # from these — they're a visual placeholder only)
    fallback_frames: int = 0


def _drain_old(q: mp.Queue) -> None:
    """Empty a queue without blocking. Used to keep the inference in-queue
    drop-old: when FrameTap finds the queue full, it drains and re-puts so
    the freshest frame is what inference sees."""
    while True:
        try:
            q.get_nowait()
        except queue.Empty:
            return


class FrameTap:
    """Reads frames from the source at native fps in its own thread.

    For every frame:
    - increments grab-side stats
    - encodes raw JPEG and publishes to `live_raw`
    - if not in fallback: pushes a FrameIn to the inference subprocess's
      in-queue (drop-old when full)
    - if in fallback: also publishes the raw JPEG onto `live_annotated`
      (no overlay) so a viewer with bboxes ON sees the fallback frame
      rather than a frozen prior frame; skips the inference round-trip.

    Owns the source (closes it on stop)."""

    def __init__(
        self,
        cfg,
        source: VideoSource,
        live_raw: LiveBuffer,
        live_annotated: LiveBuffer,
        in_queue: mp.Queue,
        stats: PipelineStats,
    ) -> None:
        self.cfg = cfg
        self.source = source
        self.live_raw = live_raw
        self.live_annotated = live_annotated
        self.in_queue = in_queue
        self.stats = stats
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

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
        was_fallback = False
        try:
            for frame_id, frame, source_name in self.source.frames():
                if self._stop.is_set():
                    break
                ts = datetime.now(UTC)
                in_fallback = bool(getattr(self.source, "is_dark", False))

                self.stats.frames_seen += 1
                self.stats.last_frame_at = ts
                self.stats.current_source = source_name
                if in_fallback:
                    self.stats.fallback_frames += 1

                # publish raw JPEG for /stream_raw.mjpeg & /snapshot_raw.jpg
                ok, buf = cv2.imencode(".jpg", frame, jpeg_q)
                jpeg_bytes = buf.tobytes() if ok else b""
                if jpeg_bytes:
                    self.live_raw.publish(LiveFrame(
                        jpeg=b"", jpeg_raw=jpeg_bytes, frame_id=frame_id, ts=ts,
                        source_name=source_name, detections_summary=[], infer_ms=0.0,
                    ))

                if in_fallback:
                    if not was_fallback:
                        log.info("autoswitch transition: in_fallback=True — PAUSING video-data collection")
                    was_fallback = True
                    # Mirror raw onto annotated so bbox-on viewers also see
                    # the fallback frame (no overlay drawn).
                    if jpeg_bytes:
                        self.live_annotated.publish(LiveFrame(
                            jpeg=jpeg_bytes, jpeg_raw=b"", frame_id=frame_id, ts=ts,
                            source_name=source_name, detections_summary=[], infer_ms=0.0,
                        ))
                    continue

                # ----- live-camera path: dispatch to inference subprocess -----
                reset_smoother = was_fallback  # first live frame after fallback
                if was_fallback:
                    log.info("autoswitch transition: in_fallback=False — RESUMING video-data collection")
                was_fallback = False

                msg = FrameIn(
                    frame_id=frame_id,
                    frame_bgr=frame,
                    ts=ts,
                    source_name=source_name,
                    reset_smoother=reset_smoother,
                )
                # Drop-old: replace whatever the subprocess hasn't picked up yet.
                try:
                    self.in_queue.put_nowait(msg)
                except queue.Full:
                    _drain_old(self.in_queue)
                    try:
                        self.in_queue.put_nowait(msg)
                    except queue.Full:
                        pass  # extreme race, skip frame
        except Exception:
            log.exception("frame-tap crashed")


class InferenceClient:
    """Owns the inference subprocess and its in/out queues. Spawns the
    subprocess on `start()`, signals shutdown on `stop()`, and runs a
    drain thread that pulls FrameOut results and publishes the annotated
    JPEG to `live_annotated`."""

    def __init__(
        self,
        cfg,
        live_annotated: LiveBuffer,
        stats: PipelineStats,
        in_queue: mp.Queue,
        out_queue: mp.Queue,
        proc: mp.Process,
    ) -> None:
        self.cfg = cfg
        self.live_annotated = live_annotated
        self.stats = stats
        self.in_queue = in_queue
        self.out_queue = out_queue
        self.proc = proc
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._drain, name="infer-drain", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        try:
            self.in_queue.put_nowait(SHUTDOWN)
        except Exception:
            pass
        if self.proc.is_alive():
            self.proc.join(timeout=timeout)
            if self.proc.is_alive():
                log.warning("inference subprocess didn't exit; terminating")
                self.proc.terminate()
                self.proc.join(timeout=2.0)
                if self.proc.is_alive():
                    self.proc.kill()
        if self._thread:
            self._thread.join(timeout=2.0)

    def _drain(self) -> None:
        while not self._stop.is_set():
            try:
                out: FrameOut = self.out_queue.get(timeout=2.0)
            except queue.Empty:
                # Sanity check: if the subprocess died, surface it loudly.
                if not self.proc.is_alive():
                    log.error("inference subprocess died; drain thread exiting")
                    return
                continue
            except Exception:
                log.exception("infer-drain: queue read failed")
                continue

            self.stats.frames_inferred += 1
            self.stats.last_infer_ms = out.infer_ms
            if out.had_fish:
                self.stats.frames_with_fish += 1
            self.stats.detections_total += out.n_detections

            if out.annotated_jpeg:
                self.live_annotated.publish(LiveFrame(
                    jpeg=out.annotated_jpeg,
                    jpeg_raw=b"",
                    frame_id=out.frame_id,
                    ts=out.ts,
                    source_name=out.source_name,
                    detections_summary=out.detections_summary,
                    infer_ms=out.infer_ms,
                ))


def spawn_inference(
    cfg,
    live_annotated: LiveBuffer,
    stats: PipelineStats,
    in_qsize: int = 1,
    out_qsize: int = 8,
) -> InferenceClient:
    """Spawn the inference subprocess and return an InferenceClient bound
    to its queues. Caller starts the client (drain thread) and gets the
    `in_queue` to hand to FrameTap."""
    ctx = mp.get_context("spawn")
    in_queue: mp.Queue = ctx.Queue(maxsize=in_qsize)
    out_queue: mp.Queue = ctx.Queue(maxsize=out_qsize)
    ready: mp.synchronize.Event = ctx.Event()
    proc = ctx.Process(
        target=run_inference_worker,
        args=(cfg, in_queue, out_queue, ready),
        name="inference-worker",
        daemon=True,  # parent crash → subprocess dies too (no orphans)
    )
    proc.start()
    log.info("inference subprocess: spawned (pid=%s); waiting for ready", proc.pid)
    if not ready.wait(timeout=120.0):
        log.error("inference subprocess: not ready after 120s; aborting startup")
        proc.terminate()
        proc.join(timeout=5.0)
        raise RuntimeError("inference subprocess did not become ready")
    log.info("inference subprocess: ready")
    return InferenceClient(
        cfg=cfg,
        live_annotated=live_annotated,
        stats=stats,
        in_queue=in_queue,
        out_queue=out_queue,
        proc=proc,
    )

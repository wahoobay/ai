"""Worker entry point — FastAPI + inference loop in one process.

Endpoints:
  GET /healthz          liveness
  GET /readyz           readiness (models loaded + first frame served)
  GET /stream.mjpeg     MJPEG multipart stream of annotated frames
  GET /snapshot.jpg     single latest annotated frame
  GET /live.json        latest frame metadata + detections
  GET /stats            runtime counters
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse, StreamingResponse

from .config import Config
from .persistence import PgWriter
from .pipeline import (
    FrameTap,
    InferenceClient,
    LiveBuffer,
    PipelineStats,
    spawn_inference,
)
from .provenance import compute as compute_provenance
from .ptz import PTZPoller
from .sources import _strip_creds, source_from_config

log = logging.getLogger("wahoobay.worker")


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


class WorkerApp:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        # Two buffers: annotated updates at inference rate; raw updates at
        # source's native fps. Viewers toggle between them via /stream{,_raw}.
        self.live = LiveBuffer()
        self.live_raw = LiveBuffer()
        self.stats = PipelineStats()
        self.source = None
        self.tap: FrameTap | None = None
        self.infer: InferenceClient | None = None
        self.ptz: PTZPoller | None = None
        self._ptz_pg: PgWriter | None = None

    def start(self) -> None:
        cfg = self.cfg
        log.info("starting worker: source=%s device=%s", _strip_creds(cfg.video_source), cfg.device)

        self.source = source_from_config(cfg)

        # Spawn the inference subprocess BEFORE we open any DB / source
        # handles in this process: the subprocess will inherit nothing
        # (spawn-context, fresh interpreter), and the parent stays
        # CUDA-free. Returns an InferenceClient bound to the subprocess's
        # in/out queues.
        self.infer = spawn_inference(
            cfg=cfg,
            live_annotated=self.live,
            stats=self.stats,
        )
        self.infer.start()

        # Frame-grabber thread owns the source and dispatches non-fallback
        # frames to the inference subprocess via the in-queue.
        self.tap = FrameTap(
            cfg=cfg,
            source=self.source,
            live_raw=self.live_raw,
            live_annotated=self.live,
            in_queue=self.infer.in_queue,
            stats=self.stats,
        )
        self.tap.start()

        # PTZ poller is the only thing in the parent process that talks to
        # Postgres. The inference subprocess has its own pg connection for
        # detection events, frame_stats, and saved_frames.
        if cfg.ptz_poll_enabled:
            try:
                provenance = compute_provenance(cfg)
                self._ptz_pg = PgWriter(cfg.database_url, provenance=provenance)
                self.ptz = PTZPoller(cfg, self._ptz_pg)
                self.ptz.start()
            except Exception as e:
                log.warning("PTZ poller startup failed (%s); skipping", e)

    def stop(self) -> None:
        if self.ptz:
            self.ptz.stop()
        if self._ptz_pg:
            try:
                self._ptz_pg.close()
            except Exception:
                pass
        if self.tap:
            self.tap.stop()
        if self.infer:
            self.infer.stop()


def build_app(cfg: Config | None = None) -> FastAPI:
    cfg = cfg or Config.from_env()
    worker = WorkerApp(cfg)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        worker.start()
        try:
            yield
        finally:
            worker.stop()

    app = FastAPI(title="Wahoo Bay worker", lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True}

    @app.get("/readyz")
    async def readyz() -> Response:
        # raw buffer ticks first (FrameTap is ahead of inference), so use it
        live = worker.live_raw.snapshot()
        if live.frame_id > 0:
            return JSONResponse({"ready": True, "frame_id": live.frame_id})
        return JSONResponse({"ready": False}, status_code=503)

    @app.get("/snapshot.jpg")
    async def snapshot() -> Response:
        live = worker.live.snapshot()
        if not live.jpeg:
            return Response(status_code=503)
        return Response(content=live.jpeg, media_type="image/jpeg")

    @app.get("/snapshot_raw.jpg")
    async def snapshot_raw() -> Response:
        live = worker.live_raw.snapshot()
        if not live.jpeg_raw:
            return Response(status_code=503)
        return Response(content=live.jpeg_raw, media_type="image/jpeg")

    @app.get("/live.json")
    async def live_json() -> dict:
        live = worker.live.snapshot()
        return {
            "frame_id": live.frame_id,
            "ts": live.ts.isoformat(),
            "source_name": live.source_name,
            "infer_ms": live.infer_ms,
            "detections": live.detections_summary,
        }

    @app.get("/stats")
    async def stats() -> dict:
        s = worker.stats if worker.infer else None
        if s is None:
            return {"running": False}
        # autoswitch state (if AutoswitchSource is active)
        autoswitch = None
        src = worker.source
        if src is not None and hasattr(src, "is_dark"):
            autoswitch = {
                "active": True,
                "is_dark": getattr(src, "is_dark", False),
                "last_luma": getattr(src, "last_luma", None),
                "last_avg_luma": getattr(src, "last_avg_luma", None),
                "switches": getattr(src, "switches", 0),
                "dark_threshold": getattr(src, "dark_threshold", None),
                "light_threshold": getattr(src, "light_threshold", None),
            }
        ptz = None
        if worker.ptz is not None:
            ps = worker.ptz.stats
            ptz = {
                "enabled": ps.enabled,
                "polls": ps.poll_count,
                "successes": ps.success_count,
                "last_success_at": ps.last_success_at.isoformat() if ps.last_success_at else None,
                "last_pan_deg": ps.last_pan_deg,
                "last_tilt_deg": ps.last_tilt_deg,
                "last_zoom": ps.last_zoom,
                "last_error": ps.last_error,
            }
        return {
            "running": True,
            "frames_seen": s.frames_seen,         # grabbed at native fps
            "frames_inferred": s.frames_inferred, # processed by detector+classifier
            "frames_with_fish": s.frames_with_fish,
            "detections_total": s.detections_total,
            "last_infer_ms": s.last_infer_ms,
            "started_at": s.started_at.isoformat(),
            "last_frame_at": s.last_frame_at.isoformat() if s.last_frame_at else None,
            "current_source": s.current_source,
            "fallback_frames": s.fallback_frames,
            "autoswitch": autoswitch,
            "ptz": ptz,
        }

    BOUNDARY = b"wahoobay-mjpeg-boundary"

    def _mjpeg_streaming_response(buffer: LiveBuffer, pick_jpeg) -> StreamingResponse:
        """Shared MJPEG generator parameterised on which buffer to drain and
        which JPEG byte-payload to send. `pick_jpeg(LiveFrame) -> bytes`."""
        async def gen() -> AsyncIterator[bytes]:
            last = -1
            loop = asyncio.get_running_loop()
            current = buffer.snapshot()
            if current.frame_id > 0:
                last = current.frame_id
                payload = pick_jpeg(current)
                if payload:
                    yield _mjpeg_chunk(payload, BOUNDARY)
            while True:
                current = await loop.run_in_executor(
                    None, buffer.wait_for_next, last, 10.0,
                )
                if current is None:
                    continue
                last = current.frame_id
                payload = pick_jpeg(current)
                if payload:
                    yield _mjpeg_chunk(payload, BOUNDARY)
        return StreamingResponse(
            gen(),
            media_type=f"multipart/x-mixed-replace; boundary={BOUNDARY.decode()}",
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate, private",
                "Pragma": "no-cache",
            },
        )

    @app.get("/stream.mjpeg")
    async def stream() -> StreamingResponse:
        # Annotated stream: bboxes drawn, gated by inference rate.
        return _mjpeg_streaming_response(worker.live, lambda f: f.jpeg)

    @app.get("/stream_raw.mjpeg")
    async def stream_raw() -> StreamingResponse:
        # Raw stream: native fps, no overlay; bbox-toggle off shows this.
        return _mjpeg_streaming_response(worker.live_raw, lambda f: f.jpeg_raw)

    return app


def _mjpeg_chunk(jpeg: bytes, boundary: bytes) -> bytes:
    return (
        b"--" + boundary + b"\r\n"
        b"Content-Type: image/jpeg\r\n"
        b"Content-Length: " + str(len(jpeg)).encode() + b"\r\n\r\n"
        + jpeg + b"\r\n"
    )


def main() -> None:
    cfg = Config.from_env()
    _setup_logging(cfg.log_level)

    # graceful SIGTERM -> uvicorn will propagate to lifespan shutdown
    def _shutdown(signum, _frame):
        log.info("received signal %s, exiting", signum)
        sys.exit(0)
    signal.signal(signal.SIGTERM, _shutdown)

    app = build_app(cfg)
    uvicorn.run(
        app,
        host=cfg.worker_http_host,
        port=cfg.worker_http_port,
        log_level=cfg.log_level.lower(),
        access_log=False,
    )


if __name__ == "__main__":
    main()

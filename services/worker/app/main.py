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
import json
import logging
import os
import signal
import sys
from contextlib import asynccontextmanager
from typing import AsyncIterator

import uvicorn
from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse, StreamingResponse

from .config import Config
from .fishial import FishialPipeline
from .persistence import EventLog, ImageSaver, PgWriter
from .pipeline import LiveBuffer, PipelineRunner
from .provenance import compute as compute_provenance
from .ptz import PTZPoller
from .sources import source_from_config

log = logging.getLogger("wahoobay.worker")


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


class WorkerApp:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.live = LiveBuffer()
        self.runner: PipelineRunner | None = None
        self.ptz: PTZPoller | None = None

    def start(self) -> None:
        cfg = self.cfg
        log.info("starting worker: source=%s device=%s", cfg.video_source, cfg.device)

        provenance = compute_provenance(cfg)

        fishial = FishialPipeline(cfg)
        source = source_from_config(cfg)
        event_log = EventLog(cfg.events_log_dir)
        saver = ImageSaver(cfg)

        pg: PgWriter | None = None
        try:
            pg = PgWriter(cfg.database_url, provenance=provenance)
            log.info("postgres connected: %s", cfg.database_url.split("@")[-1])
        except Exception as e:
            log.warning("postgres unavailable (%s); continuing with jsonl-only", e)
            pg = None

        self.runner = PipelineRunner(
            cfg=cfg,
            source=source,
            fishial=fishial,
            live=self.live,
            pg=pg,
            event_log=event_log,
            saver=saver,
            provenance=provenance,
        )
        self.runner.start()

        if pg is not None:
            self.ptz = PTZPoller(cfg, pg)
            self.ptz.start()

    def stop(self) -> None:
        if self.ptz:
            self.ptz.stop()
        if self.runner:
            self.runner.stop()


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
        live = worker.live.snapshot()
        if live.frame_id > 0:
            return JSONResponse({"ready": True, "frame_id": live.frame_id})
        return JSONResponse({"ready": False}, status_code=503)

    @app.get("/snapshot.jpg")
    async def snapshot() -> Response:
        live = worker.live.snapshot()
        if not live.jpeg:
            return Response(status_code=503)
        return Response(content=live.jpeg, media_type="image/jpeg")

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
        s = worker.runner.stats if worker.runner else None
        if s is None:
            return {"running": False}
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
            "frames_seen": s.frames_seen,
            "frames_with_fish": s.frames_with_fish,
            "detections_total": s.detections_total,
            "last_infer_ms": s.last_infer_ms,
            "started_at": s.started_at.isoformat(),
            "last_frame_at": s.last_frame_at.isoformat() if s.last_frame_at else None,
            "current_source": s.current_source,
            "ptz": ptz,
        }

    BOUNDARY = b"wahoobay-mjpeg-boundary"

    @app.get("/stream.mjpeg")
    async def stream() -> StreamingResponse:
        async def gen() -> AsyncIterator[bytes]:
            last = -1
            loop = asyncio.get_running_loop()
            # Emit an initial frame (if any) immediately so the browser shows something.
            current = worker.live.snapshot()
            if current.frame_id > 0:
                last = current.frame_id
                yield _mjpeg_chunk(current.jpeg, BOUNDARY)
            while True:
                current = await loop.run_in_executor(
                    None, worker.live.wait_for_next, last, 10.0,
                )
                if current is None:
                    continue
                last = current.frame_id
                yield _mjpeg_chunk(current.jpeg, BOUNDARY)

        headers = {
            "Cache-Control": "no-cache, no-store, must-revalidate, private",
            "Pragma": "no-cache",
        }
        return StreamingResponse(
            gen(),
            media_type=f"multipart/x-mixed-replace; boundary={BOUNDARY.decode()}",
            headers=headers,
        )

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

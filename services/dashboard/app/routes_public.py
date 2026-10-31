"""The public read-only API under /api/public/, which the public website calls.

The site gate leaves this prefix open (auth.OPEN_PREFIXES), so nothing here may
carry reviewer or personal data. Every answer goes out through `cached()`, so
CloudFront and browsers hold it for a few seconds instead of every visitor
reaching the dashboard. Later routes (detections, species, environment,
exports) join this module."""

from __future__ import annotations

import asyncio

import httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from .common_names import common as common_name
from .config import autoplay_embed

# How long a public request waits for a camera's worker.
WORKER_TIMEOUT_S = 2.0


def cached(payload: dict | list, seconds: int, status: int = 200) -> JSONResponse:
    """A JSON answer that browsers and CloudFront may keep for `seconds`."""
    return JSONResponse(
        payload, status_code=status, headers={"Cache-Control": f"public, max-age={seconds}"}
    )


def build_public_router(cfg, state) -> APIRouter:
    router = APIRouter(prefix="/api/public")

    # (id, worker URL, suffix of the stream and snapshot paths) for each camera of the site.
    # The pier camera exists only where it has a worker; the reef camera is always listed
    # and, with no worker, is the embed.
    cameras = [("seahivecam", cfg.worker_url, "")]
    if cfg.worker_url_pier:
        cameras.append(("pier_cam", cfg.worker_url_pier, "_pier"))
    workers = {cam_id: worker for cam_id, worker, _ in cameras}

    async def worker_live(client: httpx.AsyncClient, worker: str) -> dict | None:
        """The worker's current frame from one GET of its /live.json; None when it does not
        answer 200 with JSON within WORKER_TIMEOUT_S (down, slow or broken all count)."""
        try:
            r = await client.get(f"{worker}/live.json", timeout=WORKER_TIMEOUT_S)
            return r.json() if r.status_code == 200 else None
        except Exception:
            return None

    @router.get("/cameras")
    async def public_cameras() -> JSONResponse:
        client: httpx.AsyncClient = state["client"]

        async def status(worker: str) -> str:
            if not worker:
                return "embed"
            return "live" if await worker_live(client, worker) is not None else "offline"

        statuses = await asyncio.gather(*(status(worker) for _, worker, _ in cameras))
        embed_url = autoplay_embed(cfg.live_video_embed_url) or None
        base = cfg.stream_base_url
        return cached(
            {
                "cameras": [
                    {
                        "id": cam_id,
                        "status": cam_status,
                        "stream_mjpeg": f"{base}/stream{suffix}_raw.mjpeg",
                        "snapshot": f"{base}/snapshot{suffix}_raw.jpg",
                        # The site's one embed shows the reef camera.
                        "embed_url": embed_url if cam_id == "seahivecam" else None,
                        "live": f"/api/public/live?camera={cam_id}",
                    }
                    for (cam_id, _, suffix), cam_status in zip(cameras, statuses, strict=True)
                ]
            },
            10,
        )

    @router.get("/live")
    async def public_live(camera: str = "seahivecam") -> JSONResponse:
        """The camera's current frame and its identified fish, for drawing boxes over the
        stream. bbox is [x1, y1, x2, y2] in frame pixels, the worker's own order."""
        if camera not in workers:
            return cached({"error": "unknown camera"}, 10, status=404)
        if not workers[camera]:
            return cached({"error": "no camera pipeline"}, 10, status=503)
        client: httpx.AsyncClient = state["client"]
        frame = await worker_live(client, workers[camera])
        if frame is None:
            return cached({"error": "camera offline"}, 10, status=502)
        detections = [
            {
                "bbox": list(d["bbox"]),
                "confidence": d.get("best_accuracy"),
                "species_id": d.get("best_species_id"),
                "latin": d["best_name"],
                "common": common_name(d["best_name"]),
            }
            for d in frame.get("detections") or []
            if d.get("best_name")
        ]
        return cached(
            {
                "camera": camera,
                "ts": frame.get("ts"),
                "frame_id": frame.get("frame_id"),
                # The worker reports no frame size (its /stats has none).
                "frame": None,
                "detections": detections,
            },
            1,
        )

    return router

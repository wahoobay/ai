"""The public read-only API under /api/public/, which the public website calls.

The site gate leaves this prefix open (auth.OPEN_PREFIXES), so nothing here may
carry reviewer or personal data. Every answer goes out through `cached()`, so
CloudFront and browsers hold it for a few seconds instead of every visitor
reaching the dashboard. Later routes (detections, species, environment,
exports) join this module, above the catch-all at the end of the router."""

from __future__ import annotations

import asyncio
import logging

import httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from .common_names import common as common_name
from .config import autoplay_embed

log = logging.getLogger(__name__)

# How long a public request waits for a camera's worker.
WORKER_TIMEOUT_S = 2.0
# STREAM_BASE_URL's default: the dashboard's own stream proxy, behind sign-in. Any other
# base is the stream origin (/stream on AWS, which CloudFront and Caddy hand to the worker
# with the prefix stripped), open to a signed-out visitor.
GATED_STREAM_BASE = "/api"


def cached(payload: dict | list, seconds: int, status: int = 200) -> JSONResponse:
    """A JSON answer that browsers and CloudFront may keep for `seconds`."""
    return JSONResponse(
        payload, status_code=status, headers={"Cache-Control": f"public, max-age={seconds}"}
    )


def _is_detection(d) -> bool:
    return isinstance(d, dict) and isinstance(d.get("bbox"), list) and len(d["bbox"]) == 4


def build_public_router(cfg, state) -> APIRouter:
    router = APIRouter(prefix="/api/public")

    embed_url = autoplay_embed(cfg.live_video_embed_url) or None
    stream_base = None if cfg.stream_base_url == GATED_STREAM_BASE else cfg.stream_base_url
    # Each camera of the site: (worker URL, whether the stream origin serves it, its embed).
    # The reef camera is always listed and is the site's one embed; the pier camera exists
    # only where it has a worker, and no stream origin serves it yet.
    cameras = {"seahivecam": (cfg.worker_url, stream_base is not None, embed_url)}
    if cfg.worker_url_pier:
        cameras["pier_cam"] = (cfg.worker_url_pier, False, None)

    async def worker_live(client: httpx.AsyncClient, camera: str, worker: str) -> dict | None:
        """The worker's current frame from one GET of its /live.json; None when it does not
        answer 200 with JSON within WORKER_TIMEOUT_S."""
        try:
            r = await client.get(f"{worker}/live.json", timeout=WORKER_TIMEOUT_S)
            if r.status_code == 200:
                return r.json()
            log.debug("public api: the %s worker answered %s", camera, r.status_code)
        except (httpx.HTTPError, ValueError) as e:
            log.debug("public api: the %s worker did not answer: %r", camera, e)
        return None

    @router.get("/cameras")
    async def public_cameras() -> JSONResponse:
        """Each camera's status and the media a signed-out visitor can fetch: `live` only
        when its worker answers and the stream origin serves it; otherwise the embed, if
        the camera has one, or offline. The site names its cameras from its own content."""
        client: httpx.AsyncClient = state["client"]

        async def entry(camera: str, worker: str, on_origin: bool, embed: str | None) -> dict:
            media = bool(worker) and on_origin
            if not worker:
                status = "embed"
            elif await worker_live(client, camera, worker) is None:
                status = "offline"
            elif media:
                status = "live"
            else:
                status = "embed" if embed else "offline"
            return {
                "id": camera,
                "status": status,
                "stream_mjpeg": f"{stream_base}/stream_raw.mjpeg" if media else None,
                "snapshot": f"{stream_base}/snapshot_raw.jpg" if media else None,
                "embed_url": embed,
                "live": f"/api/public/live?camera={camera}",
            }

        entries = await asyncio.gather(*(entry(cam, *spec) for cam, spec in cameras.items()))
        return cached({"cameras": list(entries)}, 10)

    @router.get("/live")
    async def public_live(camera: str = "seahivecam") -> JSONResponse:
        """The camera's current frame and its identified fish, for drawing boxes over the
        stream. bbox is [x1, y1, x2, y2] in frame pixels, the worker's own order. 404 for a
        camera the site does not have; 503 when there is no camera pipeline or its worker
        does not answer with a frame of the worker's shape."""
        if camera not in cameras:
            return cached({"error": "unknown camera"}, 10, status=404)
        worker = cameras[camera][0]
        if not worker:
            return cached({"error": "no camera pipeline"}, 10, status=503)
        client: httpx.AsyncClient = state["client"]
        frame = await worker_live(client, camera, worker)
        found = frame.get("detections") if isinstance(frame, dict) else None
        if not isinstance(found, list) or not all(_is_detection(d) for d in found):
            return cached({"error": "camera offline"}, 10, status=503)
        return cached(
            {
                "camera": camera,
                "ts": frame.get("ts"),
                "frame_id": frame.get("frame_id"),
                # The worker reports no frame size (its /stats has none).
                "frame": None,
                "detections": [
                    {
                        "bbox": d["bbox"],
                        "confidence": d.get("best_accuracy"),
                        "species_id": d.get("best_species_id"),
                        "latin": d["best_name"],
                        "common": common_name(d["best_name"]),
                    }
                    for d in found
                    if d.get("best_name")
                ],
            },
            1,
        )

    # Keep last: whatever no route above answers under the prefix is a cached 404 too.
    @router.get("/{rest:path}")
    async def public_not_found(rest: str) -> JSONResponse:
        return cached({"error": "not found"}, 10, status=404)

    return router

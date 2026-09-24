"""HTTP surface of the review tool (spec §10).

Thin handlers. The rules live in review_queue (and, from Tasks 8 and 9,
review_frames and review_browse), which raise ReviewError subclasses;
this module turns them into status codes. Every change carries
require_role and require_csrf, as everywhere since spec 1."""
from __future__ import annotations

import asyncio
import random

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from .auth import effective_role, make_require_role, require_csrf
from .review_images import frame_size, render_frame, render_zoom, zoom_region
from .review_queue import (
    FrameMissing,
    ReviewError,
    VerdictIn,
    build_payload,
    event_with_frame,
    fetch_one,
    next_item,
    recent_verdicts,
    release_item,
    reopen_payload,
    retire_item,
    skip_item,
    submit_verdict,
)

IMAGE_HEADERS = {"Cache-Control": "private, max-age=86400, immutable"}


def build_review_router(cfg, state, templates) -> APIRouter:
    router = APIRouter()
    require_role = make_require_role(cfg, state)
    reviewer = require_role("reviewer", "admin")
    admin_only = require_role("admin")
    rng = random.Random()

    def _db():
        pool = state.get("pool")
        if pool is None:
            raise HTTPException(status_code=503, detail="database unavailable")
        return pool

    def _catalogue():
        return state["review_catalogue"].get()

    def _pool():
        return state.get("review_pool")

    def _counts(name):
        pool_obj = _pool()
        return pool_obj.count(name) if pool_obj else 0

    def _fail(e: ReviewError) -> JSONResponse:
        return JSONResponse({"error": e.message}, status_code=e.status)

    async def _jpeg(fn, *args) -> Response:
        try:
            data = await asyncio.to_thread(fn, *args)
        except (OSError, ValueError):
            return JSONResponse({"error": "image file missing on disk"}, status_code=404)
        return Response(content=data, media_type="image/jpeg", headers=IMAGE_HEADERS)

    @router.get("/review", response_class=HTMLResponse)
    async def review_page(request: Request, user=Depends(reviewer)):
        version = state.get("asset_version")
        return templates.TemplateResponse(request, "review.html", {
            "display_name": user.display_name,
            "role": effective_role(user.status, user.role),
            "asset_version": version() if version else "",
        })

    @router.get("/api/review/next", dependencies=[Depends(require_csrf)])
    async def next_box(lane: str = Query("check"), user=Depends(reviewer)):
        pool_obj = _pool()
        order = pool_obj.species_order() if pool_obj else []
        empty = {"item": None, "pool": pool_obj.last_run if pool_obj else None}
        async with _db().connection() as conn:
            try:
                for _attempt in range(5):
                    item = await next_item(
                        conn, user_id=user.id, lane=lane, species_order=order,
                        spot_serve_rate=cfg.review_spotcheck_serve_rate,
                        claim_s=cfg.review_claim_s, rng=rng)
                    if item is None:
                        return JSONResponse(empty)
                    try:
                        return JSONResponse(await build_payload(
                            conn, item, catalogue=_catalogue(), counts=_counts,
                            target=cfg.review_target))
                    except FrameMissing:
                        # Review Focus 3: never hand out a box nobody can see.
                        await retire_item(conn, item["id"], "frame_missing")
                return JSONResponse(empty)
            except ReviewError as e:
                return _fail(e)

    @router.post("/api/review/verdicts", dependencies=[Depends(require_csrf)])
    async def post_verdict(body: VerdictIn, user=Depends(reviewer)):
        async with _db().connection() as conn:
            try:
                out = await submit_verdict(conn, user=user, body=body, catalogue=_catalogue(),
                                           spot_rate=cfg.review_spotcheck_rate, rng=rng)
            except ReviewError as e:
                return _fail(e)
        pool_obj = _pool()
        if pool_obj and out["first_pass"] and out["counts"] and not out["duplicate"]:
            pool_obj.note_verified(out["label"])
        return JSONResponse(out)

    @router.post("/api/review/items/{item_id}/skip", dependencies=[Depends(require_csrf)])
    async def skip(item_id: int, user=Depends(reviewer)):
        async with _db().connection() as conn:
            try:
                await skip_item(conn, user_id=user.id, item_id=item_id)
            except ReviewError as e:
                return _fail(e)
        return JSONResponse({"ok": True})

    @router.post("/api/review/items/{item_id}/release", dependencies=[Depends(require_csrf)])
    async def release(item_id: int, user=Depends(reviewer)):
        async with _db().connection() as conn:
            await release_item(conn, user_id=user.id, item_id=item_id)
        return JSONResponse({"ok": True})

    @router.get("/api/review/boxes/{event_id}")
    async def reopen(event_id: int, user=Depends(reviewer)):
        async with _db().connection() as conn:
            try:
                return JSONResponse(await reopen_payload(
                    conn, user=user, event_id=event_id, catalogue=_catalogue(),
                    counts=_counts, target=cfg.review_target))
            except ReviewError as e:
                return _fail(e)

    @router.get("/api/review/mine/recent")
    async def mine(user=Depends(reviewer)):
        async with _db().connection() as conn:
            return JSONResponse(await recent_verdicts(
                conn, user_id=user.id, session_id=user.session_id, catalogue=_catalogue()))

    @router.get("/api/review/species")
    async def species(user=Depends(reviewer)):
        return JSONResponse({"species": _catalogue().to_json()})

    @router.get("/api/review/species/{name_slug}/photo.jpg")
    async def species_photo(name_slug: str, user=Depends(reviewer)):
        found = _catalogue().photo(name_slug)
        if found is None:
            return JSONResponse({"error": "no photo for that species"}, status_code=404)
        return await _jpeg(render_frame, str(found[0]), 320)

    @router.get("/api/review/frames/{saved_frame_id}/image.jpg")
    async def frame_image(saved_frame_id: int, size: int = Query(1280, ge=160, le=1920),
                          user=Depends(reviewer)):
        async with _db().connection() as conn:
            row = await fetch_one(conn, "SELECT image_path FROM saved_frames WHERE id = %s",
                                  (saved_frame_id,))
        if row is None:
            return JSONResponse({"error": "no such frame"}, status_code=404)
        return await _jpeg(render_frame, row["image_path"], size)

    @router.get("/api/review/boxes/{event_id}/zoom.jpg")
    async def box_zoom(event_id: int, user=Depends(reviewer)):
        async with _db().connection() as conn:
            ev = await event_with_frame(conn, event_id)
        if ev is None or ev["image_path"] is None:
            return JSONResponse({"error": "no frame for this box"}, status_code=404)
        try:
            width, height = await asyncio.to_thread(frame_size, ev["image_path"])
        except (OSError, ValueError):
            return JSONResponse({"error": "image file missing on disk"}, status_code=404)
        return await _jpeg(render_zoom, ev["image_path"],
                           zoom_region(ev["bbox"], width, height))

    _ = admin_only    # used by the admin routes added in Task 9

    return router

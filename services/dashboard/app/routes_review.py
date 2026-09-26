"""HTTP surface of the review tool (spec §10).

Thin handlers. The rules live in review_queue (and, from Tasks 8 and 9,
review_frames and review_browse), which raise ReviewError subclasses;
this module turns them into status codes. Every change carries
require_role and require_csrf, as everywhere since spec 1."""
from __future__ import annotations

import asyncio
import logging
import random
from datetime import date

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from .auth import effective_role, make_require_role, require_csrf
from .review_browse import (
    AdjudicateIn,
    BrowseFilters,
    WindowIn,
    agreement,
    browse,
    get_window,
    list_disputes,
    pool_status,
    progress_summary,
    set_window,
)
from .review_frames import (
    AddedBoxIn,
    add_box,
    complete_frame,
    frame_record,
    frame_state,
    open_frame,
    release_frame,
    remove_box,
    skip_frame,
    uncomplete_frame,
    update_box,
)
from .review_images import frame_size, render_frame, render_zoom, zoom_region
from .review_labels import resolve_all
from .review_queue import (
    Conflict,
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

log = logging.getLogger(__name__)

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

    async def _catalogue():
        return await state["review_catalogue"].aget()

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
                        claim_s=cfg.review_claim_s, rng=rng,
                        skip_ttl_days=cfg.review_skip_ttl_days)
                    if item is None:
                        return JSONResponse(empty)
                    try:
                        return JSONResponse(await build_payload(
                            conn, item, catalogue=await _catalogue(), counts=_counts,
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
                out = await submit_verdict(conn, user=user, body=body, catalogue=await _catalogue(),
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
                    conn, user=user, event_id=event_id, catalogue=await _catalogue(),
                    counts=_counts, target=cfg.review_target))
            except ReviewError as e:
                return _fail(e)

    @router.get("/api/review/mine/recent")
    async def mine(user=Depends(reviewer)):
        async with _db().connection() as conn:
            return JSONResponse(await recent_verdicts(
                conn, user_id=user.id, session_id=user.session_id, catalogue=await _catalogue()))

    @router.get("/api/review/species")
    async def species(user=Depends(reviewer)):
        return JSONResponse({"species": (await _catalogue()).to_json()})

    @router.get("/api/review/species/{name_slug}/photo.jpg")
    async def species_photo(name_slug: str, user=Depends(reviewer)):
        found = (await _catalogue()).photo(name_slug)
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

    # ---- whole-frame mode (Task 8) ----

    async def _frame_call(fn, **kw):
        async with _db().connection() as conn:
            try:
                return JSONResponse(await fn(conn, **kw))
            except ReviewError as e:
                return _fail(e)

    @router.post("/api/review/frames/{saved_frame_id}/open", dependencies=[Depends(require_csrf)])
    async def frame_open(saved_frame_id: int, user=Depends(reviewer)):
        async with _db().connection() as conn:
            try:
                # Read (and validate) the frame before claiming anything,
                # so a missing or unreadable frame leaves no claims behind.
                await frame_record(conn, saved_frame_id)
                await open_frame(conn, saved_frame_id=saved_frame_id, user_id=user.id,
                                 claim_s=cfg.review_claim_s,
                                 skip_ttl_days=cfg.review_skip_ttl_days)
                return JSONResponse(await frame_state(
                    conn, saved_frame_id=saved_frame_id, user=user, catalogue=await _catalogue(),
                    counts=_counts, target=cfg.review_target))
            except ReviewError as e:
                return _fail(e)

    @router.get("/api/review/frames/{saved_frame_id}")
    async def frame_get(saved_frame_id: int, user=Depends(reviewer)):
        return await _frame_call(frame_state, saved_frame_id=saved_frame_id, user=user,
                                 catalogue=await _catalogue(), counts=_counts, target=cfg.review_target)

    @router.post("/api/review/frames/{saved_frame_id}/boxes",
                 dependencies=[Depends(require_csrf)])
    async def frame_add_box(saved_frame_id: int, body: AddedBoxIn, user=Depends(reviewer)):
        return await _frame_call(add_box, saved_frame_id=saved_frame_id, user=user, body=body,
                                 catalogue=await _catalogue())

    @router.patch("/api/review/frames/{saved_frame_id}/boxes/{box_id}",
                  dependencies=[Depends(require_csrf)])
    async def frame_update_box(saved_frame_id: int, box_id: int, body: AddedBoxIn,
                               user=Depends(reviewer)):
        return await _frame_call(update_box, saved_frame_id=saved_frame_id, box_id=box_id,
                                 user=user, body=body, catalogue=await _catalogue())

    @router.delete("/api/review/frames/{saved_frame_id}/boxes/{box_id}",
                   dependencies=[Depends(require_csrf)])
    async def frame_remove_box(saved_frame_id: int, box_id: int, user=Depends(reviewer)):
        async with _db().connection() as conn:
            try:
                await remove_box(conn, saved_frame_id=saved_frame_id, box_id=box_id, user=user)
            except ReviewError as e:
                return _fail(e)
        return JSONResponse({"ok": True})

    @router.post("/api/review/frames/{saved_frame_id}/complete",
                 dependencies=[Depends(require_csrf)])
    async def frame_complete(saved_frame_id: int, user=Depends(reviewer)):
        return await _frame_call(complete_frame, saved_frame_id=saved_frame_id, user=user,
                                 catalogue=await _catalogue())

    @router.post("/api/review/frames/{saved_frame_id}/release",
                 dependencies=[Depends(require_csrf)])
    async def frame_release(saved_frame_id: int, user=Depends(reviewer)):
        async with _db().connection() as conn:
            await release_frame(conn, saved_frame_id=saved_frame_id, user_id=user.id)
        return JSONResponse({"ok": True})

    @router.post("/api/review/frames/{saved_frame_id}/skip",
                 dependencies=[Depends(require_csrf)])
    async def frame_skip(saved_frame_id: int, user=Depends(reviewer)):
        async with _db().connection() as conn:
            await skip_frame(conn, saved_frame_id=saved_frame_id, user_id=user.id)
        return JSONResponse({"ok": True})

    @router.delete("/api/review/frames/{saved_frame_id}/complete",
                   dependencies=[Depends(require_csrf)])
    async def frame_uncomplete(saved_frame_id: int, user=Depends(admin_only)):
        async with _db().connection() as conn:
            await uncomplete_frame(conn, saved_frame_id=saved_frame_id)
        return JSONResponse({"ok": True})

    @router.get("/api/review/added/{box_id}/zoom.jpg")
    async def added_zoom(box_id: int, user=Depends(reviewer)):
        async with _db().connection() as conn:
            row = await fetch_one(conn, """
                SELECT a.bbox, sf.image_path FROM review_added_boxes a
                  JOIN saved_frames sf ON sf.id = a.saved_frame_id
                 WHERE a.id = %s AND a.removed_at IS NULL""", (box_id,))
        if row is None:
            return JSONResponse({"error": "no such drawn box"}, status_code=404)
        try:
            width, height = await asyncio.to_thread(frame_size, row["image_path"])
        except (OSError, ValueError):
            return JSONResponse({"error": "image file missing on disk"}, status_code=404)
        return await _jpeg(render_zoom, row["image_path"],
                           zoom_region(row["bbox"], width, height))

    # ---- browse, progress and the admin tools (Task 9) ----

    @router.get("/api/review/progress")
    async def progress(user=Depends(reviewer)):
        pool_obj = _pool()
        stale = False
        if pool_obj:
            try:
                await pool_obj.refresh_progress()
            except psycopg.errors.QueryCanceled:
                # The refresh runs under its own 5s statement timeout and
                # can lose that race under load; the counts it already has
                # are still worth serving, flagged stale, rather than a 500.
                stale = True
                log.warning("review progress: refresh_progress timed out; serving stale counts")
        try:
            async with _db().connection() as conn:
                summary = await progress_summary(
                    conn, pool_obj=pool_obj, catalogue=await _catalogue(), session_id=user.session_id,
                    target=cfg.review_target)
        except psycopg.errors.QueryCanceled:
            # Unlike the refresh above, this is progress_summary's own
            # query timing out: there is no cached summary to fall back
            # to, so this is a clear 503, never a hang and never a 500
            # (fix round 1, finding 6).
            log.warning("review progress: progress_summary timed out")
            return JSONResponse(
                {"error": "progress is taking too long to compute; try again shortly"},
                status_code=503)
        summary["stale"] = stale
        return JSONResponse(summary)

    @router.get("/api/review/browse")
    async def browse_boxes(status: str = "any", species: str | None = None,
                           camera: str | None = None, who: str = "anyone", lane: str = "any",
                           date_from: date | None = None, date_to: date | None = None,
                           page: int = Query(0, ge=0, le=100000), user=Depends(reviewer)):
        filters = BrowseFilters(status=status, species=species or None, camera=camera or None,
                                who=who, lane=lane, date_from=date_from, date_to=date_to)
        async with _db().connection() as conn:
            try:
                return JSONResponse(await browse(conn, catalogue=await _catalogue(), user=user,
                                                 filters=filters, page=page))
            except ReviewError as e:
                return _fail(e)
            except psycopg.errors.QueryCanceled:
                log.warning("review browse: query timed out")
                return JSONResponse(
                    {"error": "browse is taking too long to compute; try again shortly"},
                    status_code=503)

    @router.get("/api/review/disputes")
    async def disputes(user=Depends(admin_only)):
        async with _db().connection() as conn:
            return JSONResponse({"disputes": await list_disputes(conn, catalogue=await _catalogue())})

    @router.post("/api/review/disputes/{event_id}/adjudicate",
                 dependencies=[Depends(require_csrf)])
    async def adjudicate(event_id: int, body: AdjudicateIn, user=Depends(admin_only)):
        verdict = VerdictIn(event_id=event_id, outcome=body.outcome, label=body.label,
                            life_stage=body.life_stage, bbox=body.bbox)
        async with _db().connection() as conn:
            try:
                # Adjudication is for disputes (spec Sec8): the adjudication
                # path in submit_verdict skips every claim/item guard, so
                # without this check a POST here could close a queued box's
                # open or claimed pass-1 item out from under a volunteer
                # who is mid-answer, or short-circuit a pending spot-check.
                current = (await resolve_all(conn, [event_id])).get(event_id)
                if current is None or current.status != "disputed":
                    raise Conflict("this box is not disputed")
                return JSONResponse(await submit_verdict(
                    conn, user=user, body=verdict, catalogue=await _catalogue(), spot_rate=0.0,
                    rng=rng, adjudication=True))
            except ReviewError as e:
                return _fail(e)

    @router.get("/api/review/agreement")
    async def agreement_stats(user=Depends(admin_only)):
        async with _db().connection() as conn:
            return JSONResponse(await agreement(conn))

    @router.get("/api/review/status")
    async def status(user=Depends(admin_only)):
        async with _db().connection() as conn:
            return JSONResponse(await pool_status(conn, pool_obj=_pool()))

    @router.get("/api/review/settings/window")
    async def window_get(user=Depends(admin_only)):
        async with _db().connection() as conn:
            return JSONResponse(await get_window(conn))

    @router.put("/api/review/settings/window", dependencies=[Depends(require_csrf)])
    async def window_put(body: WindowIn, user=Depends(admin_only)):
        async with _db().connection() as conn:
            try:
                return JSONResponse(await set_window(conn, admin_id=user.id, start=body.start,
                                                     end=body.end))
            except ReviewError as e:
                return _fail(e)

    return router

"""HTTP surface of the review tool (spec §10).

Thin handlers. The rules live in review_queue (and, from Tasks 8 and 9,
review_frames and review_browse), which raise ReviewError subclasses;
this module turns them into status codes. Every change carries
require_role and require_csrf, as everywhere since spec 1."""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import date, timedelta
from typing import Literal

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from .auth import effective_role, make_require_role, require_csrf
from .review_browse import (
    AdjudicateIn,
    BrowseFilters,
    _check_date,
    agreement,
    browse,
    list_disputes,
    local_day_start,
    pool_status,
    progress_summary,
)
from .review_frames import (
    AddedBoxIn,
    MarkRestIn,
    add_box,
    complete_frame,
    frame_record,
    frame_state,
    mark_rest,
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
    Invalid,
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
from .species_suggestions import (
    ApproveIn,
    DetachIn,
    RejectIn,
    SuggestionIn,
    approve,
    create_suggestion,
    detach,
    list_for_admin,
    list_mine,
    merged_catalogue,
    pending_count,
    pending_entries,
    reject,
    suggestion_boxes,
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
            raise HTTPException(
                status_code=503, detail="database unavailable", headers={"Retry-After": "5"}
            )
        return pool

    async def _catalogue(conn=None):
        """The file-built catalogue plus every approved species suggestion. A caller that
        holds a pool connection passes it: taking a second one from the same pool while
        holding the first can stall every request once the pool is full (Ruling P1)."""
        base = await state["review_catalogue"].aget()
        if conn is not None:
            return await merged_catalogue(conn, base)
        async with _db().connection() as own:
            return await merged_catalogue(own, base)

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
        return templates.TemplateResponse(
            request,
            "review.html",
            {
                "display_name": user.display_name,
                "role": effective_role(user.status, user.role),
                "env_name": cfg.env_name,
                "asset_version": version() if version else "",
            },
        )

    @router.get("/api/review/next", dependencies=[Depends(require_csrf)])
    async def next_box(
        lane: str = Query("check"),
        date_from: date | None = Query(None),
        date_to: date | None = Query(None),
        user=Depends(reviewer),
    ):
        """`date_from`/`date_to` are the reviewer's own date window (local days, inclusive), kept
        in their browser session: only boxes inside it are handed out; nothing is stored or
        retired for anyone (2026-10-27)."""
        pool_obj = _pool()
        order = pool_obj.species_order() if pool_obj else []
        empty = {"item": None, "pool": pool_obj.last_run if pool_obj else None}
        async with _db().connection() as conn:
            try:
                _check_date(date_from, "date_from")
                _check_date(date_to, "date_to")
                if date_from and date_to and date_from > date_to:
                    raise Invalid("the window starts after it ends")
                window_start = local_day_start(date_from) if date_from else None
                window_end = local_day_start(date_to + timedelta(days=1)) if date_to else None
                for _attempt in range(5):
                    item = await next_item(
                        conn,
                        user_id=user.id,
                        lane=lane,
                        species_order=order,
                        spot_serve_rate=cfg.review_spotcheck_serve_rate,
                        claim_s=cfg.review_claim_s,
                        rng=rng,
                        skip_ttl_days=cfg.review_skip_ttl_days,
                        window_start=window_start,
                        window_end=window_end,
                    )
                    if item is None:
                        return JSONResponse(empty)
                    try:
                        return JSONResponse(
                            await build_payload(
                                conn,
                                item,
                                catalogue=await _catalogue(conn),
                                counts=_counts,
                                target=cfg.review_target,
                            )
                        )
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
                out = await submit_verdict(
                    conn,
                    user=user,
                    body=body,
                    catalogue=await _catalogue(conn),
                    spot_rate=cfg.review_spotcheck_rate,
                    rng=rng,
                )
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
                return JSONResponse(
                    await reopen_payload(
                        conn,
                        user=user,
                        event_id=event_id,
                        catalogue=await _catalogue(conn),
                        counts=_counts,
                        target=cfg.review_target,
                    )
                )
            except ReviewError as e:
                return _fail(e)

    @router.get("/api/review/mine/recent")
    async def mine(user=Depends(reviewer)):
        async with _db().connection() as conn:
            return JSONResponse(
                await recent_verdicts(
                    conn,
                    user_id=user.id,
                    session_id=user.session_id,
                    catalogue=await _catalogue(conn),
                )
            )

    @router.get("/api/review/species")
    async def species(user=Depends(reviewer)):
        """Every choice, reviewers' additions included, plus any pending suggestion left over
        from ai #49 that this caller may pick (their own; all of them for an admin), marked
        "waiting for approval"."""
        async with _db().connection() as conn:
            catalogue = await _catalogue(conn)
            pending = await pending_entries(conn, user=user, catalogue=catalogue)
        return JSONResponse({"species": catalogue.to_json() + pending})

    @router.post("/api/review/species-suggestions", dependencies=[Depends(require_csrf)])
    async def suggest_species(body: SuggestionIn, user=Depends(reviewer)):
        """Add the species the reviewer chose from WoRMS to everyone's list at once. The
        response is the new choice (`key`, `name`, `life_stage`, `common`) and its
        `suggestion_id`, for the page to answer the box with; a refusal is 400 or 409."""
        async with _db().connection() as conn:
            try:
                return JSONResponse(
                    await create_suggestion(
                        conn, user=user, body=body, catalogue=await _catalogue(conn)
                    )
                )
            except ReviewError as e:
                return _fail(e)

    @router.get("/api/review/species-suggestions/mine")
    async def my_suggestions(user=Depends(reviewer)):
        async with _db().connection() as conn:
            return JSONResponse({"suggestions": await list_mine(conn, user=user)})

    # ---- an admin decides suggested species (spec 2026-11-21, "Admin panel") ----
    # Approve and reject leave the review pool's in-memory counts alone: its next
    # refresh_progress reads the settled answers.

    @router.get("/api/review/species-suggestions")
    async def admin_suggestions(
        status: Literal["pending", "decided"] = Query("pending"), user=Depends(admin_only)
    ):
        async with _db().connection() as conn:
            listed = await list_for_admin(conn, status=status)
            pending = len(listed) if status == "pending" else await pending_count(conn)
        return JSONResponse({"suggestions": listed, "pending": pending})

    @router.get("/api/review/species-suggestions/{suggestion_id}/boxes")
    async def admin_suggestion_boxes(
        suggestion_id: int, page: int = Query(0, ge=0, le=100000), user=Depends(admin_only)
    ):
        async with _db().connection() as conn:
            return JSONResponse(
                await suggestion_boxes(conn, suggestion_id=suggestion_id, page=page)
            )

    @router.post(
        "/api/review/species-suggestions/{suggestion_id}/approve",
        dependencies=[Depends(require_csrf)],
    )
    async def approve_suggestion(suggestion_id: int, body: ApproveIn, user=Depends(admin_only)):
        async with _db().connection() as conn:
            try:
                return JSONResponse(
                    await approve(
                        conn,
                        user=user,
                        suggestion_id=suggestion_id,
                        body=body,
                        catalogue=await _catalogue(conn),
                    )
                )
            except ReviewError as e:
                return _fail(e)

    @router.post(
        "/api/review/species-suggestions/{suggestion_id}/reject",
        dependencies=[Depends(require_csrf)],
    )
    async def reject_suggestion(suggestion_id: int, body: RejectIn, user=Depends(admin_only)):
        async with _db().connection() as conn:
            try:
                return JSONResponse(
                    await reject(
                        conn,
                        user=user,
                        suggestion_id=suggestion_id,
                        body=body,
                        catalogue=await _catalogue(conn),
                    )
                )
            except ReviewError as e:
                return _fail(e)

    @router.post(
        "/api/review/species-suggestions/{suggestion_id}/detach",
        dependencies=[Depends(require_csrf)],
    )
    async def detach_box(suggestion_id: int, body: DetachIn, user=Depends(admin_only)):
        async with _db().connection() as conn:
            try:
                return JSONResponse(
                    await detach(
                        conn, user=user, suggestion_id=suggestion_id, event_id=body.event_id
                    )
                )
            except ReviewError as e:
                return _fail(e)

    @router.get("/api/review/species/{name_slug}/photo.jpg")
    async def species_photo(name_slug: str, user=Depends(reviewer)):
        # The file catalogue, not _catalogue(): approved suggestions carry no photos, so
        # merging them would only cost a database round trip per thumbnail (Ruling P1a).
        found = (await state["review_catalogue"].aget()).photo(name_slug)
        if found is None:
            return JSONResponse({"error": "no photo for that species"}, status_code=404)
        return await _jpeg(render_frame, str(found[0]), 320)

    @router.get("/api/review/frames/{saved_frame_id}/image.jpg")
    async def frame_image(
        saved_frame_id: int, size: int = Query(1280, ge=160, le=1920), user=Depends(reviewer)
    ):
        async with _db().connection() as conn:
            row = await fetch_one(
                conn, "SELECT image_path FROM saved_frames WHERE id = %s", (saved_frame_id,)
            )
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
        return await _jpeg(render_zoom, ev["image_path"], zoom_region(ev["bbox"], width, height))

    # ---- whole-frame mode (Task 8) ----

    async def _frame_call(fn, **kw):
        async with _db().connection() as conn:
            try:
                return JSONResponse(await fn(conn, catalogue=await _catalogue(conn), **kw))
            except ReviewError as e:
                return _fail(e)

    @router.post("/api/review/frames/{saved_frame_id}/open", dependencies=[Depends(require_csrf)])
    async def frame_open(saved_frame_id: int, user=Depends(reviewer)):
        async with _db().connection() as conn:
            try:
                # Read (and validate) the frame before claiming anything,
                # so a missing or unreadable frame leaves no claims behind.
                await frame_record(conn, saved_frame_id)
                await open_frame(
                    conn,
                    saved_frame_id=saved_frame_id,
                    user_id=user.id,
                    claim_s=cfg.review_claim_s,
                    skip_ttl_days=cfg.review_skip_ttl_days,
                )
                return JSONResponse(
                    await frame_state(
                        conn,
                        saved_frame_id=saved_frame_id,
                        user=user,
                        catalogue=await _catalogue(conn),
                        counts=_counts,
                        target=cfg.review_target,
                    )
                )
            except ReviewError as e:
                return _fail(e)

    @router.get("/api/review/frames/{saved_frame_id}")
    async def frame_get(saved_frame_id: int, user=Depends(reviewer)):
        return await _frame_call(
            frame_state,
            saved_frame_id=saved_frame_id,
            user=user,
            counts=_counts,
            target=cfg.review_target,
        )

    @router.post("/api/review/frames/{saved_frame_id}/boxes", dependencies=[Depends(require_csrf)])
    async def frame_add_box(saved_frame_id: int, body: AddedBoxIn, user=Depends(reviewer)):
        return await _frame_call(
            add_box,
            saved_frame_id=saved_frame_id,
            user=user,
            body=body,
        )

    @router.patch(
        "/api/review/frames/{saved_frame_id}/boxes/{box_id}", dependencies=[Depends(require_csrf)]
    )
    async def frame_update_box(
        saved_frame_id: int, box_id: int, body: AddedBoxIn, user=Depends(reviewer)
    ):
        return await _frame_call(
            update_box,
            saved_frame_id=saved_frame_id,
            box_id=box_id,
            user=user,
            body=body,
        )

    @router.delete(
        "/api/review/frames/{saved_frame_id}/boxes/{box_id}", dependencies=[Depends(require_csrf)]
    )
    async def frame_remove_box(saved_frame_id: int, box_id: int, user=Depends(reviewer)):
        async with _db().connection() as conn:
            try:
                await remove_box(conn, saved_frame_id=saved_frame_id, box_id=box_id, user=user)
            except ReviewError as e:
                return _fail(e)
        return JSONResponse({"ok": True})

    @router.post(
        "/api/review/frames/{saved_frame_id}/mark-rest", dependencies=[Depends(require_csrf)]
    )
    async def frame_mark_rest(saved_frame_id: int, body: MarkRestIn, user=Depends(reviewer)):
        async with _db().connection() as conn:
            try:
                out = await mark_rest(
                    conn,
                    saved_frame_id=saved_frame_id,
                    user=user,
                    body=body,
                    catalogue=await _catalogue(conn),
                    spot_rate=cfg.review_spotcheck_rate,
                    rng=rng,
                )
            except ReviewError as e:
                return _fail(e)
        pool_obj = _pool()
        if pool_obj:
            for v in out["verdicts"]:
                if v["first_pass"] and v["counts"] and not v["duplicate"]:
                    pool_obj.note_verified(v["label"])
        return JSONResponse({"marked": out["marked"], "skipped": out["skipped"]})

    @router.post(
        "/api/review/frames/{saved_frame_id}/complete", dependencies=[Depends(require_csrf)]
    )
    async def frame_complete(saved_frame_id: int, user=Depends(reviewer)):
        return await _frame_call(complete_frame, saved_frame_id=saved_frame_id, user=user)

    @router.post(
        "/api/review/frames/{saved_frame_id}/release", dependencies=[Depends(require_csrf)]
    )
    async def frame_release(saved_frame_id: int, user=Depends(reviewer)):
        async with _db().connection() as conn:
            await release_frame(conn, saved_frame_id=saved_frame_id, user_id=user.id)
        return JSONResponse({"ok": True})

    @router.post("/api/review/frames/{saved_frame_id}/skip", dependencies=[Depends(require_csrf)])
    async def frame_skip(saved_frame_id: int, user=Depends(reviewer)):
        async with _db().connection() as conn:
            await skip_frame(conn, saved_frame_id=saved_frame_id, user_id=user.id)
        return JSONResponse({"ok": True})

    @router.delete(
        "/api/review/frames/{saved_frame_id}/complete", dependencies=[Depends(require_csrf)]
    )
    async def frame_uncomplete(saved_frame_id: int, user=Depends(admin_only)):
        async with _db().connection() as conn:
            await uncomplete_frame(conn, saved_frame_id=saved_frame_id)
        return JSONResponse({"ok": True})

    @router.get("/api/review/added/{box_id}/zoom.jpg")
    async def added_zoom(box_id: int, user=Depends(reviewer)):
        async with _db().connection() as conn:
            row = await fetch_one(
                conn,
                """
                SELECT a.bbox, sf.image_path FROM review_added_boxes a
                  JOIN saved_frames sf ON sf.id = a.saved_frame_id
                 WHERE a.id = %s AND a.removed_at IS NULL""",
                (box_id,),
            )
        if row is None:
            return JSONResponse({"error": "no such drawn box"}, status_code=404)
        try:
            width, height = await asyncio.to_thread(frame_size, row["image_path"])
        except (OSError, ValueError):
            return JSONResponse({"error": "image file missing on disk"}, status_code=404)
        return await _jpeg(render_zoom, row["image_path"], zoom_region(row["bbox"], width, height))

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
                    conn,
                    pool_obj=pool_obj,
                    catalogue=await _catalogue(conn),
                    session_id=user.session_id,
                    target=cfg.review_target,
                )
        except psycopg.errors.QueryCanceled:
            # Unlike the refresh above, this is progress_summary's own
            # query timing out: there is no cached summary to fall back
            # to, so this is a clear 503, never a hang and never a 500
            # (fix round 1, finding 6).
            log.warning("review progress: progress_summary timed out")
            return JSONResponse(
                {"error": "progress is taking too long to compute; try again shortly"},
                status_code=503,
            )
        summary["stale"] = stale
        return JSONResponse(summary)

    @router.get("/api/review/browse")
    async def browse_boxes(
        status: str = "any",
        species: str | None = None,
        camera: str | None = None,
        who: str = "anyone",
        lane: str = "any",
        date_from: date | None = None,
        date_to: date | None = None,
        page: int = Query(0, ge=0, le=100000),
        user=Depends(reviewer),
    ):
        filters = BrowseFilters(
            status=status,
            species=species or None,
            camera=camera or None,
            who=who,
            lane=lane,
            date_from=date_from,
            date_to=date_to,
        )
        async with _db().connection() as conn:
            try:
                return JSONResponse(
                    await browse(
                        conn,
                        catalogue=await _catalogue(conn),
                        user=user,
                        filters=filters,
                        page=page,
                    )
                )
            except ReviewError as e:
                return _fail(e)
            except psycopg.errors.QueryCanceled:
                log.warning("review browse: query timed out")
                return JSONResponse(
                    {"error": "browse is taking too long to compute; try again shortly"},
                    status_code=503,
                )

    @router.get("/api/review/disputes")
    async def disputes(user=Depends(admin_only)):
        async with _db().connection() as conn:
            return JSONResponse(
                {"disputes": await list_disputes(conn, catalogue=await _catalogue(conn))}
            )

    @router.post("/api/review/disputes/{event_id}/adjudicate", dependencies=[Depends(require_csrf)])
    async def adjudicate(event_id: int, body: AdjudicateIn, user=Depends(admin_only)):
        verdict = VerdictIn(
            event_id=event_id,
            outcome=body.outcome,
            label=body.label,
            life_stage=body.life_stage,
            bbox=body.bbox,
        )
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
                return JSONResponse(
                    await submit_verdict(
                        conn,
                        user=user,
                        body=verdict,
                        catalogue=await _catalogue(conn),
                        spot_rate=0.0,
                        rng=rng,
                        adjudication=True,
                    )
                )
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

    return router

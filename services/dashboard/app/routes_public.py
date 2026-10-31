"""The public read-only API under /api/public/, which the public website calls.

The site gate leaves this prefix open (auth.OPEN_PREFIXES), so nothing here may
carry reviewer or personal data. Every answer goes out through `cached()`, so
CloudFront and browsers hold it for a few seconds instead of every visitor
reaching the dashboard. The SQL behind the detection, species and environment
answers is public_queries.py's, shared with the dashboard's own routes; the
downloads are the dashboard's own exports (exports.PUBLIC_EXPORTS) over a few
fixed windows, read from a server-side cursor, at most DOWNLOADS_AT_ONCE at a
time. Every route sits above the catch-all at the end of the router; a request
the API cannot read is a cached 422 too (public_validation_error)."""

from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import tempfile
from collections.abc import AsyncGenerator, Sequence
from datetime import datetime, time, timedelta
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

from . import public_queries as q
from .common_names import common as common_name
from .config import autoplay_embed
from .detection_images import annotate_frame, crop_roi
from .exports import PUBLIC_EXPORTS, public_query, stream_csv, write_parquet
from .frames import frame_exists
from .review_catalogue import normalize_raw, slug
from .review_images import render_frame

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


# The ids of the reef camera and the pier camera, as the dashboard and this API name them. A
# worker stamps its SOURCE_LABEL_PRIMARY on every detection (detection_events.source_name),
# and the launchers set it to the camera's id (deploy/gpu-host/boot.sh: seahivecam;
# scripts/dev/run_worker_pier.sh: pier_cam), so a camera's id is its source name. Any other
# source (an unlabelled worker stamps its video URL's host) has no public name.
CAMERAS = ("seahivecam", "pier_cam")
# A track of fewer frames is a flicker, not a sighting (/api/species_counts' default).
MIN_FRAMES = 3
# A species' recent detections: the newest saved ones of the last week, as /detections/recent
# answers them with its default confidence floor.
SPECIES_RECENT = 12
RECENT_HOURS_MAX = 168
MIN_CONFIDENCE = 0.5
IMMUTABLE = "public, max-age=86400, immutable"
# The environment history's windows (days) and resolutions (points): few, so CloudFront's cache
# keys stay few.
HISTORY_DAYS = (1, 7, 30, 90, 365, 800)
HISTORY_POINTS = (100, 200, 500)
# The downloads: where they are, how long a browser or CloudFront keeps one, and how many may
# run at once (each holds a database connection while it reads).
EXPORTS = "/api/public/exports"
DOWNLOAD_CACHE = "public, max-age=3600"
DOWNLOADS_AT_ONCE = 2

# The README the dashboard's own downloads carry (main.py, /api/export/README.md).
EXPORTS_README = Path(__file__).resolve().parent / "data" / "README.md"
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
# What the public copy of the README adds: how its files name a source.
README_NOTE = (
    "\n---\n\n## Files from the public API\n\n"
    "In the files downloaded from the public API, `source_name` is the id of one of the site's "
    "cameras, or empty for any other source.\n"
)


def _iso(ts: datetime | None) -> str | None:
    return ts.isoformat() if ts else None


def _reading(ts: datetime | None, row: dict, columns: tuple) -> dict:
    """A reading as the public sees it: its time and the feed's columns, and nothing about
    where it came from (the deployment is said once, the source not at all)."""
    return {"ts": _iso(ts), **{c: row[c] for c in columns}}


def _one_of(name: str, value: int, allowed: Sequence[int]) -> int:
    """`value` when it is one of `allowed`, else the 422 FastAPI gives a query parameter outside
    a Literal (which these cannot be typed as: a Literal of ints refuses the query's string)."""
    if value in allowed:
        return value
    expected = ", ".join(map(str, allowed[:-1])) + f" or {allowed[-1]}"
    raise RequestValidationError(
        [
            {
                "type": "literal_error",
                "loc": ("query", name),
                "msg": f"Input should be {expected}",
                "input": str(value),
                "ctx": {"expected": expected},
            }
        ]
    )


async def public_validation_error(request: Request, exc: RequestValidationError) -> Response:
    """The app's handler for a request it cannot read: under /api/public/, a 422 cached briefly
    like every other answer there; anywhere else, FastAPI's own."""
    if request.url.path.startswith("/api/public/"):
        detail = jsonable_encoder(exc.errors())
        return cached({"error": "invalid request", "detail": detail}, 10, status=422)
    return await request_validation_exception_handler(request, exc)


class _Primed:
    """`first`, then the rest of the async generator `rest`, whose first item it was."""

    def __init__(self, first: str, rest: AsyncGenerator[str, None]):
        self.first, self.rest = first, rest

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        if self.first is None:
            return await self.rest.__anext__()
        first, self.first = self.first, None
        return first

    async def aclose(self) -> None:
        await self.rest.aclose()


class _ClosingStream(StreamingResponse):
    """A streaming response that closes its body once sent, or once sending fails, in the
    request's own task: the stream's cleanup never waits for the garbage collector."""

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self.body_iterator.aclose()


class _TemporaryFile(FileResponse):
    """A file response whose file is deleted once sent, or once sending fails."""

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            Path(self.path).unlink(missing_ok=True)


def _public_readme() -> str | None:
    """The exports' README without the lines that name an e-mail address, and with
    README_NOTE; None when the file is missing."""
    try:
        text = EXPORTS_README.read_text()
    except OSError:
        return None
    kept = "".join(line for line in text.splitlines(keepends=True) if not EMAIL.search(line))
    return kept + README_NOTE


def _names(catalogue, raw: str | None) -> tuple[str | None, str | None]:
    """(latin, common) for a name the classifier or a verdict wrote: its WoRMS accepted name
    (a name the catalogue does not map passes through as given), and that name's common
    name (the curated list first, then the Species Log)."""
    if not raw:
        return None, None
    accepted = catalogue.accepted(raw)
    latin = raw if accepted == normalize_raw(raw) else accepted
    return latin, common_name(latin) or catalogue.common_name(latin)


def _detection(row: dict, catalogue, camera: str | None) -> dict:
    """A detection as the public sees it, on the camera named `camera`: the latest verdict's
    species when there is one (the model's own species_id when the verdict agrees with it),
    the model's score, and links to its crop and frame when its own frame was saved."""
    raw, species_id = row["best_name"], row["best_species_id"]
    if row["reviewed"]:
        raw = row["corrected_name"]
        agrees = raw is not None and catalogue.accepted(raw) == catalogue.accepted(row["best_name"])
        if not agrees:
            species_id = row["corrected_species_id"] or raw
    latin, common = _names(catalogue, raw)
    base = f"/api/public/detections/{row['id']}"
    return {
        "id": row["id"],
        "ts": _iso(row["ts"]),
        "camera": camera,
        "species_id": species_id,
        "latin": latin,
        "common": common,
        "confidence": row["best_accuracy"],
        "crop": f"{base}/crop.jpg" if row["image_path"] else None,
        "frame": f"{base}/frame.jpg" if row["image_path"] else None,
    }


def _photo(catalogue, species_id: str) -> tuple | None:
    """The catalogue's iNaturalist reference photo for a species: (path, attribution)."""
    return catalogue.photo(slug(catalogue.accepted(species_id)))


def _species(row: dict, week: int, catalogue) -> dict:
    latin, common = _names(catalogue, row["name"])
    taxon = catalogue.taxon(latin)
    photo = _photo(catalogue, row["species_id"])
    return {
        "species_id": row["species_id"],
        "latin": latin,
        "common": common,
        "family": taxon.family if taxon else None,
        "order": taxon.order if taxon else None,
        "class": taxon.class_ if taxon else None,
        "phylum": taxon.phylum if taxon else None,
        "aphia_id": taxon.aphia_id if taxon else None,
        "photo": {
            "url": f"/api/public/species/{quote(row['species_id'], safe='')}/photo.jpg",
            "attribution": photo[1],
        }
        if photo
        else None,
        "latest_crop": None,  # the caller's: it takes a query
        "sightings_total": row["sightings"],
        "sightings_7d": week,
        "first_seen": _iso(row["first_seen"]),
        "last_seen": _iso(row["last_seen"]),
    }


def _month_starts(tz: str, n: int = 12) -> list[datetime]:
    """The first instants of the last `n` calendar months in `tz`, oldest first."""
    zone = ZoneInfo(tz)
    today = datetime.now(zone).date()
    starts = []
    for back in range(n - 1, -1, -1):
        year, month = divmod(today.year * 12 + today.month - 1 - back, 12)
        starts.append(datetime(year, month + 1, 1, tzinfo=zone))
    return starts


def _render(draw, image_path: str, bbox: list[int], quality: int) -> bytes | None:
    """The JPEG of `draw(image_path, bbox)`; None when the frame is gone from storage."""
    if not frame_exists(image_path):
        return None
    buf = io.BytesIO()
    draw(image_path, bbox).save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def _crop(image_path: str, bbox: list[int]):
    return crop_roi(image_path, bbox, 0.10, 384)


def _frame(image_path: str, bbox: list[int]):
    # The box without a label: the model's name would contradict a volunteer's verdict.
    return annotate_frame(image_path, bbox, None, 1280)


def build_public_router(cfg, state) -> APIRouter:
    router = APIRouter(prefix="/api/public")

    embed_url = autoplay_embed(cfg.live_video_embed_url) or None
    stream_base = None if cfg.stream_base_url == GATED_STREAM_BASE else cfg.stream_base_url
    # Each camera of the site: (worker URL, whether the stream origin serves it, its embed).
    # The reef camera is always listed and is the site's one embed; the pier camera exists
    # only where it has a worker, and no stream origin serves it yet.
    reef, pier = CAMERAS
    cameras = {reef: (cfg.worker_url, stream_base is not None, embed_url)}
    if cfg.worker_url_pier:
        cameras[pier] = (cfg.worker_url_pier, False, None)

    def _camera(name: str | None) -> str | None:
        """A camera id for a detection's source_name, and the source_name for a camera id
        (they are the same name): None for a name that is not one of the site's cameras."""
        return name if name in cameras else None

    def _unknown_camera() -> JSONResponse:
        return cached({"error": "unknown camera"}, 10, status=404)

    async def _catalogue():
        return await state["review_catalogue"].aget()

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

    async def worker_dark(client: httpx.AsyncClient, camera: str, worker: str) -> bool | None:
        """Whether the worker's source reports it is dark, from one GET of its /stats
        (autoswitch.is_dark); None when it does not answer 200 within WORKER_TIMEOUT_S or
        the body has no boolean there."""
        try:
            r = await client.get(f"{worker}/stats", timeout=WORKER_TIMEOUT_S)
            if r.status_code == 200:
                source = r.json().get("autoswitch")
                dark = source.get("is_dark") if isinstance(source, dict) else None
                return dark if isinstance(dark, bool) else None
            log.debug("public api: the %s worker's stats answered %s", camera, r.status_code)
        except (httpx.HTTPError, ValueError, AttributeError) as e:
            log.debug("public api: the %s worker's stats did not answer: %r", camera, e)
        return None

    @router.get("/cameras")
    async def public_cameras() -> JSONResponse:
        """Each camera's status and the media a signed-out visitor can fetch: `live` only
        when its worker answers and the stream origin serves it; otherwise the embed, if
        the camera has one, or offline. The site names its cameras from its own content.

        `dark` is true or false as the camera worker reports about its own source (its
        /stats `autoswitch.is_dark`, from frame luminance) and null when that is not known:
        no worker, a /stats that fails or answers non-200, or a body without the field. It
        never changes `status`."""
        client: httpx.AsyncClient = state["client"]

        async def entry(camera: str, worker: str, on_origin: bool, embed: str | None) -> dict:
            media = bool(worker) and on_origin
            dark = None
            if worker:
                live, dark = await asyncio.gather(
                    worker_live(client, camera, worker), worker_dark(client, camera, worker)
                )
            if not worker:
                status = "embed"
            elif live is None:
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
                "dark": dark,
            }

        entries = await asyncio.gather(*(entry(cam, *spec) for cam, spec in cameras.items()))
        return cached({"cameras": list(entries)}, 10)

    @router.get("/live")
    async def public_live(camera: str = reef) -> JSONResponse:
        """The camera's current frame and its identified fish, for drawing boxes over the
        stream. bbox is [x1, y1, x2, y2] in frame pixels, the worker's own order. 404 for a
        camera the site does not have; 503 when there is no camera pipeline or its worker
        does not answer with a frame of the worker's shape."""
        if _camera(camera) is None:
            return _unknown_camera()
        worker = cameras[camera][0]
        if not worker:
            return cached({"error": "no camera pipeline"}, 10, status=503)
        client: httpx.AsyncClient = state["client"]
        frame = await worker_live(client, camera, worker)
        found = frame.get("detections") if isinstance(frame, dict) else None
        if not isinstance(found, list) or not all(_is_detection(d) for d in found):
            return cached({"error": "camera offline"}, 10, status=503)
        catalogue = await _catalogue()
        named = [(d, *_names(catalogue, d["best_name"])) for d in found if d.get("best_name")]
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
                        "latin": latin,
                        "common": common,
                    }
                    for d, latin, common in named
                ],
            },
            1,
        )

    def _pool():
        return state.get("pool")

    def _no_database() -> JSONResponse:
        return cached({"error": "database unavailable"}, 10, status=503)

    def _not_found() -> JSONResponse:
        return cached({"error": "not found"}, 10, status=404)

    @router.get("/detections/recent")
    async def public_recent_detections(
        camera_id: str = Query("", alias="camera"),
        hours: int = Query(24, ge=1, le=RECENT_HOURS_MAX),
        limit: int = Query(24, ge=1, le=100),
        min_confidence: float = Query(MIN_CONFIDENCE, ge=0.0, le=1.0),
    ) -> JSONResponse:
        """The newest identified fish, as a volunteer last named them; a box a volunteer
        called not a fish is left out. Every camera, or one."""
        source = _camera(camera_id)
        if camera_id and source is None:
            return _unknown_camera()
        pool = _pool()
        if pool is None:
            return _no_database()
        async with pool.connection() as conn:
            rows = await q.recent_detections(
                conn,
                hours=hours,
                limit=limit,
                min_confidence=min_confidence,
                source_name=source,
            )
        catalogue = await _catalogue()
        return cached(
            {"detections": [_detection(r, catalogue, _camera(r["source_name"])) for r in rows]}, 15
        )

    @router.get("/detections/summary")
    async def public_detections_summary(
        hours: int = Query(1, ge=1, le=RECENT_HOURS_MAX),
        camera_id: str = Query("", alias="camera"),
    ) -> JSONResponse:
        """Sightings per species in the last `hours`, most first: tracks of at least
        MIN_FRAMES frames whose last frame falls in the window, and that no volunteer's
        latest verdict calls not a fish."""
        source = _camera(camera_id)
        if camera_id and source is None:
            return _unknown_camera()
        pool = _pool()
        if pool is None:
            return _no_database()
        async with pool.connection() as conn:
            rows = await q.sighting_counts(
                conn, hours, MIN_FRAMES, source_name=source, limit=None, hide_not_a_fish=True
            )
        catalogue = await _catalogue()
        species = []
        for r in sorted(rows, key=lambda r: (-r["n"], r["species_id"])):
            latin, common = _names(catalogue, r["name"])
            species.append(
                {
                    "species_id": r["species_id"],
                    "latin": latin,
                    "common": common,
                    "count": r["n"],
                    "last_seen": _iso(r["last_seen"]),
                }
            )
        total = sum(s["count"] for s in species)
        return cached({"hours": hours, "total": total, "species": species}, 30)

    async def _saved_image(event_id: int, draw, quality: int) -> Response:
        """The detection's image drawn from the frame saved at its own timestamp (the private
        routes' rule); a cached 404 when that frame was never saved or is gone, or when the
        detection's latest visible verdict calls it not a fish."""
        pool = _pool()
        if pool is None:
            return _no_database()
        async with pool.connection() as conn:
            row = await q.detection_with_frame(conn, event_id, verdict=True)
        image_path = row["image_path"] if row and not row["not_a_fish"] else None
        bbox = (row["bbox"] if row else None) or []
        data = None
        if image_path and len(bbox) == 4:
            try:
                data = await asyncio.to_thread(_render, draw, image_path, bbox, quality)
            except (OSError, ValueError) as e:  # unreadable, or not an image
                log.warning("public api: no image for detection %s: %r", event_id, e)
        if data is None:
            return cached({"error": "no saved frame"}, 60, status=404)
        return Response(data, media_type="image/jpeg", headers={"Cache-Control": IMMUTABLE})

    @router.get("/detections/{event_id}/crop.jpg")
    async def public_detection_crop(event_id: int) -> Response:
        return await _saved_image(event_id, _crop, 85)

    @router.get("/detections/{event_id}/frame.jpg")
    async def public_detection_frame(event_id: int) -> Response:
        return await _saved_image(event_id, _frame, 82)

    async def _species_recent(conn, entry: dict, limit: int) -> list[dict]:
        """The species' newest detections with a saved frame (in the last RECENT_HOURS_MAX
        hours, scored at least MIN_CONFIDENCE, not called not a fish nor renamed)."""
        return await q.recent_detections(
            conn,
            hours=RECENT_HOURS_MAX,
            limit=limit,
            min_confidence=MIN_CONFIDENCE,
            species_id=entry["species_id"],
            species_names=[n for n in (entry["latin"], entry["species_id"]) if n],
            saved_only=True,
        )

    async def _species_entries(conn, catalogue, species_id: str | None = None) -> list[dict]:
        """Every species seen (or `species_id` alone), with lifetime and last-week sightings
        (not counting tracks a volunteer's latest verdict calls not a fish), its catalogue
        taxonomy and its latest crop; most seen first, then by name."""
        seen = {"min_frames": MIN_FRAMES, "species_id": species_id, "hide_not_a_fish": True}
        lifetime = await q.species_sightings(conn, **seen)
        week = await q.species_sightings(conn, hours=7 * 24, **seen)
        in_week = {r["species_id"]: r["sightings"] for r in week}
        entries = [_species(r, in_week.get(r["species_id"], 0), catalogue) for r in lifetime]
        for entry in entries:
            latest = await _species_recent(conn, entry, 1)
            if latest:
                entry["latest_crop"] = f"/api/public/detections/{latest[0]['id']}/crop.jpg"
        return sorted(
            entries, key=lambda e: (-e["sightings_total"], e["latin"] or "", e["species_id"])
        )

    @router.get("/species")
    async def public_species() -> JSONResponse:
        pool = _pool()
        if pool is None:
            return _no_database()
        catalogue = await _catalogue()
        async with pool.connection() as conn:
            entries = await _species_entries(conn, catalogue)
        return cached({"species": entries}, 3600)

    # Before /species/{species_id}, which would otherwise take "counts" for a species.
    @router.get("/species/counts")
    async def public_species_counts(
        days: int = Query(30, ge=1, le=400),
        granularity: str = Query("day", pattern="^(day|week)$"),
        top: int = Query(6, ge=1, le=8),
    ) -> JSONResponse:
        """Sightings per local day (or ISO week, labelled by its Monday) in the site's zone
        over the last `days` days, today included: the `top` species by total each with one
        count per bucket, and the rest summed as `other`."""
        pool = _pool()
        if pool is None:
            return _no_database()
        zone = ZoneInfo(cfg.tz)
        today = datetime.now(zone).date()
        first = today - timedelta(days=days - 1)
        if granularity == "week":
            monday = first - timedelta(days=first.weekday())
            buckets = [monday + timedelta(weeks=i) for i in range((today - monday).days // 7 + 1)]
        else:
            buckets = [first + timedelta(days=i) for i in range(days)]
        since = datetime.combine(first, time(), zone)
        async with pool.connection() as conn:
            rows = await q.sightings_by_bucket(
                conn, since, cfg.tz, MIN_FRAMES, week=granularity == "week"
            )
        index = {b: i for i, b in enumerate(buckets)}
        counts: dict[str, list[int]] = {}
        names: dict[str, str] = {}
        for r in rows:
            i = index.get(r["bucket"])
            if i is None:
                continue
            counts.setdefault(r["species_id"], [0] * len(buckets))[i] += r["n"]
            names[r["species_id"]] = r["name"]
        ranked = sorted(counts, key=lambda s: (-sum(counts[s]), s))
        catalogue = await _catalogue()
        series = []
        for species_id in ranked[:top]:
            latin, common = _names(catalogue, names[species_id])
            series.append(
                {
                    "species_id": species_id,
                    "latin": latin,
                    "common": common,
                    "counts": counts[species_id],
                }
            )
        other = [sum(counts[s][i] for s in ranked[top:]) for i in range(len(buckets))]
        return cached(
            {
                "granularity": granularity,
                "tz": cfg.tz,
                "buckets": [b.isoformat() for b in buckets],
                "series": series,
                "other": other,
            },
            3600,
        )

    @router.get("/species/{species_id}/photo.jpg")
    async def public_species_photo(species_id: str) -> Response:
        """The species' iNaturalist reference photo, which the species list credits."""
        found = _photo(await _catalogue(), species_id)
        data = None
        if found is not None:
            try:
                data = await asyncio.to_thread(render_frame, str(found[0]), 640)
            except (OSError, ValueError) as e:
                log.warning("public api: no photo for %s: %r", species_id, e)
        if data is None:
            return cached({"error": "no photo"}, 60, status=404)
        return Response(data, media_type="image/jpeg", headers={"Cache-Control": IMMUTABLE})

    @router.get("/species/{species_id}")
    async def public_species_profile(species_id: str) -> JSONResponse:
        """The species' list entry, its sightings by local hour of day and by month over the
        last twelve months (the site's zone), and its newest saved detections."""
        pool = _pool()
        if pool is None:
            return _no_database()
        catalogue = await _catalogue()
        months = _month_starts(cfg.tz)
        async with pool.connection() as conn:
            found = await _species_entries(conn, catalogue, species_id)
            if not found:
                return cached({"error": "unknown species"}, 60, status=404)
            [entry] = found
            by_hour = await q.sightings_by_hour_of_day(
                conn, species_id, cfg.tz, min_frames=MIN_FRAMES, hide_not_a_fish=True
            )
            by_month = await q.sightings_by_month(conn, species_id, months[0], cfg.tz, MIN_FRAMES)
            recent = await _species_recent(conn, entry, SPECIES_RECENT)
        labels = [m.strftime("%Y-%m") for m in months]
        return cached(
            {
                **entry,
                "by_hour": by_hour,
                "by_month": [{"month": m, "sightings": by_month.get(m, 0)} for m in labels],
                "recent": [_detection(r, catalogue, _camera(r["source_name"])) for r in recent],
            },
            3600,
        )

    deployment = cfg.deployment_uri

    @router.get("/environment/latest")
    async def public_environment_latest() -> JSONResponse:
        """The site's newest sonde reading and newest weather reading; null for a feed that
        has none."""
        pool = _pool()
        if pool is None:
            return _no_database()
        async with pool.connection() as conn:
            water = await q.latest_water(conn, deployment)
            weather = await q.latest_weather(conn, deployment)
        return cached(
            {
                "deployment": deployment,
                "water": _reading(water["ts"], water, q.SENSOR_COLUMNS) if water else None,
                "weather": _reading(weather["ts"], weather, q.WEATHER_COLUMNS) if weather else None,
            },
            60,
        )

    @router.get("/environment/history")
    async def public_environment_history(days: int = 7, max_points: int = 200) -> JSONResponse:
        """The site's readings of the last `days` days (one of HISTORY_DAYS), averaged into at
        most `max_points` (one of HISTORY_POINTS) even buckets, the dashboard's history rule;
        oldest first, each point its bucket's start."""
        days = _one_of("days", days, HISTORY_DAYS)
        max_points = _one_of("max_points", max_points, HISTORY_POINTS)
        pool = _pool()
        if pool is None:
            return _no_database()
        hours = days * 24
        bucket_s = q.history_bucket_seconds(hours, max_points)
        async with pool.connection() as conn:
            water = await q.water_by_bucket(conn, deployment, hours, bucket_s)
            weather = await q.weather_by_bucket(conn, deployment, hours, bucket_s)
        return cached(
            {
                "days": days,
                "water": [_reading(r["bucket"], r, q.SENSOR_COLUMNS) for r in water],
                "weather": [_reading(r["bucket"], r, q.WEATHER_COLUMNS) for r in weather],
            },
            600,
        )

    @router.get("/exports")
    async def public_exports() -> JSONResponse:
        """The downloads: each export's CSV and Parquet and the windows they may cover, the
        README, and the data's licence."""
        return cached(
            {
                "exports": [
                    {
                        "name": name,
                        "description": export.description,
                        "csv": f"{EXPORTS}/{name}.csv",
                        "parquet": f"{EXPORTS}/{name}.parquet",
                        "default_days": export.default_days,
                        "allowed_days": list(export.allowed_days),
                    }
                    for name, export in PUBLIC_EXPORTS.items()
                ],
                "readme": f"{EXPORTS}/README.md",
                # No document licenses this data yet (assets/LICENSES_AND_CREDITS.md covers
                # the classifier's training inputs): null until one does.
                "license": None,
            },
            3600,
        )

    def _download(filename: str) -> dict:
        return {
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": DOWNLOAD_CACHE,
        }

    @router.get("/exports/README.md")
    async def public_exports_readme() -> Response:
        text = _public_readme()
        if text is None:
            return cached({"error": "no README"}, 60, status=404)
        return Response(text, media_type="text/markdown", headers=_download("README.md"))

    # A source in the public files is a camera's id, or empty: a worker without a camera label
    # stamps its video URL's host as its source.
    public_values = {"source_name": lambda source: _camera(source) or ""}
    busy = state["public_downloads"] = asyncio.Semaphore(DOWNLOADS_AT_ONCE)

    def _busy() -> JSONResponse:
        return cached({"error": "busy"}, 10, status=503)

    def _download_query(name: str, fmt: str, days: int | None) -> tuple | None:
        """The public export's (sql, params, columns) over `days` (its default when None; a
        422 when not one of its windows); None for a name that is not a public export."""
        export = PUBLIC_EXPORTS.get(name)
        if export is None:
            return None
        days = _one_of("days", export.default_days if days is None else days, export.allowed_days)
        return public_query(
            name, fmt, hours=days * 24, deployment=deployment, cameras=list(cameras)
        )

    @router.get("/exports/{name}.csv")
    async def public_export_csv(name: str, days: int | None = None) -> Response:
        """One of PUBLIC_EXPORTS over the last `days` days, streamed as CSV from a server-side
        cursor; a cached 503 when DOWNLOADS_AT_ONCE are already running."""
        query = _download_query(name, "csv", days)
        if query is None:
            return _not_found()
        pool = _pool()
        if pool is None:
            return _no_database()
        if busy.locked():
            return _busy()
        await busy.acquire()
        lines = stream_csv(pool, *query, values=public_values, done=busy.release)
        # The query runs before the answer starts. From here the stream gives its place back
        # when it is closed: by the response once sent or failed, or, were the response never
        # sent, by the event loop's finalizer.
        header = await anext(lines)
        return _ClosingStream(
            _Primed(header, lines), media_type="text/csv", headers=_download(f"{name}.csv")
        )

    @router.get("/exports/{name}.parquet")
    async def public_export_parquet(name: str, days: int | None = None) -> Response:
        """One of PUBLIC_EXPORTS over the last `days` days as Parquet: written a row group at a
        time to a temporary file, which is sent and then deleted; a cached 503 when
        DOWNLOADS_AT_ONCE are already running."""
        query = _download_query(name, "parquet", days)
        if query is None:
            return _not_found()
        pool = _pool()
        if pool is None:
            return _no_database()
        if busy.locked():
            return _busy()
        await busy.acquire()
        try:
            fd, path = tempfile.mkstemp(prefix="public-export-", suffix=".parquet")
            os.close(fd)
            try:
                await write_parquet(pool, *query, path, values=public_values)
            except BaseException:
                Path(path).unlink(missing_ok=True)
                raise
        finally:
            busy.release()
        return _TemporaryFile(
            path, media_type="application/vnd.apache.parquet", headers=_download(f"{name}.parquet")
        )

    # Keep last: whatever no route above answers under the prefix is a cached 404 too.
    @router.get("/{rest:path}")
    async def public_not_found(rest: str) -> JSONResponse:
        return _not_found()

    return router

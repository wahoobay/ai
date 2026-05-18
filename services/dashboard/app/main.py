"""Wahoo Bay dashboard.

- Proxies the worker's MJPEG stream (so the browser only talks to one origin).
- Serves a small HTML page with live overlay + species counts + recent events.
- Queries Postgres directly for detection history.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import httpx
import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from PIL import Image
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, Field

from .alerts import SLO_RULES, SLOChecker
from .common_names import common as common_name
from .exports import (
    export_alerts,
    export_corrections,
    export_events,
    export_frame_stats,
    export_hourly_summary,
    export_labeled_corrections,
    export_saved_frames,
    export_sightings,
    export_species_counts,
    export_topk_long,
    export_tracks_timeline,
    export_water_quality,
    export_weather,
    fetch_parquet_bytes,
    query_for,
)
from .common_names import reload_manifest_names
from .matviews import MatviewRefresher
from .sheet_poller import SheetPoller
from .stats import (
    rate_ci,
    species_counts_with_ci,
)


class CorrectionIn(BaseModel):
    event_id: int
    corrected_name: str | None = None
    corrected_species_id: str | None = None
    not_a_fish: bool = False
    is_endorsement: bool = False  # reviewer hit "Looks right" — corrected_name == best_name
    corrected_bbox: list[int] | None = None  # [x1,y1,x2,y2] in source pixels if reviewer redrew
    confidence: str = Field("probable", pattern="^(certain|probable|uncertain)$")
    reviewer: str | None = None
    notes: str | None = None
    # Optional reviewer-assigned celebrity id — slug from assets/celebrities.json
    # (Bobby, Marty, Stumpy, …). Most detections are anonymous, so this is
    # usually None; when set, the per-celebrity gallery / sightings count
    # picks the detection up.
    celebrity_id: str | None = None

log = logging.getLogger("wahoobay.dashboard")

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _crop_roi(
    image_path: Path,
    bbox: list[int],
    pad_frac: float,
    max_edge: int,
) -> Image.Image:
    """Crop a detection bbox out of a source frame with a fractional pad.

    Synchronous; run in a threadpool from the route handler. Bbox is
    [x1, y1, x2, y2] in source pixel coordinates. Pad expands the crop by
    `pad_frac * max(width, height)` on each side, clamped to image bounds.
    Output is downscaled so the longer edge is at most `max_edge` px.
    """
    x1, y1, x2, y2 = bbox
    with Image.open(image_path) as src:
        src.load()
        W, H = src.size
        w = max(1, x2 - x1)
        h = max(1, y2 - y1)
        pad = int(round(max(w, h) * pad_frac))
        cx1 = max(0, x1 - pad)
        cy1 = max(0, y1 - pad)
        cx2 = min(W, x2 + pad)
        cy2 = min(H, y2 + pad)
        roi = src.crop((cx1, cy1, cx2, cy2)).convert("RGB")
    rw, rh = roi.size
    longest = max(rw, rh)
    if longest > max_edge:
        scale = max_edge / longest
        roi = roi.resize((max(1, int(rw * scale)), max(1, int(rh * scale))), Image.LANCZOS)
    return roi


def _resize_frame(image_path: Path, max_edge: int) -> Image.Image:
    """Return the source frame downscaled so the longest edge is at most
    ``max_edge`` pixels. No bbox is drawn. Used by the reviewer modal's
    edit-bbox mode — the SVG overlay handles the bbox interactively, so
    the underlying image must be unannotated.
    """
    with Image.open(image_path) as src:
        src.load()
        frame = src.convert("RGB").copy()
    W, H = frame.size
    if max(W, H) > max_edge:
        scale = max_edge / max(W, H)
        frame = frame.resize((max(1, int(W * scale)), max(1, int(H * scale))), Image.LANCZOS)
    return frame


def _annotate_frame(
    image_path: Path,
    bbox: list[int],
    label: str | None,
    max_edge: int,
) -> Image.Image:
    """Draw the detection bbox on the full source frame for context view.

    Lets a reviewer see the scene around the fish — water clarity, other
    fish, depth — alongside the cropped ROI. Stroke + label use the
    dashboard's accent color.
    """
    from PIL import ImageDraw, ImageFont
    x1, y1, x2, y2 = bbox
    with Image.open(image_path) as src:
        src.load()
        frame = src.convert("RGB").copy()
    W, H = frame.size
    if max(W, H) > max_edge:
        scale = max_edge / max(W, H)
        nw, nh = max(1, int(W * scale)), max(1, int(H * scale))
        frame = frame.resize((nw, nh), Image.LANCZOS)
        x1, y1, x2, y2 = (int(c * scale) for c in (x1, y1, x2, y2))
    draw = ImageDraw.Draw(frame)
    stroke_color = (79, 193, 255)  # dashboard accent
    stroke_width = max(2, int(max(frame.size) / 400))
    for inset in range(stroke_width):
        draw.rectangle(
            [(x1 - inset, y1 - inset), (x2 + inset, y2 + inset)],
            outline=stroke_color,
        )
    if label:
        try:
            font = ImageFont.load_default(size=max(12, int(max(frame.size) / 70)))
        except (TypeError, OSError):
            font = ImageFont.load_default()
        bbox_txt = draw.textbbox((0, 0), label, font=font)
        tw = bbox_txt[2] - bbox_txt[0]
        th = bbox_txt[3] - bbox_txt[1]
        pad = 4
        ly1 = max(0, y1 - th - 2 * pad)
        draw.rectangle(
            [(x1, ly1), (x1 + tw + 2 * pad, ly1 + th + 2 * pad)],
            fill=stroke_color,
        )
        draw.text((x1 + pad, ly1 + pad), label, fill=(11, 26, 36), font=font)
    return frame


def _env(key: str, default: str) -> str:
    return os.environ.get(key, default)


class DashboardCfg:
    def __init__(self) -> None:
        self.worker_url = _env("WORKER_URL", "http://localhost:8081").rstrip("/")
        # Pier-cam worker — same code, different camera + GPU. Optional; if
        # WORKER_URL_PIER is empty the dashboard's /api/stream_pier.mjpeg
        # etc. return 503 and the camera-toggle UI falls back to SEAHIVECAM.
        self.worker_url_pier = _env("WORKER_URL_PIER", "").rstrip("/")
        self.poller_url = _env("POLLER_URL", "http://localhost:8082").rstrip("/")
        self.database_url = _env(
            "DATABASE_URL",
            "postgresql://wahoobay:wahoobay@localhost:5432/wahoobay",
        )
        self.host = _env("DASHBOARD_HOST", "0.0.0.0")
        self.port = int(_env("DASHBOARD_PORT", "8080"))
        self.log_level = _env("LOG_LEVEL", "INFO")
        # If set, mutation endpoints require Authorization: Bearer <token>.
        # Read endpoints stay open (so a public tunnel exposes view-only by
        # default and you only hand the token to people who should be able
        # to submit corrections / acknowledge alerts).
        self.write_token = _env("DASHBOARD_WRITE_TOKEN", "").strip()
        # How often to refresh species_sightings_mat. 60 s gives the dashboard
        # near-real-time data; raise on a busy DB to reduce refresh cost.
        self.matview_refresh_interval_s = float(_env("MATVIEW_REFRESH_INTERVAL_S", "60"))
        # Per-camera no-fish-zones config. Read by the admin endpoint that
        # serves the regions-drawing UI, and written by its POST companion.
        # The worker reads the same file at subprocess startup and uses it
        # to drop false-positive detections on stationary reef geometry.
        self.negative_regions_path = _env(
            "NEGATIVE_REGIONS_PATH",
            "/raid/scratch/dzimmerman2021/wahoobay/data/camera_negative_regions.json",
        )
        # Google Sheet polling — refreshes assets/species.xlsx +
        # downstream JSON catalogues on a schedule. Default URL points
        # at Wahoo Bay's "Species + Fish named by Chatters" sheet
        # (shared "anyone with link" so no auth needed). Setting
        # SHEET_URL to an empty string disables the poller entirely.
        self.sheet_url = _env(
            "SHEET_URL",
            "https://docs.google.com/spreadsheets/d/1Git0Chs3pnEDngCVfDJmQ7noGM90h7uCu3AKecNgiOQ/export?format=xlsx",
        )
        self.sheet_poll_interval_s = float(_env("SHEET_POLL_INTERVAL_HOURS", "24")) * 3600.0


def build_app() -> FastAPI:
    cfg = DashboardCfg()

    state: dict = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=None))
        state["client"] = client
        try:
            pool = AsyncConnectionPool(cfg.database_url, min_size=1, max_size=4, open=False)
            await pool.open()
            state["pool"] = pool
            log.info("dashboard: postgres pool open")
        except Exception as e:
            log.warning("dashboard: postgres unavailable (%s); history endpoints will 503", e)
            state["pool"] = None

        # Load the celebrities catalogue once at startup. Small file
        # (~25 named animals) sourced from the "Fish named by Chatters"
        # sheet via scripts/build_celebrities.py. Indexed by slug so
        # the correction POST + the GET endpoints can look up O(1).
        try:
            celeb_doc = json.loads((BASE_DIR.parent.parent.parent / "assets" / "celebrities.json").read_text())
            celebs = celeb_doc.get("celebrities") or []
            state["celebrities"] = celebs
            state["celebrities_by_id"] = {c["id"]: c for c in celebs}
            log.info("dashboard: celebrities catalogue loaded (%d named individuals)", len(celebs))
        except FileNotFoundError:
            state["celebrities"] = []
            state["celebrities_by_id"] = {}
            log.info("dashboard: assets/celebrities.json not present; celebrities feature inert")
        except Exception as e:
            state["celebrities"] = []
            state["celebrities_by_id"] = {}
            log.warning("dashboard: failed to load celebrities.json (%s); feature inert", e)

        # SLO checker runs as a background task and upserts rows into `alerts`.
        # Reusable hook: re-read the celebrities catalogue + manifest
        # common-name fallback layer from disk. Called by the sheet
        # poller after a successful build-script run, and by the
        # /api/admin/refresh_sheets endpoint when invoked manually.
        async def _reload_catalogues() -> dict:
            stats: dict = {"celebrities": 0, "manifest_names": 0}
            try:
                doc = json.loads(
                    (BASE_DIR.parent.parent.parent / "assets" / "celebrities.json").read_text()
                )
                celebs = doc.get("celebrities") or []
                state["celebrities"] = celebs
                state["celebrities_by_id"] = {c["id"]: c for c in celebs}
                stats["celebrities"] = len(celebs)
            except Exception as e:
                log.warning("reload_catalogues: celebrities reload failed (%s)", e)
            try:
                stats["manifest_names"] = reload_manifest_names()
            except Exception as e:
                log.warning("reload_catalogues: manifest_names reload failed (%s)", e)
            log.info(
                "reload_catalogues: celebrities=%d, manifest_names=%d",
                stats["celebrities"], stats["manifest_names"],
            )
            return stats

        state["reload_catalogues"] = _reload_catalogues

        slo_task: asyncio.Task | None = None
        matview_task: asyncio.Task | None = None
        sheet_task: asyncio.Task | None = None
        if state.get("pool") is not None:
            checker = SLOChecker(
                pool=state["pool"],
                worker_url=cfg.worker_url,
                poller_url=cfg.poller_url,
                http_client=client,
            )
            state["slo"] = checker
            slo_task = asyncio.create_task(checker.run_forever(interval_s=30.0), name="slo-checker")

            # Keep species_sightings_mat fresh so user-facing endpoints
            # don't seq-scan detection_events on every request.
            refresher = MatviewRefresher(
                pool=state["pool"],
                # Order matters: species_sightings_hourly_mat reads from
                # species_sightings_mat, so the parent must refresh first.
                names=(
                    "species_sightings_mat",
                    "species_sightings_hourly_mat",
                ),
                interval_s=cfg.matview_refresh_interval_s,
            )
            state["matviews"] = refresher
            matview_task = asyncio.create_task(refresher.run_forever(), name="matview-refresher")

        # Google Sheet poller. Refreshes assets/species.xlsx + the
        # downstream JSON catalogues on the configured cadence. The
        # poller runs even without a DB pool (so manual /api/admin/
        # refresh_sheets still works in DB-degraded mode).
        sheet_poller: SheetPoller | None = None
        if cfg.sheet_url and cfg.sheet_poll_interval_s > 0:
            sheet_poller = SheetPoller(
                sheet_url=cfg.sheet_url,
                xlsx_path=BASE_DIR.parent.parent.parent / "assets" / "species.xlsx",
                repo_root=BASE_DIR.parent.parent.parent,
                interval_s=cfg.sheet_poll_interval_s,
                http_client=client,
                refresh_fn=lambda: _reload_catalogues(),
            )
            state["sheet_poller"] = sheet_poller
            sheet_task = asyncio.create_task(sheet_poller.run_forever(), name="sheet-poller")
            log.info(
                "sheet poller: scheduled every %.1f h (url=%s)",
                cfg.sheet_poll_interval_s / 3600, cfg.sheet_url,
            )
        else:
            log.info("sheet poller: disabled (SHEET_URL empty or interval=0)")

        try:
            yield
        finally:
            for t in (slo_task, matview_task, sheet_task):
                if t is not None:
                    t.cancel()
                    try:
                        await t
                    except (asyncio.CancelledError, Exception):
                        pass
            await client.aclose()
            if state.get("pool") is not None:
                await state["pool"].close()

    app = FastAPI(title="Wahoo Bay dashboard", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

    def require_write_token(authorization: str | None = Header(None)) -> None:
        """Gate mutation endpoints on a static bearer token if one is configured."""
        if not cfg.write_token:
            return  # open mode (no token configured)
        expected = f"Bearer {cfg.write_token}"
        if authorization != expected:
            raise HTTPException(status_code=401, detail="invalid or missing bearer token")

    @app.get("/api/auth/mode")
    async def auth_mode() -> dict:
        """Tells the UI whether mutation endpoints require a token."""
        return {"write_protected": bool(cfg.write_token)}

    # Cache-bust static assets so a new dashboard build doesn't get hidden
    # behind a stale browser cache. We hash the JS+CSS+template mtimes at
    # request time and append it to <link>/<script> URLs.
    def _asset_version() -> str:
        from hashlib import md5
        h = md5()
        for p in (
            BASE_DIR / "static" / "app.js",
            BASE_DIR / "static" / "style.css",
            BASE_DIR / "templates" / "index.html",
        ):
            try:
                h.update(str(p.stat().st_mtime_ns).encode())
            except Exception:
                pass
        return h.hexdigest()[:10]

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(
            request, "index.html",
            {"worker_url": cfg.worker_url, "asset_version": _asset_version()},
        )

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True}

    @app.get("/api/live.json")
    async def live_json() -> Response:
        client: httpx.AsyncClient = state["client"]
        try:
            r = await client.get(f"{cfg.worker_url}/live.json")
            if r.status_code != 200:
                return Response(content=r.content, media_type="application/json", status_code=r.status_code)
            try:
                data = r.json()
            except Exception:
                return Response(content=r.content, media_type="application/json")
            # Enrich with common names so the dashboard's Current detections
            # panel can show "Sergeant Major (A. saxatilis)" without a
            # client-side lookup table. Same enrichment is applied to each
            # top-K candidate so the secondary picks render in the same
            # canonical format.
            for d in data.get("detections", []) or []:
                latin = d.get("best_name")
                if latin:
                    d["best_common"] = common_name(latin)
                for p in d.get("topk") or []:
                    if isinstance(p, dict) and p.get("name"):
                        p["common"] = common_name(p["name"])
            return JSONResponse(data)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=502)

    @app.get("/api/stats")
    async def stats() -> Response:
        client: httpx.AsyncClient = state["client"]
        try:
            r = await client.get(f"{cfg.worker_url}/stats")
            return Response(content=r.content, media_type="application/json", status_code=r.status_code)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=502)

    def _proxy_mjpeg(worker_path: str, base_url: str | None = None) -> StreamingResponse:
        client: httpx.AsyncClient = state["client"]
        base = (base_url or cfg.worker_url).rstrip("/")

        async def gen() -> AsyncIterator[bytes]:
            async with client.stream("GET", f"{base}{worker_path}") as r:
                async for chunk in r.aiter_raw():
                    yield chunk

        return StreamingResponse(
            gen(),
            media_type="multipart/x-mixed-replace; boundary=wahoobay-mjpeg-boundary",
            headers={"Cache-Control": "no-cache, no-store, must-revalidate, private"},
        )

    def _no_pier_response() -> Response:
        return JSONResponse(
            {"error": "pier-cam worker not configured (WORKER_URL_PIER unset)"},
            status_code=503,
        )

    @app.get("/api/stream.mjpeg")
    async def stream() -> Response:
        return _proxy_mjpeg("/stream.mjpeg")

    @app.get("/api/stream_raw.mjpeg")
    async def stream_raw() -> Response:
        return _proxy_mjpeg("/stream_raw.mjpeg")

    @app.get("/api/stream_pier.mjpeg")
    async def stream_pier() -> Response:
        if not cfg.worker_url_pier:
            return _no_pier_response()
        return _proxy_mjpeg("/stream.mjpeg", cfg.worker_url_pier)

    @app.get("/api/stream_pier_raw.mjpeg")
    async def stream_pier_raw() -> Response:
        if not cfg.worker_url_pier:
            return _no_pier_response()
        return _proxy_mjpeg("/stream_raw.mjpeg", cfg.worker_url_pier)

    @app.get("/api/snapshot.jpg")
    async def snapshot(raw: int = 0) -> Response:
        """Single-frame snapshot from the primary worker. `raw=1` returns
        the bare camera frame (no bbox overlay) — used by the
        negative-regions admin UI so polygons get drawn against a clean
        image."""
        client: httpx.AsyncClient = state["client"]
        worker_path = "/snapshot_raw.jpg" if raw else "/snapshot.jpg"
        try:
            r = await client.get(f"{cfg.worker_url}{worker_path}")
            return Response(content=r.content, media_type="image/jpeg", status_code=r.status_code)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=502)

    @app.get("/api/snapshot_pier.jpg")
    async def snapshot_pier(raw: int = 0) -> Response:
        if not cfg.worker_url_pier:
            return _no_pier_response()
        client: httpx.AsyncClient = state["client"]
        worker_path = "/snapshot_raw.jpg" if raw else "/snapshot.jpg"
        try:
            r = await client.get(f"{cfg.worker_url_pier}{worker_path}")
            return Response(content=r.content, media_type="image/jpeg", status_code=r.status_code)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=502)

    @app.get("/api/live_pier.json")
    async def live_pier_json() -> Response:
        if not cfg.worker_url_pier:
            return _no_pier_response()
        client: httpx.AsyncClient = state["client"]
        try:
            r = await client.get(f"{cfg.worker_url_pier}/live.json")
            if r.status_code != 200:
                return Response(content=r.content, media_type="application/json", status_code=r.status_code)
            try:
                data = r.json()
            except Exception:
                return Response(content=r.content, media_type="application/json")
            for d in data.get("detections", []) or []:
                latin = d.get("best_name")
                if latin:
                    d["best_common"] = common_name(latin)
            return JSONResponse(data)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=502)

    @app.get("/api/stats_pier")
    async def stats_pier() -> Response:
        if not cfg.worker_url_pier:
            return _no_pier_response()
        client: httpx.AsyncClient = state["client"]
        try:
            r = await client.get(f"{cfg.worker_url_pier}/stats")
            return Response(content=r.content, media_type="application/json", status_code=r.status_code)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=502)

    @app.get("/api/cameras")
    async def cameras_list() -> Response:
        """List of configured cameras the dashboard can switch between.
        Drives the camera-toggle UI on the live stream."""
        cams = [{
            "id": "seahivecam",
            "label": "SEAHIVECAM",
            "stream": "/api/stream.mjpeg",
            "stream_raw": "/api/stream_raw.mjpeg",
            "snapshot": "/api/snapshot.jpg",
            "live_json": "/api/live.json",
            "stats": "/api/stats",
        }]
        if cfg.worker_url_pier:
            cams.append({
                "id": "pier_cam",
                "label": "Pier",
                "stream": "/api/stream_pier.mjpeg",
                "stream_raw": "/api/stream_pier_raw.mjpeg",
                "snapshot": "/api/snapshot_pier.jpg",
                "live_json": "/api/live_pier.json",
                "stats": "/api/stats_pier",
            })
        return JSONResponse({"cameras": cams})

    @app.get("/api/events")
    async def events(
        limit: int = Query(50, ge=1, le=500),
        species_id: str | None = None,
        min_accuracy: float = Query(0.0, ge=0.0, le=1.0),
        include_reviewed: bool = Query(False),
    ) -> Response:
        pool: AsyncConnectionPool | None = state.get("pool")
        if pool is None:
            return JSONResponse({"error": "database unavailable"}, status_code=503)
        # Each predicate is already fully qualified with its table alias
        # (de.*) — no string-mangling on the join side.
        where = ["de.best_name IS NOT NULL", "de.best_accuracy >= %s"]
        params: list = [min_accuracy]
        if species_id:
            where.append("de.best_species_id = %s")
            params.append(species_id)
        # Hide events a reviewer already corrected or endorsed — once anyone
        # has acted on a detection it shouldn't keep clogging the queue.
        # Pass include_reviewed=true to see the full stream (e.g. for audits).
        if not include_reviewed:
            where.append(
                "NOT EXISTS (SELECT 1 FROM detection_corrections dc "
                "WHERE dc.event_id = de.id)"
            )
        params.append(limit)
        # detection_events.image_path is legacy / always NULL — the worker
        # writes interesting frames to saved_frames (keyed by frame_id +
        # source_name). Use a correlated subquery instead of a JOIN: the LIMIT
        # caps how many lookups fire, the saved_frames index on
        # (frame_id, source_name) makes each one O(log N), and we avoid the
        # duplicate-event-row problem when saved_frames has multiple rows for
        # the same key (which happens — same frame can be saved under several
        # reasons). Newest matching saved_frame wins.
        where_q = " AND ".join(where)
        sql = f"""
            SELECT de.id, de.ts, de.frame_id, de.source_name, de.det_conf,
                   de.bbox, de.topk, de.best_name, de.best_species_id,
                   de.best_accuracy,
                   COALESCE(
                     de.image_path,
                     (SELECT image_path FROM saved_frames sf
                       WHERE sf.frame_id = de.frame_id
                         AND sf.source_name = de.source_name
                       ORDER BY sf.ts DESC LIMIT 1)
                   ) AS image_path
              FROM detection_events de
             WHERE {where_q}
             ORDER BY de.ts DESC
             LIMIT %s
        """
        async with pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(sql, params)
                rows = await cur.fetchall()
        for r in rows:
            r["ts"] = r["ts"].isoformat() if r["ts"] else None
            # Enrich with common names so the dashboard's recent-events
            # list + correction modal can show "Sergeant Major (A. saxatilis)"
            # without a client-side lookup. Best_common is None for any
            # species not in common_names.py — the renderer falls back to
            # italic-Latin alone.
            r["best_common"] = common_name(r["best_name"]) if r.get("best_name") else None
            for p in r.get("topk") or []:
                if isinstance(p, dict) and p.get("name"):
                    p["common"] = common_name(p["name"])
        return JSONResponse(rows)

    @app.get("/api/detections/{event_id}/roi.jpg")
    async def detection_roi(
        event_id: int,
        size: int = Query(384, ge=64, le=1024),
        pad: float = Query(0.10, ge=0.0, le=0.5),
    ) -> Response:
        """Return the cropped bbox region for a detection as a JPEG.

        Reviewers see the actual ROI rather than just a text label when
        deciding whether to accept / correct a prediction. ``pad`` adds a
        small fractional margin around the bbox so the fish isn't flush
        against the crop edge. ``size`` caps the longer edge of the output.
        """
        pool: AsyncConnectionPool | None = state.get("pool")
        if pool is None:
            return JSONResponse({"error": "database unavailable"}, status_code=503)
        async with pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT de.bbox,
                           COALESCE(
                             de.image_path,
                             (SELECT image_path FROM saved_frames sf
                               WHERE sf.frame_id = de.frame_id
                                 AND sf.source_name = de.source_name
                               ORDER BY sf.ts DESC LIMIT 1)
                           ) AS image_path
                      FROM detection_events de
                     WHERE de.id = %s
                    """,
                    (event_id,),
                )
                row = await cur.fetchone()
        if row is None:
            return JSONResponse({"error": "event not found"}, status_code=404)
        image_path = row.get("image_path")
        bbox = row.get("bbox") or []
        if not image_path or len(bbox) != 4:
            return JSONResponse(
                {"error": "no frame saved for this detection"}, status_code=404,
            )
        path = Path(image_path)
        if not path.exists():
            return JSONResponse({"error": "frame file missing on disk"}, status_code=404)
        try:
            img = await asyncio.get_running_loop().run_in_executor(
                None, _crop_roi, path, bbox, pad, size,
            )
        except Exception as e:
            log.warning("ROI crop failed for event %s: %s", event_id, e)
            return JSONResponse({"error": f"crop failed: {e}"}, status_code=500)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85, optimize=True)
        return Response(
            content=buf.getvalue(),
            media_type="image/jpeg",
            headers={
                # ROI is immutable for an event_id (bbox + frame don't change)
                "Cache-Control": "public, max-age=86400, immutable",
            },
        )

    @app.get("/api/detections/{event_id}/frame.jpg")
    async def detection_frame(
        event_id: int,
        size: int = Query(1280, ge=320, le=4096),
        annotate: bool = Query(True),
    ) -> Response:
        """Return the full source frame, optionally with the detection bbox
        drawn on it.

        Default (annotate=True): bbox drawn in the dashboard accent colour
        with the species label above it, for the modal's read-only context
        view.

        annotate=False: raw frame with no overlays. The reviewer's modal
        uses this when entering bbox-edit mode so the SVG overlay can
        manipulate the box on a clean background.
        """
        pool: AsyncConnectionPool | None = state.get("pool")
        if pool is None:
            return JSONResponse({"error": "database unavailable"}, status_code=503)
        async with pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT de.bbox, de.best_name, de.best_accuracy,
                           COALESCE(
                             de.image_path,
                             (SELECT image_path FROM saved_frames sf
                               WHERE sf.frame_id = de.frame_id
                                 AND sf.source_name = de.source_name
                               ORDER BY sf.ts DESC LIMIT 1)
                           ) AS image_path
                      FROM detection_events de
                     WHERE de.id = %s
                    """,
                    (event_id,),
                )
                row = await cur.fetchone()
        if row is None:
            return JSONResponse({"error": "event not found"}, status_code=404)
        image_path = row.get("image_path")
        bbox = row.get("bbox") or []
        if not image_path or len(bbox) != 4:
            return JSONResponse(
                {"error": "no frame saved for this detection"}, status_code=404,
            )
        path = Path(image_path)
        if not path.exists():
            return JSONResponse({"error": "frame file missing on disk"}, status_code=404)
        label_parts = []
        if row.get("best_name"):
            label_parts.append(str(row["best_name"]))
        if row.get("best_accuracy") is not None:
            label_parts.append(f"{float(row['best_accuracy']) * 100:.0f}%")
        label = " · ".join(label_parts) if label_parts else None
        try:
            if annotate:
                img = await asyncio.get_running_loop().run_in_executor(
                    None, _annotate_frame, path, bbox, label, size,
                )
            else:
                img = await asyncio.get_running_loop().run_in_executor(
                    None, _resize_frame, path, size,
                )
        except Exception as e:
            log.warning("frame annotate failed for event %s: %s", event_id, e)
            return JSONResponse({"error": f"annotate failed: {e}"}, status_code=500)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=82, optimize=True)
        return Response(
            content=buf.getvalue(),
            media_type="image/jpeg",
            headers={"Cache-Control": "public, max-age=86400, immutable"},
        )

    @app.get("/api/species_counts")
    async def species_counts(
        hours: int = Query(24, ge=1, le=24 * 14),
        mode: str = Query("sightings", pattern="^(sightings|events)$"),
        min_frames: int = Query(3, ge=1, le=1000),
    ) -> Response:
        """
        mode=sightings (default): one row per persistent track, weighted-vote
            species over the track's lifetime. ``min_frames`` filters out
            tracks that only existed for a brief flicker (default 3 frames).
            This is the count you actually want for "how many fish did we see".
        mode=events: every detection counts independently — the old behaviour.
            Useful for low-level debugging / event-rate monitoring.
        """
        pool: AsyncConnectionPool | None = state.get("pool")
        if pool is None:
            return JSONResponse({"error": "database unavailable"}, status_code=503)
        if mode == "sightings":
            sql = """
                SELECT species_id, name,
                       count(*)::int                AS n,
                       avg(mean_accuracy)::real     AS mean_acc,
                       max(last_seen)               AS last_seen,
                       sum(frame_count)::int        AS total_frames,
                       avg(frame_count)::real       AS mean_frames_per_track
                  FROM species_sightings_mat
                 WHERE last_seen >= NOW() - (%s::int || ' hours')::interval
                   AND frame_count >= %s
                 GROUP BY 1, 2
                 ORDER BY n DESC
                 LIMIT 50
            """
            params = (hours, min_frames)
        else:
            sql = """
                SELECT best_species_id AS species_id,
                       best_name       AS name,
                       count(*)::int   AS n,
                       avg(best_accuracy)::real AS mean_acc,
                       max(ts)         AS last_seen
                  FROM detection_events
                 WHERE ts >= NOW() - (%s::int || ' hours')::interval
                   AND best_species_id IS NOT NULL
                 GROUP BY 1, 2
                 ORDER BY n DESC
                 LIMIT 50
            """
            params = (hours,)
        async with pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(sql, params)
                rows = await cur.fetchall()
        for r in rows:
            r["last_seen"] = r["last_seen"].isoformat() if r["last_seen"] else None
            r["common"] = common_name(r["name"]) if r.get("name") else None
        return JSONResponse({"mode": mode, "items": rows})

    # ------------------------------------------------------------------
    # Water quality (sensor_readings table; fed by synthetic generator now,
    # live SenseStream poller later)
    # ------------------------------------------------------------------
    SENSOR_COLUMNS = (
        "water_temp_c", "ph", "do_pct", "chlorophyll_rfu",
        "phycoerythrin_rfu", "turbidity_fnu", "spcond_ms_cm",
    )

    @app.get("/api/water_quality/latest")
    async def water_quality_latest(
        deployment: str = Query("wahoo_2"),
    ) -> Response:
        pool: AsyncConnectionPool | None = state.get("pool")
        if pool is None:
            return JSONResponse({"error": "database unavailable"}, status_code=503)
        cols = ", ".join(SENSOR_COLUMNS)
        sql = f"""
            SELECT ts, deployment_uri, source, {cols}
              FROM sensor_readings
             WHERE deployment_uri = %s
             ORDER BY ts DESC
             LIMIT 1
        """
        async with pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(sql, (deployment,))
                row = await cur.fetchone()
        if not row:
            return JSONResponse({"error": "no readings for deployment"}, status_code=404)
        row["ts"] = row["ts"].isoformat() if row["ts"] else None
        return JSONResponse(row)

    @app.get("/api/water_quality/history")
    async def water_quality_history(
        deployment: str = Query("wahoo_2"),
        hours: int = Query(24, ge=1, le=24 * 30),
        max_points: int = Query(200, ge=10, le=2000),
    ) -> Response:
        """Return downsampled time-series for sparklines.

        Bucketed by an even interval so max_points is honored regardless of the
        sample rate underlying the data.
        """
        pool: AsyncConnectionPool | None = state.get("pool")
        if pool is None:
            return JSONResponse({"error": "database unavailable"}, status_code=503)
        # bucket size in seconds
        bucket_s = max(60, (hours * 3600) // max_points)
        aggs = ",\n              ".join(
            f"avg({c})::real AS {c}" for c in SENSOR_COLUMNS
        )
        sql = f"""
            SELECT to_timestamp(floor(extract(epoch FROM ts) / %s) * %s) AS bucket,
                   {aggs}
              FROM sensor_readings
             WHERE deployment_uri = %s
               AND ts >= NOW() - (%s::int || ' hours')::interval
             GROUP BY 1
             ORDER BY 1 ASC
        """
        async with pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(sql, (bucket_s, bucket_s, deployment, hours))
                rows = await cur.fetchall()
        for r in rows:
            r["bucket"] = r["bucket"].isoformat() if r.get("bucket") else None
        return JSONResponse({
            "deployment": deployment,
            "hours": hours,
            "bucket_seconds": bucket_s,
            "series": rows,
        })

    @app.get("/api/water_quality/summary")
    async def water_quality_summary(
        deployment: str = Query("wahoo_2"),
        hours: int = Query(24, ge=1, le=24 * 30),
    ) -> Response:
        """Min / mean / max for each parameter over the window."""
        pool: AsyncConnectionPool | None = state.get("pool")
        if pool is None:
            return JSONResponse({"error": "database unavailable"}, status_code=503)
        aggs = ",\n              ".join(
            f"min({c})::real AS {c}_min, "
            f"avg({c})::real AS {c}_mean, "
            f"max({c})::real AS {c}_max"
            for c in SENSOR_COLUMNS
        )
        sql = f"""
            SELECT count(*)::int AS n,
                   min(ts) AS first_ts,
                   max(ts) AS last_ts,
                   {aggs}
              FROM sensor_readings
             WHERE deployment_uri = %s
               AND ts >= NOW() - (%s::int || ' hours')::interval
        """
        async with pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(sql, (deployment, hours))
                row = await cur.fetchone() or {}
        for k in ("first_ts", "last_ts"):
            if row.get(k):
                row[k] = row[k].isoformat()
        return JSONResponse(row)

    # ------------------------------------------------------------------
    # Weather (weather_readings table; fed by the same poller as the
    # sonde, in synthetic stub mode until the SenseStream auth token
    # arrives)
    # ------------------------------------------------------------------
    WEATHER_COLUMNS = (
        "bar_press_hpa",
        "wind_dir_avg_deg", "wind_dir_min_deg", "wind_dir_max_deg",
        "wind_speed_avg_ms", "wind_speed_min_ms", "wind_speed_max_ms",
        "air_temp_c", "rel_humidity_pct",
        "rain_accum_mm", "rain_dur_frac_hr", "rain_int_mm_hr", "rain_peak_int_mm_hr",
        "hail_accum", "hail_dur", "hail_int", "hail_peak_int",
        "water_level_mm", "solar_rad_wm2",
        "cloud_cover_pct", "dew_point_c", "forecast_humidity_pct",
        "precip_intensity_mm_hr", "precip_prob_pct",
        "press_sea_level_hpa", "press_surface_hpa",
        "temp_apparent_c", "uv_index", "weather_code",
        "weather_description",
    )

    @app.get("/api/weather/latest")
    async def weather_latest(deployment: str = Query("wahoo_2")) -> Response:
        pool: AsyncConnectionPool | None = state.get("pool")
        if pool is None:
            return JSONResponse({"error": "database unavailable"}, status_code=503)
        cols = ", ".join(WEATHER_COLUMNS)
        sql = f"""
            SELECT ts, deployment_uri, source, {cols}
              FROM weather_readings
             WHERE deployment_uri = %s
             ORDER BY ts DESC
             LIMIT 1
        """
        async with pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(sql, (deployment,))
                row = await cur.fetchone()
        if not row:
            return JSONResponse({"error": "no weather readings for deployment"}, status_code=404)
        row["ts"] = row["ts"].isoformat() if row["ts"] else None
        return JSONResponse(row)

    @app.get("/api/weather/history")
    async def weather_history(
        deployment: str = Query("wahoo_2"),
        hours: int = Query(24, ge=1, le=24 * 30),
        max_points: int = Query(200, ge=10, le=2000),
    ) -> Response:
        """Bucket-averaged weather history. weather_code is mode-bucketed
        (taking the most-recent value per bucket) since it's categorical."""
        pool: AsyncConnectionPool | None = state.get("pool")
        if pool is None:
            return JSONResponse({"error": "database unavailable"}, status_code=503)
        bucket_s = max(60, (hours * 3600) // max_points)
        # Categorical columns (weather_code, weather_description) can't be
        # averaged — we take the most-recent value per bucket via array_agg.
        categorical = {"weather_code", "weather_description"}
        numeric_cols = tuple(c for c in WEATHER_COLUMNS if c not in categorical)
        aggs = ",\n              ".join(
            f"avg({c})::real AS {c}" for c in numeric_cols
        )
        sql = f"""
            SELECT to_timestamp(floor(extract(epoch FROM ts) / %s) * %s) AS bucket,
                   {aggs},
                   (array_agg(weather_code ORDER BY ts DESC))[1] AS weather_code,
                   (array_agg(weather_description ORDER BY ts DESC)
                    FILTER (WHERE weather_description IS NOT NULL))[1] AS weather_description
              FROM weather_readings
             WHERE deployment_uri = %s
               AND ts >= NOW() - (%s::int || ' hours')::interval
             GROUP BY 1
             ORDER BY 1 ASC
        """
        async with pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(sql, (bucket_s, bucket_s, deployment, hours))
                rows = await cur.fetchall()
        for r in rows:
            r["bucket"] = r["bucket"].isoformat() if r.get("bucket") else None
        return JSONResponse({
            "deployment": deployment,
            "hours": hours,
            "bucket_seconds": bucket_s,
            "series": rows,
        })

    # ------------------------------------------------------------------
    # Drift monitor (input drift on the video feed)
    # ------------------------------------------------------------------

    @app.get("/api/drift/recent")
    async def drift_recent() -> Response:
        """Current hour vs. 7-day and 28-day baselines from frame_stats_drift view."""
        pool: AsyncConnectionPool | None = state.get("pool")
        if pool is None:
            return JSONResponse({"error": "database unavailable"}, status_code=503)
        async with pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT * FROM frame_stats_drift ORDER BY source_name")
                rows = await cur.fetchall()
        return JSONResponse(rows)

    @app.get("/api/drift/timeline")
    async def drift_timeline(
        hours: int = Query(72, ge=1, le=24 * 30),
        source_name: str | None = None,
    ) -> Response:
        """Hourly rollup for plotting brightness / detection rate over time."""
        pool: AsyncConnectionPool | None = state.get("pool")
        if pool is None:
            return JSONResponse({"error": "database unavailable"}, status_code=503)
        where = ["hour >= NOW() - (%s::int || ' hours')::interval"]
        params: list = [hours]
        if source_name:
            where.append("source_name = %s")
            params.append(source_name)
        sql = f"""
            SELECT hour, source_name, mean_luma, mean_r, mean_g, mean_b,
                   mean_std_luma, mean_detections_per_frame,
                   frame_with_fish_rate, samples
              FROM frame_stats_hourly
             WHERE {' AND '.join(where)}
             ORDER BY hour ASC
        """
        async with pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(sql, params)
                rows = await cur.fetchall()
        for r in rows:
            r["hour"] = r["hour"].isoformat() if r.get("hour") else None
        return JSONResponse(rows)

    # ------------------------------------------------------------------
    # Species counts with confidence intervals
    # ------------------------------------------------------------------

    @app.get("/api/species_counts_ci")
    async def species_counts_ci(
        hours: int = Query(24, ge=1, le=24 * 30),
        method: str = Query("poisson", pattern="^(poisson|bootstrap)$"),
        min_accuracy: float = Query(0.0, ge=0.0, le=1.0),
        max_species: int = Query(50, ge=1, le=500),
    ) -> Response:
        """Per-species counts over a time window, each with a 95% CI.

        Use this for anything that ships outside the lab (reports, public
        dashboards). The dashboard's regular /api/species_counts is a point
        estimate; this endpoint is what belongs in a written claim.
        """
        pool: AsyncConnectionPool | None = state.get("pool")
        if pool is None:
            return JSONResponse({"error": "database unavailable"}, status_code=503)
        sql = """
            SELECT best_species_id, best_name
              FROM detection_events
             WHERE ts >= NOW() - (%s::int || ' hours')::interval
               AND best_species_id IS NOT NULL
               AND best_accuracy  >= %s
        """
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, (hours, min_accuracy))
                rows = await cur.fetchall()

        species_ids = [r[0] for r in rows]
        name_by_id: dict[str, str] = {}
        for sid, name in rows:
            if sid and name:
                name_by_id.setdefault(sid, name)

        enriched = species_counts_with_ci(species_ids, method=method)[:max_species]
        for item in enriched:
            item["name"] = name_by_id.get(item["species_id"])

        return JSONResponse({
            "window_hours": hours,
            "method": method,
            "min_accuracy": min_accuracy,
            "n_events": len(species_ids),
            "items": enriched,
        })

    @app.get("/api/frame_rate_ci")
    async def frame_rate_ci(hours: int = Query(24, ge=1, le=24 * 30)) -> Response:
        """Wilson CI for frame-with-fish rate from frame_stats."""
        pool: AsyncConnectionPool | None = state.get("pool")
        if pool is None:
            return JSONResponse({"error": "database unavailable"}, status_code=503)
        sql = """
            SELECT count(*) AS n,
                   sum(CASE WHEN num_detections > 0 THEN 1 ELSE 0 END) AS k
              FROM frame_stats
             WHERE ts >= NOW() - (%s::int || ' hours')::interval
        """
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, (hours,))
                row = await cur.fetchone() or (0, 0)
        n, k = int(row[0] or 0), int(row[1] or 0)
        ci = rate_ci(k, n)
        return JSONResponse({"window_hours": hours, "trials": n, "successes": k, "ci": ci.as_dict()})

    # ------------------------------------------------------------------
    # Human corrections
    # ------------------------------------------------------------------

    @app.post("/api/corrections", dependencies=[Depends(require_write_token)])
    async def post_correction(body: CorrectionIn) -> Response:
        pool: AsyncConnectionPool | None = state.get("pool")
        if pool is None:
            return JSONResponse({"error": "database unavailable"}, status_code=503)
        if not body.not_a_fish and not (body.corrected_name or body.corrected_species_id):
            return JSONResponse(
                {"error": "provide corrected_name, corrected_species_id, or set not_a_fish"},
                status_code=400,
            )
        # Validate corrected_bbox shape if provided — must be exactly four
        # integers, x1 < x2 and y1 < y2. Anything else is a client bug and
        # we reject rather than silently writing garbage to Postgres.
        bbox = body.corrected_bbox
        if bbox is not None:
            if len(bbox) != 4 or bbox[0] >= bbox[2] or bbox[1] >= bbox[3]:
                return JSONResponse(
                    {"error": "corrected_bbox must be [x1,y1,x2,y2] with x1<x2 and y1<y2"},
                    status_code=400,
                )
            bbox = [int(v) for v in bbox]

        # If a celebrity_id was supplied, validate it against the loaded
        # catalogue so a typo can't poison the gallery / sightings count.
        celeb_id = (body.celebrity_id or "").strip() or None
        if celeb_id is not None:
            known = state.get("celebrities_by_id") or {}
            if celeb_id not in known:
                return JSONResponse(
                    {"error": f"unknown celebrity_id={celeb_id!r}; not in assets/celebrities.json"},
                    status_code=400,
                )

        async with pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    INSERT INTO detection_corrections
                    (event_id, corrected_name, corrected_species_id,
                     not_a_fish, is_endorsement, corrected_bbox,
                     confidence, reviewer, notes, celebrity_id)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    RETURNING id, created_at
                    """,
                    (body.event_id, body.corrected_name, body.corrected_species_id,
                     body.not_a_fish, body.is_endorsement, bbox,
                     body.confidence, body.reviewer, body.notes, celeb_id),
                )
                row = await cur.fetchone()
        return JSONResponse({
            "id": row["id"],
            "event_id": body.event_id,
            "created_at": row["created_at"].isoformat(),
            "is_endorsement": body.is_endorsement,
        })

    @app.get("/api/corrections")
    async def list_corrections(
        limit: int = Query(50, ge=1, le=500),
        reviewer: str | None = None,
    ) -> Response:
        pool: AsyncConnectionPool | None = state.get("pool")
        if pool is None:
            return JSONResponse({"error": "database unavailable"}, status_code=503)
        where = []
        params: list = []
        if reviewer:
            where.append("reviewer = %s")
            params.append(reviewer)
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        params.append(limit)
        sql = f"""
            SELECT c.id, c.event_id, c.corrected_name, c.corrected_species_id,
                   c.not_a_fish, c.confidence, c.reviewer, c.notes, c.created_at,
                   e.best_name AS original_name, e.best_accuracy, e.ts AS event_ts
              FROM detection_corrections c
              JOIN detection_events e ON e.id = c.event_id
              {where_sql}
             ORDER BY c.created_at DESC
             LIMIT %s
        """
        async with pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(sql, params)
                rows = await cur.fetchall()
        for r in rows:
            r["created_at"] = r["created_at"].isoformat() if r.get("created_at") else None
            r["event_ts"]   = r["event_ts"].isoformat()   if r.get("event_ts")   else None
        return JSONResponse(rows)

    # ------------------------------------------------------------------
    # Celebrities — named recurring animals from the "Fish named by
    # Chatters" sheet. The catalogue is loaded once at startup; the
    # sightings + canonical-photo data joins to detection_corrections
    # where reviewers have tagged a detection with a celebrity_id.
    # ------------------------------------------------------------------

    @app.post(
        "/api/admin/refresh_sheets",
        dependencies=[Depends(require_write_token)],
    )
    async def refresh_sheets() -> Response:
        """Force-refresh the Wahoo Bay Google Sheet right now. Returns
        the poller's result blob (sheet_changed, rebuilt scripts, any
        errors). Useful when a chatter just named a new fish and you
        don't want to wait for the daily cycle."""
        poller: SheetPoller | None = state.get("sheet_poller")
        if poller is None:
            return JSONResponse(
                {"error": "sheet poller disabled (SHEET_URL empty)"},
                status_code=503,
            )
        result = await poller.refresh_once(force=True)
        return JSONResponse(result)

    @app.get("/api/celebrities")
    async def list_celebrities() -> Response:
        """The full celebrities directory plus per-celebrity sightings
        count + canonical photo. Sightings = count of distinct events
        where a reviewer tagged this celebrity. Canonical photo =
        ROI of the most-recent tagged event (rendered through the
        existing /api/detections/{id}/roi.jpg endpoint)."""
        celebs = state.get("celebrities") or []
        if not celebs:
            return JSONResponse({"celebrities": [], "count": 0})
        pool: AsyncConnectionPool | None = state.get("pool")
        stats: dict[str, dict] = {}
        if pool is not None:
            async with pool.connection() as conn:
                async with conn.cursor(row_factory=dict_row) as cur:
                    # Per-celebrity aggregate: total tagged corrections,
                    # most recent tagged event_id, first + last tag time.
                    await cur.execute("""
                        SELECT celebrity_id,
                               COUNT(*)::int  AS sightings,
                               MAX(created_at) AS last_tagged_at,
                               MIN(created_at) AS first_tagged_at,
                               (ARRAY_AGG(event_id ORDER BY created_at DESC))[1] AS recent_event_id
                          FROM detection_corrections
                         WHERE celebrity_id IS NOT NULL
                         GROUP BY celebrity_id
                    """)
                    for r in await cur.fetchall():
                        stats[r["celebrity_id"]] = {
                            "sightings": int(r["sightings"]),
                            "last_tagged_at": r["last_tagged_at"].isoformat() if r["last_tagged_at"] else None,
                            "first_tagged_at": r["first_tagged_at"].isoformat() if r["first_tagged_at"] else None,
                            "recent_event_id": int(r["recent_event_id"]) if r["recent_event_id"] is not None else None,
                        }
        out = []
        for c in celebs:
            s = stats.get(c["id"], {})
            photo_event = s.get("recent_event_id")
            out.append({
                **c,
                "common": common_name(c["binomial"]) if c.get("binomial") else None,
                "sightings": s.get("sightings", 0),
                "last_tagged_at": s.get("last_tagged_at"),
                "first_tagged_at": s.get("first_tagged_at"),
                "photo_event_id": photo_event,
                "photo_url": f"/api/detections/{photo_event}/roi.jpg?size=320" if photo_event else None,
            })
        return JSONResponse({"celebrities": out, "count": len(out)})

    @app.get("/api/celebrities/{celebrity_id}")
    async def get_celebrity(
        celebrity_id: str,
        gallery_limit: int = Query(24, ge=1, le=200),
    ) -> Response:
        """One celebrity: catalogue entry + sightings count + recent
        tagged-detection gallery (event_id + ts + reviewer + bbox so
        the client can render thumbnails via the existing roi.jpg
        endpoint)."""
        celebs = state.get("celebrities_by_id") or {}
        c = celebs.get(celebrity_id)
        if c is None:
            return JSONResponse({"error": f"unknown celebrity_id={celebrity_id!r}"}, status_code=404)
        pool: AsyncConnectionPool | None = state.get("pool")
        sightings = 0
        gallery: list[dict] = []
        first_tagged_at = last_tagged_at = None
        if pool is not None:
            async with pool.connection() as conn:
                async with conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute("""
                        SELECT COUNT(*)::int   AS sightings,
                               MIN(created_at) AS first_tagged_at,
                               MAX(created_at) AS last_tagged_at
                          FROM detection_corrections
                         WHERE celebrity_id = %s
                    """, (celebrity_id,))
                    r = await cur.fetchone()
                    if r:
                        sightings = int(r["sightings"] or 0)
                        first_tagged_at = r["first_tagged_at"].isoformat() if r["first_tagged_at"] else None
                        last_tagged_at = r["last_tagged_at"].isoformat() if r["last_tagged_at"] else None
                    # Recent tagged events for the gallery. LEFT JOIN
                    # so a correction whose event was wiped (e.g. by a
                    # reset) still surfaces but with image_path NULL.
                    await cur.execute("""
                        SELECT dc.event_id, dc.created_at, dc.reviewer, dc.confidence,
                               dc.corrected_name,
                               e.ts AS event_ts, e.source_name, e.bbox
                          FROM detection_corrections dc
                          LEFT JOIN detection_events e ON e.id = dc.event_id
                         WHERE dc.celebrity_id = %s
                         ORDER BY dc.created_at DESC
                         LIMIT %s
                    """, (celebrity_id, gallery_limit))
                    for r in await cur.fetchall():
                        gallery.append({
                            "event_id": int(r["event_id"]),
                            "tagged_at": r["created_at"].isoformat() if r["created_at"] else None,
                            "reviewer": r["reviewer"],
                            "confidence": r["confidence"],
                            "corrected_name": r["corrected_name"],
                            "event_ts": r["event_ts"].isoformat() if r["event_ts"] else None,
                            "source_name": r["source_name"],
                            "bbox": list(r["bbox"]) if r["bbox"] else None,
                            "roi_url": f"/api/detections/{r['event_id']}/roi.jpg?size=240",
                        })
        photo_event = gallery[0]["event_id"] if gallery else None
        return JSONResponse({
            **c,
            "common": common_name(c["binomial"]) if c.get("binomial") else None,
            "sightings": sightings,
            "first_tagged_at": first_tagged_at,
            "last_tagged_at": last_tagged_at,
            "photo_event_id": photo_event,
            "photo_url": f"/api/detections/{photo_event}/roi.jpg?size=640" if photo_event else None,
            "gallery": gallery,
        })

    # ------------------------------------------------------------------
    # Per-camera no-fish-zone admin (polygon mask for false-positive
    # geometry — SEAHIVE holes, dock pilings, etc.). The drawing UI is
    # at /admin/regions.html; the worker reads the saved JSON at
    # subprocess startup.
    # ------------------------------------------------------------------

    @app.get("/admin/regions.html")
    async def regions_admin_page() -> Response:
        """The polygon-drawing admin UI. Standalone HTML page — no
        templating needed, the JS fetches everything else from the
        sibling API endpoints below."""
        try:
            html = (BASE_DIR / "templates" / "regions_admin.html").read_text()
        except FileNotFoundError:
            return JSONResponse(
                {"error": "regions_admin.html missing on disk"}, status_code=500,
            )
        return Response(content=html, media_type="text/html")

    @app.get("/api/admin/negative_regions")
    async def get_negative_regions() -> Response:
        try:
            data = json.loads(Path(cfg.negative_regions_path).read_text())
        except FileNotFoundError:
            data = {"sources": {}}
        except Exception as e:
            return JSONResponse(
                {"error": f"failed to read regions config: {e}"}, status_code=500,
            )
        return JSONResponse(data)

    @app.post(
        "/api/admin/negative_regions",
        dependencies=[Depends(require_write_token)],
    )
    async def post_negative_regions(body: dict) -> Response:
        """Replace the full negative-regions JSON. Validates shape so a
        malformed POST can't poison the worker's startup. Worker reads
        this file at boot — restart workers to apply changes."""
        if not isinstance(body, dict) or "sources" not in body:
            return JSONResponse(
                {"error": "payload must be a JSON object with a 'sources' key"},
                status_code=400,
            )
        sources = body.get("sources")
        if not isinstance(sources, dict):
            return JSONResponse(
                {"error": "'sources' must be an object keyed by source_name"},
                status_code=400,
            )
        for src, cfg_per_src in sources.items():
            if not isinstance(cfg_per_src, dict):
                return JSONResponse(
                    {"error": f"sources.{src} must be an object"}, status_code=400,
                )
            regions = cfg_per_src.get("regions") or []
            if not isinstance(regions, list):
                return JSONResponse(
                    {"error": f"sources.{src}.regions must be a list"}, status_code=400,
                )
            for i, r in enumerate(regions):
                if not isinstance(r, dict):
                    return JSONResponse(
                        {"error": f"sources.{src}.regions[{i}] must be an object"},
                        status_code=400,
                    )
                poly = r.get("polygon")
                if not isinstance(poly, list) or len(poly) < 3:
                    return JSONResponse(
                        {"error": f"sources.{src}.regions[{i}].polygon needs >=3 vertices"},
                        status_code=400,
                    )
                for v in poly:
                    if (
                        not isinstance(v, list)
                        or len(v) != 2
                        or not all(isinstance(x, (int, float)) for x in v)
                    ):
                        return JSONResponse(
                            {"error": f"vertices must be [x, y] number pairs ({src}#{i})"},
                            status_code=400,
                        )
        path = Path(cfg.negative_regions_path)
        try:
            # Preserve any underscore-prefixed top-level keys (the
            # _about / _format / _admin_ui docstrings in the on-disk
            # file). The admin UI doesn't know about them and will
            # otherwise wipe them on save.
            existing: dict = {}
            try:
                existing = json.loads(path.read_text())
            except FileNotFoundError:
                pass
            merged = {k: v for k, v in existing.items() if k.startswith("_")}
            merged.update(body)
            # Write to a temp file, then rename — atomic on the same
            # filesystem. Worker is reading the file at startup only,
            # so we don't need to coordinate with a live reader.
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(merged, indent=2))
            tmp.replace(path)
        except Exception as e:
            return JSONResponse(
                {"error": f"failed to write regions config: {e}"}, status_code=500,
            )
        return JSONResponse({"ok": True, "saved": str(path)})

    @app.get("/api/corrections/stats")
    async def correction_stats() -> Response:
        pool: AsyncConnectionPool | None = state.get("pool")
        if pool is None:
            return JSONResponse({"error": "database unavailable"}, status_code=503)
        sql = """
            SELECT count(*)::int AS total,
                   sum(CASE WHEN not_a_fish THEN 1 ELSE 0 END)::int AS not_a_fish,
                   count(DISTINCT reviewer) AS reviewers,
                   count(DISTINCT corrected_species_id) AS distinct_species
              FROM detection_corrections
        """
        async with pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(sql)
                row = await cur.fetchone() or {}
        return JSONResponse(row)

    # ------------------------------------------------------------------
    # Alerts
    # ------------------------------------------------------------------

    @app.get("/api/alerts/active")
    async def alerts_active() -> Response:
        pool: AsyncConnectionPool | None = state.get("pool")
        if pool is None:
            return JSONResponse({"error": "database unavailable"}, status_code=503)
        sql = """
            SELECT id, name, severity, message, details, first_seen, last_seen,
                   acknowledged_by, acknowledged_at
              FROM alerts
             WHERE resolved_at IS NULL
             ORDER BY CASE severity
                        WHEN 'critical' THEN 0
                        WHEN 'warning'  THEN 1
                        WHEN 'info'     THEN 2
                        ELSE 3 END,
                      last_seen DESC
        """
        async with pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(sql)
                rows = await cur.fetchall()
        for r in rows:
            for k in ("first_seen", "last_seen", "acknowledged_at"):
                if r.get(k):
                    r[k] = r[k].isoformat()
        return JSONResponse(rows)

    @app.post("/api/alerts/{alert_id}/ack", dependencies=[Depends(require_write_token)])
    async def ack_alert(alert_id: int, reviewer: str | None = None) -> Response:
        pool: AsyncConnectionPool | None = state.get("pool")
        if pool is None:
            return JSONResponse({"error": "database unavailable"}, status_code=503)
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE alerts
                       SET acknowledged_by = COALESCE(%s, 'anonymous'),
                           acknowledged_at = NOW()
                     WHERE id = %s AND resolved_at IS NULL
                    """,
                    (reviewer, alert_id),
                )
                acked = cur.rowcount
        return JSONResponse({"acknowledged": bool(acked)})

    @app.get("/api/alerts/rules")
    async def alerts_rules() -> Response:
        return JSONResponse([
            {"name": r.name, "severity": r.severity, "description": r.description}
            for r in SLO_RULES
        ])

    # ------------------------------------------------------------------
    # Chart endpoints (Trends view)
    # ------------------------------------------------------------------
    # All read from species_sightings_hourly_mat (refreshed every ~60s)
    # so they're always sub-100ms regardless of detection_events size.

    @app.get("/api/charts/detection_rate")
    async def chart_detection_rate(
        hours: int = Query(24, ge=1, le=24 * 30),
    ) -> Response:
        pool: AsyncConnectionPool | None = state.get("pool")
        if pool is None:
            return JSONResponse({"error": "database unavailable"}, status_code=503)
        sql = """
            SELECT hour, sum(sightings)::int AS sightings
              FROM species_sightings_hourly_mat
             WHERE hour >= NOW() - (%s::int || ' hours')::interval
             GROUP BY 1
             ORDER BY 1 ASC
        """
        async with pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(sql, (hours,))
                rows = await cur.fetchall()
        return JSONResponse({
            "hours": hours,
            "series": [
                {"hour": r["hour"].isoformat(), "sightings": r["sightings"]}
                for r in rows
            ],
        })

    @app.get("/api/charts/top_species_timeseries")
    async def chart_top_species_timeseries(
        hours: int = Query(24, ge=1, le=24 * 30),
        top_n: int = Query(8, ge=1, le=20),
    ) -> Response:
        """Top-N species by total sightings in window, with per-hour series
        for each. Returns one row per (hour, species) for charting; the
        client builds the multi-line chart."""
        pool: AsyncConnectionPool | None = state.get("pool")
        if pool is None:
            return JSONResponse({"error": "database unavailable"}, status_code=503)
        sql = """
            WITH top AS (
              SELECT species_id, MIN(name) AS name,
                     SUM(sightings)::int AS total
                FROM species_sightings_hourly_mat
               WHERE hour >= NOW() - (%s::int || ' hours')::interval
               GROUP BY 1
               ORDER BY total DESC
               LIMIT %s
            )
            SELECT m.hour, m.species_id, t.name,
                   m.sightings::int AS sightings,
                   t.total::int     AS total
              FROM species_sightings_hourly_mat m
              JOIN top t USING (species_id)
             WHERE m.hour >= NOW() - (%s::int || ' hours')::interval
             ORDER BY t.total DESC, m.hour ASC
        """
        async with pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(sql, (hours, top_n, hours))
                rows = await cur.fetchall()
        # group into per-species series for the client
        series: dict = {}
        for r in rows:
            sid = r["species_id"]
            if sid not in series:
                series[sid] = {
                    "species_id": sid,
                    "name": r["name"],
                    "total": r["total"],
                    "points": [],
                }
            series[sid]["points"].append({
                "hour": r["hour"].isoformat(),
                "sightings": r["sightings"],
            })
        # preserve "top" ordering by total desc
        ordered = sorted(series.values(), key=lambda s: s["total"], reverse=True)
        return JSONResponse({"hours": hours, "top_n": top_n, "species": ordered})

    @app.get("/api/charts/sightings_x_water")
    async def chart_sightings_x_water(
        hours: int = Query(24, ge=1, le=24 * 30),
        deployment: str = Query("wahoo_2"),
    ) -> Response:
        """Hourly sightings joined with hourly avg water-quality. Lets
        the client overlay fish activity against any water parameter."""
        pool: AsyncConnectionPool | None = state.get("pool")
        if pool is None:
            return JSONResponse({"error": "database unavailable"}, status_code=503)
        sql = """
            WITH s AS (
              SELECT hour, SUM(sightings)::int AS sightings
                FROM species_sightings_hourly_mat
               WHERE hour >= NOW() - (%s::int || ' hours')::interval
               GROUP BY 1
            ),
            w AS (
              SELECT date_trunc('hour', ts)              AS hour,
                     avg(water_temp_c)::real             AS water_temp_c,
                     avg(ph)::real                       AS ph,
                     avg(do_pct)::real                   AS do_pct,
                     avg(turbidity_fnu)::real            AS turbidity_fnu,
                     avg(chlorophyll_rfu)::real          AS chlorophyll_rfu,
                     avg(spcond_ms_cm)::real             AS spcond_ms_cm
                FROM sensor_readings
               WHERE deployment_uri = %s
                 AND ts >= NOW() - (%s::int || ' hours')::interval
               GROUP BY 1
            )
            SELECT COALESCE(s.hour, w.hour) AS hour,
                   s.sightings,
                   w.water_temp_c, w.ph, w.do_pct,
                   w.turbidity_fnu, w.chlorophyll_rfu, w.spcond_ms_cm
              FROM s
              FULL OUTER JOIN w ON s.hour = w.hour
             ORDER BY hour ASC
        """
        async with pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(sql, (hours, deployment, hours))
                rows = await cur.fetchall()
        return JSONResponse({
            "hours": hours,
            "deployment": deployment,
            "series": [
                {
                    "hour": r["hour"].isoformat(),
                    "sightings": r["sightings"],
                    "water_temp_c": r["water_temp_c"],
                    "ph": r["ph"],
                    "do_pct": r["do_pct"],
                    "turbidity_fnu": r["turbidity_fnu"],
                    "chlorophyll_rfu": r["chlorophyll_rfu"],
                    "spcond_ms_cm": r["spcond_ms_cm"],
                }
                for r in rows
            ],
        })

    @app.get("/api/charts/sightings_x_weather")
    async def chart_sightings_x_weather(
        hours: int = Query(24, ge=1, le=24 * 30),
        deployment: str = Query("wahoo_2"),
    ) -> Response:
        """Hourly sightings joined with hourly weather. Lets the client
        overlay fish activity against air temp, wind, rain, pressure,
        solar, or cloud cover."""
        pool: AsyncConnectionPool | None = state.get("pool")
        if pool is None:
            return JSONResponse({"error": "database unavailable"}, status_code=503)
        sql = """
            WITH s AS (
              SELECT hour, SUM(sightings)::int AS sightings
                FROM species_sightings_hourly_mat
               WHERE hour >= NOW() - (%s::int || ' hours')::interval
               GROUP BY 1
            ),
            w AS (
              SELECT date_trunc('hour', ts)               AS hour,
                     avg(air_temp_c)::real                AS air_temp_c,
                     avg(wind_speed_avg_ms)::real         AS wind_speed_avg_ms,
                     avg(rain_accum_mm)::real             AS rain_accum_mm,
                     avg(rain_int_mm_hr)::real            AS rain_int_mm_hr,
                     sum(rain_accum_mm)::real             AS rain_total_mm,
                     avg(bar_press_hpa)::real             AS bar_press_hpa,
                     avg(solar_rad_wm2)::real             AS solar_rad_wm2,
                     avg(cloud_cover_pct)::real           AS cloud_cover_pct,
                     avg(rel_humidity_pct)::real          AS rel_humidity_pct,
                     avg(uv_index)::real                  AS uv_index
                FROM weather_readings
               WHERE deployment_uri = %s
                 AND ts >= NOW() - (%s::int || ' hours')::interval
               GROUP BY 1
            )
            SELECT COALESCE(s.hour, w.hour) AS hour,
                   s.sightings,
                   w.air_temp_c, w.wind_speed_avg_ms,
                   w.rain_accum_mm, w.rain_int_mm_hr, w.rain_total_mm,
                   w.bar_press_hpa, w.solar_rad_wm2, w.cloud_cover_pct,
                   w.rel_humidity_pct, w.uv_index
              FROM s
              FULL OUTER JOIN w ON s.hour = w.hour
             ORDER BY hour ASC
        """
        async with pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(sql, (hours, deployment, hours))
                rows = await cur.fetchall()
        return JSONResponse({
            "hours": hours,
            "deployment": deployment,
            "series": [
                {
                    "hour": r["hour"].isoformat(),
                    "sightings": r["sightings"],
                    "air_temp_c": r["air_temp_c"],
                    "wind_speed_avg_ms": r["wind_speed_avg_ms"],
                    "rain_accum_mm": r["rain_accum_mm"],
                    "rain_int_mm_hr": r["rain_int_mm_hr"],
                    "rain_total_mm": r["rain_total_mm"],
                    "bar_press_hpa": r["bar_press_hpa"],
                    "solar_rad_wm2": r["solar_rad_wm2"],
                    "cloud_cover_pct": r["cloud_cover_pct"],
                    "rel_humidity_pct": r["rel_humidity_pct"],
                    "uv_index": r["uv_index"],
                }
                for r in rows
            ],
        })

    # ------------------------------------------------------------------
    # Species explorer — list of species in the window + per-species
    # drill-through profile. Backs the Fish Explorer tab of the
    # Reports panel.
    # ------------------------------------------------------------------

    @app.get("/api/species/list")
    async def species_list(
        hours: int = Query(168, ge=1, le=24 * 30),
        min_frames: int = Query(3, ge=1, le=1000),
    ) -> Response:
        """Per-species summary in the window: count, mean acc, first/last seen,
        sources the species was seen on. Used by the Fish Explorer table.
        Reads from the matview so it stays sub-100 ms."""
        pool: AsyncConnectionPool | None = state.get("pool")
        if pool is None:
            return JSONResponse({"error": "database unavailable"}, status_code=503)
        sql = """
            SELECT species_id,
                   MIN(name)                          AS name,
                   COUNT(*)::int                      AS sightings,
                   SUM(frame_count)::bigint           AS total_frames,
                   AVG(mean_accuracy)::real           AS mean_accuracy,
                   MAX(peak_accuracy)::real           AS peak_accuracy,
                   array_agg(DISTINCT source_name)    AS sources,
                   MIN(first_seen)                    AS first_seen,
                   MAX(last_seen)                     AS last_seen
              FROM species_sightings_mat
             WHERE last_seen >= NOW() - (%s::int || ' hours')::interval
               AND frame_count >= %s
             GROUP BY species_id
             ORDER BY sightings DESC
             LIMIT 200
        """
        async with pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(sql, (hours, min_frames))
                rows = await cur.fetchall()
        out = []
        for r in rows:
            out.append({
                "species_id": r["species_id"],
                "latin": r["name"],
                "common": common_name(r["name"]),
                "sightings": r["sightings"],
                "total_frames": int(r["total_frames"]) if r["total_frames"] is not None else 0,
                "mean_accuracy": r["mean_accuracy"],
                "peak_accuracy": r["peak_accuracy"],
                "sources": r["sources"] or [],
                "first_seen": r["first_seen"].isoformat() if r["first_seen"] else None,
                "last_seen": r["last_seen"].isoformat() if r["last_seen"] else None,
            })
        return JSONResponse({"hours": hours, "min_frames": min_frames, "species": out})

    @app.get("/api/species/habitat_matrix")
    async def species_habitat_matrix(
        hours: int = Query(168, ge=1, le=24 * 30),
        top_n: int = Query(15, ge=1, le=50),
    ) -> Response:
        """Top-N species × camera (used as the 'habitat' dimension since we
        don't yet have a per-detection habitat label). Powers the heatmap
        on the Fish Explorer tab."""
        pool: AsyncConnectionPool | None = state.get("pool")
        if pool is None:
            return JSONResponse({"error": "database unavailable"}, status_code=503)
        sql = """
            WITH top AS (
              SELECT species_id, MIN(name) AS name, SUM(frame_count)::bigint AS total
                FROM species_sightings_mat
               WHERE last_seen >= NOW() - (%s::int || ' hours')::interval
                 AND frame_count >= 3
               GROUP BY species_id
               ORDER BY total DESC
               LIMIT %s
            )
            SELECT m.species_id,
                   t.name,
                   t.total,
                   m.source_name,
                   COUNT(*)::int AS sightings
              FROM species_sightings_mat m
              JOIN top t USING (species_id)
             WHERE m.last_seen >= NOW() - (%s::int || ' hours')::interval
               AND m.frame_count >= 3
             GROUP BY m.species_id, t.name, t.total, m.source_name
             ORDER BY t.total DESC
        """
        async with pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(sql, (hours, top_n, hours))
                rows = await cur.fetchall()

        # Pivot to species × source matrix; keep species ordering by total.
        by_species: dict = {}
        sources: list = []
        for r in rows:
            sid = r["species_id"]
            if sid not in by_species:
                by_species[sid] = {
                    "species_id": sid,
                    "latin": r["name"],
                    "common": common_name(r["name"]),
                    "counts": {},
                }
            by_species[sid]["counts"][r["source_name"]] = r["sightings"]
            if r["source_name"] not in sources:
                sources.append(r["source_name"])
        return JSONResponse({
            "hours": hours,
            "sources": sources,
            "species": list(by_species.values()),
        })

    @app.get("/api/species/{species_id}/profile")
    async def species_profile(
        species_id: str,
        hours: int = Query(168, ge=1, le=24 * 30),
    ) -> Response:
        """Per-species drill-through: lifetime + windowed stats, activity
        by hour-of-day, top-K accuracy distribution, recent saved-frame
        crops the dashboard can link to."""
        pool: AsyncConnectionPool | None = state.get("pool")
        if pool is None:
            return JSONResponse({"error": "database unavailable"}, status_code=503)

        async with pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                # Summary card
                await cur.execute(
                    """
                    SELECT MIN(name)              AS latin,
                           COUNT(*)::int          AS sightings,
                           SUM(frame_count)::bigint AS total_frames,
                           AVG(mean_accuracy)::real AS mean_accuracy,
                           MAX(peak_accuracy)::real AS peak_accuracy,
                           MIN(first_seen)        AS first_seen,
                           MAX(last_seen)         AS last_seen,
                           array_agg(DISTINCT source_name) AS sources
                      FROM species_sightings_mat
                     WHERE species_id = %s
                       AND last_seen >= NOW() - (%s::int || ' hours')::interval
                    """,
                    (species_id, hours),
                )
                summary = await cur.fetchone() or {}

                # Activity by local hour of day (Eastern; same convention as
                # the visitor_stats hourly chart)
                await cur.execute(
                    """
                    SELECT extract(hour FROM (first_seen AT TIME ZONE 'America/New_York'))::int AS hod,
                           COUNT(*)::int AS sightings
                      FROM species_sightings_mat
                     WHERE species_id = %s
                       AND last_seen >= NOW() - (%s::int || ' hours')::interval
                     GROUP BY 1
                     ORDER BY 1
                    """,
                    (species_id, hours),
                )
                hod_rows = await cur.fetchall()

                # Top-K accuracy distribution (when this species was the top-1)
                await cur.execute(
                    """
                    SELECT
                      CASE
                        WHEN best_accuracy < 0.30 THEN '0.0–0.3'
                        WHEN best_accuracy < 0.50 THEN '0.3–0.5'
                        WHEN best_accuracy < 0.70 THEN '0.5–0.7'
                        WHEN best_accuracy < 0.85 THEN '0.7–0.85'
                        ELSE                          '0.85+'
                      END AS bucket,
                      COUNT(*)::int AS n
                    FROM detection_events
                    WHERE best_species_id = %s
                      AND ts >= NOW() - (%s::int || ' hours')::interval
                    GROUP BY 1
                    ORDER BY 1
                    """,
                    (species_id, hours),
                )
                acc_rows = await cur.fetchall()

                # A handful of recent saved-frame crops (so the UI can show
                # actual images of this species). Pull saved_frames that
                # contain at least one detection_event for the species.
                await cur.execute(
                    """
                    SELECT sf.image_path, sf.ts, sf.source_name, sf.num_fish
                      FROM saved_frames sf
                      JOIN detection_events e ON e.frame_id = sf.frame_id
                                              AND e.source_name = sf.source_name
                     WHERE e.best_species_id = %s
                       AND sf.ts >= NOW() - (%s::int || ' hours')::interval
                       AND sf.image_path IS NOT NULL
                     GROUP BY sf.image_path, sf.ts, sf.source_name, sf.num_fish
                     ORDER BY sf.ts DESC
                     LIMIT 12
                    """,
                    (species_id, hours),
                )
                frame_rows = await cur.fetchall()

        # Fill hour-of-day buckets so the UI gets all 24 values
        hod = {i: 0 for i in range(24)}
        for r in hod_rows:
            if r["hod"] is not None:
                hod[int(r["hod"])] = r["sightings"]

        return JSONResponse({
            "species_id": species_id,
            "hours": hours,
            "latin": summary.get("latin"),
            "common": common_name(summary.get("latin") or ""),
            "sightings": summary.get("sightings") or 0,
            "total_frames": int(summary.get("total_frames") or 0),
            "mean_accuracy": summary.get("mean_accuracy"),
            "peak_accuracy": summary.get("peak_accuracy"),
            "first_seen": summary["first_seen"].isoformat() if summary.get("first_seen") else None,
            "last_seen": summary["last_seen"].isoformat() if summary.get("last_seen") else None,
            "sources": summary.get("sources") or [],
            "hour_of_day": [{"hour": h, "sightings": hod[h]} for h in range(24)],
            "accuracy_bins": [{"bucket": r["bucket"], "n": r["n"]} for r in acc_rows],
            "recent_frames": [
                {
                    "image_path": r["image_path"],
                    "ts": r["ts"].isoformat() if r["ts"] else None,
                    "source_name": r["source_name"],
                    "num_fish": r["num_fish"],
                }
                for r in frame_rows
            ],
        })

    # ------------------------------------------------------------------
    # CSV exports
    # ------------------------------------------------------------------

    def _csv_response(generator, filename: str) -> StreamingResponse:
        return StreamingResponse(
            generator,
            media_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Cache-Control": "no-store",
            },
        )

    def _csv_filename(stem: str, **bits) -> str:
        ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        suffix = "_".join(f"{k}-{v}" for k, v in bits.items() if v not in (None, ""))
        return f"wahoobay_{stem}{('_' + suffix) if suffix else ''}_{ts}.csv"

    @app.get("/api/export/events.csv")
    async def export_events_csv(
        hours: int = Query(24, ge=1, le=24 * 365),
        species_id: str | None = None,
        min_accuracy: float = Query(0.0, ge=0.0, le=1.0),
    ) -> Response:
        pool: AsyncConnectionPool | None = state.get("pool")
        if pool is None:
            return JSONResponse({"error": "database unavailable"}, status_code=503)
        gen = export_events(pool, hours, species_id, min_accuracy)
        return _csv_response(gen, _csv_filename("events", hours=hours, species=species_id))

    @app.get("/api/export/species_counts.csv")
    async def export_species_counts_csv(
        hours: int = Query(24, ge=1, le=24 * 365),
    ) -> Response:
        pool: AsyncConnectionPool | None = state.get("pool")
        if pool is None:
            return JSONResponse({"error": "database unavailable"}, status_code=503)
        gen = export_species_counts(pool, hours)
        return _csv_response(gen, _csv_filename("species_counts", hours=hours))

    @app.get("/api/export/water_quality.csv")
    async def export_water_quality_csv(
        hours: int = Query(24, ge=1, le=24 * 365),
        deployment: str = Query("wahoo_2"),
    ) -> Response:
        pool: AsyncConnectionPool | None = state.get("pool")
        if pool is None:
            return JSONResponse({"error": "database unavailable"}, status_code=503)
        gen = export_water_quality(pool, hours, deployment)
        return _csv_response(gen, _csv_filename("water_quality", hours=hours, deployment=deployment))

    @app.get("/api/export/weather.csv")
    async def export_weather_csv(
        hours: int = Query(24, ge=1, le=24 * 365),
        deployment: str = Query("wahoo_2"),
    ) -> Response:
        """All columns of weather_readings: on-site WXT-536 station
        readings (wind, pressure, rain, air temp, RH, solar rad, …)
        plus the merged Open-Meteo / NWS forecast layer (cloud cover,
        dew point, UV index, weather code, METAR description)."""
        pool: AsyncConnectionPool | None = state.get("pool")
        if pool is None:
            return JSONResponse({"error": "database unavailable"}, status_code=503)
        gen = export_weather(pool, hours, deployment)
        return _csv_response(gen, _csv_filename("weather", hours=hours, deployment=deployment))

    @app.get("/api/export/frame_stats.csv")
    async def export_frame_stats_csv(
        hours: int = Query(24, ge=1, le=24 * 365),
    ) -> Response:
        pool: AsyncConnectionPool | None = state.get("pool")
        if pool is None:
            return JSONResponse({"error": "database unavailable"}, status_code=503)
        gen = export_frame_stats(pool, hours)
        return _csv_response(gen, _csv_filename("frame_stats", hours=hours))

    @app.get("/api/export/saved_frames.csv")
    async def export_saved_frames_csv(
        hours: int = Query(24, ge=1, le=24 * 365),
    ) -> Response:
        pool: AsyncConnectionPool | None = state.get("pool")
        if pool is None:
            return JSONResponse({"error": "database unavailable"}, status_code=503)
        gen = export_saved_frames(pool, hours)
        return _csv_response(gen, _csv_filename("saved_frames", hours=hours))

    @app.get("/api/export/corrections.csv")
    async def export_corrections_csv(
        hours: int | None = Query(None, ge=1, le=24 * 365),
        reviewer: str | None = None,
    ) -> Response:
        pool: AsyncConnectionPool | None = state.get("pool")
        if pool is None:
            return JSONResponse({"error": "database unavailable"}, status_code=503)
        gen = export_corrections(pool, hours, reviewer)
        return _csv_response(gen, _csv_filename("corrections", hours=hours, reviewer=reviewer))

    @app.get("/api/export/alerts.csv")
    async def export_alerts_csv(
        include_resolved: bool = Query(False),
    ) -> Response:
        pool: AsyncConnectionPool | None = state.get("pool")
        if pool is None:
            return JSONResponse({"error": "database unavailable"}, status_code=503)
        gen = export_alerts(pool, include_resolved)
        return _csv_response(gen, _csv_filename("alerts", scope="all" if include_resolved else "active"))

    # ----- biology-friendly + ML-friendly bonus exports ----------------------

    @app.get("/api/export/sightings.csv")
    async def export_sightings_csv(
        hours: int = Query(24, ge=1, le=24 * 365),
        min_frames: int = Query(3, ge=1, le=1000),
    ) -> Response:
        pool: AsyncConnectionPool | None = state.get("pool")
        if pool is None:
            return JSONResponse({"error": "database unavailable"}, status_code=503)
        gen = export_sightings(pool, hours, min_frames)
        return _csv_response(gen, _csv_filename("sightings", hours=hours))

    @app.get("/api/export/hourly_summary.csv")
    async def export_hourly_summary_csv(
        hours: int = Query(168, ge=1, le=24 * 365),
        deployment: str = Query("wahoo_2"),
    ) -> Response:
        pool: AsyncConnectionPool | None = state.get("pool")
        if pool is None:
            return JSONResponse({"error": "database unavailable"}, status_code=503)
        gen = export_hourly_summary(pool, hours, deployment)
        return _csv_response(gen, _csv_filename("hourly_summary", hours=hours, deployment=deployment))

    @app.get("/api/export/tracks_timeline.csv")
    async def export_tracks_timeline_csv(
        hours: int = Query(24, ge=1, le=24 * 365),
        min_frames: int = Query(3, ge=1, le=1000),
    ) -> Response:
        pool: AsyncConnectionPool | None = state.get("pool")
        if pool is None:
            return JSONResponse({"error": "database unavailable"}, status_code=503)
        gen = export_tracks_timeline(pool, hours, min_frames)
        return _csv_response(gen, _csv_filename("tracks_timeline", hours=hours))

    @app.get("/api/export/topk_long.csv")
    async def export_topk_long_csv(
        hours: int = Query(24, ge=1, le=24 * 365),
        min_accuracy: float = Query(0.0, ge=0.0, le=1.0),
    ) -> Response:
        pool: AsyncConnectionPool | None = state.get("pool")
        if pool is None:
            return JSONResponse({"error": "database unavailable"}, status_code=503)
        gen = export_topk_long(pool, hours, min_accuracy)
        return _csv_response(gen, _csv_filename("topk_long", hours=hours))

    @app.get("/api/export/labeled_corrections.csv")
    async def export_labeled_corrections_csv() -> Response:
        pool: AsyncConnectionPool | None = state.get("pool")
        if pool is None:
            return JSONResponse({"error": "database unavailable"}, status_code=503)
        gen = export_labeled_corrections(pool)
        return _csv_response(gen, _csv_filename("labeled_corrections"))

    # ----- Parquet variants for the heaviest endpoints -----------------------

    PARQUET_RESOURCES = {"events", "tracks_timeline", "topk_long", "hourly_summary"}

    @app.get("/api/export/{resource}.parquet")
    async def export_parquet(
        resource: str,
        hours: int = Query(24, ge=1, le=24 * 365),
        species_id: str | None = None,
        min_accuracy: float = Query(0.0, ge=0.0, le=1.0),
        min_frames: int = Query(3, ge=1, le=1000),
        deployment: str = Query("wahoo_2"),
    ) -> Response:
        if resource not in PARQUET_RESOURCES:
            return JSONResponse(
                {"error": f"parquet only available for: {sorted(PARQUET_RESOURCES)}"},
                status_code=400,
            )
        pool: AsyncConnectionPool | None = state.get("pool")
        if pool is None:
            return JSONResponse({"error": "database unavailable"}, status_code=503)
        try:
            sql, params, cols = query_for(
                resource,
                hours=hours,
                species_id=species_id,
                min_accuracy=min_accuracy,
                min_frames=min_frames,
                deployment=deployment,
            )
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        blob = await fetch_parquet_bytes(pool, sql, params, cols)
        filename = _csv_filename(resource, hours=hours).replace(".csv", ".parquet")
        return Response(
            content=blob,
            media_type="application/vnd.apache.parquet",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Cache-Control": "no-store",
            },
        )

    # ----- Static-ish sidecars: README + camera metadata ---------------------

    @app.get("/api/export/README.md")
    async def export_readme() -> Response:
        path = BASE_DIR / "data" / "README.md"
        if not path.exists():
            return JSONResponse({"error": "README missing"}, status_code=500)
        text = path.read_text()
        return Response(
            content=text,
            media_type="text/markdown",
            headers={"Content-Disposition": 'attachment; filename="README.md"'},
        )

    @app.get("/api/export/camera_metadata.json")
    async def export_camera_metadata() -> Response:
        # Try several candidates so this works in:
        #   - bare-metal DGX dev: file lives in repo root
        #   - container: file is COPYed to /data/camera_metadata.json
        #   - any other layout: relative to dashboard's BASE_DIR
        for candidate in (
            Path("/data/camera_metadata.json"),
            Path("/raid/scratch/dzimmerman2021/wahoobay/data/camera_metadata.json"),
            BASE_DIR / "data" / "camera_metadata.json",
        ):
            if candidate.exists():
                return Response(
                    content=candidate.read_text(),
                    media_type="application/json",
                    headers={"Content-Disposition": 'attachment; filename="camera_metadata.json"'},
                )
        return JSONResponse({"error": "camera_metadata.json not found"}, status_code=500)

    # ------------------------------------------------------------------
    # Visitor-friendly bundle: one call returns everything for the
    # public/educational "Reef explorer" panel — top species, hourly
    # activity, current reef conditions.
    # ------------------------------------------------------------------

    @app.get("/api/visitor_stats")
    async def visitor_stats(
        hours: int = Query(24, ge=1, le=24 * 30),
        deployment: str = Query("wahoo_2"),
    ) -> Response:
        pool: AsyncConnectionPool | None = state.get("pool")
        if pool is None:
            return JSONResponse({"error": "database unavailable"}, status_code=503)

        async with pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                # Top species by sighting count (matview, refreshed ~60s)
                await cur.execute(
                    """
                    SELECT name AS latin, count(*)::int AS sightings
                      FROM species_sightings_mat
                     WHERE last_seen >= NOW() - (%s::int || ' hours')::interval
                       AND frame_count >= 3
                     GROUP BY 1
                     ORDER BY sightings DESC
                     LIMIT 8
                    """,
                    (hours,),
                )
                top_rows = await cur.fetchall()

                # Activity by hour (sightings per hour bucket — kid-friendly)
                await cur.execute(
                    """
                    SELECT date_trunc('hour', first_seen) AS hour,
                           count(*)::int AS sightings
                      FROM species_sightings_mat
                     WHERE first_seen >= NOW() - (%s::int || ' hours')::interval
                       AND frame_count >= 3
                     GROUP BY 1
                     ORDER BY 1 ASC
                    """,
                    (hours,),
                )
                hour_rows = await cur.fetchall()

                # Latest water-quality reading
                await cur.execute(
                    """
                    SELECT ts, source,
                           water_temp_c, ph, do_pct,
                           turbidity_fnu, chlorophyll_rfu, spcond_ms_cm
                      FROM sensor_readings
                     WHERE deployment_uri = %s
                     ORDER BY ts DESC
                     LIMIT 1
                    """,
                    (deployment,),
                )
                latest_wq = await cur.fetchone()

                # Totals
                await cur.execute(
                    """
                    SELECT count(*)::int AS sightings,
                           count(DISTINCT species_id) AS unique_species
                      FROM species_sightings_mat
                     WHERE last_seen >= NOW() - (%s::int || ' hours')::interval
                       AND frame_count >= 3
                    """,
                    (hours,),
                )
                totals = await cur.fetchone() or {}

        top_species = [
            {
                "latin": r["latin"],
                "common": common_name(r["latin"]),
                "sightings": r["sightings"],
            }
            for r in top_rows
        ]

        hourly = [
            {"hour": r["hour"].isoformat() if r["hour"] else None,
             "sightings": r["sightings"]}
            for r in hour_rows
        ]

        wq = None
        if latest_wq:
            wq = {
                "ts":            latest_wq["ts"].isoformat() if latest_wq["ts"] else None,
                "source":        latest_wq["source"],
                "water_temp_c":  latest_wq["water_temp_c"],
                "ph":            latest_wq["ph"],
                "do_pct":        latest_wq["do_pct"],
                "turbidity_fnu": latest_wq["turbidity_fnu"],
            }

        return JSONResponse({
            "window_hours": hours,
            "totals": {
                "sightings": (totals or {}).get("sightings") or 0,
                "unique_species": (totals or {}).get("unique_species") or 0,
            },
            "top_species": top_species,
            "hourly_activity": hourly,
            "water_quality": wq,
        })

    @app.get("/api/provenance/current")
    async def provenance_current() -> Response:
        """Most recent provenance tuple seen in detection_events."""
        pool: AsyncConnectionPool | None = state.get("pool")
        if pool is None:
            return JSONResponse({"error": "database unavailable"}, status_code=503)
        sql = """
            SELECT model_version, detector_sha256, classifier_sha256,
                   config_hash, pipeline_git_sha, max(ts) AS last_seen,
                   count(*) AS events
              FROM detection_events
             WHERE model_version IS NOT NULL
             GROUP BY 1,2,3,4,5
             ORDER BY last_seen DESC
             LIMIT 5
        """
        async with pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(sql)
                rows = await cur.fetchall()
        for r in rows:
            if r.get("last_seen"):
                r["last_seen"] = r["last_seen"].isoformat()
        return JSONResponse(rows)

    return app


def main() -> None:
    cfg = DashboardCfg()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    app = build_app()
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level=cfg.log_level.lower())


if __name__ == "__main__":
    main()

"""Wahoo Bay dashboard.

- Proxies the worker's MJPEG stream (so the browser only talks to one origin).
- Serves a small HTML page with live overlay + species counts + recent events.
- Queries Postgres directly for detection history.
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator, Optional

import httpx
import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from psycopg_pool import AsyncConnectionPool
from psycopg.rows import dict_row
from pydantic import BaseModel, Field

from .alerts import SLOChecker, SLO_RULES
from .exports import (
    export_events,
    export_species_counts,
    export_water_quality,
    export_frame_stats,
    export_saved_frames,
    export_corrections,
    export_alerts,
    export_sightings,
    export_hourly_summary,
    export_tracks_timeline,
    export_topk_long,
    export_labeled_corrections,
    fetch_parquet_bytes,
    query_for,
)
from .stats import (
    poisson_count_ci,
    rate_ci,
    mean_ci,
    bootstrap_count_ci,
    species_counts_with_ci,
)


class CorrectionIn(BaseModel):
    event_id: int
    corrected_name: Optional[str] = None
    corrected_species_id: Optional[str] = None
    not_a_fish: bool = False
    confidence: str = Field("probable", pattern="^(certain|probable|uncertain)$")
    reviewer: Optional[str] = None
    notes: Optional[str] = None

log = logging.getLogger("wahoobay.dashboard")

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _env(key: str, default: str) -> str:
    return os.environ.get(key, default)


class DashboardCfg:
    def __init__(self) -> None:
        self.worker_url = _env("WORKER_URL", "http://localhost:8081").rstrip("/")
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

        # SLO checker runs as a background task and upserts rows into `alerts`.
        slo_task: Optional[asyncio.Task] = None
        if state.get("pool") is not None:
            checker = SLOChecker(
                pool=state["pool"],
                worker_url=cfg.worker_url,
                poller_url=cfg.poller_url,
                http_client=client,
            )
            state["slo"] = checker
            slo_task = asyncio.create_task(checker.run_forever(interval_s=30.0), name="slo-checker")

        try:
            yield
        finally:
            if slo_task is not None:
                slo_task.cancel()
                try:
                    await slo_task
                except (asyncio.CancelledError, Exception):
                    pass
            await client.aclose()
            if state.get("pool") is not None:
                await state["pool"].close()

    app = FastAPI(title="Wahoo Bay dashboard", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

    def require_write_token(authorization: Optional[str] = Header(None)) -> None:
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

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(
            request, "index.html", {"worker_url": cfg.worker_url},
        )

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True}

    @app.get("/api/live.json")
    async def live_json() -> Response:
        client: httpx.AsyncClient = state["client"]
        try:
            r = await client.get(f"{cfg.worker_url}/live.json")
            return Response(content=r.content, media_type="application/json", status_code=r.status_code)
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

    @app.get("/api/stream.mjpeg")
    async def stream() -> Response:
        client: httpx.AsyncClient = state["client"]

        async def gen() -> AsyncIterator[bytes]:
            async with client.stream("GET", f"{cfg.worker_url}/stream.mjpeg") as r:
                async for chunk in r.aiter_raw():
                    yield chunk

        return StreamingResponse(
            gen(),
            media_type="multipart/x-mixed-replace; boundary=wahoobay-mjpeg-boundary",
            headers={"Cache-Control": "no-cache, no-store, must-revalidate, private"},
        )

    @app.get("/api/events")
    async def events(
        limit: int = Query(50, ge=1, le=500),
        species_id: Optional[str] = None,
        min_accuracy: float = Query(0.0, ge=0.0, le=1.0),
    ) -> Response:
        pool: Optional[AsyncConnectionPool] = state.get("pool")
        if pool is None:
            return JSONResponse({"error": "database unavailable"}, status_code=503)
        where = ["best_name IS NOT NULL", "best_accuracy >= %s"]
        params: list = [min_accuracy]
        if species_id:
            where.append("best_species_id = %s")
            params.append(species_id)
        params.append(limit)
        sql = f"""
            SELECT id, ts, frame_id, source_name, det_conf, bbox,
                   best_name, best_species_id, best_accuracy, image_path
              FROM detection_events
             WHERE {' AND '.join(where)}
             ORDER BY ts DESC
             LIMIT %s
        """
        async with pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(sql, params)
                rows = await cur.fetchall()
        for r in rows:
            r["ts"] = r["ts"].isoformat() if r["ts"] else None
        return JSONResponse(rows)

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
        pool: Optional[AsyncConnectionPool] = state.get("pool")
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
                  FROM species_sightings
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
        return JSONResponse({"mode": mode, "items": rows})

    # ------------------------------------------------------------------
    # Water quality (sensor_readings table; fed by synthetic generator now,
    # live SenseStream poller later)
    # ------------------------------------------------------------------
    SENSOR_COLUMNS = (
        "water_temp_c", "ph", "do_pct", "chlorophyll_rfu",
        "phycoerythrin_rfu", "turbidity_fnu", "no3_mg_l", "spcond_ms_cm",
    )

    @app.get("/api/water_quality/latest")
    async def water_quality_latest(
        deployment: str = Query("wahoo_2"),
    ) -> Response:
        pool: Optional[AsyncConnectionPool] = state.get("pool")
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
        pool: Optional[AsyncConnectionPool] = state.get("pool")
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
        pool: Optional[AsyncConnectionPool] = state.get("pool")
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
    # Drift monitor (input drift on the video feed)
    # ------------------------------------------------------------------

    @app.get("/api/drift/recent")
    async def drift_recent() -> Response:
        """Current hour vs. 7-day and 28-day baselines from frame_stats_drift view."""
        pool: Optional[AsyncConnectionPool] = state.get("pool")
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
        source_name: Optional[str] = None,
    ) -> Response:
        """Hourly rollup for plotting brightness / detection rate over time."""
        pool: Optional[AsyncConnectionPool] = state.get("pool")
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
        pool: Optional[AsyncConnectionPool] = state.get("pool")
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
        pool: Optional[AsyncConnectionPool] = state.get("pool")
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
        pool: Optional[AsyncConnectionPool] = state.get("pool")
        if pool is None:
            return JSONResponse({"error": "database unavailable"}, status_code=503)
        if not body.not_a_fish and not (body.corrected_name or body.corrected_species_id):
            return JSONResponse(
                {"error": "provide corrected_name, corrected_species_id, or set not_a_fish"},
                status_code=400,
            )
        async with pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    INSERT INTO detection_corrections
                    (event_id, corrected_name, corrected_species_id,
                     not_a_fish, confidence, reviewer, notes)
                    VALUES (%s,%s,%s,%s,%s,%s,%s)
                    RETURNING id, created_at
                    """,
                    (body.event_id, body.corrected_name, body.corrected_species_id,
                     body.not_a_fish, body.confidence, body.reviewer, body.notes),
                )
                row = await cur.fetchone()
        return JSONResponse({
            "id": row["id"],
            "event_id": body.event_id,
            "created_at": row["created_at"].isoformat(),
        })

    @app.get("/api/corrections")
    async def list_corrections(
        limit: int = Query(50, ge=1, le=500),
        reviewer: Optional[str] = None,
    ) -> Response:
        pool: Optional[AsyncConnectionPool] = state.get("pool")
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

    @app.get("/api/corrections/stats")
    async def correction_stats() -> Response:
        pool: Optional[AsyncConnectionPool] = state.get("pool")
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
        pool: Optional[AsyncConnectionPool] = state.get("pool")
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
    async def ack_alert(alert_id: int, reviewer: Optional[str] = None) -> Response:
        pool: Optional[AsyncConnectionPool] = state.get("pool")
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
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        suffix = "_".join(f"{k}-{v}" for k, v in bits.items() if v not in (None, ""))
        return f"wahoobay_{stem}{('_' + suffix) if suffix else ''}_{ts}.csv"

    @app.get("/api/export/events.csv")
    async def export_events_csv(
        hours: int = Query(24, ge=1, le=24 * 365),
        species_id: Optional[str] = None,
        min_accuracy: float = Query(0.0, ge=0.0, le=1.0),
    ) -> Response:
        pool: Optional[AsyncConnectionPool] = state.get("pool")
        if pool is None:
            return JSONResponse({"error": "database unavailable"}, status_code=503)
        gen = export_events(pool, hours, species_id, min_accuracy)
        return _csv_response(gen, _csv_filename("events", hours=hours, species=species_id))

    @app.get("/api/export/species_counts.csv")
    async def export_species_counts_csv(
        hours: int = Query(24, ge=1, le=24 * 365),
    ) -> Response:
        pool: Optional[AsyncConnectionPool] = state.get("pool")
        if pool is None:
            return JSONResponse({"error": "database unavailable"}, status_code=503)
        gen = export_species_counts(pool, hours)
        return _csv_response(gen, _csv_filename("species_counts", hours=hours))

    @app.get("/api/export/water_quality.csv")
    async def export_water_quality_csv(
        hours: int = Query(24, ge=1, le=24 * 365),
        deployment: str = Query("wahoo_2"),
    ) -> Response:
        pool: Optional[AsyncConnectionPool] = state.get("pool")
        if pool is None:
            return JSONResponse({"error": "database unavailable"}, status_code=503)
        gen = export_water_quality(pool, hours, deployment)
        return _csv_response(gen, _csv_filename("water_quality", hours=hours, deployment=deployment))

    @app.get("/api/export/frame_stats.csv")
    async def export_frame_stats_csv(
        hours: int = Query(24, ge=1, le=24 * 365),
    ) -> Response:
        pool: Optional[AsyncConnectionPool] = state.get("pool")
        if pool is None:
            return JSONResponse({"error": "database unavailable"}, status_code=503)
        gen = export_frame_stats(pool, hours)
        return _csv_response(gen, _csv_filename("frame_stats", hours=hours))

    @app.get("/api/export/saved_frames.csv")
    async def export_saved_frames_csv(
        hours: int = Query(24, ge=1, le=24 * 365),
    ) -> Response:
        pool: Optional[AsyncConnectionPool] = state.get("pool")
        if pool is None:
            return JSONResponse({"error": "database unavailable"}, status_code=503)
        gen = export_saved_frames(pool, hours)
        return _csv_response(gen, _csv_filename("saved_frames", hours=hours))

    @app.get("/api/export/corrections.csv")
    async def export_corrections_csv(
        hours: Optional[int] = Query(None, ge=1, le=24 * 365),
        reviewer: Optional[str] = None,
    ) -> Response:
        pool: Optional[AsyncConnectionPool] = state.get("pool")
        if pool is None:
            return JSONResponse({"error": "database unavailable"}, status_code=503)
        gen = export_corrections(pool, hours, reviewer)
        return _csv_response(gen, _csv_filename("corrections", hours=hours, reviewer=reviewer))

    @app.get("/api/export/alerts.csv")
    async def export_alerts_csv(
        include_resolved: bool = Query(False),
    ) -> Response:
        pool: Optional[AsyncConnectionPool] = state.get("pool")
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
        pool: Optional[AsyncConnectionPool] = state.get("pool")
        if pool is None:
            return JSONResponse({"error": "database unavailable"}, status_code=503)
        gen = export_sightings(pool, hours, min_frames)
        return _csv_response(gen, _csv_filename("sightings", hours=hours))

    @app.get("/api/export/hourly_summary.csv")
    async def export_hourly_summary_csv(
        hours: int = Query(168, ge=1, le=24 * 365),
        deployment: str = Query("wahoo_2"),
    ) -> Response:
        pool: Optional[AsyncConnectionPool] = state.get("pool")
        if pool is None:
            return JSONResponse({"error": "database unavailable"}, status_code=503)
        gen = export_hourly_summary(pool, hours, deployment)
        return _csv_response(gen, _csv_filename("hourly_summary", hours=hours, deployment=deployment))

    @app.get("/api/export/tracks_timeline.csv")
    async def export_tracks_timeline_csv(
        hours: int = Query(24, ge=1, le=24 * 365),
        min_frames: int = Query(3, ge=1, le=1000),
    ) -> Response:
        pool: Optional[AsyncConnectionPool] = state.get("pool")
        if pool is None:
            return JSONResponse({"error": "database unavailable"}, status_code=503)
        gen = export_tracks_timeline(pool, hours, min_frames)
        return _csv_response(gen, _csv_filename("tracks_timeline", hours=hours))

    @app.get("/api/export/topk_long.csv")
    async def export_topk_long_csv(
        hours: int = Query(24, ge=1, le=24 * 365),
        min_accuracy: float = Query(0.0, ge=0.0, le=1.0),
    ) -> Response:
        pool: Optional[AsyncConnectionPool] = state.get("pool")
        if pool is None:
            return JSONResponse({"error": "database unavailable"}, status_code=503)
        gen = export_topk_long(pool, hours, min_accuracy)
        return _csv_response(gen, _csv_filename("topk_long", hours=hours))

    @app.get("/api/export/labeled_corrections.csv")
    async def export_labeled_corrections_csv() -> Response:
        pool: Optional[AsyncConnectionPool] = state.get("pool")
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
        species_id: Optional[str] = None,
        min_accuracy: float = Query(0.0, ge=0.0, le=1.0),
        min_frames: int = Query(3, ge=1, le=1000),
        deployment: str = Query("wahoo_2"),
    ) -> Response:
        if resource not in PARQUET_RESOURCES:
            return JSONResponse(
                {"error": f"parquet only available for: {sorted(PARQUET_RESOURCES)}"},
                status_code=400,
            )
        pool: Optional[AsyncConnectionPool] = state.get("pool")
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
        # Try the repo-root location first (where humans edit it).
        for candidate in (
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

    @app.get("/api/provenance/current")
    async def provenance_current() -> Response:
        """Most recent provenance tuple seen in detection_events."""
        pool: Optional[AsyncConnectionPool] = state.get("pool")
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

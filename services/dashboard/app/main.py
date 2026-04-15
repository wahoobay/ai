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
from pathlib import Path
from typing import AsyncIterator, Optional

import httpx
import uvicorn
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from psycopg_pool import AsyncConnectionPool
from psycopg.rows import dict_row

log = logging.getLogger("wahoobay.dashboard")

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _env(key: str, default: str) -> str:
    return os.environ.get(key, default)


class DashboardCfg:
    def __init__(self) -> None:
        self.worker_url = _env("WORKER_URL", "http://localhost:8081").rstrip("/")
        self.database_url = _env(
            "DATABASE_URL",
            "postgresql://wahoobay:wahoobay@localhost:5432/wahoobay",
        )
        self.host = _env("DASHBOARD_HOST", "0.0.0.0")
        self.port = int(_env("DASHBOARD_PORT", "8080"))
        self.log_level = _env("LOG_LEVEL", "INFO")


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
        try:
            yield
        finally:
            await client.aclose()
            if state.get("pool") is not None:
                await state["pool"].close()

    app = FastAPI(title="Wahoo Bay dashboard", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

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
    async def species_counts(hours: int = Query(24, ge=1, le=24 * 14)) -> Response:
        pool: Optional[AsyncConnectionPool] = state.get("pool")
        if pool is None:
            return JSONResponse({"error": "database unavailable"}, status_code=503)
        sql = """
            SELECT best_species_id AS species_id,
                   best_name       AS name,
                   count(*)        AS n,
                   avg(best_accuracy)::real AS mean_acc,
                   max(ts)         AS last_seen
              FROM detection_events
             WHERE ts >= NOW() - (%s::int || ' hours')::interval
               AND best_species_id IS NOT NULL
             GROUP BY 1, 2
             ORDER BY n DESC
             LIMIT 50
        """
        async with pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(sql, (hours,))
                rows = await cur.fetchall()
        for r in rows:
            r["last_seen"] = r["last_seen"].isoformat() if r["last_seen"] else None
        return JSONResponse(rows)

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

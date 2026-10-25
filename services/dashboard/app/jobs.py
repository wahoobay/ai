"""Periodic work, run from an event rather than inside the web process.

    python -m app.jobs refresh_matviews|alert_rules|migrate|seed|status|all
    app.lambda_entry.handler({"job": "..."})   the jobs and migrate Lambdas

The dashboard holds no loops: the materialized-view refresh and the alert rules
used to run as background tasks in every web instance, which does not survive
scaling to several instances or a database that pauses when idle. Each run is
recorded in job_runs. The alert rules read the pollers' status from the
poller_status table, which the pollers write with record_poller_status.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from .alerts import SLOChecker
from .config import settings
from .matviews import MatviewRefresher

log = logging.getLogger("wahoobay.jobs")

MATVIEWS = ("species_sightings_mat", "species_sightings_hourly_mat")


async def refresh_matviews(pool: AsyncConnectionPool) -> dict[str, Any]:
    """Refresh every materialized view; detail = seconds per view."""
    refresher = MatviewRefresher(pool, names=MATVIEWS)
    return {name: round(await refresher.refresh_one(name), 3) for name in MATVIEWS}


async def alert_rules(pool: AsyncConnectionPool) -> dict[str, Any]:
    """Evaluate the SLO rules and upsert `alerts`; detail = {rule: firing}.
    WORKER_URL empty means no camera pipeline here: its rules are skipped."""
    worker_url = os.environ.get("WORKER_URL", "").strip().rstrip("/")
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as http:
        checker = SLOChecker(pool=pool, http_client=http, worker_url=worker_url)
        return await checker.run_once()


POLLER_STATUS_SQL = """
    INSERT INTO poller_status (name, last_ok, last_error, updated_at)
    VALUES (%(name)s, CASE WHEN %(ok)s THEN NOW() END, %(error)s, NOW())
    ON CONFLICT (name) DO UPDATE SET
        last_ok    = CASE WHEN %(ok)s THEN NOW() ELSE poller_status.last_ok END,
        last_error = %(error)s,
        updated_at = NOW()
"""


async def record_poller_status(
    conn: AsyncConnection, name: str, *, ok: bool, error: str = ""
) -> None:
    """What a poller writes after each attempt; the poller_probe_stale rule reads it."""
    await conn.execute(POLLER_STATUS_SQL, {"name": name, "ok": ok, "error": error})


async def seed_sample(pool: AsyncConnectionPool) -> dict[str, Any]:
    """Load the sample dataset from SAMPLE_URL into an empty database (staging's first fill)."""
    from .frames import frames_url
    from .seed import default_source, seed

    site = settings()
    return await asyncio.to_thread(
        seed,
        site.database_url,
        default_source(),
        location_id=site.location_id,
        frames_url=frames_url(),
        deployment_uri=site.deployment_uri,
        admin_password=os.environ.get("ADMIN_PASSWORD") or None,
    )


async def status(pool: AsyncConnectionPool) -> dict[str, Any]:
    """What is going on inside: the schema revision, the last job runs, the pollers,
    row counts. A read for the platform's verify script; not recorded as a run."""
    from psycopg.rows import dict_row

    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT version_num FROM alembic_version")
        revision = (await cur.fetchone() or {}).get("version_num")
        await cur.execute(
            "SELECT job, ran_at, ok, detail FROM job_runs ORDER BY ran_at DESC, id DESC LIMIT 10"
        )
        runs = [{**r, "ran_at": r["ran_at"].isoformat()} for r in await cur.fetchall()]
        await cur.execute("SELECT name, last_ok, last_error FROM poller_status ORDER BY name")
        pollers = [
            {**r, "last_ok": r["last_ok"].isoformat() if r["last_ok"] else None}
            for r in await cur.fetchall()
        ]
        counts = {}
        for table in (
            "detection_events",
            "saved_frames",
            "sensor_readings",
            "weather_readings",
            "users",
            "species_sightings_mat",
        ):
            await cur.execute(f"SELECT count(*) AS n FROM {table}")
            counts[table] = (await cur.fetchone())["n"]
    return {"revision": revision, "job_runs": runs, "pollers": pollers, "counts": counts}


JOBS: dict[str, Callable[[AsyncConnectionPool], Awaitable[dict[str, Any]]]] = {
    "refresh_matviews": refresh_matviews,
    "alert_rules": alert_rules,
    "seed": seed_sample,
    "status": status,
}
READ_ONLY = {"status"}  # not recorded in job_runs
ALL = ("refresh_matviews", "alert_rules")


def run_job(name: str) -> dict[str, Any]:
    """Run one job against the configured database and record it in job_runs.
    Returns {"job", "ok", "detail"}; an unknown job is refused without a connection."""
    if name != "migrate" and name not in JOBS:
        known = ", ".join([*JOBS, "migrate"])
        return {
            "job": name,
            "ok": False,
            "detail": {"error": f"unknown job {name!r}; known: {known}"},
        }
    return asyncio.run(_run(name))


async def _run(name: str) -> dict[str, Any]:
    ok, detail = True, {}
    if name == "migrate":
        # No pool yet: the table this run is recorded in may not exist until it ran.
        from .migrate import upgrade

        try:
            detail = {"revision": await asyncio.to_thread(upgrade)}
        except Exception as e:  # noqa: BLE001 - the run itself is the thing to record
            log.exception("job migrate failed")
            ok, detail = False, {"error": f"{type(e).__name__}: {e}"}
    pool = AsyncConnectionPool(
        settings().database_url,
        min_size=0,
        max_size=2,
        open=False,
        kwargs={"application_name": "wahoobay-jobs"},
    )
    await pool.open()
    try:
        if name != "migrate":
            try:
                detail = await JOBS[name](pool)
            except Exception as e:  # noqa: BLE001
                log.exception("job %s failed", name)
                ok, detail = False, {"error": f"{type(e).__name__}: {e}"}
        if name not in READ_ONLY:
            await _record(pool, name, ok, detail)
    finally:
        await pool.close()
    log.info("job %s: ok=%s %s", name, ok, json.dumps(detail, default=str))
    return {"job": name, "ok": ok, "detail": detail}


async def _record(pool: AsyncConnectionPool, name: str, ok: bool, detail: dict[str, Any]) -> None:
    try:
        async with pool.connection() as conn:
            await conn.execute(
                "INSERT INTO job_runs (job, ok, detail) VALUES (%s, %s, %s)",
                (name, ok, Jsonb(detail)),
            )
    except Exception:  # noqa: BLE001 - a missing job_runs table (database behind) must not fail the job
        log.warning("job %s: could not record the run in job_runs", name, exc_info=True)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    names = list(argv if argv is not None else sys.argv[1:]) or ["all"]
    if names == ["all"]:
        names = list(ALL)
    results = [run_job(name) for name in names]
    for result in results:
        print(json.dumps(result, default=str))
    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

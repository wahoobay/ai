"""Periodic work, run from an event rather than inside the web process.

    python -m app.jobs refresh_matviews|alert_rules|migrate|all
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


JOBS: dict[str, Callable[[AsyncConnectionPool], Awaitable[dict[str, Any]]]] = {
    "refresh_matviews": refresh_matviews,
    "alert_rules": alert_rules,
}
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

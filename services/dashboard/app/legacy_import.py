"""The Java backend's sensor history, brought in once: the import_legacy job.

    python -m app.jobs import_legacy        the jobs Lambda with {"job": "import_legacy"}

The old site's MySQL database holds the sonde's and the weather station's readings
from before this system's poller started. scripts/export_legacy_sensors.py, run where
that database's credentials are, writes them as water_quality.csv and weather.csv;
they are uploaded to LEGACY_URL (default <MODELS_URL>/legacy/) and this job reads them
through common.storage. A table gains only the rows older than its first non-legacy
reading for this deployment (all of them when it has none), tagged source = 'legacy'
and stamped with this site's location. ON CONFLICT DO NOTHING and that cut-off make a
second run insert nothing. A missing file is skipped and reported.

A run has a time budget (budget_s, inside the Lambda's timeout), checked between
batches: once it is spent the run stops after the batch in hand and reports
partial: true with the counts so far. Each batch commits on its own and the rows
already imported are skipped, so the next run carries on where this one stopped;
every run inserts at least one batch, however small its budget.
"""

from __future__ import annotations

import asyncio
import csv
import io
import os
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from common.storage import join, open_read
from psycopg_pool import AsyncConnectionPool

BATCH = 500  # rows per INSERT batch and commit: well under a second each
BUDGET_S = 840.0  # a run's time, inside the jobs Lambda's 900 s timeout

# (result key, table, file, the columns after ts): the export script writes exactly these headers.
FILES = (
    (
        "water",
        "sensor_readings",
        "water_quality.csv",
        (
            "water_temp_c",
            "ph",
            "do_pct",
            "chlorophyll_rfu",
            "phycoerythrin_rfu",
            "turbidity_fnu",
            "spcond_ms_cm",
        ),
    ),
    (
        "weather",
        "weather_readings",
        "weather.csv",
        (
            "bar_press_hpa",
            "wind_dir_avg_deg",
            "wind_dir_min_deg",
            "wind_dir_max_deg",
            "wind_speed_avg_ms",
            "wind_speed_min_ms",
            "wind_speed_max_ms",
            "air_temp_c",
            "rel_humidity_pct",
            "rain_accum_mm",
        ),
    ),
)


def default_legacy_url(env: Mapping[str, str] = os.environ) -> str:
    """LEGACY_URL, or the models bucket's legacy/ prefix; neither set is an error, not a guess."""
    url = env.get("LEGACY_URL", "").strip()
    if url:
        return url
    models = env.get("MODELS_URL", "").strip()
    if not models:
        raise RuntimeError(
            "set LEGACY_URL, or MODELS_URL (the files are then <MODELS_URL>/legacy/)"
        )
    return join(models, "legacy/")


def _parse(name: str, text: str, columns: tuple[str, ...]) -> list[tuple]:
    """(ts, *values) per data line; an empty cell is None."""
    reader = csv.reader(io.StringIO(text))
    expected = ["ts", *columns]
    header = next(reader, None)
    if header != expected:
        raise ValueError(f"{name}: the header is {header!r}, expected {','.join(expected)}")
    rows = []
    for line_no, line in enumerate(reader, 2):
        if not line:
            continue
        if len(line) != len(expected):
            raise ValueError(f"{name}:{line_no}: {len(line)} fields, expected {len(expected)}")
        ts = datetime.fromisoformat(line[0].replace("Z", "+00:00"))
        ts = ts if ts.tzinfo else ts.replace(tzinfo=UTC)
        rows.append((ts, *(float(v) if v.strip() else None for v in line[1:])))
    return rows


class _Budget:
    """A run's time: spent once the deadline has passed and the run has done a batch."""

    def __init__(self, seconds: float) -> None:
        self.deadline = time.monotonic() + seconds
        self.batches = 0

    def spent(self) -> bool:
        return self.batches > 0 and time.monotonic() >= self.deadline


async def _load(
    pool: AsyncConnectionPool,
    table: str,
    columns: tuple[str, ...],
    rows: list[tuple],
    budget: _Budget,
    *,
    deployment_uri: str,
    location_id: str,
) -> tuple[int, bool]:
    """Insert the rows older than the table's first non-legacy reading that are not in yet;
    (the number inserted, whether the file is done before the budget was spent)."""
    sql = (
        f"INSERT INTO {table} (ts, deployment_uri, {', '.join(columns)}, source, location_id) "
        f"VALUES (%s, %s, {', '.join(['%s'] * len(columns))}, 'legacy', %s) "
        "ON CONFLICT (deployment_uri, ts) DO NOTHING"
    )
    inserted = 0
    async with pool.connection() as conn:
        found = await conn.execute(
            f"SELECT min(ts) FROM {table} WHERE deployment_uri = %s AND source <> 'legacy'",
            (deployment_uri,),
        )
        (first_live,) = await found.fetchone()
        # An earlier run's rows: skipped here, so a run continues where the last one stopped.
        found = await conn.execute(
            f"SELECT ts FROM {table} WHERE deployment_uri = %s AND source = 'legacy'",
            (deployment_uri,),
        )
        imported = {ts for (ts,) in await found.fetchall()}
        todo = [
            r for r in rows if (first_live is None or r[0] < first_live) and r[0] not in imported
        ]
        for start in range(0, len(todo), BATCH):
            if budget.spent():
                return inserted, False
            batch = todo[start : start + BATCH]
            async with conn.cursor() as cur:
                await cur.executemany(
                    sql, [(r[0], deployment_uri, *r[1:], location_id) for r in batch]
                )
                inserted += cur.rowcount
            await conn.commit()
            budget.batches += 1
    return inserted, True


async def import_legacy(
    pool: AsyncConnectionPool,
    *,
    legacy_url: str,
    deployment_uri: str,
    location_id: str,
    budget_s: float = BUDGET_S,
) -> dict[str, Any]:
    """Load water_quality.csv and weather.csv from legacy_url within budget_s seconds:
    {"water": {"read", "inserted"}, "weather": {...}, "partial": bool}, with "missing" for
    a file not there. partial: true means the budget ran out (a file not reached is left
    out); the next run carries on."""
    budget = _Budget(budget_s)
    out: dict[str, Any] = {}
    for key, table, name, columns in FILES:
        url = join(legacy_url, name)
        try:
            data = await asyncio.to_thread(open_read, url)
        except FileNotFoundError:
            out[key] = {"read": 0, "inserted": 0, "missing": url}
            continue
        rows = _parse(name, data.decode("utf-8-sig"), columns)
        inserted, done = await _load(
            pool,
            table,
            columns,
            rows,
            budget,
            deployment_uri=deployment_uri,
            location_id=location_id,
        )
        out[key] = {"read": len(rows), "inserted": inserted}
        if not done:
            out["partial"] = True
            return out
    out["partial"] = False
    return out

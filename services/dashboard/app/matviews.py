"""Background refresher for materialized views.

The user-facing dashboard reads from `species_sightings_mat` (a
materialized mirror of the `species_sightings` view) so endpoint queries
finish in milliseconds instead of seq-scanning 30 M+ rows on every
request. This module owns the refresh loop that keeps the matview from
going stale.

Refresh uses `CONCURRENTLY` so reads are never blocked. That requires a
unique index on the matview, which db/init.sql already creates on
track_id.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Iterable

from psycopg_pool import AsyncConnectionPool

log = logging.getLogger("wahoobay.dashboard.matviews")


class MatviewRefresher:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        names: Iterable[str] = ("species_sightings_mat",),
        interval_s: float = 60.0,
    ) -> None:
        self.pool = pool
        self.names = tuple(names)
        self.interval_s = max(10.0, float(interval_s))

    async def refresh_one(self, name: str) -> float:
        """Refresh one matview. Returns wall-clock seconds taken. Tries
        CONCURRENTLY first; falls back to a blocking refresh if the matview
        was never populated (CONCURRENTLY requires existing rows)."""
        # psycopg won't parametrise object names, so the caller is responsible
        # for vetting `name` — this module only ever passes hard-coded constants.
        sql_concurrent = f"REFRESH MATERIALIZED VIEW CONCURRENTLY {name}"
        sql_blocking = f"REFRESH MATERIALIZED VIEW {name}"
        t0 = time.monotonic()
        async with self.pool.connection() as conn:
            await conn.set_autocommit(True)
            try:
                async with conn.cursor() as cur:
                    await cur.execute(sql_concurrent)
            except Exception as e:
                log.warning("CONCURRENTLY refresh of %s failed (%s); retrying blocking", name, e)
                async with conn.cursor() as cur:
                    await cur.execute(sql_blocking)
        return time.monotonic() - t0

    async def run_forever(self) -> None:
        log.info("matview refresher: cadence=%.0fs targets=%s", self.interval_s, list(self.names))
        # Don't run an initial refresh on startup — db/init.sql already
        # creates the matview WITH DATA, so the first read sees fresh data
        # already; sleep through the first cadence before refreshing.
        while True:
            try:
                await asyncio.sleep(self.interval_s)
                for name in self.names:
                    secs = await self.refresh_one(name)
                    log.debug("matview refreshed: %s in %.1fs", name, secs)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("matview refresher: unexpected error; continuing")

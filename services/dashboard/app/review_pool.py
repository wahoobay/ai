"""Keeps review_items topped up (spec §6).

A background task in the dashboard's lifespan. Each run:

1. refreshes the progress counts (training-set baseline + verified boxes);
2. retires open items outside the admin's date window;
3. tops up the `check` lane species by species, thinnest first;
4. tops up the `identify` lane with the largest unguessed box per frame.

Every statement the pool issues -- the big-table probes, the window read,
the retire update, the small per-box lookups, and the progress refresh --
runs inside its own transaction with a 5 s timeout set by
set_config(..., true): the limit ends with the transaction and never
reaches the next borrower of the pooled connection (a session-level SET
would, and the matview refresher's long REFRESH would then die). For the
same reason every statement here runs inside `conn.transaction()`: a
statement outside one opens an implicit transaction, the next block
becomes a savepoint, and its local timeout would outlive it. A cancelled
statement -- the timeout itself, or a lock wait it gave up on -- is
logged and counted, never left to end the run; a run is also capped in
probes and wall time, and a run that finds the previous one still going
returns at once.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from .review_labels import resolve_all

log = logging.getLogger("wahoobay.dashboard.review_pool")

CHECK_PROBE_SQL = """
    SELECT de.id, de.ts, de.source_name, de.bbox, de.best_accuracy, sf.id AS saved_frame_id
      FROM detection_events de
      JOIN saved_frames sf
        ON sf.frame_id = de.frame_id AND sf.source_name = de.source_name AND sf.ts = de.ts
     WHERE de.best_species_id = %s AND de.ts <= %s AND de.ts >= %s
     ORDER BY de.ts DESC
     LIMIT 50
"""
IDENTIFY_FRAMES_SQL = """
    SELECT id, ts, source_name, frame_id
      FROM saved_frames
     WHERE ts <= %s AND ts >= %s AND num_fish > 0
     ORDER BY ts DESC
     LIMIT 40
"""
IDENTIFY_BOXES_SQL = """
    SELECT f.saved_frame_id, de.id, de.ts, de.source_name, de.bbox
      FROM unnest(%s::bigint[], %s::text[], %s::timestamptz[], %s::bigint[])
           AS f(saved_frame_id, source_name, ts, frame_id)
      JOIN detection_events de
        ON de.source_name = f.source_name AND de.ts = f.ts AND de.frame_id = f.frame_id
     WHERE de.best_species_id IS NULL AND de.det_conf >= %s
"""
_FREE_EVENTS_SQL = """
    SELECT id FROM unnest(%s::bigint[]) AS c(id)
    EXCEPT SELECT event_id FROM detection_corrections WHERE event_id = ANY(%s)
    EXCEPT SELECT event_id FROM review_items WHERE event_id = ANY(%s)
"""
_TOO_CLOSE_SQL = """
    SELECT 1 FROM review_items
     WHERE source_name = %s AND lane = %s AND ts BETWEEN %s AND %s
       AND (%s::text IS NULL OR route_species = %s)
     LIMIT 1
"""
_INSERT_ITEM_SQL = """
    INSERT INTO review_items (event_id, pass, lane, saved_frame_id, source_name, ts, bbox,
                              predicted, predicted_p, route_species)
    VALUES (%s, 1, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (event_id, pass) DO NOTHING
    RETURNING id
"""

# A probe that times out told us nothing about whether the species has
# candidates -- unlike an empty result, which means "found nothing in the
# window" and earns the day-long skip below. Back off for much less than
# that so a species doesn't come back thin because one run hit lock
# contention.
TIMEOUT_BACKOFF_S = 3600


@dataclass
class PoolSettings:
    interval_s: float = 300.0
    max_probes: int = 200
    max_run_s: float = 30.0
    check_open_per_species: int = 20
    identify_open: int = 500
    spacing_s: int = 120
    min_box_px: int = 32
    identify_min_det_conf: float = 0.4
    target: int = 200
    statement_timeout_ms: int = 5000
    recent_exclusion_s: int = 600
    empty_species_skip_s: int = 86400

    @classmethod
    def from_cfg(cls, cfg) -> PoolSettings:
        return cls(interval_s=cfg.review_pool_interval_s,
                   max_probes=cfg.review_pool_max_probes,
                   max_run_s=cfg.review_pool_max_run_s,
                   check_open_per_species=cfg.review_check_open_per_species,
                   identify_open=cfg.review_identify_open,
                   spacing_s=cfg.review_spacing_s,
                   min_box_px=cfg.review_min_box_px,
                   identify_min_det_conf=cfg.review_identify_min_det_conf,
                   target=cfg.review_target)


def big_enough(bbox, min_px: int) -> bool:
    x1, y1, x2, y2 = bbox
    return min(x2 - x1, y2 - y1) >= min_px


def _area(bbox) -> int:
    return max(0, bbox[2] - bbox[0]) * max(0, bbox[3] - bbox[1])


async def read_window(conn) -> tuple[datetime | None, datetime | None]:
    """The admin's date window as stored, or (None, None) when unset."""
    async with conn.cursor() as cur:
        await cur.execute("SELECT value FROM review_settings WHERE key = 'window'")
        row = await cur.fetchone()
    value = row[0] if row else None
    if not value:
        return None, None
    start = datetime.fromisoformat(value["start"]) if value.get("start") else None
    end = datetime.fromisoformat(value["end"]) if value.get("end") else None
    return start, end


async def retire_outside_window(conn, start: datetime, end: datetime) -> int:
    async with conn.cursor() as cur:
        await cur.execute(
            """UPDATE review_items SET status = 'retired', retired_reason = 'outside_window'
                WHERE status = 'open' AND pass = 1 AND (ts < %s OR ts > %s)""",
            (start, end))
        return cur.rowcount


class ReviewPool:
    def __init__(self, db, settings: PoolSettings, catalogue_store, baseline_path,
                 rng: random.Random | None = None) -> None:
        self.db = db
        self.s = settings
        self.catalogue_store = catalogue_store
        self.baseline_path = Path(baseline_path)
        self.rng = rng or random.Random()
        self._lock = asyncio.Lock()
        self._baseline: dict[str, int] = {}
        self._baseline_stamp: object = None
        self._baseline_catalogue: object = None
        self._verified: dict[str, int] = {}
        self._empty_until: dict[str, float] = {}
        self._last_window: tuple[datetime, datetime] | None = None
        self.last_run: dict | None = None

    # ---- progress ---------------------------------------------------------

    def _load_baseline(self) -> None:
        try:
            stamp = self.baseline_path.stat().st_mtime_ns
        except FileNotFoundError:
            if self._baseline_stamp != "missing":
                log.warning("review pool: %s not found; training-set counts taken as 0",
                            self.baseline_path)
            self._baseline = {}
            self._baseline_stamp, self._baseline_catalogue = "missing", None
            return
        catalogue = self.catalogue_store.get()
        # The manifest's own mtime isn't the only thing that can make these
        # counts stale: a live catalogue rebuild (the sheet poller rewrites
        # species_manifest.json daily) can change which accepted name a raw
        # label resolves to without touching the baseline file at all, so
        # the cache follows the catalogue object's identity too.
        if stamp == self._baseline_stamp and catalogue is self._baseline_catalogue:
            return
        counts: dict[str, int] = {}
        for rec in json.loads(self.baseline_path.read_text()):
            # Crops merged back from a verified snapshot carry source_kind
            # 'review:...'. They are already counted as verified boxes.
            if str(rec.get("source_kind") or "").startswith("review:"):
                continue
            label = rec.get("label")
            if label:
                name = catalogue.accepted(label)
                counts[name] = counts.get(name, 0) + 1
        self._baseline, self._baseline_stamp, self._baseline_catalogue = counts, stamp, catalogue

    async def refresh_progress(self, stats: dict | None = None) -> None:
        stats = {"timeouts": 0} if stats is None else stats
        self._load_baseline()
        try:
            async with self.db.connection() as conn, conn.transaction():
                await conn.execute("SELECT set_config('statement_timeout', %s, true)",
                                   (str(self.s.statement_timeout_ms),))
                resolved = await resolve_all(conn)
                async with conn.cursor() as cur:
                    await cur.execute(
                        """SELECT label, count(*) FROM review_added_boxes
                            WHERE removed_at IS NULL AND outcome = 'species'
                              AND confidence <> 'uncertain'
                            GROUP BY label""")
                    added = await cur.fetchall()
        except psycopg.errors.QueryCanceled:
            stats["timeouts"] += 1
            log.warning("review pool: refreshing progress counts hit the %d ms statement "
                        "timeout; keeping the previous counts", self.s.statement_timeout_ms)
            return
        verified: dict[str, int] = {}
        for r in resolved.values():
            if r.is_verified and r.species and r.confidence != "uncertain":
                verified[r.species] = verified.get(r.species, 0) + 1
        for label, n in added:
            verified[label] = verified.get(label, 0) + n
        self._verified = verified

    def baseline(self, name: str) -> int:
        return self._baseline.get(name, 0)

    def verified(self, name: str) -> int:
        return self._verified.get(name, 0)

    def count(self, name: str | None) -> int:
        return self.baseline(name) + self.verified(name) if name else 0

    def note_verified(self, name: str | None) -> None:
        if name:
            self._verified[name] = self._verified.get(name, 0) + 1

    def species_order(self) -> list[str]:
        """Every catalogue species still below target, thinnest first."""
        below = [n for n in self.catalogue_store.get().names() if self.count(n) < self.s.target]
        return sorted(below, key=lambda n: (self.count(n), n))

    # ---- the run ------------------------------------------------------------

    async def run_forever(self) -> None:
        log.info("review pool: every %.0fs, target %d per species",
                 self.s.interval_s, self.s.target)
        while True:
            try:
                log.info("review pool: %s", await self.run_once())
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("review pool: unexpected error; continuing")
            await asyncio.sleep(self.s.interval_s)

    async def run_once(self) -> dict:
        if self._lock.locked():
            return {"skipped": "previous run still going"}
        async with self._lock:
            t0 = time.monotonic()
            stats = {"check_added": 0, "identify_added": 0, "probes": 0, "retired": 0,
                     "timeouts": 0, "errors": 0}
            try:
                await self.refresh_progress(stats)
                async with self.db.connection() as conn:
                    window = await self._window(conn, stats)
                    if window is None:
                        stats["note"] = "no saved frames in the window yet"
                    else:
                        # A window that moved since the last run makes any
                        # earlier "found nothing" skip (or timeout backoff)
                        # unreliable -- it may have looked at data that's no
                        # longer, or wasn't yet, in range.
                        if window != self._last_window:
                            self._empty_until.clear()
                            self._last_window = window
                        try:
                            async with conn.transaction():
                                await conn.execute(
                                    "SELECT set_config('statement_timeout', %s, true)",
                                    (str(self.s.statement_timeout_ms),))
                                stats["retired"] = await retire_outside_window(conn, *window)
                        except psycopg.errors.QueryCanceled:
                            stats["timeouts"] += 1
                            log.warning(
                                "review pool: retiring outside-window items hit the %d ms "
                                "statement timeout; will retry next run",
                                self.s.statement_timeout_ms)
                        await self._fill_check(conn, window, stats, t0)
                        await self._fill_identify(conn, window, stats, t0)
            except asyncio.CancelledError:
                raise
            except Exception:
                stats["errors"] += 1
                log.exception("review pool: run failed")
            stats["duration_s"] = round(time.monotonic() - t0, 2)
            stats["finished_at"] = datetime.now(UTC).isoformat()
            self.last_run = stats
            return stats

    async def _window(self, conn, stats: dict) -> tuple[datetime, datetime] | None:
        try:
            async with conn.transaction():
                await conn.execute("SELECT set_config('statement_timeout', %s, true)",
                                   (str(self.s.statement_timeout_ms),))
                start, end = await read_window(conn)
                if start is None:
                    async with conn.cursor() as cur:
                        await cur.execute("SELECT min(ts) FROM saved_frames")
                        start = (await cur.fetchone())[0]
        except psycopg.errors.QueryCanceled:
            stats["timeouts"] += 1
            log.warning("review pool: reading the review window hit the %d ms statement "
                        "timeout", self.s.statement_timeout_ms)
            return None
        latest = datetime.now(UTC) - timedelta(seconds=self.s.recent_exclusion_s)
        end = min(end, latest) if end else latest
        if start is None or start >= end:
            return None
        return start, end

    def _exhausted(self, stats: dict, t0: float) -> bool:
        return (stats["probes"] >= self.s.max_probes
                or time.monotonic() - t0 >= self.s.max_run_s)

    def _probe_time(self, window) -> datetime:
        start, end = window
        return start + (end - start) * self.rng.random()

    async def _exec(self, conn, sql: str, params, stats: dict, *, dict_rows: bool = False
                    ) -> list | None:
        """Run one statement inside its own transaction-local timeout.
        None means it was cancelled -- the timeout itself, or a lock wait
        the timeout gave up on -- which is counted and logged here rather
        than raised, so one stuck statement can't end the run."""
        try:
            async with conn.transaction():
                await conn.execute("SELECT set_config('statement_timeout', %s, true)",
                                   (str(self.s.statement_timeout_ms),))
                kwargs = {"row_factory": dict_row} if dict_rows else {}
                async with conn.cursor(**kwargs) as cur:
                    await cur.execute(sql, params)
                    return await cur.fetchall()
        except psycopg.errors.QueryCanceled:
            stats["timeouts"] += 1
            log.warning("review pool: a statement hit the %d ms statement timeout",
                        self.s.statement_timeout_ms)
            return None

    async def _probe(self, conn, sql: str, params, stats: dict) -> list[dict] | None:
        """One bounded read against the big tables, counted toward the
        run's probe budget. None means it timed out."""
        stats["probes"] += 1
        return await self._exec(conn, sql, params, stats, dict_rows=True)

    async def _rows(self, conn, sql: str, params, stats: dict) -> list:
        """One bounded read against a small, already-indexed table --
        review_items or detection_corrections by id, never a scan of the
        big tables -- so it isn't counted against the probe budget. A
        timeout here means lock contention, not table size, and is
        treated the same as finding nothing."""
        return await self._exec(conn, sql, params, stats) or []

    async def _free(self, conn, ids: list[int], stats: dict) -> set[int]:
        if not ids:
            return set()
        return {r[0] for r in await self._rows(conn, _FREE_EVENTS_SQL, (ids, ids, ids), stats)}

    async def _too_close(self, conn, stats: dict, *, source: str, lane: str, ts: datetime,
                         species: str | None) -> bool:
        gap = timedelta(seconds=self.s.spacing_s)
        return bool(await self._rows(conn, _TOO_CLOSE_SQL,
                                     (source, lane, ts - gap, ts + gap, species, species), stats))

    async def _insert(self, conn, stats: dict, *, event_id, lane, saved_frame_id, source, ts,
                      bbox, predicted, predicted_p, route) -> bool:
        return bool(await self._rows(conn, _INSERT_ITEM_SQL, (
            event_id, lane, saved_frame_id, source, ts, list(bbox), predicted, predicted_p,
            route), stats))

    async def _fill_check(self, conn, window, stats: dict, t0: float) -> None:
        catalogue = self.catalogue_store.get()
        open_counts = {name: n for name, n in await self._rows(conn, """
            SELECT route_species, count(*) FROM review_items
             WHERE lane = 'check' AND pass = 1 AND status IN ('open', 'claimed')
             GROUP BY route_species""", None, stats)}
        # The model writes class names with spaces (classifier_v2.py): folder
        # 'Pomatomus_saltarix' appears in detection_events as 'Pomatomus
        # saltarix' and counts toward the accepted name.
        classes = sorted({(e.name, e.class_dir.replace("_", " "))
                          for e in catalogue.entries if e.class_dir},
                         key=lambda p: (self.count(p[0]), p[0]))
        now = time.time()
        for name, raw in classes:
            if self._exhausted(stats, t0):
                return
            if self.count(name) >= self.s.target or self._empty_until.get(raw, 0) > now:
                continue
            need = self.s.check_open_per_species - open_counts.get(name, 0)
            if need > 0:
                open_counts[name] = open_counts.get(name, 0) + await self._probe_check(
                    conn, window, name, raw, need, stats, t0)

    async def _probe_check(self, conn, window, name: str, raw: str, need: int,
                           stats: dict, t0: float) -> int:
        rows = await self._probe(conn, CHECK_PROBE_SQL,
                                 (raw, self._probe_time(window), window[0]), stats)
        if rows is None:
            # Timed out, not "found nothing" -- the fallback probe would
            # only spend more of the run's budget on the same question.
            # Back off briefly and let a later run try again.
            log.warning("review pool: check-lane probe for %r timed out; backing off %d s",
                        raw, TIMEOUT_BACKOFF_S)
            self._empty_until[raw] = time.time() + TIMEOUT_BACKOFF_S
            return 0
        if rows == []:
            # Nothing before the random point: look once more from the end
            # of the window before deciding the species has no candidates
            # -- unless the run's probe/time budget is already spent.
            if self._exhausted(stats, t0):
                return 0
            rows = await self._probe(conn, CHECK_PROBE_SQL, (raw, window[1], window[0]), stats)
            if rows is None:
                log.warning(
                    "review pool: check-lane fallback probe for %r timed out; backing off %d s",
                    raw, TIMEOUT_BACKOFF_S)
                self._empty_until[raw] = time.time() + TIMEOUT_BACKOFF_S
                return 0
            if rows == []:
                self._empty_until[raw] = time.time() + self.s.empty_species_skip_s
        if not rows:
            return 0
        rows = [r for r in rows if big_enough(r["bbox"], self.s.min_box_px)]
        free = await self._free(conn, [r["id"] for r in rows], stats)
        added = 0
        for r in rows:
            if added >= need:
                break
            if r["id"] not in free or await self._too_close(
                    conn, stats, source=r["source_name"], lane="check", ts=r["ts"],
                    species=name):
                continue
            if await self._insert(conn, stats, event_id=r["id"], lane="check",
                                  saved_frame_id=r["saved_frame_id"], source=r["source_name"],
                                  ts=r["ts"], bbox=r["bbox"], predicted=name,
                                  predicted_p=r["best_accuracy"], route=name):
                added += 1
        stats["check_added"] += added
        return added

    async def _fill_identify(self, conn, window, stats: dict, t0: float) -> None:
        open_n = (await self._rows(conn, """
            SELECT count(*) FROM review_items
             WHERE lane = 'identify' AND pass = 1 AND status IN ('open', 'claimed')""",
                                   None, stats))[0][0]
        while open_n < self.s.identify_open and not self._exhausted(stats, t0):
            frames = await self._probe(conn, IDENTIFY_FRAMES_SQL,
                                       (self._probe_time(window), window[0]), stats)
            if not frames:
                continue
            boxes = await self._probe(conn, IDENTIFY_BOXES_SQL, (
                [f["id"] for f in frames], [f["source_name"] for f in frames],
                [f["ts"] for f in frames], [f["frame_id"] for f in frames],
                self.s.identify_min_det_conf), stats)
            if not boxes:
                continue
            boxes = [b for b in boxes if big_enough(b["bbox"], self.s.min_box_px)]
            free = await self._free(conn, [b["id"] for b in boxes], stats)
            best: dict[int, dict] = {}
            for b in boxes:
                current = best.get(b["saved_frame_id"])
                if b["id"] in free and (current is None
                                        or _area(b["bbox"]) > _area(current["bbox"])):
                    best[b["saved_frame_id"]] = b
            for saved_frame_id, b in best.items():
                if open_n >= self.s.identify_open:
                    break
                if await self._too_close(conn, stats, source=b["source_name"], lane="identify",
                                         ts=b["ts"], species=None):
                    continue
                if await self._insert(conn, stats, event_id=b["id"], lane="identify",
                                      saved_frame_id=saved_frame_id, source=b["source_name"],
                                      ts=b["ts"], bbox=b["bbox"], predicted=None,
                                      predicted_p=None, route=None):
                    open_n += 1
                    stats["identify_added"] += 1

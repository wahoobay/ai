"""SLO alert checker.

Run by the jobs runner (``python -m app.jobs alert_rules``; the jobs Lambda on
the platform's cadence), not inside the web process. Each rule queries Postgres
or the worker's health endpoint, computes a pass/fail, and upserts into the
``alerts`` table. Rules about the camera pipeline are skipped when no worker is
configured (WORKER_URL empty: a sensors-only site, or staging).

Current rules:
  - pipeline_silence:       no detection_events inserted in > 5 min
  - inference_latency_p95:  worker /stats reports last_infer_ms > 100 ms
  - poller_probe_stale:     a poller's last OK in poller_status is > 30 min old
  - frame_stats_stalled:    no frame_stats rows in > 5 min
  - drift_luma_delta:       |luma_1h - luma_7d| > 25   (biofouling or lighting change)
  - drift_fish_rate_crash:  fish_rate_1h < 0.5 * fish_rate_7d and samples>=100

Rules are data-driven in SLO_RULES; adding a new one is one dataclass entry.
Alerts auto-resolve (``resolved_at`` set) once the condition clears.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

log = logging.getLogger("wahoobay.slo")


RuleResult = tuple[bool, str, dict]  # (firing, message, details)


@dataclass
class SLORule:
    name: str
    severity: str  # info | warning | critical
    check: Callable[[SLOChecker], Awaitable[RuleResult]]
    description: str = ""
    needs_worker: bool = False  # about the camera pipeline: skipped when WORKER_URL is empty


# ---------------------------------------------------------------------------
# Rule checks
# ---------------------------------------------------------------------------


async def _check_pipeline_silence(c: SLOChecker) -> RuleResult:
    async with c.pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT max(ts) FROM detection_events")
            (last,) = await cur.fetchone() or (None,)
    if last is None:
        return (True, "no detection_events ever written", {"last_event_ts": None})
    age_s = (datetime.now(UTC) - last).total_seconds()
    if age_s > 300:
        return (
            True,
            f"no detections in {age_s/60:.1f} min (threshold 5 min)",
            {"last_event_ts": last.isoformat(), "age_seconds": age_s},
        )
    return (False, "ok", {"last_event_ts": last.isoformat(), "age_seconds": age_s})


async def _check_frame_stats_stalled(c: SLOChecker) -> RuleResult:
    async with c.pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT max(ts) FROM frame_stats")
            (last,) = await cur.fetchone() or (None,)
    if last is None:
        return (False, "no frame_stats yet (waiting for worker)", {})
    age_s = (datetime.now(UTC) - last).total_seconds()
    if age_s > 300:
        return (
            True,
            f"frame_stats sampler silent for {age_s/60:.1f} min",
            {"last_ts": last.isoformat(), "age_seconds": age_s},
        )
    return (False, "ok", {"age_seconds": age_s})


async def _check_inference_latency(c: SLOChecker) -> RuleResult:
    try:
        r = await c.http.get(f"{c.worker_url}/stats", timeout=5.0)
        if r.status_code != 200:
            return (False, f"worker /stats returned {r.status_code}", {})
        j = r.json()
    except Exception as e:
        return (False, f"worker unreachable: {e}", {})
    # we expose last_infer_ms (per-frame) — treat a single sample as p95
    # proxy until we add a real histogram. Threshold is deliberately loose.
    last_ms = j.get("last_infer_ms")
    if last_ms is None:
        return (False, "no inference samples yet", {})
    if last_ms > 100.0:
        return (
            True,
            f"last inference took {last_ms:.1f} ms (threshold 100)",
            {"last_infer_ms": last_ms},
        )
    return (False, "ok", {"last_infer_ms": last_ms})


async def _check_poller_probe_stale(c: SLOChecker) -> RuleResult:
    """The pollers write poller_status (jobs.record_poller_status). A poller whose
    last OK is older than 30 min, or that has only ever reported errors, fires;
    a site where no poller has reported yet is not an alert."""
    async with c.pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT name, last_ok, last_error FROM poller_status ORDER BY name")
            rows = await cur.fetchall()
    if not rows:
        return (False, "no poller has reported yet", {})
    now = datetime.now(UTC)
    stale: dict[str, str] = {}
    for row in rows:
        if row["last_ok"] is None:
            stale[row["name"]] = f"never ok: {row['last_error'] or 'no detail'}"
            continue
        age_s = (now - row["last_ok"]).total_seconds()
        if age_s > 1800:
            stale[row["name"]] = (
                f"last ok {age_s / 60:.1f} min ago; {row['last_error'] or 'no error recorded'}"
            )
    last_ok = {r["name"]: r["last_ok"].isoformat() if r["last_ok"] else None for r in rows}
    if stale:
        message = "; ".join(f"{name}: {why}" for name, why in stale.items())
        return (True, message, {"last_ok": last_ok, "stale": stale})
    return (False, "ok", {"last_ok": last_ok})


async def _check_drift_luma(c: SLOChecker) -> RuleResult:
    async with c.pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT * FROM frame_stats_drift LIMIT 1")
            row = await cur.fetchone()
    if not row or row.get("luma_1h") is None or row.get("luma_7d") is None:
        return (False, "baseline not yet established", {})
    delta = float(row["luma_1h"]) - float(row["luma_7d"])
    if abs(delta) > 25.0:
        return (
            True,
            f"luma shift of {delta:+.1f} vs 7d baseline (threshold ±25)",
            {"luma_1h": row["luma_1h"], "luma_7d": row["luma_7d"], "delta": delta},
        )
    return (False, "ok", {"delta": delta})


async def _check_drift_fish_rate(c: SLOChecker) -> RuleResult:
    async with c.pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT * FROM frame_stats_drift LIMIT 1")
            row = await cur.fetchone()
    if not row or row.get("fish_rate_1h") is None or row.get("fish_rate_7d") is None:
        return (False, "baseline not yet established", {})
    if (row.get("samples") or 0) < 100:
        return (False, "insufficient samples in last hour", {})
    fr1, fr7 = float(row["fish_rate_1h"]), float(row["fish_rate_7d"])
    if fr7 > 0.01 and fr1 < 0.5 * fr7:
        return (
            True,
            f"fish-frame rate collapsed: {fr1*100:.1f}% vs 7d {fr7*100:.1f}%",
            {"fish_rate_1h": fr1, "fish_rate_7d": fr7},
        )
    return (False, "ok", {"fish_rate_1h": fr1, "fish_rate_7d": fr7})


SLO_RULES: list[SLORule] = [
    SLORule(
        "pipeline_silence",
        "critical",
        _check_pipeline_silence,
        "No detection_events inserted in the last 5 min.",
        needs_worker=True,
    ),
    SLORule(
        "frame_stats_stalled",
        "warning",
        _check_frame_stats_stalled,
        "Drift sampler stopped producing frame_stats rows.",
        needs_worker=True,
    ),
    SLORule(
        "inference_latency_p95",
        "warning",
        _check_inference_latency,
        "Last per-frame inference exceeded 100 ms.",
        needs_worker=True,
    ),
    SLORule(
        "poller_probe_stale",
        "warning",
        _check_poller_probe_stale,
        "SenseStream poller hasn't successfully probed the deployment recently.",
    ),
    SLORule(
        "drift_luma_delta",
        "warning",
        _check_drift_luma,
        "Current-hour brightness differs from 7-day baseline by >25 (biofouling, lighting change).",
        needs_worker=True,
    ),
    SLORule(
        "drift_fish_rate_crash",
        "warning",
        _check_drift_fish_rate,
        "Fish-frame rate in the last hour is less than half the 7-day baseline.",
        needs_worker=True,
    ),
]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


@dataclass
class SLOChecker:
    pool: AsyncConnectionPool
    http_client: httpx.AsyncClient
    worker_url: str = ""  # empty: no camera pipeline here; its rules are skipped
    rules: list[SLORule] = field(default_factory=lambda: SLO_RULES)

    @property
    def http(self) -> httpx.AsyncClient:
        return self.http_client

    async def run_once(self) -> dict[str, bool]:
        """Evaluate each rule, upsert into `alerts`, return {name: firing}."""
        results: dict[str, bool] = {}
        for rule in self.rules:
            if rule.needs_worker and not self.worker_url:
                continue
            try:
                firing, message, details = await rule.check(self)
            except Exception:
                log.exception("SLO rule %s crashed", rule.name)
                firing, message, details = False, "rule raised", {}
            results[rule.name] = firing
            try:
                await self._upsert(rule, firing, message, details)
            except Exception:
                log.exception("SLO upsert for %s failed", rule.name)
        return results

    async def _upsert(self, rule: SLORule, firing: bool, message: str, details: dict) -> None:
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                if firing:
                    await cur.execute(
                        """
                        INSERT INTO alerts (name, severity, message, details, first_seen, last_seen)
                        VALUES (%s, %s, %s, %s, NOW(), NOW())
                        ON CONFLICT (name) DO UPDATE SET
                            severity = EXCLUDED.severity,
                            message  = EXCLUDED.message,
                            details  = EXCLUDED.details,
                            last_seen = NOW(),
                            resolved_at = CASE
                                WHEN alerts.resolved_at IS NOT NULL THEN NULL
                                ELSE alerts.resolved_at
                            END,
                            first_seen = CASE
                                WHEN alerts.resolved_at IS NOT NULL THEN NOW()
                                ELSE alerts.first_seen
                            END
                        """,
                        (rule.name, rule.severity, message, Jsonb(details)),
                    )
                else:
                    # clear if currently firing
                    await cur.execute(
                        """
                        UPDATE alerts
                           SET resolved_at = NOW(),
                               last_seen   = NOW(),
                               message     = %s,
                               details     = %s
                         WHERE name = %s AND resolved_at IS NULL
                        """,
                        (message, Jsonb(details), rule.name),
                    )

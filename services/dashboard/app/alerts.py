"""SLO alert checker.

Runs as a background asyncio task inside the dashboard process. Every
``interval_s`` seconds, each rule queries Postgres or another service's
health endpoint, computes a pass/fail, and upserts into the ``alerts`` table.

Current rules:
  - pipeline_silence:       no detection_events inserted in > 5 min
  - inference_latency_p95:  worker /stats reports last_infer_ms > 100 ms
  - poller_probe_stale:     sensestream_poller last_probe_ok_at > 30 min old
  - frame_stats_stalled:    no frame_stats rows in > 5 min
  - drift_luma_delta:       |luma_1h - luma_7d| > 25   (biofouling or lighting change)
  - drift_fish_rate_crash:  fish_rate_1h < 0.5 * fish_rate_7d and samples>=100

Rules are data-driven in SLO_RULES; adding a new one is one dataclass entry.
Alerts auto-resolve (``resolved_at`` set) once the condition clears.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

import httpx
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

log = logging.getLogger("wahoobay.slo")


RuleResult = tuple[bool, str, dict]           # (firing, message, details)


@dataclass
class SLORule:
    name: str
    severity: str                             # info | warning | critical
    check: Callable[["SLOChecker"], Awaitable[RuleResult]]
    description: str = ""


# ---------------------------------------------------------------------------
# Rule checks
# ---------------------------------------------------------------------------


async def _check_pipeline_silence(c: "SLOChecker") -> RuleResult:
    async with c.pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT max(ts) FROM detection_events")
            (last,) = await cur.fetchone() or (None,)
    if last is None:
        return (True, "no detection_events ever written", {"last_event_ts": None})
    age_s = (datetime.now(timezone.utc) - last).total_seconds()
    if age_s > 300:
        return (True,
                f"no detections in {age_s/60:.1f} min (threshold 5 min)",
                {"last_event_ts": last.isoformat(), "age_seconds": age_s})
    return (False, "ok", {"last_event_ts": last.isoformat(), "age_seconds": age_s})


async def _check_frame_stats_stalled(c: "SLOChecker") -> RuleResult:
    async with c.pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT max(ts) FROM frame_stats")
            (last,) = await cur.fetchone() or (None,)
    if last is None:
        return (False, "no frame_stats yet (waiting for worker)", {})
    age_s = (datetime.now(timezone.utc) - last).total_seconds()
    if age_s > 300:
        return (True, f"frame_stats sampler silent for {age_s/60:.1f} min",
                {"last_ts": last.isoformat(), "age_seconds": age_s})
    return (False, "ok", {"age_seconds": age_s})


async def _check_inference_latency(c: "SLOChecker") -> RuleResult:
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
        return (True, f"last inference took {last_ms:.1f} ms (threshold 100)",
                {"last_infer_ms": last_ms})
    return (False, "ok", {"last_infer_ms": last_ms})


async def _check_poller_probe_stale(c: "SLOChecker") -> RuleResult:
    try:
        r = await c.http.get(f"{c.poller_url}/status", timeout=5.0)
        if r.status_code != 200:
            return (True, f"poller /status returned {r.status_code}", {})
        j = r.json()
    except Exception as e:
        return (True, f"poller unreachable: {e}", {})
    last = j.get("last_probe_ok_at")
    if not last:
        return (False, "poller hasn't probed yet", j)
    try:
        last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
    except Exception:
        return (False, f"bad timestamp from poller: {last}", {})
    age_s = (datetime.now(timezone.utc) - last_dt).total_seconds()
    if age_s > 1800:
        return (True, f"poller last probe was {age_s/60:.1f} min ago",
                {"last_probe_ok_at": last, "age_seconds": age_s})
    return (False, "ok", {"age_seconds": age_s})


async def _check_drift_luma(c: "SLOChecker") -> RuleResult:
    async with c.pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT * FROM frame_stats_drift LIMIT 1")
            row = await cur.fetchone()
    if not row or row.get("luma_1h") is None or row.get("luma_7d") is None:
        return (False, "baseline not yet established", {})
    delta = float(row["luma_1h"]) - float(row["luma_7d"])
    if abs(delta) > 25.0:
        return (True, f"luma shift of {delta:+.1f} vs 7d baseline (threshold ±25)",
                {"luma_1h": row["luma_1h"], "luma_7d": row["luma_7d"], "delta": delta})
    return (False, "ok", {"delta": delta})


async def _check_drift_fish_rate(c: "SLOChecker") -> RuleResult:
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
        return (True,
                f"fish-frame rate collapsed: {fr1*100:.1f}% vs 7d {fr7*100:.1f}%",
                {"fish_rate_1h": fr1, "fish_rate_7d": fr7})
    return (False, "ok", {"fish_rate_1h": fr1, "fish_rate_7d": fr7})


SLO_RULES: list[SLORule] = [
    SLORule("pipeline_silence",       "critical", _check_pipeline_silence,
            "No detection_events inserted in the last 5 min."),
    SLORule("frame_stats_stalled",    "warning",  _check_frame_stats_stalled,
            "Drift sampler stopped producing frame_stats rows."),
    SLORule("inference_latency_p95",  "warning",  _check_inference_latency,
            "Last per-frame inference exceeded 100 ms."),
    SLORule("poller_probe_stale",     "warning",  _check_poller_probe_stale,
            "SenseStream poller hasn't successfully probed the deployment recently."),
    SLORule("drift_luma_delta",       "warning",  _check_drift_luma,
            "Current-hour brightness differs from 7-day baseline by >25 (biofouling, lighting change)."),
    SLORule("drift_fish_rate_crash",  "warning",  _check_drift_fish_rate,
            "Fish-frame rate in the last hour is less than half the 7-day baseline."),
]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


@dataclass
class SLOChecker:
    pool: AsyncConnectionPool
    worker_url: str
    poller_url: str
    http_client: httpx.AsyncClient
    rules: list[SLORule] = field(default_factory=lambda: SLO_RULES)

    @property
    def http(self) -> httpx.AsyncClient:
        return self.http_client

    async def run_once(self) -> dict[str, bool]:
        """Evaluate each rule, upsert into `alerts`, return {name: firing}."""
        results: dict[str, bool] = {}
        for rule in self.rules:
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

    async def run_forever(self, interval_s: float = 30.0) -> None:
        log.info("SLO checker starting (%d rules, interval=%.0fs)",
                 len(self.rules), interval_s)
        while True:
            await self.run_once()
            await asyncio.sleep(interval_s)

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

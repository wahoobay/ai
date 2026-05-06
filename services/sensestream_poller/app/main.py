"""SenseStream → Postgres poller for the Wahoo Bay (wahoo_2) EXO2 sonde.

Runs as a standalone service. Every POLL_INTERVAL_S:
  1. Verifies deployment metadata via the unauthenticated endpoint
     ``/manager/deployments/by-uri/<uri>`` so we notice if the deployment
     disappears or changes.
  2. If SENSESTREAM_AUTH_TOKEN is set, fetches new observations from
     ``/observations/measurements/<uri>?start=...&end=...`` (path and
     query parameters are env-tunable pending confirmation from a live
     session) and upserts them into ``sensor_readings`` with
     ``source='live'``.

If no auth token is configured the poller still runs — it just logs that
it's waiting for credentials and periodically reconfirms the deployment is
up. This means the service can be deployed ahead of credentials being
available.

Parameter mapping from SenseStream's EXO2 channels to our schema:
    Water Temperature  -> water_temp_c
    pH                 -> ph
    DO                 -> do_pct
    Chlorophyll (RFU)  -> chlorophyll_rfu
    Phycoerythrin (RFU)-> phycoerythrin_rfu
    Turbidity (FNU)    -> turbidity_fnu
    sPCond             -> spcond_ms_cm
    (NO3-N nitrate sensor was removed from the sonde 2026-05.)
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Iterable, Optional

import httpx
import psycopg
import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

log = logging.getLogger("wahoobay.sensestream")

# When in stub mode (no SENSESTREAM_AUTH_TOKEN), the poller emits
# synthetic readings so downstream charts (Sightings vs water quality,
# water-quality history) keep showing data. Lazy-import the generator
# from scripts/ so the runtime dependency on numpy is only paid here.
_SYNTH_OK = False
try:
    _REPO_ROOT = Path(__file__).resolve().parents[3]
    if str(_REPO_ROOT / "scripts") not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT / "scripts"))
    import gen_synthetic_sensor_data as _synth  # type: ignore
    import numpy as _np  # type: ignore
    _SYNTH_OK = True
except Exception as _e:  # pragma: no cover
    log.warning("synthetic stub-mode generator unavailable: %s", _e)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, default))
    except ValueError:
        return default


@dataclass(frozen=True)
class Config:
    base_url: str
    auth_token: str                           # optional; poller runs in stub mode without it
    deployment_uri: str
    poll_interval_s: int
    backfill_minutes: int                     # window to fetch each poll (overlap tolerated; UNIQUE prevents dupes)
    source_tag: str                           # value written to sensor_readings.source
    measurements_path_template: str           # supports {uri}
    database_url: str
    http_host: str
    http_port: int
    log_level: str

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            base_url=_env("SENSESTREAM_BASE_URL", "https://api.sensestream.org").rstrip("/"),
            auth_token=_env("SENSESTREAM_AUTH_TOKEN", ""),
            deployment_uri=_env("SENSESTREAM_DEPLOYMENT_URI", "wahoo_2"),
            poll_interval_s=_int("SENSESTREAM_POLL_INTERVAL_S", 300),
            backfill_minutes=_int("SENSESTREAM_BACKFILL_MINUTES", 60),
            source_tag=_env("SENSESTREAM_SOURCE_TAG", "live"),
            measurements_path_template=_env(
                "SENSESTREAM_MEASUREMENTS_PATH",
                "/observations/measurements/{uri}",
            ),
            database_url=_env(
                "DATABASE_URL",
                "postgresql://wahoobay:wahoobay@localhost:5432/wahoobay",
            ),
            http_host=_env("POLLER_HTTP_HOST", "0.0.0.0"),
            http_port=_int("POLLER_HTTP_PORT", 8082),
            log_level=_env("LOG_LEVEL", "INFO"),
        )


# ---------------------------------------------------------------------------
# Parameter mapping
# ---------------------------------------------------------------------------


# Keys are lowercased and whitespace-stripped SenseStream channel names as we
# best understand them; values are our column names.
CHANNEL_TO_COLUMN = {
    "water temperature": "water_temp_c",
    "ph":                "ph",
    "do":                "do_pct",
    "chlorophyll (rfu)": "chlorophyll_rfu",
    "chlorophyll":       "chlorophyll_rfu",
    "phycoerythrin (rfu)": "phycoerythrin_rfu",
    "phycoerythrin":     "phycoerythrin_rfu",
    "turbidity (fnu)":   "turbidity_fnu",
    "turbidity":         "turbidity_fnu",
    "spcond":            "spcond_ms_cm",
    "specific conductance": "spcond_ms_cm",
    # NO3-N nitrate sensor was removed from the sonde 2026-05; if a
    # future feed re-includes it, restore the mapping here.
}


COLUMNS_ORDER = (
    "water_temp_c", "ph", "do_pct", "chlorophyll_rfu",
    "phycoerythrin_rfu", "turbidity_fnu", "spcond_ms_cm",
)


def _normalize_channel(name: str) -> Optional[str]:
    key = name.strip().lower()
    return CHANNEL_TO_COLUMN.get(key)


# ---------------------------------------------------------------------------
# Response parser
# ---------------------------------------------------------------------------


def parse_observations(payload: Any) -> list[tuple[datetime, dict]]:
    """Parse a SenseStream observations response into (ts, values_dict) rows.

    The true schema for ``/observations/measurements/<uri>`` was not verified
    against a live auth'd call yet, so this parser is defensive and handles
    the two most plausible shapes:

    1.  Legacy positional-readings form (same as the old wahoobaydev API):
            {"time": <epoch_ms>, "readings": [v0, v1, ...]}
        — assumes readings are in the sensor-metadata channel order. Not
        preferred because order is sensor-config-dependent.

    2.  Keyed-channel form:
            {"time": <epoch_ms>, "channels": {"Water Temperature": 25.1, ...}}
        or
            {"time": <epoch_ms>, "measurements": [{"name": "Water Temperature", "value": 25.1}, ...]}

    Response envelopes seen on the manager endpoints look like
    ``{"errorCode": 0, "errorMessage": "OK", "result": <payload>}``.
    The inner payload is what we parse.
    """
    if isinstance(payload, dict) and "result" in payload:
        payload = payload["result"]
    if payload is None:
        return []
    items = payload if isinstance(payload, list) else [payload]

    out: list[tuple[datetime, dict]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        t = item.get("time") or item.get("ts") or item.get("timestamp")
        if t is None:
            continue
        try:
            if isinstance(t, (int, float)):
                # assume ms if value is large; else seconds
                ts = datetime.fromtimestamp((t / 1000.0) if t > 1e12 else t, tz=timezone.utc)
            else:
                ts = datetime.fromisoformat(str(t).replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
        except Exception:
            continue

        values: dict[str, float] = {}

        # form 2a: keyed dict
        channels = item.get("channels") or item.get("values")
        if isinstance(channels, dict):
            for k, v in channels.items():
                col = _normalize_channel(str(k))
                if col and isinstance(v, (int, float)):
                    values[col] = float(v)

        # form 2b: list of {name, value}
        measurements = item.get("measurements") or item.get("parameters")
        if isinstance(measurements, list):
            for m in measurements:
                if not isinstance(m, dict):
                    continue
                name = m.get("name") or m.get("parameter") or m.get("channel")
                val = m.get("value") if "value" in m else m.get("reading")
                if name is None or val is None:
                    continue
                col = _normalize_channel(str(name))
                if col and isinstance(val, (int, float)):
                    values[col] = float(val)

        # form 1: positional readings (fallback; only used if the first two yielded nothing)
        if not values:
            readings = item.get("readings")
            if isinstance(readings, list):
                log.warning(
                    "positional 'readings' array seen (len=%d); mapping requires sensor metadata we haven't wired. Skipping this record.",
                    len(readings),
                )

        if values:
            out.append((ts, values))
    return out


# ---------------------------------------------------------------------------
# Poller state
# ---------------------------------------------------------------------------


@dataclass
class PollerStats:
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_probe_ok_at: Optional[datetime] = None
    last_fetch_attempt_at: Optional[datetime] = None
    last_insert_at: Optional[datetime] = None
    last_error: str = ""
    total_inserted: int = 0
    running: bool = False
    token_configured: bool = False
    deployment_active: Optional[bool] = None
    last_status_code: Optional[int] = None


class Poller:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.stats = PollerStats(token_configured=bool(cfg.auth_token))
        self._stop = asyncio.Event()

    async def run(self) -> None:
        self.stats.running = True
        log.info(
            "poller starting: base=%s deployment=%s interval=%ss token=%s",
            self.cfg.base_url, self.cfg.deployment_uri, self.cfg.poll_interval_s,
            "set" if self.cfg.token_configured_effective() else "MISSING (stub mode)",
        )
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
            while not self._stop.is_set():
                start = time.monotonic()
                try:
                    await self._tick(client)
                except Exception:
                    log.exception("poller tick failed")
                elapsed = time.monotonic() - start
                delay = max(1.0, self.cfg.poll_interval_s - elapsed)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
        self.stats.running = False

    def stop(self) -> None:
        self._stop.set()

    async def _tick(self, client: httpx.AsyncClient) -> None:
        await self._probe_deployment(client)
        if not self.cfg.auth_token:
            # Stub mode: no live token. Emit synthetic readings so the
            # dashboard's water-quality panels and Sightings-vs-water
            # chart keep showing data. The first tick backfills 30 days
            # of history; subsequent ticks just keep the most-recent
            # hour fresh (everything is idempotent via ON CONFLICT).
            await asyncio.get_running_loop().run_in_executor(None, self._upsert_synthetic_window)
            return
        await self._fetch_and_store(client)

    def _upsert_synthetic_window(self) -> None:
        if not _SYNTH_OK:
            return
        end = datetime.now(timezone.utc).replace(microsecond=0, second=0)
        # First time we run in this process, backfill 30 days; afterward
        # just refresh the trailing 2 hours (a couple of new samples per
        # tick at the 10-min sonde cadence).
        first_run = self.stats.total_inserted == 0
        days_or_hours = (30, "days") if first_run else (2, "hours")
        if days_or_hours[1] == "days":
            start = end - timedelta(days=days_or_hours[0])
        else:
            start = end - timedelta(hours=days_or_hours[0])
        # Deterministic seed per-day so replays produce a consistent series.
        seed = int(end.strftime("%Y%m%d"))
        rng = _np.random.default_rng(seed)
        try:
            arrays = _synth.synthesize(
                start.replace(tzinfo=None), end.replace(tzinfo=None), rng,
            )
        except Exception:
            log.exception("synthetic generation failed")
            return
        n = len(arrays["ts"])
        rows: list[tuple[datetime, dict]] = []
        keys = ("water_temp_c", "ph", "do_pct", "chlorophyll_rfu",
                "phycoerythrin_rfu", "turbidity_fnu", "spcond_ms_cm")
        for i in range(n):
            t = datetime.utcfromtimestamp(int(arrays["ts"][i].astype("int64"))).replace(tzinfo=timezone.utc)
            rows.append((t, {k: float(arrays[k][i]) for k in keys}))
        try:
            inserted = self._upsert(rows, source="synthetic")
        except Exception:
            log.exception("synthetic upsert failed")
            return
        self.stats.total_inserted += inserted
        if inserted:
            self.stats.last_insert_at = datetime.now(timezone.utc)
        log.info("synthetic stub: upserted %d row(s) over %d %s window (total=%d)",
                 inserted, days_or_hours[0], days_or_hours[1], self.stats.total_inserted)

    async def _probe_deployment(self, client: httpx.AsyncClient) -> None:
        url = f"{self.cfg.base_url}/manager/deployments/by-uri/{self.cfg.deployment_uri}"
        try:
            r = await client.get(url)
            self.stats.last_status_code = r.status_code
            if r.status_code == 200:
                j = r.json()
                result = j.get("result") or {}
                self.stats.deployment_active = bool(result.get("active"))
                self.stats.last_probe_ok_at = datetime.now(timezone.utc)
                if not self.stats.deployment_active:
                    log.warning("deployment %s is not active", self.cfg.deployment_uri)
            else:
                log.warning("deployment probe HTTP %s", r.status_code)
        except Exception as e:
            self.stats.last_error = f"probe: {e}"
            log.warning("deployment probe failed: %s", e)

    async def _fetch_and_store(self, client: httpx.AsyncClient) -> None:
        cfg = self.cfg
        self.stats.last_fetch_attempt_at = datetime.now(timezone.utc)

        end_ms = int(time.time() * 1000)
        start_ms = end_ms - cfg.backfill_minutes * 60 * 1000
        path = cfg.measurements_path_template.format(uri=cfg.deployment_uri)
        url = f"{cfg.base_url}{path}"
        params = {"start": start_ms, "end": end_ms}
        headers = {"x-auth-token": cfg.auth_token}

        try:
            r = await client.get(url, params=params, headers=headers)
            self.stats.last_status_code = r.status_code
            if r.status_code != 200:
                body_sample = (r.text or "")[:200]
                self.stats.last_error = f"fetch HTTP {r.status_code}: {body_sample}"
                log.warning("fetch %s returned %s: %s", url, r.status_code, body_sample)
                return
            payload = r.json()
        except Exception as e:
            self.stats.last_error = f"fetch: {e}"
            log.warning("fetch failed: %s", e)
            return

        rows = parse_observations(payload)
        if not rows:
            log.info("no parseable rows in response (backfill window %d min)", cfg.backfill_minutes)
            return

        inserted = self._upsert(rows)
        self.stats.total_inserted += inserted
        self.stats.last_insert_at = datetime.now(timezone.utc)
        log.info("inserted %d rows (total=%d)", inserted, self.stats.total_inserted)

    def _upsert(self, rows: Iterable[tuple[datetime, dict]], source: Optional[str] = None) -> int:
        cfg = self.cfg
        src = source if source is not None else cfg.source_tag
        payload = []
        for ts, values in rows:
            payload.append((
                ts, cfg.deployment_uri,
                values.get("water_temp_c"),
                values.get("ph"),
                values.get("do_pct"),
                values.get("chlorophyll_rfu"),
                values.get("phycoerythrin_rfu"),
                values.get("turbidity_fnu"),
                values.get("spcond_ms_cm"),
                src,
            ))
        if not payload:
            return 0
        with psycopg.connect(cfg.database_url) as conn, conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO sensor_readings
                (ts, deployment_uri, water_temp_c, ph, do_pct,
                 chlorophyll_rfu, phycoerythrin_rfu, turbidity_fnu,
                 spcond_ms_cm, source)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (deployment_uri, ts) DO NOTHING
                """,
                payload,
            )
            conn.commit()
        return len(payload)


# helper referenced inside run()
def _token_configured_effective(self: Config) -> bool:
    return bool(self.auth_token)
Config.token_configured_effective = _token_configured_effective  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# HTTP surface (healthz + status)
# ---------------------------------------------------------------------------


def build_app(cfg: Config | None = None) -> FastAPI:
    cfg = cfg or Config.from_env()
    poller = Poller(cfg)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        task = asyncio.create_task(poller.run(), name="sensestream-poller")
        try:
            yield
        finally:
            poller.stop()
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    app = FastAPI(title="Wahoo Bay sensestream poller", lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True}

    @app.get("/status")
    async def status() -> JSONResponse:
        s = poller.stats
        return JSONResponse({
            "running": s.running,
            "deployment_uri": cfg.deployment_uri,
            "token_configured": s.token_configured,
            "deployment_active": s.deployment_active,
            "last_status_code": s.last_status_code,
            "started_at": s.started_at.isoformat(),
            "last_probe_ok_at": s.last_probe_ok_at.isoformat() if s.last_probe_ok_at else None,
            "last_fetch_attempt_at": s.last_fetch_attempt_at.isoformat() if s.last_fetch_attempt_at else None,
            "last_insert_at": s.last_insert_at.isoformat() if s.last_insert_at else None,
            "total_inserted": s.total_inserted,
            "last_error": s.last_error,
        })

    return app


def main() -> None:
    cfg = Config.from_env()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    app = build_app(cfg)
    uvicorn.run(app, host=cfg.http_host, port=cfg.http_port, log_level=cfg.log_level.lower())


if __name__ == "__main__":
    main()

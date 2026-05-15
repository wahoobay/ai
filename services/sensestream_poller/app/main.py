"""Sensor + weather poller for the Wahoo Bay live dashboard.

Every POLL_INTERVAL_S the poller tries three data sources in order:

  1. **Wahoo Bay public API** (``api.wahoobay.net``). No auth required.
     Endpoints:
       - ``/api/v1/sitedata/latest?site=3`` — wahoo_2 EXO2 sonde
         (water temp, pH, DO, chlorophyll, phycoerythrin, turbidity, sPCond)
       - ``/api/v1/sitedata/latest?site=1`` — wahoo_1 Vaisala WXT-536
         weather station (pressure, wind, air temp, humidity, rain, hail,
         water level, solar radiation)
       - ``/api/v1/weatherdata/latest``      — Tomorrow.io forecast layer
         (cloud cover, dew point, apparent temp, UV index, precip)
     Backfill: ``/api/v1/sitedata?site=N&start=<ms>&end=<ms>`` and
     ``/api/v1/weatherdata?start=<ms>&end=<ms>``.
     Readings are returned as positional arrays in the order documented
     by Wahoo Bay's backend (see ``progressNotes.md`` in their frontend
     repo and the sensor metadata at
     ``/manager/deployments/by-uri/<uri>`` on SenseStream).

  2. **Direct SenseStream observations endpoint** (legacy path, requires
     ``SENSESTREAM_AUTH_TOKEN``). Disabled unless the token is set; kept
     in place so we can switch over if Wahoo Bay's API ever goes dark.

  3. **Synthetic stub generator** (``scripts/gen_synthetic_sensor_data.py``).
     Last-resort fallback so the dashboard's water-quality and weather
     panels keep showing data when both live paths are unreachable.

Storage: all rows land in ``sensor_readings`` / ``weather_readings`` with
``source='live'`` (Wahoo Bay API or direct SenseStream) or
``source='synthetic'`` (stub generator). ``UNIQUE (deployment_uri, ts)``
makes every path idempotent.

Sonde parameter mapping (wahoo_2 EXO2):
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
import bisect
import logging
import os
import sys
import time
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

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
    auth_token: str                           # optional; legacy direct-SenseStream path uses this
    deployment_uri: str                       # sonde deployment URI (defaults to wahoo_2; used as DB tag)
    weather_deployment_uri: str               # weather-station deployment URI (defaults to wahoo_1)
    poll_interval_s: int
    backfill_minutes: int                     # window to fetch each poll (overlap tolerated; UNIQUE prevents dupes)
    initial_backfill_days: int                # one-shot historical window on first tick
    source_tag: str                           # value written to *_readings.source for live data
    measurements_path_template: str           # supports {uri} — legacy direct-SenseStream path
    wahoobay_api_url: str                     # base URL for the public Wahoo Bay API
    wahoobay_site_sonde: int                  # /api/v1/sitedata site id for the EXO2 sonde
    wahoobay_site_weather: int                # /api/v1/sitedata site id for the weather station
    wahoobay_enabled: bool
    database_url: str
    http_host: str
    http_port: int
    log_level: str

    @classmethod
    def from_env(cls) -> Config:
        return cls(
            base_url=_env("SENSESTREAM_BASE_URL", "https://api.sensestream.org").rstrip("/"),
            auth_token=_env("SENSESTREAM_AUTH_TOKEN", ""),
            deployment_uri=_env("SENSESTREAM_DEPLOYMENT_URI", "wahoo_2"),
            # Weather rows go under the same deployment tag as the sonde so the
            # dashboard's default `deployment=wahoo_2` query keeps returning data
            # without a migration. Override with SENSESTREAM_WEATHER_DEPLOYMENT_URI
            # if you want to split them out (the actual SenseStream uri for the
            # weather station is `wahoo_1`).
            weather_deployment_uri=_env("SENSESTREAM_WEATHER_DEPLOYMENT_URI", "wahoo_2"),
            poll_interval_s=_int("SENSESTREAM_POLL_INTERVAL_S", 300),
            backfill_minutes=_int("SENSESTREAM_BACKFILL_MINUTES", 60),
            initial_backfill_days=_int("SENSESTREAM_INITIAL_BACKFILL_DAYS", 30),
            source_tag=_env("SENSESTREAM_SOURCE_TAG", "live"),
            measurements_path_template=_env(
                "SENSESTREAM_MEASUREMENTS_PATH",
                "/observations/measurements/{uri}",
            ),
            wahoobay_api_url=_env("WAHOOBAY_API_URL", "https://api.wahoobay.net").rstrip("/"),
            wahoobay_site_sonde=_int("WAHOOBAY_SITE_SONDE", 3),
            wahoobay_site_weather=_int("WAHOOBAY_SITE_WEATHER", 1),
            wahoobay_enabled=_env("WAHOOBAY_API_ENABLED", "1").lower() not in ("0", "false", "no", ""),
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


def _normalize_channel(name: str) -> str | None:
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
            if isinstance(t, int | float):
                # assume ms if value is large; else seconds
                ts = datetime.fromtimestamp((t / 1000.0) if t > 1e12 else t, tz=UTC)
            else:
                ts = datetime.fromisoformat(str(t).replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=UTC)
        except Exception:
            continue

        values: dict[str, float] = {}

        # form 2a: keyed dict
        channels = item.get("channels") or item.get("values")
        if isinstance(channels, dict):
            for k, v in channels.items():
                col = _normalize_channel(str(k))
                if col and isinstance(v, int | float):
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
                if col and isinstance(val, int | float):
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
# Wahoo Bay public API parsers (positional `readings[]` arrays)
# ---------------------------------------------------------------------------
#
# All three Wahoo Bay endpoints return either ``{"time": <ms>, "readings": [...]}``
# (latest) or a JSON array of those (range). Index order is fixed by Wahoo Bay's
# backend; the mappings below come from:
#   - sonde:    /manager/deployments/by-uri/wahoo_2 sensor metadata
#   - weather:  progressNotes.md in the wahoobay/wahoobay.github.io repo
#   - forecast: Tomorrow.io field order (alphabetical) — confirmed against
#     live values (pressureSeaLevel ≈ 1015, temperature ≈ 27°C, weatherCode = 1000).
# A wrong index would silently store junk, so unit-plausibility checks below
# would catch sensor reshuffles.


def _parse_readings_payload(payload: Any) -> list[tuple[datetime, list]]:
    """Pull (ts, readings[]) tuples out of a Wahoo Bay API payload.

    Accepts both the single-record (``/latest``) and range (``[{time, readings}, ...]``)
    shapes.
    """
    if payload is None:
        return []
    items = payload if isinstance(payload, list) else [payload]
    out: list[tuple[datetime, list]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        t = item.get("time") or item.get("ts") or item.get("timestamp")
        readings = item.get("readings")
        if t is None or not isinstance(readings, list):
            continue
        try:
            if isinstance(t, int | float):
                ts = datetime.fromtimestamp((t / 1000.0) if t > 1e12 else t, tz=UTC)
            else:
                ts = datetime.fromisoformat(str(t).replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=UTC)
        except Exception:
            continue
        out.append((ts, readings))
    return out


def _get(arr: list, idx: int) -> float | None:
    """Safe indexed read returning ``None`` for missing / non-numeric entries."""
    if 0 <= idx < len(arr):
        v = arr[idx]
        if isinstance(v, int | float):
            return float(v)
    return None


def parse_wahoobay_sonde(payload: Any) -> list[tuple[datetime, dict]]:
    """Convert wahoo_2 EXO2 sonde readings (8 positional values).

    Index order from /manager/deployments/by-uri/wahoo_2:
        0 Water Temperature (°C)
        1 pH
        2 DO (%)
        3 Chlorophyll (RFU)
        4 Phycoerythrin (RFU)
        5 Turbidity (FNU)
        6 NO3-N (removed 2026-05; ignored)
        7 sPCond (mS/cm)
    """
    rows: list[tuple[datetime, dict]] = []
    for ts, r in _parse_readings_payload(payload):
        values = {
            "water_temp_c":       _get(r, 0),
            "ph":                 _get(r, 1),
            "do_pct":             _get(r, 2),
            "chlorophyll_rfu":    _get(r, 3),
            "phycoerythrin_rfu":  _get(r, 4),
            "turbidity_fnu":      _get(r, 5),
            "spcond_ms_cm":       _get(r, 7),
        }
        # Drop entirely-empty rows (sensor offline / payload junk).
        if any(v is not None for v in values.values()):
            rows.append((ts, values))
    return rows


def parse_wahoobay_weather_station(payload: Any) -> list[tuple[datetime, dict]]:
    """Convert wahoo_1 Vaisala WXT-536 readings (24 positional values).

    Index order from progressNotes.md (Wahoo Bay frontend repo) and confirmed
    against the live SenseStream sensor metadata:
        0 Barometric pressure (hPa)
        1 Wind direction minimum (deg)
        2 Wind direction average (deg)
        3 Wind direction maximum (deg)
        4 Wind speed minimum (m/s)
        5 Wind speed average (m/s)
        6 Wind speed maximum (m/s)
        7 Air temperature (°C)
        8 Internal temperature (°C)  — not stored (diagnostic)
        9 Relative humidity (%)
       10 Rain accumulation (mm)
       11 Rain duration (raw — divide by 3600 to convert seconds → hours)
       12 Rain intensity (mm/h)
       13 Hail accumulation (count)
       14 Hail duration
       15 Hail intensity
       16 Rain peak intensity (mm/h)
       17 Hail peak intensity
       18 Water level (raw V — convert via 1024 * 10 * x / 3.3 → mm)
       19 Solar radiation (raw V — convert via x * 566.25 → W/m²)
       20–23 Heating / supply voltages (diagnostic; not stored)
    """
    rows: list[tuple[datetime, dict]] = []
    for ts, r in _parse_readings_payload(payload):
        rain_dur_raw = _get(r, 11)
        hail_dur_raw = _get(r, 14)
        water_level_raw = _get(r, 18)
        solar_raw = _get(r, 19)
        values = {
            "bar_press_hpa":       _get(r, 0),
            "wind_dir_min_deg":    _get(r, 1),
            "wind_dir_avg_deg":    _get(r, 2),
            "wind_dir_max_deg":    _get(r, 3),
            "wind_speed_min_ms":   _get(r, 4),
            "wind_speed_avg_ms":   _get(r, 5),
            "wind_speed_max_ms":   _get(r, 6),
            "air_temp_c":          _get(r, 7),
            "rel_humidity_pct":    _get(r, 9),
            "rain_accum_mm":       _get(r, 10),
            "rain_dur_frac_hr":    (rain_dur_raw / 3600.0) if rain_dur_raw is not None else None,
            "rain_int_mm_hr":      _get(r, 12),
            "hail_accum":          _get(r, 13),
            "hail_dur":            (hail_dur_raw / 3600.0) if hail_dur_raw is not None else None,
            "hail_int":            _get(r, 15),
            "rain_peak_int_mm_hr": _get(r, 16),
            "hail_peak_int":       _get(r, 17),
            "water_level_mm":      (1024.0 * 10.0 * water_level_raw / 3.3) if water_level_raw is not None else None,
            "solar_rad_wm2":       (solar_raw * 566.25) if solar_raw is not None else None,
        }
        if any(v is not None for v in values.values()):
            rows.append((ts, values))
    return rows


def parse_wahoobay_forecast(payload: Any) -> list[tuple[datetime, dict]]:
    """Convert Tomorrow.io forecast readings (13 positional values).

    Index order (alphabetical by Tomorrow.io field name, verified against
    a live response on 2026-05-15: pressureSeaLevel ≈ 1015 hPa, temperature ≈
    27 °C, weatherCode = 1000):
        0  cloudCover (%)
        1  dewPoint (°C)
        2  humidity (%)
        3  precipitationIntensity (mm/hr)
        4  precipitationProbability (%)
        5  pressureSeaLevel (hPa)
        6  pressureSurfaceLevel (hPa)
        7  temperature (°C)            — not stored (redundant with site=1 air_temp_c)
        8  temperatureApparent (°C)
        9  uvIndex
       10  weatherCode (int)
       11  windDirection (deg)         — not stored (redundant with site=1)
       12  windSpeed (m/s)             — not stored (redundant with site=1)
    """
    rows: list[tuple[datetime, dict]] = []
    for ts, r in _parse_readings_payload(payload):
        wc_raw = _get(r, 10)
        values = {
            "cloud_cover_pct":        _get(r, 0),
            "dew_point_c":            _get(r, 1),
            "forecast_humidity_pct":  _get(r, 2),
            "precip_intensity_mm_hr": _get(r, 3),
            "precip_prob_pct":        _get(r, 4),
            "press_sea_level_hpa":    _get(r, 5),
            "press_surface_hpa":      _get(r, 6),
            "temp_apparent_c":        _get(r, 8),
            "uv_index":               _get(r, 9),
            "weather_code":           int(wc_raw) if wc_raw is not None else None,
        }
        if any(v is not None for v in values.values()):
            rows.append((ts, values))
    return rows


# ---------------------------------------------------------------------------
# Poller state
# ---------------------------------------------------------------------------


@dataclass
class PollerStats:
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_probe_ok_at: datetime | None = None
    last_fetch_attempt_at: datetime | None = None
    last_insert_at: datetime | None = None
    last_weather_insert_at: datetime | None = None
    last_error: str = ""
    total_inserted: int = 0
    weather_inserted_total: int = 0
    running: bool = False
    token_configured: bool = False
    deployment_active: bool | None = None
    last_status_code: int | None = None
    # Wahoo Bay public API tracking
    wahoobay_enabled: bool = False
    wahoobay_last_ok_at: datetime | None = None
    wahoobay_last_error: str = ""
    wahoobay_sonde_inserted_total: int = 0
    wahoobay_weather_inserted_total: int = 0
    last_data_source: str = ""  # "wahoobay" | "sensestream" | "synthetic"


class Poller:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.stats = PollerStats(
            token_configured=bool(cfg.auth_token),
            wahoobay_enabled=cfg.wahoobay_enabled and bool(cfg.wahoobay_api_url),
        )
        self._stop = asyncio.Event()

    async def run(self) -> None:
        self.stats.running = True
        if self.stats.wahoobay_enabled:
            primary = f"wahoobay({self.cfg.wahoobay_api_url})"
        elif self.cfg.auth_token:
            primary = f"sensestream({self.cfg.base_url}, token=set)"
        else:
            primary = "synthetic(stub)"
        log.info(
            "poller starting: primary=%s sonde=%s weather=%s interval=%ss backfill=%dd/%dmin",
            primary, self.cfg.deployment_uri, self.cfg.weather_deployment_uri,
            self.cfg.poll_interval_s, self.cfg.initial_backfill_days,
            self.cfg.backfill_minutes,
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
                except TimeoutError:
                    pass
        self.stats.running = False

    def stop(self) -> None:
        self._stop.set()

    async def _tick(self, client: httpx.AsyncClient) -> None:
        # Wahoo Bay public API is the preferred path (no auth needed).
        if self.stats.wahoobay_enabled:
            ok = await self._tick_wahoobay(client)
            if ok:
                self.stats.last_data_source = "wahoobay"
                return
            log.warning("Wahoo Bay API tick failed; falling back to next source")

        # Legacy direct-SenseStream path (auth token required).
        await self._probe_deployment(client)
        if self.cfg.auth_token:
            await self._fetch_and_store(client)
            self.stats.last_data_source = "sensestream"
            return

        # Last resort: synthetic stub generator.
        await asyncio.get_running_loop().run_in_executor(None, self._upsert_synthetic_window)
        self.stats.last_data_source = "synthetic"

    # ---- Wahoo Bay public API path ----------------------------------------

    async def _tick_wahoobay(self, client: httpx.AsyncClient) -> bool:
        """Pull from api.wahoobay.net for sonde + weather + forecast.

        Returns True if at least one of the three feeds returned usable rows.
        Failures on individual feeds are logged but don't abort the tick.
        """
        cfg = self.cfg
        end = datetime.now(UTC).replace(microsecond=0, second=0)
        first_run = (
            self.stats.wahoobay_sonde_inserted_total == 0
            and self.stats.wahoobay_weather_inserted_total == 0
        )
        if first_run:
            start = end - timedelta(days=cfg.initial_backfill_days)
        else:
            start = end - timedelta(minutes=cfg.backfill_minutes)
        start_ms = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        base = cfg.wahoobay_api_url
        any_ok = False

        # Sonde (wahoo_2) → sensor_readings
        try:
            r = await client.get(f"{base}/api/v1/sitedata", params={
                "site": cfg.wahoobay_site_sonde, "start": start_ms, "end": end_ms,
            })
            if r.status_code == 200:
                rows = parse_wahoobay_sonde(r.json())
                inserted = self._upsert(rows, source=cfg.source_tag, uri=cfg.deployment_uri)
                self.stats.wahoobay_sonde_inserted_total += inserted
                self.stats.total_inserted += inserted
                if inserted:
                    self.stats.last_insert_at = datetime.now(UTC)
                log.info("wahoobay sonde: %d row(s), %d inserted (%s..%s)",
                         len(rows), inserted, start.isoformat(timespec="minutes"),
                         end.isoformat(timespec="minutes"))
                any_ok = True
            else:
                self.stats.wahoobay_last_error = f"sonde HTTP {r.status_code}"
                log.warning("wahoobay sonde fetch HTTP %s", r.status_code)
        except Exception as e:
            self.stats.wahoobay_last_error = f"sonde: {e}"
            log.warning("wahoobay sonde fetch failed: %s", e)

        # Weather station (wahoo_1) → weather_readings (on-site columns)
        weather_station_rows: list[tuple[datetime, dict]] = []
        try:
            r = await client.get(f"{base}/api/v1/sitedata", params={
                "site": cfg.wahoobay_site_weather, "start": start_ms, "end": end_ms,
            })
            if r.status_code == 200:
                weather_station_rows = parse_wahoobay_weather_station(r.json())
                any_ok = True
            else:
                self.stats.wahoobay_last_error = f"weather HTTP {r.status_code}"
                log.warning("wahoobay weather fetch HTTP %s", r.status_code)
        except Exception as e:
            self.stats.wahoobay_last_error = f"weather: {e}"
            log.warning("wahoobay weather fetch failed: %s", e)

        # Tomorrow.io forecast layer → merged into the same weather_readings row
        forecast_rows: list[tuple[datetime, dict]] = []
        try:
            r = await client.get(f"{base}/api/v1/weatherdata", params={
                "start": start_ms, "end": end_ms,
            })
            if r.status_code == 200:
                forecast_rows = parse_wahoobay_forecast(r.json())
                any_ok = True
            else:
                self.stats.wahoobay_last_error = f"forecast HTTP {r.status_code}"
                log.warning("wahoobay forecast fetch HTTP %s", r.status_code)
        except Exception as e:
            self.stats.wahoobay_last_error = f"forecast: {e}"
            log.warning("wahoobay forecast fetch failed: %s", e)

        merged = self._merge_weather_rows(weather_station_rows, forecast_rows)
        if merged:
            inserted = self._upsert_weather(merged, source=cfg.source_tag,
                                            uri=cfg.weather_deployment_uri)
            self.stats.wahoobay_weather_inserted_total += inserted
            self.stats.weather_inserted_total += inserted
            if inserted:
                self.stats.last_weather_insert_at = datetime.now(UTC)
            log.info("wahoobay weather: station=%d forecast=%d merged=%d (%s..%s)",
                     len(weather_station_rows), len(forecast_rows), inserted,
                     start.isoformat(timespec="minutes"), end.isoformat(timespec="minutes"))

        if any_ok:
            self.stats.wahoobay_last_ok_at = datetime.now(UTC)
        return any_ok

    @staticmethod
    def _merge_weather_rows(
        station: list[tuple[datetime, dict]],
        forecast: list[tuple[datetime, dict]],
    ) -> list[tuple[datetime, dict]]:
        """Combine on-site station rows with Tomorrow.io forecast rows.

        The two feeds cadence differently: the station is on a strict 10-min
        boundary; the forecast layer is ~3-min. Each forecast row is snapped
        to the nearest station timestamp (within 10 minutes) and merged.
        Forecast-only rows beyond that window are passed through unchanged so
        the forecast layer remains visible if the station goes offline.
        """
        if not station and not forecast:
            return []
        if not station:
            return [(ts, dict(v)) for ts, v in forecast]

        # Sort once, bisect per forecast row → O((N+M) log N).
        by_ts: dict[datetime, dict] = {ts: dict(vals) for ts, vals in station}
        station_keys = sorted(by_ts.keys())
        n = len(station_keys)

        for ts, vals in forecast:
            i = bisect.bisect_left(station_keys, ts)
            # Compare ts to keys at i-1 and i; pick the closer one.
            candidates: list[datetime] = []
            if i > 0:
                candidates.append(station_keys[i - 1])
            if i < n:
                candidates.append(station_keys[i])
            nearest = min(candidates, key=lambda k: abs((ts - k).total_seconds()))
            if abs((ts - nearest).total_seconds()) <= 600:
                row = by_ts.setdefault(nearest, {})
                for k, v in vals.items():
                    if v is not None:
                        row[k] = v
            else:
                by_ts[ts] = dict(vals)
        return sorted(by_ts.items(), key=lambda kv: kv[0])

    def _upsert_synthetic_window(self) -> None:
        if not _SYNTH_OK:
            return
        end = datetime.now(UTC).replace(microsecond=0, second=0)
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
        rng_w = _np.random.default_rng(seed + 1)  # independent draw for weather
        start_naive = start.replace(tzinfo=None)
        end_naive = end.replace(tzinfo=None)

        # ---- sonde readings ----
        try:
            arrays = _synth.synthesize(start_naive, end_naive, rng)
        except Exception:
            log.exception("synthetic sonde generation failed")
            arrays = None

        if arrays is not None:
            n = len(arrays["ts"])
            rows: list[tuple[datetime, dict]] = []
            keys = ("water_temp_c", "ph", "do_pct", "chlorophyll_rfu",
                    "phycoerythrin_rfu", "turbidity_fnu", "spcond_ms_cm")
            for i in range(n):
                t = datetime.utcfromtimestamp(int(arrays["ts"][i].astype("int64"))).replace(tzinfo=UTC)
                rows.append((t, {k: float(arrays[k][i]) for k in keys}))
            try:
                inserted = self._upsert(rows, source="synthetic")
            except Exception:
                log.exception("synthetic sonde upsert failed")
                inserted = 0
            self.stats.total_inserted += inserted
            if inserted:
                self.stats.last_insert_at = datetime.now(UTC)
            log.info("synthetic stub (sonde): upserted %d row(s) over %d %s window (total=%d)",
                     inserted, days_or_hours[0], days_or_hours[1], self.stats.total_inserted)

        # ---- weather readings ----
        if not hasattr(_synth, "synthesize_weather"):
            return  # older generator; weather support not yet present
        try:
            w_arrays = _synth.synthesize_weather(start_naive, end_naive, rng_w)
        except Exception:
            log.exception("synthetic weather generation failed")
            return
        w_n = len(w_arrays["ts"])
        w_rows: list[tuple[datetime, dict]] = []
        w_keys = (
            "bar_press_hpa", "wind_dir_avg_deg", "wind_dir_min_deg", "wind_dir_max_deg",
            "wind_speed_avg_ms", "wind_speed_min_ms", "wind_speed_max_ms",
            "air_temp_c", "rel_humidity_pct",
            "rain_accum_mm", "rain_dur_frac_hr", "rain_int_mm_hr", "rain_peak_int_mm_hr",
            "hail_accum", "hail_dur", "hail_int", "hail_peak_int",
            "water_level_mm", "solar_rad_wm2",
            "cloud_cover_pct", "dew_point_c", "forecast_humidity_pct",
            "precip_intensity_mm_hr", "precip_prob_pct",
            "press_sea_level_hpa", "press_surface_hpa", "temp_apparent_c", "uv_index",
        )
        for i in range(w_n):
            t = datetime.utcfromtimestamp(int(w_arrays["ts"][i].astype("int64"))).replace(tzinfo=UTC)
            vals: dict = {k: float(w_arrays[k][i]) for k in w_keys}
            vals["weather_code"] = int(w_arrays["weather_code"][i])
            w_rows.append((t, vals))
        try:
            w_inserted = self._upsert_weather(w_rows, source="synthetic")
        except Exception:
            log.exception("synthetic weather upsert failed")
            return
        self.stats.weather_inserted_total += w_inserted
        if w_inserted:
            self.stats.last_weather_insert_at = datetime.now(UTC)
        log.info("synthetic stub (weather): upserted %d row(s) (total=%d)",
                 w_inserted, self.stats.weather_inserted_total)

    async def _probe_deployment(self, client: httpx.AsyncClient) -> None:
        url = f"{self.cfg.base_url}/manager/deployments/by-uri/{self.cfg.deployment_uri}"
        try:
            r = await client.get(url)
            self.stats.last_status_code = r.status_code
            if r.status_code == 200:
                j = r.json()
                result = j.get("result") or {}
                self.stats.deployment_active = bool(result.get("active"))
                self.stats.last_probe_ok_at = datetime.now(UTC)
                if not self.stats.deployment_active:
                    log.warning("deployment %s is not active", self.cfg.deployment_uri)
            else:
                log.warning("deployment probe HTTP %s", r.status_code)
        except Exception as e:
            self.stats.last_error = f"probe: {e}"
            log.warning("deployment probe failed: %s", e)

    async def _fetch_and_store(self, client: httpx.AsyncClient) -> None:
        cfg = self.cfg
        self.stats.last_fetch_attempt_at = datetime.now(UTC)

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
        self.stats.last_insert_at = datetime.now(UTC)
        log.info("inserted %d rows (total=%d)", inserted, self.stats.total_inserted)

    def _upsert(
        self,
        rows: Iterable[tuple[datetime, dict]],
        source: str | None = None,
        uri: str | None = None,
    ) -> int:
        cfg = self.cfg
        src = source if source is not None else cfg.source_tag
        deploy_uri = uri if uri is not None else cfg.deployment_uri
        payload = []
        for ts, values in rows:
            payload.append((
                ts, deploy_uri,
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

    # Column order in the INSERT must match this tuple exactly.
    _WEATHER_INSERT_COLS = (
        "bar_press_hpa",
        "wind_dir_avg_deg", "wind_dir_min_deg", "wind_dir_max_deg",
        "wind_speed_avg_ms", "wind_speed_min_ms", "wind_speed_max_ms",
        "air_temp_c", "rel_humidity_pct",
        "rain_accum_mm", "rain_dur_frac_hr", "rain_int_mm_hr", "rain_peak_int_mm_hr",
        "hail_accum", "hail_dur", "hail_int", "hail_peak_int",
        "water_level_mm", "solar_rad_wm2",
        "cloud_cover_pct", "dew_point_c", "forecast_humidity_pct",
        "precip_intensity_mm_hr", "precip_prob_pct",
        "press_sea_level_hpa", "press_surface_hpa",
        "temp_apparent_c", "uv_index", "weather_code",
    )

    def _upsert_weather(
        self,
        rows: Iterable[tuple[datetime, dict]],
        source: str | None = None,
        uri: str | None = None,
    ) -> int:
        cfg = self.cfg
        src = source if source is not None else cfg.source_tag
        deploy_uri = uri if uri is not None else cfg.weather_deployment_uri
        payload = []
        for ts, values in rows:
            row = [ts, deploy_uri]
            row.extend(values.get(c) for c in self._WEATHER_INSERT_COLS)
            row.append(src)
            payload.append(tuple(row))
        if not payload:
            return 0
        col_list = ", ".join(("ts", "deployment_uri", *self._WEATHER_INSERT_COLS, "source"))
        placeholders = ",".join(["%s"] * (2 + len(self._WEATHER_INSERT_COLS) + 1))
        sql = (
            f"INSERT INTO weather_readings ({col_list}) "
            f"VALUES ({placeholders}) "
            f"ON CONFLICT (deployment_uri, ts) DO NOTHING"
        )
        with psycopg.connect(cfg.database_url) as conn, conn.cursor() as cur:
            cur.executemany(sql, payload)
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
            "data_source": s.last_data_source or ("wahoobay" if s.wahoobay_enabled else "synthetic"),
            "deployment_uri": cfg.deployment_uri,
            "weather_deployment_uri": cfg.weather_deployment_uri,
            "token_configured": s.token_configured,
            "deployment_active": s.deployment_active,
            "last_status_code": s.last_status_code,
            "started_at": s.started_at.isoformat(),
            "last_probe_ok_at": s.last_probe_ok_at.isoformat() if s.last_probe_ok_at else None,
            "last_fetch_attempt_at": s.last_fetch_attempt_at.isoformat() if s.last_fetch_attempt_at else None,
            "last_insert_at": s.last_insert_at.isoformat() if s.last_insert_at else None,
            "last_weather_insert_at": s.last_weather_insert_at.isoformat() if s.last_weather_insert_at else None,
            "total_inserted": s.total_inserted,
            "weather_inserted_total": s.weather_inserted_total,
            "last_error": s.last_error,
            "wahoobay": {
                "enabled": s.wahoobay_enabled,
                "api_url": cfg.wahoobay_api_url,
                "last_ok_at": s.wahoobay_last_ok_at.isoformat() if s.wahoobay_last_ok_at else None,
                "last_error": s.wahoobay_last_error,
                "sonde_inserted_total": s.wahoobay_sonde_inserted_total,
                "weather_inserted_total": s.wahoobay_weather_inserted_total,
            },
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

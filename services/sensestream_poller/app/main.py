"""Sensor + weather poller for the Wahoo Bay live dashboard.

Every POLL_INTERVAL_S the poller tries three data sources in order:

  1. **Wahoo Bay public API** (``api.wahoobay.net``). No auth required.
     Endpoints:
       - ``/api/v1/sitedata/latest?site=3`` — the EXO2 sonde (WAHOOBAY_SITE_SONDE)
         (water temp, pH, DO, chlorophyll, phycoerythrin, turbidity, sPCond)
       - ``/api/v1/sitedata/latest?site=1`` — the Vaisala WXT-536 (WAHOOBAY_SITE_WEATHER)
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

Sonde parameter mapping (EXO2):
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

from .config import settings

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
    auth_token: str  # optional; legacy direct-SenseStream path uses this
    deployment_uri: str  # tag on sonde readings; required, there is no default site
    weather_deployment_uri: str  # tag on weather rows; defaults to deployment_uri
    poll_interval_s: int
    backfill_minutes: int  # window to fetch each poll (overlap tolerated; UNIQUE prevents dupes)
    initial_backfill_days: int  # one-shot historical window on first tick
    source_tag: str  # value written to *_readings.source for live data
    measurements_path_template: str  # supports {uri} — legacy direct-SenseStream path
    wahoobay_api_url: str  # base URL for the public Wahoo Bay API
    wahoobay_site_sonde: int  # /api/v1/sitedata site id for the EXO2 sonde
    wahoobay_site_weather: int  # /api/v1/sitedata site id for the weather station
    wahoobay_enabled: bool
    open_meteo_url: str  # base URL for Open-Meteo forecast layer
    nws_metar_station: str  # ICAO station id for NWS text observations
    nws_user_agent: str  # required by api.weather.gov (rejects empty UAs)
    site_latitude: float  # Wahoo Bay location for Open-Meteo / NWS lookups
    site_longitude: float
    database_url: str
    http_host: str
    http_port: int
    log_level: str

    @classmethod
    def from_env(cls) -> Config:
        site = settings()
        return cls(
            base_url=_env("SENSESTREAM_BASE_URL", "https://api.sensestream.org").rstrip("/"),
            auth_token=_env("SENSESTREAM_AUTH_TOKEN", ""),
            deployment_uri=site.deployment_uri,
            # Weather rows go under the same deployment tag as the sonde so the
            # dashboard's default query (this site's deployment) returns them
            # without a migration; SENSESTREAM_WEATHER_DEPLOYMENT_URI splits
            # them out.
            weather_deployment_uri=site.weather_deployment_uri,
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
            wahoobay_enabled=_env("WAHOOBAY_API_ENABLED", "1").lower()
            not in ("0", "false", "no", ""),
            open_meteo_url=_env("OPEN_METEO_URL", "https://api.open-meteo.com").rstrip("/"),
            nws_metar_station=_env("NWS_METAR_STATION", "KPMP"),
            nws_user_agent=_env("NWS_USER_AGENT", "wahoobay-dashboard (scoopnisker@gmail.com)"),
            site_latitude=float(_env("WAHOOBAY_LAT", "26.2603")),
            site_longitude=float(_env("WAHOOBAY_LON", "-80.0834")),
            database_url=site.database_url,
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
    "ph": "ph",
    "do": "do_pct",
    "chlorophyll (rfu)": "chlorophyll_rfu",
    "chlorophyll": "chlorophyll_rfu",
    "phycoerythrin (rfu)": "phycoerythrin_rfu",
    "phycoerythrin": "phycoerythrin_rfu",
    "turbidity (fnu)": "turbidity_fnu",
    "turbidity": "turbidity_fnu",
    "spcond": "spcond_ms_cm",
    "specific conductance": "spcond_ms_cm",
    # NO3-N nitrate sensor was removed from the sonde 2026-05; if a
    # future feed re-includes it, restore the mapping here.
}


COLUMNS_ORDER = (
    "water_temp_c",
    "ph",
    "do_pct",
    "chlorophyll_rfu",
    "phycoerythrin_rfu",
    "turbidity_fnu",
    "spcond_ms_cm",
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
#   - sonde:    /manager/deployments/by-uri/<SENSESTREAM_DEPLOYMENT_URI> sensor metadata
#   - weather:  progressNotes.md in the wahoobay/wahoobay.github.io repo
#   - forecast: Tomorrow.io field order (alphabetical) — confirmed against
#     live values (pressureSeaLevel ≈ 1015, temperature ≈ 27°C, weatherCode = 1000).
# A wrong index would silently store junk, so unit-plausibility checks below
# would catch sensor reshuffles.


def _parse_readings_payload(payload: Any) -> list[tuple[datetime, list]]:
    """Pull (ts, readings[]) tuples out of a Wahoo Bay or SenseStream payload.

    Accepts:
      - Wahoo Bay /latest:       ``{"time": <ms>, "readings": [...]}``
      - Wahoo Bay range:         ``[{"time": ..., "readings": ...}, ...]``
      - SenseStream observations: ``{"errorCode": 0, "errorMessage": "success",
                                     "result": [{"time": ..., "readings": ...}, ...]}``
    """
    if payload is None:
        return []
    # Unwrap SenseStream's {"errorCode", "errorMessage", "result"} envelope.
    if isinstance(payload, dict) and "result" in payload and "readings" not in payload:
        payload = payload["result"]
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
    """Convert EXO2 sonde readings (8 positional values).

    Index order from /manager/deployments/by-uri/<deployment>:
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
            "water_temp_c": _get(r, 0),
            "ph": _get(r, 1),
            "do_pct": _get(r, 2),
            "chlorophyll_rfu": _get(r, 3),
            "phycoerythrin_rfu": _get(r, 4),
            "turbidity_fnu": _get(r, 5),
            "spcond_ms_cm": _get(r, 7),
        }
        # Drop entirely-empty rows (sensor offline / payload junk).
        if any(v is not None for v in values.values()):
            rows.append((ts, values))
    return rows


def parse_wahoobay_weather_station(payload: Any) -> list[tuple[datetime, dict]]:
    """Convert Vaisala WXT-536 weather-station readings (24 positional values).

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
            "bar_press_hpa": _get(r, 0),
            "wind_dir_min_deg": _get(r, 1),
            "wind_dir_avg_deg": _get(r, 2),
            "wind_dir_max_deg": _get(r, 3),
            "wind_speed_min_ms": _get(r, 4),
            "wind_speed_avg_ms": _get(r, 5),
            "wind_speed_max_ms": _get(r, 6),
            "air_temp_c": _get(r, 7),
            "rel_humidity_pct": _get(r, 9),
            "rain_accum_mm": _get(r, 10),
            "rain_dur_frac_hr": (rain_dur_raw / 3600.0) if rain_dur_raw is not None else None,
            "rain_int_mm_hr": _get(r, 12),
            "hail_accum": _get(r, 13),
            "hail_dur": (hail_dur_raw / 3600.0) if hail_dur_raw is not None else None,
            "hail_int": _get(r, 15),
            "rain_peak_int_mm_hr": _get(r, 16),
            "hail_peak_int": _get(r, 17),
            "water_level_mm": (1024.0 * 10.0 * water_level_raw / 3.3)
            if water_level_raw is not None
            else None,
            "solar_rad_wm2": (solar_raw * 566.25) if solar_raw is not None else None,
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
            "cloud_cover_pct": _get(r, 0),
            "dew_point_c": _get(r, 1),
            "forecast_humidity_pct": _get(r, 2),
            "precip_intensity_mm_hr": _get(r, 3),
            "precip_prob_pct": _get(r, 4),
            "press_sea_level_hpa": _get(r, 5),
            "press_surface_hpa": _get(r, 6),
            "temp_apparent_c": _get(r, 8),
            "uv_index": _get(r, 9),
            "weather_code": int(wc_raw) if wc_raw is not None else None,
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
            primary,
            self.cfg.deployment_uri,
            self.cfg.weather_deployment_uri,
            self.cfg.poll_interval_s,
            self.cfg.initial_backfill_days,
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
        cfg = self.cfg
        end = datetime.now(UTC).replace(microsecond=0, second=0)
        first_run = self.stats.total_inserted == 0 and self.stats.weather_inserted_total == 0
        start = end - (
            timedelta(days=cfg.initial_backfill_days)
            if first_run
            else timedelta(minutes=cfg.backfill_minutes)
        )

        # Stage 1: sonde + weather station observations. Try Wahoo Bay's
        # proxy first, then api.sensestream.org direct. Both return the same
        # SenseStream readings (Wahoo Bay just proxies); we keep two paths so
        # one outage doesn't take us offline.
        prev_sonde = self.stats.total_inserted
        station_rows: list[tuple[datetime, dict]] = []
        data_source = ""
        if self.stats.wahoobay_enabled:
            station_rows = await self._tick_wahoobay(client, start, end)
            if station_rows or self.stats.total_inserted > prev_sonde:
                data_source = "wahoobay"
            else:
                log.warning("Wahoo Bay API tick produced no rows; falling back to next source")

        if not data_source:
            prev_sonde = self.stats.total_inserted
            await self._probe_deployment(client)
            station_rows = await self._tick_sensestream_direct(client, start, end)
            if station_rows or self.stats.total_inserted > prev_sonde:
                data_source = "sensestream"
            else:
                log.warning("SenseStream direct tick produced no rows; falling back to next source")

        # Stage 2: forecast layer. Open-Meteo (no key, no rate limit) supplies
        # the cloud-cover / dew-point / apparent-temp / UV / weather-code
        # columns; NWS METAR from KPMP (2 mi from Wahoo Bay) supplies a
        # human-readable text description. Both run on every tick, regardless
        # of which Stage 1 path supplied station data — they're independent.
        if data_source:
            try:
                await self._apply_forecast_layer(client, start, end, station_rows)
            except Exception:
                log.exception("forecast-layer step failed (continuing)")
            self.stats.last_data_source = data_source
            return

        # Legacy authenticated direct-SenseStream path (only useful if a token
        # is configured; otherwise behaves the same as the unauthenticated one
        # above).
        if cfg.auth_token:
            await self._fetch_and_store(client)
            try:
                await self._apply_forecast_layer(client, start, end, [])
            except Exception:
                log.exception("forecast-layer step failed (continuing)")
            self.stats.last_data_source = "sensestream_auth"
            return

        # Last resort: synthetic stub generator (no live source reachable).
        await asyncio.get_running_loop().run_in_executor(None, self._upsert_synthetic_window)
        self.stats.last_data_source = "synthetic"

    # ---- SenseStream-direct path (unauthenticated) -----------------------

    async def _tick_sensestream_direct(
        self,
        client: httpx.AsyncClient,
        start: datetime,
        end: datetime,
    ) -> list[tuple[datetime, dict]]:
        """Pull sonde + weather-station directly from api.sensestream.org.

        Same positional readings shape as the Wahoo Bay API (Wahoo Bay's
        backend just proxies these), so our existing parsers apply. Sonde
        rows are upserted to ``sensor_readings`` here. Weather-station rows
        are RETURNED for merging with the forecast layer downstream.
        """
        cfg = self.cfg
        start_ms = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        base = cfg.base_url

        # Sonde → sensor_readings
        try:
            r = await client.get(
                f"{base}/observations/measurements/{cfg.deployment_uri}/0",
                params={"start": start_ms, "end": end_ms},
            )
            if r.status_code == 200:
                rows = parse_wahoobay_sonde(r.json())
                inserted = self._upsert(rows, source=cfg.source_tag, uri=cfg.deployment_uri)
                self.stats.total_inserted += inserted
                if inserted:
                    self.stats.last_insert_at = datetime.now(UTC)
                log.info(
                    "sensestream-direct sonde: %d row(s), %d inserted (%s..%s)",
                    len(rows),
                    inserted,
                    start.isoformat(timespec="minutes"),
                    end.isoformat(timespec="minutes"),
                )
            else:
                log.warning("sensestream-direct sonde HTTP %s", r.status_code)
        except Exception as e:
            log.warning("sensestream-direct sonde failed: %s", e)

        # Weather station — returned, not upserted yet. The direct path needs
        # the station's own SenseStream URI (SENSESTREAM_WEATHER_FETCH_URI);
        # without one the station is skipped.
        station_rows: list[tuple[datetime, dict]] = []
        uri = _env("SENSESTREAM_WEATHER_FETCH_URI", "")
        if not uri:
            log.debug("SENSESTREAM_WEATHER_FETCH_URI unset; direct weather-station fetch skipped")
        else:
            try:
                r = await client.get(
                    f"{base}/observations/measurements/{uri}/0",
                    params={"start": start_ms, "end": end_ms},
                )
                if r.status_code == 200:
                    station_rows = parse_wahoobay_weather_station(r.json())
                    log.info(
                        "sensestream-direct weather station: %d row(s) fetched", len(station_rows)
                    )
                else:
                    log.warning("sensestream-direct weather HTTP %s", r.status_code)
            except Exception as e:
                log.warning("sensestream-direct weather failed: %s", e)

        return station_rows

    # ---- Wahoo Bay public API path ----------------------------------------

    async def _tick_wahoobay(
        self,
        client: httpx.AsyncClient,
        start: datetime,
        end: datetime,
    ) -> list[tuple[datetime, dict]]:
        """Pull sonde + weather-station observations from api.wahoobay.net.

        Sonde rows are upserted to ``sensor_readings`` here. Weather-station
        rows are RETURNED (not upserted yet) so the caller can merge them
        with the forecast layer before writing to ``weather_readings``. The
        Tomorrow.io feed Wahoo Bay used to bundle here is dropped — we get
        the same fields more reliably from Open-Meteo in the forecast layer
        step.
        """
        cfg = self.cfg
        start_ms = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        base = cfg.wahoobay_api_url

        # Sonde → sensor_readings
        try:
            r = await client.get(
                f"{base}/api/v1/sitedata",
                params={
                    "site": cfg.wahoobay_site_sonde,
                    "start": start_ms,
                    "end": end_ms,
                },
            )
            if r.status_code == 200:
                rows = parse_wahoobay_sonde(r.json())
                inserted = self._upsert(rows, source=cfg.source_tag, uri=cfg.deployment_uri)
                self.stats.wahoobay_sonde_inserted_total += inserted
                self.stats.total_inserted += inserted
                if inserted:
                    self.stats.last_insert_at = datetime.now(UTC)
                log.info(
                    "wahoobay sonde: %d row(s), %d inserted (%s..%s)",
                    len(rows),
                    inserted,
                    start.isoformat(timespec="minutes"),
                    end.isoformat(timespec="minutes"),
                )
                self.stats.wahoobay_last_ok_at = datetime.now(UTC)
            else:
                self.stats.wahoobay_last_error = f"sonde HTTP {r.status_code}"
                log.warning("wahoobay sonde fetch HTTP %s", r.status_code)
        except Exception as e:
            self.stats.wahoobay_last_error = f"sonde: {e}"
            log.warning("wahoobay sonde fetch failed: %s", e)

        # Weather station — returned, not upserted yet
        station_rows: list[tuple[datetime, dict]] = []
        try:
            r = await client.get(
                f"{base}/api/v1/sitedata",
                params={
                    "site": cfg.wahoobay_site_weather,
                    "start": start_ms,
                    "end": end_ms,
                },
            )
            if r.status_code == 200:
                station_rows = parse_wahoobay_weather_station(r.json())
                self.stats.wahoobay_last_ok_at = datetime.now(UTC)
            else:
                self.stats.wahoobay_last_error = f"weather HTTP {r.status_code}"
                log.warning("wahoobay weather fetch HTTP %s", r.status_code)
        except Exception as e:
            self.stats.wahoobay_last_error = f"weather: {e}"
            log.warning("wahoobay weather fetch failed: %s", e)

        return station_rows

    # ---- Forecast layer (Open-Meteo + NWS METAR) -------------------------

    async def _fetch_open_meteo(
        self,
        client: httpx.AsyncClient,
        start: datetime,
        end: datetime,
    ) -> list[tuple[datetime, dict]]:
        """Fetch the forecast layer from Open-Meteo.

        Open-Meteo is free, no API key, no rate limits — fills all the
        columns Tomorrow.io used to provide (cloud cover, dew point,
        apparent temp, precipitation probability, surface + sea-level
        pressure, UV index, weather code). Weather codes follow the WMO
        spec (0=clear, 3=overcast, 95=thunderstorm, ...) — different
        numbering from Tomorrow.io, mapping handled in the dashboard.

        Returns hourly rows in our weather_readings forecast-column shape.
        """
        cfg = self.cfg
        # past_days caps at 92; we cap initial backfill at 30d via config.
        days_back = max(1, (datetime.now(UTC) - start).days + 1)
        days_back = min(days_back, 92)
        try:
            r = await client.get(
                f"{cfg.open_meteo_url}/v1/forecast",
                params={
                    "latitude": cfg.site_latitude,
                    "longitude": cfg.site_longitude,
                    "past_days": days_back,
                    "forecast_days": 1,
                    "hourly": ",".join(
                        (
                            "cloud_cover",
                            "weather_code",
                            "apparent_temperature",
                            "dew_point_2m",
                            "precipitation",
                            "precipitation_probability",
                            "pressure_msl",
                            "surface_pressure",
                            "uv_index",
                            "relative_humidity_2m",
                        )
                    ),
                    "timezone": "UTC",
                },
            )
            if r.status_code != 200:
                log.warning("open-meteo HTTP %s: %s", r.status_code, (r.text or "")[:160])
                return []
            j = r.json()
        except Exception as e:
            log.warning("open-meteo fetch failed: %s", e)
            return []

        hourly = j.get("hourly") or {}
        times = hourly.get("time") or []
        if not times:
            return []
        # Clip strictly to "now". Open-Meteo returns the rest of today's
        # forecast hours by default; we don't want future-hour rows inserted
        # with all station columns NULL — they'd beat the most-recent station
        # observation in ORDER BY ts DESC LIMIT 1 queries.
        rows: list[tuple[datetime, dict]] = []
        for i, t in enumerate(times):
            try:
                ts = datetime.fromisoformat(t).replace(tzinfo=UTC)
            except Exception:
                continue
            if ts < start - timedelta(hours=1) or ts > end:
                continue
            wc = _get(hourly.get("weather_code") or [], i)
            values = {
                "cloud_cover_pct": _get(hourly.get("cloud_cover") or [], i),
                "dew_point_c": _get(hourly.get("dew_point_2m") or [], i),
                "forecast_humidity_pct": _get(hourly.get("relative_humidity_2m") or [], i),
                "precip_intensity_mm_hr": _get(hourly.get("precipitation") or [], i),
                "precip_prob_pct": _get(hourly.get("precipitation_probability") or [], i),
                "press_sea_level_hpa": _get(hourly.get("pressure_msl") or [], i),
                "press_surface_hpa": _get(hourly.get("surface_pressure") or [], i),
                "temp_apparent_c": _get(hourly.get("apparent_temperature") or [], i),
                "uv_index": _get(hourly.get("uv_index") or [], i),
                "weather_code": int(wc) if wc is not None else None,
            }
            if any(v is not None for v in values.values()):
                rows.append((ts, values))
        return rows

    async def _fetch_nws_metar(
        self,
        client: httpx.AsyncClient,
        station: str,
    ) -> tuple[datetime, str] | None:
        """Fetch the latest METAR observation from NWS api.weather.gov.

        Returns (observation_ts, text_description) or None. KPMP (Pompano
        Beach Air Park) is ~2 miles from Wahoo Bay; the textDescription
        field is what the dashboard surfaces as "current conditions".
        """
        try:
            r = await client.get(
                f"https://api.weather.gov/stations/{station}/observations/latest",
                headers={"User-Agent": self.cfg.nws_user_agent},
            )
            if r.status_code != 200:
                log.warning("NWS METAR HTTP %s for %s", r.status_code, station)
                return None
            props = (r.json() or {}).get("properties") or {}
            desc = props.get("textDescription")
            ts_str = props.get("timestamp")
            if not desc or not ts_str:
                return None
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            return (ts, str(desc))
        except Exception as e:
            log.warning("NWS METAR fetch failed: %s", e)
            return None

    async def _apply_forecast_layer(
        self,
        client: httpx.AsyncClient,
        start: datetime,
        end: datetime,
        station_rows: list[tuple[datetime, dict]],
    ) -> None:
        """Merge station observations with Open-Meteo + NWS into one upsert.

        Open-Meteo runs hourly, the on-site station every ~10 min; each
        forecast row snaps to the nearest station timestamp within 30 min.
        NWS METAR provides a single latest text description, attached to
        the most recent merged row. Station-only rows pass through unchanged
        so a Wahoo Bay outage doesn't suppress on-site readings.
        """
        cfg = self.cfg
        forecast_rows = await self._fetch_open_meteo(client, start, end)
        metar = await self._fetch_nws_metar(client, cfg.nws_metar_station)

        # Open-Meteo is hourly; the closest forecast hour to any station row
        # is at most ~30 min away under perfect alignment, but station ticks
        # land on arbitrary seconds. 3600s covers the full hour either side
        # so every station row gets the forecast values from whichever hour
        # is nearest in time.
        merged = self._merge_weather_rows(station_rows, forecast_rows, snap_seconds=3600)
        if metar and merged:
            metar_ts, desc = metar
            # Attach text description to the row closest to the METAR observation
            # timestamp (NWS updates roughly hourly, so it propagates to one row
            # per tick — older rows keep their previous description).
            nearest_idx = min(
                range(len(merged)),
                key=lambda i: abs((merged[i][0] - metar_ts).total_seconds()),
            )
            merged[nearest_idx][1]["weather_description"] = desc
        if not merged:
            return
        inserted = self._upsert_weather(
            merged, source=cfg.source_tag, uri=cfg.weather_deployment_uri
        )
        self.stats.weather_inserted_total += inserted
        if inserted:
            self.stats.last_weather_insert_at = datetime.now(UTC)
        log.info(
            "forecast layer: station=%d open-meteo=%d nws=%s upserted=%d",
            len(station_rows),
            len(forecast_rows),
            "ok" if metar else "—",
            inserted,
        )

    @staticmethod
    def _merge_weather_rows(
        station: list[tuple[datetime, dict]],
        forecast: list[tuple[datetime, dict]],
        snap_seconds: int = 600,
    ) -> list[tuple[datetime, dict]]:
        """Combine on-site station rows with forecast-layer rows.

        The station runs every ~10 min; the forecast layer is hourly. We
        broadcast each forecast value onto EVERY station row within
        ``snap_seconds`` of the forecast timestamp by inverting the join:
        for each station row, look up the closest forecast row. That way
        the dashboard's "latest weather" query (ORDER BY ts DESC LIMIT 1)
        sees forecast columns regardless of which 10-min slot the latest
        station ping landed in.

        Forecast rows with no station row in range pass through unchanged so
        the forecast layer remains visible during station outages.
        """
        if not station and not forecast:
            return []
        if not station:
            return [(ts, dict(v)) for ts, v in forecast]

        by_ts: dict[datetime, dict] = {ts: dict(vals) for ts, vals in station}
        if not forecast:
            return sorted(by_ts.items(), key=lambda kv: kv[0])

        forecast_sorted = sorted(forecast, key=lambda kv: kv[0])
        forecast_keys = [ts for ts, _ in forecast_sorted]
        forecast_vals = [vals for _, vals in forecast_sorted]
        fn = len(forecast_keys)
        matched_forecasts: set[int] = set()

        # Bisect each station row's ts into forecast_keys to find the nearest
        # forecast within snap_seconds.
        for s_ts in by_ts:
            i = bisect.bisect_left(forecast_keys, s_ts)
            cand_idx: list[int] = []
            if i > 0:
                cand_idx.append(i - 1)
            if i < fn:
                cand_idx.append(i)
            if not cand_idx:
                continue
            nearest_idx = min(
                cand_idx, key=lambda j: abs((s_ts - forecast_keys[j]).total_seconds())
            )
            if abs((s_ts - forecast_keys[nearest_idx]).total_seconds()) <= snap_seconds:
                for k, v in forecast_vals[nearest_idx].items():
                    if v is not None:
                        by_ts[s_ts][k] = v
                matched_forecasts.add(nearest_idx)

        # Forecast rows that didn't get matched to any station row pass through
        # as forecast-only rows (e.g., during a station outage).
        for j in range(fn):
            if j not in matched_forecasts and forecast_keys[j] not in by_ts:
                by_ts[forecast_keys[j]] = dict(forecast_vals[j])

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
            keys = (
                "water_temp_c",
                "ph",
                "do_pct",
                "chlorophyll_rfu",
                "phycoerythrin_rfu",
                "turbidity_fnu",
                "spcond_ms_cm",
            )
            for i in range(n):
                t = datetime.utcfromtimestamp(int(arrays["ts"][i].astype("int64"))).replace(
                    tzinfo=UTC
                )
                rows.append((t, {k: float(arrays[k][i]) for k in keys}))
            try:
                inserted = self._upsert(rows, source="synthetic")
            except Exception:
                log.exception("synthetic sonde upsert failed")
                inserted = 0
            self.stats.total_inserted += inserted
            if inserted:
                self.stats.last_insert_at = datetime.now(UTC)
            log.info(
                "synthetic stub (sonde): upserted %d row(s) over %d %s window (total=%d)",
                inserted,
                days_or_hours[0],
                days_or_hours[1],
                self.stats.total_inserted,
            )

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
            "rain_dur_frac_hr",
            "rain_int_mm_hr",
            "rain_peak_int_mm_hr",
            "hail_accum",
            "hail_dur",
            "hail_int",
            "hail_peak_int",
            "water_level_mm",
            "solar_rad_wm2",
            "cloud_cover_pct",
            "dew_point_c",
            "forecast_humidity_pct",
            "precip_intensity_mm_hr",
            "precip_prob_pct",
            "press_sea_level_hpa",
            "press_surface_hpa",
            "temp_apparent_c",
            "uv_index",
        )
        for i in range(w_n):
            t = datetime.utcfromtimestamp(int(w_arrays["ts"][i].astype("int64"))).replace(
                tzinfo=UTC
            )
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
        log.info(
            "synthetic stub (weather): upserted %d row(s) (total=%d)",
            w_inserted,
            self.stats.weather_inserted_total,
        )

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
            payload.append(
                (
                    ts,
                    deploy_uri,
                    values.get("water_temp_c"),
                    values.get("ph"),
                    values.get("do_pct"),
                    values.get("chlorophyll_rfu"),
                    values.get("phycoerythrin_rfu"),
                    values.get("turbidity_fnu"),
                    values.get("spcond_ms_cm"),
                    src,
                )
            )
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
        "wind_dir_avg_deg",
        "wind_dir_min_deg",
        "wind_dir_max_deg",
        "wind_speed_avg_ms",
        "wind_speed_min_ms",
        "wind_speed_max_ms",
        "air_temp_c",
        "rel_humidity_pct",
        "rain_accum_mm",
        "rain_dur_frac_hr",
        "rain_int_mm_hr",
        "rain_peak_int_mm_hr",
        "hail_accum",
        "hail_dur",
        "hail_int",
        "hail_peak_int",
        "water_level_mm",
        "solar_rad_wm2",
        "cloud_cover_pct",
        "dew_point_c",
        "forecast_humidity_pct",
        "precip_intensity_mm_hr",
        "precip_prob_pct",
        "press_sea_level_hpa",
        "press_surface_hpa",
        "temp_apparent_c",
        "uv_index",
        "weather_code",
        "weather_description",
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
        # On conflict, fill in any missing columns from the new payload (e.g.,
        # back-filling forecast values onto rows the station wrote earlier).
        # COALESCE preserves the existing value when the new one is NULL so a
        # forecast-only update can't wipe a station-only column.
        update_clause = ", ".join(
            f"{c} = COALESCE(EXCLUDED.{c}, weather_readings.{c})" for c in self._WEATHER_INSERT_COLS
        )
        sql = (
            f"INSERT INTO weather_readings ({col_list}) "
            f"VALUES ({placeholders}) "
            f"ON CONFLICT (deployment_uri, ts) DO UPDATE SET {update_clause}"
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
        return JSONResponse(
            {
                "running": s.running,
                "data_source": s.last_data_source
                or ("wahoobay" if s.wahoobay_enabled else "synthetic"),
                "deployment_uri": cfg.deployment_uri,
                "weather_deployment_uri": cfg.weather_deployment_uri,
                "token_configured": s.token_configured,
                "deployment_active": s.deployment_active,
                "last_status_code": s.last_status_code,
                "started_at": s.started_at.isoformat(),
                "last_probe_ok_at": s.last_probe_ok_at.isoformat() if s.last_probe_ok_at else None,
                "last_fetch_attempt_at": s.last_fetch_attempt_at.isoformat()
                if s.last_fetch_attempt_at
                else None,
                "last_insert_at": s.last_insert_at.isoformat() if s.last_insert_at else None,
                "last_weather_insert_at": s.last_weather_insert_at.isoformat()
                if s.last_weather_insert_at
                else None,
                "total_inserted": s.total_inserted,
                "weather_inserted_total": s.weather_inserted_total,
                "last_error": s.last_error,
                "wahoobay": {
                    "enabled": s.wahoobay_enabled,
                    "api_url": cfg.wahoobay_api_url,
                    "last_ok_at": s.wahoobay_last_ok_at.isoformat()
                    if s.wahoobay_last_ok_at
                    else None,
                    "last_error": s.wahoobay_last_error,
                    "sonde_inserted_total": s.wahoobay_sonde_inserted_total,
                    "weather_inserted_total": s.wahoobay_weather_inserted_total,
                },
            }
        )

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

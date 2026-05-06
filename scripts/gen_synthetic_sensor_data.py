#!/usr/bin/env python3
"""Synthetic EXO2 sonde readings for Hillsboro Inlet / Wahoo Bay.

Generates a physics-motivated time series for the eight parameters reported
by the ``wahoo_2`` deployment on sensestream.org:

    - Water Temperature (°C)
    - pH
    - Dissolved Oxygen (% saturation)
    - Chlorophyll (RFU)
    - Phycoerythrin (RFU)
    - Turbidity (FNU)
    - Specific Conductance (mS/cm)
    (NO3-N nitrate sensor was removed from the sonde 2026-05;
     it's no longer generated or written.)

Model assumptions (Pompano Beach / Hillsboro Inlet, tidal, oligotrophic,
Atlantic-influenced with some Intracoastal mixing):
    - Seasonal water temperature sinusoid peaking in late August.
    - Solar-driven diurnal cycle for temp / pH / DO / chlorophyll.
    - M2 semidiurnal tide + S2 daily tide drives turbidity + minor mixing.
    - Rain events (Poisson-like, more frequent in wet season) dilute sPCond,
      pulse turbidity, transiently depress DO and pH.
    - Occasional chlorophyll bloom events, correlated with phycoerythrin.
    - Gaussian instrument noise per channel (scale set by typical EXO2 spec).

Outputs a CSV and optionally bulk-loads Postgres.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import numpy as np


# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------

SAMPLE_PERIOD_S = 600            # 10-minute cadence, matches sPeriod=600
LAT = 26.2603                    # Hillsboro Inlet / Wahoo Bay
LNG = -80.0834
DEPLOYMENT_URI = "wahoo_2"


# ---------------------------------------------------------------------------
# Seasonal + event drivers
# ---------------------------------------------------------------------------


def _day_of_year(ts: np.ndarray) -> np.ndarray:
    """Fractional day of year for each timestamp (float, 1..366)."""
    # ts is numpy datetime64[s]; convert per-element for DOY + hour is expensive;
    # compute seconds-since-year-start relative to each ts's year start.
    years = ts.astype("datetime64[Y]")
    year_start = years.astype("datetime64[s]")
    seconds_in = (ts - year_start).astype("int64")
    # leap-year length handled implicitly by using seconds-from-year-start.
    return 1.0 + seconds_in / 86400.0


def _hour_of_day(ts: np.ndarray) -> np.ndarray:
    days = ts.astype("datetime64[D]")
    day_start = days.astype("datetime64[s]")
    return (ts - day_start).astype("int64") / 3600.0


def _seasonal_temp(doy: np.ndarray) -> np.ndarray:
    """Water temperature seasonal cycle peaking late August (DOY ~240)."""
    return 25.0 + 4.5 * np.sin(2 * np.pi * (doy - 240) / 365.25)


def _wet_season(doy: np.ndarray) -> np.ndarray:
    """0..1 intensity curve, peaks late summer (Jun-Oct wet season)."""
    phase = np.clip(np.sin(2 * np.pi * (doy - 215) / 365.25), 0.0, 1.0)
    return phase ** 2


def _rain_series(ts: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """Construct a 0..1 'storm decay' signal with exponential tails on events.

    Rain probability is higher in wet season. Each event has random magnitude
    and a 6-hour e-folding decay.
    """
    n = len(ts)
    doy = _day_of_year(ts)
    wet = _wet_season(doy)
    # per-sample probability of an event start
    base_prob = 0.003 * (0.3 + 1.7 * wet)          # ~3-20x / month depending on season
    starts = rng.random(n) < base_prob
    magnitudes = np.where(starts, rng.gamma(shape=2.0, scale=0.5, size=n), 0.0)
    # convolve with exponential decay (tau = 6 h = 36 samples at 10-min cadence)
    tau_samples = 6 * 60 // (SAMPLE_PERIOD_S // 60)
    kernel = np.exp(-np.arange(0, 6 * tau_samples) / tau_samples)
    storm = np.convolve(magnitudes, kernel, mode="full")[:n]
    # clip to a reasonable [0, 3] range so downstream coupling is bounded
    return np.clip(storm, 0.0, 3.0)


def _bloom_series(ts: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """Rare chlorophyll/phycoerythrin blooms, biased toward warm months."""
    n = len(ts)
    doy = _day_of_year(ts)
    warm = 0.3 + 0.7 * np.clip(np.sin(2 * np.pi * (doy - 200) / 365.25), 0.0, 1.0)
    base_prob = 0.00015 * warm                      # sparse
    starts = rng.random(n) < base_prob
    magnitudes = np.where(starts, rng.gamma(shape=2.5, scale=1.2, size=n), 0.0)
    tau_samples = 18 * 60 // (SAMPLE_PERIOD_S // 60)  # 18h e-folding
    kernel = np.exp(-np.arange(0, 6 * tau_samples) / tau_samples)
    return np.clip(np.convolve(magnitudes, kernel, mode="full")[:n], 0.0, 8.0)


# ---------------------------------------------------------------------------
# Parameter synthesizers
# ---------------------------------------------------------------------------


def synthesize(
    start: datetime,
    end: datetime,
    rng: np.random.Generator,
) -> dict:
    """Return a dict of arrays: ts plus the eight measurements."""
    sp = SAMPLE_PERIOD_S
    # aligned to sample period
    start = start.replace(tzinfo=timezone.utc)
    end = end.replace(tzinfo=timezone.utc)
    steps = int((end - start).total_seconds() // sp)
    start_np = np.datetime64(start.replace(tzinfo=None), "s")
    ts = start_np + np.arange(steps, dtype="int64") * np.timedelta64(sp, "s")

    doy = _day_of_year(ts)
    hour = _hour_of_day(ts)

    # tidal phases (ignore nodal variations; close enough for synthetic)
    t_sec = np.arange(steps, dtype=np.float64) * sp
    M2 = np.sin(2 * np.pi * t_sec / (12.42 * 3600))        # dominant semidiurnal
    S2 = np.sin(2 * np.pi * t_sec / (12.00 * 3600))        # solar semidiurnal
    K1 = np.sin(2 * np.pi * t_sec / (23.93 * 3600))        # lunar diurnal
    tide_level = 0.55 * M2 + 0.20 * S2 + 0.15 * K1           # ~[-0.9, +0.9]

    rain = _rain_series(ts, rng)                             # 0..3
    bloom = _bloom_series(ts, rng)                           # 0..8

    # --- Water temperature ----------------------------------------------------
    seasonal = _seasonal_temp(doy)
    diurnal_temp = 0.9 * np.sin(2 * np.pi * (hour - 15.5) / 24.0)   # peaks ~3:30 pm
    tidal_mix_temp = 0.15 * M2                                # cooler ocean on flood
    rain_cool = -0.6 * rain
    noise_temp = rng.normal(0.0, 0.08, steps)
    water_temp_c = seasonal + diurnal_temp + tidal_mix_temp + rain_cool + noise_temp

    # --- pH -------------------------------------------------------------------
    # Coastal seawater with slight diurnal (photosynthesis lifts during day)
    diurnal_ph = 0.07 * np.sin(2 * np.pi * (hour - 14) / 24.0)
    rain_ph = -0.06 * rain                                    # dilution + runoff acidity
    bloom_ph = 0.04 * bloom / (1 + bloom) - 0.02 * np.roll(bloom, 6)  # bloom lifts then crashes
    ph_noise = rng.normal(0.0, 0.01, steps)
    ph = 8.05 + diurnal_ph + rain_ph + bloom_ph + ph_noise

    # --- DO % saturation ------------------------------------------------------
    # Inverse relationship to temperature (saturation concept), but DO% already
    # normalizes for temperature, so here we model biological drivers.
    diurnal_do = 8.0 * np.sin(2 * np.pi * (hour - 14) / 24.0)       # photosynthesis
    bloom_do = 4.0 * bloom / (1 + bloom) - 6.0 * np.roll(bloom, 12)  # bloom crash depletion
    rain_do = -3.5 * rain
    tide_do = 1.5 * M2                                        # better flushing on flood
    do_noise = rng.normal(0.0, 1.2, steps)
    do_pct = 95.0 + diurnal_do + bloom_do + rain_do + tide_do + do_noise
    do_pct = np.clip(do_pct, 40.0, 140.0)

    # --- Chlorophyll (RFU) ----------------------------------------------------
    base_chl = 1.1 + 0.3 * np.clip(np.sin(2 * np.pi * (doy - 200) / 365.25), -0.5, 1.5)
    diurnal_chl = 0.15 * np.sin(2 * np.pi * (hour - 13) / 24.0)
    chl_noise = rng.normal(0.0, 0.1, steps)
    chlorophyll_rfu = np.maximum(0.05, base_chl + diurnal_chl + 0.9 * bloom + chl_noise)

    # --- Phycoerythrin (RFU) --------------------------------------------------
    # Usually quiet in marine; slight bloom coupling.
    phyco_noise = rng.normal(0.0, 0.05, steps)
    phycoerythrin_rfu = np.maximum(0.02, 0.25 + 0.35 * bloom + phyco_noise)

    # --- Turbidity (FNU) ------------------------------------------------------
    # Tidal resuspension on ebb; storm spikes; bloom doesn't really touch FNU.
    ebb_flag = (np.gradient(tide_level) < 0).astype(float)
    turb_tidal = 0.8 * np.abs(M2) + 0.6 * ebb_flag * np.abs(M2)
    turb_storm = 4.0 * rain
    turb_noise = np.maximum(0.0, rng.normal(0.0, 0.25, steps))
    turbidity_fnu = 1.8 + turb_tidal + turb_storm + turb_noise

    # --- Specific Conductance (mS/cm) ----------------------------------------
    # Seawater ~53 mS/cm; freshwater dilution from storms + ICW mixing on ebb.
    base_sp = 52.5
    rain_dilution = -3.5 * rain
    tide_sp = 0.7 * M2                                         # saltier on flood
    wet_season_dilution = -1.0 * _wet_season(doy)
    sp_noise = rng.normal(0.0, 0.15, steps)
    spcond_ms_cm = base_sp + rain_dilution + tide_sp + wet_season_dilution + sp_noise
    spcond_ms_cm = np.clip(spcond_ms_cm, 20.0, 60.0)

    return {
        "ts": ts,
        "water_temp_c": water_temp_c,
        "ph": ph,
        "do_pct": do_pct,
        "chlorophyll_rfu": chlorophyll_rfu,
        "phycoerythrin_rfu": phycoerythrin_rfu,
        "turbidity_fnu": turbidity_fnu,
        "spcond_ms_cm": spcond_ms_cm,
    }


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------

COLUMNS = (
    "ts", "deployment_uri", "water_temp_c", "ph", "do_pct",
    "chlorophyll_rfu", "phycoerythrin_rfu", "turbidity_fnu",
    "spcond_ms_cm", "source",
)


def _iter_rows(arrays: dict, deployment_uri: str, source: str) -> Iterable[tuple]:
    ts = arrays["ts"]
    n = len(ts)
    for i in range(n):
        t = datetime.utcfromtimestamp(int(ts[i].astype("int64"))).replace(tzinfo=timezone.utc)
        yield (
            t.isoformat(),
            deployment_uri,
            round(float(arrays["water_temp_c"][i]), 3),
            round(float(arrays["ph"][i]), 3),
            round(float(arrays["do_pct"][i]), 2),
            round(float(arrays["chlorophyll_rfu"][i]), 3),
            round(float(arrays["phycoerythrin_rfu"][i]), 3),
            round(float(arrays["turbidity_fnu"][i]), 3),
            round(float(arrays["spcond_ms_cm"][i]), 3),
            source,
        )


def write_csv(path: Path, arrays: dict, deployment_uri: str, source: str) -> int:
    import csv
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(COLUMNS)
        n = 0
        for row in _iter_rows(arrays, deployment_uri, source):
            w.writerow(row)
            n += 1
    return n


def load_postgres(dsn: str, arrays: dict, deployment_uri: str, source: str) -> int:
    import psycopg
    rows = list(_iter_rows(arrays, deployment_uri, source))
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO sensor_readings
                (ts, deployment_uri, water_temp_c, ph, do_pct,
                 chlorophyll_rfu, phycoerythrin_rfu, turbidity_fnu,
                 spcond_ms_cm, source)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (deployment_uri, ts) DO NOTHING
            """,
            rows,
        )
        conn.commit()
    return len(rows)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=90,
                    help="Number of days ending at --end (default: 90)")
    ap.add_argument("--end", type=str, default=None,
                    help="ISO UTC end timestamp (default: now)")
    ap.add_argument("--seed", type=int, default=20260414,
                    help="RNG seed (default: deterministic date-based)")
    ap.add_argument("--deployment", type=str, default=DEPLOYMENT_URI)
    ap.add_argument("--source-tag", type=str, default="synthetic",
                    help="Value stored in the 'source' column (default: synthetic)")
    ap.add_argument("--csv", type=str,
                    default="data/synthetic/wahoo_2_sensor_readings.csv",
                    help="CSV output path (relative to repo root)")
    ap.add_argument("--no-csv", action="store_true", help="Skip CSV write")
    ap.add_argument("--load-postgres", action="store_true",
                    help="Insert into Postgres (requires DATABASE_URL env var)")
    return ap.parse_args()


def main() -> int:
    args = _parse_args()

    end = datetime.fromisoformat(args.end) if args.end else datetime.utcnow()
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    start = end - timedelta(days=args.days)

    rng = np.random.default_rng(args.seed)
    print(f"generating synthetic series  {start.isoformat()}  →  {end.isoformat()}")
    arrays = synthesize(start.replace(tzinfo=None), end.replace(tzinfo=None), rng)

    n = len(arrays["ts"])
    print(f"samples: {n:,} rows @ {SAMPLE_PERIOD_S}s cadence")

    for key in ("water_temp_c", "ph", "do_pct", "chlorophyll_rfu",
                "phycoerythrin_rfu", "turbidity_fnu", "spcond_ms_cm"):
        vals = arrays[key]
        print(f"  {key:20s}  mean={vals.mean():8.3f}  min={vals.min():8.3f}  max={vals.max():8.3f}")

    if not args.no_csv:
        csv_path = Path(args.csv)
        wrote = write_csv(csv_path, arrays, args.deployment, args.source_tag)
        print(f"wrote {wrote:,} rows → {csv_path}")

    if args.load_postgres:
        dsn = os.environ.get("DATABASE_URL")
        if not dsn:
            print("DATABASE_URL not set; refusing to load postgres", file=sys.stderr)
            return 2
        loaded = load_postgres(dsn, arrays, args.deployment, args.source_tag)
        print(f"inserted {loaded:,} rows into sensor_readings (ON CONFLICT DO NOTHING)")

    return 0


if __name__ == "__main__":
    sys.exit(main())

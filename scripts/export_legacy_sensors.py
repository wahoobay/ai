#!/usr/bin/env python3
"""Export the Java backend's sensor history to the two CSV files the import_legacy job reads.

    read -rs LEGACY_MYSQL_URL && export LEGACY_MYSQL_URL  # mysql://user:password@host[:port]/database
    python scripts/export_legacy_sensors.py --inspect     # first: the JSON blobs' keys, never values
    python scripts/export_legacy_sensors.py --out legacy/

The old site's backend keeps the water-quality sonde's readings in the typed table
water_quality (temperature in °F), under the sonde's datasource id (--water-datasource),
and the weather station's in sitedata, as JSON, under the station's (--weather-datasource);
Tomorrow.io's forecast is in weatherdata. Both JSON blobs are {"time": <epoch ms>, "readings": [positional values]}.
WEATHER_FIELDS says which value fills which column of weather.csv; --inspect shows the
keys and array lengths of the newest 50 rows of each, and water_quality's row count per
datasource id, so a layout that differs from the table is seen before anything is
exported (docs/data_pipeline.md, "Legacy history").

LEGACY_MYSQL_URL comes from the environment only and is never printed, nor is any part of
it (a connection failure reports the MySQL error number alone); the session is read-only
and runs SELECTs only. --out writes water_quality.csv and weather.csv, one row
per distinct ts, ascending, and prints each file's row count and first and last ts.
The MySQL driver is the `legacy` extra: pip install '.[legacy]'.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import sys
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

WATER_COLUMNS = (
    "water_temp_c",
    "ph",
    "do_pct",
    "chlorophyll_rfu",
    "phycoerythrin_rfu",
    "turbidity_fnu",
    "spcond_ms_cm",
)
WEATHER_COLUMNS = (
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
)

# water_quality's columns in WATER_COLUMNS order (after the epoch); no3_n_mg_l is left out
# (the nitrate sensor is gone), and water_temperature_f becomes °C.
WATER_SQL = (
    "SELECT `time`, water_temperature_f, ph, do_percent, chlorophyll_rfu, phycoerythrin_rfu, "
    "turbidity_fnu, spcond_mscm FROM water_quality WHERE datasource_id = %s ORDER BY `time`, id"
)
# The JSON sources of weather.csv, in the order their values win: (time, JSON) per row.
WEATHER_SOURCES = {
    "sitedata": "SELECT `time`, sitedata FROM sitedata WHERE datasourceid = %s ORDER BY `time`, id",
    "weatherdata": "SELECT `time`, weatherdata FROM weatherdata ORDER BY `time`, id",
}
WATER_COUNTS_SQL = "SELECT datasource_id, count(*) FROM water_quality GROUP BY 1 ORDER BY 1"
INSPECT_SQL = {
    "sitedata": "SELECT sitedata FROM sitedata WHERE datasourceid = %s ORDER BY `time` DESC LIMIT 50",
    "weatherdata": "SELECT weatherdata FROM weatherdata ORDER BY `time` DESC LIMIT 50",
}


def as_is(value: float) -> float:
    return value


# Which value fills each column of weather.csv: candidates (source, key, converter), the
# first one present in a row wins. A key is a top-level JSON key, or name[i] for the i-th
# value of an array; a column with no candidate found is written empty.
#   sitedata: the weather station (a Vaisala WXT-536), 24 readings in the order the Java
#     backend's own converter (SiteDataConversionService) and the live poller
#     (services/sensestream_poller, parse_wahoobay_weather_station) read them, already in
#     the header's units (hPa, degrees, m/s, °C, %, mm).
#   weatherdata: Tomorrow.io's values fill none of these columns: they are modelled, not
#     measured, and the live poller keeps them out of them too.
WEATHER_FIELDS: dict[str, list[tuple[str, str, Callable[[float], float]]]] = {
    "bar_press_hpa": [("sitedata", "readings[0]", as_is)],
    "wind_dir_avg_deg": [("sitedata", "readings[2]", as_is)],
    "wind_dir_min_deg": [("sitedata", "readings[1]", as_is)],
    "wind_dir_max_deg": [("sitedata", "readings[3]", as_is)],
    "wind_speed_avg_ms": [("sitedata", "readings[5]", as_is)],
    "wind_speed_min_ms": [("sitedata", "readings[4]", as_is)],
    "wind_speed_max_ms": [("sitedata", "readings[6]", as_is)],
    "air_temp_c": [("sitedata", "readings[7]", as_is)],
    "rel_humidity_pct": [("sitedata", "readings[9]", as_is)],
    "rain_accum_mm": [("sitedata", "readings[10]", as_is)],
}

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
BAD_URL = (
    "LEGACY_MYSQL_URL is not mysql://user:password@host[:port]/database "
    "(percent-encode any : / ? # @ in the password)"
)
_INDEXED = re.compile(r"^(\w+)\[(\d+)\]$")


# ---- pure helpers -----------------------------------------------------------------


def f_to_c(value: Any) -> float | None:
    """°F to °C, rounded to two places; None stays None."""
    if value is None:
        return None
    return round((float(value) - 32) * 5 / 9, 2)


def to_ms(value: Any) -> int:
    """An epoch in seconds or milliseconds (from 1e11 up it is milliseconds) as milliseconds."""
    if abs(value) >= 1e11:
        return int(value)
    return round(float(value) * 1000)


def epoch_to_iso(value: Any) -> str:
    """An epoch (seconds or milliseconds) as ISO 8601 UTC with a Z; milliseconds only if any."""
    ms = to_ms(value)
    ts = EPOCH + timedelta(milliseconds=ms)
    return ts.isoformat(timespec="milliseconds" if ms % 1000 else "seconds").replace("+00:00", "Z")


def _number(blob: Any, key: str) -> float | None:
    """The finite number at key (name, or name[i]) of a JSON object; None otherwise."""
    if not isinstance(blob, dict):
        return None
    indexed = _INDEXED.match(key)
    if indexed:
        seq, i = blob.get(indexed.group(1)), int(indexed.group(2))
        value = seq[i] if isinstance(seq, list) and i < len(seq) else None
    else:
        value = blob.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return None
    return float(value)


def map_weather(source: str, blob: Any) -> dict[str, float | None]:
    """One JSON blob of `source` as weather.csv's columns, through WEATHER_FIELDS."""
    out: dict[str, float | None] = {}
    for column in WEATHER_COLUMNS:
        out[column] = None
        for src, key, convert in WEATHER_FIELDS.get(column, ()):
            value = _number(blob, key) if src == source else None
            if value is not None:
                out[column] = convert(value)
                break
    return out


def blob_keys(blobs: Iterable[Any]) -> list[str]:
    """The distinct top-level keys of JSON objects, an array as name[0..n-1] for each
    length seen; keys and lengths only, never a value."""
    seen: set[tuple[str, int]] = set()
    for blob in blobs:
        if isinstance(blob, dict):
            seen.update((k, len(v) if isinstance(v, list) else -1) for k, v in blob.items())
    return [k if n < 0 else f"{k}[0..{n - 1}]" for k, n in sorted(seen)]


def _json(text: Any) -> Any:
    try:
        return json.loads(text) if text else None
    except (TypeError, ValueError):
        return None


def water_rows(records: Iterable[Sequence[Any]]) -> dict[int, list[Any]]:
    """water_quality rows (time, water_temperature_f, ph, do_percent, chlorophyll_rfu,
    phycoerythrin_rfu, turbidity_fnu, spcond_mscm) as {epoch ms: WATER_COLUMNS values};
    the first row of a ts wins, and a row with no value is left out."""
    out: dict[int, list[Any]] = {}
    for time, temp_f, *rest in records:
        ms = to_ms(time)
        values = [f_to_c(temp_f), *rest]
        if ms not in out and any(v is not None for v in values):
            out[ms] = values
    return out


def weather_rows(sources: Mapping[str, Iterable[tuple[Any, Any]]]) -> dict[int, list[Any]]:
    """(time, JSON) rows per source as {epoch ms: WEATHER_COLUMNS values} through
    WEATHER_FIELDS; for each ts and column the first value found wins (sources in the
    order given, rows in the order read), and a row with nothing mapped is left out."""
    out: dict[int, list[Any]] = {}
    for source, records in sources.items():
        for time, text in records:
            values = map_weather(source, _json(text))
            if all(v is None for v in values.values()):
                continue
            row = out.setdefault(to_ms(time), [None] * len(WEATHER_COLUMNS))
            for i, column in enumerate(WEATHER_COLUMNS):
                if row[i] is None:
                    row[i] = values[column]
    return out


def write_csv(
    path: Path, columns: Sequence[str], rows: Mapping[int, Sequence[Any]]
) -> tuple[int, str | None, str | None]:
    """ts and the columns, one row per ts, ascending; (rows written, first ts, last ts)."""
    times = sorted(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(["ts", *columns])
        for ms in times:
            writer.writerow([epoch_to_iso(ms), *("" if v is None else v for v in rows[ms])])
    if not times:
        return 0, None, None
    return len(times), epoch_to_iso(times[0]), epoch_to_iso(times[-1])


def mysql_params(url: str) -> dict[str, Any]:
    """The connection's parts. A malformed URL is refused without quoting any of it."""
    try:
        parts = urlsplit(url.strip())
        params = {
            "host": parts.hostname,
            "port": parts.port or 3306,
            "user": unquote(parts.username or ""),
            "password": unquote(parts.password or ""),
            "database": unquote(parts.path.lstrip("/")),
        }
    except ValueError:
        raise SystemExit(BAD_URL) from None
    if parts.scheme not in ("mysql", "mysql+mysqlconnector") or parts.query or parts.fragment:
        raise SystemExit(BAD_URL)
    if not (params["host"] and params["user"] and params["database"]):
        raise SystemExit(BAD_URL)
    # A database name is one path segment: '/' or '@' there means a raw one in the password
    # moved the host (mysql://u:ab@cd/ef@host/db reads as host 'cd'), which the driver's
    # error would then print.
    if "/" in params["database"] or "@" in params["database"]:
        raise SystemExit(BAD_URL)
    return params


# ---- the database ------------------------------------------------------------------


def connect(url: str) -> Any:
    """A read-only session on the Java backend's database."""
    try:
        import mysql.connector  # the `legacy` extra
    except ImportError:
        raise SystemExit("the MySQL driver is missing: pip install '.[legacy]'") from None
    params = mysql_params(url)
    try:
        conn = mysql.connector.connect(**params, connection_timeout=30)
        cur = conn.cursor()
        cur.execute("SET SESSION TRANSACTION READ ONLY")
        cur.close()
    except mysql.connector.Error as e:
        # The driver's message names the host or the user: the error number only.
        raise SystemExit(
            f"could not connect to the Java database (MySQL error {e.errno})"
        ) from None
    return conn


def _select(conn: Any, sql: str, params: Sequence[Any] = ()) -> Iterator[tuple]:
    cur = conn.cursor()
    try:
        cur.execute(sql, tuple(params))
        yield from cur
    finally:
        cur.close()


def _params(source: str, weather_datasource: int) -> tuple:
    return (weather_datasource,) if source == "sitedata" else ()


def inspect(conn: Any, weather_datasource: int) -> None:
    counts = ", ".join(f"{ds}={n}" for ds, n in _select(conn, WATER_COUNTS_SQL)) or "(none)"
    print(f"water_quality, rows per datasource_id: {counts}")
    for source, sql in INSPECT_SQL.items():
        blobs = [_json(text) for (text,) in _select(conn, sql, _params(source, weather_datasource))]
        label = f"sitedata (datasource {weather_datasource})" if source == "sitedata" else source
        keys = ", ".join(blob_keys(blobs)) or "(none)"
        unreadable = sum(1 for b in blobs if not isinstance(b, dict))
        note = f"; {unreadable} not a JSON object" if unreadable else ""
        print(f"{label}, newest {len(blobs)} rows: {keys}{note}")


def export(conn: Any, out: Path, weather_datasource: int, water_datasource: int) -> None:
    water = water_rows(_select(conn, WATER_SQL, (water_datasource,)))
    used = {source for candidates in WEATHER_FIELDS.values() for source, _, _ in candidates}
    weather = weather_rows(
        {
            source: _select(conn, sql, _params(source, weather_datasource))
            for source, sql in WEATHER_SOURCES.items()
            if source in used
        }
    )
    for name, columns, rows in (
        ("water_quality.csv", WATER_COLUMNS, water),
        ("weather.csv", WEATHER_COLUMNS, weather),
    ):
        count, first, last = write_csv(out / name, columns, rows)
        print(f"{name}: {count} rows, {first or '-'} .. {last or '-'}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--out", type=Path, help="directory for water_quality.csv and weather.csv")
    mode.add_argument(
        "--inspect", action="store_true", help="print the JSON blobs' keys (never values)"
    )
    ap.add_argument(
        "--water-datasource",
        type=int,
        default=3,
        help="the water-quality sonde's datasource id in the Java database (default: 3)",
    )
    ap.add_argument(
        "--weather-datasource",
        type=int,
        default=1,
        help="the weather station's datasource id in the Java database (default: 1)",
    )
    args = ap.parse_args(argv)
    url = os.environ.get("LEGACY_MYSQL_URL", "").strip()
    if not url:
        raise SystemExit(
            "set LEGACY_MYSQL_URL in the environment: read -rs LEGACY_MYSQL_URL && export LEGACY_MYSQL_URL"
        )
    conn = connect(url)
    try:
        if args.inspect:
            inspect(conn, args.weather_datasource)
        else:
            export(conn, args.out, args.weather_datasource, args.water_datasource)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

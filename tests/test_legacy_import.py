"""The Java backend's sensor history, brought in once (plan 7, task 11).

scripts/export_legacy_sensors.py turns the old site's MySQL tables into two CSV
files; the import_legacy job loads them from LEGACY_URL under the live readings,
never over them: only rows older than a table's first non-legacy reading for the
site's deployment, tagged source = 'legacy', and a second run inserts nothing.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import types
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from test_migrations import alembic, query

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests" / "fixtures" / "legacy"
sys.path.insert(0, str(REPO / "scripts"))

DEPLOYMENT = "site_x"
LIVE_TS = datetime(2026, 6, 1, tzinfo=UTC)
WATER_HEADER = (
    "ts,water_temp_c,ph,do_pct,chlorophyll_rfu,phycoerythrin_rfu,turbidity_fnu,spcond_ms_cm"
)
WEATHER_HEADER = (
    "ts,bar_press_hpa,wind_dir_avg_deg,wind_dir_min_deg,wind_dir_max_deg,wind_speed_avg_ms,"
    "wind_speed_min_ms,wind_speed_max_ms,air_temp_c,rel_humidity_pct,rain_accum_mm"
)

# A sitedata blob of the weather station (the Vaisala WXT-536) as the Java backend stores
# it: {"time": <ms>, "readings": [24 positional values]}, in the order its own converter
# (SiteDataConversionService) and the live poller read them.
STATION = {
    "time": 1735689600000,
    "readings": [
        1016.3,  # 0 barometric pressure (hPa)
        40.0,  # 1 wind direction min (deg)
        75.0,  # 2 wind direction avg
        110.0,  # 3 wind direction max
        1.2,  # 4 wind speed min (m/s)
        3.4,  # 5 wind speed avg
        6.1,  # 6 wind speed max
        22.5,  # 7 air temperature (°C)
        25.0,  # 8 internal temperature (diagnostic)
        68.0,  # 9 relative humidity (%)
        0.4,  # 10 rain accumulation (mm)
        *[0.0] * 7,  # 11-17 rain duration/intensity, hail
        0.1,  # 18 water level (raw V)
        0.5,  # 19 solar radiation (raw V)
        30.1,  # 20 heating temperature
        12.0,  # 21 heating voltage
        12.3,  # 22 supply voltage
        3.5,  # 23 reference voltage
    ],
}
# A weatherdata blob: Tomorrow.io's values, positional, in the response's (alphabetical) order.
FORECAST = {
    "time": 1735689900000,
    "readings": [20, 18.2, 70, 0, 5, 1016.9, 1016.1, 24.0, 24.5, 3, 1100, 80, 4.2],
}


def _import(pg_url: str, legacy_url: str = f"file://{FIXTURES}/", **budget) -> dict:
    from app.legacy_import import import_legacy
    from psycopg_pool import AsyncConnectionPool

    async def go() -> dict:
        async with AsyncConnectionPool(pg_url, min_size=0, max_size=2, open=False) as pool:
            return await import_legacy(
                pool,
                legacy_url=legacy_url,
                deployment_uri=DEPLOYMENT,
                location_id="reef_b",
                **budget,
            )

    return asyncio.run(go())


def _live(pg_url: str, ts: datetime, deployment: str = DEPLOYMENT) -> None:
    import psycopg

    with psycopg.connect(pg_url) as conn:
        conn.execute(
            "INSERT INTO sensor_readings (ts, deployment_uri, water_temp_c, source) "
            "VALUES (%s, %s, 99.5, 'live')",
            (ts, deployment),
        )
        conn.execute(
            "INSERT INTO weather_readings (ts, deployment_uri, air_temp_c, source) "
            "VALUES (%s, %s, 99.5, 'live')",
            (ts, deployment),
        )


# ---- the import job -----------------------------------------------------------------


def test_the_fixtures_load(pg_url):
    alembic("upgrade", "head", url=pg_url)
    out = _import(pg_url)
    assert out == {
        "water": {"read": 5, "inserted": 5},
        "weather": {"read": 5, "inserted": 5},
        "partial": False,
    }
    water = query(
        pg_url,
        "SELECT ts, deployment_uri, water_temp_c, ph, spcond_ms_cm FROM sensor_readings ORDER BY ts",
    )
    assert len(water) == 5
    assert water[0][:2] == (datetime(2025, 3, 1, tzinfo=UTC), DEPLOYMENT)
    assert water[0][2:] == (
        pytest.approx(24.11, abs=1e-4),
        pytest.approx(8.05, abs=1e-4),
        pytest.approx(52.1, abs=1e-4),
    )
    weather = query(
        pg_url, "SELECT ts, bar_press_hpa, air_temp_c FROM weather_readings ORDER BY ts"
    )
    assert len(weather) == 5
    assert weather[-1][0] == datetime(2026, 6, 3, 9, 10, tzinfo=UTC)
    assert weather[-1][1:] == (pytest.approx(1016.0), pytest.approx(26.4, abs=1e-4))
    # An empty cell is NULL, not zero.
    empty = datetime(2026, 5, 31, 23, 45, tzinfo=UTC)
    assert query(pg_url, "SELECT turbidity_fnu FROM sensor_readings WHERE ts = %s", empty) == [
        (None,)
    ]
    empty = datetime(2026, 5, 31, 23, 50, tzinfo=UTC)
    assert query(pg_url, "SELECT rain_accum_mm FROM weather_readings WHERE ts = %s", empty) == [
        (None,)
    ]


def test_every_imported_row_is_marked_legacy(pg_url):
    alembic("upgrade", "head", url=pg_url)
    _import(pg_url)
    for table in ("sensor_readings", "weather_readings"):
        assert query(
            pg_url, f"SELECT DISTINCT source, deployment_uri, location_id FROM {table}"
        ) == [("legacy", DEPLOYMENT, "reef_b")], table


def test_a_second_run_inserts_nothing(pg_url):
    alembic("upgrade", "head", url=pg_url)
    _import(pg_url)
    again = _import(pg_url)
    assert again == {
        "water": {"read": 5, "inserted": 0},
        "weather": {"read": 5, "inserted": 0},
        "partial": False,
    }
    assert query(pg_url, "SELECT count(*) FROM sensor_readings") == [(5,)]
    assert query(pg_url, "SELECT count(*) FROM weather_readings") == [(5,)]


def test_only_rows_older_than_the_first_live_reading_are_inserted(pg_url):
    alembic("upgrade", "head", url=pg_url)
    _live(pg_url, LIVE_TS)
    _live(pg_url, datetime(2026, 7, 1, tzinfo=UTC))
    # Another deployment's readings do not move this deployment's cut-off.
    _live(pg_url, datetime(2025, 1, 1, tzinfo=UTC), deployment="elsewhere")
    out = _import(pg_url)
    assert out == {
        "water": {"read": 5, "inserted": 3},
        "weather": {"read": 5, "inserted": 3},
        "partial": False,
    }
    for table, column in (("sensor_readings", "water_temp_c"), ("weather_readings", "air_temp_c")):
        mine = f"FROM {table} WHERE deployment_uri = %s"
        newest_legacy = query(pg_url, f"SELECT max(ts) {mine} AND source = 'legacy'", DEPLOYMENT)
        assert newest_legacy[0][0] < LIVE_TS
        # The live reading at the cut-off is left as it was.
        assert query(
            pg_url, f"SELECT source, {column} {mine} AND ts = %s", DEPLOYMENT, LIVE_TS
        ) == [("live", 99.5)]
        assert query(pg_url, f"SELECT count(*) {mine} AND ts >= %s", DEPLOYMENT, LIVE_TS) == [(2,)]
    assert _import(pg_url)["water"]["inserted"] == 0


def test_a_missing_file_is_skipped_and_reported(pg_url, tmp_path):
    alembic("upgrade", "head", url=pg_url)
    shutil.copy(FIXTURES / "water_quality.csv", tmp_path)
    out = _import(pg_url, f"file://{tmp_path}/")
    assert out["water"] == {"read": 5, "inserted": 5}
    assert out["weather"] == {"read": 0, "inserted": 0, "missing": f"file://{tmp_path}/weather.csv"}


def test_a_spent_budget_stops_after_the_batch_and_the_next_run_finishes(pg_url, monkeypatch):
    from app import legacy_import

    alembic("upgrade", "head", url=pg_url)
    monkeypatch.setattr(legacy_import, "BATCH", 2)
    # No time at all: the batch in hand is finished, then the run stops and says so.
    out = _import(pg_url, budget_s=0)
    assert out == {"water": {"read": 5, "inserted": 2}, "partial": True}
    assert query(pg_url, "SELECT count(*) FROM sensor_readings") == [(2,)]
    # Dispatched again, it carries on where it stopped.
    out = _import(pg_url)
    assert out == {
        "water": {"read": 5, "inserted": 3},
        "weather": {"read": 5, "inserted": 5},
        "partial": False,
    }
    assert query(pg_url, "SELECT count(*) FROM sensor_readings") == [(5,)]
    assert query(pg_url, "SELECT count(*) FROM weather_readings") == [(5,)]


def test_a_file_not_reached_is_left_out(pg_url):
    alembic("upgrade", "head", url=pg_url)
    # The water file in one batch spends the budget: the weather file is not read.
    assert _import(pg_url, budget_s=0) == {"water": {"read": 5, "inserted": 5}, "partial": True}
    assert query(pg_url, "SELECT count(*) FROM weather_readings") == [(0,)]
    assert _import(pg_url, budget_s=0) == {
        "water": {"read": 5, "inserted": 0},
        "weather": {"read": 5, "inserted": 5},
        "partial": False,
    }


def test_every_run_makes_progress_however_small_the_budget(pg_url, monkeypatch):
    from app import legacy_import

    alembic("upgrade", "head", url=pg_url)
    monkeypatch.setattr(legacy_import, "BATCH", 2)
    runs = [_import(pg_url, budget_s=0)]
    while runs[-1]["partial"]:
        assert len(runs) < 6, runs  # three batches a file: one batch a run at the least
        runs.append(_import(pg_url, budget_s=0))
    assert len(runs) == 6
    assert sum(r.get("water", {}).get("inserted", 0) for r in runs) == 5
    assert sum(r.get("weather", {}).get("inserted", 0) for r in runs) == 5
    assert query(pg_url, "SELECT count(*) FROM weather_readings WHERE source = 'legacy'") == [(5,)]


def test_a_file_with_another_header_is_refused(pg_url, tmp_path):
    alembic("upgrade", "head", url=pg_url)
    (tmp_path / "water_quality.csv").write_text("ts,temp\n2025-01-01T00:00:00Z,20\n")
    with pytest.raises(ValueError, match="water_quality.csv"):
        _import(pg_url, f"file://{tmp_path}/")


def test_a_bad_value_names_the_file_and_the_line(pg_url, tmp_path):
    alembic("upgrade", "head", url=pg_url)
    header = (FIXTURES / "water_quality.csv").read_text().splitlines()[0]
    (tmp_path / "water_quality.csv").write_text(f"{header}\n2025-01-01T00:00:00Z,1,2,3,4,5,six,7\n")
    with pytest.raises(ValueError, match=r"water_quality\.csv:2: "):
        _import(pg_url, f"file://{tmp_path}/")
    (tmp_path / "water_quality.csv").write_text(f"{header}\n\n2025-13-01,1,2,3,4,5,6,7\n")
    with pytest.raises(ValueError, match=r"water_quality\.csv:3: "):
        _import(pg_url, f"file://{tmp_path}/")


def test_import_legacy_is_a_job(pg_url, tmp_path, monkeypatch):
    from app.jobs import run_job

    alembic("upgrade", "head", url=pg_url)
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SENSESTREAM_DEPLOYMENT_URI", DEPLOYMENT)
    monkeypatch.setenv("LOCATION_ID", "reef_b")
    monkeypatch.delenv("LEGACY_URL", raising=False)
    monkeypatch.delenv("MODELS_URL", raising=False)
    # Neither LEGACY_URL nor MODELS_URL: nowhere to read from, said so.
    out = run_job("import_legacy")
    assert out["ok"] is False and "LEGACY_URL" in out["detail"]["error"]
    # LEGACY_URL defaults to <MODELS_URL>/legacy/.
    shutil.copytree(FIXTURES, tmp_path / "models" / "legacy")
    monkeypatch.setenv("MODELS_URL", f"file://{tmp_path}/models")
    out = run_job("import_legacy")
    assert out == {
        "job": "import_legacy",
        "ok": True,
        "detail": {
            "water": {"read": 5, "inserted": 5},
            "weather": {"read": 5, "inserted": 5},
            "partial": False,
        },
    }
    assert query(pg_url, "SELECT job, ok FROM job_runs ORDER BY id") == [
        ("import_legacy", False),
        ("import_legacy", True),
    ]


# ---- the export script's pure parts (no MySQL connection) -------------------------


def test_fahrenheit_becomes_celsius_to_two_places():
    from export_legacy_sensors import f_to_c

    assert f_to_c(32) == 0.0
    assert f_to_c(Decimal("82.40")) == 28.0
    assert f_to_c(Decimal("75.31")) == 24.06
    assert f_to_c(None) is None


def test_an_epoch_is_seconds_or_milliseconds_by_its_magnitude():
    from export_legacy_sensors import epoch_to_iso

    assert epoch_to_iso(1735689600) == "2025-01-01T00:00:00Z"
    assert epoch_to_iso(1735689600000) == "2025-01-01T00:00:00Z"
    assert epoch_to_iso(1735689600250) == "2025-01-01T00:00:00.250Z"


def test_the_weather_stations_readings_map_onto_the_header():
    from export_legacy_sensors import WEATHER_COLUMNS, map_weather

    assert ",".join(("ts", *WEATHER_COLUMNS)) == WEATHER_HEADER
    assert map_weather("sitedata", STATION) == {
        "bar_press_hpa": 1016.3,
        "wind_dir_avg_deg": 75.0,
        "wind_dir_min_deg": 40.0,
        "wind_dir_max_deg": 110.0,
        "wind_speed_avg_ms": 3.4,
        "wind_speed_min_ms": 1.2,
        "wind_speed_max_ms": 6.1,
        "air_temp_c": 22.5,
        "rel_humidity_pct": 68.0,
        "rain_accum_mm": 0.4,
    }
    # The placeholder the backend writes when a source had nothing ("{}"), a short array
    # and a non-number (Jackson writes NaN as "NaN") are empty fields, not errors.
    assert set(map_weather("sitedata", {}).values()) == {None}
    assert set(map_weather("sitedata", {"readings": [1012.0]}).values()) == {None, 1012.0}
    nan = {**STATION, "readings": ["NaN", *STATION["readings"][1:]]}
    assert map_weather("sitedata", nan)["bar_press_hpa"] is None
    # Tomorrow.io's forecast layer is modelled, not measured: it fills none of the
    # station's columns (the live poller keeps it out of them too).
    assert set(map_weather("weatherdata", FORECAST).values()) == {None}


def test_inspect_shows_keys_never_values():
    from export_legacy_sensors import blob_keys

    keys = blob_keys([STATION, {}, {"time": 1, "readings": [1.0, 2.0]}])
    assert keys == ["readings[0..1]", "readings[0..23]", "time"]
    assert not any("1016" in k for k in keys)


def test_the_export_writes_one_row_per_ts_ascending(tmp_path):
    from export_legacy_sensors import (
        WATER_COLUMNS,
        WEATHER_COLUMNS,
        water_rows,
        weather_rows,
        write_csv,
    )

    t0 = 1735689600000
    D = Decimal
    water = water_rows(
        [
            # (time, water_temperature_f, ph, do_percent, chlorophyll_rfu, phycoerythrin_rfu,
            #  turbidity_fnu, spcond_mscm), as the water_quality table has them
            (t0 + 900_000, D("75.20"), D("8.02"), None, None, None, None, None),
            (t0, D("82.40"), D("8.05"), D("96.40"), D("1.21"), None, None, D("52.100")),
            (t0, D("90.00"), D("7.00"), None, None, None, None, None),  # same ts: the first wins
        ]
    )
    assert write_csv(tmp_path / "water_quality.csv", WATER_COLUMNS, water) == (
        2,
        "2025-01-01T00:00:00Z",
        "2025-01-01T00:15:00Z",
    )
    assert (tmp_path / "water_quality.csv").read_text().splitlines() == [
        WATER_HEADER,
        "2025-01-01T00:00:00Z,28.0,8.05,96.40,1.21,,,52.100",
        "2025-01-01T00:15:00Z,24.0,8.02,,,,,",
    ]
    weather = weather_rows(
        {
            "sitedata": [
                (t0 + 600_000, json.dumps(STATION)),
                (t0, json.dumps(STATION)),
                (t0, json.dumps({**STATION, "readings": [999.0] * 24})),  # same ts: the first wins
                (t0 + 1_200_000, "{}"),  # nothing mapped: no row
                (t0 + 1_800_000, "not json"),  # unreadable: no row
            ],
            "weatherdata": [(t0 + 300_000, json.dumps(FORECAST))],
        }
    )
    assert write_csv(tmp_path / "weather.csv", WEATHER_COLUMNS, weather) == (
        2,
        "2025-01-01T00:00:00Z",
        "2025-01-01T00:10:00Z",
    )
    lines = (tmp_path / "weather.csv").read_text().splitlines()
    assert lines[0] == WEATHER_HEADER
    assert lines[1] == "2025-01-01T00:00:00Z,1016.3,75.0,40.0,110.0,3.4,1.2,6.1,22.5,68.0,0.4"
    assert [line.split(",")[0] for line in lines[1:]] == [
        "2025-01-01T00:00:00Z",
        "2025-01-01T00:10:00Z",
    ]


def test_what_the_export_writes_the_import_reads(pg_url, tmp_path):
    from export_legacy_sensors import (
        WATER_COLUMNS,
        WEATHER_COLUMNS,
        water_rows,
        weather_rows,
        write_csv,
    )

    alembic("upgrade", "head", url=pg_url)
    t0 = 1735689600000
    write_csv(
        tmp_path / "water_quality.csv",
        WATER_COLUMNS,
        water_rows([(t0, Decimal("82.40"), Decimal("8.05"), None, None, None, None, None)]),
    )
    write_csv(
        tmp_path / "weather.csv",
        WEATHER_COLUMNS,
        weather_rows({"sitedata": [(t0, json.dumps(STATION))]}),
    )
    assert _import(pg_url, f"file://{tmp_path}/") == {
        "water": {"read": 1, "inserted": 1},
        "weather": {"read": 1, "inserted": 1},
        "partial": False,
    }
    assert query(pg_url, "SELECT ts, water_temp_c, ph, do_pct FROM sensor_readings") == [
        (datetime(2025, 1, 1, tzinfo=UTC), 28.0, pytest.approx(8.05, abs=1e-4), None)
    ]
    assert query(pg_url, "SELECT bar_press_hpa, rain_accum_mm FROM weather_readings") == [
        (pytest.approx(1016.3, abs=1e-3), pytest.approx(0.4, abs=1e-4))
    ]


def test_the_mysql_url_is_read_without_echoing_it():
    from export_legacy_sensors import mysql_params

    assert mysql_params("mysql://reader:p%40ss@db.example:3307/legacy") == {
        "host": "db.example",
        "port": 3307,
        "user": "reader",
        "password": "p@ss",
        "database": "legacy",
    }
    assert mysql_params("mysql://reader:x@db.example/legacy")["port"] == 3306
    # A password with an unescaped '#' cuts the URL short; the complaint must not quote it.
    with pytest.raises(SystemExit) as refused:
        mysql_params("mysql://reader:s3cr#et@db.example/legacy")
    assert "s3cr" not in str(refused.value) and "LEGACY_MYSQL_URL" in str(refused.value)
    # A raw password 'ab@cd/ef' would read as host 'cd' and database 'ef@db.example/legacy',
    # and the driver's "unknown host 'cd'" would then print a piece of it: refused here.
    with pytest.raises(SystemExit) as refused:
        mysql_params("mysql://reader:ab@cd/ef@db.example/legacy")
    message = str(refused.value)
    assert "LEGACY_MYSQL_URL" in message
    assert not any(part in message for part in ("reader", "cd", "ef@", "db.example"))


def test_a_driver_error_is_reported_without_the_url(monkeypatch):
    """The driver's own messages name the host (refused) or the user (access denied)."""
    from export_legacy_sensors import connect

    class Error(Exception):
        def __init__(self, msg: str, errno: int) -> None:
            super().__init__(msg)
            self.msg, self.errno = msg, errno

    def refuse(**params):
        raise Error(f"Unknown MySQL server host '{params['host']}' as {params['user']}", 2005)

    connector = types.ModuleType("mysql.connector")
    connector.Error, connector.connect = Error, refuse
    mysql = types.ModuleType("mysql")
    mysql.connector = connector
    monkeypatch.setitem(sys.modules, "mysql", mysql)
    monkeypatch.setitem(sys.modules, "mysql.connector", connector)
    with pytest.raises(SystemExit) as refused:
        connect("mysql://reader:pw@db.example/legacy")
    assert str(refused.value) == "could not connect to the Java database (MySQL error 2005)"
    assert refused.value.__cause__ is None and refused.value.__suppress_context__


def test_the_export_wants_the_url_from_the_environment(monkeypatch, tmp_path):
    from export_legacy_sensors import main

    monkeypatch.delenv("LEGACY_MYSQL_URL", raising=False)
    with pytest.raises(SystemExit) as refused:
        main(["--out", str(tmp_path)])
    assert "LEGACY_MYSQL_URL" in str(refused.value)


class _FakeMySQL:
    """A connection whose cursors answer by SQL (the first matching key) and record each
    (sql, params): the export's queries without a MySQL server."""

    def __init__(self, answers: list[tuple[str, list[tuple]]]) -> None:
        self.answers, self.queries = answers, []

    def cursor(self):
        conn = self

        class Cursor:
            def execute(self, sql, params=()):
                conn.queries.append((sql, tuple(params)))
                self.rows = next(rows for key, rows in conn.answers if key in sql)

            def __iter__(self):
                return iter(self.rows)

            def close(self):
                pass

        return Cursor()

    def close(self):
        pass


def _main_with(monkeypatch, conn, *argv):
    import export_legacy_sensors

    monkeypatch.setenv("LEGACY_MYSQL_URL", "mysql://reader:pw@db.example/legacy")
    monkeypatch.setattr(export_legacy_sensors, "connect", lambda url: conn)
    return export_legacy_sensors.main(list(argv))


def test_inspect_counts_the_water_rows_per_datasource(monkeypatch, capsys):
    conn = _FakeMySQL(
        [
            ("GROUP BY", [(3, 70123), (4, 12)]),
            ("FROM sitedata", [(json.dumps(STATION),), ("{}",)]),
            ("FROM weatherdata", [(json.dumps(FORECAST),)]),
        ]
    )
    assert _main_with(monkeypatch, conn, "--inspect") == 0
    printed = capsys.readouterr().out.splitlines()
    assert printed == [
        "water_quality, rows per datasource_id: 3=70123, 4=12",
        "sitedata (datasource 1), newest 2 rows: readings[0..23], time",
        "weatherdata, newest 1 rows: readings[0..12], time",
    ]
    assert ("FROM sitedata" in conn.queries[1][0]) and conn.queries[1][1] == (1,)


def test_the_export_reads_the_sondes_datasource_and_the_stations(monkeypatch, capsys, tmp_path):
    t0 = 1735689600000
    water = (t0, Decimal("82.40"), Decimal("8.05"), None, None, None, None, None)
    conn = _FakeMySQL(
        [("FROM water_quality", [water]), ("FROM sitedata", [(t0, json.dumps(STATION))])]
    )
    assert _main_with(monkeypatch, conn, "--out", str(tmp_path)) == 0
    (water_sql, water_params), (site_sql, site_params) = conn.queries
    assert "WHERE datasource_id = %s" in water_sql and water_params == (3,)
    assert "FROM sitedata" in site_sql and site_params == (1,)  # measured only: no weatherdata
    assert capsys.readouterr().out.splitlines() == [
        "water_quality.csv: 1 rows, 2025-01-01T00:00:00Z .. 2025-01-01T00:00:00Z",
        "weather.csv: 1 rows, 2025-01-01T00:00:00Z .. 2025-01-01T00:00:00Z",
    ]
    conn = _FakeMySQL([("FROM water_quality", []), ("FROM sitedata", [])])
    _main_with(
        monkeypatch,
        conn,
        "--out",
        str(tmp_path),
        "--water-datasource",
        "7",
        "--weather-datasource",
        "5",
    )
    assert [params for _, params in conn.queries] == [(7,), (5,)]

"""Alembic owns the schema (Plan 2a, Task 3).

A fresh database comes up from nothing with `alembic upgrade head`, upgrades
are idempotent, and a database built by the old db/init.sql (the DGX) joins
in by stamping the baseline and upgrading from there.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import uuid
from pathlib import Path

import psycopg
import pytest

REPO = Path(__file__).resolve().parents[1]
ALEMBIC = [sys.executable, "-m", "alembic", "-c", str(REPO / "db" / "alembic.ini")]
BASELINE_SQL = REPO / "db" / "migrations" / "versions" / "0001_baseline.sql"
PG_BIN_DIRS = (
    os.environ.get("PG_BIN", ""),
    "/opt/homebrew/opt/postgresql@16/bin",
    "/usr/lib/postgresql/16/bin",
)
DATA_TABLES = {
    "detection_events",
    "frame_stats",
    "saved_frames",
    "sensor_readings",
    "weather_readings",
    "ptz_states",
    "detection_corrections",
    "review_items",
}


def _pg_bin(name: str) -> str | None:
    for d in PG_BIN_DIRS:
        if d and (Path(d) / name).exists():
            return str(Path(d) / name)
    return shutil.which(name)


@pytest.fixture(scope="session")
def pg_server(tmp_path_factory):
    """A server URL with CREATE DATABASE rights: TEST_DATABASE_URL's server, or an
    ephemeral cluster started from the PostgreSQL binaries on this machine."""
    dsn = os.environ.get("TEST_DATABASE_URL")
    if dsn:
        yield dsn
        return
    pg_ctl, initdb = _pg_bin("pg_ctl"), _pg_bin("initdb")
    if not (pg_ctl and initdb):
        pytest.skip("no TEST_DATABASE_URL and no PostgreSQL binaries (set PG_BIN)")
    data = tmp_path_factory.mktemp("pg") / "data"
    # macOS: the postmaster refuses to start under an unset or invalid locale
    # ("postmaster became multithreaded during startup"); C is always valid.
    env = {**os.environ, "LC_ALL": "C", "LANG": "C"}
    subprocess.run(
        [initdb, "-D", str(data), "-U", "postgres", "--auth=trust", "-E", "UTF8", "--locale=C"],
        check=True,
        capture_output=True,
        env=env,
    )
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    options = f"-p {port} -h 127.0.0.1 -c unix_socket_directories="
    subprocess.run(
        [
            pg_ctl,
            "-D",
            str(data),
            "-o",
            options,
            "-l",
            str(data.parent / "server.log"),
            "-w",
            "start",
        ],
        check=True,
        capture_output=True,
        env=env,
    )
    try:
        yield f"postgresql://postgres@127.0.0.1:{port}/postgres"
    finally:
        subprocess.run(
            [pg_ctl, "-D", str(data), "-m", "fast", "stop"], capture_output=True, env=env
        )


@pytest.fixture
def pg_url(pg_server):
    """An empty database of its own for each test, dropped afterwards."""
    name = f"mig_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(pg_server, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    base = pg_server.rpartition("/")[0]
    try:
        yield f"{base}/{name}"
    finally:
        with psycopg.connect(pg_server, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


def alembic(*args: str, url: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [*ALEMBIC, *args],
        cwd=REPO,
        env={**os.environ, "DATABASE_URL": url},
        capture_output=True,
        text=True,
        check=True,
    )


def query(url: str, sql: str, *params) -> list[tuple]:
    with psycopg.connect(url) as conn:
        return conn.execute(sql, params).fetchall()


def tables(url: str) -> set[str]:
    return {r[0] for r in query(url, "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")}


def columns(url: str, table: str) -> set[str]:
    rows = query(
        url, "SELECT column_name FROM information_schema.columns WHERE table_name = %s", table
    )
    return {r[0] for r in rows}


def column_default(url: str, table: str, column: str) -> str | None:
    rows = query(
        url,
        "SELECT column_default FROM information_schema.columns WHERE table_name = %s AND column_name = %s",
        table,
        column,
    )
    return rows[0][0] if rows else None


def test_upgrade_head_on_empty_database_creates_all_tables(pg_url):
    alembic("upgrade", "head", url=pg_url)
    assert tables(pg_url) >= DATA_TABLES | {"users", "alerts", "alembic_version"}
    matviews = {r[0] for r in query(pg_url, "SELECT matviewname FROM pg_matviews")}
    assert matviews == {"species_sightings_mat", "species_sightings_hourly_mat"}
    # init.sql altered saved_frames before creating it, so a fresh database
    # lacked these three columns; the baseline creates them.
    assert {"model_version", "config_hash", "pipeline_git_sha"} <= columns(pg_url, "saved_frames")


def test_upgrade_is_idempotent_and_adds_location_id(pg_url):
    alembic("upgrade", "head", url=pg_url)
    alembic("upgrade", "head", url=pg_url)
    for table in DATA_TABLES:
        assert column_default(pg_url, table, "location_id") == "'wahoobay'::text", table
    indexes = {
        r[0] for r in query(pg_url, "SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")
    }
    assert {
        "detection_events_location_ts",
        "sensor_readings_location_ts",
        "review_items_location_ts",
    } <= indexes
    assert "detection_corrections_location_ts" not in indexes  # no ts column there


def test_database_built_by_init_sql_joins_by_stamping_the_baseline(pg_url):
    # The DGX path: the schema is already there (built by db/init.sql over the
    # years); Alembic is told so, then applies only what is newer.
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute(BASELINE_SQL.read_text())
    assert "location_id" not in columns(pg_url, "sensor_readings")
    alembic("stamp", "0001", url=pg_url)
    alembic("upgrade", "head", url=pg_url)
    assert "location_id" in columns(pg_url, "sensor_readings")
    assert "0002" in alembic("current", url=pg_url).stdout


def test_location_id_downgrade_is_reversible(pg_url):
    alembic("upgrade", "head", url=pg_url)
    alembic("downgrade", "0001", url=pg_url)
    assert "location_id" not in columns(pg_url, "detection_events")


def test_migrate_entry_point_brings_the_database_to_head(pg_url, monkeypatch):
    from app import migrate

    monkeypatch.setenv("DATABASE_URL", pg_url)
    assert migrate.handler({"job": "migrate"}, None) == {"revision": "0002"}
    assert "review_items" in tables(pg_url)
    assert migrate.main([]) == 0  # the CLI: python -m app.migrate

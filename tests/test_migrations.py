"""Alembic owns the schema (Plan 2a, Task 3).

A fresh database comes up from nothing with `alembic upgrade head`, upgrades
are idempotent, and a database built by the old db/init.sql (the DGX) joins
in by stamping the baseline and upgrading from there. The `pg_url` fixture
(conftest.py) hands each test an empty database of its own.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import psycopg
import pytest

REPO = Path(__file__).resolve().parents[1]
ALEMBIC = [sys.executable, "-m", "alembic", "-c", str(REPO / "db" / "alembic.ini")]
BASELINE_SQL = REPO / "db" / "migrations" / "versions" / "0001_baseline.sql"
HEAD = "0008"
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
    assert tables(pg_url) >= DATA_TABLES | {
        "users",
        "alerts",
        "alembic_version",
        "job_runs",
        "poller_status",
    }
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
    assert HEAD in alembic("current", url=pg_url).stdout


def test_location_id_downgrade_is_reversible(pg_url):
    alembic("upgrade", "head", url=pg_url)
    alembic("downgrade", "0001", url=pg_url)
    assert "location_id" not in columns(pg_url, "detection_events")


def test_migrate_entry_point_brings_the_database_to_head(pg_url, monkeypatch):
    from app import migrate

    monkeypatch.setenv("DATABASE_URL", pg_url)
    assert migrate.handler({"job": "migrate"}, None) == {"revision": HEAD}
    assert "review_items" in tables(pg_url)
    assert migrate.main([]) == 0  # the CLI: python -m app.migrate


def test_0004_drops_the_shared_review_window(pg_url):
    """The review date window became each reviewer's own (per session, nothing stored); the
    setting the old shared window left behind goes, and the items it retired come back (the
    reopen statement is exercised against real rows in tests/test_review_window.py)."""
    alembic("upgrade", "0003", url=pg_url)
    with psycopg.connect(pg_url) as conn:
        conn.execute(
            "INSERT INTO review_settings (key, value) VALUES ('window', '{\"start\": \"2026-05-17T00:00:00-04:00\"}'::jsonb)"
        )
    alembic("upgrade", "head", url=pg_url)
    assert query(pg_url, "SELECT count(*) FROM review_settings WHERE key = 'window'") == [(0,)]
    assert HEAD in alembic("current", url=pg_url).stdout


def test_label_source_accepts_bulk_at_head(pg_url):
    """0005: answers marked with the rest in whole-frame review carry label_source 'bulk';
    anything else outside the list is still refused."""
    alembic("upgrade", "head", url=pg_url)
    with psycopg.connect(pg_url) as conn:
        (eid,) = conn.execute(
            "INSERT INTO detection_events (ts, frame_id, source_name, det_conf, bbox, topk) "
            "VALUES (now(), 1, 'cam', 0.9, ARRAY[1,2,3,4], '[]'::jsonb) RETURNING id"
        ).fetchone()
        conn.execute(
            "INSERT INTO detection_corrections (event_id, corrected_name, label_source) "
            "VALUES (%s, 'Seriola zonata', 'bulk')",
            (eid,),
        )
    with psycopg.connect(pg_url) as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO detection_corrections (event_id, corrected_name, label_source) "
            "VALUES (%s, 'Seriola zonata', 'other')",
            (eid,),
        )


def test_species_suggestions_at_head(pg_url):
    """0006: a reviewer's suggested species lives in species_suggestions; an answer that uses one
    carries label_source 'suggested' and a link the suggestion cannot lose."""
    alembic("upgrade", "head", url=pg_url)
    with psycopg.connect(pg_url) as conn:
        (uid,) = conn.execute(
            "INSERT INTO users (username, display_name, email, password_hash) "
            "VALUES ('r', 'R', 'r@example.org', 'x') RETURNING id"
        ).fetchone()
        sid, status, location = conn.execute(
            "INSERT INTO species_suggestions (common_name, suggested_by) VALUES ('Blue tang', %s) "
            "RETURNING id, status, location_id",
            (uid,),
        ).fetchone()
        assert (status, location) == ("pending", "wahoobay")
        (eid,) = conn.execute(
            "INSERT INTO detection_events (ts, frame_id, source_name, det_conf, bbox, topk) "
            "VALUES (now(), 1, 'cam', 0.9, ARRAY[1,2,3,4], '[]'::jsonb) RETURNING id"
        ).fetchone()
        conn.execute(
            "INSERT INTO detection_corrections "
            "(event_id, corrected_name, label_source, species_suggestion_id) "
            "VALUES (%s, 'Blue tang', 'suggested', %s)",
            (eid, sid),
        )
    with psycopg.connect(pg_url) as conn, pytest.raises(psycopg.errors.ForeignKeyViolation):
        conn.execute("DELETE FROM species_suggestions WHERE id = %s", (sid,))
    with psycopg.connect(pg_url) as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO species_suggestions (common_name, suggested_by, status) "
            "VALUES ('Blue tang', %s, 'other')",
            (uid,),
        )


def test_suggestion_worms_columns_at_head(pg_url):
    """0007: a reviewer's addition records the WoRMS record chosen; the downgrade drops it."""
    alembic("upgrade", "head", url=pg_url)
    with psycopg.connect(pg_url) as conn:
        (uid,) = conn.execute(
            "INSERT INTO users (username, display_name, email, password_hash) "
            "VALUES ('r', 'R', 'r@example.org', 'x') RETURNING id"
        ).fetchone()
        row = conn.execute(
            "INSERT INTO species_suggestions (common_name, suggested_by, aphia_id, family) "
            "VALUES ('Spotted Goatfish', %s, 159855, 'Mullidae') RETURNING aphia_id, family",
            (uid,),
        ).fetchone()
    assert row == (159855, "Mullidae")
    alembic("downgrade", "0006", url=pg_url)
    assert not {"aphia_id", "family"} & columns(pg_url, "species_suggestions")


GRUNT, GRUNT_NEW = "Haemulon chrysargyreum", "Brachygenys chrysargyrea"
CHUB, CHUB_NEW = "Kyphosus sectatrixbigibbus", "Kyphosus"


def _guess(name: str, p: float) -> dict:
    return {"name": name, "species_id": name, "accuracy": p}


def _event(url: str, best: str, topk: list[dict], *, track_id=None, ts="now()") -> int:
    with psycopg.connect(url) as conn:
        (eid,) = conn.execute(
            "INSERT INTO detection_events (ts, frame_id, source_name, det_conf, bbox, topk, "
            f"best_name, best_species_id, best_accuracy, track_id) VALUES ({ts}, 1, 'cam', 0.9, "
            "ARRAY[1,2,3,4], %s::jsonb, %s, %s, %s, %s) RETURNING id",
            (json.dumps(topk), best, best, topk[0]["accuracy"], track_id),
        ).fetchone()
    return eid


def _read(url: str, eid: int) -> tuple:
    return query(
        url, "SELECT best_name, best_species_id, topk FROM detection_events WHERE id = %s", eid
    )[0]


def _seed_best_guesses(url: str) -> dict[int, list[dict]]:
    seeded = {}
    for old in (GRUNT, CHUB):
        topk = [_guess(old, 0.8), _guess("Scarus iseri", 0.1)]
        seeded[_event(url, old, topk)] = topk
    return seeded


def test_0008_renames_best_guess_and_topk(pg_url):
    """Option 3 (Dan, 2026-11-22): past detections take the reviewers' names for the two classes the
    model still spelled the Species Log's old way; the other guesses in the list stay as they were."""
    alembic("upgrade", "0007", url=pg_url)
    seeded = _seed_best_guesses(url=pg_url)
    alembic("upgrade", "head", url=pg_url)
    for (eid, topk), new in zip(seeded.items(), (GRUNT_NEW, CHUB_NEW), strict=True):
        assert _read(pg_url, eid) == (new, new, [_guess(new, 0.8), topk[1]])


def test_0008_renames_topk_entries_below_the_top(pg_url):
    alembic("upgrade", "0007", url=pg_url)
    topk = [_guess("Scarus iseri", 0.5), _guess("Haemulon flavolineatum", 0.2), _guess(GRUNT, 0.1)]
    eid = _event(pg_url, "Scarus iseri", topk)
    alembic("upgrade", "head", url=pg_url)
    assert _read(pg_url, eid) == (
        "Scarus iseri",
        "Scarus iseri",
        [*topk[:2], _guess(GRUNT_NEW, 0.1)],
    )


def test_0008_refreshes_the_sightings_views(pg_url):
    """The species list and trends read the materialized views; after the rename they hold the new
    id only, so the fish is one row, not two."""
    alembic("upgrade", "0007", url=pg_url)
    for i in range(3):
        _event(
            pg_url, GRUNT, [_guess(GRUNT, 0.9)], track_id=7, ts=f"now() - interval '{i} minutes'"
        )
    with psycopg.connect(pg_url) as conn:
        conn.execute("REFRESH MATERIALIZED VIEW species_sightings_mat")
        conn.execute("REFRESH MATERIALIZED VIEW species_sightings_hourly_mat")
    alembic("upgrade", "head", url=pg_url)
    assert query(pg_url, "SELECT species_id, name FROM species_sightings_mat") == [
        (GRUNT_NEW, GRUNT_NEW)
    ]
    hourly = query(pg_url, "SELECT DISTINCT species_id, name FROM species_sightings_hourly_mat")
    assert hourly == [(GRUNT_NEW, GRUNT_NEW)]


def test_0008_downgrade_restores_the_old_names(pg_url):
    alembic("upgrade", "0007", url=pg_url)
    seeded = _seed_best_guesses(url=pg_url)
    alembic("upgrade", "head", url=pg_url)
    assert [_read(pg_url, eid)[0] for eid in seeded] == [GRUNT_NEW, CHUB_NEW]
    alembic("downgrade", "0007", url=pg_url)
    for (eid, topk), old in zip(seeded.items(), (GRUNT, CHUB), strict=True):
        assert _read(pg_url, eid) == (old, old, topk)


def test_0008_renames_are_the_live_table():
    """The migration keeps its own frozen copy (a migration never imports code that may change
    later); every pair in it is still in the table the rest of the system reads."""
    sys.path.insert(0, str(REPO / "services"))
    from common.class_renames import CLASS_RENAMES

    path = REPO / "db" / "migrations" / "versions" / "0008_class_renames.py"
    spec = importlib.util.spec_from_file_location("migration_0008", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.RENAMES == {GRUNT: GRUNT_NEW, CHUB: CHUB_NEW}
    assert module.RENAMES.items() <= CLASS_RENAMES.items()

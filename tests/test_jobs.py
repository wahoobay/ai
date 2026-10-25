"""Periodic work runs as jobs from an event (Plan 2a, Task 4).

`app.lambda_entry.handler({"job": ...})` refreshes the materialized views,
evaluates the alert rules, or migrates; every run is recorded in job_runs. The
alert rules read the poller's status from a table and skip the worker rules
when there is no worker.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import psycopg
from test_migrations import alembic, query

REPO = Path(__file__).resolve().parents[1]
WORKER_RULES = {
    "pipeline_silence",
    "frame_stats_stalled",
    "inference_latency_p95",
    "drift_luma_delta",
    "drift_fish_rate_crash",
}


def _site(monkeypatch, pg_url, worker_url=""):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("WORKER_URL", worker_url)


def test_jobs_run_from_an_event(pg_url, monkeypatch):
    from app.lambda_entry import handler

    _site(monkeypatch, pg_url)
    assert handler({"job": "migrate"}, None) == {
        "job": "migrate",
        "ok": True,
        "detail": {"revision": "0003"},
    }
    out = handler({"job": "refresh_matviews"}, None)
    assert out["ok"] is True
    assert set(out["detail"]) == {"species_sightings_mat", "species_sightings_hourly_mat"}
    out = handler({"job": "alert_rules"}, None)
    assert out["ok"] is True and out["job"] == "alert_rules"
    runs = query(pg_url, "SELECT job, ok FROM job_runs ORDER BY ran_at, id")
    assert runs == [("migrate", True), ("refresh_matviews", True), ("alert_rules", True)]


def test_an_unknown_job_is_refused_without_touching_the_database(pg_url, monkeypatch):
    from app.lambda_entry import handler

    _site(monkeypatch, pg_url)
    out = handler({"job": "make_coffee"}, None)
    assert out["ok"] is False and "make_coffee" in out["detail"]["error"]
    assert "job_runs" not in {r[0] for r in query(pg_url, "SELECT tablename FROM pg_tables")}


def test_worker_rules_are_skipped_without_a_worker(pg_url, monkeypatch):
    from app.jobs import run_job

    alembic("upgrade", "head", url=pg_url)
    _site(monkeypatch, pg_url, worker_url="")
    assert set(run_job("alert_rules")["detail"]) == {"poller_probe_stale"}
    _site(monkeypatch, pg_url, worker_url="http://127.0.0.1:1")
    assert WORKER_RULES <= set(run_job("alert_rules")["detail"])


def test_the_poller_status_table_feeds_the_stale_rule(pg_url, monkeypatch):
    from app.jobs import run_job

    alembic("upgrade", "head", url=pg_url)
    _site(monkeypatch, pg_url)
    # Nothing reported yet: not an alert (a fresh site has no poller history).
    assert run_job("alert_rules")["detail"]["poller_probe_stale"] is False
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO poller_status (name, last_ok, last_error) "
            "VALUES ('sensestream', NOW() - INTERVAL '2 hours', '')"
        )
    assert run_job("alert_rules")["detail"]["poller_probe_stale"] is True
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute("UPDATE poller_status SET last_ok = NOW() WHERE name = 'sensestream'")
    assert run_job("alert_rules")["detail"]["poller_probe_stale"] is False
    firing = query(pg_url, "SELECT name FROM alerts WHERE resolved_at IS NULL")
    assert firing == []


def test_the_cli_runs_a_job(pg_url):
    alembic("upgrade", "head", url=pg_url)
    r = subprocess.run(
        [sys.executable, "-m", "app.jobs", "refresh_matviews"],
        cwd=REPO / "services" / "dashboard",
        env={**os.environ, "DATABASE_URL": pg_url, "WORKER_URL": ""},
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout.splitlines()[-1])["ok"] is True

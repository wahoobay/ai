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
        "detail": {"revision": "0004"},
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


def test_the_lambda_prints_its_result_for_the_log_group(capsys):
    """An asynchronous invocation (run-job-staging) has no caller to return the result to,
    and the runtime keeps the root logger at WARNING: the result goes to stdout, which
    Lambda keeps in the function's log group whatever the logging level."""
    from app.lambda_entry import handler

    out = handler({"job": "make_coffee"}, None)
    printed = capsys.readouterr().out.strip().splitlines()
    assert printed and json.loads(printed[-1]) == out


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


def test_status_reports_the_last_runs_and_the_pollers(pg_url, monkeypatch):
    # Plan 2b: `{"job": "status"}` is how the platform's verify script reads the
    # inside of an environment without a database connection of its own.
    from app.lambda_entry import handler

    _site(monkeypatch, pg_url)
    assert handler({"job": "migrate"}, None)["ok"] is True
    handler({"job": "refresh_matviews"}, None)
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO poller_status (name, last_ok, last_error) VALUES ('sensestream', NOW(), '')"
        )
    out = handler({"job": "status"}, None)
    assert out["ok"] is True
    detail = out["detail"]
    assert detail["revision"] == "0004"
    assert [r["job"] for r in detail["job_runs"]][:2] == ["refresh_matviews", "migrate"]
    assert detail["pollers"][0]["name"] == "sensestream" and detail["pollers"][0]["last_ok"]
    assert detail["counts"]["detection_events"] == 0 and "species_sightings_mat" in detail["counts"]
    # A status report is a read: it is not itself recorded as a run.
    assert [r["job"] for r in detail["job_runs"]].count("status") == 0
    assert query(pg_url, "SELECT count(*) FROM job_runs WHERE job = 'status'") == [(0,)]

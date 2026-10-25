"""Shared pytest fixtures for the whole test suite.

Autouse network isolation lives here, not in an individual test file's
fixtures, specifically so isolation cannot be skipped by omission. See
the fixture docstring below for what that protects against and why an
autouse fixture — rather than something each test file has to remember
to apply — is the fix."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# The test database DSN can be handed over in a file rather than the
# environment, so it never appears on a command line or needs shell
# expansion: TEST_DATABASE_URL_FILE=.superpowers/test_dsn pytest ...
# Test modules read TEST_DATABASE_URL when they are imported, which is
# after this conftest runs, so setting it here is early enough.
_dsn_file = os.environ.get("TEST_DATABASE_URL_FILE")
if _dsn_file and not os.environ.get("TEST_DATABASE_URL"):
    os.environ["TEST_DATABASE_URL"] = Path(_dsn_file).read_text().strip()

if os.environ.get("TEST_DATABASE_URL"):
    from psycopg.conninfo import conninfo_to_dict

    if conninfo_to_dict(os.environ["TEST_DATABASE_URL"]).get("dbname") == "wahoobay":
        # These tests TRUNCATE tables. The production database is named
        # 'wahoobay'. Refuse outright instead of trusting every caller.
        raise pytest.UsageError("TEST_DATABASE_URL points at the production database 'wahoobay'")

from review_fixtures import rclient, rdb, review_assets  # noqa: E402,F401

MIGRATION = REPO_ROOT / "db" / "migrations" / "2026-09-24-review-tool.sql"


@pytest.fixture(scope="session", autouse=True)
def _review_schema():
    """Bring the test database up to the review-tool schema once per run.
    The migration is idempotent, so after the first run this is a no-op."""
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        return
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(MIGRATION.read_text())


@pytest.fixture(autouse=True)
def _isolate_network(monkeypatch):
    """build_app()'s lifespan starts background tasks that reach the
    network: a Google Sheets poller, and an SLOChecker that polls
    worker_url/poller_url every 30s (and once immediately on startup).

    On this shared machine, WORKER_URL/POLLER_URL default to
    localhost:8081/8082 — the REAL production worker and poller — and
    SHEET_URL defaults to the production Google Sheet. Any test that
    builds the app without overriding these would issue live requests
    against production services and the production sheet: slow, rude to
    production, and (per an earlier incident) able to overwrite
    regenerated catalogue files under assets/.

    Autouse, applied via monkeypatch (auto-reverted at test teardown) so
    every test in this suite gets it regardless of which fixtures it
    requests — a test file that builds the app through a fixture that
    forgets to isolate the network no longer silently depends on a
    sibling test having left the isolation in place process-globally."""
    monkeypatch.setenv("SHEET_URL", "")
    monkeypatch.setenv("WORKER_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("POLLER_URL", "http://127.0.0.1:1")

    # Mail is the same hazard wearing different clothes. Once a real relay
    # is configured in .env, a test run from a shell that sourced it would
    # build the app with the smtp backend and send genuine mail through the
    # project's account -- to whatever addresses the tests happen to use,
    # against a metered quota, from a domain whose sending reputation is
    # shared with the organisation. Pin the backend to `manual` and blank
    # the relay so no test can reach a mail server by accident.
    monkeypatch.setenv("DASHBOARD_MAIL_BACKEND", "manual")
    monkeypatch.setenv("DASHBOARD_SMTP_HOST", "")
    monkeypatch.setenv("DASHBOARD_SMTP_PASSWORD", "")

    # The review pool's background task would fill review_items behind a
    # test's back. Tests that need a pool run call ReviewPool.run_once().
    monkeypatch.setenv("REVIEW_POOL_ENABLED", "false")

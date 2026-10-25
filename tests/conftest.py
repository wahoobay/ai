"""Shared pytest fixtures for the whole test suite.

Autouse network isolation lives here, not in an individual test file's
fixtures, specifically so isolation cannot be skipped by omission. See
the fixture docstring below for what that protects against and why an
autouse fixture — rather than something each test file has to remember
to apply — is the fix."""

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

REPO_ROOT = Path(__file__).resolve().parents[1]
# The dashboard is imported as `app` (review_fixtures puts services/dashboard on
# the path); the poller and the worker, whose packages are also called `app`,
# are imported through the repository root as services.<service>.app.
sys.path.insert(0, str(REPO_ROOT))

# The services refuse to start without a site and a database address (the code
# has no default site). The suite is not a site, so it names one here; tests
# that need a real database still set DATABASE_URL to TEST_DATABASE_URL.
os.environ.setdefault("SENSESTREAM_DEPLOYMENT_URI", "test_site")
os.environ.setdefault("DATABASE_URL", "postgresql://wahoobay:wahoobay@127.0.0.1:5432/wahoobay")

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

ALEMBIC_INI = REPO_ROOT / "db" / "alembic.ini"


@pytest.fixture(scope="session", autouse=True)
def _schema_at_head():
    """Bring the test database to the current schema (Alembic head) once per run;
    an empty database is built from nothing, an existing one only gains what is new."""
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        return
    subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(ALEMBIC_INI), "upgrade", "head"],
        check=True,
        cwd=REPO_ROOT,
        env={**os.environ, "DATABASE_URL": dsn},
        capture_output=True,
    )


@pytest.fixture(autouse=True)
def _isolate_network(monkeypatch):
    """build_app()'s lifespan starts background tasks that reach the
    network: a Google Sheets poller; and the worker proxy routes dial
    WORKER_URL.

    On this shared machine, WORKER_URL defaults to
    localhost:8081 — the REAL production worker — and
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


# ---- an empty PostgreSQL database per test (migrations, jobs) -------------------

PG_BIN_DIRS = (
    os.environ.get("PG_BIN", ""),
    "/opt/homebrew/opt/postgresql@16/bin",
    "/usr/lib/postgresql/16/bin",
)


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

"""The dashboard holds nothing between requests (Plan 2a, Task 4).

No periodic work runs inside the web process (that is the jobs Lambda's),
health answers without a database, an idle instance holds no connection, and
a database outage is a 503 with Retry-After, never a crash.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from review_fixtures import TEST_DSN, login, make_user

APP = Path(__file__).resolve().parents[1] / "services" / "dashboard" / "app"
pytestmark = pytest.mark.anyio


def test_building_the_app_starts_no_periodic_work():
    source = (APP / "main.py").read_text()
    for loop in ("SLOChecker(", "MatviewRefresher(", "slo-checker", "matview-refresher"):
        assert loop not in source, loop


async def test_startup_fires_no_alerts_and_holds_no_connection(rdb, rclient):
    # The in-process SLO checker used to fire pipeline_silence against an
    # empty database the moment the app started; now nothing runs at startup.
    await make_user(rdb, "boss", role="admin")
    login(rclient, "boss")
    names = {a["name"] for a in rclient.get("/api/alerts/active").json()}
    assert "pipeline_silence" not in names
    # Idle instances hold no connection: the pool starts empty and the one
    # connection the requests above used is the most the dashboard keeps.
    async with rdb.connection() as conn:
        row = await (
            await conn.execute(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() AND application_name = 'wahoobay-dashboard'"
            )
        ).fetchone()
    assert row[0] <= 1


def test_health_and_sign_in_without_a_database(monkeypatch):
    from app.auth import COOKIE_NAME
    from app.main import build_app

    monkeypatch.setenv("DATABASE_URL", "postgresql://nobody@127.0.0.1:1/none")
    monkeypatch.setenv("DASHBOARD_DB_TIMEOUT_S", "1")
    monkeypatch.setenv("DASHBOARD_COOKIE_SECURE", "false")
    with TestClient(build_app()) as client:
        assert client.get("/healthz").status_code == 200
        assert "<title>" in client.get("/login").text
        client.cookies.set(COOKIE_NAME, "a-session-the-database-would-have-to-check")
        r = client.get("/api/events")
        assert r.status_code == 503 and r.headers["Retry-After"] == "5"
        assert r.headers["content-type"].startswith("application/json")
        # A browser on a page gets a page: "waking the database", refreshing itself
        # (browsers ignore Retry-After on navigations), not a wall of JSON.
        page = client.get("/", headers={"Accept": "text/html,application/xhtml+xml"})
        assert page.status_code == 503 and page.headers["Retry-After"] == "5"
        assert page.headers["content-type"].startswith("text/html")
        assert 'http-equiv="refresh"' in page.text and "database" in page.text.lower()


async def test_the_worker_proxy_is_optional(rdb, review_assets, monkeypatch):
    from app.main import build_app

    monkeypatch.setenv("DATABASE_URL", TEST_DSN)
    monkeypatch.setenv("DASHBOARD_COOKIE_SECURE", "false")
    monkeypatch.setenv("WORKER_URL", "")
    for key, value in review_assets["env"].items():
        monkeypatch.setenv(key, value)
    await make_user(rdb, "viewer", role="viewer")
    with TestClient(build_app()) as client:
        login(client, "viewer")
        # No worker configured: the live-video routes say so instead of dialling nowhere.
        r = client.get("/api/live.json")
        assert r.status_code == 503 and "WORKER_URL" in r.json()["error"]
        assert client.get("/api/stats").status_code == 503

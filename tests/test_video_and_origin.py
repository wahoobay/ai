"""Live video without a worker, the mail backend, the origin check and robots
(Plan 2a, Task 8). Plan 2b sets these per environment: LIVE_VIDEO_EMBED_URL,
MAIL_BACKEND (+ MAIL_SMTP_*), ORIGIN_VERIFY_SECRET, ROBOTS."""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient
from review_fixtures import TEST_DSN, login, make_user

pytestmark = pytest.mark.anyio
EMBED = "https://www.youtube.com/embed/abc123"


def _app(monkeypatch, **env):
    from app.main import build_app

    monkeypatch.setenv("DATABASE_URL", env.pop("DATABASE_URL", "postgresql://u:p@h:5432/d"))
    monkeypatch.setenv("DASHBOARD_COOKIE_SECURE", "false")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return build_app()


async def test_video_panel_is_the_embed_when_no_worker(rdb, review_assets, monkeypatch):
    for key, value in review_assets["env"].items():
        monkeypatch.setenv(key, value)
    app = _app(monkeypatch, DATABASE_URL=TEST_DSN, WORKER_URL="", LIVE_VIDEO_EMBED_URL=EMBED)
    assert not any(r.path.startswith("/api/stream") for r in app.routes)
    await make_user(rdb, "viewer", role="viewer")
    with TestClient(app) as client:
        login(client, "viewer")
        html = client.get("/").text
    assert "<iframe" in html and f'src="{EMBED}"' in html
    assert 'id="stream"' not in html and 'id="bbox-toggle"' not in html


async def test_video_panel_is_the_worker_stream_when_there_is_one(rdb, review_assets, monkeypatch):
    for key, value in review_assets["env"].items():
        monkeypatch.setenv(key, value)
    app = _app(
        monkeypatch,
        DATABASE_URL=TEST_DSN,
        WORKER_URL="http://127.0.0.1:1",
        LIVE_VIDEO_EMBED_URL=EMBED,
    )
    assert "/api/stream.mjpeg" in {r.path for r in app.routes}
    await make_user(rdb, "viewer", role="viewer")
    with TestClient(app) as client:
        login(client, "viewer")
        html = client.get("/").text
    assert 'id="stream"' in html and 'id="bbox-toggle"' in html and "<iframe" not in html


def test_origin_header_required_when_secret_set(monkeypatch):
    with TestClient(_app(monkeypatch, ORIGIN_VERIFY_SECRET="s3cret")) as client:
        assert (
            client.get("/healthz").status_code == 200
        )  # App Runner's health check comes straight in
        assert client.get("/login").status_code == 403
        assert client.get("/login", headers={"X-Origin-Verify": "wrong"}).status_code == 403
        assert client.get("/login", headers={"X-Origin-Verify": "s3cret"}).status_code == 200
    with TestClient(_app(monkeypatch, ORIGIN_VERIFY_SECRET="")) as client:
        assert (
            client.get("/login").status_code == 200
        )  # no secret: the check is off (local, the DGX)


def test_robots_disallows_by_default(monkeypatch):
    with TestClient(_app(monkeypatch)) as client:
        r = client.get("/robots.txt")  # open to anonymous crawlers, like /healthz
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
        assert "Disallow: /" in r.text
    with TestClient(_app(monkeypatch, ROBOTS="allow")) as client:
        text = client.get("/robots.txt").text
        assert "Allow: /" in text and "Disallow: /" not in text


def test_console_mail_backend_logs_instead_of_sending(caplog):
    from app.mailer import Mailer

    mailer = Mailer("console", None, "noreply@example.test", "http://localhost:18080")
    with caplog.at_level(logging.INFO):
        assert (
            mailer.send(
                "kid@example.test",
                "Verify your address",
                "Open http://localhost:18080/verify-email?token=t0k",
            )
            == "console"
        )
    assert "kid@example.test" in caplog.text and "Verify your address" in caplog.text
    assert "token=t0k" in caplog.text


def test_mail_settings_come_from_the_mail_variables(monkeypatch):
    from app.main import DashboardCfg

    monkeypatch.setenv("SENSESTREAM_DEPLOYMENT_URI", "site_x")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h:5432/d")
    for key in ("MAIL_BACKEND", "DASHBOARD_MAIL_BACKEND", "MAIL_FROM", "DASHBOARD_SMTP_FROM"):
        monkeypatch.delenv(key, raising=False)
    assert DashboardCfg().mail_backend == "console"  # nothing set: log the message, send nothing
    monkeypatch.setenv("DASHBOARD_MAIL_BACKEND", "manual")
    assert DashboardCfg().mail_backend == "manual"  # the old spelling still counts
    monkeypatch.setenv("MAIL_BACKEND", "smtp")
    monkeypatch.setenv("MAIL_SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("MAIL_SMTP_PORT", "2525")
    monkeypatch.setenv("MAIL_SMTP_USER", "mailer")
    monkeypatch.setenv("MAIL_SMTP_PASSWORD", "pw")
    monkeypatch.setenv("MAIL_FROM", "hello@example.test")
    cfg = DashboardCfg()
    assert cfg.mail_backend == "smtp" and cfg.smtp["host"] == "smtp.example.test"
    assert (
        cfg.smtp["port"] == 2525
        and cfg.smtp["user"] == "mailer"
        and cfg.smtp_from == "hello@example.test"
    )

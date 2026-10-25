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
    # The player starts by itself: browsers only autoplay a muted player, and
    # playsinline keeps phones from taking it full screen.
    assert "<iframe" in html and f'src="{EMBED}?autoplay=1&amp;mute=1&amp;playsinline=1"' in html
    assert 'id="stream"' not in html and 'id="bbox-toggle"' not in html


async def test_no_worker_url_at_all_means_no_worker(rdb, review_assets, monkeypatch):
    """Staging's first look: App Runner gave the dashboard no WORKER_URL and the page
    showed a dead MJPEG panel instead of the embed, because the code's default was a
    worker on localhost. A site without a configured worker has no worker."""
    for key, value in review_assets["env"].items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("WORKER_URL", raising=False)
    app = _app(monkeypatch, DATABASE_URL=TEST_DSN, LIVE_VIDEO_EMBED_URL=EMBED)
    assert not any(r.path.startswith("/api/stream") for r in app.routes)
    await make_user(rdb, "viewer2", role="viewer")
    with TestClient(app) as client:
        login(client, "viewer2")
        html = client.get("/").text
    assert "<iframe" in html and 'id="bbox-toggle"' not in html


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


async def test_stream_urls_come_from_stream_base_url(rdb, review_assets, monkeypatch):
    """Plan 4a: on AWS the browser fetches the MJPEG from CloudFront (/stream/*, the GPU host),
    not through App Runner, which cuts requests at 120 s; the page says where."""
    for key, value in review_assets["env"].items():
        monkeypatch.setenv(key, value)
    app = _app(monkeypatch, DATABASE_URL=TEST_DSN, WORKER_URL="http://127.0.0.1:1", STREAM_BASE_URL="/stream")
    await make_user(rdb, "viewer3", role="viewer")
    with TestClient(app) as client:
        login(client, "viewer3")
        html = client.get("/").text
    assert 'src="/stream/stream.mjpeg"' in html and 'data-stream-base="/stream"' in html


def test_app_js_builds_stream_urls_from_the_page_base():
    from pathlib import Path

    js = (Path(__file__).resolve().parents[1] / "services" / "dashboard" / "app" / "static" / "app.js").read_text()
    assert "dataset.streamBase" in js and js.count('"/api/stream') == 0


def test_autoplay_parameters_are_added_once_and_keep_what_is_there():
    from app.config import autoplay_embed

    assert autoplay_embed("") == ""
    assert autoplay_embed(EMBED) == f"{EMBED}?autoplay=1&mute=1&playsinline=1"
    assert (
        autoplay_embed(f"{EMBED}?si=abc&mute=0")
        == f"{EMBED}?si=abc&mute=0&autoplay=1&playsinline=1"
    )
    assert autoplay_embed(autoplay_embed(EMBED)) == autoplay_embed(EMBED)


def test_origin_header_required_when_secret_set(monkeypatch):
    with TestClient(_app(monkeypatch, ORIGIN_VERIFY_SECRET="s3cret")) as client:
        assert (
            client.get("/healthz").status_code == 200
        )  # App Runner's health check comes straight in
        assert client.get("/login").status_code == 403
        assert client.get("/login", headers={"X-Origin-Verify": "wrong"}).status_code == 403
        # A non-ASCII header value (raw bytes, as a stray client would send) is refused, never a 500.
        assert client.get("/login", headers={"X-Origin-Verify": b"s\xe9cret"}).status_code == 403
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

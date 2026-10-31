"""The live panel plays HLS through hls.js when LIVE_HLS_BASE_URL is set, and keeps
the MJPEG <img> otherwise."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from review_fixtures import TEST_DSN, login, make_user

pytestmark = pytest.mark.anyio
APP_DIR = Path(__file__).resolve().parents[1] / "services" / "dashboard" / "app"


def _app(monkeypatch, **env):
    from app.main import build_app

    monkeypatch.setenv("DATABASE_URL", TEST_DSN)
    monkeypatch.setenv("DASHBOARD_COOKIE_SECURE", "false")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return build_app()


async def test_the_page_plays_hls_when_a_base_is_configured(rdb, review_assets, monkeypatch):
    for key, value in review_assets["env"].items():
        monkeypatch.setenv(key, value)
    await make_user(rdb, "rev", role="reviewer")
    app = _app(monkeypatch, WORKER_URL="http://worker:8081", LIVE_HLS_BASE_URL="/hls")
    with TestClient(app) as client:
        login(client, "rev")
        html = client.get("/").text
    assert '<video id="stream"' in html and 'data-hls-base="/hls"' in html
    assert "/static/vendor/hls.min.js" in html
    assert '<img id="stream"' not in html


async def test_the_page_keeps_mjpeg_without_an_hls_base(rdb, review_assets, monkeypatch):
    for key, value in review_assets["env"].items():
        monkeypatch.setenv(key, value)
    await make_user(rdb, "rev", role="reviewer")
    monkeypatch.delenv("LIVE_HLS_BASE_URL", raising=False)
    app = _app(monkeypatch, WORKER_URL="http://worker:8081")
    with TestClient(app) as client:
        login(client, "rev")
        html = client.get("/").text
    assert '<img id="stream"' in html and 'data-hls-base=""' in html
    assert "/static/vendor/hls.min.js" not in html
    assert '<video id="stream"' not in html


def test_the_player_retries_forever_and_falls_back_to_native_hls():
    js = (APP_DIR / "static" / "app.js").read_text()
    assert "dataset.hlsBase" in js and "index.m3u8" in js and '"_boxes"' in js
    assert "Hls.isSupported()" in js and 'canPlayType("application/vnd.apple.mpegurl")' in js
    assert "Hls.Events.ERROR" in js and "startLoad(" in js and "recoverMediaError(" in js
    assert "liveSourceUrl(" in js and "setLiveSource(" in js
    assert "shouldRetry" in js


def test_hls_js_is_vendored_and_pinned():
    vendor = APP_DIR / "static" / "vendor"
    assert (vendor / "hls.min.js").stat().st_size > 100_000
    assert "1.6.16" in (vendor / "README.md").read_text()

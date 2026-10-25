"""A site with the review tool off (FEATURE_REVIEW_TOOL=false) must not advertise
it: the first staging sign-in showed a "Review fish" link that led to a 404,
because the header chip only looked at the account's role."""

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


async def test_the_page_tells_the_header_whether_the_review_tool_exists(
    rdb, review_assets, monkeypatch
):
    for key, value in review_assets["env"].items():
        monkeypatch.setenv(key, value)
    await make_user(rdb, "rev", role="reviewer")
    for flag, review_status in (("false", 404), ("true", 200)):
        app = _app(monkeypatch, FEATURE_REVIEW_TOOL=flag, WORKER_URL="")
        with TestClient(app) as client:
            login(client, "rev")
            assert f'data-review-tool="{flag}"' in client.get("/").text
            assert client.get("/review").status_code == review_status


def test_the_header_chip_reads_the_flag_before_offering_the_review_link():
    js = (APP_DIR / "static" / "auth.js").read_text()
    assert "dataset.reviewTool" in js
    chip = js[: js.index("chip-review")]
    assert "reviewTool" in chip[-500:], "the Review link must be conditional on the page's flag"

"""While the GPU host plays a recording in the camera's place (deploy/gpu-host/replay.conf names one for
this environment), the page says "historical footage" instead of "Live"; with the camera, it says "Live"."""

from __future__ import annotations

from pathlib import Path

import pytest
from app.config import replay_conf_path, replay_key
from fastapi.testclient import TestClient
from review_fixtures import TEST_DSN, login, make_user

pytestmark = pytest.mark.anyio
ROOT = Path(__file__).resolve().parents[1]
REPLAY_STAGING = "# comment\nstaging=replay/pier.mp4\nprod=\n"


def test_replay_key_reads_the_file_the_way_the_host_does(tmp_path):
    conf = tmp_path / "replay.conf"
    conf.write_text(
        "# staging=replay/old.mp4\nstaging=replay/a.mp4\nstaging= replay/b.mp4 \nprod=\n"
    )
    assert replay_key("staging", conf) == "replay/b.mp4"  # the last line wins, whitespace removed
    assert replay_key("prod", conf) == ""
    assert replay_key("", conf) == ""  # no environment name: the DGX, a laptop
    assert replay_key("staging", tmp_path / "missing.conf") == ""


def test_the_checkout_and_the_image_find_the_same_replay_conf():
    assert replay_conf_path({}) == ROOT / "deploy" / "gpu-host" / "replay.conf"
    # The image's layout: the app is /app/app, whose parents are only /app and /.
    assert replay_conf_path({}, app_dir=Path("/app/app")) == Path(
        "/app/deploy/gpu-host/replay.conf"
    )
    dockerfile = (ROOT / "services" / "dashboard" / "Dockerfile").read_text()
    assert "COPY deploy/gpu-host/replay.conf /app/deploy/gpu-host/replay.conf" in dockerfile


def _page(rdb_env, monkeypatch, tmp_path, env_name: str) -> str:
    from app.main import build_app

    for key, value in rdb_env.items():
        monkeypatch.setenv(key, value)
    conf = tmp_path / "replay.conf"
    conf.write_text(REPLAY_STAGING)
    monkeypatch.setenv("DATABASE_URL", TEST_DSN)
    monkeypatch.setenv("DASHBOARD_COOKIE_SECURE", "false")
    monkeypatch.setenv("WORKER_URL", "http://worker:8081")
    monkeypatch.setenv("LIVE_HLS_BASE_URL", "/hls")
    monkeypatch.setenv("REPLAY_CONF", str(conf))
    monkeypatch.setenv("ENV", env_name)
    with TestClient(build_app()) as client:
        login(client, "viewer")
        return client.get("/").text


async def test_a_replaying_environment_says_historical_footage_not_live(
    rdb, review_assets, monkeypatch, tmp_path
):
    await make_user(rdb, "viewer", role="viewer")
    html = _page(review_assets["env"], monkeypatch, tmp_path, "staging")
    assert "<title>Wahoo Bay · Fish ID</title>" in html
    assert "Live detections and tracking from historical footage" in html
    assert (
        "Live Fish ID" not in html and "live video" not in html and "Live camera feed" not in html
    )


async def test_an_environment_on_its_camera_still_says_live(
    rdb, review_assets, monkeypatch, tmp_path
):
    await make_user(rdb, "viewer", role="viewer")
    html = _page(review_assets["env"], monkeypatch, tmp_path, "prod")
    assert "<h1>Wahoo Bay · Live Fish ID</h1>" in html and "Live camera feed" in html
    assert "historical footage" not in html

"""No site in the code: the environment names the site (Plan 2a, Task 2).

The same contract is read by the dashboard, the poller and the worker:
SENSESTREAM_DEPLOYMENT_URI (required), LOCATION_ID, TZ, EXPORT_PREFIX, the two
data paths, and DATABASE_URL or the DB_* parts it is assembled from.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DB_PARTS = {
    "DB_HOST": "db.internal",
    "DB_PORT": "5432",
    "DB_NAME": "ai",
    "DB_USER": "ai",
    "DB_PASSWORD": "p w",
}
SITE_KEYS = (
    "LOCATION_ID",
    "EXPORT_PREFIX",
    "TZ",
    "CAMERA_METADATA_PATH",
    "NEGATIVE_REGIONS_PATH",
    "SENSESTREAM_WEATHER_DEPLOYMENT_URI",
)


def _site(monkeypatch, **extra):
    monkeypatch.setenv("SENSESTREAM_DEPLOYMENT_URI", "site_x")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h:5432/d")
    for key in SITE_KEYS:
        monkeypatch.delenv(key, raising=False)
    for key, value in extra.items():
        monkeypatch.setenv(key, value)


def test_deployment_uri_is_required(monkeypatch):
    from services.dashboard.app.config import settings

    _site(monkeypatch)
    monkeypatch.delenv("SENSESTREAM_DEPLOYMENT_URI")
    with pytest.raises(RuntimeError, match="SENSESTREAM_DEPLOYMENT_URI"):
        settings()


def test_poller_deployment_uri_is_required(monkeypatch):
    from services.sensestream_poller.app.main import Config

    _site(monkeypatch)
    monkeypatch.delenv("SENSESTREAM_DEPLOYMENT_URI")
    with pytest.raises(RuntimeError, match="SENSESTREAM_DEPLOYMENT_URI"):
        Config.from_env()


def test_database_url_is_assembled_from_parts(monkeypatch):
    from services.dashboard.app.config import settings

    _site(monkeypatch)
    monkeypatch.delenv("DATABASE_URL")
    for key, value in DB_PARTS.items():
        monkeypatch.setenv(key, value)
    assert settings().database_url == "postgresql://ai:p%20w@db.internal:5432/ai"


def test_database_url_or_parts_are_required(monkeypatch):
    from services.dashboard.app.config import settings

    _site(monkeypatch)
    monkeypatch.delenv("DATABASE_URL")
    for key in DB_PARTS:
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        settings()


def test_dashboard_config_carries_the_site(monkeypatch):
    from services.dashboard.app.main import DashboardCfg

    _site(monkeypatch)
    cfg = DashboardCfg()
    assert (cfg.location_id, cfg.export_prefix, cfg.deployment_uri, cfg.tz) == (
        "wahoobay",
        "wahoobay",
        "site_x",
        "UTC",
    )
    assert cfg.camera_metadata_path == "/data/camera_metadata.json"
    assert cfg.negative_regions_path == "/data/camera_negative_regions.json"
    assert cfg.port == 8080


def test_export_prefix_follows_location_id(monkeypatch):
    from services.dashboard.app.config import settings

    _site(monkeypatch, LOCATION_ID="reef_b")
    assert settings().export_prefix == "reef_b"


def test_poller_weather_rows_default_to_the_sonde_deployment(monkeypatch):
    from services.sensestream_poller.app.main import Config

    _site(monkeypatch)
    cfg = Config.from_env()
    assert cfg.weather_deployment_uri == cfg.deployment_uri == "site_x"


def test_worker_config_names_its_location(monkeypatch):
    from services.worker.app.config import Config

    _site(monkeypatch)
    assert Config.from_env().location_id == "wahoobay"


def test_no_site_strings_left_in_code():
    # The application code and the shared generator carry no site. The DGX
    # launchers (scripts/dev) and the one-off analyses (scripts/contest) are
    # host tooling for one installation and are left out on purpose.
    cmd = [
        "git",
        "grep",
        "-nIE",
        "--untracked",
        r"wahoo_[0-9]|/raid/scratch",
        "--",
        "services",
        "scripts",
        ":!scripts/dev",
        ":!scripts/contest",
    ]
    env = {**os.environ, "GIT_PAGER": "cat"}
    hits = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True, env=env).stdout
    assert hits == "", hits

"""The dashboard renders in the site's time zone (TZ), not in a zone written into the code."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

APP = Path(__file__).resolve().parents[1] / "services" / "dashboard" / "app"


def test_review_windows_use_the_configured_time_zone(monkeypatch):
    from app import review_browse

    monkeypatch.setenv("TZ", "Pacific/Auckland")
    assert review_browse.local_day_start(date(2026, 1, 1)).tzinfo == ZoneInfo("Pacific/Auckland")


def test_hour_of_day_sql_takes_the_zone_as_a_parameter():
    # The dashboard's and the public API's local-time SQL lives in public_queries.py.
    for name in ("main.py", "public_queries.py", "routes_public.py"):
        assert "AT TIME ZONE 'America/New_York'" not in (APP / name).read_text(), name
    assert "AT TIME ZONE %s" in (APP / "public_queries.py").read_text()


def test_pages_hand_the_browser_the_site_time_zone():
    for name in ("index.html", "users_admin.html", "review.html"):
        assert 'data-tz="{{ tz }}"' in (APP / "templates" / name).read_text(), name
    for name in ("time-et.js", "review-core.js"):
        source = (APP / "static" / name).read_text()
        assert "document.documentElement.dataset.tz" in source, name
        assert "America/New_York" not in source, name


def test_the_app_publishes_its_zone_to_the_templates(monkeypatch):
    from app import main

    monkeypatch.setenv("SENSESTREAM_DEPLOYMENT_URI", "site_x")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h:5432/d")
    monkeypatch.setenv("TZ", "Pacific/Auckland")
    main.build_app()
    assert main.TEMPLATES.env.globals["tz"] == "Pacific/Auckland"

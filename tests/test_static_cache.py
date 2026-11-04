"""Static files carry Cache-Control: no-cache, so CloudFront (whose /static/* policy ignores the ?v=
query) and browsers never keep a deploy's old CSS or JS in front of the new ones."""

from __future__ import annotations

from fastapi.testclient import TestClient
from review_fixtures import TEST_DSN


def test_static_files_are_revalidated_not_cached(monkeypatch):
    from app.main import build_app

    monkeypatch.setenv("DATABASE_URL", TEST_DSN or "postgresql://unused@localhost/unused")
    monkeypatch.setenv("DASHBOARD_COOKIE_SECURE", "false")
    with TestClient(build_app()) as client:
        for path in ("/static/style.css", "/static/tour.js?v=abc", "/static/vendor/driver.css"):
            r = client.get(path)
            assert r.status_code == 200, path
            assert r.headers["cache-control"] == "no-cache", path
            assert r.headers.get("etag"), path  # what a revalidation is answered against
        etag = client.get("/static/style.css").headers["etag"]
        assert client.get("/static/style.css", headers={"If-None-Match": etag}).status_code == 304

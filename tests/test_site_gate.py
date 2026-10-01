"""The whole site sits behind sign-in (owner's decision 2026-09-30, after
meeting Wahoo Bay): signed out, only the sign-in, sign-up and
forgot-password pages (and the email links they send) answer. An account
waiting for approval sees only the waiting page. Viewers get the
dashboard, reviewers the dashboard and the review site, admins
everything."""
from __future__ import annotations

import re

import pytest
from fastapi.routing import APIRoute
from review_fixtures import CSRF, PASSWORD, login, make_user

pytestmark = pytest.mark.anyio

# Every path a signed-out visitor may reach. Everything else answers 401.
OPEN_PATHS = {"/login", "/register", "/forgot-password", "/reset-password", "/verify-email",
              "/waiting", "/healthz"}
OPEN_PREFIXES = ("/api/auth/",)
JSON = {"Accept": "application/json"}
HTML = {"Accept": "text/html,application/xhtml+xml"}
WAITING = "your account is waiting for approval"


def _routes(client):
    """(method, concrete path) for every API route, path parameters filled in."""
    out = []
    for r in client.app.routes:
        if not isinstance(r, APIRoute):
            continue
        path = re.sub(r"\{[^}]+\}", "1", r.path)
        for method in sorted(r.methods):
            out.append((method, path, r.path))
    assert len(out) > 100, "expected the full route table"
    return out


def _is_open(path: str) -> bool:
    return path in OPEN_PATHS or path.startswith(OPEN_PREFIXES)


def _request(client, method, path, headers):
    extra = CSRF if method != "GET" else {}
    return client.request(method, path, headers={**headers, **extra}, follow_redirects=False)


async def test_signed_out_every_route_but_the_sign_in_pages_needs_a_session(rdb, rclient):
    leaks = []
    for method, path, template in _routes(rclient):
        if _is_open(template):
            continue
        r = _request(rclient, method, path, JSON)
        if r.status_code != 401:
            leaks.append(f"{method} {template} -> {r.status_code}")
    assert not leaks, "open to signed-out visitors:\n" + "\n".join(leaks)


async def test_signed_out_pages_go_to_sign_in_and_come_back(rdb, rclient):
    r = rclient.get("/", headers=HTML, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/login?next=/"
    r = rclient.get("/admin/users.html", headers=HTML, follow_redirects=False)
    assert r.headers["location"] == "/login?next=/admin/users.html"


async def test_the_sign_in_pages_and_their_assets_stay_open(rdb, rclient):
    for path in ("/login", "/register", "/forgot-password", "/reset-password?token=x",
                 "/verify-email?token=x", "/healthz", "/static/style.css", "/static/auth.js",
                 "/static/theme.css", "/static/theme.js"):
        assert rclient.get(path, follow_redirects=False).status_code == 200, path
    assert rclient.get("/api/auth/me").json()["authenticated"] is False


async def test_the_api_docs_pages_are_gone(rdb, rclient):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert rclient.get(path).status_code == 404, path


async def test_an_account_waiting_for_approval_sees_only_the_waiting_page(rdb, rclient):
    await make_user(rdb, "newbie", role="viewer", status="pending")
    login(rclient, "newbie")
    leaks = []
    for method, path, template in _routes(rclient):
        if _is_open(template):
            continue
        r = _request(rclient, method, path, JSON)
        if r.status_code != 403 or r.json().get("detail") != WAITING:
            leaks.append(f"{method} {template} -> {r.status_code}")
    assert not leaks, "reachable while waiting for approval:\n" + "\n".join(leaks)
    r = rclient.get("/", headers=HTML, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/waiting"
    page = rclient.get("/waiting", headers=HTML)
    assert page.status_code == 200 and "waiting for approval" in page.text.lower()


async def test_the_waiting_page_sends_everyone_else_on(rdb, rclient):
    r = rclient.get("/waiting", headers=HTML, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith("/login")
    await make_user(rdb, "vera", role="viewer")
    login(rclient, "vera")
    r = rclient.get("/waiting", headers=HTML, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/"


DASHBOARD = ["/", "/api/species_counts", "/api/export/README.md", "/api/celebrities"]
REVIEW = ["/review", "/api/review/species", "/api/review/mine/recent"]
ADMIN = ["/admin/users.html", "/api/admin/users", "/admin/regions.html", "/api/admin/negative_regions"]


@pytest.mark.parametrize("role, allowed, refused", [
    ("viewer", DASHBOARD, REVIEW + ADMIN),
    ("reviewer", DASHBOARD + REVIEW, ADMIN),
    ("admin", DASHBOARD + REVIEW + ADMIN, []),
])
async def test_each_role_reaches_exactly_its_part_of_the_site(rdb, rclient, role, allowed,
                                                              refused):
    await make_user(rdb, "someone", role=role)
    login(rclient, "someone")
    for path in allowed:
        assert rclient.get(path, headers=JSON).status_code == 200, f"{role} {path}"
    for path in refused:
        assert rclient.get(path, headers=JSON).status_code == 403, f"{role} {path}"


async def test_a_refused_page_says_so_in_the_sites_style(rdb, rclient):
    await make_user(rdb, "vera", role="viewer")
    login(rclient, "vera")
    r = rclient.get("/review", headers=HTML)
    assert r.status_code == 403
    assert "auth-card" in r.text and "access" in r.text.lower()


async def test_signing_up_still_works_signed_out(rdb, rclient):
    r = rclient.post("/api/auth/register", headers=CSRF, json={
        "username": "fresh", "display_name": "Fresh", "email": "fresh@example.org",
        "password": PASSWORD})
    assert r.status_code == 200, r.text

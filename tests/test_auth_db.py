"""Integration tests: schema, session store, and the HTTP auth flows.

Skipped unless TEST_DATABASE_URL points at a throwaway database. These
tests truncate tables — never point it at the production database."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import psycopg
import pytest
from review_fixtures import T0, jpeg

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "dashboard"))

TEST_DSN = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DSN, reason="TEST_DATABASE_URL not set")


def test_schema_has_auth_tables_and_columns():
    with psycopg.connect(TEST_DSN) as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_name IN ('users','user_sessions','user_tokens','user_audit')
        """)
        assert {r[0] for r in cur.fetchall()} == {
            "users",
            "user_sessions",
            "user_tokens",
            "user_audit",
        }

        cur.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'detection_corrections'
              AND column_name IN ('user_id','session_id')
        """)
        assert {r[0] for r in cur.fetchall()} == {"user_id", "session_id"}


def test_status_and_role_check_constraints_reject_bad_values():
    with psycopg.connect(TEST_DSN) as conn, conn.cursor() as cur:
        for column, bad in (("role", "superuser"), ("status", "banned")):
            with pytest.raises(psycopg.errors.CheckViolation):
                cur.execute(
                    f"""INSERT INTO users (username, display_name, email,
                                           password_hash, {column})
                        VALUES ('x','X','x@example.org','h', %s)""",
                    (bad,),
                )
            conn.rollback()


from datetime import UTC, datetime, timedelta  # noqa: E402

from app.auth import (  # noqa: E402
    TOUCH_THROTTLE_S,
    create_session,
    create_user_token,
    effective_role,
    hash_password,
    load_session,
    needs_touch,
    redeem_user_token,
    revoke_all_sessions,
    revoke_session,
    touch_session,
)
from psycopg_pool import AsyncConnectionPool  # noqa: E402


@pytest.fixture
async def pool():
    p = AsyncConnectionPool(TEST_DSN, min_size=1, max_size=2, open=False)
    await p.open()
    async with p.connection() as conn:
        await conn.execute("TRUNCATE users, user_sessions, user_tokens, user_audit CASCADE")
        # detection_events/detection_corrections aren't otherwise part of
        # this fixture's domain, but test_approved_reviewer_can_submit_a_
        # correction seeds a fixed-id (4242) detection_events row and a
        # correction referencing it. Truncate both here so a second run
        # against the same database doesn't collide on that primary key.
        await conn.execute("TRUNCATE detection_events, detection_corrections, saved_frames CASCADE")
    yield p
    await p.close()


@pytest.fixture
def client(pool, monkeypatch):
    from app.main import build_app

    # Network isolation (SHEET_URL/WORKER_URL/POLLER_URL) comes from the
    # autouse fixture in conftest.py — every fixture that builds the app
    # gets it unconditionally, so it isn't repeated (or, as happened
    # before, silently omitted) here.
    monkeypatch.setenv("DATABASE_URL", TEST_DSN)
    monkeypatch.setenv("DASHBOARD_COOKIE_SECURE", "false")
    with TestClient(build_app()) as c:
        yield c


@pytest.fixture
def client_secure(pool, monkeypatch):
    from app.main import build_app

    monkeypatch.setenv("DATABASE_URL", TEST_DSN)
    monkeypatch.setenv("DASHBOARD_COOKIE_SECURE", "true")
    with TestClient(build_app()) as c:
        yield c


async def _make_user(pool, username="rev", role="reviewer", status="active"):
    async with pool.connection() as conn:
        cur = await conn.execute(
            """INSERT INTO users (username, display_name, email, password_hash,
                                  role, status)
               VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
            (
                username,
                username.title(),
                f"{username}@example.org",
                hash_password("a long enough passphrase"),
                role,
                status,
            ),
        )
        return (await cur.fetchone())[0]


@pytest.mark.anyio
async def test_session_roundtrip_and_raw_token_is_not_stored(pool):
    uid = await _make_user(pool)
    raw, sid = await create_session(pool, uid, 12, 7, "pytest", "127.0.0.1")

    who = await load_session(pool, raw)
    assert who is not None and who.id == uid and who.role == "reviewer"
    assert who.session_id == sid

    async with pool.connection() as conn:
        cur = await conn.execute("SELECT token_hash FROM user_sessions WHERE id = %s", (sid,))
        assert (await cur.fetchone())[0] != raw  # only the digest is persisted


@pytest.mark.anyio
async def test_load_session_rejects_unknown_revoked_and_expired(pool):
    uid = await _make_user(pool)
    raw, sid = await create_session(pool, uid, 12, 7, None, None)

    assert await load_session(pool, "not-a-real-token") is None

    await revoke_session(pool, sid)
    assert await load_session(pool, raw) is None

    raw2, sid2 = await create_session(pool, uid, 12, 7, None, None)
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE user_sessions SET expires_at = NOW() - INTERVAL '1 minute' WHERE id = %s",
            (sid2,),
        )
    assert await load_session(pool, raw2) is None


@pytest.mark.anyio
async def test_load_session_rejects_disabled_user(pool):
    uid = await _make_user(pool, username="gone", status="disabled")
    raw, _ = await create_session(pool, uid, 12, 7, None, None)
    assert await load_session(pool, raw) is None


@pytest.mark.anyio
async def test_revoke_all_sessions(pool):
    uid = await _make_user(pool)
    tokens = [(await create_session(pool, uid, 12, 7, None, None))[0] for _ in range(3)]
    assert await revoke_all_sessions(pool, uid) == 3
    for raw in tokens:
        assert await load_session(pool, raw) is None


@pytest.mark.anyio
async def test_touch_session_extends_and_never_exceeds_absolute_cap(pool):
    # Brief correction / Ruling 5: the absolute session lifetime is
    # configurable via abs_ttl_days, not a hardcoded 7 days. A touch must
    # (a) push expires_at forward when there's room under the cap, and
    # (b) never let expires_at cross created_at + abs_ttl_days even when
    # ttl_hours alone would overshoot it. A swapped hours/days argument,
    # or a LEAST/GREATEST flip, passes (a) but fails (b).
    uid = await _make_user(pool)
    raw, sid = await create_session(pool, uid, 1, 2, None, None)  # ttl_hours=1, abs_ttl_days=2

    async def _row():
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT created_at, expires_at FROM user_sessions WHERE id = %s",
                (sid,),
            )
            return await cur.fetchone()

    created_at, expires_before = await _row()

    # A normal touch, well inside the absolute cap, re-extends expires_at forward.
    await touch_session(pool, sid, 1, 2)
    _, expires_after_normal = await _row()
    assert expires_after_normal > expires_before

    # A touch whose ttl_hours is huge enough to blow past the absolute cap must
    # still be clamped to created_at + abs_ttl_days, not to NOW() + ttl_hours.
    await touch_session(pool, sid, 24 * 365, 2)  # a "1 year" ttl vs. a 2-day cap
    _, expires_after_overshoot = await _row()
    abs_cap = created_at + timedelta(days=2)
    assert expires_after_overshoot <= abs_cap + timedelta(seconds=5)
    assert abs(expires_after_overshoot - abs_cap) < timedelta(seconds=5)


def test_needs_touch_respects_throttle():
    # Review Focus 3: these comparisons must be timezone-aware or every
    # request 500s with "can't compare offset-naive and offset-aware".
    now = datetime.now(UTC)
    assert needs_touch(now - timedelta(seconds=TOUCH_THROTTLE_S + 5), now) is True
    assert needs_touch(now - timedelta(seconds=5), now) is False


def test_effective_role_pins_pending_and_disabled():
    assert effective_role("active", "admin") == "admin"
    # Since 2026-09-30 the site is behind sign-in and anyone can register,
    # so an account waiting for approval gets nothing, not viewer access.
    assert effective_role("pending", "admin") == "pending"
    assert effective_role("pending", "reviewer") == "pending"
    assert effective_role("disabled", "admin") == "none"


@pytest.mark.anyio
async def test_user_token_is_single_use_scoped_and_expiring(pool):
    uid = await _make_user(pool)

    raw = await create_user_token(pool, uid, "password_reset", 30, None)
    assert await redeem_user_token(pool, raw, "email_verify") is None  # wrong purpose
    assert await redeem_user_token(pool, raw, "password_reset") == uid
    assert await redeem_user_token(pool, raw, "password_reset") is None  # single use

    stale = await create_user_token(pool, uid, "email_verify", 30, None)
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE user_tokens SET expires_at = NOW() - INTERVAL '1 minute' "
            "WHERE token_hash IS NOT NULL AND used_at IS NULL",
        )
    assert await redeem_user_token(pool, stale, "email_verify") is None


@pytest.mark.anyio
async def test_redeem_strips_surrounding_whitespace(pool):
    # Review Focus 4: mail clients wrap and pad links.
    uid = await _make_user(pool)
    raw = await create_user_token(pool, uid, "password_reset", 30, None)
    assert await redeem_user_token(pool, f"  {raw}\n", "password_reset") == uid


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


from app.auth import (  # noqa: E402
    COOKIE_NAME,
    make_optional_user,
    make_require_role,
    require_csrf,
)
from fastapi import Depends, FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


class _Cfg:
    cookie_secure = False
    session_ttl_h = 12
    session_abs_ttl_d = 7


def _mini_app(pool):
    app = FastAPI()
    require_role = make_require_role(_Cfg(), {"pool": pool})
    optional_user = make_optional_user({"pool": pool})

    @app.get("/only-reviewers")
    async def only_reviewers(user=Depends(require_role("reviewer", "admin"))):
        return {"username": user.username}

    @app.get("/open")
    async def open_route():
        return {"ok": True}

    @app.get("/maybe")
    async def maybe(user=Depends(optional_user)):
        return {"username": user.username if user else None}

    @app.post("/csrf-check")
    async def csrf_check(_=Depends(require_csrf)):
        return {"ok": True}

    return app


async def _session_last_seen(pool, session_id):
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT last_seen_at FROM user_sessions WHERE id = %s",
            (session_id,),
        )
        return (await cur.fetchone())[0]


@pytest.mark.anyio
async def test_dependency_enforces_401_403_200(pool):
    admin_id = await _make_user(pool, username="boss", role="admin")
    pending_id = await _make_user(pool, username="newbie", role="reviewer", status="pending")
    admin_tok, _ = await create_session(pool, admin_id, 12, 7, None, None)
    pending_tok, _ = await create_session(pool, pending_id, 12, 7, None, None)

    with TestClient(_mini_app(pool)) as c:
        assert c.get("/only-reviewers").status_code == 401  # anonymous
        assert c.get("/open").status_code == 200  # public stays public

        c.cookies.set(COOKIE_NAME, pending_tok)
        assert c.get("/only-reviewers").status_code == 403  # pending → viewer
        c.cookies.clear()

        c.cookies.set(COOKIE_NAME, admin_tok)
        r = c.get("/only-reviewers")
        assert r.status_code == 200 and r.json()["username"] == "boss"


@pytest.mark.anyio
async def test_require_role_throttles_touch_within_window_then_touches_past_it(pool):
    # The task framing calls out that the dashboard polls several endpoints
    # every 30s, well inside TOUCH_THROTTLE_S (60s) — so most requests must
    # NOT write to user_sessions. Assert on last_seen_at in the database
    # itself, not on a mock, so this proves the write is genuinely suppressed
    # (and genuinely happens once the throttle window has passed), rather than
    # just proving needs_touch()/touch_session() work in isolation (Task 3
    # already covers that).
    uid = await _make_user(pool, username="throttled", role="reviewer")
    tok, sid = await create_session(pool, uid, 12, 7, None, None)
    seen_at_creation = await _session_last_seen(pool, sid)

    with TestClient(_mini_app(pool)) as c:
        c.cookies.set(COOKIE_NAME, tok)

        # First request, immediately after creation: well within the
        # throttle window, so no UPDATE should fire — and, per D1, no
        # cookie should be re-issued either: only a throttled touch slides
        # the cookie's max_age, so an unthrottled request must not carry
        # a Set-Cookie header at all.
        r1 = c.get("/only-reviewers")
        assert r1.status_code == 200
        assert "set-cookie" not in r1.headers
        seen_after_first = await _session_last_seen(pool, sid)
        assert seen_after_first == seen_at_creation

        # A second immediate request: still within the window.
        r2 = c.get("/only-reviewers")
        assert r2.status_code == 200
        assert "set-cookie" not in r2.headers
        seen_after_second = await _session_last_seen(pool, sid)
        assert seen_after_second == seen_at_creation

        # Force the throttle window to have elapsed by ageing the row
        # directly, rather than sleeping TOUCH_THROTTLE_S seconds.
        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE user_sessions SET last_seen_at = NOW() - INTERVAL '2 minutes' "
                "WHERE id = %s",
                (sid,),
            )
        aged_seen_at = await _session_last_seen(pool, sid)

        # D1: the throttled touch that fires here must also re-issue the
        # session cookie (same token, refreshed max_age) — this is what
        # keeps an actively-used session from being signed out by the
        # browser dropping an unrefreshed cookie despite a server session
        # that's still valid (and was just extended).
        r3 = c.get("/only-reviewers")
        assert r3.status_code == 200
        assert COOKIE_NAME in r3.headers.get("set-cookie", "")
        assert f"max-age={_Cfg.session_ttl_h * 3600}" in r3.headers["set-cookie"].lower()
        seen_after_third = await _session_last_seen(pool, sid)
        assert seen_after_third > aged_seen_at  # throttle expired: this request touched it


@pytest.mark.anyio
async def test_require_csrf_header_present_absent_wrong_value_and_case(pool):
    with TestClient(_mini_app(pool)) as c:
        # Correct header, correct value → passes through to the route.
        r = c.post("/csrf-check", headers={"X-Requested-With": "wahoobay"})
        assert r.status_code == 200 and r.json() == {"ok": True}

        # Header absent entirely → 403.
        assert c.post("/csrf-check").status_code == 403

        # Header present but wrong value → 403.
        r = c.post("/csrf-check", headers={"X-Requested-With": "not-wahoobay"})
        assert r.status_code == 403

        # Header present with the right value in the wrong case → still
        # passes: the comparison is deliberately case-insensitive.
        r = c.post("/csrf-check", headers={"X-Requested-With": "WaHoObAy"})
        assert r.status_code == 200 and r.json() == {"ok": True}


@pytest.mark.anyio
async def test_optional_user_yields_none_or_current_user_without_gating_or_touching(pool):
    uid = await _make_user(pool, username="maybeuser", role="viewer")
    tok, sid = await create_session(pool, uid, 12, 7, None, None)
    seen_before = await _session_last_seen(pool, sid)

    with TestClient(_mini_app(pool)) as c:
        # Anonymous: no cookie, not gated — 200 with a None user rather
        # than 401.
        r = c.get("/maybe")
        assert r.status_code == 200
        assert r.json()["username"] is None

        # Authenticated: yields the CurrentUser.
        c.cookies.set(COOKIE_NAME, tok)
        r = c.get("/maybe")
        assert r.status_code == 200
        assert r.json()["username"] == "maybeuser"

        # make_optional_user never touches the session (no throttled-touch
        # logic in it at all), regardless of how much time has passed.
        seen_after = await _session_last_seen(pool, sid)
        assert seen_after == seen_before


CSRF = {"X-Requested-With": "wahoobay"}


@pytest.mark.anyio
async def test_login_sets_httponly_cookie_and_never_returns_raw_token(pool, client):
    await _make_user(pool, username="dan", role="reviewer")
    r = client.post(
        "/api/auth/login",
        json={"username": "dan", "password": "a long enough passphrase"},
        headers=CSRF,
    )
    assert r.status_code == 200
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie
    token = client.cookies.get(COOKIE_NAME)
    assert token and token not in r.text


@pytest.mark.anyio
async def test_login_failures_are_indistinguishable(pool, client):
    await _make_user(pool, username="dan")
    unknown = client.post(
        "/api/auth/login", json={"username": "nobody", "password": "whatever at all"}, headers=CSRF
    )
    wrong = client.post(
        "/api/auth/login",
        json={"username": "dan", "password": "wrong but long enough"},
        headers=CSRF,
    )
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json() == wrong.json()


@pytest.mark.anyio
async def test_login_requires_csrf_header(pool, client):
    await _make_user(pool, username="dan")
    r = client.post(
        "/api/auth/login", json={"username": "dan", "password": "a long enough passphrase"}
    )
    assert r.status_code == 403


@pytest.mark.anyio
async def test_lockout_after_configured_failures(pool, client):
    await _make_user(pool, username="dan")
    for _ in range(8):
        client.post(
            "/api/auth/login",
            json={"username": "dan", "password": "definitely wrong here"},
            headers=CSRF,
        )
    # Correct password now refused because the account is locked.
    r = client.post(
        "/api/auth/login",
        json={"username": "dan", "password": "a long enough passphrase"},
        headers=CSRF,
    )
    assert r.status_code == 401


@pytest.mark.anyio
async def test_locked_account_returns_identical_generic_401_as_other_failures(pool, client):
    # Coordinator Finding 2: login() must perform exactly one
    # verify_password() call before any failure branch returns, so a
    # locked account can't be distinguished from an unknown username or
    # a wrong password — by response content here, and by construction
    # (not by a flaky wall-clock assertion) via the single verification
    # call that now precedes every branch in routes_auth.py's login().
    await _make_user(pool, username="dan")
    for _ in range(8):
        client.post(
            "/api/auth/login",
            json={"username": "dan", "password": "definitely wrong here"},
            headers=CSRF,
        )

    locked = client.post(
        "/api/auth/login",
        json={"username": "dan", "password": "a long enough passphrase"},
        headers=CSRF,
    )
    unknown = client.post(
        "/api/auth/login", json={"username": "nobody", "password": "whatever at all"}, headers=CSRF
    )

    assert locked.status_code == unknown.status_code == 401
    assert locked.json() == unknown.json() == {"error": "Invalid username or password."}


@pytest.mark.anyio
async def test_login_has_its_own_per_ip_rate_limit_separate_from_registration(pool, monkeypatch):
    # A1: an anonymous flood against /api/auth/login must be capped — a
    # nonexistent username still pays a full scrypt verification (the
    # timing-equalisation dummy hash) and can never trip the per-account
    # lockout, so this per-IP budget is what actually bounds the CPU cost
    # an anonymous attacker can force. Set a small, distinct budget so the
    # test doesn't need 61 requests, and confirm it's login's own counter
    # (DASHBOARD_LOGIN_MAX_PER_IP_H), not registration's.
    from app.main import build_app

    monkeypatch.setenv("DATABASE_URL", TEST_DSN)
    monkeypatch.setenv("DASHBOARD_COOKIE_SECURE", "false")
    monkeypatch.setenv("DASHBOARD_LOGIN_MAX_PER_IP_H", "3")
    # TestClient's default client tuple is ("testclient", 50000) — not a
    # parseable IP, so auth.client_ip() returns None for it and the
    # per-IP limiter (which no-ops on a falsy ip) never engages. Give it
    # a real address so this test actually exercises the limiter.
    with TestClient(build_app(), client=("203.0.113.5", 51000)) as c:
        for _ in range(3):
            r = c.post(
                "/api/auth/login",
                json={"username": "nobody", "password": "whatever at all"},
                headers=CSRF,
            )
            assert r.status_code == 401

        limited = c.post(
            "/api/auth/login",
            json={"username": "nobody", "password": "whatever at all"},
            headers=CSRF,
        )
        assert limited.status_code == 429

        # Registration's separate counter is untouched by login's budget.
        reg = c.post("/api/auth/register", json=REG, headers=CSRF)
        assert reg.status_code == 200


@pytest.mark.anyio
async def test_me_reports_anonymous_then_authenticated(pool, client):
    assert client.get("/api/auth/me").json() == {"authenticated": False}
    await _make_user(pool, username="dan")
    client.post(
        "/api/auth/login",
        json={"username": "dan", "password": "a long enough passphrase"},
        headers=CSRF,
    )
    me = client.get("/api/auth/me").json()
    assert me["authenticated"] is True
    assert me["username"] == "dan" and me["role"] == "reviewer" and me["status"] == "active"


@pytest.mark.anyio
async def test_logout_revokes_the_session(pool, client):
    await _make_user(pool, username="dan")
    client.post(
        "/api/auth/login",
        json={"username": "dan", "password": "a long enough passphrase"},
        headers=CSRF,
    )
    assert client.post("/api/auth/logout", headers=CSRF).status_code == 200
    assert client.get("/api/auth/me").json()["authenticated"] is False


@pytest.mark.anyio
async def test_cookie_secure_flag_comes_from_config(pool, client, client_secure):
    # Review Focus 5: a hardcoded Secure flag silently breaks login on the
    # plain-HTTP local origin — the browser drops the cookie and the user
    # sees a login that "works" then immediately forgets them, with nothing
    # in the logs. Assert the flag actually tracks configuration.
    await _make_user(pool, username="dan")
    creds = {"username": "dan", "password": "a long enough passphrase"}

    insecure = client.post("/api/auth/login", json=creds, headers=CSRF)
    assert "Secure" not in insecure.headers["set-cookie"]

    secure = client_secure.post("/api/auth/login", json=creds, headers=CSRF)
    assert "Secure" in secure.headers["set-cookie"]


def _next_field_value(html: str) -> str:
    m = re.search(r'name="next" value="([^"]*)"', html)
    assert m, "next hidden field not found in rendered login page"
    return m.group(1)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "malicious",
    [
        "https://evil.example/",  # absolute URL: open redirect
        "//evil.example/",  # protocol-relative URL: open redirect
        "javascript:alert(1)",  # scheme URI: XSS via location.href
        "/\\evil.example",  # backslash trick some browsers treat as //
        # Coordinator Finding 1: the WHATWG URL parser strips ASCII
        # tab/LF/CR wherever they occur in the input (not only at the
        # edges), so each of these three starts with a single "/" and
        # contains no "//" or "/\" as a plain string — passing every other
        # check — but the browser parses it to "//evil.example" (a
        # protocol-relative URL) the moment it reaches location.href.
        "/\t/evil.example",  # embedded tab (next=/%09/evil.example)
        "/\n/evil.example",  # embedded LF   (next=/%0A/evil.example)
        "/\r/evil.example",  # embedded CR   (next=/%0D/evil.example)
    ],
)
async def test_login_page_next_rejects_unsafe_redirect_targets(client, malicious):
    r = client.get("/login", params={"next": malicious})
    assert r.status_code == 200
    assert _next_field_value(r.text) == "/"


@pytest.mark.anyio
async def test_login_page_next_accepts_a_legitimate_relative_path(client):
    r = client.get("/login", params={"next": "/review"})
    assert r.status_code == 200
    assert _next_field_value(r.text) == "/review"


@pytest.fixture
def client_closed_registration(pool, monkeypatch):
    from app.main import build_app

    monkeypatch.setenv("DATABASE_URL", TEST_DSN)
    monkeypatch.setenv("DASHBOARD_COOKIE_SECURE", "false")
    monkeypatch.setenv("DASHBOARD_REGISTRATION_OPEN", "false")
    with TestClient(build_app()) as c:
        yield c


REG = {
    "username": "newbie",
    "display_name": "New Bie",
    "email": "newbie@example.org",
    "password": "a long enough passphrase",
}


@pytest.mark.anyio
async def test_registration_creates_pending_account_that_cannot_write(pool, client):
    assert client.post("/api/auth/register", json=REG, headers=CSRF).status_code == 200

    async with pool.connection() as conn:
        cur = await conn.execute("SELECT status, role FROM users WHERE username = %s", ("newbie",))
        assert await cur.fetchone() == ("pending", "viewer")

    client.post(
        "/api/auth/login", json={"username": "newbie", "password": REG["password"]}, headers=CSRF
    )
    # Logged in, but the correction endpoint is closed until approval.
    # This is the test that protects the training set.
    r = client.post("/api/corrections", json={"event_id": 1, "not_a_fish": True}, headers=CSRF)
    assert r.status_code == 403


@pytest.mark.anyio
async def test_duplicate_registration_is_indistinguishable_and_creates_nothing(pool, client):
    first = client.post("/api/auth/register", json=REG, headers=CSRF)
    # Review Focus 2: the UNIQUE constraint must not produce a 500 or a
    # different body — either would be an enumeration oracle.
    same_name = client.post("/api/auth/register", json=REG, headers=CSRF)
    same_email = client.post("/api/auth/register", json={**REG, "username": "other"}, headers=CSRF)
    assert first.status_code == same_name.status_code == same_email.status_code == 200
    assert first.json() == same_name.json() == same_email.json()

    async with pool.connection() as conn:
        cur = await conn.execute("SELECT COUNT(*) FROM users")
        assert (await cur.fetchone())[0] == 1


@pytest.mark.anyio
async def test_registration_rejects_weak_password(pool, client):
    r = client.post("/api/auth/register", json={**REG, "password": "short"}, headers=CSRF)
    assert r.status_code == 400 and "12" in r.json()["error"]


@pytest.mark.anyio
async def test_registration_can_be_closed_by_config(pool, client_closed_registration):
    r = client_closed_registration.post("/api/auth/register", json=REG, headers=CSRF)
    assert r.status_code == 403


@pytest.mark.anyio
async def test_email_verification_marks_the_user_and_is_single_use(pool, client):
    client.post("/api/auth/register", json=REG, headers=CSRF)
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT u.id FROM users u WHERE u.username = 'newbie'")
        uid = (await cur.fetchone())[0]
    from app.auth import create_user_token

    token = await create_user_token(pool, uid, "email_verify", 60, None)

    assert (
        client.post("/api/auth/verify-email", json={"token": token}, headers=CSRF).status_code
        == 200
    )
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT email_verified_at, status FROM users WHERE id = %s", (uid,)
        )
        verified_at, status = await cur.fetchone()
    assert verified_at is not None
    assert status == "pending"  # verification is not approval

    assert (
        client.post("/api/auth/verify-email", json={"token": token}, headers=CSRF).status_code
        == 400
    )


@pytest.mark.anyio
async def test_username_only_collision_sends_no_mail_and_identical_body(pool, client, monkeypatch):
    # Fix round 1, Finding 2: a username-only collision must not email the
    # address the request body carried — that address didn't collide with
    # anything, so telling it "this address already has an account" is
    # false, and doing so at all confirms the username exists through a
    # channel the HTTP response deliberately withholds.
    from app.mailer import Mailer

    calls: list[tuple[str, str]] = []

    def fake_send(self, to, subject, text):
        calls.append((to, subject))
        return "manual"

    monkeypatch.setattr(Mailer, "send", fake_send)

    first = client.post("/api/auth/register", json=REG, headers=CSRF)
    assert first.status_code == 200
    calls_after_create = len(calls)  # the verify-email send for the new account

    same_username = client.post(
        "/api/auth/register",
        json={**REG, "email": "attacker-controlled@example.org"},
        headers=CSRF,
    )
    assert same_username.status_code == 200
    assert same_username.json() == first.json()  # identical body either way

    assert len(calls) == calls_after_create  # no new mail sent
    assert all(to != "attacker-controlled@example.org" for to, _ in calls)


@pytest.mark.anyio
async def test_email_collision_sends_notice_to_existing_address_and_identical_body(
    pool, client, monkeypatch
):
    # Fix round 1, Finding 2: an email collision must notify the address
    # that actually owns the existing account, and the HTTP response must
    # stay identical to every other registration outcome.
    from app.mailer import Mailer

    calls: list[tuple[str, str]] = []

    def fake_send(self, to, subject, text):
        calls.append((to, subject))
        return "manual"

    monkeypatch.setattr(Mailer, "send", fake_send)

    first = client.post("/api/auth/register", json=REG, headers=CSRF)
    assert first.status_code == 200
    calls_after_create = len(calls)

    same_email = client.post(
        "/api/auth/register",
        json={**REG, "username": "other"},
        headers=CSRF,
    )
    assert same_email.status_code == 200
    assert same_email.json() == first.json()  # identical body either way

    new_calls = calls[calls_after_create:]
    assert len(new_calls) == 1
    assert new_calls[0][0] == REG["email"]


@pytest.mark.anyio
async def test_forgot_password_response_is_identical_for_known_and_unknown(pool, client):
    await _make_user(pool, username="dan")
    known = client.post(
        "/api/auth/forgot-password", json={"email": "dan@example.org"}, headers=CSRF
    )
    unknown = client.post(
        "/api/auth/forgot-password", json={"email": "ghost@example.org"}, headers=CSRF
    )
    assert known.status_code == unknown.status_code == 200
    assert known.json() == unknown.json()


@pytest.mark.anyio
async def test_reset_changes_password_and_kills_every_session(pool, client):
    uid = await _make_user(pool, username="dan")
    # Two live sessions, as if signed in on two devices.
    old_a, _ = await create_session(pool, uid, 12, 7, None, None)
    old_b, _ = await create_session(pool, uid, 12, 7, None, None)

    from app.auth import create_user_token

    token = await create_user_token(pool, uid, "password_reset", 30, None)

    r = client.post(
        "/api/auth/reset-password",
        json={"token": token, "password": "a brand new passphrase"},
        headers=CSRF,
    )
    assert r.status_code == 200

    # Both old sessions are dead — the point of the exercise if the reset
    # happened because the account was compromised.
    from app.auth import load_session

    assert await load_session(pool, old_a) is None
    assert await load_session(pool, old_b) is None

    assert (
        client.post(
            "/api/auth/login",
            json={"username": "dan", "password": "a brand new passphrase"},
            headers=CSRF,
        ).status_code
        == 200
    )


@pytest.mark.anyio
async def test_reset_token_cannot_be_reused(pool, client):
    uid = await _make_user(pool, username="dan")
    from app.auth import create_user_token

    token = await create_user_token(pool, uid, "password_reset", 30, None)
    client.post(
        "/api/auth/reset-password",
        json={"token": token, "password": "a brand new passphrase"},
        headers=CSRF,
    )
    again = client.post(
        "/api/auth/reset-password",
        json={"token": token, "password": "yet another passphrase"},
        headers=CSRF,
    )
    assert again.status_code == 400


@pytest.mark.anyio
async def test_reset_enforces_password_policy(pool, client):
    uid = await _make_user(pool, username="dan")
    from app.auth import create_user_token

    token = await create_user_token(pool, uid, "password_reset", 30, None)
    r = client.post(
        "/api/auth/reset-password", json={"token": token, "password": "tiny"}, headers=CSRF
    )
    assert r.status_code == 400
    # A rejected password must not burn the token.
    ok = client.post(
        "/api/auth/reset-password",
        json={"token": token, "password": "a brand new passphrase"},
        headers=CSRF,
    )
    assert ok.status_code == 200


@pytest.mark.anyio
async def test_reset_clears_lockout(pool, client):
    uid = await _make_user(pool, username="dan")
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE users SET failed_logins = 99, "
            "locked_until = NOW() + INTERVAL '1 hour' WHERE id = %s",
            (uid,),
        )
    from app.auth import create_user_token

    token = await create_user_token(pool, uid, "password_reset", 30, None)
    client.post(
        "/api/auth/reset-password",
        json={"token": token, "password": "a brand new passphrase"},
        headers=CSRF,
    )
    assert (
        client.post(
            "/api/auth/login",
            json={"username": "dan", "password": "a brand new passphrase"},
            headers=CSRF,
        ).status_code
        == 200
    )


async def _login_as(pool, client, username, role="admin", status="active"):
    await _make_user(pool, username=username, role=role, status=status)
    client.post(
        "/api/auth/login",
        json={"username": username, "password": "a long enough passphrase"},
        headers=CSRF,
    )


@pytest.mark.anyio
async def test_approving_an_account_emails_its_owner(pool, client, monkeypatch):
    # Owner's request 2026-10-01: with the site behind sign-in, a new member
    # learns they're in from an email, not by signing in to check.
    from app.mailer import Mailer

    calls: list[tuple[str, str, str]] = []

    def fake_send(self, to, subject, text):
        calls.append((to, subject, text))
        return "manual"

    monkeypatch.setattr(Mailer, "send", fake_send)
    assert client.post("/api/auth/register", json=REG, headers=CSRF).status_code == 200
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT id FROM users WHERE username = %s", (REG["username"],))
        target = (await cur.fetchone())[0]
    calls.clear()
    await _login_as(pool, client, "boss")
    r = client.post(f"/api/admin/users/{target}/approve", json={"role": "reviewer"}, headers=CSRF)
    assert r.status_code == 200
    [(to, subject, text)] = calls
    assert to == REG["email"] and "approved" in subject.lower()
    assert "/login" in text and "review site" in text
    # Approving it again does nothing, so it sends nothing.
    calls.clear()
    again = client.post(f"/api/admin/users/{target}/approve", json={"role": "viewer"}, headers=CSRF)
    assert again.status_code == 404 and calls == []


@pytest.mark.anyio
async def test_admin_routes_are_closed_to_reviewers(pool, client):
    await _login_as(pool, client, "rev", role="reviewer")
    assert client.get("/api/admin/users").status_code == 403


@pytest.mark.anyio
async def test_admin_create_user_rejects_weak_password_and_creates_no_row(pool, client):
    # Correction to the brief: POST /api/admin/users must run the same
    # password policy check registration does. Without it an admin-created
    # account can hold a three-character password, silently bypassing the
    # policy every other path enforces.
    await _login_as(pool, client, "boss")
    r = client.post(
        "/api/admin/users",
        json={
            "username": "shortpw",
            "display_name": "Short Pw",
            "email": "shortpw@example.org",
            "password": "short",
            "role": "reviewer",
        },
        headers=CSRF,
    )
    assert r.status_code == 400

    async with pool.connection() as conn:
        cur = await conn.execute("SELECT COUNT(*) FROM users WHERE username = 'shortpw'")
        assert (await cur.fetchone())[0] == 0


@pytest.mark.anyio
async def test_approve_promotes_pending_user_and_writes_audit(pool, client):
    client.post("/api/auth/register", json=REG, headers=CSRF)
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT id FROM users WHERE username = 'newbie'")
        target = (await cur.fetchone())[0]

    client.cookies.clear()
    await _login_as(pool, client, "boss")

    r = client.post(f"/api/admin/users/{target}/approve", json={"role": "reviewer"}, headers=CSRF)
    assert r.status_code == 200

    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT status, role, approved_at, approved_by FROM users WHERE id = %s", (target,)
        )
        status, role, approved_at, approved_by = await cur.fetchone()
        assert (status, role) == ("active", "reviewer")
        assert approved_at is not None and approved_by is not None

        cur = await conn.execute(
            "SELECT action FROM user_audit WHERE target_user_id = %s", (target,)
        )
        assert [r[0] for r in await cur.fetchall()] == ["user.approve"]


@pytest.mark.anyio
async def test_approved_reviewer_can_submit_a_correction(pool, client, tmp_path):
    # The whole point of the feature: approval is what opens the write path.
    client.post("/api/auth/register", json=REG, headers=CSRF)
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT id FROM users WHERE username = 'newbie'")
        target = (await cur.fetchone())[0]
        await conn.execute(
            """INSERT INTO detection_events (id, ts, frame_id, source_name, det_conf,
                                             bbox, topk)
               VALUES (4242, %s, 1, 'seahivecam', 0.9, '{1,2,3,4}', '[]'::jsonb)""",
            (T0,),
        )
        # The form saves through the review rules now, which need the frame.
        image_path = jpeg(tmp_path / "frame.jpg")
        await conn.execute(
            """INSERT INTO saved_frames (ts, frame_id, source_name, reason, image_path,
                                         coco_path, num_fish)
               VALUES (%s, 1, 'seahivecam', 'interesting:high_conf', %s, %s, 1)""",
            (T0, image_path, image_path + ".coco.json"),
        )
    client.cookies.clear()
    await _login_as(pool, client, "boss")
    client.post(f"/api/admin/users/{target}/approve", json={"role": "reviewer"}, headers=CSRF)

    client.cookies.clear()
    client.post(
        "/api/auth/login", json={"username": "newbie", "password": REG["password"]}, headers=CSRF
    )
    r = client.post("/api/corrections", json={"event_id": 4242, "not_a_fish": True}, headers=CSRF)
    assert r.status_code == 200

    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT user_id, session_id FROM detection_corrections WHERE event_id = 4242"
        )
        user_id, session_id = await cur.fetchone()
    assert user_id == target and session_id is not None


@pytest.mark.anyio
async def test_admin_can_revoke_a_session(pool, client):
    uid = await _make_user(pool, username="rev", role="reviewer")
    victim_token, victim_sid = await create_session(pool, uid, 12, 7, None, None)
    await _login_as(pool, client, "boss")
    assert client.post(f"/api/admin/sessions/{victim_sid}/revoke", headers=CSRF).status_code == 200
    from app.auth import load_session

    assert await load_session(pool, victim_token) is None


@pytest.mark.anyio
async def test_admin_revoke_nonexistent_session_returns_404_and_writes_no_audit(pool, client):
    await _login_as(pool, client, "boss")
    r = client.post("/api/admin/sessions/999999999/revoke", headers=CSRF)
    assert r.status_code == 404

    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT COUNT(*) FROM user_audit WHERE action = 'session.revoke' "
            "AND (detail->>'session_id')::bigint = 999999999"
        )
        assert (await cur.fetchone())[0] == 0


@pytest.mark.anyio
async def test_admin_reset_link_exposes_pending_links_to_admins_only(pool, client):
    # C1: with DASHBOARD_MAIL_BACKEND=manual (the default, no relay
    # configured), a reset link minted by /api/auth/forgot-password is
    # unrecoverable — the raw token is deliberately never stored. This
    # admin-only endpoint is the spec's documented recovery path: mint a
    # fresh link and hand it directly to the requesting administrator,
    # the intended out-of-band channel.
    #
    # (Renamed/repointed from a test of the same name that asserted only
    # that a pending user appears in GET /api/admin/users — true, but not
    # what "exposes pending links to admins" actually means. This is.)
    client.post("/api/auth/register", json=REG, headers=CSRF)
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT id FROM users WHERE username = 'newbie'")
        target = (await cur.fetchone())[0]

    client.cookies.clear()
    await _login_as(pool, client, "boss")
    r = client.post(f"/api/admin/users/{target}/reset-link", headers=CSRF)
    assert r.status_code == 200
    link = r.json()["link"]
    assert "token=" in link and "/reset-password" in link

    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT action FROM user_audit WHERE target_user_id = %s", (target,)
        )
        assert "user.reset_link" in [row[0] for row in await cur.fetchall()]

    # Not just "not admin" — a reviewer specifically must not be able to
    # mint this either (already covered structurally by the parametrized
    # admin-mutations guard above; asserted again here so this test is
    # self-contained about what "admins only" in its name means).
    client.cookies.clear()
    await _login_as(pool, client, "rev2", role="reviewer")
    assert client.post(f"/api/admin/users/{target}/reset-link", headers=CSRF).status_code == 403


@pytest.mark.anyio
async def test_admin_reset_link_for_nonexistent_user_returns_404(pool, client):
    await _login_as(pool, client, "boss")
    r = client.post("/api/admin/users/999999999/reset-link", headers=CSRF)
    assert r.status_code == 404


@pytest.mark.anyio
async def test_ack_alert_records_the_authenticated_admins_display_name(pool, client):
    # D4: acknowledged_by must come from the session (user.display_name),
    # never from client input — the route no longer even accepts a
    # ?reviewer= query param.
    async with pool.connection() as conn:
        cur = await conn.execute(
            """INSERT INTO alerts (name, severity, message)
               VALUES ('test_ack_alert_d4', 'warning', 'test')
               ON CONFLICT (name) DO UPDATE
                  SET resolved_at = NULL, acknowledged_by = NULL, acknowledged_at = NULL
               RETURNING id"""
        )
        alert_id = (await cur.fetchone())[0]

    await _login_as(pool, client, "boss")  # _make_user's display_name is username.title()
    r = client.post(f"/api/alerts/{alert_id}/ack", headers=CSRF)
    assert r.status_code == 200
    assert r.json() == {"acknowledged": True}

    async with pool.connection() as conn:
        cur = await conn.execute("SELECT acknowledged_by FROM alerts WHERE id = %s", (alert_id,))
        assert (await cur.fetchone())[0] == "Boss"


# --- Fix round 1: duplicate create and nonexistent-id handling. ---
# Finding 1: create_user must not 500 on a UniqueViolation — the admin caller
# can already list every user, so there is nothing to conceal by returning a
# clean 400 instead of registration's enumeration-safe identical-body dance.
# Finding 2: set_role and deactivate must be consistent with approve() and
# return a clean 404 for a nonexistent user id (via UPDATE ... RETURNING id,
# same race-safe pattern already used by approve()), rather than falling
# through to a user_audit FK violation and a 500.


@pytest.mark.anyio
async def test_admin_create_user_rejects_duplicate_and_creates_no_second_row(pool, client):
    await _login_as(pool, client, "boss")
    payload = {
        "username": "dupuser",
        "display_name": "Dup User",
        "email": "dupuser@example.org",
        "password": "a long enough passphrase",
        "role": "reviewer",
    }
    first = client.post("/api/admin/users", json=payload, headers=CSRF)
    assert first.status_code == 200

    dup = client.post("/api/admin/users", json=payload, headers=CSRF)
    assert dup.status_code == 400

    async with pool.connection() as conn:
        cur = await conn.execute("SELECT COUNT(*) FROM users WHERE username = 'dupuser'")
        assert (await cur.fetchone())[0] == 1


@pytest.mark.anyio
async def test_admin_created_account_defaults_to_viewer_when_role_omitted(pool, client):
    # D2: spec §3 — a new account must never arrive holding write access
    # by default, whoever created it. An admin who omits `role` from the
    # request must get the same 'viewer' floor self-registration gets,
    # not the old 'reviewer' default.
    await _login_as(pool, client, "boss")
    payload = {
        "username": "noroleuser",
        "display_name": "No Role User",
        "email": "norole@example.org",
        "password": "a long enough passphrase",
    }
    r = client.post("/api/admin/users", json=payload, headers=CSRF)
    assert r.status_code == 200

    async with pool.connection() as conn:
        cur = await conn.execute("SELECT role FROM users WHERE username = 'noroleuser'")
        assert (await cur.fetchone())[0] == "viewer"


@pytest.mark.anyio
async def test_admin_set_role_on_nonexistent_user_returns_404_and_writes_no_audit(pool, client):
    await _login_as(pool, client, "boss")
    r = client.post("/api/admin/users/999999999/role", json={"role": "admin"}, headers=CSRF)
    assert r.status_code == 404

    async with pool.connection() as conn:
        cur = await conn.execute("SELECT COUNT(*) FROM user_audit WHERE target_user_id = 999999999")
        assert (await cur.fetchone())[0] == 0


@pytest.mark.anyio
async def test_admin_deactivate_nonexistent_user_returns_404_and_writes_no_audit(pool, client):
    await _login_as(pool, client, "boss")
    r = client.post("/api/admin/users/999999999/deactivate", headers=CSRF)
    assert r.status_code == 404

    async with pool.connection() as conn:
        cur = await conn.execute("SELECT COUNT(*) FROM user_audit WHERE target_user_id = 999999999")
        assert (await cur.fetchone())[0] == 0


@pytest.mark.anyio
async def test_signed_out_only_the_sign_in_pages_answer(pool, client):
    # Reversed 2026-09-30: Wahoo Bay asked for the whole site behind sign-in.
    # tests/test_site_gate.py walks every route; this pins the headline.
    for path in ("/login", "/register", "/forgot-password", "/api/auth/me"):
        assert client.get(path).status_code == 200, path
    for path in ("/", "/api/species_counts", "/api/export/README.md"):
        assert client.get(path).status_code == 401, path


@pytest.mark.anyio
async def test_all_four_legacy_mutation_routes_reject_anonymous(pool, client):
    # The four routes that carried Depends(require_write_token) before
    # this branch retired it (see docs/architecture.md's URL-surface
    # table). All four, not three — a prior version of this test named
    # itself "four" but only asserted three, silently leaving
    # refresh_sheets unpinned.
    assert (
        client.post(
            "/api/corrections", json={"event_id": 1, "not_a_fish": True}, headers=CSRF
        ).status_code
        == 401
    )
    assert client.post("/api/alerts/1/ack", headers=CSRF).status_code == 401
    assert client.post("/api/admin/negative_regions", json={}, headers=CSRF).status_code == 401
    assert client.post("/api/admin/refresh_sheets", headers=CSRF).status_code == 401


# Every admin-only mutation route, each with a body shaped to pass its own
# pydantic model / manual validation — so a reviewer's request fails on the
# role check alone, never on an incidental 422/400 that would let the role
# guard go untested by accident. Path-id routes use ids that don't need to
# exist: the role dependency runs (and must reject) before the handler
# ever looks the id up.
#
# Nine, not eight: this also covers reset-link (C1), an admin-only mutation
# added alongside this fix wave. Leaving a freshly-added admin route out of
# the same regression guard that this fix closes for refresh_sheets above
# would recreate the exact blind spot being fixed.
ADMIN_ONLY_MUTATION_ROUTES = [
    ("/api/alerts/1/ack", None),
    ("/api/admin/negative_regions", {}),
    ("/api/admin/refresh_sheets", None),
    (
        "/api/admin/users",
        {
            "username": "paramuser",
            "display_name": "Param User",
            "email": "paramuser@example.org",
            "password": "a long enough passphrase",
            "role": "viewer",
        },
    ),
    ("/api/admin/users/1/approve", {"role": "viewer"}),
    ("/api/admin/users/1/role", {"role": "viewer"}),
    ("/api/admin/users/1/deactivate", None),
    ("/api/admin/sessions/1/revoke", None),
    ("/api/admin/users/1/reset-link", None),
]


@pytest.mark.anyio
@pytest.mark.parametrize("path,body", ADMIN_ONLY_MUTATION_ROUTES)
async def test_reviewer_cannot_reach_admin_only_mutations(pool, client, path, body):
    await _login_as(pool, client, "rev", role="reviewer")
    kwargs = {"headers": CSRF}
    if body is not None:
        kwargs["json"] = body
    assert client.post(path, **kwargs).status_code == 403


@pytest.mark.anyio
async def test_review_route_requires_reviewer_and_redirects_anonymous(pool, client):
    # TestClient's default Accept header is "*/*" (no "text/html"), unlike
    # a real browser navigating to the page — set it explicitly so the 401
    # handler's content-negotiation branch takes the redirect path instead
    # of the JSON-API path.
    r = client.get("/review", follow_redirects=False, headers={"Accept": "text/html"})
    assert r.status_code in (302, 307)
    assert "/login" in r.headers["location"]

    await _login_as(pool, client, "rev", role="reviewer")
    assert client.get("/review").status_code == 200


def test_write_token_machinery_is_gone():
    src = (
        Path(__file__).resolve().parents[1] / "services" / "dashboard" / "app" / "main.py"
    ).read_text()
    assert "require_write_token" not in src
    assert "DASHBOARD_WRITE_TOKEN" not in src


@pytest.mark.anyio
async def test_forgot_password_is_rate_limited_per_ip(pool, monkeypatch):
    # /api/auth/forgot-password is the one unauthenticated endpoint that
    # makes the server send mail to an address the CALLER chooses. Without
    # a cap, anyone who finds the public URL can mail-bomb a victim and
    # burn the sending domain's reputation. Small distinct budget so the
    # test needs three requests rather than eleven.
    from app.main import build_app

    monkeypatch.setenv("DATABASE_URL", TEST_DSN)
    monkeypatch.setenv("DASHBOARD_COOKIE_SECURE", "false")
    monkeypatch.setenv("DASHBOARD_FORGOT_MAX_PER_IP_H", "2")

    await _make_user(pool, username="dan")

    # TestClient's default client tuple is ("testclient", 50000), which
    # _client_ip() cannot parse as an address — the limiter would no-op.
    with TestClient(build_app(), client=("203.0.113.9", 51000)) as c:
        body = {"email": "dan@example.org"}
        first = c.post("/api/auth/forgot-password", json=body, headers=CSRF)
        second = c.post("/api/auth/forgot-password", json=body, headers=CSRF)
        third = c.post("/api/auth/forgot-password", json=body, headers=CSRF)

    assert first.status_code == 200
    assert second.status_code == 200
    assert third.status_code == 429

    # The cap must not become an enumeration side channel: it is keyed on
    # the caller's IP, so an unknown address is throttled identically.
    with TestClient(build_app(), client=("203.0.113.10", 51000)) as c:
        ghost = {"email": "nobody-here@example.org"}
        for _ in range(2):
            c.post("/api/auth/forgot-password", json=ghost, headers=CSRF)
        assert c.post("/api/auth/forgot-password", json=ghost, headers=CSRF).status_code == 429


@pytest.mark.anyio
async def test_forgot_password_message_is_honest_about_the_manual_backend(pool, client):
    # conftest pins the mail backend to `manual`, where nothing is sent.
    # Telling a user "a reset link is on its way" would leave them waiting
    # on mail that never arrives, with no hint that an administrator is the
    # actual channel. The message must still be identical for every address
    # so it cannot reveal whether an account exists.
    await _make_user(pool, username="dan")

    known = client.post(
        "/api/auth/forgot-password", json={"email": "dan@example.org"}, headers=CSRF
    )
    unknown = client.post(
        "/api/auth/forgot-password", json={"email": "ghost@example.org"}, headers=CSRF
    )

    assert known.status_code == unknown.status_code == 200
    assert known.json() == unknown.json()

    message = known.json()["message"]
    assert "on its way" not in message
    assert "administrator" in message.lower()


@pytest.mark.anyio
async def test_forgot_password_message_says_the_link_is_on_its_way_with_ses(pool, monkeypatch):
    """2026-10-27, staging: the mail went out through SES and the page still said "Automatic email is
    not enabled on this deployment", because the wording only recognised the smtp backend. Any backend
    that actually sends gets the honest message; it stays identical for known and unknown addresses."""
    import boto3

    from app.main import build_app

    class _Sent:
        def __init__(self, service, **kwargs):
            assert service == "sesv2"

        def send_email(self, **kwargs):
            return {"MessageId": "0100-fake"}

    monkeypatch.setattr(boto3, "client", _Sent)
    monkeypatch.setenv("DATABASE_URL", TEST_DSN)
    monkeypatch.setenv("DASHBOARD_COOKIE_SECURE", "false")
    monkeypatch.setenv("DASHBOARD_MAIL_BACKEND", "ses")
    monkeypatch.setenv("MAIL_BACKEND", "ses")
    monkeypatch.setenv("MAIL_FROM", "no-reply@wahoobay.net")
    await _make_user(pool, username="dan")

    with TestClient(build_app()) as c:
        known = c.post("/api/auth/forgot-password", json={"email": "dan@example.org"}, headers=CSRF)
        unknown = c.post("/api/auth/forgot-password", json={"email": "ghost@example.org"}, headers=CSRF)

    assert known.status_code == unknown.status_code == 200
    assert known.json() == unknown.json()
    message = known.json()["message"]
    assert "on its way" in message
    assert "not enabled" not in message

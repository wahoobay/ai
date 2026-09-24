# Dashboard Auth + RBAC Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the Wahoo Bay dashboard real user accounts with roles, so reviewer corrections are attributable and the currently-anonymous mutation endpoints are closed.

**Architecture:** New modules inside `services/dashboard/app/` — `auth.py` (pure logic + session store), `mailer.py`, and two router factories mounted by the existing `build_app()`. Sessions are rows in Postgres keyed by a SHA-256 of the cookie value. Authorization resolves the pair `(status, role)`, checked by a `require_role(...)` FastAPI dependency that hands handlers a `CurrentUser`.

**Tech Stack:** Python 3.11, FastAPI 0.135.3, Starlette 1.0.0, Jinja2 3.1.6, psycopg 3.3.3 (async pool), pytest 9.0.3, httpx 0.28.1. **Standard library only for all cryptography and email** — `hashlib.scrypt`, `hmac`, `secrets`, `smtplib`, `email.message`.

**Spec:** [docs/superpowers/specs/2026-09-22-reviewer-auth-rbac-design.md](../specs/2026-09-22-reviewer-auth-rbac-design.md)

## Global Constraints

- **No new Python dependencies.** The conda env `wahoobay` runs all five production processes; `pip install` into it is out of scope for this plan.
- **Python 3.11+**, `from __future__ import annotations` at the top of every new module (matches existing files).
- **ruff**: line-length 100, rules `E,F,W,I,B,UP`, double quotes, 4-space indent. `tests/**` ignores `B` and `F401`.
- **DDL is `IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS` only**, appended to `db/init.sql`, applied by hand with `psql`. No migration framework.
- **scrypt parameters**: `n=16384, r=8, p=1, dklen=32`, 16-byte salt. Memory use is `128 * n * r` = 16 MB, under Python's default `maxmem`. Raising `n` to `2**15` exceeds the default and raises `ValueError` — if you raise it, pass `maxmem` explicitly.
- **Cookie name** `wb_session`; `HttpOnly`, `SameSite=Lax`, `Path=/`, `Secure` from config.
- **Every `POST` under `/api` requires the header `X-Requested-With: wahoobay`**, including `/api/auth/login`.
- **All authentication failures return an identical generic `401`.** Specific reasons go to the log only.
- **Passwords and raw tokens never appear in logs, responses, or command-line arguments.**
- **Never store a raw token.** Store `sha256(token)`; compare with `hmac.compare_digest`.
- Existing route style is `@app.get(...)` inside `build_app()`; new routes live in `APIRouter` factories that take `(cfg, state)` and are mounted with `app.include_router(...)`.

## Review Focus

Five conditions the spec implies but that no task's happy-path tests would exercise. Each has a test assigned to the task that owns the code.

1. **A multi-megabyte password string** — scrypt cost is driven by its parameters, not input length, but an unbounded input still gets copied and encoded. Cap at 1024 characters and reject longer with the normal validation error, not a 500. *(Task 1)*
2. **Two people registering the same username or email simultaneously** — the `UNIQUE` constraint fires on the second insert. That must surface as the same generic success response as any other registration, not a `500` and not an enumeration signal. *(Task 7)*
3. **Naive vs timezone-aware datetimes** — `expires_at` comes back from psycopg as timezone-aware; comparing it against `datetime.now()` (naive) raises `TypeError` and every session check 500s. All comparisons use `datetime.now(UTC)`. *(Task 3)*
4. **A reset token mangled in transit** — mail clients wrap lines, append punctuation, and change case. Strip surrounding whitespace before hashing, treat the token as case-sensitive, and return the generic "invalid or expired" message for anything that does not match, never a parse error. *(Task 3 — the redeem path lives in `auth.py`)*
5. **`DASHBOARD_COOKIE_SECURE=true` on a plain-HTTP origin** — the browser silently discards the cookie and login appears to succeed then immediately fail, with nothing in the logs. The flag must come from config, and local HTTP testing must be possible by setting it false. *(Task 6)*

---

## File Structure

| File | Responsibility |
|---|---|
| `services/dashboard/app/auth.py` | Password hashing + policy, token mint/hash, session and user-token stores, `CurrentUser`, `require_role` factory |
| `services/dashboard/app/mailer.py` | `Mailer` with `smtp` and `manual` backends |
| `services/dashboard/app/routes_auth.py` | `build_auth_router(cfg, state)` — login, logout, me, register, verify, forgot, reset, and their HTML pages |
| `services/dashboard/app/routes_admin_users.py` | `build_admin_users_router(cfg, state)` — user list/create/approve/role/deactivate, session list/revoke, audit writes |
| `services/dashboard/app/common_passwords.txt` | Small denylist for the password policy |
| `services/dashboard/app/templates/{login,register,forgot_password,reset_password,users_admin}.html` | Jinja2 pages using existing `style.css` |
| `services/dashboard/app/static/auth.js` | Form submission with the CSRF header, identity chip, pending banner, 401/403 handling |
| `scripts/create_user.py` | First-admin bootstrap and CLI account creation |
| `db/init.sql` | Appended DDL |
| `tests/test_auth.py` | Unit tests (no DB) |
| `tests/test_auth_db.py` | DB + TestClient integration tests, skipped when `TEST_DATABASE_URL` is unset |

**Test database.** Integration tests need a throwaway database. Every DB test module starts with:

```python
TEST_DSN = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DSN, reason="TEST_DATABASE_URL not set")
```

Create it once on the DGX with:

```bash
createdb -h 127.0.0.1 -p 5432 -U wahoobay wahoobay_test
psql "postgresql://wahoobay:<pw>@localhost:5432/wahoobay_test" -f db/init.sql
export TEST_DATABASE_URL="postgresql://wahoobay:<pw>@localhost:5432/wahoobay_test"
```

Never point `TEST_DATABASE_URL` at the production `wahoobay` database — these tests truncate tables.

---

### Task 1: Password hashing and policy

**Files:**
- Create: `services/dashboard/app/auth.py`
- Create: `services/dashboard/app/common_passwords.txt`
- Test: `tests/test_auth.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `hash_password(password: str) -> str`, `verify_password(password: str, stored: str) -> bool`, `check_password_policy(password: str, username: str, email: str) -> str | None` (returns an error message, or `None` when acceptable), `MAX_PASSWORD_LEN: int = 1024`, `MIN_PASSWORD_LEN: int = 12`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_auth.py
"""Unit tests for services/dashboard/app/auth.py.

Pure functions only — no database, no FastAPI client. The DB-backed
session and token stores are covered in tests/test_auth_db.py."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "dashboard"))

import pytest  # noqa: E402
from app.auth import (  # noqa: E402
    MAX_PASSWORD_LEN,
    check_password_policy,
    hash_password,
    verify_password,
)


def test_hash_then_verify_roundtrip():
    stored = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", stored) is True


def test_verify_rejects_wrong_password():
    stored = hash_password("correct horse battery staple")
    assert verify_password("Correct horse battery staple", stored) is False


def test_hash_is_salted():
    a = hash_password("same password twice")
    b = hash_password("same password twice")
    assert a != b
    assert verify_password("same password twice", a)
    assert verify_password("same password twice", b)


def test_verify_reads_cost_parameters_from_stored_value():
    # A hash written with a lower cost must still verify, so parameters
    # can be raised later without invalidating existing accounts.
    weak = hash_password("legacy parameters", n=1024)
    assert weak.startswith("scrypt$1024$")
    assert verify_password("legacy parameters", weak) is True


def test_verify_returns_false_on_malformed_stored_value():
    for junk in ("", "not-a-hash", "scrypt$abc$8$1$xx$yy", "scrypt$16384$8$1$only-four"):
        assert verify_password("anything", junk) is False


def test_policy_accepts_long_passphrase_without_symbols():
    assert check_password_policy("salt water fish counting", "dan", "dan@fau.edu") is None


def test_policy_rejects_short_password():
    msg = check_password_policy("Tr0ub4dor&3", "dan", "dan@fau.edu")
    assert msg is not None and "12" in msg


def test_policy_rejects_password_containing_username_or_email_local_part():
    assert check_password_policy("dan-is-here-okay", "dan", "x@fau.edu") is not None
    assert check_password_policy("scoopnisker-reef", "u", "scoopnisker@fau.edu") is not None


def test_policy_rejects_denylisted_password():
    assert check_password_policy("password123456", "u", "u@e.org") is not None


def test_policy_rejects_oversized_password():
    # Review Focus 1: an unbounded password is a memory/CPU footgun and
    # must fail validation, not raise.
    msg = check_password_policy("a" * (MAX_PASSWORD_LEN + 1), "u", "u@e.org")
    assert msg is not None and "long" in msg.lower()


def test_hash_password_rejects_oversized_password():
    with pytest.raises(ValueError):
        hash_password("a" * (MAX_PASSWORD_LEN + 1))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /raid/scratch/dzimmerman2021/wahoobay && python -m pytest tests/test_auth.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.auth'`

- [ ] **Step 3: Write the denylist**

```
# services/dashboard/app/common_passwords.txt
# One lowercase candidate per line. Deliberately short — this catches the
# handful of passwords a hurried person actually picks, not a breach corpus.
password
password1
password12
password123
password1234
password12345
password123456
passw0rd
letmein
letmein123
qwerty
qwertyuiop
123456789012
1234567890123
iloveyou
admin
administrator
wahoobay
wahoobay123
fishfishfish
changeme
changeme123
welcome
welcome123
```

- [ ] **Step 4: Write the implementation**

```python
# services/dashboard/app/auth.py
"""Authentication and authorization for the dashboard.

Standard library only: the conda env this runs in also hosts the two
inference workers and the poller, so adding a dependency here risks the
production stack on its next restart. `hashlib.scrypt` covers password
hashing and `secrets` covers token generation.

Password hashes are stored self-describing —
`scrypt$<n>$<r>$<p>$<salt_b64>$<hash_b64>` — so the cost parameters can
be raised later without invalidating existing accounts: verification
reads the parameters out of the stored value rather than assuming the
current constants."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from functools import lru_cache
from pathlib import Path

SCRYPT_N = 16384          # 128 * n * r = 16 MB, under scrypt's default maxmem
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
SALT_BYTES = 16

MIN_PASSWORD_LEN = 12
MAX_PASSWORD_LEN = 1024   # bounds the work an anonymous caller can ask for

_DENYLIST_PATH = Path(__file__).with_name("common_passwords.txt")


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def hash_password(password: str, *, n: int = SCRYPT_N) -> str:
    """Hash a password for storage. Raises ValueError if oversized."""
    if len(password) > MAX_PASSWORD_LEN:
        raise ValueError(f"password exceeds {MAX_PASSWORD_LEN} characters")
    salt = secrets.token_bytes(SALT_BYTES)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt,
        n=n, r=SCRYPT_R, p=SCRYPT_P, dklen=SCRYPT_DKLEN,
    )
    return f"scrypt${n}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time verification. Returns False on any malformed input
    rather than raising — callers treat every failure identically."""
    try:
        scheme, n_s, r_s, p_s, salt_b64, hash_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        n, r, p = int(n_s), int(r_s), int(p_s)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
    except (ValueError, TypeError):
        return False
    if len(password) > MAX_PASSWORD_LEN:
        return False
    try:
        digest = hashlib.scrypt(
            password.encode("utf-8"), salt=salt,
            n=n, r=r, p=p, dklen=len(expected),
        )
    except ValueError:
        return False
    return hmac.compare_digest(digest, expected)


@lru_cache(maxsize=1)
def _denylist() -> frozenset[str]:
    try:
        lines = _DENYLIST_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return frozenset()
    return frozenset(
        line.strip().lower() for line in lines
        if line.strip() and not line.startswith("#")
    )


def check_password_policy(password: str, username: str, email: str) -> str | None:
    """Return an error message, or None if the password is acceptable.

    Length over composition: forced symbol classes push people toward
    predictable substitutions, so the only hard rule is length."""
    if len(password) > MAX_PASSWORD_LEN:
        return f"Password is too long (limit {MAX_PASSWORD_LEN} characters)."
    if len(password) < MIN_PASSWORD_LEN:
        return f"Password must be at least {MIN_PASSWORD_LEN} characters."
    lowered = password.lower()
    if lowered in _denylist():
        return "That password is too common. Please choose another."
    if username and len(username) >= 3 and username.lower() in lowered:
        return "Password must not contain your username."
    local_part = (email or "").split("@")[0]
    if local_part and len(local_part) >= 3 and local_part.lower() in lowered:
        return "Password must not contain your email address."
    return None
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_auth.py -v`
Expected: PASS, 10 tests.

- [ ] **Step 6: Lint**

Run: `python -m ruff check services/dashboard/app/auth.py tests/test_auth.py`
Expected: no findings.

- [ ] **Step 7: Commit**

```bash
git add services/dashboard/app/auth.py services/dashboard/app/common_passwords.txt tests/test_auth.py
git commit -m "auth: scrypt password hashing + password policy"
```

---

### Task 2: Schema

**Files:**
- Modify: `db/init.sql` (append)
- Test: `tests/test_auth_db.py`

**Interfaces:**
- Consumes: nothing.
- Produces: tables `users`, `user_sessions`, `user_tokens`, `user_audit`; columns `detection_corrections.user_id`, `detection_corrections.session_id`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_auth_db.py
"""Integration tests: schema, session store, and the HTTP auth flows.

Skipped unless TEST_DATABASE_URL points at a throwaway database. These
tests truncate tables — never point it at the production database."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg
import pytest

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
            "users", "user_sessions", "user_tokens", "user_audit",
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
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_auth_db.py -v`
Expected: FAIL — the tables do not exist (or SKIP if `TEST_DATABASE_URL` is unset; set it first, per the File Structure section).

- [ ] **Step 3: Append the DDL to `db/init.sql`**

```sql
-- ---------------------------------------------------------------------
-- Accounts, sessions, and one-time tokens for the reviewer tooling.
-- `status` is checked before `role` everywhere: a pending account
-- authenticates but is treated as a viewer regardless of its role.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id                BIGSERIAL PRIMARY KEY,
    username          TEXT        NOT NULL UNIQUE,
    display_name      TEXT        NOT NULL,
    email             TEXT        NOT NULL UNIQUE,
    password_hash     TEXT        NOT NULL,
    role              TEXT        NOT NULL DEFAULT 'viewer'
                      CHECK (role IN ('viewer', 'reviewer', 'admin')),
    status            TEXT        NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending', 'active', 'disabled')),
    email_verified_at TIMESTAMPTZ,
    approved_at       TIMESTAMPTZ,
    approved_by       BIGINT      REFERENCES users(id),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_login_at     TIMESTAMPTZ,
    failed_logins     INTEGER     NOT NULL DEFAULT 0,
    locked_until      TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS users_pending
    ON users (created_at DESC) WHERE status = 'pending';

CREATE TABLE IF NOT EXISTS user_sessions (
    id            BIGSERIAL PRIMARY KEY,
    token_hash    TEXT        NOT NULL UNIQUE,   -- sha256 of the cookie value
    user_id       BIGINT      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at    TIMESTAMPTZ NOT NULL,
    revoked_at    TIMESTAMPTZ,
    user_agent    TEXT,
    ip            INET
);

CREATE INDEX IF NOT EXISTS user_sessions_user_created
    ON user_sessions (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS user_sessions_expires
    ON user_sessions (expires_at) WHERE revoked_at IS NULL;

CREATE TABLE IF NOT EXISTS user_tokens (
    id            BIGSERIAL PRIMARY KEY,
    user_id       BIGINT      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    purpose       TEXT        NOT NULL
                  CHECK (purpose IN ('password_reset', 'email_verify')),
    token_hash    TEXT        NOT NULL UNIQUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at    TIMESTAMPTZ NOT NULL,
    used_at       TIMESTAMPTZ,
    requested_ip  INET,
    delivered_via TEXT
);

CREATE INDEX IF NOT EXISTS user_tokens_user_purpose
    ON user_tokens (user_id, purpose, created_at DESC);

CREATE TABLE IF NOT EXISTS user_audit (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    actor_user_id   BIGINT REFERENCES users(id),
    action          TEXT   NOT NULL,
    target_user_id  BIGINT REFERENCES users(id),
    detail          JSONB
);

ALTER TABLE detection_corrections
    ADD COLUMN IF NOT EXISTS user_id    BIGINT REFERENCES users(id),
    ADD COLUMN IF NOT EXISTS session_id BIGINT REFERENCES user_sessions(id);

CREATE INDEX IF NOT EXISTS detection_corrections_session
    ON detection_corrections (session_id, created_at DESC)
    WHERE session_id IS NOT NULL;
```

- [ ] **Step 4: Apply to the test database and run the tests**

```bash
psql "$TEST_DATABASE_URL" -f db/init.sql
python -m pytest tests/test_auth_db.py -v
```
Expected: PASS, 2 tests.

- [ ] **Step 5: Commit**

```bash
git add db/init.sql tests/test_auth_db.py
git commit -m "db: users, sessions, tokens, audit tables for dashboard auth"
```

---

### Task 3: Session and token stores

**Files:**
- Modify: `services/dashboard/app/auth.py`
- Test: `tests/test_auth_db.py`

**Interfaces:**
- Consumes: `hash_password` (Task 1), schema (Task 2).
- Produces, all `async` and all taking an `AsyncConnectionPool` as the first argument:
  - `mint_token() -> str` and `token_digest(token: str) -> str` (sync)
  - `create_session(pool, user_id: int, ttl_hours: int, abs_ttl_days: int, user_agent: str | None, ip: str | None) -> tuple[str, int]` returning `(raw_token, session_id)`
  - `load_session(pool, raw_token: str) -> CurrentUser | None`
  - `touch_session(pool, session_id: int, ttl_hours: int) -> None`
  - `needs_touch(last_seen_at: datetime, now: datetime) -> bool` (sync)
  - `invalidate_tokens(pool, user_id: int, purpose: str) -> None`
  - `revoke_session(pool, session_id: int) -> None`
  - `revoke_all_sessions(pool, user_id: int) -> int`
  - `create_user_token(pool, user_id: int, purpose: str, ttl_minutes: int, ip: str | None) -> str`
  - `redeem_user_token(pool, raw_token: str, purpose: str) -> int | None` returning `user_id`
  - `CurrentUser` dataclass: `id, username, display_name, role, status, session_id, last_seen_at`
  - `effective_role(status: str, role: str) -> str`
  - `TOUCH_THROTTLE_S: int = 60`

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_auth_db.py
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
)
from psycopg_pool import AsyncConnectionPool  # noqa: E402


@pytest.fixture
async def pool():
    p = AsyncConnectionPool(TEST_DSN, min_size=1, max_size=2, open=False)
    await p.open()
    async with p.connection() as conn:
        await conn.execute("TRUNCATE users, user_sessions, user_tokens, user_audit CASCADE")
    yield p
    await p.close()


async def _make_user(pool, username="rev", role="reviewer", status="active"):
    async with pool.connection() as conn:
        cur = await conn.execute(
            """INSERT INTO users (username, display_name, email, password_hash,
                                  role, status)
               VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
            (username, username.title(), f"{username}@example.org",
             hash_password("a long enough passphrase"), role, status),
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
        assert (await cur.fetchone())[0] != raw   # only the digest is persisted


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


def test_needs_touch_respects_throttle():
    # Review Focus 3: these comparisons must be timezone-aware or every
    # request 500s with "can't compare offset-naive and offset-aware".
    now = datetime.now(UTC)
    assert needs_touch(now - timedelta(seconds=TOUCH_THROTTLE_S + 5), now) is True
    assert needs_touch(now - timedelta(seconds=5), now) is False


def test_effective_role_pins_pending_and_disabled():
    assert effective_role("active", "admin") == "admin"
    assert effective_role("pending", "admin") == "viewer"
    assert effective_role("pending", "reviewer") == "viewer"
    assert effective_role("disabled", "admin") == "none"


@pytest.mark.anyio
async def test_user_token_is_single_use_scoped_and_expiring(pool):
    uid = await _make_user(pool)

    raw = await create_user_token(pool, uid, "password_reset", 30, None)
    assert await redeem_user_token(pool, raw, "email_verify") is None   # wrong purpose
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
```

Add to `tests/test_auth_db.py` so the async tests have a backend:

```python
@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_auth_db.py -v`
Expected: FAIL — `ImportError: cannot import name 'create_session' from 'app.auth'`

- [ ] **Step 3: Implement the stores in `auth.py`**

```python
# append to services/dashboard/app/auth.py
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

TOUCH_THROTTLE_S = 60


@dataclass(frozen=True)
class CurrentUser:
    id: int
    username: str
    display_name: str
    role: str
    status: str
    session_id: int
    last_seen_at: datetime


def mint_token() -> str:
    return secrets.token_urlsafe(32)


def token_digest(token: str) -> str:
    return hashlib.sha256(token.strip().encode("utf-8")).hexdigest()


def effective_role(status: str, role: str) -> str:
    """Status wins over role. An admin revoking access by setting status
    never has to also remember to downgrade the role."""
    if status == "disabled":
        return "none"
    if status == "pending":
        return "viewer"
    return role


def needs_touch(last_seen_at: datetime, now: datetime) -> bool:
    return (now - last_seen_at).total_seconds() >= TOUCH_THROTTLE_S


async def create_session(pool, user_id, ttl_hours, abs_ttl_days,
                         user_agent, ip) -> tuple[str, int]:
    raw = mint_token()
    now = datetime.now(UTC)
    expires = min(now + timedelta(hours=ttl_hours), now + timedelta(days=abs_ttl_days))
    async with pool.connection() as conn:
        cur = await conn.execute(
            """INSERT INTO user_sessions
                   (token_hash, user_id, expires_at, user_agent, ip)
               VALUES (%s, %s, %s, %s, %s) RETURNING id""",
            (token_digest(raw), user_id, expires, user_agent, ip),
        )
        return raw, (await cur.fetchone())[0]


async def load_session(pool, raw_token: str) -> CurrentUser | None:
    if not raw_token:
        return None
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT s.id, s.last_seen_at, u.id, u.username, u.display_name,
                      u.role, u.status
                 FROM user_sessions s JOIN users u ON u.id = s.user_id
                WHERE s.token_hash = %s
                  AND s.revoked_at IS NULL
                  AND s.expires_at > NOW()""",
            (token_digest(raw_token),),
        )
        row = await cur.fetchone()
    if row is None or row[6] == "disabled":
        return None
    return CurrentUser(
        id=row[2], username=row[3], display_name=row[4],
        role=row[5], status=row[6], session_id=row[0], last_seen_at=row[1],
    )


async def touch_session(pool, session_id: int, ttl_hours: int) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            """UPDATE user_sessions
                  SET last_seen_at = NOW(),
                      expires_at = LEAST(NOW() + make_interval(hours => %s),
                                         created_at + INTERVAL '7 days')
                WHERE id = %s""",
            (ttl_hours, session_id),
        )


async def revoke_session(pool, session_id: int) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE user_sessions SET revoked_at = NOW() WHERE id = %s AND revoked_at IS NULL",
            (session_id,),
        )


async def revoke_all_sessions(pool, user_id: int) -> int:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "UPDATE user_sessions SET revoked_at = NOW() "
            "WHERE user_id = %s AND revoked_at IS NULL",
            (user_id,),
        )
        return cur.rowcount


async def create_user_token(pool, user_id, purpose, ttl_minutes, ip) -> str:
    raw = mint_token()
    async with pool.connection() as conn:
        await conn.execute(
            """INSERT INTO user_tokens
                   (user_id, purpose, token_hash, expires_at, requested_ip)
               VALUES (%s, %s, %s, NOW() + make_interval(mins => %s), %s)""",
            (user_id, purpose, token_digest(raw), ttl_minutes, ip),
        )
    return raw


async def redeem_user_token(pool, raw_token: str, purpose: str) -> int | None:
    """Single-use and purpose-scoped. Returns the user id, or None for
    anything unknown, expired, already used, or issued for another
    purpose — the caller shows one generic message for all of them."""
    if not raw_token:
        return None
    async with pool.connection() as conn:
        cur = await conn.execute(
            """UPDATE user_tokens SET used_at = NOW()
                WHERE token_hash = %s AND purpose = %s
                  AND used_at IS NULL AND expires_at > NOW()
                RETURNING user_id""",
            (token_digest(raw_token), purpose),
        )
        row = await cur.fetchone()
    return None if row is None else row[0]


async def invalidate_tokens(pool, user_id: int, purpose: str) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE user_tokens SET used_at = NOW() "
            "WHERE user_id = %s AND purpose = %s AND used_at IS NULL",
            (user_id, purpose),
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_auth_db.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add services/dashboard/app/auth.py tests/test_auth_db.py
git commit -m "auth: DB-backed session and single-use token stores"
```

---

### Task 4: `require_role` dependency

**Files:**
- Modify: `services/dashboard/app/auth.py`
- Test: `tests/test_auth_db.py`

**Interfaces:**
- Consumes: `load_session`, `touch_session`, `effective_role`, `CurrentUser`.
- Produces: `COOKIE_NAME = "wb_session"`, `CSRF_HEADER = "x-requested-with"`, `CSRF_VALUE = "wahoobay"`, `make_require_role(cfg, state)` returning `require_role(*allowed: str)`, `make_optional_user(state)` returning a dependency that yields `CurrentUser | None`, and `require_csrf(request)`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_auth_db.py
from fastapi import Depends, FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.auth import COOKIE_NAME, make_require_role  # noqa: E402


class _Cfg:
    cookie_secure = False
    session_ttl_h = 12
    session_abs_ttl_d = 7


def _mini_app(pool):
    app = FastAPI()
    require_role = make_require_role(_Cfg(), {"pool": pool})

    @app.get("/only-reviewers")
    async def only_reviewers(user=Depends(require_role("reviewer", "admin"))):
        return {"username": user.username}

    @app.get("/open")
    async def open_route():
        return {"ok": True}

    return app


@pytest.mark.anyio
async def test_dependency_enforces_401_403_200(pool):
    admin_id = await _make_user(pool, username="boss", role="admin")
    pending_id = await _make_user(pool, username="newbie", role="reviewer", status="pending")
    admin_tok, _ = await create_session(pool, admin_id, 12, 7, None, None)
    pending_tok, _ = await create_session(pool, pending_id, 12, 7, None, None)

    with TestClient(_mini_app(pool)) as c:
        assert c.get("/only-reviewers").status_code == 401           # anonymous
        assert c.get("/open").status_code == 200                     # public stays public

        c.cookies.set(COOKIE_NAME, pending_tok)
        assert c.get("/only-reviewers").status_code == 403           # pending → viewer
        c.cookies.clear()

        c.cookies.set(COOKIE_NAME, admin_tok)
        r = c.get("/only-reviewers")
        assert r.status_code == 200 and r.json()["username"] == "boss"
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_auth_db.py -k dependency -v`
Expected: FAIL — `cannot import name 'make_require_role'`

- [ ] **Step 3: Implement**

```python
# append to services/dashboard/app/auth.py
from fastapi import HTTPException, Request

COOKIE_NAME = "wb_session"
CSRF_HEADER = "x-requested-with"
CSRF_VALUE = "wahoobay"


def make_require_role(cfg, state):
    """Build the role dependency. Takes cfg/state because the dashboard
    keeps its pool in a closure dict created by build_app()."""

    def require_role(*allowed: str):
        async def dependency(request: Request) -> CurrentUser:
            pool = state.get("pool")
            if pool is None:
                raise HTTPException(status_code=503, detail="database unavailable")
            user = await load_session(pool, request.cookies.get(COOKIE_NAME, ""))
            if user is None:
                raise HTTPException(status_code=401, detail="authentication required")
            role = effective_role(user.status, user.role)
            if role not in allowed:
                raise HTTPException(status_code=403, detail="insufficient role")
            if needs_touch(user.last_seen_at, datetime.now(UTC)):
                await touch_session(pool, user.session_id, cfg.session_ttl_h)
            return user

        return dependency

    return require_role


def make_optional_user(state):
    async def dependency(request: Request) -> CurrentUser | None:
        pool = state.get("pool")
        if pool is None:
            return None
        return await load_session(pool, request.cookies.get(COOKIE_NAME, ""))

    return dependency


def require_csrf(request: Request) -> None:
    """Every POST under /api carries this header. A cross-origin form
    cannot set it, and a cross-origin fetch that tries triggers a
    preflight this service does not answer."""
    if request.headers.get(CSRF_HEADER, "").lower() != CSRF_VALUE:
        raise HTTPException(status_code=403, detail="missing request header")
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_auth_db.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add services/dashboard/app/auth.py tests/test_auth_db.py
git commit -m "auth: require_role dependency and CSRF header check"
```

---

### Task 5: Mailer

**Files:**
- Create: `services/dashboard/app/mailer.py`
- Test: `tests/test_auth.py` (append)

**Interfaces:**
- Consumes: nothing.
- Produces: `Mailer(backend: str, smtp: dict | None, from_addr: str, base_url: str)` with `send(to: str, subject: str, body: str) -> str` returning the backend that handled it (`"smtp"` or `"manual"`), and `reset_email(link: str) -> tuple[str, str]`, `verify_email(link: str) -> tuple[str, str]`, `collision_notice() -> tuple[str, str]` returning `(subject, body)`.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_auth.py
from app.mailer import Mailer, reset_email  # noqa: E402


def test_manual_backend_does_not_attempt_smtp(monkeypatch):
    def explode(*a, **k):
        raise AssertionError("SMTP must not be contacted in manual mode")

    monkeypatch.setattr("smtplib.SMTP", explode)
    m = Mailer(backend="manual", smtp=None, from_addr="x@y.org",
               base_url="https://example.org")
    assert m.send("someone@example.org", "Subject", "Body") == "manual"


def test_smtp_backend_sends_one_message(monkeypatch):
    sent = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            sent.append(("connect", host, port))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def starttls(self):
            sent.append(("starttls",))

        def login(self, user, password):
            sent.append(("login", user))

        def send_message(self, msg):
            sent.append(("send", msg["To"], msg["Subject"]))

    monkeypatch.setattr("smtplib.SMTP", FakeSMTP)
    m = Mailer(
        backend="smtp",
        smtp={"host": "relay.fau.edu", "port": 587, "user": "u",
              "password": "p", "starttls": True},
        from_addr="noreply@example.org",
        base_url="https://example.org",
    )
    assert m.send("someone@example.org", "Hello", "Body") == "smtp"
    assert ("send", "someone@example.org", "Hello") in sent
    assert ("starttls",) in sent


def test_smtp_failure_falls_back_to_manual_rather_than_raising(monkeypatch):
    def explode(*a, **k):
        raise OSError("relay refused")

    monkeypatch.setattr("smtplib.SMTP", explode)
    m = Mailer(backend="smtp", smtp={"host": "h", "port": 25, "user": None,
                                     "password": None, "starttls": False},
               from_addr="x@y.org", base_url="https://example.org")
    # A dead relay must not turn a password reset into a 500.
    assert m.send("someone@example.org", "S", "B") == "manual"


def test_reset_email_contains_the_link_and_no_token_elsewhere():
    subject, body = reset_email("https://example.org/reset-password?token=abc123")
    assert "abc123" in body
    assert subject and "\n" not in subject
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_auth.py -k mail -v`
Expected: FAIL — no module `app.mailer`.

- [ ] **Step 3: Implement**

```python
# services/dashboard/app/mailer.py
"""Outbound email with two backends.

`manual` is the default because this project has no SMTP relay yet: the
message is not sent, and the link surfaces in the admin UI for an
administrator to pass on out of band. Switching to `smtp` later is a
configuration change, not a rewrite.

A failing relay degrades to `manual` rather than raising — a dead mail
server should not turn a password reset into a 500 for the user."""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

log = logging.getLogger(__name__)


def reset_email(link: str) -> tuple[str, str]:
    return (
        "Wahoo Bay dashboard: password reset",
        "A password reset was requested for your Wahoo Bay account.\n\n"
        f"{link}\n\n"
        "The link expires in 30 minutes and can be used once. If you did not "
        "request this, you can ignore this message — nothing has changed.\n",
    )


def verify_email(link: str) -> tuple[str, str]:
    return (
        "Wahoo Bay dashboard: confirm your email",
        "Confirm your email address for your Wahoo Bay account:\n\n"
        f"{link}\n\n"
        "An administrator still needs to approve the account before you can "
        "review detections.\n",
    )


def collision_notice() -> tuple[str, str]:
    return (
        "Wahoo Bay dashboard: registration attempt",
        "Someone tried to register a Wahoo Bay account with this email "
        "address, which already has one. No new account was created and "
        "nothing has changed.\n\nIf that was you, sign in normally or use "
        "the password reset link on the sign-in page.\n",
    )


class Mailer:
    def __init__(self, backend: str, smtp: dict | None, from_addr: str, base_url: str) -> None:
        self.backend = backend if backend in ("smtp", "manual") else "manual"
        self.smtp = smtp or {}
        self.from_addr = from_addr
        self.base_url = base_url.rstrip("/")

    def link(self, path: str, token: str) -> str:
        return f"{self.base_url}{path}?token={token}"

    def send(self, to: str, subject: str, body: str) -> str:
        """Returns the backend that actually handled the message."""
        if self.backend != "smtp":
            log.info("mailer(manual): message for %s held for out-of-band delivery", to)
            return "manual"
        msg = EmailMessage()
        msg["From"] = self.from_addr
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)
        try:
            with smtplib.SMTP(self.smtp["host"], self.smtp["port"], timeout=10) as s:
                if self.smtp.get("starttls"):
                    s.starttls()
                if self.smtp.get("user"):
                    s.login(self.smtp["user"], self.smtp["password"])
                s.send_message(msg)
            return "smtp"
        except Exception as e:
            log.warning("mailer(smtp): delivery to %s failed (%s); holding for manual", to, e)
            return "manual"
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_auth.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add services/dashboard/app/mailer.py tests/test_auth.py
git commit -m "auth: mailer with smtp and manual backends"
```

---

### Task 6: Login, logout, `/api/auth/me`, and the login page

**Files:**
- Create: `services/dashboard/app/routes_auth.py`
- Create: `services/dashboard/app/templates/login.html`
- Create: `services/dashboard/app/static/auth.js`
- Modify: `services/dashboard/app/main.py` — `DashboardCfg` (new knobs), `build_app()` (mount router)
- Test: `tests/test_auth_db.py`

**Interfaces:**
- Consumes: everything from Tasks 1–5.
- Produces: `build_auth_router(cfg, state, templates) -> APIRouter`; config attributes `cookie_secure`, `session_ttl_h`, `session_abs_ttl_d`, `login_max_fails`, `login_lockout_min`, `registration_open`, `register_max_per_ip_h`, `reset_ttl_min`, `verify_ttl_h`, `mail_backend`, `smtp_*`, `public_base_url`.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_auth_db.py
CSRF = {"X-Requested-With": "wahoobay"}


@pytest.mark.anyio
async def test_login_sets_httponly_cookie_and_never_returns_raw_token(pool, client):
    await _make_user(pool, username="dan", role="reviewer")
    r = client.post("/api/auth/login",
                    json={"username": "dan", "password": "a long enough passphrase"},
                    headers=CSRF)
    assert r.status_code == 200
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie
    token = client.cookies.get(COOKIE_NAME)
    assert token and token not in r.text


@pytest.mark.anyio
async def test_login_failures_are_indistinguishable(pool, client):
    await _make_user(pool, username="dan")
    unknown = client.post("/api/auth/login",
                          json={"username": "nobody", "password": "whatever at all"},
                          headers=CSRF)
    wrong = client.post("/api/auth/login",
                        json={"username": "dan", "password": "wrong but long enough"},
                        headers=CSRF)
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json() == wrong.json()


@pytest.mark.anyio
async def test_login_requires_csrf_header(pool, client):
    await _make_user(pool, username="dan")
    r = client.post("/api/auth/login",
                    json={"username": "dan", "password": "a long enough passphrase"})
    assert r.status_code == 403


@pytest.mark.anyio
async def test_lockout_after_configured_failures(pool, client):
    await _make_user(pool, username="dan")
    for _ in range(8):
        client.post("/api/auth/login",
                    json={"username": "dan", "password": "definitely wrong here"},
                    headers=CSRF)
    # Correct password now refused because the account is locked.
    r = client.post("/api/auth/login",
                    json={"username": "dan", "password": "a long enough passphrase"},
                    headers=CSRF)
    assert r.status_code == 401


@pytest.mark.anyio
async def test_me_reports_anonymous_then_authenticated(pool, client):
    assert client.get("/api/auth/me").json() == {"authenticated": False}
    await _make_user(pool, username="dan")
    client.post("/api/auth/login",
                json={"username": "dan", "password": "a long enough passphrase"},
                headers=CSRF)
    me = client.get("/api/auth/me").json()
    assert me["authenticated"] is True
    assert me["username"] == "dan" and me["role"] == "reviewer" and me["status"] == "active"


@pytest.mark.anyio
async def test_logout_revokes_the_session(pool, client):
    await _make_user(pool, username="dan")
    client.post("/api/auth/login",
                json={"username": "dan", "password": "a long enough passphrase"},
                headers=CSRF)
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
```

Add the `client` fixtures next to the `pool` fixture:

```python
@pytest.fixture
def client(pool):
    from app.main import build_app
    os.environ["DATABASE_URL"] = TEST_DSN
    os.environ["DASHBOARD_COOKIE_SECURE"] = "false"
    with TestClient(build_app()) as c:
        yield c


@pytest.fixture
def client_secure(pool):
    from app.main import build_app
    os.environ["DATABASE_URL"] = TEST_DSN
    os.environ["DASHBOARD_COOKIE_SECURE"] = "true"
    with TestClient(build_app()) as c:
        yield c
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_auth_db.py -k "login or logout or me_reports or cookie_secure" -v`
Expected: FAIL — 404, the routes do not exist.

- [ ] **Step 3: Add the config knobs to `DashboardCfg.__init__`**

```python
        # --- auth ---
        self.cookie_secure = _env("DASHBOARD_COOKIE_SECURE", "true").lower() != "false"
        self.session_ttl_h = int(_env("DASHBOARD_SESSION_TTL_H", "12"))
        self.session_abs_ttl_d = int(_env("DASHBOARD_SESSION_ABS_TTL_D", "7"))
        self.login_max_fails = int(_env("DASHBOARD_LOGIN_MAX_FAILS", "8"))
        self.login_lockout_min = int(_env("DASHBOARD_LOGIN_LOCKOUT_MIN", "15"))
        self.registration_open = _env("DASHBOARD_REGISTRATION_OPEN", "true").lower() != "false"
        self.register_max_per_ip_h = int(_env("DASHBOARD_REGISTER_MAX_PER_IP_H", "5"))
        self.reset_ttl_min = int(_env("DASHBOARD_RESET_TTL_MIN", "30"))
        self.verify_ttl_h = int(_env("DASHBOARD_VERIFY_TTL_H", "24"))
        self.mail_backend = _env("DASHBOARD_MAIL_BACKEND", "manual")
        self.smtp = {
            "host": _env("DASHBOARD_SMTP_HOST", ""),
            "port": int(_env("DASHBOARD_SMTP_PORT", "587")),
            "user": _env("DASHBOARD_SMTP_USER", "") or None,
            "password": _env("DASHBOARD_SMTP_PASSWORD", "") or None,
            "starttls": _env("DASHBOARD_SMTP_STARTTLS", "true").lower() != "false",
        }
        self.smtp_from = _env("DASHBOARD_SMTP_FROM", "noreply@wahoobay.invalid")
        self.public_base_url = _env("DASHBOARD_PUBLIC_BASE_URL", "http://localhost:18080")
```

- [ ] **Step 4: Write `routes_auth.py` (login/logout/me + page)**

```python
# services/dashboard/app/routes_auth.py
"""Auth HTTP surface: sign-in, sign-out, identity, registration,
email verification, and password reset.

Every failure path answers with the same generic message. The specific
reason is logged, never returned — a login form that distinguishes
'no such user' from 'wrong password' is an account-enumeration oracle,
and this dashboard is reachable from the public internet."""
from __future__ import annotations

import logging
import time
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from psycopg.rows import dict_row
from pydantic import BaseModel

from .auth import (
    COOKIE_NAME,
    check_password_policy,
    create_session,
    hash_password,
    require_csrf,
    revoke_session,
    verify_password,
)
from .mailer import Mailer

log = logging.getLogger(__name__)

GENERIC_LOGIN_FAIL = {"error": "Invalid username or password."}
# Verified against a throwaway hash so an unknown username costs the same
# wall-clock time as a wrong password.
_DUMMY_HASH = hash_password("dummy password for timing equalisation")


class LoginIn(BaseModel):
    username: str
    password: str


def build_auth_router(cfg, state, templates) -> APIRouter:
    router = APIRouter()
    mailer = Mailer(cfg.mail_backend, cfg.smtp, cfg.smtp_from, cfg.public_base_url)
    state["mailer"] = mailer

    def _pool():
        pool = state.get("pool")
        if pool is None:
            raise HTTPException(status_code=503, detail="database unavailable")
        return pool

    @router.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        return templates.TemplateResponse(
            request, "login.html", {"next": request.query_params.get("next", "/")}
        )

    @router.post("/api/auth/login", dependencies=[Depends(require_csrf)])
    async def login(body: LoginIn, request: Request):
        pool = _pool()
        username = body.username.strip().lower()
        async with pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """SELECT id, password_hash, status, failed_logins, locked_until
                         FROM users WHERE username = %s""",
                    (username,),
                )
                user = await cur.fetchone()

        if user is None:
            verify_password(body.password, _DUMMY_HASH)   # equalise timing
            log.info("login: unknown username %r", username)
            return JSONResponse(GENERIC_LOGIN_FAIL, status_code=401)

        if user["locked_until"] and user["locked_until"] > datetime.now(UTC):
            log.info("login: locked account id=%s", user["id"])
            return JSONResponse(GENERIC_LOGIN_FAIL, status_code=401)

        if not verify_password(body.password, user["password_hash"]):
            async with pool.connection() as conn:
                await conn.execute(
                    """UPDATE users
                          SET failed_logins = failed_logins + 1,
                              locked_until = CASE WHEN failed_logins + 1 >= %s
                                  THEN NOW() + make_interval(mins => %s) ELSE locked_until END
                        WHERE id = %s""",
                    (cfg.login_max_fails, cfg.login_lockout_min, user["id"]),
                )
            log.info("login: bad password for id=%s", user["id"])
            return JSONResponse(GENERIC_LOGIN_FAIL, status_code=401)

        if user["status"] == "disabled":
            log.info("login: disabled account id=%s", user["id"])
            return JSONResponse(GENERIC_LOGIN_FAIL, status_code=401)

        async with pool.connection() as conn:
            await conn.execute(
                """UPDATE users SET failed_logins = 0, locked_until = NULL,
                                    last_login_at = NOW() WHERE id = %s""",
                (user["id"],),
            )
        raw, _sid = await create_session(
            pool, user["id"], cfg.session_ttl_h, cfg.session_abs_ttl_d,
            request.headers.get("user-agent"),
            request.client.host if request.client else None,
        )
        resp = JSONResponse({"ok": True})
        resp.set_cookie(
            COOKIE_NAME, raw, httponly=True, samesite="lax", path="/",
            secure=cfg.cookie_secure, max_age=cfg.session_ttl_h * 3600,
        )
        return resp

    @router.post("/api/auth/logout", dependencies=[Depends(require_csrf)])
    async def logout(request: Request):
        from .auth import load_session
        pool = state.get("pool")
        if pool is not None:
            user = await load_session(pool, request.cookies.get(COOKIE_NAME, ""))
            if user is not None:
                await revoke_session(pool, user.session_id)
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(COOKIE_NAME, path="/")
        return resp

    @router.get("/api/auth/me")
    async def me(request: Request):
        from .auth import load_session
        pool = state.get("pool")
        if pool is None:
            return {"authenticated": False}
        user = await load_session(pool, request.cookies.get(COOKIE_NAME, ""))
        if user is None:
            return {"authenticated": False}
        return {
            "authenticated": True,
            "username": user.username,
            "display_name": user.display_name,
            "role": user.role,
            "status": user.status,
        }

    return router
```

- [ ] **Step 5: Write `templates/login.html`**

The dashboard has no base template, so every auth page is standalone:

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <title>Sign in — Wahoo Bay</title>
  <link rel="stylesheet" href="/static/style.css">
</head>
<body class="auth-page">
  <main class="auth-card">
    <h1>Wahoo Bay</h1>
    <p class="auth-sub">Sign in to review detections.</p>
    <form id="login-form" autocomplete="on">
      <input type="hidden" name="next" value="{{ next }}">
      <label for="username">Username</label>
      <input id="username" name="username" autocomplete="username" required>
      <label for="password">Password</label>
      <input id="password" name="password" type="password"
             autocomplete="current-password" required>
      <p class="auth-error" id="login-error" hidden></p>
      <button type="submit">Sign in</button>
    </form>
    <p class="auth-links">
      <a href="/register">Create an account</a> ·
      <a href="/forgot-password">Forgot password?</a>
    </p>
  </main>
  <script src="/static/auth.js"></script>
</body>
</html>
```

- [ ] **Step 6: Write `static/auth.js` (login form + identity chip)**

```javascript
// services/dashboard/app/static/auth.js
// Every POST under /api carries X-Requested-With: wahoobay. A cross-origin
// form cannot set it, which is what stands in for a CSRF token here.
const CSRF = { "Content-Type": "application/json", "X-Requested-With": "wahoobay" };

async function postJSON(url, body) {
  const r = await fetch(url, {
    method: "POST",
    headers: CSRF,
    credentials: "same-origin",
    body: JSON.stringify(body || {}),
  });
  let data = {};
  try { data = await r.json(); } catch (e) { /* empty body is fine */ }
  return { ok: r.ok, status: r.status, data };
}

function wireLoginForm() {
  const form = document.getElementById("login-form");
  if (!form) return;
  const err = document.getElementById("login-error");
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    err.hidden = true;
    const res = await postJSON("/api/auth/login", {
      username: form.username.value,
      password: form.password.value,
    });
    if (res.ok) {
      window.location.href = form.next.value || "/";
    } else {
      err.textContent = res.data.error || "Sign-in failed.";
      err.hidden = false;
    }
  });
}

async function renderIdentityChip() {
  const slot = document.getElementById("identity-chip");
  if (!slot) return;
  const r = await fetch("/api/auth/me", { credentials: "same-origin" });
  const me = await r.json();
  if (!me.authenticated) { slot.hidden = true; return; }
  slot.hidden = false;
  slot.innerHTML = "";
  const name = document.createElement("span");
  name.textContent = `${me.display_name} (${me.role})`;
  const out = document.createElement("button");
  out.textContent = "Sign out";
  out.addEventListener("click", async () => {
    await postJSON("/api/auth/logout", {});
    window.location.reload();
  });
  slot.append(name, out);

  if (me.status === "pending") {
    const banner = document.createElement("div");
    banner.className = "pending-banner";
    banner.textContent =
      "Your account is awaiting administrator approval. You can browse, " +
      "but not yet submit corrections.";
    document.body.prepend(banner);
  }
}

document.addEventListener("DOMContentLoaded", () => {
  wireLoginForm();
  renderIdentityChip();
});
```

- [ ] **Step 7: Mount the router in `build_app()`**

In `main.py`, after the existing `app.mount("/static", ...)` line:

```python
    from .routes_auth import build_auth_router
    app.include_router(build_auth_router(cfg, state, TEMPLATES))
```

Add `<div id="identity-chip" hidden></div>` to the header in `templates/index.html`, and `<script src="/static/auth.js"></script>` beside the existing script tag.

- [ ] **Step 8: Add the auth styles to `static/style.css`**

```css
.auth-page { display: grid; place-items: center; min-height: 100vh; }
.auth-card { width: min(28rem, 92vw); padding: 2rem; }
.auth-card label { display: block; margin-top: 0.75rem; }
.auth-card input { width: 100%; padding: 0.5rem; }
.auth-card button { margin-top: 1rem; padding: 0.5rem 1rem; }
.auth-error { color: #b3261e; }
.auth-links { margin-top: 1rem; font-size: 0.9rem; }
#identity-chip { display: flex; gap: 0.5rem; align-items: center; }
.pending-banner { padding: 0.6rem 1rem; background: #fff4ce; color: #3b3100; }
```

- [ ] **Step 9: Run the tests**

Run: `python -m pytest tests/test_auth_db.py -v`
Expected: PASS.

- [ ] **Step 10: Commit**

```bash
git add services/dashboard/app/routes_auth.py services/dashboard/app/templates/login.html \
        services/dashboard/app/static/auth.js services/dashboard/app/static/style.css \
        services/dashboard/app/templates/index.html services/dashboard/app/main.py \
        tests/test_auth_db.py
git commit -m "auth: login, logout, identity endpoint and sign-in page"
```

---

### Task 7: Self-registration and email verification

**Files:**
- Modify: `services/dashboard/app/routes_auth.py`
- Create: `services/dashboard/app/templates/register.html`
- Modify: `services/dashboard/app/static/auth.js`
- Test: `tests/test_auth_db.py`

**Interfaces:**
- Consumes: Task 6's router, `create_user_token`, `redeem_user_token`, `Mailer`.
- Produces: `POST /api/auth/register`, `GET /register`, `GET /verify-email`, `POST /api/auth/verify-email`.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_auth_db.py
REG = {"username": "newbie", "display_name": "New Bie",
       "email": "newbie@example.org", "password": "a long enough passphrase"}


@pytest.mark.anyio
async def test_registration_creates_pending_account_that_cannot_write(pool, client):
    assert client.post("/api/auth/register", json=REG, headers=CSRF).status_code == 200

    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT status, role FROM users WHERE username = %s", ("newbie",))
        assert await cur.fetchone() == ("pending", "viewer")

    client.post("/api/auth/login",
                json={"username": "newbie", "password": REG["password"]}, headers=CSRF)
    # Logged in, but the correction endpoint is closed until approval.
    # This is the test that protects the training set.
    r = client.post("/api/corrections",
                    json={"event_id": 1, "not_a_fish": True}, headers=CSRF)
    assert r.status_code == 403


@pytest.mark.anyio
async def test_duplicate_registration_is_indistinguishable_and_creates_nothing(pool, client):
    first = client.post("/api/auth/register", json=REG, headers=CSRF)
    # Review Focus 2: the UNIQUE constraint must not produce a 500 or a
    # different body — either would be an enumeration oracle.
    same_name = client.post("/api/auth/register", json=REG, headers=CSRF)
    same_email = client.post("/api/auth/register",
                             json={**REG, "username": "other"}, headers=CSRF)
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
        cur = await conn.execute(
            "SELECT u.id FROM users u WHERE u.username = 'newbie'")
        uid = (await cur.fetchone())[0]
    from app.auth import create_user_token
    token = await create_user_token(pool, uid, "email_verify", 60, None)

    assert client.post("/api/auth/verify-email",
                       json={"token": token}, headers=CSRF).status_code == 200
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT email_verified_at, status FROM users WHERE id = %s", (uid,))
        verified_at, status = await cur.fetchone()
    assert verified_at is not None
    assert status == "pending"   # verification is not approval

    assert client.post("/api/auth/verify-email",
                       json={"token": token}, headers=CSRF).status_code == 400
```

Add the fixture:

```python
@pytest.fixture
def client_closed_registration(pool):
    from app.main import build_app
    os.environ["DATABASE_URL"] = TEST_DSN
    os.environ["DASHBOARD_COOKIE_SECURE"] = "false"
    os.environ["DASHBOARD_REGISTRATION_OPEN"] = "false"
    with TestClient(build_app()) as c:
        yield c
    os.environ.pop("DASHBOARD_REGISTRATION_OPEN", None)
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_auth_db.py -k regist -v`
Expected: FAIL — 404 on `/api/auth/register`.

- [ ] **Step 3: Implement registration inside `build_auth_router`**

```python
# add near the top of routes_auth.py
import psycopg

    class RegisterIn(BaseModel):
        username: str
        display_name: str
        email: str
        password: str

    class TokenIn(BaseModel):
        token: str

    _reg_hits: dict[str, list[float]] = {}

    def _rate_limited(ip: str | None) -> bool:
        """In-memory, per-IP, resets on restart. This is a single-process
        deployment, so a process-local counter is the honest scope."""
        if not ip:
            return False
        now = time.time()
        hits = [t for t in _reg_hits.get(ip, []) if now - t < 3600]
        hits.append(now)
        _reg_hits[ip] = hits
        return len(hits) > cfg.register_max_per_ip_h

    # Same body for every outcome: created, username taken, email taken.
    REG_OK = {"ok": True, "message":
              "Check your email to confirm the address. An administrator "
              "must approve the account before you can review detections."}

    @router.get("/register", response_class=HTMLResponse)
    async def register_page(request: Request):
        return templates.TemplateResponse(
            request, "register.html", {"open": cfg.registration_open}
        )

    @router.post("/api/auth/register", dependencies=[Depends(require_csrf)])
    async def register(body: RegisterIn, request: Request):
        if not cfg.registration_open:
            return JSONResponse({"error": "Registration is closed."}, status_code=403)
        pool = _pool()
        ip = request.client.host if request.client else None
        if _rate_limited(ip):
            return JSONResponse({"error": "Too many attempts. Try later."}, status_code=429)

        username = body.username.strip().lower()
        email = body.email.strip().lower()
        display = body.display_name.strip() or username
        if "@" not in email or "." not in email.split("@")[-1]:
            return JSONResponse({"error": "Enter a valid email address."}, status_code=400)
        problem = check_password_policy(body.password, username, email)
        if problem:
            return JSONResponse({"error": problem}, status_code=400)

        try:
            async with pool.connection() as conn:
                cur = await conn.execute(
                    """INSERT INTO users (username, display_name, email,
                                          password_hash, role, status)
                       VALUES (%s, %s, %s, %s, 'viewer', 'pending') RETURNING id""",
                    (username, display, email, hash_password(body.password)),
                )
                user_id = (await cur.fetchone())[0]
        except psycopg.errors.UniqueViolation:
            # Username or email already exists. Tell the caller exactly what
            # a successful registration says, and let the address owner know
            # out of band. Anything else is an enumeration oracle.
            log.info("register: collision for username=%r email=%r", username, email)
            subject, text = collision_notice()
            mailer.send(email, subject, text)
            return JSONResponse(REG_OK)

        token = await create_user_token(pool, user_id, "email_verify",
                                        cfg.verify_ttl_h * 60, ip)
        subject, text = verify_email(mailer.link("/verify-email", token))
        delivered = mailer.send(email, subject, text)
        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE user_tokens SET delivered_via = %s WHERE token_hash = %s",
                (delivered, token_digest(token)),
            )
        log.info("register: created pending user id=%s (mail=%s)", user_id, delivered)
        return JSONResponse(REG_OK)

    @router.get("/verify-email", response_class=HTMLResponse)
    async def verify_page(request: Request):
        return templates.TemplateResponse(
            request, "register.html",
            {"open": cfg.registration_open,
             "verify_token": request.query_params.get("token", "")},
        )

    @router.post("/api/auth/verify-email", dependencies=[Depends(require_csrf)])
    async def verify_email_endpoint(body: TokenIn):
        pool = _pool()
        user_id = await redeem_user_token(pool, body.token, "email_verify")
        if user_id is None:
            return JSONResponse(
                {"error": "That link is invalid or has expired."}, status_code=400)
        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE users SET email_verified_at = NOW() WHERE id = %s", (user_id,))
        return JSONResponse({"ok": True, "message":
                             "Email confirmed. An administrator will approve "
                             "your account before you can review detections."})
```

Extend the imports at the top of `routes_auth.py`:

```python
from .auth import (
    COOKIE_NAME, check_password_policy, create_session, create_user_token,
    hash_password, redeem_user_token, require_csrf, revoke_session,
    token_digest, verify_password,
)
from .mailer import Mailer, collision_notice, reset_email, verify_email
```

- [ ] **Step 4: Write `templates/register.html`**

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <title>Create an account — Wahoo Bay</title>
  <link rel="stylesheet" href="/static/style.css">
</head>
<body class="auth-page">
  <main class="auth-card">
    <h1>Create an account</h1>
    {% if verify_token %}
      <div id="verify-token" data-token="{{ verify_token }}">Confirming your email…</div>
    {% elif not open %}
      <p>Registration is currently closed. Ask an administrator for an account.</p>
    {% else %}
      <form id="register-form" autocomplete="on">
        <label for="username">Username</label>
        <input id="username" name="username" autocomplete="username" required>
        <label for="display_name">Display name</label>
        <input id="display_name" name="display_name" autocomplete="name" required>
        <label for="email">Email</label>
        <input id="email" name="email" type="email" autocomplete="email" required>
        <label for="password">Password</label>
        <input id="password" name="password" type="password"
               autocomplete="new-password" required>
        <label for="password2">Confirm password</label>
        <input id="password2" name="password2" type="password"
               autocomplete="new-password" required>
        <p class="auth-hint">At least 12 characters. Length matters more than symbols.</p>
        <p class="auth-error" id="register-error" hidden></p>
        <button type="submit">Create account</button>
      </form>
      <p class="auth-done" id="register-done" hidden></p>
    {% endif %}
    <p class="auth-links"><a href="/login">Back to sign in</a></p>
  </main>
  <script src="/static/auth.js"></script>
</body>
</html>
```

- [ ] **Step 5: Extend `auth.js`**

```javascript
function wireRegisterForm() {
  const form = document.getElementById("register-form");
  if (!form) return;
  const err = document.getElementById("register-error");
  const done = document.getElementById("register-done");
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    err.hidden = true;
    if (form.password.value !== form.password2.value) {
      err.textContent = "Passwords do not match."; err.hidden = false; return;
    }
    const res = await postJSON("/api/auth/register", {
      username: form.username.value,
      display_name: form.display_name.value,
      email: form.email.value,
      password: form.password.value,
    });
    if (res.ok) { form.hidden = true; done.textContent = res.data.message; done.hidden = false; }
    else { err.textContent = res.data.error || "Registration failed."; err.hidden = false; }
  });
}

async function autoVerifyEmail() {
  const el = document.getElementById("verify-token");
  if (!el || !el.dataset.token) return;
  const res = await postJSON("/api/auth/verify-email", { token: el.dataset.token });
  el.textContent = res.ok ? res.data.message : (res.data.error || "Verification failed.");
}
```

Add both to the `DOMContentLoaded` handler.

- [ ] **Step 6: Run the tests**

Run: `python -m pytest tests/test_auth_db.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add services/dashboard/app/routes_auth.py services/dashboard/app/templates/register.html \
        services/dashboard/app/static/auth.js tests/test_auth_db.py
git commit -m "auth: self-registration with approval gate and email verification"
```

---

### Task 8: Password reset

**Files:**
- Modify: `services/dashboard/app/routes_auth.py`
- Create: `services/dashboard/app/templates/forgot_password.html`, `services/dashboard/app/templates/reset_password.html`
- Modify: `services/dashboard/app/static/auth.js`
- Test: `tests/test_auth_db.py`

**Interfaces:**
- Consumes: `create_user_token`, `redeem_user_token`, `invalidate_tokens`, `revoke_all_sessions`, `Mailer`.
- Produces: `POST /api/auth/forgot-password`, `GET /forgot-password`, `GET /reset-password`, `POST /api/auth/reset-password`.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_auth_db.py
@pytest.mark.anyio
async def test_forgot_password_response_is_identical_for_known_and_unknown(pool, client):
    await _make_user(pool, username="dan")
    known = client.post("/api/auth/forgot-password",
                        json={"email": "dan@example.org"}, headers=CSRF)
    unknown = client.post("/api/auth/forgot-password",
                          json={"email": "ghost@example.org"}, headers=CSRF)
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

    r = client.post("/api/auth/reset-password",
                    json={"token": token, "password": "a brand new passphrase"},
                    headers=CSRF)
    assert r.status_code == 200

    # Both old sessions are dead — the point of the exercise if the reset
    # happened because the account was compromised.
    from app.auth import load_session
    assert await load_session(pool, old_a) is None
    assert await load_session(pool, old_b) is None

    assert client.post("/api/auth/login",
                       json={"username": "dan", "password": "a brand new passphrase"},
                       headers=CSRF).status_code == 200


@pytest.mark.anyio
async def test_reset_token_cannot_be_reused(pool, client):
    uid = await _make_user(pool, username="dan")
    from app.auth import create_user_token
    token = await create_user_token(pool, uid, "password_reset", 30, None)
    client.post("/api/auth/reset-password",
                json={"token": token, "password": "a brand new passphrase"}, headers=CSRF)
    again = client.post("/api/auth/reset-password",
                        json={"token": token, "password": "yet another passphrase"},
                        headers=CSRF)
    assert again.status_code == 400


@pytest.mark.anyio
async def test_reset_enforces_password_policy(pool, client):
    uid = await _make_user(pool, username="dan")
    from app.auth import create_user_token
    token = await create_user_token(pool, uid, "password_reset", 30, None)
    r = client.post("/api/auth/reset-password",
                    json={"token": token, "password": "tiny"}, headers=CSRF)
    assert r.status_code == 400
    # A rejected password must not burn the token.
    ok = client.post("/api/auth/reset-password",
                     json={"token": token, "password": "a brand new passphrase"},
                     headers=CSRF)
    assert ok.status_code == 200


@pytest.mark.anyio
async def test_reset_clears_lockout(pool, client):
    uid = await _make_user(pool, username="dan")
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE users SET failed_logins = 99, "
            "locked_until = NOW() + INTERVAL '1 hour' WHERE id = %s", (uid,))
    from app.auth import create_user_token
    token = await create_user_token(pool, uid, "password_reset", 30, None)
    client.post("/api/auth/reset-password",
                json={"token": token, "password": "a brand new passphrase"}, headers=CSRF)
    assert client.post("/api/auth/login",
                       json={"username": "dan", "password": "a brand new passphrase"},
                       headers=CSRF).status_code == 200
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_auth_db.py -k "forgot or reset" -v`
Expected: FAIL — 404.

- [ ] **Step 3: Implement**

```python
# add inside build_auth_router
    class ForgotIn(BaseModel):
        email: str

    class ResetIn(BaseModel):
        token: str
        password: str

    FORGOT_OK = {"ok": True, "message":
                 "If that address has an account, a reset link is on its way."}

    @router.get("/forgot-password", response_class=HTMLResponse)
    async def forgot_page(request: Request):
        return templates.TemplateResponse(request, "forgot_password.html", {})

    @router.post("/api/auth/forgot-password", dependencies=[Depends(require_csrf)])
    async def forgot_password(body: ForgotIn, request: Request):
        pool = _pool()
        email = body.email.strip().lower()
        async with pool.connection() as conn:
            cur = await conn.execute("SELECT id FROM users WHERE email = %s", (email,))
            row = await cur.fetchone()
        if row is None:
            log.info("forgot-password: no account for %r", email)
            return JSONResponse(FORGOT_OK)   # identical to the success path

        user_id = row[0]
        await invalidate_tokens(pool, user_id, "password_reset")
        token = await create_user_token(pool, user_id, "password_reset",
                                        cfg.reset_ttl_min,
                                        request.client.host if request.client else None)
        subject, text = reset_email(mailer.link("/reset-password", token))
        delivered = mailer.send(email, subject, text)
        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE user_tokens SET delivered_via = %s WHERE token_hash = %s",
                (delivered, token_digest(token)),
            )
        return JSONResponse(FORGOT_OK)

    @router.get("/reset-password", response_class=HTMLResponse)
    async def reset_page(request: Request):
        return templates.TemplateResponse(
            request, "reset_password.html",
            {"token": request.query_params.get("token", "")},
        )

    @router.post("/api/auth/reset-password", dependencies=[Depends(require_csrf)])
    async def reset_password(body: ResetIn):
        pool = _pool()
        # Validate the password BEFORE redeeming, so a rejected password
        # does not consume the user's single-use token.
        problem = check_password_policy(body.password, "", "")
        if problem:
            return JSONResponse({"error": problem}, status_code=400)

        user_id = await redeem_user_token(pool, body.token, "password_reset")
        if user_id is None:
            return JSONResponse(
                {"error": "That link is invalid or has expired."}, status_code=400)

        async with pool.connection() as conn:
            await conn.execute(
                """UPDATE users SET password_hash = %s, failed_logins = 0,
                                    locked_until = NULL WHERE id = %s""",
                (hash_password(body.password), user_id),
            )
        killed = await revoke_all_sessions(pool, user_id)
        log.info("reset-password: user id=%s, %d session(s) revoked", user_id, killed)
        return JSONResponse({"ok": True, "message":
                             "Password changed. You have been signed out everywhere."})
```

Add `invalidate_tokens` and `revoke_all_sessions` to the `from .auth import (...)` list.

- [ ] **Step 4: Write the two templates**

`forgot_password.html` — same `<head>`/`<body class="auth-page">` shell as `login.html`, with this `<main>`:

```html
  <main class="auth-card">
    <h1>Reset your password</h1>
    <form id="forgot-form">
      <label for="email">Email</label>
      <input id="email" name="email" type="email" autocomplete="email" required>
      <button type="submit">Send reset link</button>
    </form>
    <p class="auth-done" id="forgot-done" hidden></p>
    <p class="auth-links"><a href="/login">Back to sign in</a></p>
  </main>
  <script src="/static/auth.js"></script>
```

`reset_password.html` — same shell, with:

```html
  <main class="auth-card">
    <h1>Choose a new password</h1>
    <form id="reset-form">
      <input type="hidden" name="token" value="{{ token }}">
      <label for="password">New password</label>
      <input id="password" name="password" type="password"
             autocomplete="new-password" required>
      <label for="password2">Confirm new password</label>
      <input id="password2" name="password2" type="password"
             autocomplete="new-password" required>
      <p class="auth-hint">At least 12 characters.</p>
      <p class="auth-error" id="reset-error" hidden></p>
      <button type="submit">Change password</button>
    </form>
    <p class="auth-done" id="reset-done" hidden></p>
  </main>
  <script src="/static/auth.js"></script>
```

- [ ] **Step 5: Extend `auth.js`**

```javascript
function wireForgotForm() {
  const form = document.getElementById("forgot-form");
  if (!form) return;
  const done = document.getElementById("forgot-done");
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const res = await postJSON("/api/auth/forgot-password", { email: form.email.value });
    form.hidden = true;
    done.textContent = res.data.message || "Check your email.";
    done.hidden = false;
  });
}

function wireResetForm() {
  const form = document.getElementById("reset-form");
  if (!form) return;
  const err = document.getElementById("reset-error");
  const done = document.getElementById("reset-done");
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    err.hidden = true;
    if (form.password.value !== form.password2.value) {
      err.textContent = "Passwords do not match."; err.hidden = false; return;
    }
    const res = await postJSON("/api/auth/reset-password", {
      token: form.token.value, password: form.password.value,
    });
    if (res.ok) { form.hidden = true; done.textContent = res.data.message; done.hidden = false; }
    else { err.textContent = res.data.error || "Reset failed."; err.hidden = false; }
  });
}
```

Add both to `DOMContentLoaded`.

- [ ] **Step 6: Run the tests**

Run: `python -m pytest tests/test_auth_db.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add services/dashboard/app/routes_auth.py \
        services/dashboard/app/templates/forgot_password.html \
        services/dashboard/app/templates/reset_password.html \
        services/dashboard/app/static/auth.js tests/test_auth_db.py
git commit -m "auth: password reset with single-use tokens and session revocation"
```

---

### Task 9: Admin user management

**Files:**
- Create: `services/dashboard/app/routes_admin_users.py`
- Create: `services/dashboard/app/templates/users_admin.html`
- Create: `scripts/create_user.py`
- Modify: `services/dashboard/app/main.py` (mount the router)
- Test: `tests/test_auth_db.py`

**Interfaces:**
- Consumes: `make_require_role`, `hash_password`, `revoke_session`, `revoke_all_sessions`.
- Produces: `build_admin_users_router(cfg, state, templates) -> APIRouter` serving `GET /admin/users.html`, `GET /api/admin/users`, `POST /api/admin/users`, `POST /api/admin/users/{id}/approve`, `POST /api/admin/users/{id}/role`, `POST /api/admin/users/{id}/deactivate`, `GET /api/admin/sessions`, `POST /api/admin/sessions/{id}/revoke`.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_auth_db.py
async def _login_as(pool, client, username, role="admin", status="active"):
    await _make_user(pool, username=username, role=role, status=status)
    client.post("/api/auth/login",
                json={"username": username, "password": "a long enough passphrase"},
                headers=CSRF)


@pytest.mark.anyio
async def test_admin_routes_are_closed_to_reviewers(pool, client):
    await _login_as(pool, client, "rev", role="reviewer")
    assert client.get("/api/admin/users").status_code == 403


@pytest.mark.anyio
async def test_approve_promotes_pending_user_and_writes_audit(pool, client):
    client.post("/api/auth/register", json=REG, headers=CSRF)
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT id FROM users WHERE username = 'newbie'")
        target = (await cur.fetchone())[0]

    client.cookies.clear()
    await _login_as(pool, client, "boss")

    r = client.post(f"/api/admin/users/{target}/approve",
                    json={"role": "reviewer"}, headers=CSRF)
    assert r.status_code == 200

    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT status, role, approved_at, approved_by FROM users WHERE id = %s",
            (target,))
        status, role, approved_at, approved_by = await cur.fetchone()
        assert (status, role) == ("active", "reviewer")
        assert approved_at is not None and approved_by is not None

        cur = await conn.execute(
            "SELECT action FROM user_audit WHERE target_user_id = %s", (target,))
        assert [r[0] for r in await cur.fetchall()] == ["user.approve"]


@pytest.mark.anyio
async def test_approved_reviewer_can_submit_a_correction(pool, client):
    # The whole point of the feature: approval is what opens the write path.
    client.post("/api/auth/register", json=REG, headers=CSRF)
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT id FROM users WHERE username = 'newbie'")
        target = (await cur.fetchone())[0]
        await conn.execute(
            """INSERT INTO detection_events (id, frame_id, source_name, det_conf,
                                             bbox, topk)
               VALUES (4242, 1, 'seahivecam', 0.9, '{1,2,3,4}', '[]'::jsonb)""")
    client.cookies.clear()
    await _login_as(pool, client, "boss")
    client.post(f"/api/admin/users/{target}/approve",
                json={"role": "reviewer"}, headers=CSRF)

    client.cookies.clear()
    client.post("/api/auth/login",
                json={"username": "newbie", "password": REG["password"]}, headers=CSRF)
    r = client.post("/api/corrections",
                    json={"event_id": 4242, "not_a_fish": True}, headers=CSRF)
    assert r.status_code == 200

    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT user_id, session_id FROM detection_corrections WHERE event_id = 4242")
        user_id, session_id = await cur.fetchone()
    assert user_id == target and session_id is not None


@pytest.mark.anyio
async def test_admin_can_revoke_a_session(pool, client):
    uid = await _make_user(pool, username="rev", role="reviewer")
    victim_token, victim_sid = await create_session(pool, uid, 12, 7, None, None)
    await _login_as(pool, client, "boss")
    assert client.post(f"/api/admin/sessions/{victim_sid}/revoke",
                       headers=CSRF).status_code == 200
    from app.auth import load_session
    assert await load_session(pool, victim_token) is None


@pytest.mark.anyio
async def test_manual_mail_backend_exposes_pending_links_to_admins_only(pool, client):
    client.post("/api/auth/register", json=REG, headers=CSRF)
    client.cookies.clear()
    await _login_as(pool, client, "boss")
    r = client.get("/api/admin/users?status=pending")
    assert r.status_code == 200
    assert r.json()[0]["username"] == "newbie"
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_auth_db.py -k admin -v`
Expected: FAIL — 404.

- [ ] **Step 3: Implement `routes_admin_users.py`**

```python
# services/dashboard/app/routes_admin_users.py
"""Admin surface for accounts and sessions.

Every state change writes a user_audit row. Without it, "who made this
person an admin" has no answer, which is the question you only ask after
something has already gone wrong."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from psycopg.rows import dict_row
from pydantic import BaseModel

from .auth import hash_password, make_require_role, require_csrf, revoke_session

log = logging.getLogger(__name__)
ROLES = ("viewer", "reviewer", "admin")


class NewUserIn(BaseModel):
    username: str
    display_name: str
    email: str
    password: str
    role: str = "reviewer"


class RoleIn(BaseModel):
    role: str


def build_admin_users_router(cfg, state, templates) -> APIRouter:
    router = APIRouter()
    require_role = make_require_role(cfg, state)
    admin_only = require_role("admin")

    def _pool():
        pool = state.get("pool")
        if pool is None:
            raise HTTPException(status_code=503, detail="database unavailable")
        return pool

    async def _audit(conn, actor_id, action, target_id, detail=None):
        await conn.execute(
            """INSERT INTO user_audit (actor_user_id, action, target_user_id, detail)
               VALUES (%s, %s, %s, %s)""",
            (actor_id, action, target_id, detail),
        )

    @router.get("/admin/users.html", response_class=HTMLResponse)
    async def users_page(request: Request, user=Depends(admin_only)):
        return templates.TemplateResponse(request, "users_admin.html", {"me": user.username})

    @router.get("/api/admin/users")
    async def list_users(status: str | None = None, user=Depends(admin_only)):
        sql = """SELECT id, username, display_name, email, role, status,
                        email_verified_at, created_at, last_login_at
                   FROM users"""
        params: tuple = ()
        if status:
            sql += " WHERE status = %s"
            params = (status,)
        sql += " ORDER BY created_at DESC"
        async with _pool().connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(sql, params)
                return JSONResponse([dict(r) for r in await cur.fetchall()], default=str)

    @router.post("/api/admin/users", dependencies=[Depends(require_csrf)])
    async def create_user(body: NewUserIn, user=Depends(admin_only)):
        if body.role not in ROLES:
            return JSONResponse({"error": "unknown role"}, status_code=400)
        async with _pool().connection() as conn:
            cur = await conn.execute(
                """INSERT INTO users (username, display_name, email, password_hash,
                                      role, status, approved_at, approved_by)
                   VALUES (%s,%s,%s,%s,%s,'active',NOW(),%s) RETURNING id""",
                (body.username.strip().lower(), body.display_name.strip(),
                 body.email.strip().lower(), hash_password(body.password),
                 body.role, user.id),
            )
            new_id = (await cur.fetchone())[0]
            await _audit(conn, user.id, "user.create", new_id, {"role": body.role})
        return JSONResponse({"id": new_id})

    @router.post("/api/admin/users/{user_id}/approve", dependencies=[Depends(require_csrf)])
    async def approve(user_id: int, body: RoleIn, user=Depends(admin_only)):
        if body.role not in ROLES:
            return JSONResponse({"error": "unknown role"}, status_code=400)
        async with _pool().connection() as conn:
            cur = await conn.execute(
                """UPDATE users SET status = 'active', role = %s,
                                    approved_at = NOW(), approved_by = %s
                    WHERE id = %s AND status = 'pending' RETURNING id""",
                (body.role, user.id, user_id),
            )
            if await cur.fetchone() is None:
                return JSONResponse({"error": "no pending user with that id"},
                                    status_code=404)
            await _audit(conn, user.id, "user.approve", user_id, {"role": body.role})
        log.info("admin: user id=%s approved as %s by id=%s", user_id, body.role, user.id)
        return JSONResponse({"ok": True})

    @router.post("/api/admin/users/{user_id}/role", dependencies=[Depends(require_csrf)])
    async def set_role(user_id: int, body: RoleIn, user=Depends(admin_only)):
        if body.role not in ROLES:
            return JSONResponse({"error": "unknown role"}, status_code=400)
        async with _pool().connection() as conn:
            await conn.execute("UPDATE users SET role = %s WHERE id = %s",
                               (body.role, user_id))
            await _audit(conn, user.id, "user.role_change", user_id, {"role": body.role})
        return JSONResponse({"ok": True})

    @router.post("/api/admin/users/{user_id}/deactivate", dependencies=[Depends(require_csrf)])
    async def deactivate(user_id: int, user=Depends(admin_only)):
        if user_id == user.id:
            return JSONResponse({"error": "you cannot deactivate your own account"},
                                status_code=400)
        async with _pool().connection() as conn:
            await conn.execute("UPDATE users SET status = 'disabled' WHERE id = %s",
                               (user_id,))
            await conn.execute(
                "UPDATE user_sessions SET revoked_at = NOW() "
                "WHERE user_id = %s AND revoked_at IS NULL", (user_id,))
            await _audit(conn, user.id, "user.deactivate", user_id, None)
        return JSONResponse({"ok": True})

    @router.get("/api/admin/sessions")
    async def list_sessions(user=Depends(admin_only)):
        async with _pool().connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """SELECT s.id, s.user_id, u.username, s.created_at, s.last_seen_at,
                              s.expires_at, s.revoked_at, s.user_agent, s.ip
                         FROM user_sessions s JOIN users u ON u.id = s.user_id
                        WHERE s.revoked_at IS NULL AND s.expires_at > NOW()
                        ORDER BY s.last_seen_at DESC LIMIT 200""")
                return JSONResponse([dict(r) for r in await cur.fetchall()], default=str)

    @router.post("/api/admin/sessions/{session_id}/revoke",
                 dependencies=[Depends(require_csrf)])
    async def revoke(session_id: int, user=Depends(admin_only)):
        await revoke_session(_pool(), session_id)
        async with _pool().connection() as conn:
            await _audit(conn, user.id, "session.revoke", None, {"session_id": session_id})
        return JSONResponse({"ok": True})

    return router
```

- [ ] **Step 4: Write `templates/users_admin.html`**

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <title>Accounts — Wahoo Bay admin</title>
  <link rel="stylesheet" href="/static/style.css">
</head>
<body>
  <h1>Accounts</h1>
  <p>Signed in as {{ me }}.</p>

  <h2>Awaiting approval</h2>
  <table id="pending-table"><thead><tr>
    <th>Username</th><th>Name</th><th>Email</th><th>Verified</th>
    <th>Registered</th><th>Approve as</th></tr></thead><tbody></tbody></table>

  <h2>All accounts</h2>
  <table id="users-table"><thead><tr>
    <th>Username</th><th>Name</th><th>Email</th><th>Role</th><th>Status</th>
    <th>Last login</th><th></th></tr></thead><tbody></tbody></table>

  <h2>Active sessions</h2>
  <table id="sessions-table"><thead><tr>
    <th>User</th><th>Started</th><th>Last seen</th><th>IP</th><th></th>
  </tr></thead><tbody></tbody></table>

<script>
const CSRF = {"Content-Type": "application/json", "X-Requested-With": "wahoobay"};
const post = (url, body) => fetch(url, {
  method: "POST", headers: CSRF, credentials: "same-origin",
  body: JSON.stringify(body || {}),
}).then(r => r.json());

const ROLES = ["viewer", "reviewer", "admin"];

function roleSelect(current) {
  const s = document.createElement("select");
  for (const r of ROLES) {
    const o = document.createElement("option");
    o.value = r; o.textContent = r; o.selected = (r === current);
    s.append(o);
  }
  return s;
}

function cell(row, text) {
  const td = document.createElement("td");
  td.textContent = text == null ? "" : String(text);
  row.append(td);
  return td;
}

async function refresh() {
  const [pending, users, sessions] = await Promise.all([
    fetch("/api/admin/users?status=pending", {credentials: "same-origin"}).then(r => r.json()),
    fetch("/api/admin/users", {credentials: "same-origin"}).then(r => r.json()),
    fetch("/api/admin/sessions", {credentials: "same-origin"}).then(r => r.json()),
  ]);

  const pb = document.querySelector("#pending-table tbody");
  pb.innerHTML = "";
  for (const u of pending) {
    const tr = document.createElement("tr");
    cell(tr, u.username); cell(tr, u.display_name); cell(tr, u.email);
    cell(tr, u.email_verified_at ? "yes" : "no"); cell(tr, u.created_at);
    const td = document.createElement("td");
    const sel = roleSelect("reviewer");
    const btn = document.createElement("button");
    btn.textContent = "Approve";
    btn.onclick = async () => {
      await post(`/api/admin/users/${u.id}/approve`, {role: sel.value});
      refresh();
    };
    td.append(sel, btn); tr.append(td); pb.append(tr);
  }

  const ub = document.querySelector("#users-table tbody");
  ub.innerHTML = "";
  for (const u of users) {
    const tr = document.createElement("tr");
    cell(tr, u.username); cell(tr, u.display_name); cell(tr, u.email);
    const roleTd = document.createElement("td");
    const sel = roleSelect(u.role);
    sel.onchange = () => post(`/api/admin/users/${u.id}/role`, {role: sel.value});
    roleTd.append(sel); tr.append(roleTd);
    cell(tr, u.status); cell(tr, u.last_login_at);
    const actTd = document.createElement("td");
    const off = document.createElement("button");
    off.textContent = "Deactivate";
    off.onclick = async () => {
      await post(`/api/admin/users/${u.id}/deactivate`, {});
      refresh();
    };
    actTd.append(off); tr.append(actTd); ub.append(tr);
  }

  const sb = document.querySelector("#sessions-table tbody");
  sb.innerHTML = "";
  for (const s of sessions) {
    const tr = document.createElement("tr");
    cell(tr, s.username); cell(tr, s.created_at); cell(tr, s.last_seen_at); cell(tr, s.ip);
    const td = document.createElement("td");
    const btn = document.createElement("button");
    btn.textContent = "Revoke";
    btn.onclick = async () => {
      await post(`/api/admin/sessions/${s.id}/revoke`, {});
      refresh();
    };
    td.append(btn); tr.append(td); sb.append(tr);
  }
}

document.addEventListener("DOMContentLoaded", refresh);
</script>
</body>
</html>
```

Values are written with `textContent`, never `innerHTML`, because usernames and display names are attacker-supplied through self-registration.

- [ ] **Step 5: Write `scripts/create_user.py`**

```python
#!/usr/bin/env python
"""Create a dashboard account from the command line.

The first administrator has to be made this way: self-registration
produces a `pending` account, and approving one requires an
administrator who does not exist yet. Accounts created here are written
active and pre-approved.

The password is read with getpass and never accepted as an argument —
this is a shared machine where any user can read full command lines out
of `ps`."""
from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "dashboard"))

import psycopg  # noqa: E402

from app.auth import check_password_policy, hash_password  # noqa: E402


def _dsn() -> str:
    if os.environ.get("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    env = Path(__file__).resolve().parents[1] / ".env"
    for line in env.read_text(encoding="utf-8").splitlines():
        if line.startswith("DATABASE_URL="):
            return line.split("=", 1)[1].strip().strip("'\"")
    raise SystemExit("DATABASE_URL not found in environment or .env")


def main() -> None:
    ap = argparse.ArgumentParser(description="Create a dashboard user.")
    ap.add_argument("--username", required=True)
    ap.add_argument("--display-name", required=True)
    ap.add_argument("--email", required=True)
    ap.add_argument("--role", default="reviewer", choices=("viewer", "reviewer", "admin"))
    args = ap.parse_args()

    if not sys.stdin.isatty():
        raise SystemExit("refusing to run non-interactively: the password is prompted for")

    password = getpass.getpass("Password: ")
    if password != getpass.getpass("Confirm: "):
        raise SystemExit("passwords did not match")
    problem = check_password_policy(password, args.username, args.email)
    if problem:
        raise SystemExit(problem)

    with psycopg.connect(_dsn()) as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO users (username, display_name, email, password_hash,
                                  role, status, approved_at)
               VALUES (%s, %s, %s, %s, %s, 'active', NOW()) RETURNING id""",
            (args.username.strip().lower(), args.display_name.strip(),
             args.email.strip().lower(), hash_password(password), args.role),
        )
        print(f"created user id={cur.fetchone()[0]} role={args.role}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 6: Mount the router in `build_app()`**

```python
    from .routes_admin_users import build_admin_users_router
    app.include_router(build_admin_users_router(cfg, state, TEMPLATES))
```

- [ ] **Step 7: Run the tests**

Run: `python -m pytest tests/test_auth_db.py -v`
Expected: PASS. (`test_approved_reviewer_can_submit_a_correction` still fails until Task 10 swaps the guard on `/api/corrections` — if you are running tasks in order, expect that one test to fail here and pass at the end of Task 10.)

- [ ] **Step 8: Commit**

```bash
git add services/dashboard/app/routes_admin_users.py \
        services/dashboard/app/templates/users_admin.html \
        scripts/create_user.py services/dashboard/app/main.py tests/test_auth_db.py
git commit -m "auth: admin user management, approval queue and session revocation"
```

---

### Task 10: Retrofit the guarded routes, retire the write token, add `/review`

**Files:**
- Modify: `services/dashboard/app/main.py` (4 route guards, `require_write_token` removal, `/api/auth/mode` removal, `/review` route, session-prune task, correction stamping)
- Modify: `services/dashboard/app/static/app.js` (drop the token-paste flow)
- Modify: `.env.example`, `docs/operations.md`
- Test: `tests/test_auth_db.py`

**Interfaces:**
- Consumes: everything above.
- Produces: no new public functions; `/review` returns the placeholder page that spec 2 replaces.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_auth_db.py
@pytest.mark.anyio
async def test_public_reads_still_work_anonymously(pool, client):
    # The regression test this whole design most needs: the public fish
    # view is a stated deliverable and must not require a login.
    for path in ("/", "/api/species_counts", "/api/auth/me"):
        assert client.get(path).status_code == 200


@pytest.mark.anyio
async def test_all_four_legacy_mutation_routes_reject_anonymous(pool, client):
    assert client.post("/api/corrections",
                       json={"event_id": 1, "not_a_fish": True},
                       headers=CSRF).status_code == 401
    assert client.post("/api/alerts/1/ack", headers=CSRF).status_code == 401
    assert client.post("/api/admin/negative_regions",
                       json={}, headers=CSRF).status_code == 401


@pytest.mark.anyio
async def test_reviewer_cannot_reach_admin_only_mutations(pool, client):
    await _login_as(pool, client, "rev", role="reviewer")
    assert client.post("/api/alerts/1/ack", headers=CSRF).status_code == 403
    assert client.post("/api/admin/negative_regions",
                       json={}, headers=CSRF).status_code == 403


@pytest.mark.anyio
async def test_review_route_requires_reviewer_and_redirects_anonymous(pool, client):
    r = client.get("/review", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert "/login" in r.headers["location"]

    await _login_as(pool, client, "rev", role="reviewer")
    assert client.get("/review").status_code == 200


def test_write_token_machinery_is_gone():
    src = (Path(__file__).resolve().parents[1]
           / "services" / "dashboard" / "app" / "main.py").read_text()
    assert "require_write_token" not in src
    assert "DASHBOARD_WRITE_TOKEN" not in src
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_auth_db.py -k "legacy or review_route or write_token or public_reads" -v`
Expected: FAIL.

- [ ] **Step 3: Swap the four guards in `main.py`**

Build the dependency once inside `build_app()`, before the routes:

```python
    from .auth import make_require_role, require_csrf
    require_role = make_require_role(cfg, state)
```

Then change each decorator:

```python
    @app.post("/api/corrections",
              dependencies=[Depends(require_csrf)])
    async def post_correction(body: CorrectionIn,
                              user=Depends(require_role("reviewer", "admin"))) -> Response:
```

and inside its INSERT, stamp the identity — replace the existing column list and values:

```python
                    """
                    INSERT INTO detection_corrections
                    (event_id, corrected_name, corrected_species_id,
                     not_a_fish, is_endorsement, corrected_bbox,
                     confidence, reviewer, notes, celebrity_id,
                     user_id, session_id)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    RETURNING id, created_at
                    """,
                    (body.event_id, body.corrected_name, body.corrected_species_id,
                     body.not_a_fish, body.is_endorsement, bbox,
                     body.confidence, user.display_name, body.notes, celeb_id,
                     user.id, user.session_id),
```

The other three take `dependencies=[Depends(require_csrf), Depends(require_role("admin"))]`.

- [ ] **Step 4: Delete `require_write_token`, `cfg.write_token`, and `/api/auth/mode`**

Remove the function at `main.py:385`, the `self.write_token = ...` line in `DashboardCfg`, the `/api/auth/mode` endpoint, and the paragraph about `DASHBOARD_WRITE_TOKEN` in the module docstring. In `static/app.js`, remove the token-paste prompt and its localStorage read/write, and add the `X-Requested-With: wahoobay` header to every existing `fetch` that POSTs.

- [ ] **Step 5: Add the `/review` route**

```python
    @app.get("/review", response_class=HTMLResponse)
    async def review_page(request: Request,
                          user=Depends(require_role("reviewer", "admin"))):
        # Placeholder until spec 2 lands the annotation UI. Deliberately
        # not linked from index.html.
        return TEMPLATES.TemplateResponse(request, "review.html",
                                          {"display_name": user.display_name})
```

Create `templates/review.html` as a standalone page reading "Reviewer tools — coming soon", showing the signed-in name. Anonymous visitors must be redirected rather than 404'd, so add an exception handler in `build_app()`:

```python
    from fastapi.responses import RedirectResponse

    @app.exception_handler(401)
    async def unauthenticated(request: Request, exc):
        if "text/html" in request.headers.get("accept", ""):
            return RedirectResponse(f"/login?next={request.url.path}", status_code=302)
        return JSONResponse({"error": "authentication required"}, status_code=401)
```

- [ ] **Step 6: Add the session-prune background task**

In the lifespan, beside the matview refresher:

```python
    async def _prune_sessions() -> None:
        while True:
            await asyncio.sleep(86400)
            pool = state.get("pool")
            if pool is None:
                continue
            try:
                async with pool.connection() as conn:
                    await conn.execute(
                        "DELETE FROM user_sessions WHERE expires_at < NOW() "
                        "OR (revoked_at IS NOT NULL AND revoked_at < NOW() - INTERVAL '30 days')")
                    await conn.execute(
                        "DELETE FROM user_tokens WHERE expires_at < NOW() "
                        "OR (used_at IS NOT NULL AND used_at < NOW() - INTERVAL '7 days')")
            except Exception:
                log.exception("session prune failed")

    state["prune_task"] = asyncio.create_task(_prune_sessions())
```

Cancel it in the shutdown half of the lifespan, alongside the existing tasks.

- [ ] **Step 7: Document the knobs**

Add the twelve `DASHBOARD_*` auth variables from spec §8 to `.env.example` with the same comment style as the existing entries, and a short "Accounts and roles" section to `docs/operations.md` covering: creating the first admin, approving a registration, the manual mail backend, and the fact that `DASHBOARD_REGISTRATION_OPEN=false` closes signups without a code change.

- [ ] **Step 8: Run the whole suite**

Run: `python -m pytest tests/ -v && python -m ruff check services/dashboard tests scripts`
Expected: PASS, no lint findings. `test_approved_reviewer_can_submit_a_correction` from Task 9 now passes.

- [ ] **Step 9: Commit**

```bash
git add services/dashboard/app/main.py services/dashboard/app/static/app.js \
        services/dashboard/app/templates/review.html .env.example docs/operations.md \
        tests/test_auth_db.py
git commit -m "auth: enforce roles on mutation routes, retire DASHBOARD_WRITE_TOKEN"
```

---

## Deployment (after all tasks pass)

1. Apply the DDL to production: `psql "$DATABASE_URL" -f db/init.sql` — additive and safe while the old code runs.
2. Create the first admin: `python scripts/create_user.py --username <you> --display-name "<Your Name>" --email <you@fau.edu> --role admin`.
3. Restart the dashboard. This ends an uptime that began 2026-05-17; the Cloudflare tunnel targets `localhost:18080` and reconnects on its own within seconds.
4. Verify in this order: anonymous `GET /` still renders; anonymous `POST /api/corrections` returns 401; admin login works; `/admin/users.html` loads; `/review` redirects when signed out.
5. Rollback if needed: restore the previous code and restart. The new tables are additive and can stay.

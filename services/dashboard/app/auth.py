"""Authentication and authorization for the dashboard.

The cryptography and token handling use the standard library only:
the conda env this runs in also hosts the two inference workers and the
poller, so adding a dependency for password hashing or token generation
here risks the production stack on its next restart. `hashlib.scrypt`
covers password hashing and `secrets` covers token generation. The HTTP
plumbing around them (session/role dependencies, cookies) necessarily
pulls in FastAPI, same as every other module in this service.

Password hashes are stored self-describing —
`scrypt$<n>$<r>$<p>$<salt_b64>$<hash_b64>` — so the cost parameters can
be raised later without invalidating existing accounts: verification
reads the parameters out of the stored value rather than assuming the
current constants."""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import logging
import secrets
from functools import lru_cache
from pathlib import Path

log = logging.getLogger(__name__)

SCRYPT_N = 16384  # 128 * n * r = 16 MB, under scrypt's default maxmem
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
SALT_BYTES = 16

MIN_PASSWORD_LEN = 12
MAX_PASSWORD_LEN = 1024  # bounds the work an anonymous caller can ask for

_DENYLIST_PATH = Path(__file__).with_name("common_passwords.txt")


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def hash_password(password: str, *, n: int = SCRYPT_N) -> str:
    """Hash a password for storage. Raises ValueError if oversized."""
    if len(password) > MAX_PASSWORD_LEN:
        raise ValueError(f"password exceeds {MAX_PASSWORD_LEN} characters")
    salt = secrets.token_bytes(SALT_BYTES)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=n,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
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
            password.encode("utf-8"),
            salt=salt,
            n=n,
            r=r,
            p=p,
            dklen=len(expected),
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
        line.strip().lower() for line in lines if line.strip() and not line.startswith("#")
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


from dataclasses import dataclass  # noqa: E402
from datetime import UTC, datetime, timedelta  # noqa: E402

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
    never has to also remember to downgrade the role. An account waiting
    for approval is "pending", not a viewer: since 2026-09-30 the whole site
    is behind sign-in, and anyone can register."""
    if status == "disabled":
        return "none"
    if status == "pending":
        return "pending"
    return role


# The 403 detail for an account an admin hasn't approved yet. The error
# handlers send a browser on to the waiting page when they see it.
AWAITING_APPROVAL = "your account is waiting for approval"

# The only paths a signed-out visitor may reach (owner's decision
# 2026-09-30): sign in, sign up, forgot password and the email links they
# send, the waiting page, and a bare health check. Static files are a mount,
# not a route, so the site gate never sees them: they hold no data.
OPEN_PATHS = frozenset(
    {
        "/login",
        "/register",
        "/forgot-password",
        "/reset-password",
        "/verify-email",
        "/waiting",
        "/healthz",
        "/robots.txt",
    }
)
OPEN_PREFIXES = ("/api/auth/",)


def is_open_path(path: str) -> bool:
    return path in OPEN_PATHS or path.startswith(OPEN_PREFIXES)


def needs_touch(last_seen_at: datetime, now: datetime) -> bool:
    return (now - last_seen_at).total_seconds() >= TOUCH_THROTTLE_S


async def create_session(pool, user_id, ttl_hours, abs_ttl_days, user_agent, ip) -> tuple[str, int]:
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
        id=row[2],
        username=row[3],
        display_name=row[4],
        role=row[5],
        status=row[6],
        session_id=row[0],
        last_seen_at=row[1],
    )


async def touch_session(pool, session_id: int, ttl_hours: int, abs_ttl_days: int) -> None:
    """Not a gap today — the only caller (`require_role`'s dependency)
    validates the session with `load_session` first, which already checks
    `revoked_at`/`expires_at`. The guard is repeated here anyway so a less
    careful future caller can't resurrect a revoked or expired session by
    touching it directly."""
    async with pool.connection() as conn:
        await conn.execute(
            """UPDATE user_sessions
                  SET last_seen_at = NOW(),
                      expires_at = LEAST(NOW() + make_interval(hours => %s),
                                         created_at + make_interval(days => %s))
                WHERE id = %s AND revoked_at IS NULL AND expires_at > NOW()""",
            (ttl_hours, abs_ttl_days, session_id),
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


from fastapi import HTTPException, Request, Response  # noqa: E402

COOKIE_NAME = "wb_session"
CSRF_HEADER = "x-requested-with"
CSRF_VALUE = "wahoobay"


def client_ip(request: Request) -> str | None:
    """request.client.host is a real IP in production (uvicorn fills it
    from the socket peer / trusted proxy header), but Starlette's
    TestClient sets it to the literal string 'testclient'. user_sessions.ip
    and user_tokens.requested_ip are INET columns, so anything that
    doesn't parse as an address is stored as NULL rather than raising."""
    host = request.client.host if request.client else None
    if not host:
        return None
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return None
    return host


def make_require_role(cfg, state):
    """Build the role dependency. Takes cfg/state because the dashboard
    keeps its pool in a closure dict created by build_app()."""

    def require_role(*allowed: str):
        async def dependency(request: Request, response: Response) -> CurrentUser:
            pool = state.get("pool")
            if pool is None:
                raise HTTPException(
                    status_code=503, detail="database unavailable", headers={"Retry-After": "5"}
                )
            raw_token = request.cookies.get(COOKIE_NAME, "")
            user = await load_session(pool, raw_token)
            if user is None:
                raise HTTPException(status_code=401, detail="authentication required")
            role = effective_role(user.status, user.role)
            if role == "pending":
                raise HTTPException(status_code=403, detail=AWAITING_APPROVAL)
            if role not in allowed:
                # A valid session repeatedly probing above its role is the
                # signal you most want after opening a public signup form.
                log.info(
                    "require_role: 403 user_id=%s role=%s path=%s",
                    user.id,
                    role,
                    request.url.path,
                )
                raise HTTPException(status_code=403, detail="insufficient role")
            if needs_touch(user.last_seen_at, datetime.now(UTC)):
                await touch_session(pool, user.session_id, cfg.session_ttl_h, cfg.session_abs_ttl_d)
                # Slide the cookie's own max_age along with the server-side
                # session on the same throttled touch, so an actively-used
                # session doesn't get signed out by the browser dropping an
                # unrefreshed cookie at exactly session_ttl_h from login,
                # despite a still-valid (and just-extended) server session.
                response.set_cookie(
                    COOKIE_NAME,
                    raw_token,
                    httponly=True,
                    samesite="Lax",
                    path="/",
                    secure=cfg.cookie_secure,
                    max_age=cfg.session_ttl_h * 3600,
                )
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

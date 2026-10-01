"""Auth HTTP surface: sign-in, sign-out, identity, registration,
email verification, and password reset.

Every failure path answers with the same generic message. The specific
reason is logged, never returned — a login form that distinguishes
'no such user' from 'wrong password' is an account-enumeration oracle,
and this dashboard is reachable from the public internet."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from psycopg.rows import dict_row
from pydantic import BaseModel

from .auth import (
    COOKIE_NAME,
    check_password_policy,
    create_session,
    create_user_token,
    hash_password,
    invalidate_tokens,
    redeem_user_token,
    require_csrf,
    revoke_all_sessions,
    revoke_session,
    token_digest,
    verify_password,
)
from .auth import client_ip as _client_ip
from .mailer import Mailer, collision_notice, reset_email, verify_email

log = logging.getLogger(__name__)

GENERIC_LOGIN_FAIL = {"error": "Invalid username or password."}
# Verified against a throwaway hash so an unknown username costs the same
# wall-clock time as a wrong password.
_DUMMY_HASH = hash_password("dummy password for timing equalisation")


class LoginIn(BaseModel):
    username: str
    password: str


class RegisterIn(BaseModel):
    username: str
    display_name: str
    email: str
    password: str


class TokenIn(BaseModel):
    token: str


class ForgotIn(BaseModel):
    email: str


class ResetIn(BaseModel):
    token: str
    password: str


def _safe_next(raw: str | None) -> str:
    """Post-login redirect targets must be same-origin absolute paths.

    Rejects absolute URLs (`https://evil.example`), protocol-relative URLs
    (`//evil.example`), backslash tricks some browsers normalise to `//`,
    scheme URIs such as `javascript:`, and embedded tab/CR/LF — any of
    which would turn the login form into an open redirect or an XSS sink.

    The tab/CR/LF check is not redundant with the others: per the WHATWG
    URL Standard, the basic URL parser strips ASCII tab/LF/CR wherever
    they occur in the input, not only at the edges. `"/\\t/evil.example"`
    starts with a single `/` and contains no `//` or `/\\` as a plain
    string, so it would pass every other check here, but the browser
    strips the tab at parse time and the value becomes `//evil.example` —
    a protocol-relative URL — the moment it's handed to `location.href`.
    Rejecting outright (rather than stripping first, then re-checking) is
    deliberate: stripping invites ordering subtleties, and no legitimate
    redirect path contains these bytes."""
    if not raw or any(c in raw for c in "\t\r\n"):
        return "/"
    if not raw.startswith("/") or raw.startswith("//") or raw.startswith("/\\"):
        return "/"
    return raw


def build_auth_router(cfg, state, templates) -> APIRouter:
    router = APIRouter()
    mailer = Mailer(cfg.mail_backend, cfg.smtp, cfg.smtp_from, cfg.public_base_url)
    state["mailer"] = mailer

    def _pool():
        pool = state.get("pool")
        if pool is None:
            raise HTTPException(status_code=503, detail="database unavailable")
        return pool

    _REG_HITS_SWEEP_CAP = 10000

    def _make_rate_limiter(max_per_ip_h: int):
        """In-memory, per-IP, resets on restart. This is a single-process
        deployment, so a process-local counter is the honest scope.

        A key is only trimmed when that same address makes another
        request, so an attacker who never repeats a source address —
        trivial with a single IPv6 /64 delegation, no spoofing required,
        every address completes a real handshake — would otherwise grow
        this dict without bound for the life of the process. Once it
        grows past _REG_HITS_SWEEP_CAP entries, every key is swept and
        the ones whose window has fully expired are dropped; the
        in-window rate-limiting behaviour itself is unchanged.

        Each caller gets its own hits dict and budget — login and
        registration must not share either the counters or the limit, or
        a burst of one would eat into the other's budget."""
        hits: dict[str, list[float]] = {}

        def _rate_limited(ip: str | None) -> bool:
            if not ip:
                return False
            now = time.time()
            ip_hits = [t for t in hits.get(ip, []) if now - t < 3600]
            ip_hits.append(now)
            hits[ip] = ip_hits

            if len(hits) > _REG_HITS_SWEEP_CAP:
                for key, timestamps in list(hits.items()):
                    if not [t for t in timestamps if now - t < 3600]:
                        del hits[key]

            return len(ip_hits) > max_per_ip_h

        return _rate_limited

    _rate_limited = _make_rate_limiter(cfg.register_max_per_ip_h)
    _login_rate_limited = _make_rate_limiter(cfg.login_max_per_ip_h)
    _forgot_rate_limited = _make_rate_limiter(cfg.forgot_max_per_ip_h)

    # Same body for every outcome: created, username taken, email taken.
    REG_OK = {"ok": True, "message":
              "Check your email to confirm the address. An administrator "
              "must approve the account before you can use the site."}

    def _page(**ctx) -> dict:
        """Template context for an auth page.

        Every one of these pages loads `auth.js`, so each needs the same
        cache-busting value the dashboard's own index uses — otherwise a
        change to the login script is served stale to returning browsers,
        which looks like an outage rather than a cache. `build_app()` owns
        the hashing closure and hands it over through `state`."""
        version = state.get("asset_version")
        return {"asset_version": version() if version else "", **ctx}

    @router.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        return templates.TemplateResponse(
            request, "login.html", _page(next=_safe_next(request.query_params.get("next")))
        )

    @router.post("/api/auth/login", dependencies=[Depends(require_csrf)])
    async def login(body: LoginIn, request: Request):
        pool = _pool()
        ip = _client_ip(request)
        # Checked before any scrypt work, not just before the DB query: an
        # anonymous login pays a full ~31ms scrypt verification even for a
        # nonexistent username (the timing-equalisation dummy hash below),
        # and a nonexistent username can never trip the per-account lockout
        # — so this per-IP budget is the only thing standing between an
        # anonymous flood and a saturated single-threaded event loop. It
        # has its own, larger budget than registration's limiter: a
        # legitimate user mistyping a password a few times in a row must
        # not compete with signup abuse for the same counter.
        if _login_rate_limited(ip):
            return JSONResponse({"error": "Too many attempts. Try later."}, status_code=429)
        username = body.username.strip().lower()
        async with pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """SELECT id, password_hash, status, failed_logins, locked_until
                         FROM users WHERE username = %s""",
                    (username,),
                )
                user = await cur.fetchone()

        # Exactly one scrypt verification per request, before any failure
        # branch below can return — verifying on only some paths (e.g.
        # returning immediately for a locked account, as this used to do)
        # makes that branch measurably faster than the others, leaking
        # account state ("this account exists and is locked") through a
        # response-time side channel to anyone who has timed a baseline.
        stored = user["password_hash"] if user is not None else _DUMMY_HASH
        # scrypt is synchronous CPU work (~31ms median on this machine) and
        # this process runs a single uvicorn worker with one event loop —
        # calling it inline would stall every other request (including the
        # public live-detection view) for the duration. to_thread hands it
        # to the default threadpool so the loop stays free.
        password_ok = await asyncio.to_thread(verify_password, body.password, stored)

        if user is None:
            log.info("login: unknown username %r", username)
            return JSONResponse(GENERIC_LOGIN_FAIL, status_code=401)

        if user["locked_until"] and user["locked_until"] > datetime.now(UTC):
            log.info("login: locked account id=%s", user["id"])
            return JSONResponse(GENERIC_LOGIN_FAIL, status_code=401)

        if not password_ok:
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
            ip,
        )
        resp = JSONResponse({"ok": True})
        resp.set_cookie(
            COOKIE_NAME, raw, httponly=True, samesite="Lax", path="/",
            secure=cfg.cookie_secure, max_age=cfg.session_ttl_h * 3600,
        )
        return resp

    @router.get("/register", response_class=HTMLResponse)
    async def register_page(request: Request):
        return templates.TemplateResponse(
            request, "register.html", _page(open=cfg.registration_open)
        )

    @router.post("/api/auth/register", dependencies=[Depends(require_csrf)])
    async def register(body: RegisterIn, request: Request):
        if not cfg.registration_open:
            return JSONResponse({"error": "Registration is closed."}, status_code=403)
        pool = _pool()
        ip = _client_ip(request)
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

        # Hashed off the event loop (see the login handler for why) before
        # either the success or collision path below, so both still pay
        # the same scrypt cost — unchanged from before this was threaded,
        # and load-bearing for the timing-channel note just below.
        password_hash = await asyncio.to_thread(hash_password, body.password)

        # The created and collision paths below differ in database round
        # trips (the collision path skips the token INSERT and the
        # delivered_via UPDATE), which is a known, accepted residual
        # timing channel: it could let a patient attacker distinguish
        # "created" from "collided" by timing alone. Every mitigation
        # considered was worse than the disease — a dummy token write
        # puts garbage rows in a real table on an unauthenticated public
        # endpoint, an artificial sleep is a self-inflicted denial-of-
        # service knob, and moving the work to a background task changes
        # registration's error-handling contract to buy a partial
        # improvement. Closing it needs statistical sampling across many
        # requests, and the per-IP rate limiter above already caps how
        # many samples one address can collect per hour. This was judged
        # not worth fixing; it is not an oversight.
        try:
            async with pool.connection() as conn:
                cur = await conn.execute(
                    """INSERT INTO users (username, display_name, email,
                                          password_hash, role, status)
                       VALUES (%s, %s, %s, %s, 'viewer', 'pending') RETURNING id""",
                    (username, display, email, password_hash),
                )
                user_id = (await cur.fetchone())[0]
        except psycopg.errors.UniqueViolation as exc:
            # Username or email already exists. Tell the caller exactly what
            # a successful registration says. Anything else is an
            # enumeration oracle.
            #
            # The collision notice goes out-of-band ONLY when the email
            # itself collided. If we emailed whatever address the request
            # body carried regardless of which constraint fired, an
            # attacker could submit a guessed-existing username together
            # with an address they control and receive "this address
            # already has an account" back through a channel the HTTP
            # response deliberately withholds — false about that address,
            # and a confirmation the username exists. Pointed the other
            # way, it would let an attacker send a misleading notice to
            # any victim address they name. So a username-only collision
            # sends no mail at all; the HTTP response stays identical
            # either way.
            constraint = getattr(exc.diag, "constraint_name", None)
            log.info("register: collision for username=%r email=%r (constraint=%s)",
                     username, email, constraint)
            if constraint == "users_email_key":
                subject, text = collision_notice()
                mailer.send(email, subject, text)
            return JSONResponse(REG_OK)

        token = await create_user_token(pool, user_id, "email_verify",
                                        cfg.verify_ttl_h * 60, ip)
        subject, text = verify_email(mailer.link("/verify-email", token),
                                     hours=cfg.verify_ttl_h)
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
            _page(open=cfg.registration_open,
                  verify_token=request.query_params.get("token", "")),
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
                             "your account before you can use the site."})

    @router.get("/waiting", response_class=HTMLResponse)
    async def waiting_page(request: Request):
        """Where an account waiting for approval lands: the site gate sends
        its every page here (owner's decision 2026-09-30). Signed out: sign
        in first. Anyone already approved: on to the dashboard."""
        from .auth import effective_role, load_session
        user = await load_session(_pool(), request.cookies.get(COOKIE_NAME, ""))
        if user is None:
            return RedirectResponse("/login?next=/waiting", status_code=302)
        if effective_role(user.status, user.role) != "pending":
            return RedirectResponse("/", status_code=302)
        return templates.TemplateResponse(request, "notice.html", _page(
            eyebrow="Account created",
            title="Waiting for approval",
            message=("An administrator needs to approve your account before you can see "
                     "the site. Sign in again once they have."),
            signout=True,
        ))

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

    # One constant message for every address, so the response cannot reveal
    # whether an account exists -- but it must also be TRUE. With the manual
    # backend nothing is sent, and telling someone a link is on its way
    # leaves them waiting on mail that will never arrive, with no hint that
    # an administrator is the actual channel. Keyed off the mailer's
    # *effective* backend, not the configured one, because a misconfigured
    # relay degrades to manual at construction.
    FORGOT_OK = {"ok": True, "message": (
        "If that address has an account, a reset link is on its way."
        if mailer.backend == "smtp" else
        "If that address has an account, an administrator can issue a reset "
        "link for it. Automatic email is not enabled on this deployment, so "
        "please contact an administrator."
    )}

    @router.get("/forgot-password", response_class=HTMLResponse)
    async def forgot_page(request: Request):
        return templates.TemplateResponse(request, "forgot_password.html", _page())

    @router.post("/api/auth/forgot-password", dependencies=[Depends(require_csrf)])
    async def forgot_password(body: ForgotIn, request: Request):
        pool = _pool()
        # Before the lookup: this endpoint makes the server send mail to an
        # address the caller picks, so without a cap anyone who finds the
        # public URL can mail-bomb a victim and burn the sending domain's
        # reputation. Keyed on IP, so it cannot leak whether an account
        # exists -- the limit is the same for every address.
        if _forgot_rate_limited(_client_ip(request)):
            return JSONResponse({"error": "Too many attempts. Try later."}, status_code=429)
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
                                        cfg.reset_ttl_min, _client_ip(request))
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
            _page(token=request.query_params.get("token", "")),
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

        password_hash = await asyncio.to_thread(hash_password, body.password)
        async with pool.connection() as conn:
            await conn.execute(
                """UPDATE users SET password_hash = %s, failed_logins = 0,
                                    locked_until = NULL WHERE id = %s""",
                (password_hash, user_id),
            )
        killed = await revoke_all_sessions(pool, user_id)
        log.info("reset-password: user id=%s, %d session(s) revoked", user_id, killed)
        return JSONResponse({"ok": True, "message":
                             "Password changed. You have been signed out everywhere."})

    return router

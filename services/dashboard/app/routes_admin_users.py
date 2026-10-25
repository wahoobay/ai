"""Admin surface for accounts and sessions.

Every state change writes a user_audit row. Without it, "who made this
person an admin" has no answer, which is the question you only ask after
something has already gone wrong."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
from datetime import datetime

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel

from .auth import (
    check_password_policy,
    create_user_token,
    hash_password,
    invalidate_tokens,
    make_require_role,
    require_csrf,
)
from .auth import client_ip as _client_ip
from .mailer import approved_email

log = logging.getLogger(__name__)
ROLES = ("viewer", "reviewer", "admin")


class NewUserIn(BaseModel):
    username: str
    display_name: str
    email: str
    password: str
    # Spec §3: a new account must never arrive holding write access by
    # default, whoever created it — self-registration already lands on
    # 'viewer'/'pending'; an admin who doesn't explicitly pick a role gets
    # the same floor rather than a silent 'reviewer' grant.
    role: str = "viewer"


class RoleIn(BaseModel):
    role: str


def _jsonable_rows(rows: list[dict]) -> list[dict]:
    """psycopg loads TIMESTAMPTZ as datetime and INET as
    ipaddress.IPv4Address/IPv6Address — neither is JSON-serialisable by
    Starlette's JSONResponse (which, unlike stdlib json.dumps, takes no
    `default=` callback). Stringify those columns before handing rows to
    JSONResponse; every other value passes through untouched."""
    out = []
    for r in rows:
        d = dict(r)
        for k, v in d.items():
            if isinstance(v, datetime):
                d[k] = v.isoformat()
            elif isinstance(v, ipaddress.IPv4Address | ipaddress.IPv6Address):
                d[k] = str(v)
        out.append(d)
    return out


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
            (actor_id, action, target_id, Jsonb(detail) if detail is not None else None),
        )

    @router.get("/admin/users.html", response_class=HTMLResponse)
    async def users_page(request: Request, user=Depends(admin_only)):
        version = state.get("asset_version")
        return templates.TemplateResponse(
            request,
            "users_admin.html",
            {
                "me": user.username,
                "asset_version": version() if version else "",
            },
        )

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
                return JSONResponse(_jsonable_rows(await cur.fetchall()))

    @router.post("/api/admin/users", dependencies=[Depends(require_csrf)])
    async def create_user(body: NewUserIn, user=Depends(admin_only)):
        if body.role not in ROLES:
            return JSONResponse({"error": "unknown role"}, status_code=400)
        problem = check_password_policy(body.password, body.username, body.email)
        if problem:
            return JSONResponse({"error": problem}, status_code=400)
        # Off the event loop — same reasoning as login/registration/reset:
        # scrypt is synchronous CPU work and this process runs a single
        # event loop.
        password_hash = await asyncio.to_thread(hash_password, body.password)
        try:
            async with _pool().connection() as conn:
                cur = await conn.execute(
                    """INSERT INTO users (username, display_name, email, password_hash,
                                          role, status, approved_at, approved_by)
                       VALUES (%s,%s,%s,%s,%s,'active',NOW(),%s) RETURNING id""",
                    (
                        body.username.strip().lower(),
                        body.display_name.strip(),
                        body.email.strip().lower(),
                        password_hash,
                        body.role,
                        user.id,
                    ),
                )
                new_id = (await cur.fetchone())[0]
                await _audit(conn, user.id, "user.create", new_id, {"role": body.role})
        except psycopg.errors.UniqueViolation:
            # Unlike registration, there's nothing to conceal here: the
            # caller is an authenticated admin who can already list every
            # user, so a clear message is strictly more useful than an
            # enumeration-safe generic one.
            return JSONResponse({"error": "username or email already exists"}, status_code=400)
        return JSONResponse({"id": new_id})

    @router.post("/api/admin/users/{user_id}/approve", dependencies=[Depends(require_csrf)])
    async def approve(user_id: int, body: RoleIn, user=Depends(admin_only)):
        if body.role not in ROLES:
            return JSONResponse({"error": "unknown role"}, status_code=400)
        async with _pool().connection() as conn:
            cur = await conn.execute(
                """UPDATE users SET status = 'active', role = %s,
                                    approved_at = NOW(), approved_by = %s
                    WHERE id = %s AND status = 'pending' RETURNING email""",
                (body.role, user.id, user_id),
            )
            row = await cur.fetchone()
            if row is None:
                return JSONResponse({"error": "no pending user with that id"}, status_code=404)
            await _audit(conn, user.id, "user.approve", user_id, {"role": body.role})
        log.info("admin: user id=%s approved as %s by id=%s", user_id, body.role, user.id)
        # Tell them they're in (owner's request 2026-10-01). Off the event
        # loop: an SMTP exchange can take seconds. The mailer never raises; a
        # failed send falls back to manual and is logged there.
        mailer = state.get("mailer")
        if mailer is not None:
            subject, text = approved_email(mailer.link_to("/login"), body.role)
            await asyncio.to_thread(mailer.send, row[0], subject, text)
        return JSONResponse({"ok": True})

    @router.post("/api/admin/users/{user_id}/role", dependencies=[Depends(require_csrf)])
    async def set_role(user_id: int, body: RoleIn, user=Depends(admin_only)):
        if body.role not in ROLES:
            return JSONResponse({"error": "unknown role"}, status_code=400)
        async with _pool().connection() as conn:
            cur = await conn.execute(
                "UPDATE users SET role = %s WHERE id = %s RETURNING id", (body.role, user_id)
            )
            if await cur.fetchone() is None:
                return JSONResponse({"error": "no user with that id"}, status_code=404)
            await _audit(conn, user.id, "user.role_change", user_id, {"role": body.role})
        return JSONResponse({"ok": True})

    @router.post("/api/admin/users/{user_id}/deactivate", dependencies=[Depends(require_csrf)])
    async def deactivate(user_id: int, user=Depends(admin_only)):
        if user_id == user.id:
            return JSONResponse(
                {"error": "you cannot deactivate your own account"}, status_code=400
            )
        async with _pool().connection() as conn:
            cur = await conn.execute(
                "UPDATE users SET status = 'disabled' WHERE id = %s RETURNING id", (user_id,)
            )
            if await cur.fetchone() is None:
                return JSONResponse({"error": "no user with that id"}, status_code=404)
            await conn.execute(
                "UPDATE user_sessions SET revoked_at = NOW() "
                "WHERE user_id = %s AND revoked_at IS NULL",
                (user_id,),
            )
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
                        ORDER BY s.last_seen_at DESC LIMIT 200"""
                )
                return JSONResponse(_jsonable_rows(await cur.fetchall()))

    @router.post("/api/admin/sessions/{session_id}/revoke", dependencies=[Depends(require_csrf)])
    async def revoke(session_id: int, user=Depends(admin_only)):
        # One connection/transaction for both the revoke and its audit
        # row, same race-safe UPDATE...RETURNING pattern as the other
        # id-targeted routes above: a nonexistent (or already-revoked)
        # session id now 404s instead of reporting ok:true and writing an
        # audit row for a no-op, and the audit can no longer exist without
        # the revocation actually having happened (previously two separate
        # transactions).
        async with _pool().connection() as conn:
            cur = await conn.execute(
                "UPDATE user_sessions SET revoked_at = NOW() "
                "WHERE id = %s AND revoked_at IS NULL RETURNING id",
                (session_id,),
            )
            if await cur.fetchone() is None:
                return JSONResponse({"error": "no active session with that id"}, status_code=404)
            await _audit(conn, user.id, "session.revoke", None, {"session_id": session_id})
        return JSONResponse({"ok": True})

    @router.post("/api/admin/users/{user_id}/reset-link", dependencies=[Depends(require_csrf)])
    async def reset_link(user_id: int, request: Request, user=Depends(admin_only)):
        """Mint a fresh password-reset link and hand it back to the
        requesting administrator, who is the intended out-of-band channel
        for it. With DASHBOARD_MAIL_BACKEND=manual (the default — no relay
        is configured), forgot-password mints a token, hashes it into
        user_tokens, and the raw link is never stored anywhere, so it
        would otherwise be unrecoverable. This endpoint is the recovery
        path: it mints a new token the same way forgot-password does and
        returns the raw, one-time link directly to the caller instead of
        discarding it.

        Does not touch password_hash — the account keeps its existing
        password until (and unless) someone actually redeems this link."""
        async with _pool().connection() as conn:
            cur = await conn.execute("SELECT id FROM users WHERE id = %s", (user_id,))
            if await cur.fetchone() is None:
                return JSONResponse({"error": "no user with that id"}, status_code=404)

        pool = _pool()
        # Same invalidate-then-mint sequence as /api/auth/forgot-password,
        # so this doesn't leave two live password_reset tokens (this one
        # and, say, a self-service request the user made minutes earlier)
        # both redeemable at once.
        await invalidate_tokens(pool, user_id, "password_reset")
        token = await create_user_token(
            pool, user_id, "password_reset", cfg.reset_ttl_min, _client_ip(request)
        )
        link = f"{cfg.public_base_url.rstrip('/')}/reset-password?token={token}"

        async with pool.connection() as conn:
            await _audit(conn, user.id, "user.reset_link", user_id, None)
        log.info("admin: reset link minted for user id=%s by id=%s", user_id, user.id)
        return JSONResponse({"link": link})

    return router

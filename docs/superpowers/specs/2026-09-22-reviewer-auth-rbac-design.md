# Design: Authentication and Role-Based Access Control

**Date:** 2026-09-22
**Status:** Approved for planning
**Spec 1 of 2.** Spec 2 (reviewer annotation UI and verified training dataset) builds on this and is written separately.

---

## 1. Purpose

The dashboard needs identified users with roles before the reviewer annotation tool can exist. Two things drive this:

1. **The annotation tool needs identity.** Reviewer corrections must be attributable to a person, and the tool must report what a reviewer verified during their current session. Both require an authenticated account, not a self-asserted name.
2. **The mutation endpoints are currently unprotected.** `DASHBOARD_WRITE_TOKEN` is unset in production, so the token gate at `services/dashboard/app/main.py:385` returns immediately and all four guarded routes accept anonymous writes. The dashboard is reachable over a public Cloudflare tunnel, so today anyone with the URL can submit corrections, acknowledge alerts, trigger a sheet refresh, and overwrite the negative-regions file that the inference workers load at startup.

### Intended outcome

Public read access is preserved exactly as it is today — the live fish identification view stays open to anyone, with no login. Every write requires an authenticated account holding the `reviewer` or `admin` role.

### Success criteria

- An anonymous visitor sees the dashboard unchanged, including live detections.
- An anonymous request to any mutation endpoint is refused.
- A `reviewer` can submit corrections; an `admin` can additionally manage users, sessions, negative regions, alert acknowledgements, and sheet refreshes.
- Every correction written after this ships carries a `user_id` and a `session_id`.
- A prospective annotator can register themselves and recover a forgotten password without an administrator editing the database by hand.
- A self-registered account cannot write anything until an administrator approves it.
- No new Python dependency is added to the conda environment that runs the production worker, poller, and dashboard processes.

### The registration constraint that shapes this design

The dashboard is reachable over a public Cloudflare tunnel. Self-registration that granted annotation rights on signup would let anyone on the internet write into the corrections table — the exact table whose output becomes the verified fine-tuning dataset. Poisoned training data is harder to detect and more expensive to undo than a defaced page.

Registration therefore creates an account with status `pending`, which authenticates but cannot write. An administrator approves it and assigns a role. Signup is self-service; authorisation is not.

### Out of scope

The annotation UI, the review queue, frame-level verification state, and the dataset export are all spec 2. This spec ends at: accounts exist, roles are enforced, and the `/review` route is served to authorized users (its contents are spec 2's problem).

---

## 2. Constraints that shaped the design

| Constraint | Consequence |
|---|---|
| No root on the shared DGX | No system packages. Anything installed lands in the conda env. |
| One conda env runs all production processes | Adding dependencies risks the live stack on next restart. Auth uses the standard library only. |
| Shared machine, other users can read `ps` output | Passwords never appear in command-line arguments. |
| `db/init.sql` uses `IF NOT EXISTS`, applied manually via `psql` (documented in the grant write-up §5.3) | Schema changes are additive and idempotent. No migration framework. |
| Postgres holds 680M+ detection rows and is not backed up off-box | Session storage must not be a hot write path, and the DB must not hold anything that is itself a credential. |
| The dashboard process has run continuously since 2026-05-17 | The deploy restart is a planned, announced event, not an afterthought. |

---

## 3. Data model

Added to `db/init.sql`, following the existing `IF NOT EXISTS` convention, and applied to the live database with `psql`.

```sql
CREATE TABLE IF NOT EXISTS users (
    id                BIGSERIAL PRIMARY KEY,
    username          TEXT        NOT NULL UNIQUE,   -- stored case-folded
    display_name      TEXT        NOT NULL,          -- shown in UI and on corrections
    email             TEXT        NOT NULL UNIQUE,   -- case-folded; needed for reset
    password_hash     TEXT        NOT NULL,          -- scrypt$n$r$p$salt_b64$hash_b64
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
```

`status` replaces the boolean `is_active` from the pre-registration draft, because there are now three distinct states and a boolean cannot express "authenticates but may not write". `role` defaults to `viewer` rather than `reviewer`: a newly created account should never arrive holding write access by default, whoever created it.

```sql
CREATE TABLE IF NOT EXISTS user_tokens (
    id            BIGSERIAL PRIMARY KEY,
    user_id       BIGINT      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    purpose       TEXT        NOT NULL
                  CHECK (purpose IN ('password_reset', 'email_verify')),
    token_hash    TEXT        NOT NULL UNIQUE,   -- sha256; raw token never stored
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at    TIMESTAMPTZ NOT NULL,
    used_at       TIMESTAMPTZ,
    requested_ip  INET,
    delivered_via TEXT                            -- 'smtp' or 'manual'
);

CREATE INDEX IF NOT EXISTS user_tokens_user_purpose
    ON user_tokens (user_id, purpose, created_at DESC);

CREATE TABLE IF NOT EXISTS user_sessions (
    id            BIGSERIAL PRIMARY KEY,
    token_hash    TEXT        NOT NULL UNIQUE,    -- sha256 of the cookie value
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

CREATE TABLE IF NOT EXISTS user_audit (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    actor_user_id   BIGINT REFERENCES users(id),
    action          TEXT   NOT NULL,   -- user.create, user.role_change,
                                       -- user.deactivate, session.revoke
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

**Why `user_id` and `session_id` are nullable:** the 53 corrections recorded between 2026-05-17 and 2026-05-20 predate accounts and carry only the free-text `reviewer` column, which stays in place. No backfill, no destructive change to existing rows.

**Why sessions live in Postgres rather than in a signed stateless cookie:** revocation, the admin session list, and spec 2's "what did I verify in this session" all become ordinary indexed queries. A stateless cookie cannot be revoked before expiry.

---

## 4. Authentication mechanics

All of this uses the standard library: `hashlib`, `hmac`, `secrets`, `base64`, `datetime`. No new packages.

### Password storage

`hashlib.scrypt(password, salt=<16 random bytes>, n=2**14, r=8, p=1, dklen=32)`.

Stored as a self-describing string: `scrypt$16384$8$1$<salt_b64>$<hash_b64>`. Verification parses the parameters out of the stored value rather than assuming current constants, so cost parameters can be raised later without invalidating existing hashes. Comparison uses `hmac.compare_digest`.

### Session tokens

- Token: `secrets.token_urlsafe(32)`.
- Cookie: name `wb_session`, `HttpOnly`, `SameSite=Lax`, `Path=/`, `Secure` controlled by config.
- The database stores only `sha256(token)`. A database dump therefore does not yield usable logins — which matters here, because the database currently has no off-box backup and 281 GB of it sits on one machine.
- Lifetime: sliding 12 hours on `last_seen_at`, absolute maximum 7 days from `created_at`. Both configurable.
- `last_seen_at` is refreshed at most once per 60 seconds per session. The dashboard polls several endpoints on a 30-second cycle; without this throttle each poll becomes a database write.

### Login flow

1. Look up the user by case-folded username.
2. If the user does not exist, verify the supplied password against a fixed dummy hash anyway, then fail. This keeps the response time for "no such user" indistinguishable from "wrong password", so the endpoint cannot be used to enumerate accounts.
3. If `locked_until` is in the future, refuse without checking the password.
4. On a wrong password, increment `failed_logins`; at the configured threshold set `locked_until`.
5. If `status = 'disabled'`, refuse with the same generic `401` as a wrong password.
6. On success, reset `failed_logins` and `locked_until`, set `last_login_at`, mint a **new** session row and token (never reuse a pre-login token — this is what prevents session fixation), and set the cookie.

A `pending` account **is allowed to log in**. It sees the dashboard and a banner explaining that an administrator has not yet approved the account; every write is refused by role, since `pending` accounts hold `viewer`. Letting them in confirms the password worked and gives them somewhere to land, rather than an indistinguishable login failure that generates a support request.

Failure responses are identical in body and status for every failure mode: `401` with a generic message. The specific reason goes to the server log, never to the client.

### Logout

Sets `revoked_at` on the session row and clears the cookie. Logging out on one device does not affect other sessions; the admin UI can revoke individually.

### Password policy

Minimum 12 characters, with no composition rules. Length carries more real strength than forced symbol classes, and complexity rules push people toward predictable substitutions and written-down passwords. The submitted password is additionally rejected if it appears in a short built-in list of common passwords shipped in the repository, or if it contains the username or email local-part. No external breach-database lookup: that would mean an outbound API dependency for every signup.

### Self-registration

`POST /api/auth/register` takes username, display name, email, and password.

1. Validate the password against the policy; validate the email shape.
2. Create the user with `status = 'pending'` and `role = 'viewer'`.
3. Issue an `email_verify` token (24-hour expiry) and hand it to the mailer.
4. Respond with the same success body whether or not the username or email was already taken.

That last point matters: a registration form that says "username already exists" is an account-enumeration oracle on a publicly reachable page. On a collision, no account is created and — if the address belongs to an existing user — the mailer sends that user a "someone tried to register with your address" notice instead. The caller cannot distinguish the two cases.

Rate limiting: per-IP, a small number of registration attempts per hour, counted in memory. This is a single-process deployment, so an in-memory counter is sufficient and honest about its scope; it resets on restart.

Verifying the email sets `email_verified_at`. It does **not** grant any capability by itself — approval is the gate that matters, and an administrator can approve an account whose address was never verified (useful when SMTP is not configured).

### Approval

An administrator reviews pending accounts and either approves — setting `status = 'active'`, assigning `role`, stamping `approved_at` and `approved_by` — or disables. Both write a `user_audit` row. Approval is the only path from `pending` to a role that can write; there is no auto-approval rule, and deliberately no allowlist-by-email-domain, because a domain match is not evidence that a particular person should be labelling this dataset.

### Password reset

`POST /api/auth/forgot-password` takes an email address.

1. Always respond `200` with a generic "if that address has an account, a reset link is on its way". Never reveal whether the address exists.
2. If it does exist: invalidate that user's outstanding unused `password_reset` tokens, mint a new one (30-minute expiry), and hand it to the mailer.

`POST /api/auth/reset-password` takes the token and a new password.

1. Hash the presented token and look it up; reject if unknown, expired, or already used.
2. Validate the new password against the policy.
3. Write the new hash, stamp `used_at`, clear `failed_logins` and `locked_until` (a successful reset means the legitimate owner is back).
4. **Revoke every existing session for that user.** If the reset happened because the account was compromised, leaving the attacker's session alive defeats the entire exercise.

Reset tokens are single-use and single-purpose: a `password_reset` token cannot be redeemed at the email-verification endpoint or vice versa, because `purpose` is checked alongside the hash.

### The mailer

`app/mailer.py` exposes one function — send a templated message to an address — behind two backends selected by configuration:

- **`smtp`** — stdlib `smtplib` and `email.message`, pointed at whatever relay is configured. No new dependency.
- **`manual`** — the default, used while no relay is configured. The message is not sent; the token row records `delivered_via = 'manual'`, and the link surfaces in the admin UI for an administrator to pass to the person out of band.

This is the part of the request that has no infrastructure behind it yet: the project currently has no SMTP relay, and this design does not invent one. The manual backend makes the feature usable from day one and means switching on email later is a configuration change, not a rewrite. Reset links shown in the admin UI are visible only to administrators and disappear once used or expired.

---

## 5. Authorization model

### Roles

| Capability | viewer | reviewer | admin |
|---|:--:|:--:|:--:|
| Read dashboard, live detections, charts, exports | ✓ | ✓ | ✓ |
| `POST /api/corrections`, spec 2 review endpoints | | ✓ | ✓ |
| `POST /api/alerts/{id}/ack` | | | ✓ |
| `POST /api/admin/negative_regions` | | | ✓ |
| Sheet refresh | | | ✓ |
| Manage users, revoke sessions | | | ✓ |
| Dataset export (spec 2) | | | ✓ |

Anonymous visitors have viewer capabilities without an account. The `viewer` role is what a self-registered account holds before approval, and what an approved account can be deliberately held at.

Authorisation is the pair `(status, role)`, and `status` is checked first: `disabled` is refused everywhere, `pending` is treated as `viewer` regardless of any role value, and only `active` lets the role table above apply. Checking status first means an administrator revoking access never has to also remember to downgrade the role.

### The dependency

`require_role(*allowed)` returns a FastAPI dependency that:

1. Reads the `wb_session` cookie.
2. Loads the session joined to its user in one query.
3. Rejects if missing, revoked, past `expires_at`, or the user's `status` is `disabled`.
4. Rejects if the user's role is not in `allowed`.
5. Refreshes `last_seen_at` subject to the 60-second throttle.
6. Returns a `CurrentUser` dataclass (`id`, `username`, `display_name`, `role`, `session_id`) that handlers accept as a parameter, so stamping `user_id` and `session_id` onto a correction requires no second lookup.

Status codes: `401` when unauthenticated, `403` when authenticated with the wrong role. HTML routes redirect to `/login?next=<path>`; JSON routes return a JSON body so existing fetch code can branch on it.

### Retrofit of existing routes

The four routes currently carrying `Depends(require_write_token)`:

| Route | New guard |
|---|---|
| `POST /api/corrections` (main.py:1162) | `require_role("reviewer", "admin")` |
| Sheet refresh (main.py:~1259) | `require_role("admin")` |
| `POST /api/admin/negative_regions` (main.py:~1427) | `require_role("admin")` |
| `POST /api/alerts/{id}/ack` (main.py:1552) | `require_role("admin")` |

`require_write_token`, the `DASHBOARD_WRITE_TOKEN` config field, and the localStorage token-paste flow in `app.js` are removed. The token is unset in production, so nothing operational depends on it.

### Endpoint surface added

| Method | Path | Guard |
|---|---|---|
| POST | `/api/auth/login` | public (rate limited) |
| POST | `/api/auth/logout` | any authenticated |
| GET | `/api/auth/me` | public; returns `{authenticated: false}` when anonymous |
| GET | `/login` | public (HTML) |
| GET | `/register` | public (HTML) |
| POST | `/api/auth/register` | public (rate limited) |
| GET | `/verify-email` | public (HTML; consumes `?token=`) |
| POST | `/api/auth/verify-email` | public (token-bearing) |
| GET | `/forgot-password` | public (HTML) |
| POST | `/api/auth/forgot-password` | public (rate limited) |
| GET | `/reset-password` | public (HTML; carries `?token=`) |
| POST | `/api/auth/reset-password` | public (token-bearing) |
| POST | `/api/admin/users/{id}/approve` | `admin` |
| GET | `/admin/users.html` | `admin` (HTML) |
| GET/POST | `/api/admin/users` | `admin` |
| POST | `/api/admin/users/{id}/role` | `admin` |
| POST | `/api/admin/users/{id}/deactivate` | `admin` |
| GET | `/api/admin/sessions` | `admin` |
| POST | `/api/admin/sessions/{id}/revoke` | `admin` |
| GET | `/review` | `reviewer`, `admin` (HTML; contents are spec 2) |

`GET /api/auth/mode` is replaced by `GET /api/auth/me`. It is an internal endpoint consumed only by this project's own `app.js`, which is updated in the same change.

### CSRF

`SameSite=Lax` prevents the cookie from riding along on cross-site form posts. Every `POST` under `/api` additionally requires the header `X-Requested-With: wahoobay`, which a cross-origin HTML form cannot set and which a cross-origin `fetch` cannot set without triggering a preflight the server does not answer.

**This requirement includes `/api/auth/login`.** The login page sets the header from its own JavaScript. Requiring it there too means an attacker cannot silently force a victim's browser into an account the attacker controls, which would otherwise let them harvest whatever that victim subsequently annotated.

**This reasoning depends on the dashboard having no CORS middleware configured** — verified absent as of this design. Adding `CORSMiddleware` with permissive origins would invalidate the custom-header defence and require a synchroniser-token scheme instead. Anyone introducing CORS to this service must revisit this section.

---

## 6. Code organisation

`services/dashboard/app/main.py` is already 2,417 lines and `static/app.js` 2,947. This work adds modules rather than extending those files:

```
services/dashboard/app/
    auth.py             # hashing, password policy, token mint/verify,
                        # session CRUD, require_role
    mailer.py           # smtp | manual backends, message templates
    routes_auth.py      # APIRouter: /api/auth/*, /login, /register,
                        # /verify-email, /forgot-password, /reset-password
    routes_admin_users.py  # APIRouter: /admin/users.html, /api/admin/users*,
                        # approve, sessions
    common_passwords.txt   # small built-in denylist for the password policy
    templates/
        login.html
        register.html
        forgot_password.html
        reset_password.html
        users_admin.html
    static/
        auth.js         # login/register/reset forms, identity chip,
                        # pending banner, 401/403 handling
scripts/
    create_user.py      # first-admin bootstrap, and ongoing CLI account creation
tests/
    test_auth.py
```

`main.py` changes only by including the routers, adding the session-prune background task, and swapping the four guards. `auth.py` holds no FastAPI imports beyond the dependency helper, so its logic is unit-testable without a client.

---

## 7. User interface

**`/login`** — a Jinja2 template using the existing `style.css`: username, password, submit, a single generic error area, and a hidden `next` field. Links to `/register` and `/forgot-password`.

**`/register`** — username, display name, email, password, password confirmation, with the length requirement stated up front rather than only as a post-submit error. On success the page explains what happens next: verify the email if a link arrives, and wait for an administrator to approve the account before annotating.

**`/forgot-password`** — a single email field, and a response that reads the same whether or not the address has an account.

**`/reset-password`** — reached from the emailed or administrator-supplied link, carrying the token in the query string. New password plus confirmation. On success it states that all existing sessions have been signed out.

**Pending banner** — an authenticated user whose `status` is `pending` sees a persistent banner explaining that the account is awaiting approval, so the absence of annotation controls reads as a known state rather than a broken page.

**Identity chip** — rendered in the dashboard header only when `/api/auth/me` reports an authenticated user: display name, role, logout. Anonymous visitors see the page exactly as it looks today.

**`/admin/users.html`** — follows the existing `regions_admin.html` pattern: table of users with role, status, last login; create-user form; role selector; deactivate button; per-user session list with revoke. Every action posts to the admin API and writes a `user_audit` row.

**`/review`** — served to `reviewer` and `admin`, deliberately not linked from `index.html`. Unauthenticated visitors are redirected to `/login?next=/review` rather than given a 404.

---

## 8. Bootstrap and operations

### First admin

`scripts/create_user.py` reads `DATABASE_URL` from `.env`, takes `--username`, `--display-name`, and `--role`, and prompts for the password through `getpass`. The password is never accepted as a command-line argument: this is a shared machine where any user can read full command lines out of `ps`.

Accounts created by this script are written with `status = 'active'` and `approved_at` set, bypassing the approval queue. Without that, the first administrator would be created as `pending` with nobody in existence who could approve them — a bootstrap deadlock. The script refuses to create an `admin` account non-interactively for the same reason it refuses a password argument.

The script is also the ongoing path for account creation if the admin UI is unavailable, and the recovery path if every administrator is locked out.

### Configuration

Added to `.env.example` following the existing documented-knob style:

| Variable | Default | Meaning |
|---|---|---|
| `DASHBOARD_COOKIE_SECURE` | `true` | Set `false` only for local HTTP testing at `127.0.0.1:18080`. The public tunnel is HTTPS. |
| `DASHBOARD_SESSION_TTL_H` | `12` | Sliding idle timeout. |
| `DASHBOARD_SESSION_ABS_TTL_D` | `7` | Absolute session lifetime. |
| `DASHBOARD_LOGIN_MAX_FAILS` | `8` | Failures before lockout. |
| `DASHBOARD_LOGIN_LOCKOUT_MIN` | `15` | Lockout duration. |
| `DASHBOARD_REGISTRATION_OPEN` | `true` | Set `false` to close self-registration without a deploy. |
| `DASHBOARD_REGISTER_MAX_PER_IP_H` | `5` | Registration attempts per IP per hour. |
| `DASHBOARD_RESET_TTL_MIN` | `30` | Password-reset token lifetime. |
| `DASHBOARD_VERIFY_TTL_H` | `24` | Email-verification token lifetime. |
| `DASHBOARD_MAIL_BACKEND` | `manual` | `smtp` once a relay exists; `manual` surfaces links in the admin UI. |
| `DASHBOARD_SMTP_HOST` / `_PORT` / `_USER` / `_PASSWORD` / `_FROM` / `_STARTTLS` | unset | Only read when the backend is `smtp`. |

`DASHBOARD_REGISTRATION_OPEN` exists because the realistic failure mode for a public signup form is a wave of junk accounts, and the response to that should be a config flip and a restart, not an emergency code change.

### Background task

A daily task deletes sessions past `expires_at` or revoked more than 30 days ago, and `user_tokens` rows that are expired or were used more than 7 days ago. It runs alongside the existing matview refresh (60 s) and sheet poller. Expired reset tokens are worthless, but they are still a list of who asked to reset and when, so they do not accumulate indefinitely.

### Deploy

Picking up the new code requires restarting the dashboard process, which has run continuously since 2026-05-17. The Cloudflare tunnel targets `localhost:18080` and reconnects by itself, so the outage is a few seconds — but it should be a deliberate, announced restart rather than a surprise during an annotation session.

Order of operations:
1. Apply the DDL to the live database via `psql` (additive; safe while the old code runs).
2. Create the first admin account with the CLI script.
3. Deploy the code and restart the dashboard.
4. Verify: anonymous read still works, anonymous write is refused, admin login succeeds.

### Rollback

Schema changes are additive, so rollback is restoring the previous code and restarting. The tables can stay; nothing else reads them.

---

## 9. Testing

Following the existing `tests/test_dashboard_stats.py` pattern — pytest, `sys.path` insert to import the `app` package without installing it.

**Unit (`tests/test_auth.py`), no database or client:**
- scrypt hash then verify returns true; wrong password returns false.
- A hash string written with different cost parameters still verifies, proving parameters are read from the stored value.
- Hash output differs across calls with the same password (salt is random).
- The role matrix: for each role and each guarded route, the expected allow/deny.
- Session expiry logic: sliding window, absolute cap, revoked session, disabled user.
- Lockout: counter increments, threshold sets `locked_until`, success resets both.
- `last_seen_at` throttle returns "no write needed" inside the 60-second window.
- Password policy: under-length rejected, username or email local-part embedded in the password rejected, a denylisted password rejected, a long passphrase with no symbols accepted.
- Token lifecycle: a token verifies once; a second redemption fails; an expired token fails; a `password_reset` token presented at the email-verification endpoint fails on `purpose` even though the hash matches.
- `(status, role)` resolution: `pending` resolves to viewer capability regardless of the stored role, `disabled` is refused everywhere, `active` honours the role.

**Integration (`TestClient`, against a test database):**
- Login sets an `HttpOnly` cookie; the raw token never appears in any response body.
- Protected route: 401 anonymous, 403 wrong role, 200 correct role.
- Logout revokes; the same cookie is then refused.
- An expired session is refused.
- Public read endpoints return 200 with no cookie — the regression test for the thing this design most needs to not break.
- Registration creates a `pending` account that can log in, and whose correction POST is refused with 403 until approval; after approval with role `reviewer`, the same POST succeeds. This is the test that protects the training set.
- Registering with an existing username and with an existing email both return the same body and status as a fresh registration, and neither creates a second account.
- Reset: requesting for an unknown address and a known address return identical responses; redeeming a token changes the password, invalidates all of that user's sessions, and cannot be redeemed twice.
- With the `manual` mail backend, no SMTP connection is attempted and the token is retrievable through the admin API.

CI already smoke-imports every module, so the new modules are covered there without additional wiring.

---

## 10. What spec 2 builds on this

Spec 2 (reviewer annotation UI and verified training dataset) depends on exactly three things from this spec: the `CurrentUser` object supplied by `require_role`, the `user_id` and `session_id` columns on `detection_corrections`, and the `/review` route being served to authorized users. Decisions already taken with the user and carried into spec 2:

- The review queue is **species-coverage-first**, prioritising classes thinnest in `data/fine_tune/` (223 classes, 12,531 crops).
- The unit of verification is **per-box**, with an optional "every fish in this frame is boxed" promotion that makes a frame usable as detector training data.

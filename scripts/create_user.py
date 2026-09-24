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
    # Spec §3: a new account must never arrive holding write access by
    # default, whoever created it — including via this script.
    ap.add_argument("--role", default="viewer", choices=("viewer", "reviewer", "admin"))
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

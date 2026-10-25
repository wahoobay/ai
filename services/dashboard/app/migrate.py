"""Bring the database to the current schema (Alembic head).

    python -m app.migrate            the CLI (compose `migrate` service, launchers)
    app.migrate.handler(event, ctx)  the migrate Lambda (infra: ai-<location>-<env>-migrate)

The database is named by the environment (DATABASE_URL or the DB_* parts), the
same way the dashboard itself finds it.
"""

from __future__ import annotations

import os
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory


def alembic_ini() -> Path:
    here = Path(__file__).resolve()
    candidates = [Path(p) for p in (os.environ.get("ALEMBIC_INI"),) if p]
    candidates += [
        here.parents[1] / "db" / "alembic.ini",  # the image: /app/app/migrate.py -> /app/db
        here.parents[3]
        / "db"
        / "alembic.ini",  # the repository: services/dashboard/app -> <root>/db
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("db/alembic.ini not found next to the application; set ALEMBIC_INI")


def upgrade(revision: str = "head") -> str:
    """Apply every revision up to `revision`; return the head revision id."""
    cfg = Config(str(alembic_ini()))
    command.upgrade(cfg, revision)
    return ScriptDirectory.from_config(cfg).get_current_head()


def handler(event, context):
    return {"revision": upgrade()}


def main(argv: list[str] | None = None) -> int:
    print(f"schema at revision {upgrade()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

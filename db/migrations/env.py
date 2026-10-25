"""Alembic environment: one database, named by the environment.

DATABASE_URL, or the DB_HOST / DB_PORT / DB_NAME / DB_USER / DB_PASSWORD parts
the cloud injects, exactly as the services read them (dashboard app.config).
"""

from __future__ import annotations

import sys
from pathlib import Path

from alembic import context
from sqlalchemy import create_engine, pool

HERE = Path(__file__).resolve().parent  # <repo>/db/migrations or /app/db/migrations


def _database_url() -> str:
    try:
        from app.config import database_url_from_env  # the dashboard image: PYTHONPATH=/app
    except ImportError:
        sys.path.insert(0, str(HERE.parents[1] / "services" / "dashboard"))  # the repository
        from app.config import database_url_from_env
    url = database_url_from_env()
    # SQLAlchemy's default postgresql driver is psycopg2; the services use psycopg 3.
    return url.replace("postgresql://", "postgresql+psycopg://", 1)


if context.is_offline_mode():
    raise SystemExit("offline (--sql) mode is not supported: migrations run against a database")

engine = create_engine(_database_url(), poolclass=pool.NullPool)
with engine.connect() as connection:
    context.configure(connection=connection, target_metadata=None)
    with context.begin_transaction():
        context.run_migrations()

"""Baseline: the schema db/init.sql built (tables, indexes, views, materialized views).

Revision ID: 0001
Revises: None
"""

from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

SQL = Path(__file__).with_suffix(".sql")


def upgrade() -> None:
    # The whole file in one call on the driver connection itself: it is many
    # statements (DO $$ blocks included) and its comments contain "%", which
    # psycopg would read as placeholders if any parameters travelled with it.
    # Same connection, same transaction as the rest of the migration.
    with op.get_bind().connection.driver_connection.cursor() as cur:
        cur.execute(SQL.read_text())


def downgrade() -> None:
    raise RuntimeError("the baseline is not reversible; drop the database instead")

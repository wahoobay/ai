"""Every table with collected data carries the location it was collected at.

Revision ID: 0002
Revises: 0001

The default is the baseline's fill: every row that exists today was collected at
Wahoo Bay. New rows are stamped from LOCATION_ID by the writers. Adding a column
with a constant default is a catalog-only change in PostgreSQL 11+; the indexes
are built CONCURRENTLY so a live writer is never blocked.
"""

from __future__ import annotations

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

WITH_TS = (
    "detection_events",
    "frame_stats",
    "saved_frames",
    "sensor_readings",
    "weather_readings",
    "ptz_states",
    "review_items",
)
TABLES = (*WITH_TS, "detection_corrections")


def upgrade() -> None:
    for table in TABLES:
        op.execute(
            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS location_id TEXT NOT NULL DEFAULT 'wahoobay'"
        )
    with op.get_context().autocommit_block():
        for table in WITH_TS:
            op.execute(
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {table}_location_ts "
                f"ON {table} (location_id, ts DESC)"
            )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for table in WITH_TS:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {table}_location_ts")
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS location_id")

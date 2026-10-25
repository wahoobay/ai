"""What the periodic work and the pollers report: job_runs and poller_status.

Revision ID: 0003
Revises: 0002

job_runs: one row per run of a job (refresh_matviews, alert_rules, migrate),
so the dashboard can show when things last happened instead of running them.
poller_status: one row per poller, written by the pollers; the alert rule
poller_probe_stale reads it instead of calling a poller over HTTP.
"""

from __future__ import annotations

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS job_runs (
            id      BIGSERIAL PRIMARY KEY,
            job     TEXT        NOT NULL,
            ran_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            ok      BOOLEAN     NOT NULL,
            detail  JSONB       NOT NULL DEFAULT '{}'::jsonb
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS job_runs_job_ran_at ON job_runs (job, ran_at DESC)")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS poller_status (
            name        TEXT        PRIMARY KEY,
            last_ok     TIMESTAMPTZ,
            last_error  TEXT        NOT NULL DEFAULT '',
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS poller_status")
    op.execute("DROP TABLE IF EXISTS job_runs")

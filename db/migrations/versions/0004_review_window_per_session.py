"""The review date window is each reviewer's own, for the session; nothing is stored.

Revision ID: 0004
Revises: 0003

The shared, stored window (review_settings key 'window') decided for everyone what the pool
admitted and retired open items outside it; left at a seeded sample's dates it silently emptied
the staging queue (2026-10-27). The window now travels with each claim request and is kept in
the reviewer's browser session. The stored setting goes, and the first-pass items it retired
come back into the queue.
"""

from __future__ import annotations

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

REOPEN_SQL = """
UPDATE review_items SET status = 'open', retired_reason = NULL
 WHERE status = 'retired' AND retired_reason = 'outside_window' AND pass = 1
"""


def upgrade() -> None:
    op.execute(REOPEN_SQL)
    op.execute("DELETE FROM review_settings WHERE key = 'window'")


def downgrade() -> None:
    pass  # the old window is a setting an admin re-creates; the reopened items stay open

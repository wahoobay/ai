"""A species a reviewer adds carries the WoRMS record it was checked against.

Revision ID: 0007
Revises: 0006

Reviewers' additions are no longer approved by an admin (2026-11-21): the reviewer's browser
checks the name against WoRMS, and the server inserts the row already approved, so it joins
everyone's list at once. The row keeps the WoRMS record chosen, its aphia_id and family, which
the species list shows for it. Rows left over from 0006 have neither.

The downgrade drops the two columns.
"""

from __future__ import annotations

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE species_suggestions ADD COLUMN aphia_id INTEGER, ADD COLUMN family TEXT"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE species_suggestions DROP COLUMN IF EXISTS aphia_id, "
        "DROP COLUMN IF EXISTS family"
    )

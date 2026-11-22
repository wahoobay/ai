"""An answer marked with the rest of a frame carries label_source 'bulk'.

Revision ID: 0005
Revises: 0004

Whole-frame review gained "mark the rest" (2026-11-21): one action answers every unanswered
box in a frame with the species the volunteer just gave. Those answers are stored with their
own label source, so the spot-check agreement on them can be read apart from boxes answered one
by one. The CHECK on detection_corrections.label_source gains the value.

The downgrade restores the three-value CHECK; it fails while any 'bulk' row exists.
"""

from __future__ import annotations

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

NAME = "detection_corrections_label_source_check"


def _check(values: tuple[str, ...]) -> str:
    listed = ", ".join(f"'{v}'" for v in values)
    return (
        f"ALTER TABLE detection_corrections DROP CONSTRAINT IF EXISTS {NAME}, "
        f"ADD CONSTRAINT {NAME} CHECK (label_source IN ({listed}))"
    )


def upgrade() -> None:
    op.execute(_check(("model_guess", "hint", "search", "bulk")))


def downgrade() -> None:
    op.execute(_check(("model_guess", "hint", "search")))

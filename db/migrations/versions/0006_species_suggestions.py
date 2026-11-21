"""Reviewers suggest a species that is not on the list; an admin decides.

Revision ID: 0006
Revises: 0005

A reviewer who meets a fish the species list lacks can suggest it (2026-11-21). The suggestion
is a row in species_suggestions, pending until an admin approves it (the approved_* columns
record what it was added as) or rejects it (rejected_to_* record the listed species it was
mapped to). An answer that uses a suggestion has label_source 'suggested' and points at it with
detection_corrections.species_suggestion_id; ON DELETE RESTRICT keeps a used suggestion from
vanishing under its answers.

The downgrade drops the column, the index and the table and restores the 'bulk' CHECK; it fails
while any 'suggested' row exists.
"""

from __future__ import annotations

from alembic import op

revision = "0006"
down_revision = "0005"
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
    op.execute("""
        CREATE TABLE species_suggestions (
            id               BIGSERIAL PRIMARY KEY,
            common_name      TEXT        NOT NULL,
            scientific_name  TEXT,
            life_stage       TEXT,
            note             TEXT,
            suggested_by     BIGINT      NOT NULL REFERENCES users(id),
            created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            status           TEXT        NOT NULL DEFAULT 'pending'
                             CHECK (status IN ('pending', 'approved', 'rejected')),
            decided_by       BIGINT      REFERENCES users(id),
            decided_at       TIMESTAMPTZ,
            decision_note    TEXT,
            approved_name    TEXT,
            approved_common  TEXT,
            approved_stage   TEXT,
            rejected_to_name  TEXT,
            rejected_to_stage TEXT,
            location_id      TEXT        NOT NULL DEFAULT 'wahoobay'
        )
        """)
    op.execute(
        "CREATE INDEX species_suggestions_status_idx ON species_suggestions (status, created_at)"
    )
    op.execute(
        "ALTER TABLE detection_corrections ADD COLUMN species_suggestion_id BIGINT "
        "REFERENCES species_suggestions(id) ON DELETE RESTRICT"
    )
    op.execute(
        "CREATE INDEX detection_corrections_species_suggestion_idx "
        "ON detection_corrections (species_suggestion_id) WHERE species_suggestion_id IS NOT NULL"
    )
    op.execute(_check(("model_guess", "hint", "search", "bulk", "suggested")))


def downgrade() -> None:
    op.execute(_check(("model_guess", "hint", "search", "bulk")))
    op.execute("DROP INDEX IF EXISTS detection_corrections_species_suggestion_idx")
    op.execute("ALTER TABLE detection_corrections DROP COLUMN IF EXISTS species_suggestion_id")
    op.execute("DROP INDEX IF EXISTS species_suggestions_status_idx")
    op.execute("DROP TABLE IF EXISTS species_suggestions")

"""Past detections take the reviewers' names for two classifier classes.

Revision ID: 0008
Revises: 0007

The live classifier spelled two classes the Species Log's old way (Haemulon chrysargyreum; the
lumped Kyphosus sectatrix/bigibbus) while reviewers label them by their WoRMS accepted names. Dan
chose to rename the model's classes and rewrite the history (2026-11-22,
docs/superpowers/specs/2026-11-22-class-renames-design.md), so the species list, trends and
exports show one row per fish. Rewritten: detection_events.best_name, best_species_id, the name
and species_id of every top-k entry, and review_items.predicted; then both sightings views are
refreshed.

RENAMES is this migration's own frozen copy of services/common/class_renames.py (a migration never
imports code that may change after it ran). The downgrade swaps the names back; it is exact only
while neither new name was a model output before the upgrade, which held on staging when this ran.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

RENAMES = {
    "Haemulon chrysargyreum": "Brachygenys chrysargyrea",
    "Kyphosus sectatrixbigibbus": "Kyphosus",
}

# Each top-k entry's name and species_id are replaced only where they equal the old name; every
# other key, and the order of the list, stay as they were.
_TOPK = sa.text(
    """
    UPDATE detection_events
       SET topk = (
           SELECT jsonb_agg(
                      CASE WHEN e->>'name' = CAST(:old AS text) OR e->>'species_id' = CAST(:old AS text)
                           THEN e
                                || CASE WHEN e->>'name' = CAST(:old AS text)
                                        THEN jsonb_build_object('name', CAST(:new AS text)) ELSE '{}'::jsonb END
                                || CASE WHEN e->>'species_id' = CAST(:old AS text)
                                        THEN jsonb_build_object('species_id', CAST(:new AS text)) ELSE '{}'::jsonb END
                           ELSE e END
                      ORDER BY i)
             FROM jsonb_array_elements(topk) WITH ORDINALITY AS t(e, i))
     WHERE topk @> jsonb_build_array(jsonb_build_object('name', CAST(:old AS text)))
        OR topk @> jsonb_build_array(jsonb_build_object('species_id', CAST(:old AS text)))
    """
)


def _rename(pairs: dict[str, str]) -> None:
    bind = op.get_bind()
    for old, new in pairs.items():
        names = {"old": old, "new": new}
        bind.execute(
            sa.text("UPDATE detection_events SET best_name = :new WHERE best_name = :old"), names
        )
        bind.execute(
            sa.text(
                "UPDATE detection_events SET best_species_id = :new WHERE best_species_id = :old"
            ),
            names,
        )
        bind.execute(_TOPK, names)
        bind.execute(
            sa.text("UPDATE review_items SET predicted = :new WHERE predicted = :old"), names
        )
    # The hourly view rolls up the per-track one, so it refreshes second.
    op.execute("REFRESH MATERIALIZED VIEW species_sightings_mat")
    op.execute("REFRESH MATERIALIZED VIEW species_sightings_hourly_mat")


def upgrade() -> None:
    _rename(RENAMES)


def downgrade() -> None:
    _rename({new: old for old, new in RENAMES.items()})

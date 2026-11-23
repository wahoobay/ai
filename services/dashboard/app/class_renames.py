"""Stored detections renamed through common.class_renames (the rename_classes job).

Migration 0008 renamed every detection stored before it ran (2026-11-22,
docs/superpowers/specs/2026-11-22-class-renames-design.md). CI restarts the worker only after
the migration, so a worker still holding the old class list can write old names in between; this
job renames those, and anything else still carrying an old name. It is idempotent: a second run
changes nothing and refreshes nothing.
"""

from __future__ import annotations

from common.class_renames import CLASS_RENAMES
from psycopg_pool import AsyncConnectionPool

_BEST = """
    UPDATE detection_events
       SET best_name = CASE WHEN best_name = %(old)s THEN %(new)s ELSE best_name END,
           best_species_id = CASE WHEN best_species_id = %(old)s THEN %(new)s ELSE best_species_id END
     WHERE best_species_id = %(old)s OR best_name = %(old)s
"""

# As in migration 0008: each top-k entry's name and species_id change only where they equal the
# old name; other keys and the list's order stay.
_TOPK = """
    UPDATE detection_events
       SET topk = (
           SELECT jsonb_agg(
                      CASE WHEN e->>'name' = %(old)s OR e->>'species_id' = %(old)s
                           THEN e
                                || CASE WHEN e->>'name' = %(old)s
                                        THEN jsonb_build_object('name', %(new)s::text)
                                        ELSE '{}'::jsonb END
                                || CASE WHEN e->>'species_id' = %(old)s
                                        THEN jsonb_build_object('species_id', %(new)s::text)
                                        ELSE '{}'::jsonb END
                           ELSE e END
                      ORDER BY i)
             FROM jsonb_array_elements(topk) WITH ORDINALITY AS t(e, i))
     WHERE topk @> jsonb_build_array(jsonb_build_object('name', %(old)s::text))
        OR topk @> jsonb_build_array(jsonb_build_object('species_id', %(old)s::text))
"""


async def rename_stored(pool: AsyncConnectionPool) -> dict[str, int]:
    """Rename every old class name still stored, in one transaction: {"best": rows whose best
    guess changed, "topk": rows whose top-k list changed}."""
    best = topk = 0
    async with pool.connection() as conn, conn.transaction():
        for old, new in CLASS_RENAMES.items():
            names = {"old": old, "new": new}
            best += (await conn.execute(_BEST, names)).rowcount
            topk += (await conn.execute(_TOPK, names)).rowcount
    return {"best": best, "topk": topk}

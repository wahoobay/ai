"""Classes renamed to the reviewers' names (docs/superpowers/specs/2026-11-22-class-renames-design.md).

The final review (2026-11-22) found a gap: CI runs migration 0008 before it restarts the worker, and
the GPU host keeps the old classes.json on its volume, so detections written in between would keep
the old names for good. The worker therefore names its classes through the table whatever bundle is
on disk, and the rename_classes job renames anything written in the gap.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from common.class_renames import class_names

REPO = Path(__file__).resolve().parents[1]
OLD, NEW = "Haemulon chrysargyreum", "Brachygenys chrysargyrea"


def test_class_names_turn_folders_into_the_names_detections_store():
    entries = [
        "Haemulon_chrysargyreum",
        "Kyphosus_sectatrixbigibbus",
        "Scarus_iseri",
        "Brachygenys_chrysargyrea",
    ]
    assert class_names(entries) == [NEW, "Kyphosus", "Scarus iseri", NEW]


def test_the_worker_names_its_classes_through_the_table():
    """The worker needs torch, which the test environment lacks, so this reads its source: the
    class list it scores against comes from class_names(), not a bare underscore swap."""
    source = (REPO / "services" / "worker" / "app" / "classifier_v2.py").read_text()
    assert "self.class_names: list[str] = class_names(class_names_raw)" in source
    assert "from common.class_renames import class_names" in source


async def _event(db, best: str, topk: list[str], track_id: int, minutes_ago: int) -> int:
    guesses = [{"name": n, "species_id": n, "accuracy": 0.5} for n in topk]
    async with db.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO detection_events (ts, frame_id, source_name, det_conf, bbox, topk, best_name, "
            "best_species_id, best_accuracy, track_id) VALUES (now() - %s * interval '1 minute', 1, 'cam', "
            "0.9, ARRAY[1,2,3,4], %s::jsonb, %s, %s, 0.5, %s) RETURNING id",
            (minutes_ago, json.dumps(guesses), best, best, track_id),
        )
        return (await cur.fetchone())[0]


@pytest.mark.anyio
async def test_rename_classes_renames_what_was_written_in_the_gap(rdb):
    from app.jobs import JOBS

    ids = [await _event(rdb, OLD, [OLD, "Scarus iseri"], 5, m) for m in range(3)]
    below = await _event(rdb, "Scarus iseri", ["Scarus iseri", OLD], 6, 0)
    first = await JOBS["rename_classes"](rdb)
    assert first == {"best": 3, "topk": 4}
    async with rdb.connection() as conn:
        rows = await (
            await conn.execute(
                "SELECT best_species_id, topk->0->>'species_id', topk->1->>'name' FROM detection_events "
                "WHERE id = ANY(%s) ORDER BY id",
                (ids + [below],),
            )
        ).fetchall()
        sightings = await (
            await conn.execute("SELECT DISTINCT species_id FROM species_sightings_mat")
        ).fetchall()
    assert rows == [(NEW, NEW, "Scarus iseri")] * 3 + [("Scarus iseri", "Scarus iseri", NEW)]
    assert sorted(sightings) == sorted([(NEW,), ("Scarus iseri",)])
    assert await JOBS["rename_classes"](rdb) == {"best": 0, "topk": 0}

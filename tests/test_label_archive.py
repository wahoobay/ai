"""The label archive keeps the listed review accounts' work outside staging
(spec infra docs/superpowers/specs/2026-11-18-staging-label-archive-design.md)."""

from __future__ import annotations

import csv
import io

import pytest
from app.exports import export_labeled_corrections
from review_fixtures import add_verdict, make_user, seed_frame

pytestmark = pytest.mark.anyio

TWO_BOXES = [
    {"bbox": [100, 100, 300, 250], "species": "Seriola zonata"},
    {"bbox": [400, 100, 600, 250], "species": "Seriola zonata"},
]


async def _rows(stream) -> list[dict]:
    text = "".join([chunk async for chunk in stream])
    return list(csv.DictReader(io.StringIO(text)))


async def test_labeled_corrections_can_be_limited_to_accounts(rdb, tmp_path):
    frame = await seed_frame(rdb, tmp_path, boxes=TWO_BOXES)
    alice = await make_user(rdb, "alice")
    bob = await make_user(rdb, "bob")
    await add_verdict(rdb, frame["event_ids"][0], alice, name="Seriola zonata")
    await add_verdict(rdb, frame["event_ids"][1], bob, name="Seriola zonata")

    only = await _rows(export_labeled_corrections(rdb, include_identity=True, user_ids=[alice]))
    both = await _rows(export_labeled_corrections(rdb, include_identity=True))

    assert [r["user_id"] for r in only] == [str(alice)]
    assert len(both) == 2
    assert list(only[0]) == list(both[0])

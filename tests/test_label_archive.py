"""The label archive keeps the listed review accounts' work outside staging
(spec infra docs/superpowers/specs/2026-11-18-staging-label-archive-design.md)."""

from __future__ import annotations

import csv
import gzip
import io
import json
from datetime import timedelta

import pytest
from app.exports import export_labeled_corrections
from app.label_archive import UnknownAccount, collect, content_hash, slug
from review_fixtures import T0, add_item, add_verdict, make_user, seed_frame

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


def _lines(data: bytes) -> list[dict]:
    return [json.loads(line) for line in gzip.decompress(data).decode().splitlines()]


def _text(name: str, data: bytes) -> str:
    return (gzip.decompress(data) if name.endswith(".gz") else data).decode()


async def _alice_with_work(rdb, tmp_path) -> dict:
    """Alice: two verdicts, an added box, a skip and a signed-in session. Bob: one verdict."""
    frame = await seed_frame(rdb, tmp_path, boxes=TWO_BOXES)
    alice = await make_user(rdb, "alice")
    bob = await make_user(rdb, "bob")
    await add_verdict(rdb, frame["event_ids"][0], alice, name="Seriola zonata")
    await add_verdict(rdb, frame["event_ids"][1], alice, not_a_fish=True)
    bobs = await add_verdict(rdb, frame["event_ids"][1], bob, name="Seriola zonata")
    item = await add_item(rdb, frame, 0)
    async with rdb.connection() as conn:
        await conn.execute(
            """INSERT INTO review_added_boxes (saved_frame_id, bbox, outcome, label, user_id)
               VALUES (%s, %s, 'species', 'Seriola zonata', %s)""",
            (frame["saved_frame_id"], [10, 10, 50, 50], alice),
        )
        await conn.execute(
            "INSERT INTO review_skips (item_id, user_id) VALUES (%s, %s)", (item, alice)
        )
        await conn.execute(
            """INSERT INTO user_sessions (token_hash, user_id, expires_at)
               VALUES ('session-hash-of-alice', %s, now() + interval '1 day')""",
            (alice,),
        )
    return {"frame": frame, "alice": alice, "bob": bob, "bobs_verdict": bobs}


async def test_collect_gathers_only_that_accounts_work(rdb, tmp_path):
    seeded = await _alice_with_work(rdb, tmp_path)

    snap = await collect(rdb, "alice")

    assert snap.slug == "alice"
    assert snap.counts == {"verdicts": 2, "review_added_boxes": 1, "review_skips": 1}
    assert set(snap.files) == {
        "account.json",
        "verdicts.jsonl.gz",
        "tables/review_added_boxes.jsonl.gz",
        "tables/review_skips.jsonl.gz",
        "labeled_corrections.csv",
        "frames.txt",
    }
    lines = _lines(snap.files["verdicts.jsonl.gz"])
    assert all(set(line) == {"verdict", "event", "saved_frame"} for line in lines)
    assert seeded["bobs_verdict"] not in {line["verdict"]["id"] for line in lines}
    csv_ids = {
        r["correction_id"]
        for r in csv.DictReader(io.StringIO(snap.files["labeled_corrections.csv"].decode()))
    }
    assert csv_ids == {str(line["verdict"]["id"]) for line in lines}
    assert seeded["frame"]["image_path"] in snap.frame_paths
    assert snap.files["frames.txt"].decode().splitlines() == sorted(set(snap.frame_paths))


async def test_collect_never_stores_email_password_or_tokens(rdb, tmp_path):
    await _alice_with_work(rdb, tmp_path)
    async with rdb.connection() as conn:
        cur = await conn.execute("SELECT password_hash FROM users WHERE username = 'alice'")
        password_hash = (await cur.fetchone())[0]

    snap = await collect(rdb, "alice")

    account = json.loads(snap.files["account.json"])
    assert set(account) == {"id", "username", "display_name", "role", "status", "created_at"}
    for name, data in snap.files.items():
        text = _text(name, data)
        assert "alice@example.org" not in text, name
        assert password_hash not in text, name
        assert "session-hash-of-alice" not in text, name
    assert not any(n.startswith(("tables/user_sessions", "tables/user_tokens")) for n in snap.files)


async def test_the_frame_is_matched_on_its_timestamp_too(rdb, tmp_path):
    # After a worker restart frame_id starts again at 1, so two saved frames share it.
    await seed_frame(rdb, tmp_path, ts=T0, frame_id=1000, boxes=TWO_BOXES[:1])
    later = await seed_frame(
        rdb, tmp_path, ts=T0 + timedelta(hours=1), frame_id=1000, boxes=TWO_BOXES[:1]
    )
    alice = await make_user(rdb, "alice")
    await add_verdict(rdb, later["event_ids"][0], alice, name="Seriola zonata")

    snap = await collect(rdb, "alice")

    [line] = _lines(snap.files["verdicts.jsonl.gz"])
    assert line["saved_frame"]["ts"] == line["event"]["ts"]
    assert snap.frame_paths[0] == later["image_path"]


async def test_an_unknown_account_is_an_error(rdb):
    with pytest.raises(UnknownAccount):
        await collect(rdb, "nobody")


async def test_unchanged_work_hashes_the_same(rdb, tmp_path):
    seeded = await _alice_with_work(rdb, tmp_path)

    first = content_hash((await collect(rdb, "alice")).files)
    again = content_hash((await collect(rdb, "alice")).files)
    await add_verdict(rdb, seeded["frame"]["event_ids"][0], seeded["alice"], not_a_fish=True)
    changed = content_hash((await collect(rdb, "alice")).files)

    assert first == again
    assert changed != first


def test_slug():
    assert slug("roy camop in germany") == "roy-camop-in-germany"
    assert slug("  A.B__c ") == "a-b-c"

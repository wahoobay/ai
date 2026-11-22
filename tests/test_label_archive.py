"""The label archive keeps the listed review accounts' work outside staging
(spec infra docs/superpowers/specs/2026-11-18-staging-label-archive-design.md)."""

from __future__ import annotations

import csv
import gzip
import io
import json
import os
from datetime import timedelta

import psycopg
import pytest
from app import label_archive
from app.exports import export_labeled_corrections
from app.label_archive import (
    ARCHIVED_TABLES,
    NOT_ARCHIVED,
    UnknownAccount,
    archive_labels,
    collect,
    content_hash,
    read_only_transaction,
    slug,
)
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


# ----- the archive_labels job: snapshots and frames under LABEL_ARCHIVE_URL -----


def _archive(monkeypatch, tmp_path, accounts=("alice",)):
    root = tmp_path / "archive"
    monkeypatch.delenv("FRAMES_URL", raising=False)  # the fixtures' frames are absolute paths
    monkeypatch.setenv("LABEL_ARCHIVE_URL", f"file://{root}")
    monkeypatch.setenv("LABEL_ARCHIVE_ACCOUNTS", json.dumps(list(accounts)))
    return root / "alice"


async def _alice_with_frame_files(rdb, tmp_path) -> dict:
    seeded = await _alice_with_work(rdb, tmp_path)
    image = seeded["frame"]["image_path"]
    with open(image + ".coco.json", "w") as f:  # the worker saves a COCO sidecar with each frame
        f.write("{}")
    return seeded


def _snapshots(base) -> list:
    return sorted(p for p in (base / "snapshots").iterdir() if p.is_dir())


async def test_first_run_writes_a_snapshot_and_copies_frames(rdb, tmp_path, monkeypatch):
    base = _archive(monkeypatch, tmp_path)
    seeded = await _alice_with_frame_files(rdb, tmp_path)

    out = await archive_labels(rdb)

    [snap] = _snapshots(base)
    assert {p.relative_to(snap).as_posix() for p in snap.rglob("*") if p.is_file()} == {
        "account.json",
        "verdicts.jsonl.gz",
        "tables/review_added_boxes.jsonl.gz",
        "tables/review_skips.jsonl.gz",
        "labeled_corrections.csv",
        "frames.txt",
        "manifest.json",
    }
    latest = json.loads((base / "latest.json").read_text())
    assert latest["snapshot"] == snap.name
    manifest = json.loads((snap / "manifest.json").read_text())
    assert latest["sha256"] == manifest["sha256"]
    assert manifest["counts"] == {"verdicts": 2, "review_added_boxes": 1, "review_skips": 1}
    assert (base / "frames" / seeded["frame"]["image_path"].lstrip("/")).is_file()
    result = out["accounts"]["alice"]
    assert result["snapshot"] == snap.name and result["unchanged"] is False
    assert (result["frames_copied"], result["frames_missing"]) == (2, 0)


async def test_an_unchanged_account_writes_nothing_new(rdb, tmp_path, monkeypatch):
    base = _archive(monkeypatch, tmp_path)
    await _alice_with_frame_files(rdb, tmp_path)

    await archive_labels(rdb)
    out = await archive_labels(rdb)

    assert out["accounts"]["alice"]["unchanged"] is True
    assert out["accounts"]["alice"]["snapshot"] is None
    assert out["accounts"]["alice"]["frames_copied"] == 0
    assert len(_snapshots(base)) == 1


async def test_a_changed_verdict_keeps_the_earlier_snapshot(rdb, tmp_path, monkeypatch):
    base = _archive(monkeypatch, tmp_path)
    seeded = await _alice_with_frame_files(rdb, tmp_path)
    await archive_labels(rdb)
    [first] = _snapshots(base)
    before = {p: p.read_bytes() for p in first.rglob("*") if p.is_file()}

    async with rdb.connection() as conn:
        await conn.execute(
            "UPDATE detection_corrections SET corrected_name = 'Seriola rivoliana' WHERE user_id = %s",
            (seeded["alice"],),
        )
    monkeypatch.setattr(label_archive, "_stamp", lambda: "2099-01-01T000000Z")
    await archive_labels(rdb)

    snaps = _snapshots(base)
    assert [s.name for s in snaps] == [first.name, "2099-01-01T000000Z"]
    assert {p: p.read_bytes() for p in first.rglob("*") if p.is_file()} == before
    assert json.loads((base / "latest.json").read_text())["snapshot"] == "2099-01-01T000000Z"


async def test_latest_is_written_last(rdb, tmp_path, monkeypatch):
    base = _archive(monkeypatch, tmp_path)
    await _alice_with_frame_files(rdb, tmp_path)
    real_put = label_archive.put

    def put(url, data, content_type):
        if url.endswith("/manifest.json"):
            raise OSError("disk full")
        real_put(url, data, content_type)

    monkeypatch.setattr(label_archive, "put", put)
    with pytest.raises(RuntimeError, match="disk full"):
        await archive_labels(rdb)

    assert not (base / "latest.json").exists()


async def test_a_missing_frame_is_reported_not_fatal(rdb, tmp_path, monkeypatch):
    base = _archive(monkeypatch, tmp_path)
    seeded = await _alice_with_frame_files(rdb, tmp_path)
    image = seeded["frame"]["image_path"]
    os.remove(image)

    out = await archive_labels(rdb)

    assert out["accounts"]["alice"]["frames_missing"] == 1
    [snap] = _snapshots(base)
    assert json.loads((snap / "manifest.json").read_text())["frames_missing"] == [image]


async def test_no_accounts_is_a_no_op(rdb, monkeypatch):
    monkeypatch.delenv("LABEL_ARCHIVE_ACCOUNTS", raising=False)
    monkeypatch.delenv("LABEL_ARCHIVE_URL", raising=False)
    assert await archive_labels(rdb) == {"accounts": {}}


async def test_accounts_without_a_url_is_an_error(rdb, monkeypatch):
    monkeypatch.setenv("LABEL_ARCHIVE_ACCOUNTS", '["alice"]')
    monkeypatch.delenv("LABEL_ARCHIVE_URL", raising=False)
    with pytest.raises(RuntimeError, match="LABEL_ARCHIVE_URL"):
        await archive_labels(rdb)


# ----- final-review fixes ---------------------------------------------------------------


async def test_one_failing_frame_does_not_hold_up_the_snapshot(rdb, tmp_path, monkeypatch):
    base = _archive(monkeypatch, tmp_path)
    seeded = await _alice_with_frame_files(rdb, tmp_path)
    image = seeded["frame"]["image_path"]
    real_open_read = label_archive.open_read

    def open_read(url):
        if url.endswith(image):
            raise PermissionError("AccessDenied")
        return real_open_read(url)

    monkeypatch.setattr(label_archive, "open_read", open_read)
    with pytest.raises(RuntimeError, match="AccessDenied"):
        await archive_labels(rdb)

    [snap] = _snapshots(base)
    assert json.loads((base / "latest.json").read_text())["snapshot"] == snap.name
    assert json.loads((snap / "manifest.json").read_text())["frames_failed"] == [image]


async def test_one_unknown_account_does_not_stop_the_others(rdb, tmp_path, monkeypatch):
    base = _archive(monkeypatch, tmp_path, accounts=("nobody", "alice"))
    await _alice_with_frame_files(rdb, tmp_path)

    with pytest.raises(RuntimeError, match="nobody"):
        await archive_labels(rdb)

    assert (base / "latest.json").is_file()


async def test_a_recreated_account_does_not_move_latest(rdb, tmp_path, monkeypatch):
    # After a staging reset, an account with the same username is a different account:
    # its (new, nearly empty) work must not become the archive's latest.
    base = _archive(monkeypatch, tmp_path)
    await _alice_with_frame_files(rdb, tmp_path)
    await archive_labels(rdb)
    latest = (base / "latest.json").read_text()
    async with rdb.connection() as conn:
        await conn.execute(
            "UPDATE users SET created_at = created_at - interval '1 day' WHERE username = 'alice'"
        )

    with pytest.raises(RuntimeError, match="alice"):
        await archive_labels(rdb)

    assert (base / "latest.json").read_text() == latest
    assert len(_snapshots(base)) == 1


async def test_every_user_table_is_archived_or_deliberately_left_out(rdb):
    # A new table with a user_id column must be a decision: archived (it holds review
    # work) or left out (credentials, contact details). Never archived by default.
    async with rdb.connection() as conn:
        cur = await conn.execute(
            """SELECT c.table_name FROM information_schema.columns c
                 JOIN information_schema.tables t
                   ON t.table_schema = c.table_schema AND t.table_name = c.table_name
                WHERE c.table_schema = 'public' AND c.column_name = 'user_id'
                  AND t.table_type = 'BASE TABLE'"""
        )
        tables = {row[0] for row in await cur.fetchall()}
    assert tables == set(ARCHIVED_TABLES) | set(NOT_ARCHIVED)


async def test_a_frame_known_only_to_its_event_is_archived(rdb, tmp_path):
    frame = await seed_frame(rdb, tmp_path, boxes=TWO_BOXES[:1], save=False)
    async with rdb.connection() as conn:
        await conn.execute(
            "UPDATE detection_events SET image_path = %s WHERE id = %s",
            (frame["image_path"], frame["event_ids"][0]),
        )
    alice = await make_user(rdb, "alice")
    await add_verdict(rdb, frame["event_ids"][0], alice, name="Seriola zonata")

    snap = await collect(rdb, "alice")

    assert snap.frame_paths == [frame["image_path"]]


async def test_relative_frame_paths_resolve_against_frames_url(rdb, tmp_path, monkeypatch):
    # Production stores keys relative to FRAMES_URL, as the worker writes them.
    base = _archive(monkeypatch, tmp_path)
    store = tmp_path / "store"
    key = "wahoobay/2026/10/26/11/20261026T113452Z_f0000526807_seahivecam.jpg"
    coco = key.replace(".jpg", ".json")
    (store / key).parent.mkdir(parents=True)
    (store / key).write_bytes(b"jpeg")
    (store / coco).write_bytes(b"{}")
    monkeypatch.setenv("FRAMES_URL", f"file://{store}")
    seeded = await _alice_with_work(rdb, tmp_path)
    async with rdb.connection() as conn:
        await conn.execute(
            "UPDATE saved_frames SET image_path = %s, coco_path = %s WHERE id = %s",
            (key, coco, seeded["frame"]["saved_frame_id"]),
        )

    out = await archive_labels(rdb)

    assert (base / "frames" / key).read_bytes() == b"jpeg"
    assert (base / "frames" / coco).read_bytes() == b"{}"
    assert (
        out["accounts"]["alice"]["frames_copied"],
        out["accounts"]["alice"]["frames_missing"],
    ) == (2, 0)


async def test_collect_reads_in_a_read_only_transaction(rdb):
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        async with rdb.connection() as conn, read_only_transaction(conn):
            await conn.execute("INSERT INTO job_runs (job, ok, detail) VALUES ('x', true, '{}')")


def test_an_account_is_the_same_whatever_the_sessions_time_zone():
    # created_at is written in the database session's time zone; the identity check must
    # not mistake a configuration change for a new account.
    def snap(created_at):
        account = json.dumps({"id": 7, "created_at": created_at}).encode()
        return label_archive.AccountSnapshot("roy", "roy", {"account.json": account}, [], {})

    utc = label_archive._identity(snap("2026-10-29 22:00:00.5+00:00"))
    eastern = label_archive._identity(snap("2026-10-29 18:00:00.5-04:00"))
    assert utc == eastern


# ----- species reviewers added (ai #49/#50) -------------------------------------------


async def _add_species(
    rdb, by: int, name: str, *, status="approved", stage=None, aphia=None
) -> int:
    async with rdb.connection() as conn:
        cur = await conn.execute(
            """INSERT INTO species_suggestions
                   (common_name, scientific_name, life_stage, suggested_by, status,
                    approved_name, approved_common, approved_stage, aphia_id, decided_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                       CASE WHEN %s = 'pending' THEN NULL ELSE now() END)
               RETURNING id""",
            (
                name,
                name,
                stage,
                by,
                status,
                None if status == "pending" else name,
                name,
                stage,
                aphia,
                status,
            ),
        )
        return (await cur.fetchone())[0]


async def test_collect_archives_the_species_the_account_added_or_used(rdb, tmp_path):
    """A restored archive must be able to name every species the account's answers use: the
    ones it added, and ones another reviewer added that it picked from the list."""
    frame = await seed_frame(rdb, tmp_path, boxes=TWO_BOXES)
    alice = await make_user(rdb, "alice")
    bob = await make_user(rdb, "bob")
    own = await _add_species(rdb, alice, "Pseudupeneus maculatus", aphia=159421)
    used = await _add_species(rdb, bob, "Acanthurus chirurgus", aphia=159578)
    unused = await _add_species(rdb, bob, "Lutjanus apodus", aphia=159782)
    await add_verdict(rdb, frame["event_ids"][0], alice, name="Acanthurus chirurgus")

    snap = await collect(rdb, "alice")

    rows = _lines(snap.files["tables/species_suggestions.jsonl.gz"])
    assert {r["id"] for r in rows} == {own, used}
    assert unused not in {r["id"] for r in rows}
    assert {r["aphia_id"] for r in rows} == {159421, 159578}
    assert snap.counts["species_suggestions"] == 2


async def test_the_training_csv_leaves_out_a_box_waiting_on_a_new_species(rdb, tmp_path):
    """The raw verdicts keep everything; the CSV the training-set builder reads does not
    carry a box whose answer still waits on a #49 suggestion (guards the merge of main's
    hide_pending_species with this branch's user_ids filter)."""
    frame = await seed_frame(rdb, tmp_path, boxes=TWO_BOXES)
    alice = await make_user(rdb, "alice")
    waiting = await _add_species(rdb, alice, "Pseudupeneus maculatus", status="pending")
    pending_answer = await add_verdict(
        rdb, frame["event_ids"][0], alice, name="Pseudupeneus maculatus"
    )
    async with rdb.connection() as conn:
        await conn.execute(
            """UPDATE detection_corrections SET label_source = 'suggested',
                      species_suggestion_id = %s WHERE id = %s""",
            (waiting, pending_answer),
        )
    listed = await add_verdict(rdb, frame["event_ids"][1], alice, name="Seriola zonata")

    snap = await collect(rdb, "alice")

    verdict_ids = {line["verdict"]["id"] for line in _lines(snap.files["verdicts.jsonl.gz"])}
    csv_ids = {
        int(r["correction_id"])
        for r in csv.DictReader(io.StringIO(snap.files["labeled_corrections.csv"].decode()))
    }
    assert verdict_ids == {pending_answer, listed}
    assert csv_ids == {listed}
    assert waiting in {r["id"] for r in _lines(snap.files["tables/species_suggestions.jsonl.gz"])}

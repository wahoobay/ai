"""The verified dataset: the listed reviewers' answers turned into a snapshot for training
(spec docs/superpowers/specs/2026-11-22-verified-dataset-training-design.md)."""

from __future__ import annotations

import pytest
from app import verified_dataset
from review_fixtures import add_item, add_verdict, make_user, seed_frame

pytestmark = pytest.mark.anyio

ONE_BOX = [{"bbox": [100, 100, 300, 250], "species": "Seriola zonata", "p": 0.62}]
THREE_BOXES = [
    {"bbox": [100, 100, 300, 250], "species": "Seriola zonata"},
    {"bbox": [400, 100, 600, 250], "species": "Seriola zonata"},
    {"bbox": [700, 100, 900, 250], "species": "Seriola zonata"},
]


async def _select(db, user_ids):
    async with db.connection() as conn:
        return await verified_dataset.select(conn, list(user_ids))


async def drawn(
    db, frame, uid, bbox, *, outcome="species", label=None, confidence="certain", removed=False
) -> int:
    async with db.connection() as conn:
        cur = await conn.execute(
            """INSERT INTO review_added_boxes (saved_frame_id, bbox, outcome, label, confidence,
                                               user_id, removed_at)
               VALUES (%s, %s, %s, %s, %s, %s, CASE WHEN %s THEN now() END) RETURNING id""",
            (frame["saved_frame_id"], list(bbox), outcome, label, confidence, uid, removed),
        )
        return (await cur.fetchone())[0]


async def complete(db, frame, uid) -> None:
    async with db.connection() as conn:
        await conn.execute(
            "INSERT INTO review_frames (saved_frame_id, completed_by) VALUES (%s, %s)",
            (frame["saved_frame_id"], uid),
        )


async def test_a_single_answer_counts_with_its_label_source(rdb, tmp_path):
    frame = await seed_frame(rdb, tmp_path, boxes=ONE_BOX)
    roy = await make_user(rdb, "roy")
    [eid] = frame["event_ids"]
    await add_verdict(rdb, eid, roy, name="Seriola rivoliana", label_source="bulk")

    [box] = (await _select(rdb, [roy])).boxes

    assert (box.box_id, box.outcome, box.accepted_name, box.status) == (
        f"ev{eid}",
        "species",
        "Seriola rivoliana",
        "single",
    )
    assert (box.label_source, box.bbox, box.bbox_source, box.held_back) == (
        "bulk",
        (100, 100, 300, 250),
        "model",
        None,
    )
    assert (box.model_name, box.model_score, box.user_ids) == ("Seriola zonata", 0.62, (roy,))
    assert (box.saved_frame_id, box.camera, box.ts) == (
        frame["saved_frame_id"],
        "seahivecam",
        frame["ts"],
    )


async def test_other_accounts_answers_are_dropped_before_resolution(rdb, tmp_path):
    frame = await seed_frame(rdb, tmp_path, boxes=THREE_BOXES[:2])
    roy = await make_user(rdb, "roy")
    admin = await make_user(rdb, "admin", role="admin")
    e0, e1 = frame["event_ids"]
    first = await add_item(rdb, frame, 0, pass_=1)
    await add_verdict(rdb, e0, roy, name="Seriola zonata", item_id=first)
    check = await add_item(rdb, frame, 0, pass_=2)
    await add_verdict(rdb, e0, admin, name="Caranx ruber", item_id=check)
    await add_verdict(rdb, e1, admin, name="Caranx ruber")

    sel = await _select(rdb, [roy])

    assert [(b.box_id, b.status, b.accepted_name) for b in sel.boxes] == [
        (f"ev{e0}", "single", "Seriola zonata")
    ]


async def test_a_disputed_box_is_held_back(rdb, tmp_path):
    frame = await seed_frame(rdb, tmp_path, boxes=ONE_BOX)
    roy, amy = await make_user(rdb, "roy"), await make_user(rdb, "amy")
    [eid] = frame["event_ids"]
    await add_verdict(rdb, eid, roy, name="Seriola zonata", item_id=await add_item(rdb, frame, 0))
    check = await add_item(rdb, frame, 0, pass_=2)
    await add_verdict(rdb, eid, amy, name="Caranx ruber", item_id=check)

    [box] = (await _select(rdb, [roy, amy])).boxes

    assert (box.status, box.held_back) == ("disputed", "disputed")


async def test_a_box_naming_a_pending_species_is_held_back(rdb, tmp_path):
    frame = await seed_frame(rdb, tmp_path, boxes=ONE_BOX)
    roy = await make_user(rdb, "roy")
    [eid] = frame["event_ids"]
    vid = await add_verdict(rdb, eid, roy, name="Coryphopterus personatus")
    async with rdb.connection() as conn:
        cur = await conn.execute(
            """INSERT INTO species_suggestions (common_name, scientific_name, suggested_by)
               VALUES ('Masked Goby', 'Coryphopterus personatus', %s) RETURNING id""",
            (roy,),
        )
        sid = (await cur.fetchone())[0]
        await conn.execute(
            "UPDATE detection_corrections SET species_suggestion_id = %s WHERE id = %s", (sid, vid)
        )

    [box] = (await _select(rdb, [roy])).boxes

    assert box.held_back == "species waiting for approval"


async def test_only_probable_or_certain_answers_count(rdb, tmp_path):
    frame = await seed_frame(rdb, tmp_path, boxes=THREE_BOXES)
    roy = await make_user(rdb, "roy")
    for eid, confidence in zip(frame["event_ids"], ("certain", "probable", "uncertain")):
        await add_verdict(rdb, eid, roy, name="Seriola zonata", confidence=confidence)

    sel = await _select(rdb, [roy])

    assert [b.held_back for b in sel.boxes] == [None, None, "confidence uncertain"]


async def test_a_corrected_box_replaces_the_model_box(rdb, tmp_path):
    frame = await seed_frame(rdb, tmp_path, boxes=ONE_BOX)
    roy = await make_user(rdb, "roy")
    await add_verdict(
        rdb, frame["event_ids"][0], roy, name="Seriola zonata", bbox=[110, 105, 290, 240]
    )

    [box] = (await _select(rdb, [roy])).boxes

    assert (box.bbox, box.bbox_source) == ((110, 105, 290, 240), "corrected")


async def test_no_animal_and_cant_identify_are_outcomes(rdb, tmp_path):
    frame = await seed_frame(rdb, tmp_path, boxes=THREE_BOXES[:2])
    roy = await make_user(rdb, "roy")
    e0, e1 = frame["event_ids"]
    await add_verdict(rdb, e0, roy, not_a_fish=True)
    await add_verdict(rdb, e1, roy, unidentifiable=True)

    sel = await _select(rdb, [roy])

    assert [(b.outcome, b.accepted_name, b.held_back) for b in sel.boxes] == [
        ("no_animal", None, None),
        ("unidentifiable", None, None),
    ]


async def test_drawn_boxes_count_and_removed_or_unlisted_ones_do_not(rdb, tmp_path):
    frame = await seed_frame(rdb, tmp_path)
    roy, amy = await make_user(rdb, "roy"), await make_user(rdb, "amy")
    kept = await drawn(rdb, frame, roy, [10, 10, 60, 60], label="Seriola zonata")
    await drawn(rdb, frame, roy, [70, 10, 120, 60], label="Seriola zonata", removed=True)
    await drawn(rdb, frame, amy, [130, 10, 180, 60], label="Seriola zonata")

    sel = await _select(rdb, [roy])

    assert [(b.box_id, b.bbox_source, b.status, b.accepted_name) for b in sel.boxes] == [
        (f"add{kept}", "drawn", "single", "Seriola zonata")
    ]
    assert (sel.boxes[0].bbox, sel.boxes[0].user_ids, sel.boxes[0].label_source) == (
        (10, 10, 60, 60),
        (roy,),
        None,
    )


async def test_sample_events_are_left_out(rdb, tmp_path):
    frame = await seed_frame(rdb, tmp_path, boxes=THREE_BOXES[:2])
    roy = await make_user(rdb, "roy")
    e0, e1 = frame["event_ids"]
    for eid in (e0, e1):
        await add_verdict(rdb, eid, roy, name="Seriola zonata")
    async with rdb.connection() as conn:
        await conn.execute(
            "UPDATE detection_events SET model_version = 'sample' WHERE id = %s", (e1,)
        )

    sel = await _select(rdb, [roy])

    assert [b.box_id for b in sel.boxes] == [f"ev{e0}"] and sel.sample_answers == 1


async def test_frames_know_completion_and_every_model_box(rdb, tmp_path):
    frame = await seed_frame(rdb, tmp_path, boxes=THREE_BOXES)
    roy = await make_user(rdb, "roy")
    await add_verdict(rdb, frame["event_ids"][0], roy, name="Seriola zonata")
    await complete(rdb, frame, roy)

    sel = await _select(rdb, [roy])

    f = sel.frames[frame["saved_frame_id"]]
    assert f.completed and f.model_box_ids == tuple(f"ev{e}" for e in frame["event_ids"])
    assert (f.camera, f.ts, f.image_path) == ("seahivecam", frame["ts"], frame["image_path"])


async def test_an_event_with_two_saved_frame_rows_is_one_box_on_the_lowest(rdb, tmp_path):
    frame = await seed_frame(rdb, tmp_path, boxes=ONE_BOX)
    roy = await make_user(rdb, "roy")
    async with rdb.connection() as conn:
        await conn.execute(
            """INSERT INTO saved_frames (ts, frame_id, source_name, reason, image_path, coco_path,
                                         num_fish)
               SELECT ts, frame_id, source_name, reason, image_path, coco_path, num_fish
                 FROM saved_frames WHERE id = %s""",
            (frame["saved_frame_id"],),
        )
    await add_verdict(rdb, frame["event_ids"][0], roy, name="Seriola zonata")

    sel = await _select(rdb, [roy])

    assert [b.saved_frame_id for b in sel.boxes] == [frame["saved_frame_id"]]
    assert list(sel.frames) == [frame["saved_frame_id"]]


async def test_the_newest_answer_and_drawn_box_ids(rdb, tmp_path):
    frame = await seed_frame(rdb, tmp_path, boxes=THREE_BOXES[:2])
    roy, amy = await make_user(rdb, "roy"), await make_user(rdb, "amy")
    e0, e1 = frame["event_ids"]
    await add_verdict(rdb, e0, roy, name="Seriola zonata")
    v2 = await add_verdict(rdb, e1, roy, name="Seriola zonata")
    await add_verdict(rdb, e1, amy, name="Caranx ruber")
    d1 = await drawn(rdb, frame, roy, [10, 10, 60, 60], label="Seriola zonata")
    await drawn(rdb, frame, amy, [70, 10, 120, 60], label="Seriola zonata")

    sel = await _select(rdb, [roy])

    assert (sel.newest_answer_id, sel.newest_drawn_box_id) == (v2, d1)

"""Whole-frame mode (spec §8)."""
from __future__ import annotations

import io
import json
import random
from datetime import UTC, datetime, timedelta

import pytest
from app.review_catalogue import CatalogueStore
from app.review_frames import (
    AddedBoxIn,
    add_box,
    complete_frame,
    frame_state,
    open_frame,
    remove_box,
    update_box,
)
from app.review_queue import Conflict, Forbidden, Invalid, VerdictIn, submit_verdict
from PIL import Image
from review_fixtures import (
    CSRF,
    add_item,
    add_verdict,
    as_user,
    catalogue_paths,
    login,
    make_user,
    seed_frame,
)

pytestmark = pytest.mark.anyio

BOXES = [{"bbox": [10, 10, 110, 110], "species": "Seriola zonata"},
         {"bbox": [200, 10, 300, 110], "species": "Pomatomus saltarix"},
         {"bbox": [400, 10, 500, 110]},
         {"bbox": [600, 10, 700, 110], "species": "Seriola zonata"}]


@pytest.fixture
def cat(review_assets):
    return CatalogueStore(catalogue_paths(review_assets)).get()


async def _state(rdb, frame, user, cat):
    async with rdb.connection() as conn:
        return await frame_state(conn, saved_frame_id=frame["saved_frame_id"], user=user,
                                 catalogue=cat)


async def test_each_box_shows_what_this_volunteer_may_see(rdb, tmp_path, cat):
    f = await seed_frame(rdb, tmp_path, boxes=BOXES)
    me, other = await make_user(rdb, "me"), await make_user(rdb, "other")
    e = f["event_ids"]
    await add_verdict(rdb, e[0], other, name="Seriola zonata")          # answered, visible
    first = await add_verdict(rdb, e[1], other, name="Pomatomus saltatrix")
    await add_item(rdb, f, 1, pass_=2, exclude_user_id=other, first_correction_id=first)
    await add_item(rdb, f, 2, lane="identify", status="claimed", claimed_by=other,
                   claimed_until=datetime.now(UTC) + timedelta(minutes=5))
    state = await _state(rdb, f, as_user(me), cat)
    by_n = {b["n"]: b for b in state["boxes"]}
    assert by_n[1]["state"] == "answered" and by_n[1]["label"] == "Seriola zonata"
    # Review Focus 2: a box waiting for a second opinion hides its answer
    assert by_n[2]["state"] == "answered_hidden" and by_n[2]["label"] is None
    assert "Pomatomus saltatrix" not in json.dumps(by_n[2])
    assert by_n[3]["state"] == "locked"
    assert by_n[4]["state"] == "needs_answer" and by_n[4]["model"]["name"] == "Seriola zonata"
    assert state["blocking"] == [3, 4]
    # the first reviewer does see their own answer
    theirs = await _state(rdb, f, as_user(other), cat)
    assert theirs["boxes"][1]["state"] == "answered"


async def test_opening_claims_the_frames_open_boxes(rdb, tmp_path, cat):
    f = await seed_frame(rdb, tmp_path, boxes=BOXES)
    me = await make_user(rdb, "me")
    item = await add_item(rdb, f, 0, predicted="Seriola zonata", route="Seriola zonata")
    async with rdb.connection() as conn:
        await open_frame(conn, saved_frame_id=f["saved_frame_id"], user_id=me, claim_s=600)
    state = await _state(rdb, f, as_user(me), cat)
    assert state["boxes"][0]["review_item_id"] == item


async def test_completing_needs_every_model_box_answered(rdb, tmp_path, cat):
    f = await seed_frame(rdb, tmp_path, boxes=BOXES[:2])
    me = await make_user(rdb, "me")
    user = as_user(me)
    async with rdb.connection() as conn:
        with pytest.raises(Conflict, match="answer box 1, 2 first"):
            await complete_frame(conn, saved_frame_id=f["saved_frame_id"], user=user,
                                 catalogue=cat)
    for eid in f["event_ids"]:
        async with rdb.connection() as conn:
            await submit_verdict(conn, user=user, body=VerdictIn(event_id=eid, outcome="accept"),
                                 catalogue=cat, spot_rate=0.0, rng=random.Random(0))
    async with rdb.connection() as conn:
        done = await complete_frame(conn, saved_frame_id=f["saved_frame_id"], user=user,
                                    catalogue=cat)
        again = await complete_frame(conn, saved_frame_id=f["saved_frame_id"], user=user,
                                     catalogue=cat)
    assert done == {"complete": True, "already": False} and again["already"] is True


async def test_drawn_boxes_belong_to_whoever_drew_them(rdb, tmp_path, cat):
    f = await seed_frame(rdb, tmp_path, boxes=[])
    me, other = await make_user(rdb, "me"), await make_user(rdb, "other")
    boss = await make_user(rdb, "boss", role="admin")
    sf = f["saved_frame_id"]
    body = AddedBoxIn(bbox=[20, 20, 120, 90], outcome="species", label="Seriola zonata")
    async with rdb.connection() as conn:
        for bad in (AddedBoxIn(bbox=[20, 20, 22, 90], outcome="unidentifiable"),
                    AddedBoxIn(bbox=[20, 20, 120, 90], outcome="species", label="Nope nope"),
                    AddedBoxIn(bbox=[20, 20, 120, 90], outcome="unidentifiable",
                               label="Seriola zonata")):
            with pytest.raises(Invalid):
                await add_box(conn, saved_frame_id=sf, user=as_user(me), body=bad, catalogue=cat)
        box = await add_box(conn, saved_frame_id=sf, user=as_user(me), body=body, catalogue=cat)
        with pytest.raises(Forbidden):
            await update_box(conn, saved_frame_id=sf, box_id=box["id"], user=as_user(other),
                             body=body, catalogue=cat)
        await update_box(conn, saved_frame_id=sf, box_id=box["id"], user=as_user(me),
                         body=AddedBoxIn(bbox=[20, 20, 130, 95], outcome="unidentifiable"),
                         catalogue=cat)
        await complete_frame(conn, saved_frame_id=sf, user=as_user(me), catalogue=cat)
        with pytest.raises(Conflict):
            await remove_box(conn, saved_frame_id=sf, box_id=box["id"], user=as_user(me))
        await remove_box(conn, saved_frame_id=sf, box_id=box["id"],
                         user=as_user(boss, role="admin"))
    state = await _state(rdb, f, as_user(me), cat)
    assert state["added"] == [] and state["complete"] is not None


async def test_whole_frame_over_http(rdb, rclient, tmp_path):
    await make_user(rdb, "rev")
    login(rclient, "rev")
    f = await seed_frame(rdb, tmp_path, boxes=BOXES[:1])
    sf = f["saved_frame_id"]
    assert rclient.post(f"/api/review/frames/{sf}/open").status_code == 403       # no CSRF
    state = rclient.post(f"/api/review/frames/{sf}/open", headers=CSRF).json()
    assert state["boxes"][0]["state"] == "needs_answer"
    added = rclient.post(f"/api/review/frames/{sf}/boxes", headers=CSRF, json={
        "bbox": [300, 300, 400, 380], "outcome": "unidentifiable"}).json()
    zoom = rclient.get(f"/api/review/added/{added['id']}/zoom.jpg")
    assert zoom.status_code == 200 and Image.open(io.BytesIO(zoom.content)).size[0] > 0
    assert rclient.patch(f"/api/review/frames/{sf}/boxes/{added['id']}", headers=CSRF, json={
        "bbox": [300, 300, 410, 390], "outcome": "unidentifiable"}).status_code == 200
    assert rclient.post(f"/api/review/frames/{sf}/complete", headers=CSRF).status_code == 409
    rclient.post("/api/review/verdicts", headers=CSRF, json={
        "event_id": f["event_ids"][0], "outcome": "accept"})
    assert rclient.post(f"/api/review/frames/{sf}/complete", headers=CSRF).status_code == 200
    assert rclient.delete(f"/api/review/frames/{sf}/complete", headers=CSRF).status_code == 403
    assert rclient.get(f"/api/review/frames/{sf}").json()["complete"] is not None

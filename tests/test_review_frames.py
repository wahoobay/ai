"""Whole-frame mode (spec §8)."""
from __future__ import annotations

import io
import json
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from app.review_catalogue import CatalogueStore
from app.review_frames import (
    AddedBoxIn,
    add_box,
    complete_frame,
    frame_state,
    open_frame,
    release_frame,
    remove_box,
    skip_frame,
    update_box,
)
from app.review_queue import (
    Conflict,
    Forbidden,
    Invalid,
    VerdictIn,
    next_item,
    skip_item,
    submit_verdict,
)
from PIL import Image
from review_fixtures import (
    CSRF,
    T0,
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
    # Important 1: admins get no exception -- they can be served a
    # spot-check themselves, so the frame view must not let them read
    # ahead of one they didn't give the first verdict on
    boss = await make_user(rdb, "boss", role="admin")
    admins = await _state(rdb, f, as_user(boss, role="admin"), cat)
    assert admins["boxes"][1]["state"] == "answered_hidden"
    assert admins["boxes"][1]["label"] is None
    assert "Pomatomus saltatrix" not in json.dumps(admins["boxes"][1])


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


SECOND_OPINION_FIRST = "finish the second opinion first"


async def _held_spot_check(rdb, tmp_path, holder: int) -> dict:
    """A two-box frame: box 1 answered by 'other' and now a spot-check
    live-claimed by `holder`; box 2 a queued, unclaimed pass-1 item."""
    f = await seed_frame(rdb, tmp_path, boxes=BOXES[:2])
    other = await make_user(rdb, "other")
    first = await add_verdict(rdb, f["event_ids"][0], other, name="Seriola zonata")
    spot = await add_item(rdb, f, 0, pass_=2, exclude_user_id=other, first_correction_id=first,
                          status="claimed", claimed_by=holder,
                          claimed_until=datetime.now(UTC) + timedelta(minutes=5))
    queued = await add_item(rdb, f, 1, predicted="Pomatomus saltarix",
                            route="Pomatomus saltatrix")
    return {**f, "spot": spot, "queued": queued}


async def _status(rdb, item_id):
    async with rdb.connection() as conn:
        cur = await conn.execute("SELECT status, claimed_by FROM review_items WHERE id = %s",
                                 (item_id,))
        return await cur.fetchone()


@pytest.mark.parametrize("role", ["reviewer", "admin"])
async def test_a_held_second_opinion_closes_its_frame_to_whoever_holds_it(rdb, rclient, tmp_path,
                                                                          role):
    """Final review I1: the frame view shows every other box's resolved
    label or model guess, so opening it mid-spot-check would contaminate
    the second opinion (spec §8). Admins get no exception. The frame's
    image carries no labels and the single view already shows it, so it
    stays open. A refused open claims nothing."""
    holder = await make_user(rdb, "checker", role=role)
    f = await _held_spot_check(rdb, tmp_path, holder)
    sf = f["saved_frame_id"]
    login(rclient, "checker")
    for r in (rclient.post(f"/api/review/frames/{sf}/open", headers=CSRF),
              rclient.get(f"/api/review/frames/{sf}")):
        assert r.status_code == 409 and r.json() == {"error": SECOND_OPINION_FIRST}
    assert rclient.get(f"/api/review/frames/{sf}/image.jpg").status_code == 200
    assert await _status(rdb, f["queued"]) == ("open", None)
    assert await _status(rdb, f["spot"]) == ("claimed", holder)


async def test_someone_not_holding_the_second_opinion_can_open_its_frame(rdb, rclient, tmp_path):
    holder = await make_user(rdb, "checker")
    f = await _held_spot_check(rdb, tmp_path, holder)
    sf = f["saved_frame_id"]
    await make_user(rdb, "bystander")
    login(rclient, "bystander")
    opened = rclient.post(f"/api/review/frames/{sf}/open", headers=CSRF)
    assert opened.status_code == 200
    assert opened.json()["boxes"][0]["state"] == "answered_hidden"
    assert rclient.get(f"/api/review/frames/{sf}").status_code == 200


async def test_the_frame_opens_again_once_the_second_opinion_lapses_or_is_answered(
        rdb, rclient, tmp_path):
    holder = await make_user(rdb, "checker")
    f = await _held_spot_check(rdb, tmp_path, holder)
    sf = f["saved_frame_id"]
    login(rclient, "checker")

    async def set_claim(delta):
        async with rdb.connection() as conn:
            await conn.execute("UPDATE review_items SET claimed_until = %s WHERE id = %s",
                               (datetime.now(UTC) + delta, f["spot"]))

    await set_claim(timedelta(minutes=-1))          # lapsed, not yet swept
    assert rclient.get(f"/api/review/frames/{sf}").status_code == 200
    assert rclient.post(f"/api/review/frames/{sf}/open", headers=CSRF).status_code == 200

    await set_claim(timedelta(minutes=5))           # live again
    assert rclient.get(f"/api/review/frames/{sf}").status_code == 409
    r = rclient.post("/api/review/verdicts", headers=CSRF, json={
        "event_id": f["event_ids"][0], "review_item_id": f["spot"], "outcome": "species",
        "label": "Seriola zonata", "label_source": "search"})
    assert r.status_code == 200, r.text
    assert rclient.get(f"/api/review/frames/{sf}").status_code == 200
    assert rclient.post(f"/api/review/frames/{sf}/open", headers=CSRF).status_code == 200


async def test_an_admin_sees_the_answer_once_the_spot_check_is_done(rdb, tmp_path, cat):
    """Important 1, second half: the hiding only lasts while the
    spot-check is open or claimed. Once a THIRD user -- not the admin
    watching, and not the first reviewer -- has answered it too, the
    admin, who gave no verdict of their own on this box, sees it resolved
    like anyone else. (Round 2: the admin must not be the one who
    answers the spot-check, or `mine` shows the answer regardless of
    hiding, making this test unable to fail.)"""
    f = await seed_frame(rdb, tmp_path, boxes=BOXES[:1])
    other, checker = await make_user(rdb, "other"), await make_user(rdb, "checker")
    boss = await make_user(rdb, "boss", role="admin")
    eid = f["event_ids"][0]
    first = await add_verdict(rdb, eid, other, name="Seriola zonata")
    item = await add_item(rdb, f, 0, pass_=2, exclude_user_id=other, first_correction_id=first)
    hidden = await _state(rdb, f, as_user(boss, role="admin"), cat)
    assert hidden["boxes"][0]["state"] == "answered_hidden"
    assert hidden["boxes"][0]["outcome"] is None and hidden["boxes"][0]["label"] is None
    await add_verdict(rdb, eid, checker, name="Seriola zonata", item_id=item)
    async with rdb.connection() as conn:
        await conn.execute("UPDATE review_items SET status = 'done' WHERE id = %s", (item,))
    resolved = await _state(rdb, f, as_user(boss, role="admin"), cat)
    assert resolved["boxes"][0]["state"] == "answered"
    assert resolved["boxes"][0]["label"] == "Seriola zonata"


async def test_open_and_get_treat_a_corrupt_or_zero_byte_frame_as_missing(rdb, rclient, tmp_path):
    """Important 2: only FileNotFoundError was caught, so a zero-byte or
    corrupt frame raised PIL.UnidentifiedImageError -- a raw 500 -- on
    open and get, and frame_open's claim (made before frame_state ever
    read the file) stayed in place regardless of the failure."""
    await make_user(rdb, "rev")
    login(rclient, "rev")
    for i, content in enumerate((b"", b"this is not a jpeg")):
        f = await seed_frame(rdb, tmp_path, ts=T0 + timedelta(minutes=i), frame_id=900 + i,
                             boxes=[{"bbox": [10, 10, 90, 90], "species": "Seriola zonata"}])
        item = await add_item(rdb, f, 0, predicted="Seriola zonata", route="Seriola zonata")
        Path(f["image_path"]).write_bytes(content)
        sf = f["saved_frame_id"]
        assert rclient.post(f"/api/review/frames/{sf}/open", headers=CSRF).status_code == 404
        assert rclient.get(f"/api/review/frames/{sf}").status_code == 404
        async with rdb.connection() as conn:
            cur = await conn.execute("SELECT status FROM review_items WHERE id = %s", (item,))
            status = (await cur.fetchone())[0]
        assert status == "open"   # no claim left behind by the failed open


async def test_added_zoom_treats_a_corrupt_or_zero_byte_frame_as_missing(rdb, rclient, tmp_path):
    """Important 2, the drawn-box zoom: added_zoom must catch the same set
    box_zoom already does, not just FileNotFoundError. The box is
    inserted directly (not through POST .../boxes, which would itself
    read the frame while it is still a valid image) because frame_size
    is cached by path: a successful read before corruption would leave
    the pre-corruption size cached and mask the bug this test is for."""
    rev = await make_user(rdb, "rev")
    login(rclient, "rev")
    for i, content in enumerate((b"", b"this is not a jpeg")):
        f = await seed_frame(rdb, tmp_path, ts=T0 + timedelta(minutes=i), frame_id=910 + i,
                             boxes=[])
        Path(f["image_path"]).write_bytes(content)
        async with rdb.connection() as conn:
            cur = await conn.execute("""
                INSERT INTO review_added_boxes (saved_frame_id, bbox, outcome, user_id)
                     VALUES (%s, %s, 'unidentifiable', %s) RETURNING id""",
                (f["saved_frame_id"], [20, 20, 120, 90], rev))
            box_id = (await cur.fetchone())[0]
        assert rclient.get(f"/api/review/added/{box_id}/zoom.jpg").status_code == 404


async def test_open_frame_does_not_reclaim_a_box_the_volunteer_skipped(rdb, tmp_path, cat):
    """Minor 1: open_frame ignored review_skips, so opening the frame
    again re-claimed a box the volunteer had just skipped."""
    f = await seed_frame(rdb, tmp_path, boxes=BOXES[:1])
    me = await make_user(rdb, "me")
    item = await add_item(rdb, f, 0, predicted="Seriola zonata", route="Seriola zonata",
                          status="claimed", claimed_by=me,
                          claimed_until=datetime.now(UTC) + timedelta(minutes=5))
    async with rdb.connection() as conn:
        await skip_item(conn, user_id=me, item_id=item)
        await open_frame(conn, saved_frame_id=f["saved_frame_id"], user_id=me, claim_s=600)
        cur = await conn.execute("SELECT status, claimed_by FROM review_items WHERE id = %s",
                                 (item,))
        status, claimed_by = await cur.fetchone()
    assert status == "open" and claimed_by is None


async def test_open_frame_leaves_someone_elses_live_claim_locked(rdb, tmp_path, cat):
    """The review's coverage gap: opening the frame must not touch a box
    someone else is actively holding -- it stays theirs, shown locked."""
    f = await seed_frame(rdb, tmp_path, boxes=BOXES[:1])
    me, other = await make_user(rdb, "me"), await make_user(rdb, "other")
    item = await add_item(rdb, f, 0, status="claimed", claimed_by=other,
                          claimed_until=datetime.now(UTC) + timedelta(minutes=5))
    async with rdb.connection() as conn:
        await open_frame(conn, saved_frame_id=f["saved_frame_id"], user_id=me, claim_s=600)
    state = await _state(rdb, f, as_user(me), cat)
    assert state["boxes"][0]["state"] == "locked"
    async with rdb.connection() as conn:
        cur = await conn.execute("SELECT claimed_by FROM review_items WHERE id = %s", (item,))
        claimed_by = (await cur.fetchone())[0]
    assert claimed_by == other


async def test_a_completion_racing_a_stale_read_still_gets_already_true(rdb, tmp_path, cat,
                                                                         monkeypatch):
    """Minor 4: the ON CONFLICT DO NOTHING result was never read, so a
    completer whose pre-check read "not complete" got already: False even
    when someone else's completion had, by the time of its own INSERT,
    already won. Reproduced deterministically -- real concurrency over
    two connections raced this same way but was timing-dependent, passing
    or failing depending on how the two interleaved -- by letting user
    "one" actually complete the frame first, then patching frame_state to
    keep telling user "two" it is still incomplete, exactly as a stale
    pre-check read before one's commit would. The fix must tell two's
    INSERT apart from one's by its own RETURNING result, not by trusting
    that stale read."""
    f = await seed_frame(rdb, tmp_path, boxes=BOXES[:1])
    one, two = await make_user(rdb, "one"), await make_user(rdb, "two")
    await add_verdict(rdb, f["event_ids"][0], one, name="Seriola zonata")
    sf = f["saved_frame_id"]
    async with rdb.connection() as conn:
        first = await complete_frame(conn, saved_frame_id=sf, user=as_user(one), catalogue=cat)
    assert first == {"complete": True, "already": False}

    async def _stale_incomplete(conn, *, saved_frame_id, user, catalogue):
        return {"complete": None, "blocking": []}

    monkeypatch.setattr("app.review_frames.frame_state", _stale_incomplete)
    async with rdb.connection() as conn:
        second = await complete_frame(conn, saved_frame_id=sf, user=as_user(two), catalogue=cat)
    assert second == {"complete": True, "already": True}


async def test_added_zoom_404s_for_a_removed_box(rdb, rclient, tmp_path):
    """Minor 5: added_zoom's query lacked removed_at IS NULL, so a
    removed box's zoom was still served."""
    await make_user(rdb, "rev")
    login(rclient, "rev")
    f = await seed_frame(rdb, tmp_path, boxes=[])
    sf = f["saved_frame_id"]
    added = rclient.post(f"/api/review/frames/{sf}/boxes", headers=CSRF, json={
        "bbox": [20, 20, 120, 90], "outcome": "unidentifiable"}).json()
    assert rclient.get(f"/api/review/added/{added['id']}/zoom.jpg").status_code == 200
    assert rclient.delete(f"/api/review/frames/{sf}/boxes/{added['id']}",
                          headers=CSRF).status_code == 200
    assert rclient.get(f"/api/review/added/{added['id']}/zoom.jpg").status_code == 404


async def test_frame_mode_verdicts_accept_a_hint_despite_no_item_id(rdb, tmp_path, cat):
    """The review's ⚠️: whole-frame verdicts carry no item id (spec), so
    the no-item path must read the event's own pass-1 item for its hints
    instead of always refusing label_source 'hint'. A species not among
    those hints is still refused."""
    f = await seed_frame(rdb, tmp_path, boxes=[{"bbox": [10, 10, 90, 90]},
                                               {"bbox": [200, 10, 300, 90]}])
    me, other = await make_user(rdb, "me"), await make_user(rdb, "other")
    hints = [{"name": "Pomatomus saltatrix", "p": 0.4}]
    await add_item(rdb, f, 0, status="done", hints=hints)
    await add_item(rdb, f, 1, status="done", hints=hints)
    async with rdb.connection() as conn:
        out = await submit_verdict(conn, user=as_user(me), body=VerdictIn(
            event_id=f["event_ids"][0], outcome="species", label="Pomatomus saltatrix",
            label_source="hint"), catalogue=cat, spot_rate=0.0, rng=random.Random(0))
    assert out["label"] == "Pomatomus saltatrix"
    async with rdb.connection() as conn:
        with pytest.raises(Invalid, match="not one of the hints"):
            await submit_verdict(conn, user=as_user(other), body=VerdictIn(
                event_id=f["event_ids"][1], outcome="species", label="Seriola zonata",
                label_source="hint"), catalogue=cat, spot_rate=0.0, rng=random.Random(0))


# ---- spec amendment 2026-09-25: frame-first review ---------------------------

async def test_opening_keeps_and_refreshes_the_box_you_were_served(rdb, tmp_path, cat):
    """The page opens the frame of the box the queue just served without
    releasing it first, so nobody can take it in between; opening must
    also push that claim's expiry out like any fresh claim."""
    f = await seed_frame(rdb, tmp_path, boxes=BOXES[:2])
    me = await make_user(rdb, "me")
    soon = datetime.now(UTC) + timedelta(seconds=30)
    served = await add_item(rdb, f, 0, status="claimed", claimed_by=me, claimed_until=soon)
    other_box = await add_item(rdb, f, 1)
    async with rdb.connection() as conn:
        await open_frame(conn, saved_frame_id=f["saved_frame_id"], user_id=me, claim_s=600)
        cur = await conn.execute(
            "SELECT id, status, claimed_by, claimed_until FROM review_items WHERE id = ANY(%s)",
            ([served, other_box],))
        rows = {r[0]: r[1:] for r in await cur.fetchall()}
    assert rows[served][:2] == ("claimed", me)
    assert rows[served][2] > soon + timedelta(minutes=5)
    assert rows[other_box][:2] == ("claimed", me)


async def test_releasing_a_frame_gives_back_only_your_own_first_pass_claims(rdb, tmp_path, cat):
    f = await seed_frame(rdb, tmp_path, boxes=BOXES)
    me, other = await make_user(rdb, "me"), await make_user(rdb, "other")
    live = datetime.now(UTC) + timedelta(minutes=5)
    mine = await add_item(rdb, f, 0, status="claimed", claimed_by=me, claimed_until=live)
    theirs = await add_item(rdb, f, 1, status="claimed", claimed_by=other, claimed_until=live)
    done = await add_item(rdb, f, 2, status="done", claimed_by=me)
    first = await add_verdict(rdb, f["event_ids"][3], other, name="Seriola zonata")
    spot = await add_item(rdb, f, 3, pass_=2, exclude_user_id=other, first_correction_id=first,
                          status="claimed", claimed_by=me, claimed_until=live)
    async with rdb.connection() as conn:
        await release_frame(conn, saved_frame_id=f["saved_frame_id"], user_id=me)
    assert await _status(rdb, mine) == ("open", None)
    assert await _status(rdb, theirs) == ("claimed", other)
    assert await _status(rdb, done) == ("done", me)
    assert await _status(rdb, spot) == ("claimed", me)       # pass 2 is never touched


async def test_releasing_a_frame_also_clears_a_claim_the_sweep_already_reopened(rdb, tmp_path,
                                                                                cat):
    """The lapsed-claim sweep keeps claimed_by (review_queue.next_item);
    giving the frame up must clear it, as release_item does."""
    f = await seed_frame(rdb, tmp_path, boxes=BOXES[:1])
    me = await make_user(rdb, "me")
    item = await add_item(rdb, f, 0, status="open", claimed_by=me)
    async with rdb.connection() as conn:
        await release_frame(conn, saved_frame_id=f["saved_frame_id"], user_id=me)
    assert await _status(rdb, item) == ("open", None)


async def test_next_frame_skips_the_unanswered_boxes_for_you_only(rdb, tmp_path, cat):
    """Spec amendment 2026-09-25, flow step 3: skipped for this volunteer
    (the queue won't bring them back), free for everyone else at once."""
    f = await seed_frame(rdb, tmp_path, boxes=BOXES[:3])
    me, other = await make_user(rdb, "me"), await make_user(rdb, "other")
    third = await make_user(rdb, "third")
    live = datetime.now(UTC) + timedelta(minutes=5)
    kw = {"predicted": "Seriola zonata", "route": "Seriola zonata"}
    mine = await add_item(rdb, f, 0, status="claimed", claimed_by=me, claimed_until=live, **kw)
    free = await add_item(rdb, f, 1, **kw)
    theirs = await add_item(rdb, f, 2, status="claimed", claimed_by=other, claimed_until=live,
                            **kw)
    async with rdb.connection() as conn:
        await skip_frame(conn, saved_frame_id=f["saved_frame_id"], user_id=me)
        cur = await conn.execute("SELECT item_id FROM review_skips WHERE user_id = %s", (me,))
        skipped = {r[0] for r in await cur.fetchall()}
    assert skipped == {mine, free, theirs}
    assert await _status(rdb, mine) == ("open", None)
    assert await _status(rdb, theirs) == ("claimed", other)
    async with rdb.connection() as conn:
        again = await next_item(conn, user_id=me, lane="check", species_order=[],
                                spot_serve_rate=0.0, claim_s=600, rng=random.Random(0))
        got = await next_item(conn, user_id=third, lane="check", species_order=[],
                              spot_serve_rate=0.0, claim_s=600, rng=random.Random(0))
    assert again is None
    assert got["id"] in {mine, free}


async def test_next_frame_leaves_spot_checks_alone(rdb, tmp_path, cat):
    f = await seed_frame(rdb, tmp_path, boxes=BOXES[:1])
    me, other = await make_user(rdb, "me"), await make_user(rdb, "other")
    first = await add_verdict(rdb, f["event_ids"][0], other, name="Seriola zonata")
    spot = await add_item(rdb, f, 0, pass_=2, exclude_user_id=other, first_correction_id=first)
    async with rdb.connection() as conn:
        await skip_frame(conn, saved_frame_id=f["saved_frame_id"], user_id=me)
        cur = await conn.execute("SELECT count(*) FROM review_skips WHERE item_id = %s", (spot,))
        n = (await cur.fetchone())[0]
    assert n == 0
    assert await _status(rdb, spot) == ("open", None)


async def test_release_and_skip_over_http(rdb, rclient, tmp_path):
    me = await make_user(rdb, "rev")
    login(rclient, "rev")
    f = await seed_frame(rdb, tmp_path, boxes=BOXES[:1])
    sf = f["saved_frame_id"]
    item = await add_item(rdb, f, 0, predicted="Seriola zonata", route="Seriola zonata")
    for action in ("release", "skip"):
        assert rclient.post(f"/api/review/frames/{sf}/{action}").status_code == 403   # no CSRF
    # Fix round 1, finding 2: role is enforced too, not just CSRF.
    await make_user(rdb, "watcher", role="viewer")
    login(rclient, "watcher")
    for action in ("release", "skip"):
        assert rclient.post(f"/api/review/frames/{sf}/{action}",
                            headers=CSRF).status_code == 403   # not a reviewer
    assert await _status(rdb, item) == ("open", None)
    login(rclient, "rev")
    rclient.post(f"/api/review/frames/{sf}/open", headers=CSRF)
    assert await _status(rdb, item) == ("claimed", me)
    assert rclient.post(f"/api/review/frames/{sf}/release", headers=CSRF).json() == {"ok": True}
    assert await _status(rdb, item) == ("open", None)
    rclient.post(f"/api/review/frames/{sf}/open", headers=CSRF)
    assert rclient.post(f"/api/review/frames/{sf}/skip", headers=CSRF).json() == {"ok": True}
    assert await _status(rdb, item) == ("open", None)
    reopened = rclient.post(f"/api/review/frames/{sf}/open", headers=CSRF).json()
    assert reopened["boxes"][0]["review_item_id"] is None      # skipped: not claimed again

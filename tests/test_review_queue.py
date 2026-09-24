"""Serving boxes and recording verdicts (spec §8), without HTTP."""
from __future__ import annotations

import asyncio
import json
import random
from datetime import UTC, datetime, timedelta

import pytest
from app.review_catalogue import CatalogueStore
from app.review_queue import (
    Conflict,
    Forbidden,
    Invalid,
    VerdictIn,
    build_payload,
    fetch_one,
    next_item,
    release_item,
    reopen_payload,
    skip_item,
    submit_verdict,
    validate_bbox,
)
from review_fixtures import (
    T0,
    add_item,
    add_verdict,
    as_user,
    catalogue_paths,
    make_user,
    seed_frame,
)

pytestmark = pytest.mark.anyio

HINTS = [{"name": "Seriola zonata", "p": 0.56}, {"name": "Pomatomus saltatrix", "p": 0.2},
         {"name": "Pomacanthus paru", "p": 0.1}]


@pytest.fixture
def cat(review_assets):
    return CatalogueStore(catalogue_paths(review_assets)).get()


async def _boxes(rdb, tmp_path, n=2, **item_kw):
    out = []
    for i in range(n):
        f = await seed_frame(rdb, tmp_path, ts=T0 + timedelta(minutes=10 * i), frame_id=10 + i,
                             boxes=[{"bbox": [100, 100, 300, 250], "species": "Seriola zonata"}])
        kw = {"predicted": "Seriola zonata", "predicted_p": 0.56, "route": "Seriola zonata",
              "hints": HINTS, **item_kw}
        out.append((f, await add_item(rdb, f, **kw)))
    return out


async def _next(rdb, uid, lane="check", order=(), rate=0.0, seed=1):
    async with rdb.connection() as conn:
        return await next_item(conn, user_id=uid, lane=lane, species_order=list(order),
                               spot_serve_rate=rate, claim_s=600, rng=random.Random(seed))


async def _verdict(rdb, cat, user, spot=0.0, **body):
    async with rdb.connection() as conn:
        return await submit_verdict(conn, user=user, body=VerdictIn(**body), catalogue=cat,
                                    spot_rate=spot, rng=random.Random(0))


async def _row(rdb, sql, *params):
    async with rdb.connection() as conn:
        return await (await conn.execute(sql, params)).fetchone()


async def _status(rdb, item_id):
    return (await _row(rdb, "SELECT status FROM review_items WHERE id = %s", item_id))[0]


# ---- bbox validation ------------------------------------------------------

def test_validate_bbox_pins_the_three_messages():
    """Controller ruling: validate_bbox is shared with Task 8's added-box
    validation, so its three messages are pinned here rather than only
    exercised indirectly through submit_verdict."""
    with pytest.raises(Invalid) as bad_shape:
        validate_bbox([1, 2, 3], 1280, 720)
    assert bad_shape.value.message == "a box is [x1, y1, x2, y2]"

    with pytest.raises(Invalid) as outside:
        validate_bbox([-1, 0, 50, 50], 1280, 720)
    assert outside.value.message == "the box must lie inside the frame"

    with pytest.raises(Invalid) as outside2:
        validate_bbox([10, 10, 1281, 100], 1280, 720)
    assert outside2.value.message == "the box must lie inside the frame"

    with pytest.raises(Invalid) as too_small:
        validate_bbox([10, 10, 12, 12], 1280, 720)
    assert too_small.value.message == "the box is too small"

    assert validate_bbox([0, 0, 1280, 720], 1280, 720) == [0, 0, 1280, 720]


# ---- serving --------------------------------------------------------------

async def test_two_volunteers_asking_at_once_never_get_the_same_box(rdb, tmp_path):
    await _boxes(rdb, tmp_path, n=2)
    one, two = await make_user(rdb, "one"), await make_user(rdb, "two")
    a, b = await asyncio.gather(_next(rdb, one), _next(rdb, two))
    assert a["id"] != b["id"] and a["claimed_by"] == one and b["claimed_by"] == two


async def test_asking_again_returns_the_box_you_already_hold(rdb, tmp_path):
    await _boxes(rdb, tmp_path, n=2)
    one = await make_user(rdb, "one")
    first = await _next(rdb, one)
    assert (await _next(rdb, one, seed=99))["id"] == first["id"]


async def test_a_lapsed_claim_goes_back_to_the_pool(rdb, tmp_path):
    [(_f, item)] = await _boxes(rdb, tmp_path, n=1)
    one, two = await make_user(rdb, "one"), await make_user(rdb, "two")
    async with rdb.connection() as conn:
        await conn.execute(
            """UPDATE review_items SET status = 'claimed', claimed_by = %s,
                      claimed_until = now() - interval '1 minute' WHERE id = %s""", (one, item))
    got = await _next(rdb, two)
    assert got["id"] == item and got["claimed_by"] == two


async def test_the_thinnest_species_comes_first(rdb, tmp_path):
    [(_fa, _a), (_fb, b)] = await _boxes(rdb, tmp_path, n=2)
    async with rdb.connection() as conn:
        await conn.execute(
            "UPDATE review_items SET route_species = 'Pomatomus saltatrix' WHERE id = %s", (b,))
    one = await make_user(rdb, "one")
    got = await _next(rdb, one, order=["Pomatomus saltatrix", "Seriola zonata"])
    assert got["id"] == b


async def test_a_skipped_box_is_not_offered_to_you_again(rdb, tmp_path):
    [(_f, item)] = await _boxes(rdb, tmp_path, n=1)
    one, two = await make_user(rdb, "one"), await make_user(rdb, "two")
    await _next(rdb, one)
    async with rdb.connection() as conn:
        await skip_item(conn, user_id=one, item_id=item)
    assert await _next(rdb, one) is None
    assert (await _next(rdb, two))["id"] == item


async def test_releasing_puts_the_box_back(rdb, tmp_path):
    [(_f, item)] = await _boxes(rdb, tmp_path, n=1)
    one = await make_user(rdb, "one")
    await _next(rdb, one)
    async with rdb.connection() as conn:
        await release_item(conn, user_id=one, item_id=item)
    assert await _status(rdb, item) == "open"


async def test_spot_checks_skip_the_first_reviewer_and_anyone_who_answered(rdb, tmp_path):
    [(f, _i)] = await _boxes(rdb, tmp_path, n=1, status="done")
    one = await make_user(rdb, "one")
    two = await make_user(rdb, "two")
    three = await make_user(rdb, "three")
    first = await add_verdict(rdb, f["event_ids"][0], one, name="Seriola zonata")
    await add_verdict(rdb, f["event_ids"][0], three, name="Seriola zonata")
    spot = await add_item(rdb, f, pass_=2, exclude_user_id=one, first_correction_id=first)
    assert await _next(rdb, one, rate=1.0) is None
    assert await _next(rdb, three, rate=1.0) is None
    assert (await _next(rdb, two, rate=1.0))["id"] == spot


async def test_a_spot_check_waiting_two_days_is_served_even_at_rate_zero(rdb, tmp_path):
    [(f, f_item), (_g, _g_item)] = await _boxes(rdb, tmp_path, n=2)
    one, two = await make_user(rdb, "one"), await make_user(rdb, "two")
    async with rdb.connection() as conn:
        await conn.execute("UPDATE review_items SET status = 'done' WHERE id = %s", (f_item,))
    first = await add_verdict(rdb, f["event_ids"][0], one, name="Seriola zonata")
    spot = await add_item(rdb, f, pass_=2, exclude_user_id=one, first_correction_id=first,
                          added_at=datetime.now(UTC) - timedelta(hours=49))
    assert (await _next(rdb, two, rate=0.0))["id"] == spot


async def test_a_spot_check_payload_says_nothing_about_the_model(rdb, tmp_path, cat):
    """Review Focus 2: not the guess, not the hints, not what it counts toward."""
    [(f, _i)] = await _boxes(rdb, tmp_path, n=1, status="done")
    one = await make_user(rdb, "one")
    first = await add_verdict(rdb, f["event_ids"][0], one, name="Seriola zonata")
    spot = await add_item(rdb, f, pass_=2, exclude_user_id=one, first_correction_id=first,
                          predicted="Seriola zonata", route="Seriola zonata", hints=HINTS)
    async with rdb.connection() as conn:
        item = await fetch_one(conn, "SELECT * FROM review_items WHERE id = %s", (spot,))
        payload = await build_payload(conn, item, catalogue=cat, counts=lambda n: 3, target=200)
    assert payload["model"] is None and payload["hints"] is None
    assert payload["counts_toward"] is None
    text = json.dumps(payload)
    for word in ("Seriola", "Pomatomus", "Pomacanthus", "Rudderfish"):
        assert word not in text


async def test_a_first_pass_payload_shows_the_guess_the_other_hints_and_progress(rdb, tmp_path,
                                                                                  cat):
    [(_f, item_id)] = await _boxes(rdb, tmp_path, n=1)
    async with rdb.connection() as conn:
        item = await fetch_one(conn, "SELECT * FROM review_items WHERE id = %s", (item_id,))
        payload = await build_payload(conn, item, catalogue=cat, counts=lambda n: 3, target=200)
    assert payload["model"]["name"] == "Seriola zonata"
    assert payload["model"]["common"] == "Banded Rudderfish"
    # the guess itself is not repeated among the hint buttons
    assert [h["name"] for h in payload["hints"]] == ["Pomatomus saltatrix", "Pomacanthus paru"]
    assert payload["counts_toward"] == {"name": "Seriola zonata", "common": "Banded Rudderfish",
                                        "count": 3, "target": 200}
    assert payload["zoom"]["region"] == [0, 25, 400, 325]
    assert payload["frame"]["width"] == 1280 and payload["frame"]["height"] == 720


# ---- verdicts -----------------------------------------------------------------

async def test_accepting_the_guess_records_an_endorsement_and_closes_the_item(rdb, tmp_path,
                                                                              cat):
    [(f, item)] = await _boxes(rdb, tmp_path, n=1)
    one = await make_user(rdb, "one")
    await _next(rdb, one)
    out = await _verdict(rdb, cat, as_user(one), event_id=f["event_ids"][0],
                         review_item_id=item, outcome="accept")
    assert out["label"] == "Seriola zonata" and out["first_pass"] and out["counts"]
    row = await _row(rdb, """SELECT corrected_name, is_endorsement, label_source, review_item_id,
                                    confidence, user_id
                               FROM detection_corrections WHERE id = %s""", out["id"])
    assert row == ("Seriola zonata", True, "model_guess", item, "certain", one)
    assert await _status(rdb, item) == "done"


async def test_a_species_from_the_search_with_its_life_stage(rdb, tmp_path, cat):
    [(f, item)] = await _boxes(rdb, tmp_path, n=1)
    one = await make_user(rdb, "one")
    out = await _verdict(rdb, cat, as_user(one), event_id=f["event_ids"][0], review_item_id=item,
                         outcome="species", label="Pomacanthus paru", life_stage="juvenile",
                         label_source="search", not_sure=True)
    row = await _row(rdb, """SELECT corrected_name, life_stage, is_endorsement, confidence
                               FROM detection_corrections WHERE id = %s""", out["id"])
    assert row == ("Pomacanthus paru", "juvenile", False, "uncertain")
    assert out["counts"] is False            # 'not sure' labels do not count toward progress


async def test_answer_validation(rdb, tmp_path, cat):
    [(f, item)] = await _boxes(rdb, tmp_path, n=1)
    user = as_user(await make_user(rdb, "one"))
    eid = f["event_ids"][0]
    bad = [
        dict(outcome="species", label="Not a species", label_source="search"),  # Review Focus 5
        dict(outcome="species", label="Pomacanthus paru", label_source="search"),  # stage needed
        dict(outcome="species", label="Neopomacentrus cyanomos", label_source="hint"),
        dict(outcome="species", label="Seriola zonata", label_source="model_guess"),
        dict(outcome="no_animal", label="Seriola zonata"),
        dict(outcome="unidentifiable", not_sure=True),
        dict(outcome="no_animal", bbox=[10, 10, 50, 50]),
        dict(outcome="accept", bbox=[-1, 0, 50, 50]),
        dict(outcome="accept", bbox=[10, 10, 13, 13]),
        dict(outcome="accept", bbox=[10, 10, 1281, 100]),
        dict(outcome="species", label="Seriola zonata", label_source="search",
             celebrity_id="bobby"),
    ]
    for extra in bad:
        with pytest.raises(Invalid):
            await _verdict(rdb, cat, user, event_id=eid, review_item_id=item, **extra)
    ok = await _verdict(rdb, cat, user, event_id=eid, review_item_id=item, outcome="accept",
                        bbox=[0, 0, 1280, 720])
    assert ok["label"] == "Seriola zonata"


async def test_a_second_opinion_cannot_use_the_guess_or_the_hints(rdb, tmp_path, cat):
    [(f, _i)] = await _boxes(rdb, tmp_path, n=1, status="done")
    one, two = await make_user(rdb, "one"), await make_user(rdb, "two")
    first = await add_verdict(rdb, f["event_ids"][0], one, name="Seriola zonata")
    spot = await add_item(rdb, f, pass_=2, exclude_user_id=one, first_correction_id=first,
                          predicted="Seriola zonata", hints=HINTS)
    for extra in (dict(outcome="accept"),
                  dict(outcome="species", label="Pomatomus saltatrix", label_source="hint")):
        with pytest.raises(Invalid):
            await _verdict(rdb, cat, as_user(two), event_id=f["event_ids"][0],
                           review_item_id=spot, **extra)
    out = await _verdict(rdb, cat, as_user(two), spot=1.0, event_id=f["event_ids"][0],
                         review_item_id=spot, outcome="species", label="Seriola zonata",
                         label_source="search")
    assert out["first_pass"] is False and out["spot_check_scheduled"] is False
    assert await _status(rdb, spot) == "done"


async def test_a_finished_or_held_box_is_a_conflict(rdb, tmp_path, cat):
    [(f, item)] = await _boxes(rdb, tmp_path, n=1)
    one, two = await make_user(rdb, "one"), await make_user(rdb, "two")
    await _next(rdb, one)
    with pytest.raises(Conflict):
        await _verdict(rdb, cat, as_user(two), event_id=f["event_ids"][0], review_item_id=item,
                       outcome="accept")
    await _verdict(rdb, cat, as_user(one), event_id=f["event_ids"][0], review_item_id=item,
                   outcome="accept")
    with pytest.raises(Conflict):
        await _verdict(rdb, cat, as_user(two), event_id=f["event_ids"][0], review_item_id=item,
                       outcome="accept")


async def test_without_an_item_you_can_only_answer_boxes_nobody_else_has(rdb, tmp_path, cat):
    f = await seed_frame(rdb, tmp_path, boxes=[{"bbox": [10, 10, 90, 90],
                                                "species": "Seriola zonata"}])
    one, two = await make_user(rdb, "one"), await make_user(rdb, "two")
    boss = await make_user(rdb, "boss", role="admin")
    eid = f["event_ids"][0]
    await _verdict(rdb, cat, as_user(one), event_id=eid, outcome="accept")
    await _verdict(rdb, cat, as_user(one), event_id=eid, outcome="no_animal")   # your own: fine
    with pytest.raises(Forbidden):
        await _verdict(rdb, cat, as_user(two), event_id=eid, outcome="accept")
    await _verdict(rdb, cat, as_user(boss, role="admin"), event_id=eid, outcome="accept")


async def test_a_first_pass_verdict_can_schedule_a_spot_check(rdb, tmp_path, cat):
    [(f, item)] = await _boxes(rdb, tmp_path, n=1)
    one = await make_user(rdb, "one")
    out = await _verdict(rdb, cat, as_user(one), spot=1.0, event_id=f["event_ids"][0],
                         review_item_id=item, outcome="accept")
    assert out["spot_check_scheduled"]
    row = await _row(rdb, """SELECT pass, status, exclude_user_id, first_correction_id
                               FROM review_items WHERE event_id = %s AND pass = 2""",
                     f["event_ids"][0])
    assert row == (2, "open", one, out["id"])


async def test_reopening_shows_your_last_answer_and_box(rdb, tmp_path, cat):
    [(f, item)] = await _boxes(rdb, tmp_path, n=1)
    one, two = await make_user(rdb, "one"), await make_user(rdb, "two")
    eid = f["event_ids"][0]
    await _verdict(rdb, cat, as_user(one), event_id=eid, review_item_id=item, outcome="accept",
                   bbox=[90, 90, 310, 260])
    async with rdb.connection() as conn:
        p = await reopen_payload(conn, user=as_user(one), event_id=eid, catalogue=cat,
                                 counts=lambda n: 0, target=200)
        assert p["item"]["id"] is None and p["box"]["bbox"] == [90, 90, 310, 260]
        assert p["mine"]["outcome"] == "species" and p["mine"]["label"] == "Seriola zonata"
        with pytest.raises(Forbidden):
            await reopen_payload(conn, user=as_user(two), event_id=eid, catalogue=cat,
                                 counts=lambda n: 0, target=200)

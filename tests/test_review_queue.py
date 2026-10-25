"""Serving boxes and recording verdicts (spec §8), without HTTP."""

from __future__ import annotations

import asyncio
import json
import random
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from app.review_catalogue import CatalogueStore
from app.review_frames import frame_record
from app.review_queue import (
    _VERDICT_IDENTITY_COLUMNS,
    _VERDICT_NON_IDENTITY_COLUMNS,
    EVENT_LOCK_SQL,
    SKIP_TTL_DAYS,
    Conflict,
    Forbidden,
    FrameMissing,
    Invalid,
    ReviewError,
    VerdictIn,
    _duplicate_verdict,
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
    wait_until_blocked,
)

pytestmark = pytest.mark.anyio

HINTS = [
    {"name": "Seriola zonata", "p": 0.56},
    {"name": "Pomatomus saltatrix", "p": 0.2},
    {"name": "Pomacanthus paru", "p": 0.1},
]


@pytest.fixture
def cat(review_assets):
    return CatalogueStore(catalogue_paths(review_assets)).get()


async def _boxes(rdb, tmp_path, n=2, **item_kw):
    out = []
    for i in range(n):
        f = await seed_frame(
            rdb,
            tmp_path,
            ts=T0 + timedelta(minutes=10 * i),
            frame_id=10 + i,
            boxes=[{"bbox": [100, 100, 300, 250], "species": "Seriola zonata"}],
        )
        kw = {
            "predicted": "Seriola zonata",
            "predicted_p": 0.56,
            "route": "Seriola zonata",
            "hints": HINTS,
            **item_kw,
        }
        out.append((f, await add_item(rdb, f, **kw)))
    return out


async def _next(rdb, uid, lane="check", order=(), rate=0.0, seed=1):
    async with rdb.connection() as conn:
        return await next_item(
            conn,
            user_id=uid,
            lane=lane,
            species_order=list(order),
            spot_serve_rate=rate,
            claim_s=600,
            rng=random.Random(seed),
        )


async def _verdict(rdb, cat, user, spot=0.0, **body):
    async with rdb.connection() as conn:
        return await submit_verdict(
            conn,
            user=user,
            body=VerdictIn(**body),
            catalogue=cat,
            spot_rate=spot,
            rng=random.Random(0),
        )


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


def test_validate_bbox_is_type_robust():
    """Task 8 feeds this raw client JSON: None, a bare scalar, a string,
    the wrong length, a non-numeric member, a non-finite number, and a
    dict -- even one whose keys happen to look numeric, which int() would
    otherwise accept as if they were the coordinates -- must all raise the
    same Invalid rather than a TypeError, ValueError or OverflowError."""
    for bad in (
        None,
        5,
        3.5,
        "1234",
        [1, 2, 3],
        [1, 2, 3, 4, 5],
        [1, 2, 3, "x"],
        [1, 2, 3, None],
        [1, 2, 3, [4]],
        {"a": 1, "b": 2, "c": 3, "d": 4},
        {"10": 1, "20": 1, "300": 1, "400": 1},
        [0, 0, float("inf"), 100],
        [0, 0, float("nan"), 100],
    ):
        with pytest.raises(Invalid) as exc:
            validate_bbox(bad, 1280, 720)
        assert exc.value.message == "a box is [x1, y1, x2, y2]"
    assert validate_bbox((0, 0, 1280, 720), 1280, 720) == [0, 0, 1280, 720]  # tuples are fine


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


async def test_asking_again_refreshes_the_claim_on_the_box_you_hold(rdb, tmp_path):
    """Final review Minor 5: a reload at minute 9 used to leave one minute
    on the claim."""
    [(_f, item)] = await _boxes(rdb, tmp_path, n=1)
    one = await make_user(rdb, "one")
    async with rdb.connection() as conn:
        await conn.execute(
            """UPDATE review_items SET status = 'claimed', claimed_by = %s,
                      claimed_until = now() + interval '30 seconds' WHERE id = %s""",
            (one, item),
        )
    got = await _next(rdb, one)
    assert got["id"] == item
    left = (await _row(rdb, "SELECT claimed_until - now() FROM review_items WHERE id = %s", item))[
        0
    ]
    assert left > timedelta(minutes=5)


async def test_the_held_refresh_does_not_resurrect_a_claim_that_moved_on_under_it(rdb, tmp_path):
    """Fix round 1, finding 1: the held-item UPDATE's outer WHERE checked
    only `id = (...)`, so if the row changed while the UPDATE was blocked
    waiting on another transaction's lock, Postgres's re-check (against
    the row's newest committed version) let the write through regardless.
    Real concurrency, not two coroutines sharing one connection: connection
    B holds the row's lock and moves it to 'done' -- as another volunteer's
    next_item re-claiming a lapsed row, or a release/verdict from a second
    tab, would -- while connection A's held-item UPDATE is blocked waiting
    for that very lock to release."""
    [(_f, item)] = await _boxes(rdb, tmp_path, n=1)
    one = await make_user(rdb, "one")
    live = datetime.now(UTC) + timedelta(minutes=5)
    async with rdb.connection() as conn:
        await conn.execute(
            """UPDATE review_items SET status = 'claimed', claimed_by = %s,
                      claimed_until = %s WHERE id = %s""",
            (one, live, item),
        )
    async with rdb.connection() as locker, locker.transaction():
        await locker.execute("SELECT id FROM review_items WHERE id = %s FOR UPDATE", (item,))
        task = asyncio.create_task(_next(rdb, one))
        await wait_until_blocked(rdb)  # A's held-item UPDATE is now waiting on our lock
        await locker.execute(
            """UPDATE review_items SET status = 'done', claimed_by = NULL,
                      claimed_until = NULL WHERE id = %s""",
            (item,),
        )
    # locker's transaction commits here, releasing the lock A was waiting on
    got = await task
    assert got is None
    row = await _row(
        rdb,
        """SELECT status, claimed_by, claimed_until
                                FROM review_items WHERE id = %s""",
        item,
    )
    assert row == ("done", None, None)


async def test_a_lapsed_claim_goes_back_to_the_pool(rdb, tmp_path):
    [(_f, item)] = await _boxes(rdb, tmp_path, n=1)
    one, two = await make_user(rdb, "one"), await make_user(rdb, "two")
    async with rdb.connection() as conn:
        await conn.execute(
            """UPDATE review_items SET status = 'claimed', claimed_by = %s,
                      claimed_until = now() - interval '1 minute' WHERE id = %s""",
            (one, item),
        )
    got = await _next(rdb, two)
    assert got["id"] == item and got["claimed_by"] == two


async def test_the_sweep_skips_a_row_locked_on_another_connection(rdb, tmp_path):
    """Finding 8: the sweep's SELECT ... FOR UPDATE SKIP LOCKED must not
    block behind a real lock held by a second connection -- it should skip
    that row and still reopen (and serve) everything else, promptly. A
    plain blocking UPDATE would instead wait behind connection B's lock
    until this test's own short statement_timeout cancels it."""
    [(_fa, item_a), (_fb, item_b)] = await _boxes(rdb, tmp_path, n=2)
    one, two = await make_user(rdb, "one"), await make_user(rdb, "two")
    async with rdb.connection() as conn:
        await conn.execute(
            """UPDATE review_items SET status = 'claimed', claimed_by = %s,
                      claimed_until = now() - interval '1 minute' WHERE id IN (%s, %s)""",
            (one, item_a, item_b),
        )

    async with rdb.connection() as locker, locker.transaction():
        await locker.execute("SELECT id FROM review_items WHERE id = %s FOR UPDATE", (item_a,))
        async with rdb.connection() as conn, conn.transaction():
            await conn.execute("SELECT set_config('statement_timeout', '2000', true)")
            t0 = time.monotonic()
            got = await next_item(
                conn,
                user_id=two,
                lane="check",
                species_order=[],
                spot_serve_rate=0.0,
                claim_s=600,
                rng=random.Random(0),
            )
            elapsed = time.monotonic() - t0
    # locker's transaction (and its lock on item_a) ends here, on exit

    assert elapsed < 1.5  # well under the 2s timeout: it never blocked
    assert got is not None and got["id"] == item_b and got["claimed_by"] == two

    row_a = await _row(rdb, "SELECT status, claimed_by FROM review_items WHERE id = %s", item_a)
    assert row_a == ("claimed", one)  # skipped: still locked by B's transaction, untouched


async def test_the_thinnest_species_comes_first(rdb, tmp_path):
    [(_fa, _a), (_fb, b)] = await _boxes(rdb, tmp_path, n=2)
    async with rdb.connection() as conn:
        await conn.execute(
            "UPDATE review_items SET route_species = 'Pomatomus saltatrix' WHERE id = %s", (b,)
        )
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


async def _age_skips(rdb, days):
    async with rdb.connection() as conn:
        await conn.execute(
            "UPDATE review_skips SET created_at = now() - make_interval(days => %s)", (days,)
        )


async def test_a_skip_stops_hiding_the_box_once_it_is_older_than_the_ttl(rdb, tmp_path):
    assert SKIP_TTL_DAYS == 30
    [(_f, item)] = await _boxes(rdb, tmp_path, n=1)
    me = await make_user(rdb, "me")
    async with rdb.connection() as conn:
        await skip_item(conn, user_id=me, item_id=item)
    await _age_skips(rdb, 29)
    assert await _next(rdb, me) is None
    await _age_skips(rdb, 31)
    assert (await _next(rdb, me))["id"] == item


async def test_the_skip_ttl_is_a_setting(rdb, tmp_path):
    [(_f, item)] = await _boxes(rdb, tmp_path, n=1)
    me = await make_user(rdb, "me")
    async with rdb.connection() as conn:
        await skip_item(conn, user_id=me, item_id=item)
    await _age_skips(rdb, 3)
    async with rdb.connection() as conn:
        got = await next_item(
            conn,
            user_id=me,
            lane="check",
            species_order=[],
            spot_serve_rate=0.0,
            claim_s=600,
            rng=random.Random(1),
            skip_ttl_days=2,
        )
    assert got["id"] == item


async def test_skipping_again_restarts_the_clock(rdb, tmp_path):
    [(_f, item)] = await _boxes(rdb, tmp_path, n=1)
    me = await make_user(rdb, "me")
    async with rdb.connection() as conn:
        await skip_item(conn, user_id=me, item_id=item)
    await _age_skips(rdb, 40)
    async with rdb.connection() as conn:
        await skip_item(conn, user_id=me, item_id=item)
    assert await _next(rdb, me) is None
    age = await _row(rdb, "SELECT now() - created_at FROM review_skips WHERE item_id = %s", item)
    assert age[0] < timedelta(minutes=1)


async def test_an_expired_skip_no_longer_hides_a_spot_check(rdb, tmp_path):
    f = await seed_frame(
        rdb, tmp_path, boxes=[{"bbox": [100, 100, 300, 250], "species": "Seriola zonata"}]
    )
    first, me = await make_user(rdb, "first"), await make_user(rdb, "me")
    spot = await add_item(rdb, f, pass_=2, exclude_user_id=first)
    async with rdb.connection() as conn:
        await skip_item(conn, user_id=me, item_id=spot)
    assert await _next(rdb, me, rate=1.0) is None
    await _age_skips(rdb, 31)
    assert (await _next(rdb, me, rate=1.0))["id"] == spot


async def test_a_retired_box_says_why_rather_than_already_answered(rdb, tmp_path, cat):
    [(f, item)] = await _boxes(rdb, tmp_path, n=1)
    me = await make_user(rdb, "me")
    for reason, words in (
        ("frame_missing", "its frame is missing"),
        ("outside_window", "outside the review window"),
    ):
        async with rdb.connection() as conn:
            await conn.execute(
                """UPDATE review_items SET status = 'retired', retired_reason = %s,
                          claimed_by = %s WHERE id = %s""",
                (reason, me, item),
            )
        with pytest.raises(Conflict) as e:
            await _verdict(
                rdb,
                cat,
                as_user(me),
                event_id=f["event_ids"][0],
                review_item_id=item,
                outcome="unidentifiable",
            )
        assert words in e.value.message and "already answered" not in e.value.message


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
    spot = await add_item(
        rdb,
        f,
        pass_=2,
        exclude_user_id=one,
        first_correction_id=first,
        added_at=datetime.now(UTC) - timedelta(hours=49),
    )
    assert (await _next(rdb, two, rate=0.0))["id"] == spot


async def test_a_spot_check_payload_says_nothing_about_the_model(rdb, tmp_path, cat):
    """Review Focus 2: not the guess, not the hints, not what it counts toward."""
    [(f, _i)] = await _boxes(rdb, tmp_path, n=1, status="done")
    one = await make_user(rdb, "one")
    first = await add_verdict(rdb, f["event_ids"][0], one, name="Seriola zonata")
    spot = await add_item(
        rdb,
        f,
        pass_=2,
        exclude_user_id=one,
        first_correction_id=first,
        predicted="Seriola zonata",
        route="Seriola zonata",
        hints=HINTS,
    )
    async with rdb.connection() as conn:
        item = await fetch_one(conn, "SELECT * FROM review_items WHERE id = %s", (spot,))
        payload = await build_payload(conn, item, catalogue=cat, counts=lambda n: 3, target=200)
    assert payload["model"] is None and payload["hints"] is None
    assert payload["counts_toward"] is None
    assert payload["item"]["lane"] is None  # the pass-1 lane is not blind information
    text = json.dumps(payload)
    for word in ("Seriola", "Pomatomus", "Pomacanthus", "Rudderfish"):
        assert word not in text


async def test_a_first_pass_payload_shows_the_guess_the_other_hints_and_progress(
    rdb, tmp_path, cat
):
    [(_f, item_id)] = await _boxes(rdb, tmp_path, n=1)
    async with rdb.connection() as conn:
        item = await fetch_one(conn, "SELECT * FROM review_items WHERE id = %s", (item_id,))
        payload = await build_payload(conn, item, catalogue=cat, counts=lambda n: 3, target=200)
    assert payload["model"]["name"] == "Seriola zonata"
    assert payload["model"]["common"] == "Banded Rudderfish"
    # the guess itself is not repeated among the hint buttons
    assert [h["name"] for h in payload["hints"]] == ["Pomatomus saltatrix", "Pomacanthus paru"]
    assert payload["counts_toward"] == {
        "name": "Seriola zonata",
        "common": "Banded Rudderfish",
        "count": 3,
        "target": 200,
    }
    assert payload["zoom"]["region"] == [0, 25, 400, 325]
    assert payload["frame"]["width"] == 1280 and payload["frame"]["height"] == 720


async def test_build_payload_treats_a_zero_byte_or_corrupt_frame_as_missing(rdb, tmp_path, cat):
    """Not only FileNotFoundError: a zero-byte or corrupt file must not
    leave the claim committed while every later `next` 500s until it
    lapses."""
    for i, content in enumerate((b"", b"this is not a jpeg")):
        f = await seed_frame(
            rdb,
            tmp_path,
            ts=T0 + timedelta(minutes=i),
            frame_id=30 + i,
            boxes=[{"bbox": [10, 10, 90, 90]}],
        )
        Path(f["image_path"]).write_bytes(content)
        item_id = await add_item(rdb, f)
        async with rdb.connection() as conn:
            item = await fetch_one(conn, "SELECT * FROM review_items WHERE id = %s", (item_id,))
            with pytest.raises(FrameMissing):
                await build_payload(conn, item, catalogue=cat, counts=lambda n: 0, target=200)


async def test_submit_verdict_treats_an_unreadable_frame_as_missing_not_a_500(rdb, tmp_path, cat):
    f = await seed_frame(
        rdb, tmp_path, boxes=[{"bbox": [10, 10, 90, 90], "species": "Seriola zonata"}]
    )
    Path(f["image_path"]).write_bytes(b"")
    user = as_user(await make_user(rdb, "one"))
    with pytest.raises(FrameMissing):
        await _verdict(rdb, cat, user, event_id=f["event_ids"][0], outcome="accept")


async def test_frame_sizes_are_read_off_the_event_loop(rdb, tmp_path, cat, monkeypatch):
    """Phase-1 Minor 7: the PIL header read runs on a worker thread from
    every async caller."""
    from app import review_queue

    seen = []
    real = review_queue.frame_size

    def spy(path):
        seen.append(threading.current_thread() is threading.main_thread())
        return real(path)

    monkeypatch.setattr(review_queue, "frame_size", spy)
    [(f, _item)] = await _boxes(rdb, tmp_path, n=1)
    me = await make_user(rdb, "me")
    served = await _next(rdb, me)
    async with rdb.connection() as conn:
        await build_payload(conn, served, catalogue=cat, counts=lambda _n: 0, target=5)
        await frame_record(conn, f["saved_frame_id"])
    await _verdict(
        rdb,
        cat,
        as_user(me),
        event_id=f["event_ids"][0],
        review_item_id=served["id"],
        outcome="unidentifiable",
    )
    assert len(seen) == 3 and not any(seen)


# ---- verdicts -----------------------------------------------------------------


async def test_accepting_the_guess_records_an_endorsement_and_closes_the_item(rdb, tmp_path, cat):
    [(f, item)] = await _boxes(rdb, tmp_path, n=1)
    one = await make_user(rdb, "one")
    await _next(rdb, one)
    out = await _verdict(
        rdb, cat, as_user(one), event_id=f["event_ids"][0], review_item_id=item, outcome="accept"
    )
    assert out["label"] == "Seriola zonata" and out["first_pass"] and out["counts"]
    row = await _row(
        rdb,
        """SELECT corrected_name, is_endorsement, label_source, review_item_id,
                                    confidence, user_id
                               FROM detection_corrections WHERE id = %s""",
        out["id"],
    )
    assert row == ("Seriola zonata", True, "model_guess", item, "certain", one)
    assert await _status(rdb, item) == "done"


async def test_a_species_from_the_search_with_its_life_stage(rdb, tmp_path, cat):
    [(f, item)] = await _boxes(rdb, tmp_path, n=1)
    one = await make_user(rdb, "one")
    await _next(rdb, one)
    out = await _verdict(
        rdb,
        cat,
        as_user(one),
        event_id=f["event_ids"][0],
        review_item_id=item,
        outcome="species",
        label="Pomacanthus paru",
        life_stage="juvenile",
        label_source="search",
        not_sure=True,
    )
    row = await _row(
        rdb,
        """SELECT corrected_name, life_stage, is_endorsement, confidence
                               FROM detection_corrections WHERE id = %s""",
        out["id"],
    )
    assert row == ("Pomacanthus paru", "juvenile", False, "uncertain")
    assert out["counts"] is False  # 'not sure' labels do not count toward progress


async def test_accept_is_validated_exactly_like_a_chosen_species(rdb, tmp_path, cat):
    """'Pomacanthus paru' has only juvenile/adult catalogue entries. Accepting
    a guess of it must be refused just like the species path already refuses
    it (Review Focus 5), not silently stored with no life stage."""
    f = await seed_frame(
        rdb, tmp_path, boxes=[{"bbox": [100, 100, 300, 250], "species": "Pomacanthus paru"}]
    )
    item = await add_item(
        rdb, f, predicted="Pomacanthus paru", predicted_p=0.7, route="Pomacanthus paru"
    )
    uid = await make_user(rdb, "one")
    user = as_user(uid)
    eid = f["event_ids"][0]
    await _next(rdb, uid)
    with pytest.raises(Invalid) as no_stage:
        await _verdict(rdb, cat, user, event_id=eid, review_item_id=item, outcome="accept")
    assert no_stage.value.message == "choose a life stage for Pomacanthus paru"
    with pytest.raises(Invalid) as bad_stage:
        await _verdict(
            rdb, cat, user, event_id=eid, review_item_id=item, outcome="accept", life_stage="adlut"
        )
    assert bad_stage.value.message == "Pomacanthus paru has no 'adlut' entry in the species list"
    out = await _verdict(
        rdb, cat, user, event_id=eid, review_item_id=item, outcome="accept", life_stage="juvenile"
    )
    assert out["label"] == "Pomacanthus paru"
    row = await _row(rdb, "SELECT life_stage FROM detection_corrections WHERE id = %s", out["id"])
    assert row == ("juvenile",)


async def test_accepting_an_unresolvable_guess_is_refused(rdb, tmp_path, cat):
    """Pre-cutover Fishial events keep a UUID-shaped species id as
    best_species_id; without an item, accept must not store it verbatim."""
    f = await seed_frame(rdb, tmp_path, boxes=[{"bbox": [10, 10, 90, 90]}])
    async with rdb.connection() as conn:
        await conn.execute(
            "UPDATE detection_events SET best_species_id = %s WHERE id = %s",
            ("3f9a6a0e-not-a-real-species", f["event_ids"][0]),
        )
    user = as_user(await make_user(rdb, "one"))
    with pytest.raises(Invalid) as exc:
        await _verdict(rdb, cat, user, event_id=f["event_ids"][0], outcome="accept")
    assert exc.value.message == (
        "the model's guess is not in the species list; " "pick the species instead"
    )


async def test_answer_validation(rdb, tmp_path, cat):
    [(f, item)] = await _boxes(rdb, tmp_path, n=1)
    uid = await make_user(rdb, "one")
    user = as_user(uid)
    eid = f["event_ids"][0]
    await _next(rdb, uid)  # a verdict is only accepted for a box served to you
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
        dict(
            outcome="species", label="Seriola zonata", label_source="search", celebrity_id="bobby"
        ),
    ]
    for extra in bad:
        with pytest.raises(Invalid):
            await _verdict(rdb, cat, user, event_id=eid, review_item_id=item, **extra)
    ok = await _verdict(
        rdb, cat, user, event_id=eid, review_item_id=item, outcome="accept", bbox=[0, 0, 1280, 720]
    )
    assert ok["label"] == "Seriola zonata"


async def test_a_second_opinion_cannot_use_the_guess_or_the_hints(rdb, tmp_path, cat):
    [(f, _i)] = await _boxes(rdb, tmp_path, n=1, status="done")
    one, two = await make_user(rdb, "one"), await make_user(rdb, "two")
    first = await add_verdict(rdb, f["event_ids"][0], one, name="Seriola zonata")
    spot = await add_item(
        rdb,
        f,
        pass_=2,
        exclude_user_id=one,
        first_correction_id=first,
        predicted="Seriola zonata",
        hints=HINTS,
    )
    got = await _next(rdb, two, rate=1.0)
    assert got["id"] == spot  # "two" must hold the claim to answer it at all
    for extra in (
        dict(outcome="accept"),
        dict(outcome="species", label="Pomatomus saltatrix", label_source="hint"),
    ):
        with pytest.raises(Invalid):
            await _verdict(
                rdb, cat, as_user(two), event_id=f["event_ids"][0], review_item_id=spot, **extra
            )
    out = await _verdict(
        rdb,
        cat,
        as_user(two),
        spot=1.0,
        event_id=f["event_ids"][0],
        review_item_id=spot,
        outcome="species",
        label="Seriola zonata",
        label_source="search",
    )
    assert out["first_pass"] is False and out["spot_check_scheduled"] is False
    assert await _status(rdb, spot) == "done"


async def test_pass_2_eligibility_is_enforced_at_submit_not_only_at_serve(rdb, tmp_path, cat):
    """The first reviewer's own response says spot_check_scheduled: true, so
    the pass-2 item's id is guessable (BIGSERIAL). Posting straight to it
    must be refused -- and per the claim-based serving rule (Finding 1,
    round 2), it's refused before the pass-2 recheck even runs, because
    neither the first reviewer nor a second answerer nor a total stranger
    holds a claim on it. A user who genuinely claims it can still answer
    it (Important 1)."""
    [(f, _i)] = await _boxes(rdb, tmp_path, n=1, status="done")
    one = await make_user(rdb, "one")
    two = await make_user(rdb, "two")
    three = await make_user(rdb, "three")
    first = await add_verdict(rdb, f["event_ids"][0], one, name="Seriola zonata")
    spot = await add_item(rdb, f, pass_=2, exclude_user_id=one, first_correction_id=first)
    # the first reviewer, by id, without ever holding a claim on it
    with pytest.raises(Forbidden):
        await _verdict(
            rdb,
            cat,
            as_user(one),
            event_id=f["event_ids"][0],
            review_item_id=spot,
            outcome="species",
            label="Seriola zonata",
            label_source="search",
        )
    # anyone else who already answered the box, not just the excluded user
    await add_verdict(rdb, f["event_ids"][0], two, name="Seriola zonata")
    with pytest.raises(Forbidden):
        await _verdict(
            rdb,
            cat,
            as_user(two),
            event_id=f["event_ids"][0],
            review_item_id=spot,
            outcome="species",
            label="Seriola zonata",
            label_source="search",
        )
    # a total stranger, by id, is refused too -- not served, full stop,
    # even though they would otherwise pass the pass-2 recheck
    with pytest.raises(Forbidden) as exc:
        await _verdict(
            rdb,
            cat,
            as_user(three),
            event_id=f["event_ids"][0],
            review_item_id=spot,
            outcome="species",
            label="Seriola zonata",
            label_source="search",
        )
    assert exc.value.message == "this box was not served to you"
    # but once "three" actually claims it, they can answer it
    got = await _next(rdb, three, rate=1.0)
    assert got["id"] == spot
    out = await _verdict(
        rdb,
        cat,
        as_user(three),
        event_id=f["event_ids"][0],
        review_item_id=spot,
        outcome="species",
        label="Seriola zonata",
        label_source="search",
    )
    assert out["first_pass"] is False
    assert await _status(rdb, spot) == "done"


async def test_pass_2_recheck_still_applies_even_with_a_genuine_claim(rdb, tmp_path, cat):
    """Defence in depth: a live, genuinely-held claim on a pass-2 item does
    not bypass the exclude_user_id / no-earlier-verdict check. Simulates a
    claim predating a same-user correction that appeared some other way --
    however that happens, the recheck must still refuse it."""
    [(f, _i)] = await _boxes(rdb, tmp_path, n=1, status="done")
    one = await make_user(rdb, "one")
    first = await add_verdict(rdb, f["event_ids"][0], one, name="Seriola zonata")
    spot = await add_item(rdb, f, pass_=2, exclude_user_id=one, first_correction_id=first)
    async with rdb.connection() as conn:
        await conn.execute(
            """UPDATE review_items SET status = 'claimed', claimed_by = %s,
                      claimed_until = now() + interval '10 minutes' WHERE id = %s""",
            (one, spot),
        )
    with pytest.raises(Forbidden) as exc:
        await _verdict(
            rdb,
            cat,
            as_user(one),
            event_id=f["event_ids"][0],
            review_item_id=spot,
            outcome="species",
            label="Seriola zonata",
            label_source="search",
        )
    assert exc.value.message == "you gave the first verdict on this box"


async def test_pass_2_already_answered_recheck_fires_even_with_a_genuine_claim(rdb, tmp_path, cat):
    """Defence in depth, the other pass-2 branch: a user who is not the
    excluded first reviewer but already has their own earlier verdict on
    this event is still refused, even holding a genuine live claim on the
    pass-2 item. Built directly with the fixtures, as next_item's own
    _SPOT_ELIGIBLE would never hand this user that claim."""
    [(f, _i)] = await _boxes(rdb, tmp_path, n=1, status="done")
    one = await make_user(rdb, "one")
    two = await make_user(rdb, "two")
    first = await add_verdict(rdb, f["event_ids"][0], one, name="Seriola zonata")
    await add_verdict(rdb, f["event_ids"][0], two, name="Pomatomus saltatrix")
    spot = await add_item(rdb, f, pass_=2, exclude_user_id=one, first_correction_id=first)
    async with rdb.connection() as conn:
        await conn.execute(
            """UPDATE review_items SET status = 'claimed', claimed_by = %s,
                      claimed_until = now() + interval '10 minutes' WHERE id = %s""",
            (two, spot),
        )
    with pytest.raises(Forbidden) as exc:
        await _verdict(
            rdb,
            cat,
            as_user(two),
            event_id=f["event_ids"][0],
            review_item_id=spot,
            outcome="species",
            label="Seriola zonata",
            label_source="search",
        )
    assert exc.value.message == "you already answered this box"


async def test_a_finished_or_held_box_is_a_conflict(rdb, tmp_path, cat):
    [(f, item)] = await _boxes(rdb, tmp_path, n=1)
    one, two = await make_user(rdb, "one"), await make_user(rdb, "two")
    await _next(rdb, one)
    with pytest.raises(Conflict):
        await _verdict(
            rdb,
            cat,
            as_user(two),
            event_id=f["event_ids"][0],
            review_item_id=item,
            outcome="accept",
        )
    await _verdict(
        rdb, cat, as_user(one), event_id=f["event_ids"][0], review_item_id=item, outcome="accept"
    )
    with pytest.raises(Conflict):
        await _verdict(
            rdb,
            cat,
            as_user(two),
            event_id=f["event_ids"][0],
            review_item_id=item,
            outcome="accept",
        )


# ---- served-to-you (Finding 1, round 2): a volunteer may only answer a box
# that was actually served to them, per spec Sec 7/15/16 -------------------


async def test_a_never_claimed_box_is_refused_not_served(rdb, tmp_path, cat):
    [(f, item)] = await _boxes(rdb, tmp_path, n=1)
    stranger = await make_user(rdb, "stranger")
    with pytest.raises(Forbidden) as exc:
        await _verdict(
            rdb,
            cat,
            as_user(stranger),
            event_id=f["event_ids"][0],
            review_item_id=item,
            outcome="accept",
        )
    assert exc.value.message == "this box was not served to you"


async def test_a_released_box_is_refused_even_to_whoever_released_it(rdb, tmp_path, cat):
    [(f, item)] = await _boxes(rdb, tmp_path, n=1)
    one = await make_user(rdb, "one")
    await _next(rdb, one)
    async with rdb.connection() as conn:
        await release_item(conn, user_id=one, item_id=item)
    with pytest.raises(Forbidden):
        await _verdict(
            rdb,
            cat,
            as_user(one),
            event_id=f["event_ids"][0],
            review_item_id=item,
            outcome="accept",
        )


async def test_a_skipped_box_is_refused_even_to_whoever_skipped_it(rdb, tmp_path, cat):
    [(f, item)] = await _boxes(rdb, tmp_path, n=1)
    one = await make_user(rdb, "one")
    await _next(rdb, one)
    async with rdb.connection() as conn:
        await skip_item(conn, user_id=one, item_id=item)
    with pytest.raises(Forbidden):
        await _verdict(
            rdb,
            cat,
            as_user(one),
            event_id=f["event_ids"][0],
            review_item_id=item,
            outcome="accept",
        )


async def test_your_own_lapsed_claim_is_accepted_before_the_sweep_runs(rdb, tmp_path, cat):
    [(f, item)] = await _boxes(rdb, tmp_path, n=1)
    one = await make_user(rdb, "one")
    await _next(rdb, one)
    async with rdb.connection() as conn:
        await conn.execute(
            "UPDATE review_items SET claimed_until = now() - interval '1 minute' WHERE id = %s",
            (item,),
        )
    out = await _verdict(
        rdb, cat, as_user(one), event_id=f["event_ids"][0], review_item_id=item, outcome="accept"
    )
    assert out["label"] == "Seriola zonata"


async def test_your_own_lapsed_claim_is_still_accepted_after_the_sweep_runs(rdb, tmp_path, cat):
    [(f, item)] = await _boxes(rdb, tmp_path, n=1)
    one, two = await make_user(rdb, "one"), await make_user(rdb, "two")
    await _next(rdb, one)
    async with rdb.connection() as conn:
        await conn.execute(
            "UPDATE review_items SET claimed_until = now() - interval '1 minute' WHERE id = %s",
            (item,),
        )
    await _next(rdb, two, lane="identify")  # any next_item call sweeps every lane
    async with rdb.connection() as conn:
        row = await fetch_one(
            conn, "SELECT status, claimed_by FROM review_items WHERE id = %s", (item,)
        )
    assert row["status"] == "open" and row["claimed_by"] == one
    out = await _verdict(
        rdb, cat, as_user(one), event_id=f["event_ids"][0], review_item_id=item, outcome="accept"
    )
    assert out["label"] == "Seriola zonata"


async def test_another_users_lapsed_claim_is_refused_not_served(rdb, tmp_path, cat):
    [(f, item)] = await _boxes(rdb, tmp_path, n=1)
    one, two = await make_user(rdb, "one"), await make_user(rdb, "two")
    await _next(rdb, one)
    async with rdb.connection() as conn:
        await conn.execute(
            "UPDATE review_items SET claimed_until = now() - interval '1 minute' WHERE id = %s",
            (item,),
        )
    with pytest.raises(Forbidden):
        await _verdict(
            rdb,
            cat,
            as_user(two),
            event_id=f["event_ids"][0],
            review_item_id=item,
            outcome="accept",
        )


async def test_a_box_reclaimed_after_your_claim_lapsed_is_a_conflict_while_live(rdb, tmp_path, cat):
    [(f, item)] = await _boxes(rdb, tmp_path, n=1)
    one, two = await make_user(rdb, "one"), await make_user(rdb, "two")
    await _next(rdb, one)
    async with rdb.connection() as conn:
        await conn.execute(
            "UPDATE review_items SET claimed_until = now() - interval '1 minute' WHERE id = %s",
            (item,),
        )
    got = await _next(rdb, two)  # sweeps it open, then claims it live for "two"
    assert got["id"] == item
    with pytest.raises(Conflict):
        await _verdict(
            rdb,
            cat,
            as_user(one),
            event_id=f["event_ids"][0],
            review_item_id=item,
            outcome="accept",
        )


async def test_skipping_after_the_sweep_still_clears_the_claimant(rdb, tmp_path, cat):
    """The sweep keeps claimed_by on a lapse (round 2), so once a box has
    been swept back to 'open' it still reads as "yours" until something
    clears it. Giving it up after that point must clear it too, or the
    volunteer who gave it up could still answer it."""
    [(f, item)] = await _boxes(rdb, tmp_path, n=1)
    one, two = await make_user(rdb, "one"), await make_user(rdb, "two")
    await _next(rdb, one)
    async with rdb.connection() as conn:
        await conn.execute(
            "UPDATE review_items SET claimed_until = now() - interval '1 minute' WHERE id = %s",
            (item,),
        )
    await _next(rdb, two, lane="identify")  # any next_item call sweeps every lane
    async with rdb.connection() as conn:
        row = await fetch_one(
            conn, "SELECT status, claimed_by FROM review_items WHERE id = %s", (item,)
        )
    assert row["status"] == "open" and row["claimed_by"] == one  # swept, still "his"
    async with rdb.connection() as conn:
        await skip_item(conn, user_id=one, item_id=item)
    async with rdb.connection() as conn:
        row = await fetch_one(
            conn, "SELECT status, claimed_by FROM review_items WHERE id = %s", (item,)
        )
    assert row["status"] == "open" and row["claimed_by"] is None
    with pytest.raises(Forbidden) as exc:
        await _verdict(
            rdb,
            cat,
            as_user(one),
            event_id=f["event_ids"][0],
            review_item_id=item,
            outcome="accept",
        )
    assert exc.value.message == "this box was not served to you"


async def test_releasing_after_the_sweep_still_clears_the_claimant(rdb, tmp_path, cat):
    [(f, item)] = await _boxes(rdb, tmp_path, n=1)
    one, two = await make_user(rdb, "one"), await make_user(rdb, "two")
    await _next(rdb, one)
    async with rdb.connection() as conn:
        await conn.execute(
            "UPDATE review_items SET claimed_until = now() - interval '1 minute' WHERE id = %s",
            (item,),
        )
    await _next(rdb, two, lane="identify")
    async with rdb.connection() as conn:
        row = await fetch_one(
            conn, "SELECT status, claimed_by FROM review_items WHERE id = %s", (item,)
        )
    assert row["status"] == "open" and row["claimed_by"] == one
    async with rdb.connection() as conn:
        await release_item(conn, user_id=one, item_id=item)
    async with rdb.connection() as conn:
        row = await fetch_one(
            conn, "SELECT status, claimed_by FROM review_items WHERE id = %s", (item,)
        )
    assert row["status"] == "open" and row["claimed_by"] is None
    with pytest.raises(Forbidden) as exc:
        await _verdict(
            rdb,
            cat,
            as_user(one),
            event_id=f["event_ids"][0],
            review_item_id=item,
            outcome="accept",
        )
    assert exc.value.message == "this box was not served to you"


async def test_without_an_item_you_can_only_answer_boxes_nobody_else_has(rdb, tmp_path, cat):
    f = await seed_frame(
        rdb, tmp_path, boxes=[{"bbox": [10, 10, 90, 90], "species": "Seriola zonata"}]
    )
    one, two = await make_user(rdb, "one"), await make_user(rdb, "two")
    boss = await make_user(rdb, "boss", role="admin")
    eid = f["event_ids"][0]
    await _verdict(rdb, cat, as_user(one), event_id=eid, outcome="accept")
    await _verdict(rdb, cat, as_user(one), event_id=eid, outcome="no_animal")  # your own: fine
    with pytest.raises(Forbidden):
        await _verdict(rdb, cat, as_user(two), event_id=eid, outcome="accept")
    await _verdict(rdb, cat, as_user(boss, role="admin"), event_id=eid, outcome="accept")


async def test_two_concurrent_no_item_submits_record_exactly_one_verdict(rdb, tmp_path, cat):
    """The no-item path's guards are plain SELECTs; without a per-event
    advisory lock serialising them against the insert, two volunteers
    racing on one box could both record a first-pass verdict (Important 2).
    Real concurrency over two separate connections, not just two coroutines
    sharing one."""
    f = await seed_frame(
        rdb, tmp_path, boxes=[{"bbox": [10, 10, 90, 90], "species": "Seriola zonata"}]
    )
    one, two = await make_user(rdb, "one"), await make_user(rdb, "two")
    eid = f["event_ids"][0]

    async def _try(uid):
        async with rdb.connection() as conn:
            try:
                return await submit_verdict(
                    conn,
                    user=as_user(uid),
                    body=VerdictIn(event_id=eid, outcome="accept"),
                    catalogue=cat,
                    spot_rate=0.0,
                    rng=random.Random(0),
                )
            except ReviewError as e:
                return e

    a, b = await asyncio.gather(_try(one), _try(two))
    oks = [r for r in (a, b) if isinstance(r, dict)]
    errs = [r for r in (a, b) if isinstance(r, ReviewError)]
    assert len(oks) == 1 and len(errs) == 1
    assert isinstance(errs[0], Forbidden | Conflict)
    row = await _row(rdb, "SELECT count(*) FROM detection_corrections WHERE event_id = %s", eid)
    assert row == (1,)


async def test_a_double_submit_within_the_window_returns_the_existing_verdict(rdb, tmp_path, cat):
    f = await seed_frame(
        rdb, tmp_path, boxes=[{"bbox": [10, 10, 90, 90], "species": "Seriola zonata"}]
    )
    one = await make_user(rdb, "one")
    eid = f["event_ids"][0]
    first = await _verdict(rdb, cat, as_user(one), event_id=eid, outcome="accept")
    assert first["duplicate"] is False
    again = await _verdict(rdb, cat, as_user(one), event_id=eid, outcome="accept")
    assert again["id"] == first["id"] and again["duplicate"] is True
    row = await _row(rdb, "SELECT count(*) FROM detection_corrections WHERE event_id = %s", eid)
    assert row == (1,)
    # a genuinely different answer from the same user is not deduplicated
    changed = await _verdict(rdb, cat, as_user(one), event_id=eid, outcome="no_animal")
    assert changed["duplicate"] is False and changed["id"] != first["id"]


async def test_every_detection_corrections_column_is_either_compared_or_excluded(rdb):
    """_duplicate_verdict compares exactly _VERDICT_IDENTITY_COLUMNS. A column
    added to the table (and to submit_verdict's INSERT) but named in neither
    tuple would go uncompared, so a resubmit that changed only that column
    within the window would be dropped as a duplicate. The two tuples must
    be disjoint and together name exactly the columns the table really has,
    read from the test database's own schema."""
    async with rdb.connection() as conn:
        cur = await conn.execute("""
            SELECT column_name FROM information_schema.columns
             WHERE table_schema = current_schema() AND table_name = 'detection_corrections'""")
        columns = {name for (name,) in await cur.fetchall()}
    compared, excluded = set(_VERDICT_IDENTITY_COLUMNS), set(_VERDICT_NON_IDENTITY_COLUMNS)
    assert compared & excluded == set()
    assert compared | excluded == columns


async def test_a_changed_corrected_name_within_the_window_is_not_deduplicated(rdb, tmp_path, cat):
    """Species X, then species Y from the same search: only corrected_name
    differs, so the second answer is a new verdict."""
    f = await seed_frame(
        rdb, tmp_path, boxes=[{"bbox": [10, 10, 90, 90], "species": "Seriola zonata"}]
    )
    one = await make_user(rdb, "one")
    eid = f["event_ids"][0]
    first = await _verdict(
        rdb,
        cat,
        as_user(one),
        event_id=eid,
        outcome="species",
        label="Seriola zonata",
        label_source="search",
    )
    changed = await _verdict(
        rdb,
        cat,
        as_user(one),
        event_id=eid,
        outcome="species",
        label="Acanthurus coeruleus",
        label_source="search",
    )
    assert changed["duplicate"] is False and changed["id"] != first["id"]
    row = await _row(
        rdb, "SELECT corrected_name FROM detection_corrections WHERE id = %s", changed["id"]
    )
    assert row == ("Acanthurus coeruleus",)


async def test_a_changed_life_stage_within_the_window_is_not_deduplicated(rdb, tmp_path, cat):
    """The model's guess accepted as a juvenile, then as an adult: only
    life_stage differs, so the second answer is a new verdict."""
    f = await seed_frame(
        rdb, tmp_path, boxes=[{"bbox": [10, 10, 90, 90], "species": "Pomacanthus paru"}]
    )
    one = await make_user(rdb, "one")
    eid = f["event_ids"][0]
    first = await _verdict(
        rdb, cat, as_user(one), event_id=eid, outcome="accept", life_stage="juvenile"
    )
    changed = await _verdict(
        rdb, cat, as_user(one), event_id=eid, outcome="accept", life_stage="adult"
    )
    assert changed["duplicate"] is False and changed["id"] != first["id"]
    row = await _row(
        rdb, "SELECT life_stage FROM detection_corrections WHERE id = %s", changed["id"]
    )
    assert row == ("adult",)


async def test_a_changed_confidence_within_the_window_is_not_deduplicated(rdb, tmp_path, cat):
    """The dedup identity must cover every stored field, not just outcome/
    label/life-stage/box -- otherwise a resubmit that only changes
    not_sure/confidence is silently dropped within the window."""
    f = await seed_frame(
        rdb, tmp_path, boxes=[{"bbox": [10, 10, 90, 90], "species": "Seriola zonata"}]
    )
    one = await make_user(rdb, "one")
    eid = f["event_ids"][0]
    first = await _verdict(rdb, cat, as_user(one), event_id=eid, outcome="accept")
    changed = await _verdict(rdb, cat, as_user(one), event_id=eid, outcome="accept", not_sure=True)
    assert changed["duplicate"] is False and changed["id"] != first["id"]
    row = await _row(
        rdb, "SELECT confidence FROM detection_corrections WHERE id = %s", changed["id"]
    )
    assert row == ("uncertain",)


async def test_a_changed_celebrity_id_within_the_window_is_not_deduplicated(rdb, tmp_path, cat):
    """One field at a time: changing celebrity_id alone must not be swallowed
    by a dedup match on everything else, so this pins celebrity_id
    specifically rather than alongside notes (deleting either comparison
    alone must not still pass this test)."""
    f = await seed_frame(
        rdb, tmp_path, boxes=[{"bbox": [10, 10, 90, 90], "species": "Acanthurus coeruleus"}]
    )
    one = await make_user(rdb, "one")
    eid = f["event_ids"][0]
    first = await _verdict(rdb, cat, as_user(one), event_id=eid, outcome="accept")
    changed = await _verdict(
        rdb, cat, as_user(one), event_id=eid, outcome="accept", celebrity_id="bobby"
    )
    assert changed["duplicate"] is False and changed["id"] != first["id"]
    row = await _row(
        rdb, "SELECT celebrity_id FROM detection_corrections WHERE id = %s", changed["id"]
    )
    assert row == ("bobby",)


async def test_a_changed_notes_within_the_window_is_not_deduplicated(rdb, tmp_path, cat):
    f = await seed_frame(
        rdb, tmp_path, boxes=[{"bbox": [10, 10, 90, 90], "species": "Seriola zonata"}]
    )
    one = await make_user(rdb, "one")
    eid = f["event_ids"][0]
    first = await _verdict(rdb, cat, as_user(one), event_id=eid, outcome="accept")
    changed = await _verdict(rdb, cat, as_user(one), event_id=eid, outcome="accept", notes="a note")
    assert changed["duplicate"] is False and changed["id"] != first["id"]
    row = await _row(rdb, "SELECT notes FROM detection_corrections WHERE id = %s", changed["id"])
    assert row == ("a note",)


async def test_a_changed_box_within_the_window_is_not_deduplicated(rdb, tmp_path, cat):
    f = await seed_frame(
        rdb, tmp_path, boxes=[{"bbox": [10, 10, 90, 90], "species": "Seriola zonata"}]
    )
    one = await make_user(rdb, "one")
    eid = f["event_ids"][0]
    first = await _verdict(
        rdb, cat, as_user(one), event_id=eid, outcome="accept", bbox=[10, 10, 90, 90]
    )
    changed = await _verdict(
        rdb, cat, as_user(one), event_id=eid, outcome="accept", bbox=[20, 20, 100, 100]
    )
    assert changed["duplicate"] is False and changed["id"] != first["id"]
    row = await _row(
        rdb, "SELECT corrected_bbox FROM detection_corrections WHERE id = %s", changed["id"]
    )
    assert list(row[0]) == [20, 20, 100, 100]


async def test_a_changed_label_source_is_not_deduplicated_and_the_dedup_response_would_lie(
    rdb, tmp_path, cat
):
    """label_source is the ONLY stored difference between 'accept' and a
    'species' pick of the exact same name -- so without comparing it, an
    accept followed by the same species chosen from search is wrongly
    merged, and a (pre-fix) dedup response describing it would say
    outcome: 'species' about a row whose label_source is still
    'model_guess'. This is also the response-describes-the-stored-row
    test: on the pre-fix code (round 2, 851dd12) this whole test fails,
    because the second call comes back duplicate: true with outcome
    'species' instead of inserting a new row."""
    f = await seed_frame(
        rdb, tmp_path, boxes=[{"bbox": [10, 10, 90, 90], "species": "Seriola zonata"}]
    )
    one = await make_user(rdb, "one")
    eid = f["event_ids"][0]
    first = await _verdict(rdb, cat, as_user(one), event_id=eid, outcome="accept")
    assert first["duplicate"] is False and first["outcome"] == "accept"
    changed = await _verdict(
        rdb,
        cat,
        as_user(one),
        event_id=eid,
        outcome="species",
        label="Seriola zonata",
        label_source="search",
    )
    assert changed["duplicate"] is False and changed["id"] != first["id"]
    assert changed["outcome"] == "species"
    rows = await _row(
        rdb,
        """SELECT count(*) FROM detection_corrections
                               WHERE event_id = %s AND user_id = %s""",
        eid,
        one,
    )
    assert rows == (2,)
    row = await _row(
        rdb, "SELECT label_source FROM detection_corrections WHERE id = %s", first["id"]
    )
    assert row == ("model_guess",)  # the accept row is untouched by the later search pick


async def test_duplicate_verdict_reads_outcome_from_the_stored_row_not_the_argument(rdb, tmp_path):
    """Once label_source is part of the identity, no request submit_verdict
    would ever validate can reach _duplicate_verdict with an `outcome`
    argument that disagrees with the stored row it matches -- that's the
    whole point of comparing it. So the only way left to pin the response's
    own outcome-from-stored-row rule is to call _duplicate_verdict directly
    with a mismatched outcome argument that still matches on every stored
    identity column: this fails on the pre-fix code, where the response
    echoes the argument ("species") instead of the stored row ("accept")."""
    f = await seed_frame(
        rdb, tmp_path, boxes=[{"bbox": [10, 10, 90, 90], "species": "Seriola zonata"}]
    )
    one = await make_user(rdb, "one")
    eid = f["event_ids"][0]
    await add_verdict(rdb, eid, one, name="Seriola zonata", label_source="model_guess")
    v = {
        "name": "Seriola zonata",
        "life_stage": None,
        "label_source": "model_guess",
        "confidence": "certain",
        "celebrity_id": None,
        "notes": None,
        "bbox": None,
    }
    async with rdb.connection() as conn:
        dup = await _duplicate_verdict(conn, event_id=eid, user_id=one, v=v, outcome="species")
    assert dup is not None
    assert dup["outcome"] == "accept"  # the stored row's outcome, not the "species" passed in


async def test_a_duplicate_response_describes_the_stored_rows_label_and_counts(rdb, tmp_path, cat):
    f = await seed_frame(
        rdb, tmp_path, boxes=[{"bbox": [10, 10, 90, 90], "species": "Seriola zonata"}]
    )
    one = await make_user(rdb, "one")
    eid = f["event_ids"][0]
    first = await _verdict(
        rdb,
        cat,
        as_user(one),
        event_id=eid,
        outcome="species",
        label="Seriola zonata",
        label_source="search",
        not_sure=True,
    )
    again = await _verdict(
        rdb,
        cat,
        as_user(one),
        event_id=eid,
        outcome="species",
        label="Seriola zonata",
        label_source="search",
        not_sure=True,
    )
    assert again["duplicate"] is True and again["id"] == first["id"]
    assert again["outcome"] == "species"
    row = await _row(
        rdb,
        """SELECT corrected_name, confidence FROM detection_corrections
                               WHERE id = %s""",
        again["id"],
    )
    assert row == ("Seriola zonata", "uncertain")
    assert again["label"] == row[0]
    assert again["counts"] is False  # matches the stored row's 'uncertain' confidence, not a
    # freshly re-derived value from the new request


async def test_a_first_pass_verdict_can_schedule_a_spot_check(rdb, tmp_path, cat):
    [(f, item)] = await _boxes(rdb, tmp_path, n=1)
    one = await make_user(rdb, "one")
    await _next(rdb, one)
    out = await _verdict(
        rdb,
        cat,
        as_user(one),
        spot=1.0,
        event_id=f["event_ids"][0],
        review_item_id=item,
        outcome="accept",
    )
    assert out["spot_check_scheduled"]
    row = await _row(
        rdb,
        """SELECT pass, status, exclude_user_id, first_correction_id
                               FROM review_items WHERE event_id = %s AND pass = 2""",
        f["event_ids"][0],
    )
    assert row == (2, "open", one, out["id"])


async def test_reopening_shows_your_last_answer_and_box(rdb, tmp_path, cat):
    [(f, item)] = await _boxes(rdb, tmp_path, n=1)
    one, two = await make_user(rdb, "one"), await make_user(rdb, "two")
    eid = f["event_ids"][0]
    await _next(rdb, one)
    await _verdict(
        rdb,
        cat,
        as_user(one),
        event_id=eid,
        review_item_id=item,
        outcome="accept",
        bbox=[90, 90, 310, 260],
    )
    async with rdb.connection() as conn:
        p = await reopen_payload(
            conn, user=as_user(one), event_id=eid, catalogue=cat, counts=lambda n: 0, target=200
        )
        assert p["item"]["id"] is None and p["box"]["bbox"] == [90, 90, 310, 260]
        assert p["mine"]["outcome"] == "species" and p["mine"]["label"] == "Seriola zonata"
        assert p["editable"] is True and p["reason"] is None
        with pytest.raises(Forbidden):
            await reopen_payload(
                conn, user=as_user(two), event_id=eid, catalogue=cat, counts=lambda n: 0, target=200
            )


async def test_reopen_marks_the_box_read_only_once_a_resubmit_would_be_refused(rdb, tmp_path, cat):
    [(f, item)] = await _boxes(rdb, tmp_path, n=1)
    one = await make_user(rdb, "one")
    eid = f["event_ids"][0]
    await _next(rdb, one)
    out = await _verdict(
        rdb, cat, as_user(one), spot=1.0, event_id=eid, review_item_id=item, outcome="accept"
    )
    two = await make_user(rdb, "two")
    spot_id = (await _next(rdb, two, rate=1.0))["id"]
    await _verdict(
        rdb,
        cat,
        as_user(two),
        event_id=eid,
        review_item_id=spot_id,
        outcome="species",
        label="Seriola zonata",
        label_source="search",
    )
    assert out["id"]  # sanity: the first verdict really was recorded
    async with rdb.connection() as conn:
        p = await reopen_payload(
            conn, user=as_user(one), event_id=eid, catalogue=cat, counts=lambda n: 0, target=200
        )
    assert p["editable"] is False
    assert p["reason"] == "another volunteer already answered this box"


async def _reopen(rdb, cat, user, event_id):
    async with rdb.connection() as conn:
        return await reopen_payload(
            conn, user=user, event_id=event_id, catalogue=cat, counts=lambda _name: 0, target=5
        )


async def _spot_checked_box(rdb, tmp_path, *, status="open"):
    """A box volunteer "one" answered first, with a spot-check on it."""
    f = await seed_frame(
        rdb, tmp_path, boxes=[{"bbox": [100, 100, 300, 250], "species": "Seriola zonata"}]
    )
    one = await make_user(rdb, "one")
    await add_item(
        rdb, f, predicted="Seriola zonata", route="Seriola zonata", hints=HINTS, status="done"
    )
    first = await add_verdict(rdb, f["event_ids"][0], one, name="Seriola zonata")
    spot = await add_item(
        rdb, f, pass_=2, status=status, exclude_user_id=one, first_correction_id=first
    )
    return f, one, spot


def _is_blind(payload):
    return (
        payload["model"] is None
        and payload["hints"] is None
        and payload["counts_toward"] is None
        and payload["item"]["pass"] == 2
        and payload["item"]["lane"] is None
    )


@pytest.mark.parametrize("status", ["open", "claimed"])
async def test_an_admin_reopening_a_pending_spot_check_sees_the_picture_alone(
    rdb, tmp_path, cat, status
):
    f, _one, _spot = await _spot_checked_box(rdb, tmp_path, status=status)
    boss = await make_user(rdb, "boss", role="admin")
    payload = await _reopen(rdb, cat, as_user(boss, role="admin"), f["event_ids"][0])
    assert _is_blind(payload) and "mine" not in payload
    assert payload["editable"] is False
    assert payload["reason"] == "a second opinion is pending on this box"


async def test_once_the_spot_check_is_answered_an_admin_reopen_shows_the_guess(rdb, tmp_path, cat):
    f, _one, _spot = await _spot_checked_box(rdb, tmp_path, status="done")
    boss = await make_user(rdb, "boss", role="admin")
    payload = await _reopen(rdb, cat, as_user(boss, role="admin"), f["event_ids"][0])
    assert payload["model"]["name"] == "Seriola zonata" and payload["item"]["pass"] == 1


async def test_an_admin_reopening_their_own_spot_check_answer_sees_it_blind(rdb, tmp_path, cat):
    f, _one, spot = await _spot_checked_box(rdb, tmp_path, status="done")
    boss = await make_user(rdb, "boss", role="admin")
    await add_verdict(
        rdb, f["event_ids"][0], boss, unidentifiable=True, item_id=spot, bbox=[110, 110, 290, 240]
    )
    payload = await _reopen(rdb, cat, as_user(boss, role="admin"), f["event_ids"][0])
    assert _is_blind(payload)
    assert payload["mine"]["outcome"] == "unidentifiable"
    assert payload["mine"]["bbox"] == [110, 110, 290, 240]


async def test_an_admins_later_adjudication_does_not_unblind_their_spot_check_answer(
    rdb, tmp_path, cat
):
    """Ruling T2a: reopen's blind check must look at ANY of the caller's
    answers linked to a pass-2 item, not only their latest row -- an
    adjudication recorded after the spot-check answer must not make the
    box look answerable-with-a-guess again."""
    f, _one, spot = await _spot_checked_box(rdb, tmp_path, status="done")
    boss = await make_user(rdb, "boss", role="admin")
    await add_verdict(rdb, f["event_ids"][0], boss, unidentifiable=True, item_id=spot)
    await add_verdict(rdb, f["event_ids"][0], boss, name="Seriola zonata", is_adjudication=True)
    payload = await _reopen(rdb, cat, as_user(boss, role="admin"), f["event_ids"][0])
    assert _is_blind(payload)
    assert "mine" in payload


async def test_an_admins_re_answer_to_their_own_spot_check_stays_a_second_opinion(
    rdb, tmp_path, cat
):
    f, _one, spot = await _spot_checked_box(rdb, tmp_path, status="done")
    boss = await make_user(rdb, "boss", role="admin")
    await add_verdict(rdb, f["event_ids"][0], boss, unidentifiable=True, item_id=spot)
    admin = as_user(boss, role="admin")
    out = await _verdict(
        rdb,
        cat,
        admin,
        spot=1.0,
        event_id=f["event_ids"][0],
        outcome="species",
        label="Seriola zonata",
        label_source="search",
    )
    assert out["first_pass"] is False and out["spot_check_scheduled"] is False
    row = await _row(
        rdb, "SELECT review_item_id FROM detection_corrections WHERE id = %s", out["id"]
    )
    assert row[0] == spot
    count = await _row(
        rdb,
        """SELECT count(*) FROM review_items
                                WHERE event_id = %s AND pass = 2""",
        f["event_ids"][0],
    )
    assert count[0] == 1
    with pytest.raises(Invalid):  # a second opinion has no guess to accept
        await _verdict(rdb, cat, admin, event_id=f["event_ids"][0], outcome="accept")


async def test_the_re_answer_is_deduplicated(rdb, tmp_path, cat):
    f, _one, spot = await _spot_checked_box(rdb, tmp_path, status="done")
    boss = await make_user(rdb, "boss", role="admin")
    await add_verdict(rdb, f["event_ids"][0], boss, unidentifiable=True, item_id=spot)
    admin = as_user(boss, role="admin")
    body = {
        "event_id": f["event_ids"][0],
        "outcome": "species",
        "label": "Seriola zonata",
        "label_source": "search",
    }
    first = await _verdict(rdb, cat, admin, **body)
    again = await _verdict(rdb, cat, admin, **body)
    assert again["duplicate"] is True and again["id"] == first["id"]
    assert again["first_pass"] is False


async def test_an_outsider_with_no_answer_is_refused_while_a_spot_check_is_pending(
    rdb, tmp_path, cat
):
    """Ruling T2b: a pending pass-2 item is a live second opinion -- an
    admin with no answer of their own on the box is an outsider to it, not
    the first reviewer changing their mind, and must be refused rather
    than being allowed to jump in through the whole-frame path."""
    f, _one, _spot = await _spot_checked_box(rdb, tmp_path, status="open")
    boss = await make_user(rdb, "boss", role="admin")
    admin = as_user(boss, role="admin")
    with pytest.raises(Conflict):
        await _verdict(
            rdb,
            cat,
            admin,
            event_id=f["event_ids"][0],
            outcome="species",
            label="Seriola zonata",
            label_source="search",
        )
    count = await _row(rdb, "SELECT count(*) FROM detection_corrections WHERE user_id = %s", boss)
    assert count[0] == 0


async def test_the_first_reviewer_reopening_while_their_spot_check_is_pending_is_not_blind(
    rdb, tmp_path, cat
):
    """The first reviewer already has an answer on the box, so a spot-check
    pending on it does not make them an outsider: their reopen still shows
    the guess, and their whole-frame re-answer still goes through."""
    f, one, _spot = await _spot_checked_box(rdb, tmp_path, status="open")
    payload = await _reopen(rdb, cat, as_user(one), f["event_ids"][0])
    assert payload["item"]["pass"] == 1 and payload["model"] is not None
    assert payload["editable"] is True and payload["reason"] is None
    out = await _verdict(
        rdb,
        cat,
        as_user(one),
        event_id=f["event_ids"][0],
        outcome="species",
        label="Seriola zonata",
        label_source="search",
    )
    assert out["id"]

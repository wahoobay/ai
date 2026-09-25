"""Progress, browse, disputes, agreement, the date window, pool status."""
from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from unittest import mock

import psycopg
import pytest
from app import review_browse as review_browse_module
from app.review_browse import (
    BrowseFilters,
    agreement,
    browse,
    list_disputes,
    pool_status,
    progress_summary,
    set_window,
)
from app.review_catalogue import CatalogueStore
from app.review_pool import PoolSettings, ReviewPool, read_window
from app.review_queue import Invalid
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


@pytest.fixture
def store(review_assets):
    return CatalogueStore(catalogue_paths(review_assets))


async def _world(rdb, tmp_path):
    """Five answered boxes and one open queue item, by two volunteers."""
    one, two = await make_user(rdb, "one"), await make_user(rdb, "two")
    f = await seed_frame(rdb, tmp_path, ts=T0, boxes=[
        {"bbox": [10, 10, 90, 90], "species": "Seriola zonata"},
        {"bbox": [100, 10, 190, 90], "species": "Seriola zonata"},
        {"bbox": [200, 10, 290, 90]},
        {"bbox": [300, 10, 390, 90], "species": "Seriola zonata"},
        {"bbox": [400, 10, 490, 90], "species": "Seriola zonata"}])
    g = await seed_frame(rdb, tmp_path, ts=T0 + timedelta(days=1), frame_id=2,
                         source="pier_cam", boxes=[{"bbox": [10, 10, 90, 90]}])
    e = f["event_ids"]
    await add_verdict(rdb, e[0], one, name="Seriola zonata")                     # single
    a1 = await add_verdict(rdb, e[1], one, name="Seriola zonata", label_source="model_guess")
    s1 = await add_item(rdb, f, 1, pass_=2, status="done", exclude_user_id=one,
                        first_correction_id=a1)
    await add_verdict(rdb, e[1], two, name="Seriola zonata", item_id=s1)          # agreed
    await add_verdict(rdb, e[2], one, not_a_fish=True)                            # no animal
    d1 = await add_verdict(rdb, e[3], one, name="Seriola zonata", label_source="model_guess")
    s2 = await add_item(rdb, f, 3, pass_=2, status="done", exclude_user_id=one,
                        first_correction_id=d1)
    await add_verdict(rdb, e[3], two, unidentifiable=True, item_id=s2)            # disputed
    p1 = await add_verdict(rdb, e[4], one, name="Seriola zonata")
    await add_item(rdb, f, 4, pass_=2, exclude_user_id=one, first_correction_id=p1)  # pending
    await add_item(rdb, g, 0, lane="identify")                                    # in queue
    return {"one": one, "two": two, "f": f, "g": g}


async def test_progress_counts(rdb, tmp_path, store, review_assets):
    await _world(rdb, tmp_path)
    pool = ReviewPool(rdb, PoolSettings(target=200), store, review_assets["baseline"])
    await pool.refresh_progress()
    async with rdb.connection() as conn:
        p = await progress_summary(conn, pool_obj=pool, catalogue=store.get(), session_id=None,
                                   target=200)
    k = p["kpis"]
    assert k["verified_boxes"] == 4           # single, agreed, no-animal, pending-but-single
    assert k["species_with_verified"] == 1
    assert k["open"] == {"check": 0, "identify": 1, "spot_checks": 1}
    seriola = next(s for s in p["species"] if s["name"] == "Seriola zonata")
    assert seriola["baseline"] == 3 and seriola["verified"] == 3
    assert p["species"][0]["baseline"] + p["species"][0]["verified"] == 0   # thinnest first


async def test_progress_survives_a_stale_refresh(rdb, rclient, tmp_path, monkeypatch):
    """review_pool.refresh_progress runs under its own 5s statement
    timeout and can raise QueryCanceled under load (Task 6's fix rounds).
    The /api/review/progress handler must catch it, log a warning, and
    still answer with the counts it has -- flagged stale -- never a 500.
    Fix round 1, finding 9d: the summary itself must still be the real
    one, not just a bare stale flag."""
    await _world(rdb, tmp_path)
    await make_user(rdb, "rev")
    login(rclient, "rev")

    async def boom(self):
        raise psycopg.errors.QueryCanceled("canceling statement due to statement timeout")

    monkeypatch.setattr(ReviewPool, "refresh_progress", boom)
    r = rclient.get("/api/review/progress")
    assert r.status_code == 200
    body = r.json()
    assert body["stale"] is True
    assert body["kpis"]["verified_boxes"] == 4        # the real summary, not a stub
    assert body["species"]


async def test_progress_returns_503_when_its_own_query_times_out(rdb, rclient, monkeypatch):
    """Unlike the refresh above, this is progress_summary's own query
    timing out: there is no cached summary to fall back to, so this must
    be a clear 503, never a hang and never a 500. Fix round 1, finding 6."""
    await make_user(rdb, "rev")
    login(rclient, "rev")

    async def boom(conn):
        raise psycopg.errors.QueryCanceled("canceling statement due to statement timeout")

    monkeypatch.setattr(review_browse_module, "_resolved", boom)
    r = rclient.get("/api/review/progress")
    assert r.status_code == 503


async def test_browse_filters(rdb, tmp_path, store):
    w = await _world(rdb, tmp_path)
    one = as_user(w["one"])
    two = as_user(w["two"])

    async def cells(user, **kw):
        async with rdb.connection() as conn:
            return (await browse(conn, catalogue=store.get(), user=user,
                                 filters=BrowseFilters(**kw), page=0))["cells"]

    assert {c["status"] for c in await cells(one, status="disputed")} == {"disputed"}
    assert len(await cells(one, status="verified")) == 4
    assert [c["kind"] for c in await cells(one, status="in_queue")] == ["queue"]
    assert len(await cells(two, who="me")) == 2
    # all() on an empty result is vacuously true, so check the filter
    # actually matched something before checking what it matched
    # (fix round 1, finding 9c).
    in_queue_pier = await cells(one, status="in_queue", camera="pier_cam")
    assert in_queue_pier
    assert all(c["camera"] == "pier_cam" for c in in_queue_pier)
    # the pending spot-check hides its answer from everyone but its first reviewer
    hidden = [c for c in await cells(two) if c["status"] == "awaiting_second_opinion"]
    assert len(hidden) == 1 and hidden[0]["label"] is None
    assert not [c for c in await cells(one) if c["status"] == "awaiting_second_opinion"]


async def test_admin_browsing_does_not_see_a_pending_spot_check_either(rdb, tmp_path, store):
    """Spec Sec7 makes no admin exception: admins can be served a
    spot-check themselves, so browse must not let them read ahead of one
    they didn't give the first verdict on -- exactly as whole-frame mode
    already enforces (test_review_frames.py)."""
    await _world(rdb, tmp_path)
    boss = await make_user(rdb, "boss", role="admin")
    admin = as_user(boss, role="admin")
    async with rdb.connection() as conn:
        cells = (await browse(conn, catalogue=store.get(), user=admin,
                              filters=BrowseFilters(), page=0))["cells"]
    hidden = [c for c in cells if c["status"] == "awaiting_second_opinion"]
    assert len(hidden) == 1
    assert hidden[0]["outcome"] is None and hidden[0]["label"] is None
    assert "Seriola zonata" not in json.dumps(hidden[0])


async def test_browse_only_looks_up_frames_for_the_returned_page(rdb, tmp_path, store):
    """The saved_frames lookup must never grow with the candidate set,
    only with the page being returned (fix round 1, finding 1: with the
    plan's original unbounded join, this spy would see every candidate
    event, not just the page's)."""
    one = await make_user(rdb, "one")
    boxes = [{"bbox": [10 + i * 100, 10, 90 + i * 100, 90], "species": "Seriola zonata"}
             for i in range(5)]
    f = await seed_frame(rdb, tmp_path, boxes=boxes)
    for event_id in f["event_ids"]:
        await add_verdict(rdb, event_id, one, name="Seriola zonata")
    user = as_user(one)
    real_fetch_all = review_browse_module.fetch_all
    frame_lookup_sizes = []

    async def spy(conn, sql, params=None):
        if "unnest" in sql and "saved_frames" in sql:
            frame_lookup_sizes.append(len(params[0]))
        return await real_fetch_all(conn, sql, params)

    async with rdb.connection() as conn:
        with mock.patch.object(review_browse_module, "fetch_all", spy):
            result = await browse(conn, catalogue=store.get(), user=user,
                                  filters=BrowseFilters(), page=0, page_size=2)
    assert len(result["cells"]) == 2
    assert result["total"] == 5
    assert frame_lookup_sizes == [2]          # not 5: only the page's cells


async def test_species_and_status_filters_cannot_surface_a_pending_spot_check(rdb, tmp_path,
                                                                               store):
    """Deleting the hidden-cell guard (review_browse.py's `if hidden and
    (filters.status != "any" or filters.species): continue`) would let
    `?species=<the pending answer>` or a non-'any' `?status=<its
    outcome>` list the pending box early, using its real (hidden)
    r.species/r.status -- every other browse test in this file only
    browses the pending box under the default status='any' with no
    species filter, so this guard was otherwise untested (fix round 1,
    finding 2)."""
    w = await _world(rdb, tmp_path)
    two = as_user(w["two"])
    pending_event_id = w["f"]["event_ids"][4]

    async def cells(**kw):
        async with rdb.connection() as conn:
            return (await browse(conn, catalogue=store.get(), user=two,
                                 filters=BrowseFilters(**kw), page=0))["cells"]

    by_species = await cells(species="Seriola zonata")
    assert by_species                                # not vacuously empty (e0, e1, e3 match)
    assert pending_event_id not in {c["event_id"] for c in by_species}
    assert not any(c["status"] == "awaiting_second_opinion" for c in by_species)

    by_status = await cells(status="verified")
    assert by_status                                 # not vacuously empty (e0, e1, e2 match)
    assert pending_event_id not in {c["event_id"] for c in by_status}
    assert not any(c["status"] == "awaiting_second_opinion" for c in by_status)


async def test_a_hidden_cells_lane_is_null_and_never_matches_a_lane_filter(rdb, tmp_path, store):
    """"check" would tell the viewer the model had a species guess, which
    the blind pass-2 payload itself hides (review_queue.build_payload
    nulls `lane` there too). Fix round 1, finding 3."""
    w = await _world(rdb, tmp_path)
    two = as_user(w["two"])
    pending_event_id = w["f"]["event_ids"][4]

    async def cells(**kw):
        async with rdb.connection() as conn:
            return (await browse(conn, catalogue=store.get(), user=two,
                                 filters=BrowseFilters(**kw), page=0))["cells"]

    hidden = next(c for c in await cells() if c["event_id"] == pending_event_id)
    assert hidden["status"] == "awaiting_second_opinion" and hidden["lane"] is None
    # e[4]'s model guess was a species, so its real lane is "check" -- a
    # lane filter of either value must still not surface it.
    for lane in ("check", "identify"):
        assert pending_event_id not in {c["event_id"] for c in await cells(lane=lane)}


async def test_drawn_boxes_show_under_the_unidentifiable_filter(rdb, tmp_path, store):
    """Drawn boxes have a single, immediate answer, so they belong under
    'verified'/'any' regardless of outcome, but were missing entirely
    from the 'unidentifiable' filter even when that is their outcome.
    Fix round 1, finding 7."""
    w = await _world(rdb, tmp_path)
    one = as_user(w["one"])
    async with rdb.connection() as conn:
        await conn.execute("""
            INSERT INTO review_added_boxes (saved_frame_id, bbox, outcome, user_id)
            VALUES (%s, %s, 'unidentifiable', %s)""",
            (w["f"]["saved_frame_id"], [500, 10, 590, 90], w["one"]))
        # A species-outcome drawn box must NOT leak into the
        # unidentifiable filter through a missing per-row outcome check.
        await conn.execute("""
            INSERT INTO review_added_boxes (saved_frame_id, bbox, outcome, label, user_id)
            VALUES (%s, %s, 'species', 'Seriola zonata', %s)""",
            (w["f"]["saved_frame_id"], [600, 10, 690, 90], w["one"]))

    async def cells(**kw):
        async with rdb.connection() as conn:
            return (await browse(conn, catalogue=store.get(), user=one,
                                 filters=BrowseFilters(**kw), page=0))["cells"]

    unidentifiable = await cells(status="unidentifiable")
    drawn = [c for c in unidentifiable if c["kind"] == "added"]
    assert len(drawn) == 1 and drawn[0]["outcome"] == "unidentifiable"
    assert all(c["outcome"] == "unidentifiable" for c in unidentifiable)
    both_drawn = [c for c in await cells(status="verified") if c["kind"] == "added"]
    assert len(both_drawn) == 2                       # "verified" shows either outcome


async def test_disputes_and_adjudication(rdb, rclient, tmp_path):
    await _world(rdb, tmp_path)
    await make_user(rdb, "boss", role="admin")
    login(rclient, "boss")
    disputes = rclient.get("/api/review/disputes").json()["disputes"]
    assert len(disputes) == 1
    d = disputes[0]
    assert d["first"]["label"] == "Seriola zonata" and d["second"]["outcome"] == "unidentifiable"
    r = rclient.post(f"/api/review/disputes/{d['event_id']}/adjudicate", headers=CSRF,
                     json={"outcome": "species", "label": "Seriola zonata"})
    assert r.status_code == 200
    assert rclient.get("/api/review/disputes").json()["disputes"] == []


async def test_adjudicate_refuses_a_box_that_is_not_disputed(rdb, rclient, tmp_path):
    """The adjudication path in submit_verdict skips every claim/item
    guard (spec Sec8 says adjudication is for disputes), so without this
    check a POST here could close a queued box's open pass-1 item out
    from under a volunteer who is mid-answer. Fix round 1, finding 8."""
    w = await _world(rdb, tmp_path)
    await make_user(rdb, "boss", role="admin")
    login(rclient, "boss")
    single_event_id = w["f"]["event_ids"][0]          # a plain single verdict, not disputed
    r = rclient.post(f"/api/review/disputes/{single_event_id}/adjudicate", headers=CSRF,
                     json={"outcome": "species", "label": "Seriola zonata"})
    assert r.status_code == 409
    assert r.json()["error"] == "this box is not disputed"
    queued_event_id = w["g"]["event_ids"][0]          # never answered at all
    r = rclient.post(f"/api/review/disputes/{queued_event_id}/adjudicate", headers=CSRF,
                     json={"outcome": "species", "label": "Seriola zonata"})
    assert r.status_code == 409


async def test_agreement_numbers(rdb, tmp_path):
    await _world(rdb, tmp_path)
    async with rdb.connection() as conn:
        a = await agreement(conn)
    assert a["overall"]["pairs"] == 2 and a["overall"]["percent"] == 0.5
    assert a["by_label_source"]["model_guess"]["pairs"] == 2


async def test_the_window_end_day_is_inclusive_in_local_time(rdb, tmp_path):
    """Review Focus 4: 2026-06-25 22:00 EDT (2026-06-26T02:00Z) is inside
    a window ending 2026-06-25. That instant is AFTER UTC midnight on the
    26th, so a naive UTC-only end check -- which a 2026-06-25T22:00Z
    fixture couldn't tell apart from the correct local-time one, since
    both are before UTC midnight either way -- would wrongly retire it
    too (fix round 1, finding 9a)."""
    boss = await make_user(rdb, "boss", role="admin")
    inside = await seed_frame(rdb, tmp_path, ts=datetime(2026, 6, 26, 2, 0, tzinfo=UTC),
                              frame_id=1, boxes=[{"bbox": [10, 10, 90, 90]}])
    outside = await seed_frame(rdb, tmp_path, ts=datetime(2026, 6, 26, 9, 0, tzinfo=UTC),
                               frame_id=2, boxes=[{"bbox": [10, 10, 90, 90]}])
    i_in, i_out = await add_item(rdb, inside, lane="identify"), await add_item(
        rdb, outside, lane="identify")
    async with rdb.connection() as conn:
        out = await set_window(conn, admin_id=boss, start=date(2026, 5, 17),
                               end=date(2026, 6, 25))
        assert out["retired"] == 1
        statuses = dict(await (await conn.execute(
            "SELECT id, status FROM review_items")).fetchall())
    assert statuses[i_in] == "open" and statuses[i_out] == "retired"
    assert out["end"].endswith("-04:00")              # local (EDT), not UTC


async def test_set_window_stores_timezone_aware_datetimes(rdb, store, review_assets):
    """A naive or date-only stored value makes the background pool's own
    `end = min(admin_end, now(UTC) - ...)` in ReviewPool._window raise
    TypeError on every run. read_window must hand back aware datetimes,
    and the pool's own comparison against them must not raise."""
    boss = await make_user(rdb, "boss", role="admin")
    async with rdb.connection() as conn:
        await set_window(conn, admin_id=boss, start=date(2026, 5, 17), end=date(2026, 6, 25))
        start, end = await read_window(conn)
        assert start.tzinfo is not None and end.tzinfo is not None
        pool = ReviewPool(rdb, PoolSettings(), store, review_assets["baseline"])
        effective, admin_window = await pool._window(conn)
    assert admin_window == (start, end)
    assert effective is not None
    assert effective[0].tzinfo is not None and effective[1].tzinfo is not None


async def test_set_window_refuses_a_future_start_or_a_backwards_range(rdb):
    """A start after today (local time) would retire every open pass-1
    item permanently: the pool never re-adds an event that already has a
    review_items row. An end before the start stays refused too. Fix
    round 1, finding 4."""
    boss = await make_user(rdb, "boss", role="admin")
    async with rdb.connection() as conn:
        with pytest.raises(Invalid):
            await set_window(conn, admin_id=boss, start=date.today() + timedelta(days=30),
                             end=None)
        with pytest.raises(Invalid):
            await set_window(conn, admin_id=boss, start=date(2026, 6, 25), end=date(2026, 5, 17))


async def test_set_window_rejects_an_out_of_range_date(rdb):
    """end=9999-12-31 + one day overflows Python's date range and would
    otherwise surface as a 500. Fix round 1, finding 5."""
    boss = await make_user(rdb, "boss", role="admin")
    async with rdb.connection() as conn:
        with pytest.raises(Invalid):
            await set_window(conn, admin_id=boss, start=None, end=date(9999, 12, 31))
        with pytest.raises(Invalid):
            await set_window(conn, admin_id=boss, start=date(1, 1, 1), end=None)


async def test_browse_rejects_bad_filters_instead_of_a_500(rdb, store):
    """date_to=9999-12-31 raises OverflowError at `+ timedelta(days=1)`
    unless it is caught first; who and lane accept only their documented
    values. Fix round 1, finding 5."""
    user = as_user(1)
    async with rdb.connection() as conn:
        with pytest.raises(Invalid):
            await browse(conn, catalogue=store.get(), user=user,
                        filters=BrowseFilters(date_to=date(9999, 12, 31)), page=0)
        with pytest.raises(Invalid):
            await browse(conn, catalogue=store.get(), user=user,
                        filters=BrowseFilters(date_from=date(1, 1, 1)), page=0)
        with pytest.raises(Invalid):
            await browse(conn, catalogue=store.get(), user=user,
                        filters=BrowseFilters(who="everyone"), page=0)
        with pytest.raises(Invalid):
            await browse(conn, catalogue=store.get(), user=user,
                        filters=BrowseFilters(lane="both"), page=0)


async def test_admin_endpoints_are_admin_only(rdb, rclient):
    await make_user(rdb, "rev")
    login(rclient, "rev")
    for path in ("/api/review/disputes", "/api/review/agreement", "/api/review/status",
                 "/api/review/settings/window"):
        assert rclient.get(path).status_code == 403
    assert rclient.get("/api/review/progress").status_code == 200
    # fix round 1, finding 9b: PUT window and POST adjudicate as a plain
    # reviewer, not just the GETs above.
    assert rclient.put("/api/review/settings/window", headers=CSRF,
                       json={"start": "2026-05-17", "end": "2026-06-25"}).status_code == 403
    assert rclient.post("/api/review/disputes/1/adjudicate", headers=CSRF,
                        json={"outcome": "species", "label": "Seriola zonata"}
                        ).status_code == 403
    await make_user(rdb, "boss", role="admin")
    login(rclient, "boss")
    assert rclient.put("/api/review/settings/window",
                       json={"start": "2026-05-17", "end": "2026-06-25"}).status_code == 403
    # fix round 1, finding 9b: adjudicate without the CSRF header, as admin.
    assert rclient.post("/api/review/disputes/1/adjudicate",
                        json={"outcome": "species", "label": "Seriola zonata"}
                        ).status_code == 403
    r = rclient.put("/api/review/settings/window", headers=CSRF,
                    json={"start": "2026-05-17", "end": "2026-06-25"})
    assert r.status_code == 200 and r.json()["end"].startswith("2026-06-26T00:00:00")
    status = rclient.get("/api/review/status").json()
    assert status["window"]["end"].startswith("2026-06-26") and "last_run" in status


async def test_pool_status_counts_items(rdb, tmp_path):
    await _world(rdb, tmp_path)
    async with rdb.connection() as conn:
        s = await pool_status(conn, pool_obj=None)
    assert {"lane": "identify", "pass": 1, "status": "open", "count": 1} in s["items"]
    assert s["unscored"] == 2 and s["last_run"] is None

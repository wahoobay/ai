"""Progress, browse, disputes, agreement, the date window, pool status."""
from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import psycopg
import pytest
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


async def test_progress_survives_a_stale_refresh(rdb, rclient, monkeypatch):
    """review_pool.refresh_progress runs under its own 5s statement
    timeout and can raise QueryCanceled under load (Task 6's fix rounds).
    The /api/review/progress handler must catch it, log a warning, and
    still answer with the counts it has -- flagged stale -- never a 500."""
    await make_user(rdb, "rev")
    login(rclient, "rev")

    async def boom(self):
        raise psycopg.errors.QueryCanceled("canceling statement due to statement timeout")

    monkeypatch.setattr(ReviewPool, "refresh_progress", boom)
    r = rclient.get("/api/review/progress")
    assert r.status_code == 200
    assert r.json()["stale"] is True


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
    assert all(c["camera"] == "pier_cam" for c in await cells(one, status="in_queue",
                                                              camera="pier_cam"))
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


async def test_agreement_numbers(rdb, tmp_path):
    await _world(rdb, tmp_path)
    async with rdb.connection() as conn:
        a = await agreement(conn)
    assert a["overall"]["pairs"] == 2 and a["overall"]["percent"] == 0.5
    assert a["by_label_source"]["model_guess"]["pairs"] == 2


async def test_the_window_end_day_is_inclusive_in_local_time(rdb, tmp_path):
    """Review Focus 4: 2026-06-25 18:00 EDT is inside a window ending 2026-06-25."""
    boss = await make_user(rdb, "boss", role="admin")
    inside = await seed_frame(rdb, tmp_path, ts=datetime(2026, 6, 25, 22, 0, tzinfo=UTC),
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


async def test_admin_endpoints_are_admin_only(rdb, rclient):
    await make_user(rdb, "rev")
    login(rclient, "rev")
    for path in ("/api/review/disputes", "/api/review/agreement", "/api/review/status",
                 "/api/review/settings/window"):
        assert rclient.get(path).status_code == 403
    assert rclient.get("/api/review/progress").status_code == 200
    await make_user(rdb, "boss", role="admin")
    login(rclient, "boss")
    assert rclient.put("/api/review/settings/window",
                       json={"start": "2026-05-17", "end": "2026-06-25"}).status_code == 403
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

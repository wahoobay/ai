"""The date window is the reviewer's own, for the session (Dan, 2026-10-27): it travels with the
claim request, and nothing is stored or retired on the server. The old shared setting, left at a
seeded sample's dates, silently emptied the staging queue for weeks."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import pytest
from review_fixtures import T0, add_item, login, make_user, seed_frame

pytestmark = pytest.mark.anyio
CSRF = {"X-Requested-With": "wahoobay"}
BOX = [{"bbox": [10, 10, 90, 90]}]


async def test_next_item_serves_only_boxes_inside_the_callers_window(rdb, tmp_path):
    from app.review_queue import next_item

    early = await seed_frame(rdb, tmp_path, ts=T0, frame_id=1, boxes=BOX)
    late = await seed_frame(rdb, tmp_path, ts=T0 + timedelta(days=2), frame_id=2, boxes=BOX)
    i_early = await add_item(rdb, early, lane="identify")
    i_late = await add_item(rdb, late, lane="identify")
    uid = await make_user(rdb, "rev")
    async with rdb.connection() as conn:
        got = await next_item(
            conn,
            user_id=uid,
            lane="identify",
            species_order=[],
            spot_serve_rate=0.0,
            claim_s=600,
            rng=random.Random(0),
            window_start=T0 + timedelta(days=1),
            window_end=T0 + timedelta(days=3),
        )
    assert got is not None and got["id"] == i_late
    async with rdb.connection() as conn:
        statuses = dict(
            await (await conn.execute("SELECT id, status FROM review_items")).fetchall()
        )
    assert statuses[i_early] == "open"  # neither retired nor touched: another reviewer may want it


async def test_the_window_end_day_is_inclusive_in_local_time(rdb, rclient, tmp_path, monkeypatch):
    """2026-06-25 22:00 EDT (2026-06-26T02:00Z) is inside a window ending 2026-06-25; 05:00 EDT on
    the 26th is not. A UTC-only end check could not tell them apart. The site's zone comes from TZ."""
    monkeypatch.setenv("TZ", "America/New_York")
    inside = await seed_frame(
        rdb, tmp_path, ts=datetime(2026, 6, 26, 2, 0, tzinfo=UTC), frame_id=1, boxes=BOX
    )
    outside = await seed_frame(
        rdb, tmp_path, ts=datetime(2026, 6, 26, 9, 0, tzinfo=UTC), frame_id=2, boxes=BOX
    )
    i_in = await add_item(rdb, inside, lane="identify")
    await add_item(rdb, outside, lane="identify")
    await make_user(rdb, "rev")
    login(rclient, "rev")
    r = rclient.get(
        "/api/review/next?lane=identify&date_from=2026-05-17&date_to=2026-06-25", headers=CSRF
    )
    assert r.status_code == 200 and r.json()["item"]["id"] == i_in
    r = rclient.get("/api/review/next?lane=identify&date_from=2026-06-27", headers=CSRF)
    assert (
        r.status_code == 200 and r.json()["item"] is None
    )  # nothing that late; nothing retired either
    r = rclient.get(
        "/api/review/next?lane=identify&date_from=2026-06-25&date_to=2026-05-17", headers=CSRF
    )
    assert r.status_code == 400 and "error" in r.json()
    async with rdb.connection() as conn:
        rows = await (
            await conn.execute("SELECT count(*) FROM review_items WHERE status = 'retired'")
        ).fetchone()
    assert rows[0] == 0


def test_the_window_lives_in_the_browser_session_and_the_server_has_no_setting():
    from js_source import STATIC
    from js_source import read as _read

    core = _read("review-core.js")
    browse = _read("review-browse.js")
    html = (STATIC.parent / "templates" / "review.html").read_text()
    assert 'id="rv-window-from"' in html and 'id="rv-window-to"' in html
    assert "sessionStorage" in core and "date_from=" in core and "date_to=" in core
    assert "/api/review/settings/window" not in browse and "windowPanel" not in browse


async def test_the_migration_reopens_what_the_old_window_retired(rdb, tmp_path):
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "m0004",
        Path(__file__).resolve().parents[1]
        / "db"
        / "migrations"
        / "versions"
        / "0004_review_window_per_session.py",
    )
    m0004 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m0004)
    frame = await seed_frame(rdb, tmp_path, ts=T0, frame_id=1, boxes=BOX + BOX)
    hidden = await add_item(rdb, frame, 0, lane="identify", status="retired")
    other = await add_item(rdb, frame, 1, lane="identify", status="retired")
    async with rdb.connection() as conn:
        await conn.execute(
            "UPDATE review_items SET retired_reason = 'outside_window' WHERE id = %s", (hidden,)
        )
        await conn.execute(
            "UPDATE review_items SET retired_reason = 'frame_missing' WHERE id = %s", (other,)
        )
        await conn.execute(m0004.REOPEN_SQL)
        rows = dict(await (await conn.execute("SELECT id, status FROM review_items")).fetchall())
    assert (
        rows[hidden] == "open" and rows[other] == "retired"
    )  # only the window's casualties come back

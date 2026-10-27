"""The review pool (spec §6) and the image helpers it relies on."""

from __future__ import annotations

import asyncio
import csv
import io
import os
import random
import threading
import time
from datetime import timedelta

import pytest
from app import review_pool
from app.review_catalogue import CatalogueStore
from app.review_images import frame_size, render_zoom, zoom_region, zoom_scale
from app.review_pool import TIMEOUT_BACKOFF_S, PoolSettings, ReviewPool
from app.review_queue import EVENT_LOCK_SQL
from PIL import Image
from psycopg.types.json import Jsonb
from review_fixtures import (
    T0,
    TAXONOMY_FIELDS,
    add_verdict,
    catalogue_paths,
    jpeg,
    make_user,
    seed_frame,
    wait_until_blocked,
)


def test_zoom_region_adds_half_a_box_of_margin_and_clamps():
    assert zoom_region([100, 100, 200, 150], 1280, 720) == (50, 75, 250, 175)
    assert zoom_region([0, 0, 100, 100], 1280, 720) == (0, 0, 150, 150)
    assert zoom_region([1200, 650, 1280, 720], 1280, 720) == (1160, 615, 1280, 720)


def test_zoom_scale_caps_at_three_and_at_960_pixels():
    assert zoom_scale((0, 0, 100, 50)) == 3.0
    assert zoom_scale((0, 0, 1280, 720)) == pytest.approx(0.75)


def test_render_zoom_and_frame_size(tmp_path):
    path = jpeg(tmp_path / "f.jpg")
    assert frame_size(path) == (1280, 720)
    im = Image.open(io.BytesIO(render_zoom(path, (50, 75, 250, 175))))
    assert im.size == (600, 300)


@pytest.mark.parametrize(
    "value, enabled",
    [
        (None, True),
        ("true", True),
        ("1", True),
        ("yes", True),
        ("on", True),
        ("", True),
        ("banana", True),
        ("false", False),
        ("0", False),
        ("no", False),
        ("off", False),
        ("FALSE", False),
        (" Off ", False),
        ("No\n", False),
    ],
)
def test_review_pool_enabled_is_off_for_the_usual_spellings_only(monkeypatch, value, enabled):
    """Final review Minor 11: the Task 14 rollback switch. 0, false, no and
    off switch the pool off, in any case and with surrounding whitespace;
    anything else, or unset, leaves it on (the default)."""
    from app.main import DashboardCfg

    if value is None:
        monkeypatch.delenv("REVIEW_POOL_ENABLED", raising=False)
    else:
        monkeypatch.setenv("REVIEW_POOL_ENABLED", value)
    assert DashboardCfg().review_pool_enabled is enabled


pytestmark_db = pytest.mark.anyio


def _pool(rdb, review_assets, **overrides):
    kw = {
        "max_probes": 30,
        "max_run_s": 20,
        "check_open_per_species": 5,
        "identify_open": 0,
        "target": 5,
    }
    kw.update(overrides)
    store = CatalogueStore(catalogue_paths(review_assets))
    return ReviewPool(
        rdb, PoolSettings(**kw), store, review_assets["baseline"], rng=random.Random(0)
    )


async def _items(rdb, lane=None):
    async with rdb.connection() as conn:
        cur = await conn.execute(
            """SELECT event_id, lane, saved_frame_id, predicted, route_species, status
                 FROM review_items WHERE (%s::text IS NULL OR lane = %s) ORDER BY event_id""",
            (lane, lane),
        )
        return await cur.fetchall()


async def _insert_args(rdb, tmp_path):
    f = await seed_frame(
        rdb, tmp_path, boxes=[{"bbox": [10, 10, 90, 90], "species": "Seriola zonata"}]
    )
    return f, {
        "event_id": f["event_ids"][0],
        "lane": "check",
        "saved_frame_id": f["saved_frame_id"],
        "source": f["source"],
        "ts": f["ts"],
        "bbox": [10, 10, 90, 90],
        "predicted": "Seriola zonata",
        "predicted_p": 0.6,
        "route": "Seriola zonata",
    }


@pytestmark_db
async def test_the_pool_never_queues_a_box_answered_after_its_free_check(
    rdb, review_assets, tmp_path
):
    pool = _pool(rdb, review_assets)
    f, args = await _insert_args(rdb, tmp_path)
    await add_verdict(rdb, f["event_ids"][0], await make_user(rdb, "one"), name="Seriola zonata")
    async with rdb.connection() as conn:
        assert await pool._insert(conn, **args) is False
    assert not await _items(rdb)


@pytestmark_db
async def test_the_pool_waits_for_a_verdict_in_flight_on_the_same_box(rdb, review_assets, tmp_path):
    """Real concurrency: a verdict holds the event's lock and has inserted
    its row but not committed. The pool's insert must wait for it and then
    see the row, not queue the box in between."""
    pool = _pool(rdb, review_assets)
    _f, args = await _insert_args(rdb, tmp_path)
    uid = await make_user(rdb, "one")

    async def insert():
        async with rdb.connection() as conn:
            return await pool._insert(conn, **args)

    async with rdb.connection() as holder:
        async with holder.transaction():
            await holder.execute(EVENT_LOCK_SQL, (args["event_id"],))
            await holder.execute(
                """INSERT INTO detection_corrections (event_id, corrected_name, user_id)
                   VALUES (%s, 'Seriola zonata', %s)""",
                (args["event_id"], uid),
            )
            task = asyncio.create_task(insert())
            await wait_until_blocked(rdb)
        # the verdict commits here
    assert await task is False
    assert not await _items(rdb)


@pytestmark_db
async def test_check_lane_takes_guessed_boxes_that_have_a_saved_frame(rdb, review_assets, tmp_path):
    saved = await seed_frame(
        rdb,
        tmp_path,
        ts=T0,
        frame_id=10,
        boxes=[{"bbox": [100, 100, 300, 250], "species": "Pomatomus saltarix"}],
    )
    await seed_frame(
        rdb,
        tmp_path,
        ts=T0 + timedelta(minutes=10),
        frame_id=11,
        save=False,
        boxes=[{"bbox": [100, 100, 300, 250], "species": "Pomatomus saltarix"}],
    )
    stats = await _pool(rdb, review_assets).run_once()
    rows = await _items(rdb, "check")
    assert [r[0] for r in rows] == saved["event_ids"]
    # stored under the accepted name, so it counts toward the right species
    assert rows[0][3] == rows[0][4] == "Pomatomus saltatrix"
    assert stats["check_added"] == 1 and stats["errors"] == 0 and stats["probes"] > 0


@pytestmark_db
async def test_a_reused_frame_id_is_matched_by_timestamp(rdb, review_assets, tmp_path):
    # frame_id 500 was used twice across a worker restart. The detection
    # belongs to the earlier frame; the later one must not be picked.
    before = await seed_frame(
        rdb,
        tmp_path,
        ts=T0,
        frame_id=500,
        boxes=[{"bbox": [100, 100, 300, 250], "species": "Seriola zonata"}],
    )
    await seed_frame(rdb, tmp_path, ts=T0 + timedelta(hours=3), frame_id=500, boxes=[])
    stats = await _pool(rdb, review_assets).run_once()
    assert stats["errors"] == 0 and stats["probes"] > 0 and stats["check_added"] == 1
    rows = await _items(rdb, "check")
    assert [(r[0], r[2]) for r in rows] == [(before["event_ids"][0], before["saved_frame_id"])]


@pytestmark_db
async def test_small_boxes_close_neighbours_and_answered_boxes_are_skipped(
    rdb, review_assets, tmp_path
):
    tiny = await seed_frame(
        rdb,
        tmp_path,
        ts=T0,
        frame_id=1,
        boxes=[{"bbox": [10, 10, 30, 30], "species": "Seriola zonata"}],
    )
    kept = await seed_frame(
        rdb,
        tmp_path,
        ts=T0 + timedelta(minutes=5),
        frame_id=2,
        boxes=[{"bbox": [10, 10, 90, 90], "species": "Seriola zonata"}],
    )
    near = await seed_frame(
        rdb,
        tmp_path,
        ts=T0 + timedelta(minutes=5, seconds=30),
        frame_id=3,
        boxes=[{"bbox": [10, 10, 90, 90], "species": "Seriola zonata"}],
    )
    answered = await seed_frame(
        rdb,
        tmp_path,
        ts=T0 + timedelta(minutes=30),
        frame_id=4,
        boxes=[{"bbox": [10, 10, 90, 90], "species": "Seriola zonata"}],
    )
    uid = await make_user(rdb, "one")
    await add_verdict(rdb, answered["event_ids"][0], uid, name="Seriola zonata")
    stats = await _pool(rdb, review_assets).run_once()
    assert stats["errors"] == 0 and stats["probes"] > 0 and stats["check_added"] == 1
    got = {r[0] for r in await _items(rdb, "check")}
    # newest first: 'near' is taken, so 'kept' (30 s earlier) is too close to it
    assert near["event_ids"][0] in got
    assert not ({tiny["event_ids"][0], kept["event_ids"][0], answered["event_ids"][0]} & got)


@pytestmark_db
async def test_species_at_target_are_not_topped_up(rdb, review_assets, tmp_path):
    # the fixture training set already has 10 Blue Tang crops; target is 5
    await seed_frame(
        rdb, tmp_path, boxes=[{"bbox": [10, 10, 90, 90], "species": "Acanthurus coeruleus"}]
    )
    stats = await _pool(rdb, review_assets).run_once()
    assert stats["errors"] == 0
    assert stats["probes"] > 0  # the other, genuinely-empty species still get probed
    assert stats["check_added"] == 0
    assert await _items(rdb, "check") == []


@pytestmark_db
async def test_identify_lane_takes_the_largest_confident_unguessed_box(
    rdb, review_assets, tmp_path
):
    frames = []
    for i in range(3):
        frames.append(
            await seed_frame(
                rdb,
                tmp_path,
                ts=T0 + timedelta(minutes=10 * i),
                frame_id=100 + i,
                boxes=[
                    {"bbox": [0, 0, 60, 60]},  # small but fine
                    {"bbox": [100, 100, 400, 300]},  # the largest
                    {"bbox": [500, 100, 900, 500], "det_conf": 0.3},  # larger, not confident
                    {"bbox": [0, 400, 300, 700], "species": "Seriola zonata"},  # guessed
                ],
            )
        )
    pool = _pool(rdb, review_assets, identify_open=10)
    stats = await pool.run_once()
    assert stats["errors"] == 0
    rows = await _items(rdb, "identify")
    assert sorted(r[0] for r in rows) == sorted(f["event_ids"][1] for f in frames)
    assert all(r[3] is None and r[4] is None for r in rows)
    assert stats["identify_added"] == 3


@pytestmark_db
async def test_progress_counts_baseline_and_verified_boxes(rdb, review_assets, tmp_path):
    frame = await seed_frame(
        rdb,
        tmp_path,
        boxes=[
            {"bbox": [10, 10, 90, 90], "species": "Seriola zonata"},
            {"bbox": [100, 10, 190, 90], "species": "Seriola zonata"},
        ],
    )
    uid = await make_user(rdb, "one")
    await add_verdict(rdb, frame["event_ids"][0], uid, name="Seriola zonata")
    await add_verdict(
        rdb, frame["event_ids"][1], uid, name="Seriola zonata", confidence="uncertain"
    )
    pool = _pool(rdb, review_assets)
    await pool.refresh_progress()
    assert pool.baseline("Seriola zonata") == 3  # the 'review:' record is excluded
    assert pool.verified("Seriola zonata") == 1  # the 'not sure' one is not counted
    assert pool.baseline("Pomatomus saltatrix") == 1  # misspelled label, accepted name
    order = pool.species_order()
    assert "Acanthurus coeruleus" not in order  # 10 >= target 5
    assert order.index("Neopomacentrus cyanomos") < order.index("Seriola zonata")


@pytestmark_db
async def test_load_baseline_runs_off_the_event_loop(rdb, review_assets, monkeypatch):
    """Fix round 1, ruling T5a: refresh_progress runs on every /api/review/
    progress request and every pool cycle, not once at start-up as the
    original brief assumed -- _load_baseline's stat, catalogue rebuild and
    JSON parse must not block the event loop there either."""
    pool = _pool(rdb, review_assets)
    seen = []
    real = pool._load_baseline

    def spy():
        seen.append(threading.current_thread() is threading.main_thread())
        return real()

    monkeypatch.setattr(pool, "_load_baseline", spy)
    await pool.refresh_progress()
    assert seen == [False]


@pytestmark_db
async def test_the_pool_never_retires_items_for_a_date_window(rdb, review_assets, tmp_path):
    """The window is each reviewer's own (test_review_window.py); the pool admits everything up to a
    few minutes ago and a leftover settings row from the old shared window changes nothing."""
    early = await seed_frame(
        rdb,
        tmp_path,
        ts=T0,
        frame_id=1,
        boxes=[{"bbox": [10, 10, 90, 90], "species": "Seriola zonata"}],
    )
    late = await seed_frame(
        rdb,
        tmp_path,
        ts=T0 + timedelta(days=2),
        frame_id=2,
        boxes=[{"bbox": [10, 10, 90, 90], "species": "Seriola zonata"}],
    )
    assert early and late
    pool = _pool(rdb, review_assets)
    stats1 = await pool.run_once()
    assert stats1["errors"] == 0 and stats1["probes"] > 0 and stats1["check_added"] == 2
    async with rdb.connection() as conn:
        await conn.execute(
            "INSERT INTO review_settings (key, value) VALUES ('window', %s)",
            (
                Jsonb(
                    {
                        "start": (T0 + timedelta(days=1)).isoformat(),
                        "end": (T0 + timedelta(days=3)).isoformat(),
                    }
                ),
            ),
        )
    stats2 = await pool.run_once()
    assert stats2["errors"] == 0 and "retired" not in stats2
    async with rdb.connection() as conn:
        n_open = (
            await (
                await conn.execute("SELECT count(*) FROM review_items WHERE status = 'open'")
            ).fetchone()
        )[0]
    assert n_open == 2

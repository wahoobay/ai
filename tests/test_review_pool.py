"""The review pool (spec §6) and the image helpers it relies on."""
from __future__ import annotations

import io
import random
from datetime import timedelta

import pytest
from app.review_catalogue import CatalogueStore
from app.review_images import frame_size, render_zoom, zoom_region, zoom_scale
from app.review_pool import PoolSettings, ReviewPool
from PIL import Image
from psycopg.types.json import Jsonb
from review_fixtures import T0, add_verdict, catalogue_paths, jpeg, make_user, seed_frame


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


pytestmark_db = pytest.mark.anyio


def _pool(rdb, review_assets, **overrides):
    kw = {"max_probes": 30, "max_run_s": 20, "check_open_per_species": 5, "identify_open": 0,
          "target": 5}
    kw.update(overrides)
    store = CatalogueStore(catalogue_paths(review_assets))
    return ReviewPool(rdb, PoolSettings(**kw), store, review_assets["baseline"],
                      rng=random.Random(0))


async def _items(rdb, lane=None):
    async with rdb.connection() as conn:
        cur = await conn.execute(
            """SELECT event_id, lane, saved_frame_id, predicted, route_species, status
                 FROM review_items WHERE (%s::text IS NULL OR lane = %s) ORDER BY event_id""",
            (lane, lane))
        return await cur.fetchall()


@pytestmark_db
async def test_check_lane_takes_guessed_boxes_that_have_a_saved_frame(rdb, review_assets, tmp_path):
    saved = await seed_frame(rdb, tmp_path, ts=T0, frame_id=10,
                             boxes=[{"bbox": [100, 100, 300, 250], "species": "Pomatomus saltarix"}])
    await seed_frame(rdb, tmp_path, ts=T0 + timedelta(minutes=10), frame_id=11, save=False,
                     boxes=[{"bbox": [100, 100, 300, 250], "species": "Pomatomus saltarix"}])
    stats = await _pool(rdb, review_assets).run_once()
    rows = await _items(rdb, "check")
    assert [r[0] for r in rows] == saved["event_ids"]
    # stored under the accepted name, so it counts toward the right species
    assert rows[0][3] == rows[0][4] == "Pomatomus saltatrix"
    assert stats["check_added"] == 1 and stats["errors"] == 0


@pytestmark_db
async def test_a_reused_frame_id_is_matched_by_timestamp(rdb, review_assets, tmp_path):
    # frame_id 500 was used twice across a worker restart. The detection
    # belongs to the earlier frame; the later one must not be picked.
    before = await seed_frame(rdb, tmp_path, ts=T0, frame_id=500,
                              boxes=[{"bbox": [100, 100, 300, 250], "species": "Seriola zonata"}])
    await seed_frame(rdb, tmp_path, ts=T0 + timedelta(hours=3), frame_id=500, boxes=[])
    await _pool(rdb, review_assets).run_once()
    rows = await _items(rdb, "check")
    assert [(r[0], r[2]) for r in rows] == [(before["event_ids"][0], before["saved_frame_id"])]


@pytestmark_db
async def test_small_boxes_close_neighbours_and_answered_boxes_are_skipped(rdb, review_assets,
                                                                           tmp_path):
    tiny = await seed_frame(rdb, tmp_path, ts=T0, frame_id=1,
                            boxes=[{"bbox": [10, 10, 30, 30], "species": "Seriola zonata"}])
    kept = await seed_frame(rdb, tmp_path, ts=T0 + timedelta(minutes=5), frame_id=2,
                            boxes=[{"bbox": [10, 10, 90, 90], "species": "Seriola zonata"}])
    near = await seed_frame(rdb, tmp_path, ts=T0 + timedelta(minutes=5, seconds=30), frame_id=3,
                            boxes=[{"bbox": [10, 10, 90, 90], "species": "Seriola zonata"}])
    answered = await seed_frame(rdb, tmp_path, ts=T0 + timedelta(minutes=30), frame_id=4,
                                boxes=[{"bbox": [10, 10, 90, 90], "species": "Seriola zonata"}])
    uid = await make_user(rdb, "one")
    await add_verdict(rdb, answered["event_ids"][0], uid, name="Seriola zonata")
    await _pool(rdb, review_assets).run_once()
    got = {r[0] for r in await _items(rdb, "check")}
    # newest first: 'near' is taken, so 'kept' (30 s earlier) is too close to it
    assert near["event_ids"][0] in got
    assert not ({tiny["event_ids"][0], kept["event_ids"][0], answered["event_ids"][0]} & got)


@pytestmark_db
async def test_species_at_target_are_not_topped_up(rdb, review_assets, tmp_path):
    # the fixture training set already has 10 Blue Tang crops; target is 5
    await seed_frame(rdb, tmp_path, boxes=[{"bbox": [10, 10, 90, 90],
                                            "species": "Acanthurus coeruleus"}])
    await _pool(rdb, review_assets).run_once()
    assert await _items(rdb, "check") == []


@pytestmark_db
async def test_identify_lane_takes_the_largest_confident_unguessed_box(rdb, review_assets,
                                                                        tmp_path):
    frames = []
    for i in range(3):
        frames.append(await seed_frame(
            rdb, tmp_path, ts=T0 + timedelta(minutes=10 * i), frame_id=100 + i, boxes=[
                {"bbox": [0, 0, 60, 60]},                           # small but fine
                {"bbox": [100, 100, 400, 300]},                     # the largest
                {"bbox": [500, 100, 900, 500], "det_conf": 0.3},    # larger, not confident
                {"bbox": [0, 400, 300, 700], "species": "Seriola zonata"},  # guessed
            ]))
    pool = _pool(rdb, review_assets, identify_open=10)
    stats = await pool.run_once()
    rows = await _items(rdb, "identify")
    assert sorted(r[0] for r in rows) == sorted(f["event_ids"][1] for f in frames)
    assert all(r[3] is None and r[4] is None for r in rows)
    assert stats["identify_added"] == 3


@pytestmark_db
async def test_progress_counts_baseline_and_verified_boxes(rdb, review_assets, tmp_path):
    frame = await seed_frame(rdb, tmp_path, boxes=[
        {"bbox": [10, 10, 90, 90], "species": "Seriola zonata"},
        {"bbox": [100, 10, 190, 90], "species": "Seriola zonata"}])
    uid = await make_user(rdb, "one")
    await add_verdict(rdb, frame["event_ids"][0], uid, name="Seriola zonata")
    await add_verdict(rdb, frame["event_ids"][1], uid, name="Seriola zonata",
                      confidence="uncertain")
    pool = _pool(rdb, review_assets)
    await pool.refresh_progress()
    assert pool.baseline("Seriola zonata") == 3        # the 'review:' record is excluded
    assert pool.verified("Seriola zonata") == 1        # the 'not sure' one is not counted
    assert pool.baseline("Pomatomus saltatrix") == 1   # misspelled label, accepted name
    order = pool.species_order()
    assert "Acanthurus coeruleus" not in order         # 10 >= target 5
    assert order.index("Neopomacentrus cyanomos") < order.index("Seriola zonata")


@pytestmark_db
async def test_the_date_window_retires_items_outside_it(rdb, review_assets, tmp_path):
    early = await seed_frame(rdb, tmp_path, ts=T0, frame_id=1,
                             boxes=[{"bbox": [10, 10, 90, 90], "species": "Seriola zonata"}])
    late = await seed_frame(rdb, tmp_path, ts=T0 + timedelta(days=2), frame_id=2,
                            boxes=[{"bbox": [10, 10, 90, 90], "species": "Seriola zonata"}])
    pool = _pool(rdb, review_assets)
    await pool.run_once()
    async with rdb.connection() as conn:
        await conn.execute(
            "INSERT INTO review_settings (key, value) VALUES ('window', %s)",
            (Jsonb({"start": (T0 + timedelta(days=1)).isoformat(),
                    "end": (T0 + timedelta(days=3)).isoformat()}),))
    stats = await pool.run_once()
    status = {r[0]: r[5] for r in await _items(rdb, "check")}
    assert status[early["event_ids"][0]] == "retired" and stats["retired"] == 1
    assert status[late["event_ids"][0]] == "open"


@pytestmark_db
async def test_a_run_does_not_overlap_the_previous_one(rdb, review_assets):
    pool = _pool(rdb, review_assets)
    async with pool._lock:
        assert (await pool.run_once()) == {"skipped": "previous run still going"}


@pytestmark_db
async def test_the_statement_timeout_never_outlives_a_probe(rdb, review_assets, tmp_path):
    """Review Focus 1: a session-level timeout would stay on the pooled
    connection and kill the next borrower's long statement."""
    async def timeouts():
        async with rdb.connection() as a, rdb.connection() as b, rdb.connection() as c, \
                rdb.connection() as d:
            return [(await (await x.execute("SHOW statement_timeout")).fetchone())[0]
                    for x in (a, b, c, d)]

    before = await timeouts()
    await seed_frame(rdb, tmp_path, boxes=[{"bbox": [10, 10, 90, 90],
                                            "species": "Seriola zonata"}])
    await _pool(rdb, review_assets, identify_open=5).run_once()
    assert await timeouts() == before

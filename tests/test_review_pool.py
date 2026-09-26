"""The review pool (spec §6) and the image helpers it relies on."""
from __future__ import annotations

import csv
import io
import os
import random
import time
from datetime import timedelta

import pytest
from app import review_pool
from app.review_catalogue import CatalogueStore
from app.review_images import frame_size, render_zoom, zoom_region, zoom_scale
from app.review_pool import TIMEOUT_BACKOFF_S, PoolSettings, ReviewPool
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


@pytest.mark.parametrize("value, enabled", [
    (None, True), ("true", True), ("1", True), ("yes", True), ("on", True), ("", True),
    ("banana", True),
    ("false", False), ("0", False), ("no", False), ("off", False), ("FALSE", False),
    (" Off ", False), ("No\n", False),
])
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
    assert stats["check_added"] == 1 and stats["errors"] == 0 and stats["probes"] > 0


@pytestmark_db
async def test_a_reused_frame_id_is_matched_by_timestamp(rdb, review_assets, tmp_path):
    # frame_id 500 was used twice across a worker restart. The detection
    # belongs to the earlier frame; the later one must not be picked.
    before = await seed_frame(rdb, tmp_path, ts=T0, frame_id=500,
                              boxes=[{"bbox": [100, 100, 300, 250], "species": "Seriola zonata"}])
    await seed_frame(rdb, tmp_path, ts=T0 + timedelta(hours=3), frame_id=500, boxes=[])
    stats = await _pool(rdb, review_assets).run_once()
    assert stats["errors"] == 0 and stats["probes"] > 0 and stats["check_added"] == 1
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
    stats = await _pool(rdb, review_assets).run_once()
    assert stats["errors"] == 0 and stats["probes"] > 0 and stats["check_added"] == 1
    got = {r[0] for r in await _items(rdb, "check")}
    # newest first: 'near' is taken, so 'kept' (30 s earlier) is too close to it
    assert near["event_ids"][0] in got
    assert not ({tiny["event_ids"][0], kept["event_ids"][0], answered["event_ids"][0]} & got)


@pytestmark_db
async def test_species_at_target_are_not_topped_up(rdb, review_assets, tmp_path):
    # the fixture training set already has 10 Blue Tang crops; target is 5
    await seed_frame(rdb, tmp_path, boxes=[{"bbox": [10, 10, 90, 90],
                                            "species": "Acanthurus coeruleus"}])
    stats = await _pool(rdb, review_assets).run_once()
    assert stats["errors"] == 0
    assert stats["probes"] > 0          # the other, genuinely-empty species still get probed
    assert stats["check_added"] == 0
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
    assert stats["errors"] == 0
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
    stats1 = await pool.run_once()
    assert stats1["errors"] == 0 and stats1["probes"] > 0 and stats1["check_added"] == 2
    async with rdb.connection() as conn:
        await conn.execute(
            "INSERT INTO review_settings (key, value) VALUES ('window', %s)",
            (Jsonb({"start": (T0 + timedelta(days=1)).isoformat(),
                    "end": (T0 + timedelta(days=3)).isoformat()}),))
    stats2 = await pool.run_once()
    assert stats2["errors"] == 0 and stats2["probes"] > 0
    status = {r[0]: r[5] for r in await _items(rdb, "check")}
    assert status[early["event_ids"][0]] == "retired" and stats2["retired"] == 1
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
    stats = await _pool(rdb, review_assets, identify_open=5).run_once()
    assert stats["errors"] == 0 and stats["probes"] > 0
    assert await timeouts() == before


@pytestmark_db
async def test_probes_are_never_prepared_on_the_server(rdb, review_assets):
    """Final review Minor 8 / ledger 119: psycopg prepares a statement
    server-side once a pooled connection has run it prepare_threshold (5)
    times, and PostgreSQL may then pick a generic plan for the probe that
    scripts/dev/explain_review_probes.py never shows. A probe runs
    unprepared however often its connection has run it."""
    pool = _pool(rdb, review_assets)
    probe_sql = "SELECT %s::int AS n  -- review pool probe"
    stats = {"probes": 0, "timeouts": 0}
    async with rdb.connection() as conn:
        for n in range(8):
            assert await pool._probe(conn, probe_sql, (n,), stats) == [{"n": n}]
        cur = await conn.execute("SELECT statement FROM pg_prepared_statements")
        prepared = [r[0] for r in await cur.fetchall()]
    assert not [s for s in prepared if "review pool probe" in s]


@pytestmark_db
async def test_a_real_probe_timeout_backs_the_species_off_and_survives_a_second_run(
        rdb, review_assets, tmp_path, monkeypatch):
    """Fix round 2: the re-review found the round-1 backoff test monkeypatched
    `_probe` itself, so the real QueryCanceled path was never exercised, and
    it only ran once, so nothing proved the backoff survived a second run.
    This one forces a REAL Postgres cancellation through the real _probe
    code path (a slow statement under a tiny statement_timeout) and checks
    both a first run (backs off, no fallback, nothing inserted) and a
    second, window-unchanged run (does not re-probe it)."""
    await seed_frame(rdb, tmp_path, boxes=[{"bbox": [10, 10, 90, 90],
                                            "species": "Seriola zonata"}])
    slow_sql = "SELECT pg_sleep(0.5), %s::text, %s::timestamptz, %s::timestamptz"
    monkeypatch.setattr(review_pool, "CHECK_PROBE_SQL", slow_sql)
    pool = _pool(rdb, review_assets, statement_timeout_ms=200)

    stats1 = await pool.run_once()
    assert stats1["errors"] == 0
    # 4 catalogue species are below target; each gets exactly one probe
    # (no fallback is attempted after a timeout) and every one times out.
    assert stats1["probes"] == 4 and stats1["timeouts"] == 4
    assert stats1["check_added"] == 0
    assert await _items(rdb, "check") == []
    backoff_at = pool._empty_until.get("Seriola zonata")
    assert backoff_at is not None
    assert 0 < backoff_at - time.time() <= TIMEOUT_BACKOFF_S < 86400

    # A second run, with no admin window change, must not re-probe a
    # species that's still inside its backoff window.
    stats2 = await pool.run_once()
    assert stats2["errors"] == 0
    assert stats2["probes"] == 0 and stats2["timeouts"] == 0


@pytestmark_db
async def test_the_empty_species_skip_survives_an_unchanged_window_and_clears_on_a_real_change(
        rdb, review_assets, tmp_path):
    """Fix round 2/3, New Breakage 1: comparing the effective window (whose
    end moves every run through the now()-based recent_exclusion_s clamp)
    instead of the admin-configured one wiped every skip on every run.
    Deleting the admin-window INSERT below must make this test fail: both
    the 'survives' and the 'clears' halves are asserted. Fix round 3 also
    checks each run's own stats, so a run that silently did nothing --
    rather than genuinely reaching the skip check -- can't pass either
    half vacuously."""
    await seed_frame(rdb, tmp_path, ts=T0, boxes=[{"bbox": [10, 10, 90, 90],
                                                    "species": "Seriola zonata"}])
    # check_open_per_species=1: run 1 finds and claims Seriola zonata's one
    # detection, satisfying its own need too, so an entirely unprobed run 2
    # (every species either skipped via _empty_until or already satisfied)
    # is unambiguous: probes == 0.
    pool = _pool(rdb, review_assets, check_open_per_species=1)
    stats1 = await pool.run_once()
    assert stats1["errors"] == 0 and stats1["timeouts"] == 0
    assert stats1["probes"] > 0 and stats1["check_added"] == 1
    assert pool._last_window is not None

    # Poke in a skip as if an earlier probe had found nothing (or timed
    # out) -- a key no real species can ever produce, so it can only
    # disappear via the window-comparison logic under test.
    pool._empty_until["sentinel"] = time.time() + 90000

    # No admin window is set before this run either: the skip must survive
    # it, and the run's own stats must show it actually reached the fill
    # logic and skipped every species, rather than bailing out early.
    stats2 = await pool.run_once()
    assert stats2["errors"] == 0 and stats2["timeouts"] == 0
    assert "note" not in stats2           # did not take the "no saved frames" exit
    assert stats2["probes"] == 0          # every species was skipped, none re-probed
    assert "sentinel" in pool._empty_until

    # Now the admin actually changes the window: the skip must clear, and
    # the next run must actually probe again.
    async with rdb.connection() as conn:
        await conn.execute(
            "INSERT INTO review_settings (key, value) VALUES ('window', %s)",
            (Jsonb({"start": T0.isoformat(), "end": (T0 + timedelta(days=1)).isoformat()}),))
    stats3 = await pool.run_once()
    assert stats3["errors"] == 0 and stats3["timeouts"] == 0
    assert stats3["probes"] > 0
    assert "sentinel" not in pool._empty_until


@pytestmark_db
async def test_the_probe_budget_is_checked_before_the_fallback_probe(rdb, review_assets,
                                                                      tmp_path):
    """Fix round 1, Finding 9: _probe_check must not spend a second 5 s
    probe once the run's budget is already gone."""
    await seed_frame(rdb, tmp_path, ts=T0, boxes=[])  # a valid window, no detections at all
    stats = await _pool(rdb, review_assets, max_probes=1).run_once()
    assert stats["errors"] == 0
    # Without the budget check before the fallback, the first (thinnest)
    # species alone would spend 2 probes (initial + fallback, both empty)
    # before the next species' loop-top check ever saw the budget was gone.
    assert stats["probes"] == 1


@pytestmark_db
async def test_the_identify_lane_checks_the_probe_budget_before_the_boxes_probe(
        rdb, review_assets, tmp_path):
    """Fix round 2, Finding 2/6 (still open after round 1): the frames
    probe and the boxes probe are two separate 5 s statements; the run's
    budget must be checked between them too, not only at the top of the
    while loop and only on the check lane's fallback."""
    await seed_frame(rdb, tmp_path, ts=T0, boxes=[{"bbox": [10, 10, 90, 90]}])  # unguessed
    stats = await _pool(rdb, review_assets, target=0, identify_open=5,
                        max_probes=1).run_once()
    assert stats["errors"] == 0
    # target=0 empties the check lane's work with zero probes. Without the
    # check before the boxes probe, the frames probe (which succeeds)
    # would be followed unconditionally by the boxes probe, making this 2.
    assert stats["probes"] == 1


@pytestmark_db
async def test_a_timed_out_non_probe_statement_ends_the_run_cleanly(
        rdb, review_assets, tmp_path, monkeypatch):
    """Fix round 2/3, controller finding 4: only _probe may turn a timeout
    into a usable result. Every other statement -- here, the small _free
    lookup that runs right after a successful probe -- must let
    QueryCanceled propagate to run_once, which ends the run with a
    warning, a timeouts count and a clear note, rather than completing as
    if nothing had gone wrong.

    _free timing out can never over-insert, here or under round 1's
    removed catch-and-convert behaviour: an empty free-set makes every
    row look claimed, not free, so 'no inserts' alone does not
    discriminate the fix (round 1's code inserted nothing here too). What
    does: the note, and the probes count. Three catalogue species have no
    detections at all (2 probes each: initial + fallback); Seriola zonata
    has this test's one real detection, whose probe succeeds (1 more) —
    then _free's own statement times out. probes == 7 pins the failure to
    _free specifically, ruling out an earlier statement (refresh_progress,
    the window read) having been the one that actually failed."""
    await seed_frame(rdb, tmp_path, ts=T0, boxes=[{"bbox": [100, 100, 300, 250],
                                                    "species": "Seriola zonata"}])
    slow_sql = "SELECT pg_sleep(0.5), %s::bigint[], %s::bigint[], %s::bigint[]"
    monkeypatch.setattr(review_pool, "_FREE_EVENTS_SQL", slow_sql)
    pool = _pool(rdb, review_assets, statement_timeout_ms=200)

    stats = await pool.run_once()
    assert stats["errors"] == 0
    assert stats["timeouts"] == 1
    assert stats["probes"] == 7           # one probe succeeded before _free failed
    assert "timed out" in (stats.get("note") or "").lower()
    assert stats["check_added"] == 0
    assert await _items(rdb, "check") == []


@pytestmark_db
async def test_the_baseline_cache_follows_the_catalogue_not_just_the_manifest_mtime(
        rdb, review_assets, tmp_path):
    """Fix round 1, Finding 2: a live catalogue rebuild (the sheet poller
    rewrites species_manifest.json / taxonomy.csv) can change which
    accepted name a baseline label resolves to without the baseline
    manifest file itself changing."""
    pool = _pool(rdb, review_assets)
    await pool.refresh_progress()
    assert pool.baseline("Pomatomus saltatrix") == 1

    path = review_assets["assets"] / "taxonomy.csv"
    rows = list(csv.DictReader(path.open(newline="")))
    for r in rows:
        if r["raw_name"] == "Pomatomus saltarix":
            r["accepted_name"] = "Pomatomus saltatrix2"
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=TAXONOMY_FIELDS)
        w.writeheader()
        w.writerows(rows)
    later = time.time_ns() + 10**9
    os.utime(path, ns=(later, later))

    await pool.refresh_progress()
    assert pool.baseline("Pomatomus saltatrix2") == 1
    assert pool.baseline("Pomatomus saltatrix") == 0


@pytestmark_db
async def test_the_identify_lane_gets_the_budget_first_every_other_run(rdb, review_assets,
                                                                      tmp_path):
    """Demo walk-through 2026-09-25: the check lane always ran first and could
    spend the whole probe budget, so "Identify new fish" stayed empty for as
    long as the check lane had species left to probe. The lanes now take
    turns going first."""
    await seed_frame(rdb, tmp_path, ts=T0, boxes=[{"bbox": [100, 100, 400, 300]}])  # unguessed
    pool = _pool(rdb, review_assets, identify_open=5, max_probes=2)
    first = await pool.run_once()
    second = await pool.run_once()
    assert first["errors"] == second["errors"] == 0
    assert first["identify_added"] == 0          # the check lane went first and spent it all
    assert second["identify_added"] == 1         # the identify lane's turn to go first
    assert len(await _items(rdb, "identify")) == 1

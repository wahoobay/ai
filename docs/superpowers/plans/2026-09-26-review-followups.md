# Review tool follow-up round — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the owner's 2026-09-26 decisions and the "fix soon after" items from the two final reviews:
- the dashboard's old correction form goes through the review rules;
- skips expire;
- server and page fixes;
- public outputs that are exact and private;
- Eastern time on the main dashboard.

**Architecture:**
- **Server:** changes stay inside the existing modules: `review_queue.py`, `review_frames.py`, `review_pool.py`, `review_catalogue.py`, `review_images.py`, `main.py` and `exports.py`.
- **Page:** changes stay inside the four review scripts and `app.js`, plus one new small script, `time-et.js`, holding the dashboard's Eastern formatter.
- **Page tests:** a new test helper, `tests/js_source.py`, lets the tests run the real page code under node wherever timing matters.
- **No schema change.**

**Tech Stack:** FastAPI + psycopg 3 (async) + PostgreSQL 18; classic browser scripts sharing a global `RV`; pytest with the `wahoobay_test` database; node for `--check` and the new script runs.

**Spec:** `docs/superpowers/specs/2026-09-26-review-followups-design.md`. It amends `docs/superpowers/specs/2026-09-24-reviewer-annotation-dataset-design.md` and `docs/superpowers/specs/2026-09-25-review-frame-first-design.md`, which stay binding except where it changes them.

## Global Constraints

- **Work only in the worktree** `/raid/scratch/dzimmerman2021/wahoobay/.claude/worktrees/review-tool`, branch `review-tool`.
  - Never touch the main checkout, the live `wahoobay` database, the running dashboard, the workers or the tunnel.
  - Never run the remap script against the live database; that is the owner's call after the controller shows them the dry run.
- **No new Python or JavaScript dependencies.**
- **Python style:**
  - `from __future__ import annotations` first in every new module.
  - ruff line-length 100, rules `E,F,W,I,B,UP`.
  - Run `/raid/scratch/dzimmerman2021/miniconda3/envs/wahoobay/bin/ruff check <files>` before every commit.
- **Run pytest as the direct child of `timeout`,** against the test database:
  `TEST_DATABASE_URL_FILE=.superpowers/test_dsn timeout 900 /raid/scratch/dzimmerman2021/miniconda3/envs/wahoobay/bin/python -m pytest <files> -q; echo exit=$?`
  - pyproject already adds `-q`, so there is no summary line. Evidence is the dots, any `F`/`E` characters, and `exit=`.
  - `wahoobay_test` is also the review demo's database (port 18090). The controller stops the demo before dispatching; if `ss -ltn | grep 18090` shows it listening, stop and report instead of running tests.
- **JavaScript:**
  - classic scripts sharing a global `RV` (the dashboard's `app.js` shares plain globals);
  - never `innerHTML` with data;
  - `/raid/scratch/dzimmerman2021/miniconda3/envs/node/bin/node --check <file>` must pass.
- **The busy-guard pattern.** Every JS function that sends a mutating request starts with `if (busy) return;`, sets `busy = true;` and resets `busy = false;` in a `finally` block.
- **Every mutating route** takes `Depends(require_csrf)` (header `X-Requested-With: wahoobay`) and a role dependency.
- **Transactions.** Pooled connections may arrive with `autocommit=True`. Any multi-statement write runs inside `async with conn.transaction():`.
- **Spot-check blindness.** A pass-2 payload never carries the model's guess, the hints, the species it counts toward, or the earlier verdict. Admins get no exception.
- **Settings and fixed values from the spec:**
  - `REVIEW_SKIP_TTL_DAYS`, default `30` (A2).
  - `RV.api` gives up after 20 s and returns `{ok: false, status: 0, data: {error: "the server took too long; try again"}}` (B3).
  - Eastern time is `America/New_York`, marked EDT or EST (F).
- **"Public" means anonymous or non-admin callers.** Admins still get identity columns and every row (D3, D5).
- **Git:**
  - Stage explicit paths only; never `git add -A`, never bare `git stash`.
  - Never stage `assets/celebrities.json` or `assets/species.xlsx`.
  - The harness refuses `$(...)` and multi-line `-m`. Write each commit message to `.superpowers/sdd/commit-msg.txt` and use `git commit -F .superpowers/sdd/commit-msg.txt`. The message is the quoted subject shown in the task's commit step, a blank line, then the trailer.
  - End each message with the `Co-Authored-By:` line your own environment specifies.

## Rulings made while planning

Each ruling is where the spec is silent, or where the code makes its literal wording insufficient. Each is stated as: what was decided — why — what it costs if wrong.

1. **The old form refuses a detection whose frame was never saved,** with 400 "this detection's frame was not saved".
   - Why: `submit_verdict` needs the frame to check a box, and phase 3 cannot crop without one.
   - Measured on 2026-09-26: 1,939 of the 20,000 most recent detections (10:25–12:00 EDT) had a saved frame, so the old form will refuse about 90% of what the public list shows.
   - Cost if wrong: the owner may prefer to hide the form's buttons on those events instead (an `app.js` change).
2. **The old form has no life-stage picker.**
   - When the catalogue records exactly one entry for the name, that entry's stage is used.
   - With two or more, the form gets 400 "choose a life stage for <name> on the review page".
   - Cost if wrong: an extra click to the review page for the few multi-stage species.
3. **The old form's "not a fish" drops any box sent with it,** because the review rules refuse a box on "no animal". Cost if wrong: none; the box meant nothing there.
4. **The old form's "uncertain" becomes `not_sure` on species and accept answers only.** The review rules refuse "not sure" on other outcomes, so those store `certain`. Cost if wrong: a lost "uncertain" on a not-a-fish answer.
5. **Reopen is also blind when the caller's own latest answer is their spot-check answer.**
   - Why: C2 stores that re-answer as a second opinion, so the page must not show the guess while taking it.
   - Cost if wrong: an admin sees less while changing their own spot-check answer.
6. **D3's filter also covers the public celebrity gallery, and D5's also covers `alerts.csv`.** They are the same leaks, with the same public/admin split. Cost if wrong: none.
7. **F also covers three chart spots** that use `getHours`/`getMonth`/`getDate`: the hour-of-day buckets and two axis-label formatters. They are viewer-zone times that the spec's count of 13 missed. Cost if wrong: none.
8. **C4 takes the verdict path's per-event advisory lock before the conditional insert.** `NOT EXISTS` alone still races a verdict that commits while the insert runs. Cost if wrong: one more lock per queued box, a few per pool run.
9. **B4 waits on `RV.nextTurn`, not only on a release already in flight.** A superseded `/next` may not have come back yet when the frame opens. Cost if wrong: a frame opens a moment later.
10. **A1b selects old-form rows with `label_source IS NULL AND corrected_name IS NOT NULL`,** not by `user_id`. Every review-tool species verdict records its label source. Cost if wrong: none.

## Review Focus

1. **The old form on a detection whose frame was never saved,** about 90% of live detections (ruling 1). Expected: a readable 400, no row written, and the dashboard shows the message. Covered by Task 3's `test_a_detection_without_a_saved_frame_is_refused_readably`.
2. **A double click on an old-form button,** i.e. two identical POSTs within a second. Expected: both 200, one row. Covered by Task 3's `test_a_double_click_saves_one_row`.
3. **A skip at the TTL edge, and re-skipping an expired skip.** Expected: an expired skip no longer hides the box, and skipping again restarts the clock. Covered by Task 1's skip tests.
4. **An admin changing their own spot-check answer twice within 10 s.** Expected: one new row, linked to their pass-2 item. Covered by Task 2's `test_the_re_answer_is_deduplicated`.
5. **A `/next` that never answers, followed by a lane click.** Expected: the stalled call times out, and the next `RV.next()` still runs. Covered by Task 6's `test_a_request_that_never_answers_times_out_and_frees_the_queue`.

---

### Task 1: Queue rules — skips expire, retired boxes say why, the pool never queues an answered box

**Files:**
- Modify: `services/dashboard/app/review_queue.py`: new constants and `not_skipped`; `_PASS1_SQL`, `_SPOT_ELIGIBLE`, `next_item`, `submit_verdict` (lock and done/retired check), `skip_item`.
- Modify: `services/dashboard/app/review_frames.py`: `open_frame`, `skip_frame`, import block.
- Modify: `services/dashboard/app/review_pool.py`: `_INSERT_ITEM_SQL`, `_rows`, `_insert`, import block.
- Modify: `services/dashboard/app/routes_review.py`: `next_box`, `frame_open`.
- Modify: `services/dashboard/app/main.py`: `DashboardCfg`, next to `review_claim_s`.
- Modify: `.env.example`, after `REVIEW_CLAIM_S=600`.
- Modify: `docs/operations.md`, "Review tool (/review)" section.
- Modify: `tests/review_fixtures.py`: new `wait_until_blocked`.
- Test: `tests/test_review_queue.py`, `tests/test_review_frames.py`, `tests/test_review_pool.py`.

**Interfaces:**
- Produces, in `review_queue`:
  - `SKIP_TTL_DAYS: int = 30`;
  - `not_skipped(alias: str) -> str`, an SQL condition using `%(uid)s` and `%(skip_days)s`;
  - `EVENT_LOCK_SQL: str`, one parameter, the event id;
  - `RETIRED_MESSAGES: dict[str, str]`.
- Produces two new parameters:
  - `next_item(..., skip_ttl_days: int = SKIP_TTL_DAYS)`;
  - `review_frames.open_frame(..., skip_ttl_days: int = SKIP_TTL_DAYS)`.
- Produces `DashboardCfg.review_skip_ttl_days: int`.
- Produces `review_fixtures.wait_until_blocked(db, *, timeout_s: float = 5.0) -> None`.
- `ReviewPool._rows` gains a keyword `lock_event: int | None = None`.

- [ ] **Step 1: Add the lock-wait helper to `tests/review_fixtures.py`** (below `add_verdict`), and add `import asyncio` and `import time` to its imports:

```python
async def wait_until_blocked(db, *, timeout_s: float = 5.0) -> None:
    """Return once some session in this database is waiting on a lock, read
    from pg_blocking_pids -- instead of sleeping and hoping the other task
    got there. Fails the test after timeout_s."""
    deadline = time.monotonic() + timeout_s
    async with db.connection() as conn:
        while True:
            cur = await conn.execute(
                """SELECT count(*) FROM pg_stat_activity
                    WHERE datname = current_database()
                      AND cardinality(pg_blocking_pids(pid)) > 0""")
            if (await cur.fetchone())[0]:
                return
            if time.monotonic() > deadline:
                raise AssertionError("no session became blocked")
            await asyncio.sleep(0.02)
```

- [ ] **Step 2: Switch the existing concurrency test to it.**
  - In `tests/test_review_queue.py`, `test_the_held_refresh_does_not_resurrect_a_claim_that_moved_on_under_it`, replace
    `await asyncio.sleep(0.3)   # let A's held-item UPDATE reach and block on our lock`
    with
    `await wait_until_blocked(rdb)   # A's held-item UPDATE is now waiting on our lock`.
  - Add `wait_until_blocked` to that module's `review_fixtures` import.

- [ ] **Step 3: Write the failing tests.**

In `tests/test_review_queue.py`:
- add to the `app.review_queue` import: `EVENT_LOCK_SQL`, `SKIP_TTL_DAYS`;
- add these tests after `test_a_skipped_box_is_not_offered_to_you_again`.

```python
async def _age_skips(rdb, days):
    async with rdb.connection() as conn:
        await conn.execute(
            "UPDATE review_skips SET created_at = now() - make_interval(days => %s)", (days,))


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
        got = await next_item(conn, user_id=me, lane="check", species_order=[],
                              spot_serve_rate=0.0, claim_s=600, rng=random.Random(1),
                              skip_ttl_days=2)
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
    f = await seed_frame(rdb, tmp_path, boxes=[{"bbox": [100, 100, 300, 250],
                                                "species": "Seriola zonata"}])
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
    for reason, words in (("frame_missing", "its frame is missing"),
                          ("outside_window", "outside the review window")):
        async with rdb.connection() as conn:
            await conn.execute(
                """UPDATE review_items SET status = 'retired', retired_reason = %s,
                          claimed_by = %s WHERE id = %s""", (reason, me, item))
        with pytest.raises(Conflict) as e:
            await _verdict(rdb, cat, as_user(me), event_id=f["event_ids"][0],
                           review_item_id=item, outcome="unidentifiable")
        assert words in e.value.message and "already answered" not in e.value.message
```

In `tests/test_review_frames.py`, add after `test_open_frame_does_not_reclaim_a_box_the_volunteer_skipped`. Reuse that module's own seeding and import style; the calls and assertions below are the requirement.

```python
async def test_open_frame_reclaims_a_box_whose_skip_has_expired(rdb, tmp_path, cat):
    f = await seed_frame(rdb, tmp_path, boxes=[{"bbox": [10, 10, 90, 90],
                                                "species": "Seriola zonata"}])
    item = await add_item(rdb, f, predicted="Seriola zonata")
    me = await make_user(rdb, "me")
    async with rdb.connection() as conn:
        await skip_frame(conn, saved_frame_id=f["saved_frame_id"], user_id=me)
        await open_frame(conn, saved_frame_id=f["saved_frame_id"], user_id=me, claim_s=600)
        cur = await conn.execute("SELECT status FROM review_items WHERE id = %s", (item,))
        assert (await cur.fetchone())[0] == "open"
        await conn.execute("UPDATE review_skips SET created_at = now() - interval '31 days'")
        await open_frame(conn, saved_frame_id=f["saved_frame_id"], user_id=me, claim_s=600)
        cur = await conn.execute("SELECT status, claimed_by FROM review_items WHERE id = %s",
                                 (item,))
        assert await cur.fetchone() == ("claimed", me)


async def test_next_frame_again_restarts_the_skip_clock(rdb, tmp_path, cat):
    f = await seed_frame(rdb, tmp_path, boxes=[{"bbox": [10, 10, 90, 90],
                                                "species": "Seriola zonata"}])
    item = await add_item(rdb, f, predicted="Seriola zonata")
    me = await make_user(rdb, "me")
    async with rdb.connection() as conn:
        await skip_frame(conn, saved_frame_id=f["saved_frame_id"], user_id=me)
        await conn.execute("UPDATE review_skips SET created_at = now() - interval '40 days'")
        await skip_frame(conn, saved_frame_id=f["saved_frame_id"], user_id=me)
        cur = await conn.execute(
            "SELECT now() - created_at < interval '1 minute' FROM review_skips WHERE item_id = %s",
            (item,))
        assert (await cur.fetchone())[0] is True
```

In `tests/test_review_pool.py`:
- add `import asyncio`;
- add `from app.review_queue import EVENT_LOCK_SQL`;
- add `add_verdict, make_user, seed_frame, wait_until_blocked` to the `review_fixtures` import (keep the existing names).

```python
async def _insert_args(rdb, tmp_path):
    f = await seed_frame(rdb, tmp_path, boxes=[{"bbox": [10, 10, 90, 90],
                                                "species": "Seriola zonata"}])
    return f, {"event_id": f["event_ids"][0], "lane": "check",
               "saved_frame_id": f["saved_frame_id"], "source": f["source"], "ts": f["ts"],
               "bbox": [10, 10, 90, 90], "predicted": "Seriola zonata", "predicted_p": 0.6,
               "route": "Seriola zonata"}


@pytestmark_db
async def test_the_pool_never_queues_a_box_answered_after_its_free_check(rdb, review_assets,
                                                                         tmp_path):
    pool = _pool(rdb, review_assets)
    f, args = await _insert_args(rdb, tmp_path)
    await add_verdict(rdb, f["event_ids"][0], await make_user(rdb, "one"), name="Seriola zonata")
    async with rdb.connection() as conn:
        assert await pool._insert(conn, **args) is False
    assert not await _items(rdb)


@pytestmark_db
async def test_the_pool_waits_for_a_verdict_in_flight_on_the_same_box(rdb, review_assets,
                                                                      tmp_path):
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
                   VALUES (%s, 'Seriola zonata', %s)""", (args["event_id"], uid))
            task = asyncio.create_task(insert())
            await wait_until_blocked(rdb)
        # the verdict commits here
    assert await task is False
    assert not await _items(rdb)
```

(`pytestmark_db` is that module's own `pytest.mark.anyio` marker.)

- [ ] **Step 4: Run them to see them fail.**
  - Run: `… -m pytest tests/test_review_queue.py tests/test_review_frames.py tests/test_review_pool.py -q -k "skip or retired or answered_after or in_flight or resurrect"`
  - Expected: ImportError for `SKIP_TTL_DAYS`/`EVENT_LOCK_SQL`, then FAILs.

- [ ] **Step 5: Implement `review_queue.py`.** Below `DUPLICATE_WINDOW_S`:

```python
# Follow-up spec A2: a skip hides a box from that volunteer for this many
# days (REVIEW_SKIP_TTL_DAYS), then it can be served to them again.
SKIP_TTL_DAYS = 30

# Every verdict on an event takes this lock first, and so does the pool
# before it queues the event (follow-up C4). Namespaced and
# transaction-scoped; hashtextextended, not the two-int4 form, because
# event ids will pass 2^31 within a year.
EVENT_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtextextended('review-event:' || %s::text, 0))"

# Follow-up C3: a verdict on a retired item says why it was retired.
RETIRED_MESSAGES = {
    "frame_missing": "this box was retired: its frame is missing on disk",
    "outside_window": "this box was retired: it is outside the review window",
}


def not_skipped(alias: str) -> str:
    """SQL: volunteer %(uid)s has not skipped item `alias` within the last
    %(skip_days)s days (follow-up A2). An older skip no longer counts."""
    return f"""NOT EXISTS (SELECT 1 FROM review_skips s
                        WHERE s.item_id = {alias}.id AND s.user_id = %(uid)s
                          AND s.created_at > now() - make_interval(days => %(skip_days)s))"""
```

`not_skipped` must be defined above `_PASS1_SQL`, since the f-strings call it at import time.

Then make these edits:
- **`_PASS1_SQL`:** replace its `AND NOT EXISTS (SELECT 1 FROM review_skips s … )` clause with `AND {not_skipped("ri")}`.
- **`_SPOT_ELIGIBLE`:** make it an f-string and replace its review_skips clause the same way.
- **`next_item`:**
  - add the parameter `skip_ttl_days: int = SKIP_TTL_DAYS` after `rng`;
  - add `"skip_days": skip_ttl_days` to `params`.
- **`submit_verdict`:**
  - the lock becomes `await cur.execute(EVENT_LOCK_SQL, (body.event_id,))`;
  - keep the comment above it, but move its hashtextextended sentence to the constant;
  - replace `if item["status"] in ("done", "retired"): raise Conflict("this box was already answered")` with:

```python
            if item["status"] == "retired":
                raise Conflict(RETIRED_MESSAGES.get(item["retired_reason"],
                                                    "this box was retired from the queue"))
            if item["status"] == "done":
                raise Conflict("this box was already answered")
```

- **`skip_item`:** the insert becomes

```python
            # Skipping again restarts the clock (follow-up A2).
            await cur.execute("""INSERT INTO review_skips (item_id, user_id) VALUES (%s, %s)
                                 ON CONFLICT (item_id, user_id)
                                 DO UPDATE SET created_at = now()""", (item_id, user_id))
```

- [ ] **Step 6: Implement `review_frames.py`.**
  - Add `SKIP_TTL_DAYS` and `not_skipped` to the `.review_queue` import.
  - Replace `open_frame` with:

```python
async def open_frame(conn, *, saved_frame_id: int, user_id: int, claim_s: int,
                     skip_ttl_days: int = SKIP_TTL_DAYS) -> None:
    """Claim the frame's open (or lapsed) first-pass boxes for this
    volunteer, and refresh the claims they already hold here -- including
    the box the queue just served them, which the page no longer releases
    before opening its frame (spec amendment 2026-09-25). Boxes the
    volunteer skipped in the last skip_ttl_days stay unclaimed (follow-up
    A2); someone else's live claim is left alone."""
    async with conn.transaction(), conn.cursor() as cur:
        await refuse_during_second_opinion(conn, saved_frame_id=saved_frame_id, user_id=user_id)
        await cur.execute(f"""
            UPDATE review_items
               SET status = 'claimed', claimed_by = %(uid)s,
                   claimed_until = now() + make_interval(secs => %(claim_s)s)
             WHERE saved_frame_id = %(sf)s AND pass = 1
               AND (status = 'open'
                    OR (status = 'claimed' AND claimed_until < now())
                    OR (status = 'claimed' AND claimed_by = %(uid)s))
               AND {not_skipped("review_items")}""",
                          {"uid": user_id, "claim_s": claim_s, "sf": saved_frame_id,
                           "skip_days": skip_ttl_days})
```

  - In `skip_frame`, change the insert's `ON CONFLICT DO NOTHING` to `ON CONFLICT (item_id, user_id) DO UPDATE SET created_at = now()`.
  - Add to its docstring: "Skipping again restarts each box's clock (follow-up A2)."

- [ ] **Step 7: Implement `review_pool.py`.**
  - Add `from .review_queue import EVENT_LOCK_SQL`. `review_queue` never imports `review_pool`, so there is no cycle.
  - Replace `_INSERT_ITEM_SQL` with:

```python
# Conditional on the box having no verdict (follow-up C4, final review Minor
# 10). _insert takes the event's verdict lock first, so this statement sees
# any verdict already committed, and a verdict still to come waits for it and
# then closes the new item itself. The casts are needed because INSERT ...
# SELECT, unlike VALUES, does not type bare parameters from the target columns.
_INSERT_ITEM_SQL = """
    INSERT INTO review_items (event_id, pass, lane, saved_frame_id, source_name, ts, bbox,
                              predicted, predicted_p, route_species)
    SELECT %s::bigint, 1, %s::text, %s::bigint, %s::text, %s::timestamptz, %s::integer[],
           %s::text, %s::real, %s::text
     WHERE NOT EXISTS (SELECT 1 FROM detection_corrections WHERE event_id = %s::bigint)
    ON CONFLICT (event_id, pass) DO NOTHING
    RETURNING id
"""
```

  - `_rows` gets a keyword-only `lock_event: int | None = None`. Inside its transaction, after the `set_config` line:

```python
            if lock_event is not None:
                await conn.execute(EVENT_LOCK_SQL, (lock_event,))
```

  - `_insert` passes the event id twice and takes the lock:

```python
        return bool(await self._rows(conn, _INSERT_ITEM_SQL, (
            event_id, lane, saved_frame_id, source, ts, list(bbox), predicted, predicted_p,
            route, event_id), lock_event=event_id))
```

- [ ] **Step 8: Wire the setting.**
  - `main.py` `DashboardCfg`, after `review_claim_s`: `self.review_skip_ttl_days = int(_env("REVIEW_SKIP_TTL_DAYS", "30"))`.
  - `routes_review.py`: pass `skip_ttl_days=cfg.review_skip_ttl_days` in `next_box`'s `next_item(...)` call and in `frame_open`'s `open_frame(...)` call.
  - `.env.example`, after `REVIEW_CLAIM_S=600`:

```
# Days a skipped box stays hidden from the volunteer who skipped it.
REVIEW_SKIP_TTL_DAYS=30
```

  - `docs/operations.md`: add one paragraph in "Review tool (/review)", after the paragraph describing S / "Next frame":

```
**Skips expire.** A box a volunteer skipped (S, or "Next frame") is hidden from them for
`REVIEW_SKIP_TTL_DAYS` days (default 30), then it can be served to them again; skipping it
again restarts the clock.
```

- [ ] **Step 9: Run the tests.**
  - Run: `… -m pytest tests/test_review_queue.py tests/test_review_frames.py tests/test_review_pool.py tests/test_review_docs.py tests/test_review_routes.py -q; echo exit=$?`
  - Expected: all dots, `exit=0`.

- [ ] **Step 10: Commit** — "review queue: skips expire after 30 days, retired boxes say why, the pool never queues an answered box"

```bash
git add services/dashboard/app/review_queue.py services/dashboard/app/review_frames.py services/dashboard/app/review_pool.py services/dashboard/app/routes_review.py services/dashboard/app/main.py .env.example docs/operations.md tests/review_fixtures.py tests/test_review_queue.py tests/test_review_frames.py tests/test_review_pool.py
git commit -F .superpowers/sdd/commit-msg.txt
```

---

### Task 2: Spot-check blindness on reopen, and an admin's re-answer stays a second opinion

**Files:**
- Modify: `services/dashboard/app/review_queue.py`: `_duplicate_verdict`, `submit_verdict` (no-item branch and dedup call), `reopen_payload`.
- Test: `tests/test_review_queue.py`.

**Interfaces:**
- Consumes: Task 1's `EVENT_LOCK_SQL` (already in `submit_verdict`).
- Produces:
  - `reopen_payload`'s `payload["mine"]` gains `"bbox": list[int] | None`, which Task 7's page reads;
  - `_duplicate_verdict(..., first_pass: bool = True)`.

- [ ] **Step 1: Write the failing tests** in `tests/test_review_queue.py`, after `test_reopen_marks_the_box_read_only_once_a_resubmit_would_be_refused`:

```python
async def _reopen(rdb, cat, user, event_id):
    async with rdb.connection() as conn:
        return await reopen_payload(conn, user=user, event_id=event_id, catalogue=cat,
                                    counts=lambda _name: 0, target=5)


async def _spot_checked_box(rdb, tmp_path, *, status="open"):
    """A box volunteer "one" answered first, with a spot-check on it."""
    f = await seed_frame(rdb, tmp_path, boxes=[{"bbox": [100, 100, 300, 250],
                                                "species": "Seriola zonata"}])
    one = await make_user(rdb, "one")
    await add_item(rdb, f, predicted="Seriola zonata", route="Seriola zonata", hints=HINTS,
                   status="done")
    first = await add_verdict(rdb, f["event_ids"][0], one, name="Seriola zonata")
    spot = await add_item(rdb, f, pass_=2, status=status, exclude_user_id=one,
                          first_correction_id=first)
    return f, one, spot


def _is_blind(payload):
    return (payload["model"] is None and payload["hints"] is None
            and payload["counts_toward"] is None and payload["item"]["pass"] == 2
            and payload["item"]["lane"] is None)


@pytest.mark.parametrize("status", ["open", "claimed"])
async def test_an_admin_reopening_a_pending_spot_check_sees_the_picture_alone(rdb, tmp_path,
                                                                             cat, status):
    f, _one, _spot = await _spot_checked_box(rdb, tmp_path, status=status)
    boss = await make_user(rdb, "boss", role="admin")
    payload = await _reopen(rdb, cat, as_user(boss, role="admin"), f["event_ids"][0])
    assert _is_blind(payload) and "mine" not in payload


async def test_once_the_spot_check_is_answered_an_admin_reopen_shows_the_guess(rdb, tmp_path,
                                                                              cat):
    f, _one, _spot = await _spot_checked_box(rdb, tmp_path, status="done")
    boss = await make_user(rdb, "boss", role="admin")
    payload = await _reopen(rdb, cat, as_user(boss, role="admin"), f["event_ids"][0])
    assert payload["model"]["name"] == "Seriola zonata" and payload["item"]["pass"] == 1


async def test_an_admin_reopening_their_own_spot_check_answer_sees_it_blind(rdb, tmp_path, cat):
    f, _one, spot = await _spot_checked_box(rdb, tmp_path, status="done")
    boss = await make_user(rdb, "boss", role="admin")
    await add_verdict(rdb, f["event_ids"][0], boss, unidentifiable=True, item_id=spot,
                      bbox=[110, 110, 290, 240])
    payload = await _reopen(rdb, cat, as_user(boss, role="admin"), f["event_ids"][0])
    assert _is_blind(payload)
    assert payload["mine"]["outcome"] == "unidentifiable"
    assert payload["mine"]["bbox"] == [110, 110, 290, 240]


async def test_an_admins_re_answer_to_their_own_spot_check_stays_a_second_opinion(rdb, tmp_path,
                                                                                 cat):
    f, _one, spot = await _spot_checked_box(rdb, tmp_path, status="done")
    boss = await make_user(rdb, "boss", role="admin")
    await add_verdict(rdb, f["event_ids"][0], boss, unidentifiable=True, item_id=spot)
    admin = as_user(boss, role="admin")
    out = await _verdict(rdb, cat, admin, spot=1.0, event_id=f["event_ids"][0],
                         outcome="species", label="Seriola zonata", label_source="search")
    assert out["first_pass"] is False and out["spot_check_scheduled"] is False
    row = await _row(rdb, "SELECT review_item_id FROM detection_corrections WHERE id = %s",
                     out["id"])
    assert row[0] == spot
    count = await _row(rdb, """SELECT count(*) FROM review_items
                                WHERE event_id = %s AND pass = 2""", f["event_ids"][0])
    assert count[0] == 1
    with pytest.raises(Invalid):   # a second opinion has no guess to accept
        await _verdict(rdb, cat, admin, event_id=f["event_ids"][0], outcome="accept")


async def test_the_re_answer_is_deduplicated(rdb, tmp_path, cat):
    f, _one, spot = await _spot_checked_box(rdb, tmp_path, status="done")
    boss = await make_user(rdb, "boss", role="admin")
    await add_verdict(rdb, f["event_ids"][0], boss, unidentifiable=True, item_id=spot)
    admin = as_user(boss, role="admin")
    body = {"event_id": f["event_ids"][0], "outcome": "species", "label": "Seriola zonata",
            "label_source": "search"}
    first = await _verdict(rdb, cat, admin, **body)
    again = await _verdict(rdb, cat, admin, **body)
    assert again["duplicate"] is True and again["id"] == first["id"]
    assert again["first_pass"] is False
```

- [ ] **Step 2: Run to see them fail.**
  - Run: `… -m pytest tests/test_review_queue.py -q -k "spot_check or re_answer"`
  - Expected: FAILs (not blind, no `bbox` in `mine`, `review_item_id` None).

- [ ] **Step 3: Implement.**

`_duplicate_verdict`:
- gains a keyword `first_pass: bool = True`;
- returns `"first_pass": first_pass` instead of the literal `True`;
- add this sentence to its docstring: "first_pass is what the caller's verdict would have been: False for a re-answer linked to the caller's own spot-check."

`submit_verdict`, in the `elif not adjudication:` branch, after its two guards:

```python
            # Follow-up C2 (phase-1 Minor 4): re-answering a box you answered
            # as a spot-check -- only admins get here, by reopening it --
            # stays a second opinion. Linked to your own pass-2 item, so the
            # resolved pair is still the first reviewer against the second,
            # the answer is blind, and nothing new is scheduled.
            item = await fetch_one(conn, """
                SELECT ri.* FROM detection_corrections c
                  JOIN review_items ri ON ri.id = c.review_item_id
                 WHERE c.event_id = %s AND c.user_id = %s AND ri.pass = 2
                 ORDER BY c.created_at DESC, c.id DESC LIMIT 1""", (body.event_id, user.id))
```

Then change the duplicate check's condition and call:

```python
        if body.review_item_id is None and not adjudication:
            dup = await _duplicate_verdict(conn, event_id=body.event_id, user_id=user.id, v=v,
                                           outcome=body.outcome, first_pass=item is None)
```

The rest already follows from `item` being the pass-2 row:
- `blind` is true;
- the hint lookup is skipped;
- the insert stores `item["id"]`;
- the closing UPDATE matches no open row;
- `first_pass` is False, so no spot-check is scheduled.

`reopen_payload`: the `mine` query reads the pass of the item it answered:

```python
    mine = await fetch_one(conn, """
        SELECT c.corrected_name, c.not_a_fish, c.unidentifiable, c.life_stage,
               c.corrected_bbox, c.confidence, c.celebrity_id, ri.pass AS item_pass
          FROM detection_corrections c
          LEFT JOIN review_items ri ON ri.id = c.review_item_id
         WHERE c.event_id = %s AND c.user_id = %s
         ORDER BY c.created_at DESC, c.id DESC LIMIT 1""", (event_id, user.id))
```

and, after `base` is built, `pseudo` becomes:

```python
    # Follow-up C1 (phase-1 triage 155) and C2: the picture alone -- no
    # guess, hints or target -- while a spot-check on the box is waiting or
    # being answered and the caller has no answer on it (an admin typing the
    # URL), and when the caller's own latest answer is their spot-check
    # answer, whose re-answer stays a second opinion.
    pending_spot = await fetch_one(conn, """
        SELECT 1 FROM review_items
         WHERE event_id = %s AND pass = 2 AND status IN ('open', 'claimed')""", (event_id,))
    blind = ((mine is None and pending_spot is not None)
             or (mine is not None and mine["item_pass"] == 2))
    pseudo = {**base, "id": None, "pass": 2 if blind else 1, "event_id": event_id}
```

`payload["mine"]` gains the box:

```python
                           "celebrity_id": mine["celebrity_id"],
                           "bbox": list(mine["corrected_bbox"]) if mine["corrected_bbox"] else None}
```

- [ ] **Step 4: Run the tests.**
  - Run: `… -m pytest tests/test_review_queue.py tests/test_review_routes.py tests/test_review_frames.py -q; echo exit=$?`
  - Expected: all dots, `exit=0`.

- [ ] **Step 5: Commit** — "review: a pending spot-check stays blind on reopen, and an admin's re-answer stays a second opinion"

```bash
git add services/dashboard/app/review_queue.py tests/test_review_queue.py
git commit -F .superpowers/sdd/commit-msg.txt
```

---

### Task 3: The old correction form saves through the review rules (A1, A1b)

**Files:**
- Modify: `services/dashboard/app/review_catalogue.py`: new `Catalogue.stages`.
- Modify: `services/dashboard/app/review_queue.py`: new `legacy_verdict`.
- Modify: `services/dashboard/app/main.py`: imports; `post_correction`.
- Modify: `services/dashboard/app/static/app.js`: `postCorrection`.
- Create: `scripts/dev/remap_legacy_corrections.py`.
- Create: `tests/js_source.py`, for reading and running page scripts in tests; Tasks 4, 6, 7 and 8 use it.
- Modify: `docs/operations.md`, the "Review tool (/review)" section.
- Modify: `tests/test_auth_db.py`, whose `pool` fixture and `test_approved_reviewer_can_submit_a_correction` need a saved frame now.
- Test: create `tests/test_legacy_corrections.py`, `tests/test_remap_legacy_corrections.py` and `tests/test_dashboard_page.py`; modify `tests/test_review_docs.py`.

**Interfaces:**
- Consumes: `submit_verdict` and `VerdictIn` (Task 2's no-item path).
- Produces:
  - `Catalogue.stages(name: str) -> list[str | None]`;
  - `review_queue.legacy_verdict(body, *, catalogue) -> VerdictIn`, raising `Invalid`;
  - in `tests/js_source.py`: `STATIC`, `NODE`, `needs_node`, `read(name) -> str`, `braces_from(text, open_index) -> str`, `function_body(text, name) -> str`, `function_source(text, name) -> str`, `run_node(sources: list[str], body: str, tmp_path) -> object`;
  - in the script: `remap(conn, catalogue, *, apply: bool) -> Report`, with `Report.changes: list[tuple[int, str, str]]`, `Report.unmapped: list[tuple[int, str]]` and `Report.unchanged: int`.

- [ ] **Step 1: Create `tests/js_source.py`:**

```python
"""Reading and running the dashboard's browser scripts in tests.

There is no browser harness in this repo. Most page checks are textual:
function_body() finds one function's source. Where timing matters,
run_node() runs the real script under node with a minimal DOM stub."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "services" / "dashboard" / "app" / "static"
NODE = shutil.which("node") or "/raid/scratch/dzimmerman2021/miniconda3/envs/node/bin/node"
needs_node = pytest.mark.skipif(not Path(NODE).exists(), reason="node is not available")


def read(name: str) -> str:
    return (STATIC / name).read_text()


def braces_from(text: str, open_index: int) -> str:
    """The `{...}` block starting at `open_index` (which must point at an
    opening brace), found by counting braces. Good enough for the functions
    these tests inspect: none hides a brace inside a string or template
    literal."""
    assert text[open_index] == "{", "open_index must point at '{'"
    depth = 0
    for j in range(open_index, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return text[open_index:j + 1]
    raise AssertionError("unbalanced braces")


def _declaration(text: str, name: str) -> re.Match:
    m = re.search(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{", text)
    assert m, f"no function named {name!r} found"
    return m


def function_body(text: str, name: str) -> str:
    m = _declaration(text, name)
    return braces_from(text, m.end() - 1)


def function_source(text: str, name: str) -> str:
    """The whole declaration, `function name(...) { ... }`, ready to run."""
    m = _declaration(text, name)
    return text[m.start():m.end() - 1] + braces_from(text, m.end() - 1)


DOM_STUB = r"""
const __els = {};
function __el(tag) {
  return { tag, hidden: true, textContent: "", className: "", dataset: {}, children: [],
    classList: { add() {}, remove() {}, toggle() {} }, setAttribute() {}, addEventListener() {},
    append(...c) { this.children.push(...c); }, replaceChildren(...c) { this.children = c; } };
}
globalThis.document = { body: { dataset: {} }, getElementById: (id) => (__els[id] ||= __el("div")),
  createElement: (tag) => __el(tag), addEventListener() {}, querySelectorAll: () => [] };
globalThis.window = { addEventListener() {}, location: { href: "" } };
"""


def run_node(sources: list[str], body: str, tmp_path: Path):
    """Run browser-script sources under node after DOM_STUB, then `body` as
    the body of an async function. Returns its return value, via JSON. Exits
    as soon as the value is printed, so page timers (toasts) don't hold the
    run open."""
    script = tmp_path / "page-test.js"
    script.write_text(DOM_STUB + "\n".join(sources) + "\n(async () => {\n" + body
                      + "\n})().then(\n  (v) => { console.log(JSON.stringify(v)); process.exit(0); },"
                      "\n  (e) => { console.error(e); process.exit(1); });\n")
    r = subprocess.run([NODE, str(script)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])
```

- [ ] **Step 2: Write the failing tests.**

Create `tests/test_legacy_corrections.py`:

```python
"""The dashboard's older correction form, saved through the review rules
(follow-up spec A1)."""
from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from app.review_catalogue import CatalogueStore
from app.review_queue import Invalid, legacy_verdict
from review_fixtures import (
    CSRF,
    TEST_DSN,
    add_item,
    add_verdict,
    catalogue_paths,
    login,
    make_user,
    seed_frame,
)

pytestmark = pytest.mark.anyio

BOX = [{"bbox": [100, 100, 300, 250], "species": "Seriola zonata"}]


def _form(event_id, **kw):
    body = {"event_id": event_id, "corrected_name": None, "corrected_species_id": None,
            "not_a_fish": False, "is_endorsement": False, "corrected_bbox": None,
            "confidence": "probable", "notes": None, "celebrity_id": None}
    body.update(kw)
    return body


async def _rows(rdb, event_id):
    async with rdb.connection() as conn:
        cur = await conn.execute(
            """SELECT corrected_name, not_a_fish, is_endorsement, corrected_bbox, confidence,
                      label_source, user_id, review_item_id
                 FROM detection_corrections WHERE event_id = %s ORDER BY id""", (event_id,))
        return await cur.fetchall()


# ---- the adapter ------------------------------------------------------------

@pytest.fixture
def cat(review_assets):
    return CatalogueStore(catalogue_paths(review_assets)).get()


def _ns(**kw):
    return SimpleNamespace(**_form(1, **kw))


def test_an_endorsement_is_an_accept(cat):
    v = legacy_verdict(_ns(corrected_name="Seriola zonata", is_endorsement=True,
                           confidence="certain"), catalogue=cat)
    assert (v.outcome, v.label, v.label_source, v.not_sure) == ("accept", None, None, False)


def test_a_correction_is_a_search_pick_under_its_accepted_name(cat):
    v = legacy_verdict(_ns(corrected_name="Pomatomus saltarix", confidence="uncertain"),
                       catalogue=cat)
    assert (v.outcome, v.label, v.label_source, v.not_sure) == (
        "species", "Pomatomus saltatrix", "search", True)


def test_not_a_fish_drops_the_box_and_the_doubt(cat):
    v = legacy_verdict(_ns(not_a_fish=True, corrected_bbox=[1, 2, 30, 40],
                           confidence="uncertain"), catalogue=cat)
    assert (v.outcome, v.bbox, v.not_sure) == ("no_animal", None, False)


def test_a_name_outside_the_species_list_is_refused(cat):
    with pytest.raises(Invalid, match="not in the species list"):
        legacy_verdict(_ns(corrected_name="Mystery fish"), catalogue=cat)


def test_a_species_with_several_life_stages_is_sent_to_the_review_page(cat):
    with pytest.raises(Invalid, match="choose a life stage for Pomacanthus paru on the review page"):
        legacy_verdict(_ns(corrected_name="Pomacanthus paru"), catalogue=cat)


# ---- over HTTP ----------------------------------------------------------------

async def test_an_endorsement_is_saved_as_a_review_verdict_and_closes_the_queue_item(
        rdb, rclient, tmp_path):
    f = await seed_frame(rdb, tmp_path, boxes=BOX)
    item = await add_item(rdb, f, predicted="Seriola zonata")
    await make_user(rdb, "alice")
    login(rclient, "alice")
    r = rclient.post("/api/corrections", headers=CSRF, json=_form(
        f["event_ids"][0], corrected_name="Seriola zonata", corrected_species_id="Seriola zonata",
        is_endorsement=True, confidence="certain"))
    assert r.status_code == 200, r.text
    assert r.json()["is_endorsement"] is True
    [row] = await _rows(rdb, f["event_ids"][0])
    assert row[:3] == ("Seriola zonata", False, True) and row[5] == "model_guess"
    async with rdb.connection() as conn:
        cur = await conn.execute("SELECT status FROM review_items WHERE id = %s", (item,))
        assert (await cur.fetchone())[0] == "done"


async def test_a_misspelled_name_is_stored_under_its_accepted_name(rdb, rclient, tmp_path):
    f = await seed_frame(rdb, tmp_path, boxes=BOX)
    await make_user(rdb, "alice")
    login(rclient, "alice")
    r = rclient.post("/api/corrections", headers=CSRF, json=_form(
        f["event_ids"][0], corrected_name="Pomatomus saltarix", confidence="uncertain"))
    assert r.status_code == 200, r.text
    [row] = await _rows(rdb, f["event_ids"][0])
    assert (row[0], row[4], row[5]) == ("Pomatomus saltatrix", "uncertain", "search")


async def test_the_form_gets_the_review_rules_refusals(rdb, rclient, tmp_path):
    f = await seed_frame(rdb, tmp_path, boxes=BOX)
    g = await seed_frame(rdb, tmp_path, frame_id=2, boxes=BOX)
    other = await make_user(rdb, "other")
    await add_verdict(rdb, f["event_ids"][0], other, name="Seriola zonata")
    await add_item(rdb, g, status="claimed", claimed_by=other,
                   claimed_until=datetime.now(UTC) + timedelta(minutes=5))
    await make_user(rdb, "alice")
    login(rclient, "alice")
    r = rclient.post("/api/corrections", headers=CSRF, json=_form(f["event_ids"][0],
                                                                  not_a_fish=True))
    assert r.status_code == 403
    assert r.json()["error"] == "another volunteer already answered this box"
    r = rclient.post("/api/corrections", headers=CSRF, json=_form(g["event_ids"][0],
                                                                  not_a_fish=True))
    assert r.status_code == 409
    r = rclient.post("/api/corrections", headers=CSRF, json=_form(
        g["event_ids"][0], corrected_name="Mystery fish"))
    assert r.status_code == 400 and "not in the species list" in r.json()["error"]


async def test_a_detection_without_a_saved_frame_is_refused_readably(rdb, rclient, tmp_path):
    f = await seed_frame(rdb, tmp_path, boxes=BOX, save=False)
    await make_user(rdb, "alice")
    login(rclient, "alice")
    r = rclient.post("/api/corrections", headers=CSRF, json=_form(f["event_ids"][0],
                                                                  not_a_fish=True))
    assert r.status_code == 400
    assert r.json()["error"] == "this detection's frame was not saved"
    assert await _rows(rdb, f["event_ids"][0]) == []


async def test_a_double_click_saves_one_row(rdb, rclient, tmp_path):
    f = await seed_frame(rdb, tmp_path, boxes=BOX)
    await make_user(rdb, "alice")
    login(rclient, "alice")
    body = _form(f["event_ids"][0], corrected_name="Seriola zonata", confidence="certain")
    assert rclient.post("/api/corrections", headers=CSRF, json=body).status_code == 200
    assert rclient.post("/api/corrections", headers=CSRF, json=body).status_code == 200
    assert len(await _rows(rdb, f["event_ids"][0])) == 1


async def test_a_too_long_note_is_a_400_not_a_500(rdb, rclient, tmp_path):
    f = await seed_frame(rdb, tmp_path, boxes=BOX)
    await make_user(rdb, "alice")
    login(rclient, "alice")
    r = rclient.post("/api/corrections", headers=CSRF, json=_form(
        f["event_ids"][0], not_a_fish=True, notes="x" * 3000))
    assert r.status_code == 400 and r.json()["error"]


@pytest.fixture
def spotty_client(rdb, review_assets, monkeypatch):
    """rclient, but every first answer through the review page is spot-checked."""
    from app.main import build_app
    from fastapi.testclient import TestClient

    monkeypatch.setenv("DATABASE_URL", TEST_DSN)
    monkeypatch.setenv("DASHBOARD_COOKIE_SECURE", "false")
    monkeypatch.setenv("REVIEW_SPOTCHECK_RATE", "1.0")
    for key, value in review_assets["env"].items():
        monkeypatch.setenv(key, value)
    with TestClient(build_app()) as client:
        yield client


async def test_the_old_form_is_never_spot_checked(rdb, spotty_client, tmp_path):
    f = await seed_frame(rdb, tmp_path, boxes=BOX)
    g = await seed_frame(rdb, tmp_path, frame_id=2, boxes=BOX)
    await make_user(rdb, "alice")
    login(spotty_client, "alice")
    assert spotty_client.post("/api/corrections", headers=CSRF, json=_form(
        f["event_ids"][0], not_a_fish=True)).status_code == 200
    # the control: the review page's own no-item answer is spot-checked at this rate
    assert spotty_client.post("/api/review/verdicts", headers=CSRF, json={
        "event_id": g["event_ids"][0], "outcome": "no_animal"}).json()["spot_check_scheduled"]
    async with rdb.connection() as conn:
        cur = await conn.execute("SELECT event_id FROM review_items WHERE pass = 2")
        assert [r[0] for r in await cur.fetchall()] == [g["event_ids"][0]]
```

`random` is unused in this module; drop the import if ruff flags it.

Create `tests/test_remap_legacy_corrections.py`:

```python
"""scripts/dev/remap_legacy_corrections.py (follow-up spec A1b)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import psycopg
import pytest
from app.review_catalogue import CatalogueStore
from review_fixtures import TEST_DSN, add_verdict, catalogue_paths, make_user, seed_frame

pytestmark = pytest.mark.anyio

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "dev" / "remap_legacy_corrections.py"


def _script():
    spec = importlib.util.spec_from_file_location("remap_legacy_corrections", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


async def _world(rdb, tmp_path):
    f = await seed_frame(rdb, tmp_path, boxes=[{"bbox": [10 + 100 * i, 10, 90 + 100 * i, 90]}
                                               for i in range(4)])
    e = f["event_ids"]
    uid = await make_user(rdb, "one")
    return {
        "maps": await add_verdict(rdb, e[0], None, name="Pomatomus saltarix"),
        "same": await add_verdict(rdb, e[1], None, name="Seriola zonata"),
        "lost": await add_verdict(rdb, e[2], None, name="Mystery fish"),
        # a review-tool row records its label source: never touched
        "tool": await add_verdict(rdb, e[3], uid, name="Pomatomus saltarix",
                                  label_source="search"),
    }


async def _names(rdb):
    async with rdb.connection() as conn:
        cur = await conn.execute(
            "SELECT id, corrected_name, corrected_species_id FROM detection_corrections")
        return {r[0]: r[1:] for r in await cur.fetchall()}


async def test_a_dry_run_lists_the_changes_and_changes_nothing(rdb, tmp_path, review_assets):
    ids = await _world(rdb, tmp_path)
    before = await _names(rdb)
    cat = CatalogueStore(catalogue_paths(review_assets)).get()
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        report = _script().remap(conn, cat, apply=False)
    assert report.changes == [(ids["maps"], "Pomatomus saltarix", "Pomatomus saltatrix")]
    assert report.unmapped == [(ids["lost"], "Mystery fish")]
    assert report.unchanged == 1
    assert await _names(rdb) == before


async def test_apply_maps_both_name_columns_and_is_idempotent(rdb, tmp_path, review_assets):
    ids = await _world(rdb, tmp_path)
    cat = CatalogueStore(catalogue_paths(review_assets)).get()
    mod = _script()
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        mod.remap(conn, cat, apply=True)
        assert mod.remap(conn, cat, apply=True).changes == []
    names = await _names(rdb)
    assert names[ids["maps"]] == ("Pomatomus saltatrix", "Pomatomus saltatrix")
    assert names[ids["lost"]][0] == "Mystery fish"
    assert names[ids["tool"]][0] == "Pomatomus saltarix"


async def test_the_command_line_prints_a_dry_run(rdb, tmp_path, review_assets, monkeypatch,
                                                 capsys):
    await _world(rdb, tmp_path)
    for key, value in review_assets["env"].items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("DATABASE_URL", TEST_DSN)
    env = tmp_path / "test.env"
    env.write_text("DATABASE_URL=unused-because-already-set\n")
    assert _script().main(["--env", str(env)]) == 0
    out = capsys.readouterr().out
    assert "'Pomatomus saltarix' -> 'Pomatomus saltatrix'" in out
    assert "'Mystery fish': not in the species list; left alone" in out
    assert "1 would change, 1 already accepted, 1 unmapped" in out
    assert TEST_DSN not in out
```

Create `tests/test_dashboard_page.py`:

```python
"""The main dashboard's script (app.js)."""
from __future__ import annotations

from js_source import function_body, read


def test_the_old_form_shows_the_review_rules_reason_for_a_refusal():
    body = function_body(read("app.js"), "postCorrection")
    assert "resp.status === 403 && !data.error" in body
    assert "alert(data.error ||" in body
```

In `tests/test_review_docs.py`, add:

```python
def test_operations_doc_covers_the_old_form_and_its_remap():
    ops = (REPO / "docs" / "operations.md").read_text()
    for phrase in ("remap_legacy_corrections.py", "--apply", "REVIEW_SKIP_TTL_DAYS"):
        assert phrase in ops, phrase
```

- [ ] **Step 3: Run to see them fail.**
  - Run: `… -m pytest tests/test_legacy_corrections.py tests/test_remap_legacy_corrections.py tests/test_dashboard_page.py tests/test_review_docs.py -q`
  - Expected: ImportError for `legacy_verdict`, then FAILs.

- [ ] **Step 4: Implement.**

`review_catalogue.py`, `Catalogue`, after `has_name`:

```python
    def stages(self, name: str) -> list[str | None]:
        """The life stages recorded for `name`; None is "no stage"."""
        return [e.life_stage for e in self._by_name.get(name, [])]
```

`review_queue.py`, after `_validate`:

```python
def legacy_verdict(body, *, catalogue) -> VerdictIn:
    """The dashboard's older correction form (POST /api/corrections;
    main.CorrectionIn) as a review verdict (follow-up spec A1), so it is
    saved under every rule submit_verdict enforces. That form has no
    life-stage picker, so a species with one catalogue entry takes that
    entry's stage, and a species with several is sent to the review page.
    A box on "not a fish" is dropped, and "uncertain" becomes "not sure"
    only where a species was chosen: the review rules refuse either
    otherwise."""
    common = {"event_id": body.event_id, "celebrity_id": body.celebrity_id,
              "notes": body.notes}
    if body.not_a_fish:
        return VerdictIn(outcome="no_animal", **common)
    raw = body.corrected_name or body.corrected_species_id
    if not raw:
        raise Invalid("provide corrected_name, corrected_species_id, or set not_a_fish")
    name = catalogue.accepted(raw)
    if not catalogue.has_name(name):
        raise Invalid(f"{raw!r} is not in the species list")
    stages = catalogue.stages(name)
    if len(stages) > 1:
        raise Invalid(f"choose a life stage for {name} on the review page")
    fields = {**common, "life_stage": stages[0], "bbox": body.corrected_bbox,
              "not_sure": body.confidence == "uncertain"}
    if body.is_endorsement:
        return VerdictIn(outcome="accept", **fields)
    return VerdictIn(outcome="species", label=name, label_source="search", **fields)
```

`main.py`:
- add `import random`;
- add `from pydantic import ValidationError` (next to the existing pydantic import);
- add `from .review_queue import ReviewError, legacy_verdict, submit_verdict` beside the other `from .` imports;
- replace the body of `post_correction`, keeping its decorator and signature:

```python
        """The dashboard's older correction form, saved through the review
        tool's own rules (follow-up spec A1): submit_verdict's no-item path,
        never spot-checked (spec §8). The form gets the event lock, the
        live-claim and pending-spot-check guards, accepted names, and the
        closing of the box's open queue items. It therefore answers 403 when
        another volunteer already answered the box, 409 while someone holds
        it, and 400 for a name outside the species list or a detection whose
        frame was not saved."""
        pool: AsyncConnectionPool | None = state.get("pool")
        if pool is None:
            return JSONResponse({"error": "database unavailable"}, status_code=503)
        catalogue = state["review_catalogue"].get()
        try:
            verdict = legacy_verdict(body, catalogue=catalogue)
            async with pool.connection() as conn:
                out = await submit_verdict(conn, user=user, body=verdict, catalogue=catalogue,
                                           spot_rate=0.0, rng=random.Random())
        except ValidationError as e:
            return JSONResponse({"error": "; ".join(
                f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors())},
                status_code=400)
        except ReviewError as e:
            return JSONResponse({"error": e.message}, status_code=e.status)
        pool_obj = state.get("review_pool")
        if pool_obj and out["first_pass"] and out["counts"] and not out["duplicate"]:
            pool_obj.note_verified(out["label"])
        return JSONResponse({"id": out["id"], "event_id": body.event_id,
                             "created_at": out["created_at"],
                             "is_endorsement": body.is_endorsement})
```

This removes the old bbox-shape, celebrity and empty-name checks: `validate_bbox`, `validate_label` and `legacy_verdict` now make them.

`app.js`, `postCorrection`: replace the lines from `if (resp.status === 403) {` through `if (!resp.ok) throw new Error(\`${resp.status}\`);` with:

```js
    let data = {};
    try { data = await resp.json(); } catch (e) { data = {}; }
    // A role refusal has no `error` field (it is FastAPI's `detail`); the
    // review rules' refusals do, and say why (follow-up spec A1).
    if (resp.status === 403 && !data.error) {
      alert("your account isn't approved for corrections yet — an administrator needs to grant reviewer access");
      return;
    }
    if (!resp.ok) {
      alert(data.error || `save failed: ${resp.status}`);
      return;
    }
```

Create `scripts/dev/remap_legacy_corrections.py` and make it executable (`chmod +x`):

```python
#!/usr/bin/env python
"""Map the old correction form's species names to the species list's accepted names.

The dashboard's older form wrote free-typed and classifier names (such as
"Pomatomus saltarix") straight into detection_corrections. This maps each
such row's corrected_name, and its corrected_species_id, to the accepted
name the review tool uses (follow-up spec A1b). Rows it can't map are
listed and left alone.

A dry run by default: it prints what would change and changes nothing.
--apply makes every change in one transaction. On production, run --apply
only after the owner has seen the dry run.

    python scripts/dev/remap_legacy_corrections.py --env /raid/scratch/dzimmerman2021/wahoobay/.env
    python scripts/dev/remap_legacy_corrections.py --env ... --apply
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "services" / "dashboard"))

# Old-form rows: every review-tool species verdict records where its label
# came from; the old form never did.
LEGACY_ROWS_SQL = """
    SELECT id, corrected_name, corrected_species_id
      FROM detection_corrections
     WHERE label_source IS NULL AND corrected_name IS NOT NULL
     ORDER BY id
"""


@dataclass
class Report:
    changes: list[tuple[int, str, str]] = field(default_factory=list)   # (id, old, new)
    unmapped: list[tuple[int, str]] = field(default_factory=list)       # (id, name)
    unchanged: int = 0


def remap(conn, catalogue, *, apply: bool) -> Report:
    """`conn` must be in autocommit mode, as main() opens it, so that the
    updates commit as one transaction of their own."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(LEGACY_ROWS_SQL)
        rows = cur.fetchall()
    report = Report()
    for r in rows:
        new = catalogue.accepted(r["corrected_name"])
        if not catalogue.has_name(new):
            report.unmapped.append((r["id"], r["corrected_name"]))
        elif new == r["corrected_name"] and new == r["corrected_species_id"]:
            report.unchanged += 1
        else:
            report.changes.append((r["id"], r["corrected_name"], new))
    if apply and report.changes:
        with conn.transaction(), conn.cursor() as cur:
            for row_id, _old, new in report.changes:
                cur.execute("""UPDATE detection_corrections
                                  SET corrected_name = %s, corrected_species_id = %s
                                WHERE id = %s""", (new, new, row_id))
    return report


def _load_env(path: Path) -> None:
    """The dashboard's own settings, so the catalogue is built from the same
    files it uses. Variables already set win."""
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--env", type=Path, required=True, help=".env file holding DATABASE_URL")
    ap.add_argument("--apply", action="store_true", help="make the changes (default: dry run)")
    args = ap.parse_args(argv)
    _load_env(args.env)
    from app.main import DashboardCfg
    from app.review_catalogue import CatalogueStore

    catalogue = CatalogueStore(DashboardCfg().review_catalogue_paths()).get()
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
        report = remap(conn, catalogue, apply=args.apply)
    for row_id, old, new in report.changes:
        print(f"{row_id}\t{old!r} -> {new!r}")
    for row_id, name in report.unmapped:
        print(f"{row_id}\t{name!r}: not in the species list; left alone")
    verb = "changed" if args.apply else "would change"
    print(f"{len(report.changes)} {verb}, {report.unchanged} already accepted, "
          f"{len(report.unmapped)} unmapped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

`docs/operations.md`, "Review tool (/review)" section, add:

```
### The dashboard's older correction form

`POST /api/corrections` (the endorse / correct / not-a-fish buttons on the public
dashboard) is saved through the review tool's rules. It is never spot-checked. It answers
403 when another volunteer already answered the box, 409 while someone holds it, and 400
for a name outside the species list or a detection whose frame was not saved (most live
detections).

Rows the form wrote before this change can carry raw or misspelled names. To see what
would be mapped to accepted names, run:

    python scripts/dev/remap_legacy_corrections.py --env .env

Then run it with `--apply` to make the changes in one transaction. Names it can't map
are listed and left alone.
```

`tests/test_auth_db.py`:
- **The `pool` fixture:** add `saved_frames` to the `TRUNCATE detection_events, detection_corrections CASCADE` line.
- **`test_approved_reviewer_can_submit_a_correction`:**
  - add the `tmp_path` parameter;
  - import `T0` and `jpeg` from `review_fixtures`;
  - seed the event with a timestamp and a saved frame, replacing its `INSERT INTO detection_events` statement with:

```python
        await conn.execute(
            """INSERT INTO detection_events (id, ts, frame_id, source_name, det_conf,
                                             bbox, topk)
               VALUES (4242, %s, 1, 'seahivecam', 0.9, '{1,2,3,4}', '[]'::jsonb)""", (T0,))
        # The form saves through the review rules now, which need the frame.
        await conn.execute(
            """INSERT INTO saved_frames (ts, frame_id, source_name, reason, image_path, num_fish)
               VALUES (%s, 1, 'seahivecam', 'interesting:high_conf', %s, 1)""",
            (T0, jpeg(tmp_path / "frame.jpg")))
```

- [ ] **Step 5: Run the tests.**
  - Run: `… -m pytest tests/test_legacy_corrections.py tests/test_remap_legacy_corrections.py tests/test_dashboard_page.py tests/test_review_docs.py tests/test_auth_db.py tests/test_review_queue.py tests/test_review_catalogue.py -q; echo exit=$?`
  - Also run `node --check services/dashboard/app/static/app.js`.
  - Expected: all dots, `exit=0`.

- [ ] **Step 6: Commit** — "dashboard: the old correction form saves through the review rules"

```bash
git add services/dashboard/app/review_catalogue.py services/dashboard/app/review_queue.py services/dashboard/app/main.py services/dashboard/app/static/app.js scripts/dev/remap_legacy_corrections.py docs/operations.md tests/js_source.py tests/test_legacy_corrections.py tests/test_remap_legacy_corrections.py tests/test_dashboard_page.py tests/test_review_docs.py tests/test_auth_db.py
git commit -F .superpowers/sdd/commit-msg.txt
```

---

### Task 4: Public outputs — exact frames, events counted once, pending spot-checks private, admin names off alerts

**Files:**
- Modify: `services/dashboard/app/exports.py`:
  - `IDENTITY_COLUMNS`;
  - new `hide_pending_spot_checks`;
  - `export_corrections`, `export_labeled_corrections`, `export_alerts`.
- Modify: `services/dashboard/app/main.py`:
  - `list_corrections`;
  - the celebrity list and `get_celebrity`;
  - `species_profile`'s frames query;
  - `alerts_active`, `export_alerts_csv`;
  - the `.exports` import.
- Modify: `services/dashboard/app/static/app.js`, `renderAlerts`.
- Modify: `tests/review_fixtures.py`, adding `alerts` to `TRUNCATE_SQL`.
- Test: `tests/test_privacy_exports.py`, `tests/test_dashboard_page.py`.

**Interfaces:**
- Consumes: `tests/js_source.py` (Task 3).
- Produces: `exports.hide_pending_spot_checks(alias: str) -> str`. `IDENTITY_COLUMNS` gains `"acknowledged_by"`.

- [ ] **Step 1: Write the failing tests.**

In `tests/test_privacy_exports.py`:
- add imports: `csv`; `from datetime import UTC, datetime`; and `add_item` from `review_fixtures`;
- add these tests at the end.

```python
def _csv_events(text: str) -> set[int]:
    return {int(r["event_id"]) for r in csv.DictReader(io.StringIO(text))}


async def _spot_pending(rdb, tmp_path, status="open"):
    frame = await _seed_named_verdict(rdb, tmp_path)
    async with rdb.connection() as conn:
        cur = await conn.execute("SELECT id, user_id FROM detection_corrections")
        first, alice = await cur.fetchone()
    spot = await add_item(rdb, frame, pass_=2, status=status, exclude_user_id=alice,
                          first_correction_id=first)
    return frame["event_ids"][0], spot


@pytest.mark.parametrize("status", ["open", "claimed"])
async def test_a_pending_spot_checks_first_answer_stays_out_of_public_lists(rdb, rclient,
                                                                           tmp_path, status):
    event, spot = await _spot_pending(rdb, tmp_path, status)
    assert event not in {r["event_id"] for r in rclient.get("/api/corrections").json()}
    assert event not in _csv_events(rclient.get("/api/export/corrections.csv").text)
    assert event not in _csv_events(rclient.get("/api/export/labeled_corrections.csv").text)
    await make_user(rdb, "boss", role="admin")
    login(rclient, "boss")
    assert event in {r["event_id"] for r in rclient.get("/api/corrections").json()}
    assert event in _csv_events(rclient.get("/api/export/corrections.csv").text)
    rclient.cookies.clear()
    async with rdb.connection() as conn:
        await conn.execute("UPDATE review_items SET status = 'done' WHERE id = %s", (spot,))
    assert event in {r["event_id"] for r in rclient.get("/api/corrections").json()}


async def test_cant_identify_shows_as_such_in_public_outputs(rdb, rclient, tmp_path):
    frame = await seed_frame(rdb, tmp_path, boxes=[{"bbox": [100, 100, 300, 250]}])
    await add_verdict(rdb, frame["event_ids"][0], await make_user(rdb, "alice"),
                      unidentifiable=True)
    [row] = rclient.get("/api/corrections").json()
    assert row["unidentifiable"] is True and row["corrected_name"] is None
    [csv_row] = list(csv.DictReader(io.StringIO(
        rclient.get("/api/export/corrections.csv").text)))
    assert csv_row["unidentifiable"] == "True"


async def test_the_celebrity_gallery_counts_each_event_once_and_hides_a_pending_spot_check(
        rdb, rclient, tmp_path, monkeypatch):
    bobby = CELEBRITIES["celebrities"][0]
    state = _app_state(rclient)
    monkeypatch.setitem(state, "celebrities_by_id", {bobby["id"]: bobby})
    monkeypatch.setitem(state, "celebrities", [bobby])
    frame = await seed_frame(rdb, tmp_path, boxes=[{"bbox": [100, 100, 300, 250],
                                                    "species": "Acanthurus coeruleus"}])
    alice = await make_user(rdb, "alice")
    event = frame["event_ids"][0]
    async with rdb.connection() as conn:
        for _ in range(2):   # an answer, then the same volunteer changing it
            await conn.execute(
                """INSERT INTO detection_corrections (event_id, corrected_name, user_id,
                                                      celebrity_id)
                   VALUES (%s, 'Acanthurus coeruleus', %s, %s)""", (event, alice, bobby["id"]))
    detail = rclient.get(f"/api/celebrities/{bobby['id']}").json()
    assert detail["sightings"] == 1 and [g["event_id"] for g in detail["gallery"]] == [event]
    [listed] = rclient.get("/api/celebrities").json()["celebrities"]
    assert listed["sightings"] == 1
    async with rdb.connection() as conn:
        cur = await conn.execute("SELECT min(id) FROM detection_corrections")
        first = (await cur.fetchone())[0]
    await add_item(rdb, frame, pass_=2, exclude_user_id=alice, first_correction_id=first)
    assert rclient.get(f"/api/celebrities/{bobby['id']}").json()["gallery"] == []
    await make_user(rdb, "boss", role="admin")
    login(rclient, "boss")
    assert len(rclient.get(f"/api/celebrities/{bobby['id']}").json()["gallery"]) == 1


async def test_the_species_gallery_uses_the_frame_saved_at_the_detections_own_time(
        rdb, rclient, tmp_path):
    now = datetime.now(UTC)
    mine = await seed_frame(rdb, tmp_path, ts=now - timedelta(hours=2), frame_id=7,
                            boxes=[{"bbox": [10, 10, 90, 90], "species": "Seriola zonata"}])
    # frame_id restarts with every worker run: the same camera and frame_id
    # an hour later is another frame, with no Seriola in it
    await seed_frame(rdb, tmp_path, ts=now - timedelta(hours=1), frame_id=7, boxes=[])
    r = rclient.get("/api/species/Seriola%20zonata/profile?hours=24")
    assert r.status_code == 200, r.text
    frames = r.json()["recent_frames"]
    assert [fr["image_path"] for fr in frames] == [mine["image_path"]]


async def _alert(rdb):
    async with rdb.connection() as conn:
        await conn.execute(
            """INSERT INTO alerts (name, severity, message, first_seen, last_seen,
                                   acknowledged_by, acknowledged_at)
               VALUES ('privacy_test_alert', 'warning', 'no frames for 5 min', now(), now(),
                       'Boss Person', now())""")
    # a name no SLO rule uses, so the app's own checker never resolves it


async def test_the_alert_acknowledger_is_named_to_admins_only(rdb, rclient):
    await _alert(rdb)
    r = rclient.get("/api/alerts/active")
    assert r.status_code == 200 and r.json()
    assert all("acknowledged_by" not in a for a in r.json()) and "Boss Person" not in r.text
    text = rclient.get("/api/export/alerts.csv").text
    assert "acknowledged_by" not in _header(text) and "Boss Person" not in text
    await make_user(rdb, "boss", role="admin")
    login(rclient, "boss")
    assert rclient.get("/api/alerts/active").json()[0]["acknowledged_by"] == "Boss Person"
    assert "Boss Person" in rclient.get("/api/export/alerts.csv").text


async def test_a_reviewer_session_is_never_treated_as_admin(rdb, rclient, tmp_path):
    """Phase-1 triage 77: a signed-in reviewer is the likeliest non-admin
    caller of every identity-filtered endpoint."""
    await _seed_named_verdict(rdb, tmp_path)
    await _alert(rdb)
    login(rclient, "alice")
    rows = rclient.get("/api/corrections").json()
    assert rows and all("reviewer" not in r and "notes" not in r for r in rows)
    assert rclient.get("/api/corrections?reviewer=Alice%20Example").status_code == 403
    for path in ("/api/export/corrections.csv", "/api/export/labeled_corrections.csv",
                 "/api/export/alerts.csv"):
        text = rclient.get(path).text
        assert not ((IDENTITY | {"acknowledged_by"}) & _header(text)), path
        assert "Alice Example" not in text and "Boss Person" not in text, path
    assert all("acknowledged_by" not in a for a in rclient.get("/api/alerts/active").json())
```

Add `timedelta` to the `datetime` import. `_seed_named_verdict` makes the user "alice", so `login(rclient, "alice")` works after it.

In `tests/test_dashboard_page.py`, add:

```python
from js_source import function_source, needs_node, run_node


@needs_node
def test_alert_text_is_set_as_text_never_as_markup(tmp_path):
    app = read("app.js")
    assert "innerHTML" not in function_body(app, "renderAlerts")
    out = run_node(['const AGE_FMT = () => "5m"; function ackAlert() {}',
                    function_source(app, "renderAlerts")], """
      renderAlerts([{ id: 1, name: "<img src=x onerror=alert(1)>", message: "<b>loud</b>",
                      severity: "warning", first_seen: "2026-09-26T12:00:00Z",
                      acknowledged_at: "2026-09-26T12:01:00Z" }]);
      const texts = [];
      const walk = (n) => {
        if (typeof n === "string") { texts.push(n); return; }
        if (n.textContent) texts.push(n.textContent);
        (n.children || []).forEach(walk);
      };
      walk(document.getElementById("alerts-banner"));
      return texts;
    """, tmp_path)
    assert "<img src=x onerror=alert(1)>" in out
    assert ": <b>loud</b>" in out
    assert "ack'd" in out        # no acknowledger name reaches a public viewer
```

In `tests/review_fixtures.py`, `TRUNCATE_SQL`: add `alerts` to the table list.

- [ ] **Step 2: Run to see them fail.**
  - Run: `… -m pytest tests/test_privacy_exports.py tests/test_dashboard_page.py -q`
  - Expected: FAILs (the event is still listed, `unidentifiable` is missing, sightings are 2, `acknowledged_by` is present, and the node test finds no text node).

- [ ] **Step 3: Implement `exports.py`.**
  - `IDENTITY_COLUMNS = ("reviewer", "notes", "user_id", "session_id", "acknowledged_by")`.
  - Its comment becomes: "Columns that say who labelled a box, what they wrote, or which admin acknowledged an alert. Spec §12.1: since spec 1 these are accounts' display names, so they are returned to administrators only."
  - Add below `_without_identity`:

```python
def hide_pending_spot_checks(alias: str) -> str:
    """SQL condition for public outputs (follow-up spec D3): leave out every
    answer on a box whose spot-check is waiting or being answered. The
    second volunteer is shown the box's event id, so a public list naming
    that event's first answer would let them look it up."""
    return (f"NOT EXISTS (SELECT 1 FROM review_items ri WHERE ri.event_id = {alias}.event_id"
            " AND ri.pass = 2 AND ri.status IN ('open', 'claimed'))")
```

  - **`export_corrections`:**
    - after its reviewer filter, add `if not include_identity: where.append(hide_pending_spot_checks("c"))`;
    - add `c.unidentifiable` after `c.not_a_fish` in the SELECT, and `"unidentifiable"` after `"not_a_fish"` in `cols`;
    - its `include_identity` means "the caller is an admin". Say so in a one-line comment: "include_identity: an admin is asking; they also see answers under a pending spot-check".
  - **`export_labeled_corrections`:** before `ORDER BY c.created_at DESC`, insert `{where_sql}` and make the SQL an f-string, where `where_sql = "" if include_identity else "WHERE " + hide_pending_spot_checks("c")`.
  - **`export_alerts(pool, include_resolved, include_identity=False)`:** return `_stream(pool, sql, tuple(), _without_identity(cols, include_identity))`.

- [ ] **Step 4: Implement `main.py`.**
  - Import `hide_pending_spot_checks` from `.exports`.
  - **`list_corrections`:**
    - after `if reviewer:` …, add `if not admin: where.append(hide_pending_spot_checks("c"))`;
    - SELECT gains `c.unidentifiable` after `c.not_a_fish`;
    - `reviewer = %s` becomes `c.reviewer = %s`.
  - **Celebrity list** (the `SELECT celebrity_id, COUNT(*)::int AS sightings, …` query): `COUNT(DISTINCT event_id)::int AS sightings`.
  - **`get_celebrity`:**
    - its first query uses `COUNT(DISTINCT event_id)::int AS sightings`;
    - the gallery query becomes:

```python
                    # One entry per event -- its latest tag -- and, for the
                    # public, none while a spot-check on it is pending
                    # (follow-up D2, D3).
                    hide = "" if _is_admin(user) else f"AND {hide_pending_spot_checks('dc')}"
                    await cur.execute(f"""
                        SELECT * FROM (
                            SELECT DISTINCT ON (dc.event_id)
                                   dc.event_id, dc.created_at, dc.reviewer, dc.confidence,
                                   dc.corrected_name,
                                   e.ts AS event_ts, e.source_name, e.bbox
                              FROM detection_corrections dc
                              LEFT JOIN detection_events e ON e.id = dc.event_id
                             WHERE dc.celebrity_id = %s {hide}
                             ORDER BY dc.event_id, dc.created_at DESC, dc.id DESC
                        ) latest
                         ORDER BY created_at DESC
                         LIMIT %s
                    """, (celebrity_id, gallery_limit))
```

  - **`species_profile`**'s saved-frames query: the JOIN becomes `JOIN detection_events e ON e.frame_id = sf.frame_id AND e.source_name = sf.source_name AND e.ts = sf.ts`, with the comment "camera + frame_id + ts: frame_id restarts with every worker run (follow-up D1)".
  - **`alerts_active`:**
    - signature `async def alerts_active(user=Depends(optional_user)) -> Response:`;
    - after the isoformat loop:

```python
        if not _is_admin(user):
            # Follow-up D5: an acknowledger is an admin's display name.
            for r in rows:
                r.pop("acknowledged_by", None)
```

  - **`export_alerts_csv`:** add `user=Depends(optional_user)`; call `export_alerts(pool, include_resolved, include_identity=_is_admin(user))`.

- [ ] **Step 5: Implement `app.js` `renderAlerts`:**

```js
function renderAlerts(rows) {
  const banner = document.getElementById("alerts-banner");
  if (!rows || rows.length === 0) {
    banner.classList.add("hidden");
    banner.replaceChildren();
    return;
  }
  banner.classList.remove("hidden");
  // Follow-up D5: alert names and messages come from the database, so they
  // are set as text, never parsed as markup; the acknowledger's name only
  // arrives for admins.
  const span = (cls, text) => {
    const s = document.createElement("span");
    s.className = cls;
    s.textContent = text;
    return s;
  };
  banner.replaceChildren(...rows.map((a) => {
    const row = document.createElement("div");
    row.className = "alert-row";
    const name = document.createElement("b");
    name.textContent = a.name;
    const msg = span("alert-msg", "");
    msg.append(name, `: ${a.message}`);
    row.append(span(`alert-sev ${a.severity}`, a.severity), msg,
      span("alert-age", `${AGE_FMT(a.first_seen)} old`));
    if (a.acknowledged_at) {
      row.append(span("alert-age", a.acknowledged_by ? `ack'd by ${a.acknowledged_by}` : "ack'd"));
    } else {
      const ack = document.createElement("button");
      ack.className = "alert-ack";
      ack.textContent = "ack";
      ack.addEventListener("click", () => ackAlert(a.id));
      row.append(ack);
    }
    return row;
  }));
}
```

- [ ] **Step 6: Run the tests.**
  - Run: `… -m pytest tests/test_privacy_exports.py tests/test_dashboard_page.py tests/test_review_routes.py tests/test_auth_db.py -q; echo exit=$?`
  - Also run `node --check` on `app.js`.
  - Expected: all dots, `exit=0`.

- [ ] **Step 7: Commit** — "public outputs: exact frames, events counted once, pending spot-checks private, admin names off alerts"

```bash
git add services/dashboard/app/exports.py services/dashboard/app/main.py services/dashboard/app/static/app.js tests/review_fixtures.py tests/test_privacy_exports.py tests/test_dashboard_page.py
git commit -F .superpowers/sdd/commit-msg.txt
```

---

### Task 5: File reads off the event loop; the frame-size cache notices a changed file

**Files:**
- Modify: `services/dashboard/app/review_images.py`: `frame_size`.
- Modify: `services/dashboard/app/review_queue.py`: `build_payload` and `submit_verdict`'s size reads; `import asyncio`.
- Modify: `services/dashboard/app/review_frames.py`: `frame_record`; `import asyncio`.
- Modify: `services/dashboard/app/review_catalogue.py`: `CatalogueStore.aget` and `current`; `import asyncio`.
- Modify: `services/dashboard/app/routes_review.py`: `_catalogue` and its call sites.
- Modify: `services/dashboard/app/review_pool.py`: `_fill_check`, `species_order`.
- Modify: `services/dashboard/app/main.py`: `post_correction`'s catalogue read.
- Test: create `tests/test_review_images.py`; modify `tests/test_review_catalogue.py` and `tests/test_review_queue.py`.

**Interfaces:**
- Produces:
  - `CatalogueStore.aget() -> Catalogue` (async);
  - `CatalogueStore.current() -> Catalogue`;
  - `review_images._frame_size_at(path: str, mtime_ns: int)`, the cached reader.

- [ ] **Step 1: Write the failing tests.**

Create `tests/test_review_images.py`:

```python
"""Frame reading helpers (review_images.py)."""
from __future__ import annotations

import os

import pytest
from app.review_images import frame_size
from review_fixtures import jpeg


def test_frame_size_notices_a_replaced_or_deleted_frame(tmp_path):
    """Phase-1 triage 146: frames deleted or replaced by hand must be seen
    as missing or changed, not served from the cache."""
    path = jpeg(tmp_path / "f.jpg", size=(1280, 720))
    assert frame_size(path) == (1280, 720)
    st = os.stat(path)
    jpeg(tmp_path / "f.jpg", size=(640, 480))
    # a later modification time, even where the clock is coarse
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    assert frame_size(path) == (640, 480)
    os.remove(path)
    with pytest.raises(FileNotFoundError):
        frame_size(path)
```

In `tests/test_review_catalogue.py`, add; add `import os`, `import threading` and `import pytest` if missing:

```python
def test_a_bad_file_keeps_the_previous_catalogue(review_assets):
    """Phase-1 triage 97: this is what keeps labelling alive when the daily
    poller writes a bad file."""
    paths = catalogue_paths(review_assets)
    store = CatalogueStore(paths)
    before = store.get()
    assert before.has_name("Seriola zonata")
    manifest = paths.species_manifest
    good, st = manifest.read_text(), manifest.stat()
    manifest.write_text("{ not json")
    os.utime(manifest, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    assert store.get() is before
    manifest.write_text(good)
    os.utime(manifest, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))
    after = store.get()
    assert after is not before and after.has_name("Seriola zonata")


def test_a_store_whose_first_load_fails_serves_an_empty_catalogue(review_assets):
    paths = catalogue_paths(review_assets)
    paths.species_manifest.write_text("{ not json")
    assert CatalogueStore(paths).get().names() == []


@pytest.mark.anyio
async def test_aget_rebuilds_off_the_event_loop_and_current_never_rebuilds(review_assets,
                                                                          monkeypatch):
    from app import review_catalogue

    seen = []
    real = review_catalogue.load_catalogue

    def spy(paths):
        seen.append(threading.current_thread() is threading.main_thread())
        return real(paths)

    monkeypatch.setattr(review_catalogue, "load_catalogue", spy)
    store = CatalogueStore(catalogue_paths(review_assets))
    cat = await store.aget()
    assert cat.has_name("Seriola zonata") and seen == [False]
    assert store.current() is cat and seen == [False]
```

Adjust the import of `catalogue_paths`/`CatalogueStore` to match what the module already has.

In `tests/test_review_queue.py`, add, with `import threading` and `frame_record` imported from `app.review_frames`:

```python
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
    await _verdict(rdb, cat, as_user(me), event_id=f["event_ids"][0],
                   review_item_id=served["id"], outcome="unidentifiable")
    assert len(seen) == 3 and not any(seen)
```

- [ ] **Step 2: Run to see them fail.**
  - Run: `… -m pytest tests/test_review_images.py tests/test_review_catalogue.py tests/test_review_queue.py -q -k "frame_size or catalogue or off_the_event_loop or bad_file or first_load"`
  - Expected: FAILs: stale cached size; no `aget`; reads on the main thread.

- [ ] **Step 3: Implement.**

`review_images.py`: add `import os`, then split `frame_size`:

```python
def frame_size(path: str) -> tuple[int, int]:
    """(width, height), read from the file header. Raises OSError for a
    missing, zero-byte or corrupt file ... (keep the existing docstring
    text). Cached per (path, modification time), so a frame deleted or
    replaced by hand is noticed (follow-up C5, phase-1 triage 146): a
    deleted one fails the stat here with FileNotFoundError."""
    return _frame_size_at(path, os.stat(path).st_mtime_ns)


@lru_cache(maxsize=4096)
def _frame_size_at(path: str, mtime_ns: int) -> tuple[int, int]:
    <the existing frame_size body, unchanged>
```

Keep the existing docstring's text in `frame_size`, and append the new sentences.

`review_queue.py`, `review_frames.py`:
- in `build_payload`: `width, height = await asyncio.to_thread(safe_frame_size, ev["image_path"])`;
- in `submit_verdict`: the same line;
- in `frame_record`:

```python
    # Off the event loop (follow-up C6): a PIL header read, and a stat.
    row["width"], row["height"] = await asyncio.to_thread(
        safe_frame_size, row["image_path"], "this frame is missing on disk")
```

`review_catalogue.py`, `CatalogueStore`, after `get`:

```python
    async def aget(self) -> Catalogue:
        """get() for async code (follow-up C6): its stat calls, and any
        rebuild's file reads, run on a worker thread, not the event loop."""
        return await asyncio.to_thread(self.get)

    def current(self) -> Catalogue:
        """The catalogue as last built, without looking at the files: for
        sync code on the event loop (ReviewPool.species_order). The pool's
        runs and every review route refresh it through aget(). Builds it
        once if nothing has yet."""
        return self._catalogue if self._catalogue is not None else self.get()
```

`routes_review.py`:
- `_catalogue` becomes `async def _catalogue(): return await state["review_catalogue"].aget()`;
- every call site becomes `await _catalogue()`, which is 16 places;
- where a method is called on the result, parenthesise: `(await _catalogue()).to_json()` and `(await _catalogue()).photo(name_slug)`.

`review_pool.py`:
- in `_fill_check`: `catalogue = await self.catalogue_store.aget()`;
- in `species_order`: `self.catalogue_store.current().names()`;
- leave `_load_baseline` as it is: it runs once at start-up.

`main.py`, `post_correction`: `catalogue = await state["review_catalogue"].aget()`.

- [ ] **Step 4: Run the tests.**
  - Run the whole review set: `… -m pytest tests/test_review_images.py tests/test_review_catalogue.py tests/test_review_queue.py tests/test_review_frames.py tests/test_review_routes.py tests/test_review_pool.py tests/test_review_browse.py tests/test_legacy_corrections.py -q; echo exit=$?`
  - Expected: all dots, `exit=0`.

- [ ] **Step 5: Commit** — "review: frame and catalogue reads off the event loop; the size cache notices replaced files"

```bash
git add services/dashboard/app/review_images.py services/dashboard/app/review_queue.py services/dashboard/app/review_frames.py services/dashboard/app/review_catalogue.py services/dashboard/app/routes_review.py services/dashboard/app/review_pool.py services/dashboard/app/main.py tests/test_review_images.py tests/test_review_catalogue.py tests/test_review_queue.py
git commit -F .superpowers/sdd/commit-msg.txt
```

---

### Task 6: Review page navigation — wait your turn, give way, time out (B1, B3, B4) and page-test fixes

**Files:**
- Modify: `services/dashboard/app/static/review-core.js`:
  - `RV.api`, with a new `RV.timedOut`;
  - `RV.next`, taking a `startToken`;
  - the `DOMContentLoaded` landing call.
- Modify: `services/dashboard/app/static/review-single.js`: `loadNext`, `submit`, `skip`.
- Modify: `services/dashboard/app/static/review-frame.js`: `enter`.
- Test: `tests/test_review_page.py`.

**Interfaces:**
- Consumes: `tests/js_source.py` (Task 3).
- Produces:
  - `RV.next(startToken?: number)`: does nothing if `startToken` is given and `RV.navToken` has moved on;
  - `RV.API_TIMEOUT_MS = 20000`;
  - `RV.TIMED_OUT = "the server took too long; try again"`;
  - in `review-single.js`: `loadNext(startToken?)`.

- [ ] **Step 1: Switch `tests/test_review_page.py` to the shared helpers and harden the busy-guard check.**
  - Delete its local `STATIC`, `NODE`, `_braces_from`, `_function_body` and `_read` definitions, and the `shutil` import if it becomes unused.
  - Replace them with:

```python
from js_source import NODE, STATIC, needs_node, run_node
from js_source import braces_from as _braces_from
from js_source import function_body as _function_body
from js_source import read as _read
```

  - Add this helper, and make the three busy-guard tests (`test_mutating_handlers_share_the_busy_guard`, `test_frame_mutating_handlers_share_the_busy_guard`, `test_browse_mutating_handlers_share_the_busy_guard`) each call `_assert_busy_guarded(body, name)` instead of their three asserts:

```python
BUSY_RETURN = re.compile(r"\bbusy\b\s*\)\s*(?:\{\s*)?return\b")


def _assert_busy_guarded(body: str, name: str) -> None:
    """The busy-guard pattern: an early return while busy (braced or not),
    busy = true, and busy = false inside the function's finally block."""
    assert BUSY_RETURN.search(body), f"{name}() must return early when busy is already true"
    assert re.search(r"\bbusy\s*=\s*true\b", body), f"{name}() must set busy = true"
    m = re.search(r"\bfinally\s*\{", body)
    assert m, f"{name}() must reset busy in a finally block"
    assert re.search(r"\bbusy\s*=\s*false\b", _braces_from(body, m.end() - 1)), (
        f"{name}() must reset busy inside its finally block")


def test_the_busy_guard_check_accepts_braces_and_wants_the_reset_in_finally():
    good = "async function f() { if (busy) { return; } busy = true; try { x(); } finally { busy = false; } }"
    _assert_busy_guarded(_function_body(good, "f"), "f")
    bad = "async function f() { if (busy) return; busy = true; try { x(); } finally { y(); } busy = false; }"
    with pytest.raises(AssertionError):
        _assert_busy_guarded(_function_body(bad, "f"), "f")


def test_preview_boxes_take_clicks_though_the_overlay_does_not():
    rule = re.search(r"\.rv-other-link\s*\{([^}]*)\}", _read("review.css")).group(1)
    assert re.search(r"pointer-events:\s*auto", rule)
```

- [ ] **Step 2: Write the failing tests** (append to `tests/test_review_page.py`):

```python
@needs_node
def test_next_gives_way_once_the_volunteer_has_moved_on(tmp_path):
    out = run_node([_read("review-core.js")], """
      let asked = 0;
      globalThis.fetch = async () => { asked += 1;
        return { status: 200, ok: true, json: async () => ({ item: null, pool: null }) }; };
      RV.views.single = { enter: async () => {} };
      const started = RV.navToken;
      RV.navToken += 1;                    // Browse was clicked meanwhile
      await RV.next(started);
      const stale = { asked, moved: RV.navToken - started };
      await RV.next();                     // a fresh navigation still asks
      return { stale, asked };
    """, tmp_path)
    assert out == {"stale": {"asked": 0, "moved": 1}, "asked": 1}


@needs_node
def test_a_request_that_never_answers_times_out_and_frees_the_queue(tmp_path):
    out = run_node([_read("review-core.js")], """
      RV.API_TIMEOUT_MS = 50;
      const shown = [];
      let hang = true, asked = 0;
      globalThis.fetch = (url, opts) => {
        asked += 1;
        if (hang) {
          hang = false;
          return new Promise((_, reject) =>
            opts.signal.addEventListener("abort", () => reject(new Error("aborted"))));
        }
        return Promise.resolve({ status: 200, ok: true,
          json: async () => ({ item: null, pool: null }) });
      };
      RV.views.single = { enter: async (arg) => { shown.push(arg.result.data.error || null); } };
      await RV.next();                     // its /next never answers
      const banner = document.getElementById("rv-banner").textContent;
      await RV.next();                     // and the next one still runs
      return { shown, banner, asked };
    """, tmp_path)
    assert out["shown"] == ["the server took too long; try again", None]
    assert "took too long" in out["banner"]
    assert out["asked"] == 2


def test_rv_api_clears_its_timer_whatever_happens():
    body = _function_body(_read("review-core.js"), "api")
    assert "new AbortController()" in body
    assert "setTimeout(() => controller.abort(), RV.API_TIMEOUT_MS)" in body
    m = re.search(r"\bfinally\s*\{", body)
    assert m and "clearTimeout(timer)" in _braces_from(body, m.end() - 1)
    assert 'RV.TIMED_OUT = "the server took too long; try again"' in _read("review-core.js")


@needs_node
def test_a_frame_opens_only_after_a_superseded_next_has_given_its_box_back(tmp_path):
    out = run_node([_read("review-core.js"), _read("review-frame.js")], """
      const posted = [];
      globalThis.fetch = async (url) => { posted.push(url);
        return { status: 404, ok: false, json: async () => ({ error: "gone" }) }; };
      RV.views.single = { enter: async () => {} };
      let handOff;
      RV.nextTurn = new Promise((resolve) => { handOff = resolve; });   // a /next still out
      const opening = RV.views.frame.enter({ savedFrameId: 5 });
      await new Promise((r) => setTimeout(r, 20));
      const before = posted.slice();
      handOff();
      await opening;
      return { before, after: posted };
    """, tmp_path)
    assert out == {"before": [], "after": ["/api/review/frames/5/open"]}


@needs_node
def test_a_frame_the_volunteer_navigated_away_from_while_waiting_is_never_claimed(tmp_path):
    out = run_node([_read("review-core.js"), _read("review-frame.js")], """
      const posted = [];
      globalThis.fetch = async (url) => { posted.push(url);
        return { status: 404, ok: false, json: async () => ({}) }; };
      let handOff;
      RV.nextTurn = new Promise((resolve) => { handOff = resolve; });
      const opening = RV.views.frame.enter({ savedFrameId: 5 });
      RV.navToken += 1;                     // a lane click meanwhile
      handOff();
      await opening;
      return posted;
    """, tmp_path)
    assert out == []


@pytest.mark.parametrize("name", ["submit", "skip"])
def test_an_answer_or_skip_asks_for_the_next_box_only_if_nobody_moved_on(name):
    body = _function_body(_read("review-single.js"), name)
    assert "const nav = RV.navToken;" in body
    assert body.index("const nav = RV.navToken;") < body.index("await ")
    calls = re.findall(r"loadNext\(([^)]*)\)", body)
    assert calls and all(c == "nav" for c in calls)


def test_load_next_passes_the_token_on_and_leaves_the_screen_alone_if_stale():
    body = _function_body(_read("review-single.js"), "loadNext")
    guard = "if (startToken !== undefined && startToken !== RV.navToken) return;"
    assert guard in body and body.index(guard) < body.index("message(")
    assert "await RV.next(startToken);" in body


def test_the_landing_call_gives_way_to_a_click_made_while_the_page_loads():
    core = _read("review-core.js")
    at = core.index('document.addEventListener("DOMContentLoaded"')
    handler = _braces_from(core, core.index("{", core.index("=>", at)))
    assert "const landing = RV.navToken;" in handler
    assert handler.index("const landing = RV.navToken;") < handler.index("RV.loadCatalogue()")
    assert "await RV.next(landing);" in handler
```

- [ ] **Step 3: Run to see them fail.**
  - Run: `… -m pytest tests/test_review_page.py -q`
  - Expected: the new tests FAIL; the rewired existing tests still pass.

- [ ] **Step 4: Implement `review-core.js`.** Replace `RV.api` with:

```js
// Follow-up B3: a request that never settles would otherwise hold RV.next()'s
// queue -- and with it every lane click -- until the page is reloaded.
RV.API_TIMEOUT_MS = 20000;
RV.TIMED_OUT = "the server took too long; try again";

RV.api = async function api(method, url, body) {
  const controller = new AbortController();
  const options = { method, credentials: "same-origin", headers: RV.HEADERS,
    signal: controller.signal };
  if (body !== undefined) options.body = JSON.stringify(body);
  const timer = setTimeout(() => controller.abort(), RV.API_TIMEOUT_MS);
  try {
    let response;
    try {
      response = await fetch(url, options);
    } catch (err) {
      if (controller.signal.aborted) return RV.timedOut();
      RV.banner("Can't reach the server. Check your connection; nothing was saved.");
      return { ok: false, status: 0, data: { error: "network error" } };
    }
    if (response.status === 401) {
      window.location.href = "/login?next=" + encodeURIComponent("/review");
      return { ok: false, status: 401, data: { error: "signed out" } };
    }
    let data = {};
    try {
      data = await response.json();
    } catch (err) {
      if (controller.signal.aborted) return RV.timedOut();
      data = {};
    }
    if (response.status === 503) {
      RV.banner("The database is unavailable right now. Nothing was saved; try again in a minute.");
    } else if (response.ok) {
      RV.banner(null);
    }
    return { ok: response.ok, status: response.status, data: data || {} };
  } finally {
    clearTimeout(timer);
  }
};

RV.timedOut = function timedOut() {
  RV.banner("The server took too long to answer, so your last action may not have been saved. Try again.");
  return { ok: false, status: 0, data: { error: RV.TIMED_OUT } };
};
```

In `RV.next`:
- the signature becomes `RV.next = async function next(startToken) {`;
- its first lines become:

```js
  // Follow-up B1: an answer, skip or page load that started before the
  // volunteer moved on (Browse, a lane, the recent strip) passes the token it
  // started with. If they have navigated since, their newer choice stands.
  if (startToken !== undefined && startToken !== RV.navToken) return;
  const token = ++RV.navToken;
```

Add one line to the comment block above `RV.navToken`: "A call made on behalf of an older action passes that action's token and gives way (follow-up B1)."

In the `DOMContentLoaded` handler:
- insert `const landing = RV.navToken;   // follow-up B1: a click made while loading wins` as its first statement, before the nav-button loop;
- change the final `await RV.next();` to `await RV.next(landing);`.

- [ ] **Step 5: Implement `review-single.js`.** `loadNext`:

```js
  async function loadNext(startToken) {
    // Follow-up B1: nothing to do -- and nothing to overwrite on screen -- if
    // the volunteer moved on since the action that asked for this started.
    if (startToken !== undefined && startToken !== RV.navToken) return;
    reopened = false;
    message("Loading the next box…");
    await RV.next(startToken);
  }
```

- In `submit` and in `skip`, directly after `busy = true;`, add `const nav = RV.navToken;   // follow-up B1`.
- Change every `loadNext()` inside those two functions to `loadNext(nav)`.
- Leave `reopen`'s and the buttons' `loadNext()` calls as they are.

- [ ] **Step 6: Implement `review-frame.js` `enter`.**
  - After `busy = true;`, add `const nav = RV.navToken;`.
  - Inside the `try`, between the two release lines and the `/open` POST:

```js
      // Follow-up B4: a /next the volunteer superseded may still be out, and
      // gives its box back only once it lands. Wait for that before claiming
      // this frame, or the late release could take back a box the frame has
      // just claimed and cost an answer redone. Then give up if they have
      // navigated again meanwhile.
      await RV.nextTurn;
      if (nav !== RV.navToken) return;
```

- [ ] **Step 7: Run the tests.**
  - Run: `… -m pytest tests/test_review_page.py -q; echo exit=$?`
  - Run `node --check` on the three edited scripts.
  - Expected: all dots, `exit=0`.

- [ ] **Step 8: Commit** — "review page: navigation waits its turn and gives way, and a stalled request times out"

```bash
git add services/dashboard/app/static/review-core.js services/dashboard/app/static/review-single.js services/dashboard/app/static/review-frame.js tests/test_review_page.py
git commit -F .superpowers/sdd/commit-msg.txt
```

---

### Task 7: Review page views — preview clicks keep the mode, Browse waits, reopening shows your answer, admins reopen completed frames (A3, B2, B5, B6)

**Files:**
- Modify: `services/dashboard/app/static/review-single.js`: `openFrame`; new `mineText`; `render`; the returned object.
- Modify: `services/dashboard/app/static/review-browse.js`: `open`.
- Modify: `services/dashboard/app/static/review-frame.js`: `editableSelection`, `addedPanel`, `toggleDraw`, `render`'s draw button, `panelContent`; new `reopenFrame`.
- Modify: `services/dashboard/app/static/review.css`: `.rv-mine`.
- Test: `tests/test_review_page.py`.

**Interfaces:**
- Consumes:
  - Task 2's `payload.mine = {outcome, label, life_stage, not_sure, celebrity_id, bbox}`;
  - Task 6's `loadNext(startToken)` and `RV.next(startToken)`;
  - the existing `DELETE /api/review/frames/{id}/complete` (admin only).
- Produces: `RV.views.single.isBusy()`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_review_page.py`; add `"reopenFrame"` to both `FRAME_MUTATING_HANDLERS` and `FRAME_LEAVE_AWARE_ACTIONS`):

```python
from js_source import function_source


def test_a_preview_click_keeps_single_box_mode():
    body = _function_body(_read("review-single.js"), "openFrame")
    assert 'if (!eventId && RV.openFrameId === savedFrameId) RV.setMode("frame");' in body


def test_browse_cells_wait_for_a_view_that_is_still_saving():
    body = _function_body(_read("review-browse.js"), "open")
    guard = ('for (const v of ["single", "frame"]) '
             "if (RV.views[v] && RV.views[v].isBusy && RV.views[v].isBusy()) return;")
    assert guard in body and body.index(guard) < body.index("RV.show(")
    assert "isBusy: () => busy" in _read("review-single.js")


@needs_node
def test_reopening_reads_back_what_you_answered(tmp_path):
    src = _read("review-single.js")
    assert "mineText(c.mine)" in _function_body(src, "render")
    out = run_node([
        "RV = { entry: (n) => (n === 'Acanthurus coeruleus' ? { common: 'Blue Tang' } : null) };",
        function_source(src, "mineText")], """
      return [
        mineText({ outcome: "species", label: "Acanthurus coeruleus", life_stage: null,
                   not_sure: true, bbox: [1, 2, 30, 40] }),
        mineText({ outcome: "species", label: "Pomacanthus paru", life_stage: "juvenile",
                   not_sure: false, bbox: null }),
        mineText({ outcome: "no_animal" }),
        mineText({ outcome: "unidentifiable" })];
    """, tmp_path)
    assert out == ["Blue Tang, not sure; the box shows where you put it",
                   "Pomacanthus paru (juvenile)", "no animal here", "can't identify"]


def test_admins_can_edit_and_reopen_a_completed_frame():
    src = _read("review-frame.js")
    assert ('if (!selected || (state.complete && RV.role !== "admin")) return false;'
            in _function_body(src, "editableSelection"))
    assert '(state.complete && RV.role !== "admin")' in _function_body(src, "addedPanel")
    assert ('if (state.complete && RV.role !== "admin") return;'
            in _function_body(src, "toggleDraw"))
    panel = _function_body(src, "panelContent")
    assert '"Reopen frame for edits"' in panel and 'RV.role === "admin"' in panel
    reopen = _function_body(src, "reopenFrame")
    assert "RV.del(`/api/review/frames/${state.saved_frame_id}/complete`)" in reopen
    assert "await reload(true)" in reopen
```

The node test's first source assigns `RV` without `const`: nothing else declares it in that run, and sloppy-mode assignment makes it a global.

- [ ] **Step 2: Run to see them fail.**
  - Run: `… -m pytest tests/test_review_page.py -q`
  - Expected: the new tests, and the two lists' new `reopenFrame` cases, FAIL.

- [ ] **Step 3: Implement `review-single.js`.**

In `openFrame`, the last line becomes:

```js
      // Follow-up A3: only "Whole frame" / F switches the saved mode; a click
      // on another fish in the preview is a one-off look at its frame.
      if (!eventId && RV.openFrameId === savedFrameId) RV.setMode("frame");
```

Add `mineText` after `renderLocked`:

```js
  // Follow-up B5: what the volunteer answered last time, from the payload's
  // `mine`. The editor already starts from their box.
  function mineText(m) {
    if (m.outcome === "no_animal") return "no animal here";
    if (m.outcome === "unidentifiable") return "can't identify";
    const e = RV.entry(m.label, m.life_stage);
    let text = (e && e.common) || (m.life_stage ? `${m.label} (${m.life_stage})` : m.label);
    if (m.not_sure) text += ", not sure";
    if (m.bbox) text += "; the box shows where you put it";
    return text;
  }
```

In `render`, inside `if (reopened) { … }`, after the "Changing your answer" note:

```js
      if (c.mine) side.append(RV.el("p", { class: "rv-mine", text: `You answered: ${mineText(c.mine)}` }));
```

The returned object becomes `return { enter, loadNext, renderRecent, isBusy: () => busy };`.

- [ ] **Step 4: Implement `review-browse.js` `open`.** First statement:

```js
    // Follow-up B2: a single-box answer or skip, or a frame action, is still
    // saving. Opening a box now would supersede its trailing next and leave
    // "Loading the next box…" on screen.
    for (const v of ["single", "frame"]) if (RV.views[v] && RV.views[v].isBusy && RV.views[v].isBusy()) return;
```

- [ ] **Step 5: Implement `review-frame.js`.**
- **`editableSelection`:** first line `if (!selected || (state.complete && RV.role !== "admin")) return false;`.
- **`addedPanel`:** its early return becomes `if ((state.complete && RV.role !== "admin") || (a && !a.mine && RV.role !== "admin")) return out;`.
- **`toggleDraw`:** first line `if (state.complete && RV.role !== "admin") return;`.
- **`render`:** the draw button's `disabled: Boolean(state.complete)` becomes `disabled: Boolean(state.complete) && RV.role !== "admin"`.
- **Add a comment** above those edits: `// Follow-up B6: admins can edit a completed frame (spec §8/§10); volunteers can't.`
- **`panelContent`:** after the "Marked complete by" note:

```js
    if (state.complete && RV.role === "admin") {
      out.push(RV.el("button", { type: "button", onclick: reopenFrame }, "Reopen frame for edits"));
    }
```

- **Add `reopenFrame`** after `complete`:

```js
  // Follow-up B6: an admin takes the frame's completion back, so volunteers
  // can change it and mark it complete again.
  async function reopenFrame() {
    if (!state || RV.openFrameId !== state.saved_frame_id) return;
    if (busy) return;
    busy = true;
    try {
      const r = await RV.del(`/api/review/frames/${state.saved_frame_id}/complete`);
      if (!r.ok) { RV.toast(r.data.error || "Couldn't reopen the frame."); return; }
      RV.toast("Frame reopened for edits.");
      await reload(true);
    } finally {
      busy = false;
    }
  }
```

`review.css`: after the existing `.rv-note` rule, add `.rv-mine { font-weight: 600; margin: 4px 0; }`.

- [ ] **Step 6: Run the tests.**
  - Run: `… -m pytest tests/test_review_page.py tests/test_theme.py -q; echo exit=$?`
  - Run `node --check` on the three edited scripts.
  - Expected: all dots, `exit=0`.

- [ ] **Step 7: Commit** — "review page: preview clicks keep the mode, reopening shows your answer, admins can reopen a completed frame"

```bash
git add services/dashboard/app/static/review-single.js services/dashboard/app/static/review-browse.js services/dashboard/app/static/review-frame.js services/dashboard/app/static/review.css tests/test_review_page.py
git commit -F .superpowers/sdd/commit-msg.txt
```

---

### Task 8: Every time on the main dashboard in Eastern (F)

**Files:**
- Create: `services/dashboard/app/static/time-et.js`.
- Modify: `services/dashboard/app/templates/index.html`, plus every other template that loads `app.js` (`grep -l 'static/app.js' services/dashboard/app/templates/*.html`).
- Modify: `services/dashboard/app/static/app.js`: 13 `toLocale*` calls and 3 `getHours`/`getMonth`/`getDate` spots.
- Test: `tests/test_dashboard_page.py`.

**Interfaces:**
- Consumes: `tests/js_source.py` (Task 3).
- Produces, as globals:
  - `WB_TIME_ZONE`;
  - `fmtET(value, style?: "full" | "time" | "short" | "day" | "date") -> string`;
  - `etParts(value) -> {month, day, hour, minute}`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_dashboard_page.py`):

```python
import re
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parents[1] / "services" / "dashboard" / "app" / "templates"


def test_the_dashboard_formats_every_time_through_the_eastern_helpers():
    app = read("app.js")
    assert not re.search(r"toLocale(String|TimeString|DateString)\(", app)
    assert not re.search(r"\.get(Hours|Minutes|Month|Date)\(", app)
    pages = [p for p in TEMPLATES.glob("*.html") if "static/app.js" in p.read_text()]
    assert pages
    for page in pages:
        html = page.read_text()
        assert "static/time-et.js" in html, page.name
        assert html.index("static/time-et.js") < html.index("static/app.js"), page.name


@needs_node
def test_eastern_times_follow_daylight_saving(tmp_path):
    out = run_node([read("time-et.js")], """
      return {
        summer: fmtET("2026-07-01T14:40:56Z", "time"),
        winter: fmtET("2026-01-15T15:05:00Z", "time"),
        full: fmtET("2026-07-01T14:40:56Z"),
        short: fmtET("2026-07-01T14:40:56Z", "short"),
        day: fmtET("2026-07-01T03:30:00Z", "day"),
        date: fmtET("2026-07-01T03:30:00Z", "date"),
        parts: etParts("2026-07-01T03:30:00Z"),
        midnight: etParts("2026-07-01T04:05:00Z").hour,
      };
    """, tmp_path)
    # newer ICU puts a narrow no-break space before AM/PM
    out = {k: v.replace(" ", " ") if isinstance(v, str) else v for k, v in out.items()}
    assert out["summer"] == "10:40:56 AM EDT"
    assert out["winter"] == "10:05:00 AM EST"
    assert out["full"] == "7/1/2026, 10:40:56 AM EDT"
    assert out["short"] == "Jul 1, 10:40 AM EDT"
    assert out["day"] == "Jun 30"
    assert out["date"] == "6/30/2026"
    assert out["parts"] == {"month": 6, "day": 30, "hour": 23, "minute": 30}
    assert out["midnight"] == 0
```

- [ ] **Step 2: Run to see them fail.**
  - Run: `… -m pytest tests/test_dashboard_page.py -q`
  - Expected: FAIL. `time-et.js` is missing, and `toLocale*` calls remain.

- [ ] **Step 3: Create `services/dashboard/app/static/time-et.js`:**

```js
// services/dashboard/app/static/time-et.js
// Every time on the dashboard is shown in Wahoo Bay's own time, US Eastern,
// with daylight saving applied from the browser's time-zone data and marked
// EDT/EST -- whatever zone the viewer's computer is set to (follow-up spec F;
// the review page's RV.fmtTime follows the same rule). Loaded before app.js.
"use strict";

const WB_TIME_ZONE = "America/New_York";
const ET_STYLES = {
  full: { timeZoneName: "short" },                                             // 7/1/2026, 10:40:56 AM EDT
  time: { hour: "numeric", minute: "2-digit", second: "2-digit", timeZoneName: "short" },  // 10:40:56 AM EDT
  short: { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
    timeZoneName: "short" },                                                   // Jul 1, 10:40 AM EDT
  day: { month: "short", day: "numeric" },                                     // Jul 1
  date: { year: "numeric", month: "numeric", day: "numeric" },                 // 7/1/2026
};
const ET_PARTS = new Intl.DateTimeFormat("en-US", { timeZone: WB_TIME_ZONE, hourCycle: "h23",
  month: "numeric", day: "numeric", hour: "numeric", minute: "numeric" });

function fmtET(value, style) {
  const d = value instanceof Date ? value : new Date(value);
  return d.toLocaleString("en-US", { timeZone: WB_TIME_ZONE, ...ET_STYLES[style || "full"] });
}

// The Eastern calendar and clock fields of a moment, for charts that bucket
// or label by hour.
function etParts(value) {
  const d = value instanceof Date ? value : new Date(value);
  const parts = {};
  for (const p of ET_PARTS.formatToParts(d)) parts[p.type] = p.value;
  return { month: Number(parts.month), day: Number(parts.day),
    hour: Number(parts.hour) % 24, minute: Number(parts.minute) };
}
```

- [ ] **Step 4: Load it.** In each template that loads `app.js`, put `<script src="/static/time-et.js?v={{ asset_version }}" defer></script>` on the line directly before the `app.js` script tag. `defer` scripts run in document order.

- [ ] **Step 5: Replace the calls in `app.js`.** Match by content; the line numbers have moved.

| Where | Old | New |
|---|---|---|
| species-count row, "last" | `new Date(r.last_seen).toLocaleTimeString()` | `fmtET(r.last_seen, "time")` |
| `renderEventCard` meta | `new Date(r.ts).toLocaleTimeString()` | `fmtET(r.ts, "time")` |
| water readings, `when` | `new Date(latest.ts).toLocaleString()` | `fmtET(latest.ts)` |
| wind tooltip | `new Date(s.x).toLocaleString()` | `fmtET(s.x)` |
| "updated" clock | `new Date().toLocaleString()` | `fmtET(new Date())` |
| species list `last` | `new Date(s.last_seen).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })` | `fmtET(s.last_seen, "short")` |
| species profile `first` / `last` (2 calls) | `new Date(data.first_seen).toLocaleString()` and the `last_seen` one | `fmtET(data.first_seen)` / `fmtET(data.last_seen)` |
| profile frames `when` | `new Date(f.ts).toLocaleString([], { month: "short", … })` | `fmtET(f.ts, "short")` |
| ROI modal parts | `new Date(r.ts).toLocaleString()` | `fmtET(r.ts)` |
| celebrity card | `new Date(c.last_tagged_at).toLocaleDateString([], { month: "short", day: "numeric" })` | `fmtET(c.last_tagged_at, "day")` |
| celebrity detail `firstSeen` / `lastSeen` (2 calls) | `new Date(d.first_tagged_at).toLocaleDateString()` and the `last_tagged_at` one | `fmtET(d.first_tagged_at, "date")` / `fmtET(d.last_tagged_at, "date")` |
| hour-of-day chart bucket | `const h = new Date(r.hour).getHours();` | `const h = etParts(r.hour).hour;` |
| time-axis ticks | `const d = new Date(t); const lbl = hours <= 48 ? \`${d.getHours()…}:00\` : \`${d.getMonth()+1}/${d.getDate()}\`;` | `const p = etParts(t); const lbl = hours <= 48 ? \`${String(p.hour).padStart(2, "0")}:00\` : \`${p.month}/${p.day}\`;` |
| scatter `fmtT` | the body built from `getMonth`/`getDate`/`getHours`/`getMinutes` | `const p = etParts(ms); return \`${p.month}/${p.day} ${String(p.hour).padStart(2, "0")}:${String(p.minute).padStart(2, "0")}\`;` |

- Over the whole file, `grep -nE 'toLocale|get(Hours|Minutes|Month|Date)\(' services/dashboard/app/static/app.js` must print nothing.
- Update the hour-of-day chart's comment to say "Eastern hour-of-day".

- [ ] **Step 6: Run the tests.**
  - Run: `… -m pytest tests/test_dashboard_page.py tests/test_dashboard_reviewer_entry.py tests/test_auth_db.py -q; echo exit=$?`
  - Run `node --check` on `app.js` and `time-et.js`.
  - Expected: all dots, `exit=0`.

- [ ] **Step 7: Commit** — "dashboard: every time in Eastern"

```bash
git add services/dashboard/app/static/time-et.js services/dashboard/app/static/app.js services/dashboard/app/templates/index.html tests/test_dashboard_page.py
git commit -F .superpowers/sdd/commit-msg.txt
```

Add any other template changed in Step 4 to the `git add` line.

---

### Task 9: Test hardening — the box rule, browse timeouts, page-bounded frame ids (103, 210, 211)

**Files:**
- Modify: `services/dashboard/app/review_labels.py`, module docstring only.
- Test: `tests/test_review_labels.py`, `tests/test_review_browse.py`.

**Interfaces:** none new. Tests and docs only.

- [ ] **Step 1: Document the box rule.** In `review_labels.py`'s module docstring, after the "Outcomes are compared as …" paragraph:

```
The box (phase 3 crops with it): the latest corrected box among the
deciding verdicts -- the adjudication, or the first-pass and spot-check
pair -- and failing that, the latest corrected box from anyone who
answered the box. None means the model's own box. A box fix says where
the animal is, not what it is, so it outlives a later answer that changes
the label.
```

- [ ] **Step 2: Pin it** in `tests/test_review_labels.py`:

```python
def test_the_box_is_the_latest_one_the_deciding_verdicts_drew():
    r = resolve([V(1, t=0, bbox=(1, 1, 10, 10)),
                 V(2, user=2, t=5, item_pass=2, bbox=(2, 2, 20, 20))])
    assert r.status == "agreed" and r.bbox == (2, 2, 20, 20)


def test_without_a_deciding_box_the_latest_box_anyone_drew_is_used():
    r = resolve([V(1, user=1, t=0, bbox=(5, 5, 50, 50)), V(2, user=2, t=10)])
    assert r.deciding == (2,) and r.bbox == (5, 5, 50, 50)


def test_a_box_fix_outlives_a_later_change_of_label():
    r = resolve([V(1, t=0, bbox=(5, 5, 50, 50)), V(2, t=10, no_animal=True)])
    assert r.outcome == ("no_animal",) and r.bbox == (5, 5, 50, 50)


def test_an_adjudication_box_wins():
    r = resolve([V(1, t=0, bbox=(1, 1, 10, 10)),
                 V(2, user=2, t=5, item_pass=2, unid=True, bbox=(2, 2, 20, 20)),
                 V(3, user=9, t=9, adj=True, bbox=(3, 3, 30, 30))])
    assert r.status == "adjudicated" and r.bbox == (3, 3, 30, 30)


def test_an_adjudication_without_a_box_falls_back_to_the_latest_box_drawn():
    r = resolve([V(1, t=0, bbox=(1, 1, 10, 10)),
                 V(2, user=2, t=5, item_pass=2, unid=True, bbox=(2, 2, 20, 20)),
                 V(3, user=9, t=9, adj=True)])
    assert r.bbox == (2, 2, 20, 20)


def test_with_no_box_drawn_the_models_box_applies():
    assert resolve([V(1), V(2, user=2, t=5, item_pass=2)]).bbox is None
```

- [ ] **Step 3: Pin the browse and progress timeouts** in `tests/test_review_browse.py`:

```python
async def test_browse_and_progress_run_under_their_own_statement_timeout(rdb, store,
                                                                        monkeypatch):
    """Phase-1 triage 210: these timeouts are the only bound on a reviewer
    request's database time. Removing either _set_statement_timeout call,
    or ignoring STATEMENT_TIMEOUT_MS, fails this test."""
    monkeypatch.setattr(review_browse_module, "STATEMENT_TIMEOUT_MS", 100)
    real = review_browse_module._set_statement_timeout

    async def then_stall(conn):
        await real(conn)
        await conn.execute("SELECT pg_sleep(2)")     # a query that outlives the limit

    monkeypatch.setattr(review_browse_module, "_set_statement_timeout", then_stall)
    one = await make_user(rdb, "one")
    async with rdb.connection() as conn:
        with pytest.raises(psycopg.errors.QueryCanceled):
            await browse(conn, catalogue=store.get(), user=as_user(one),
                         filters=BrowseFilters(), page=0)
        # transaction-local: the limit ends with the transaction
        cur = await conn.execute("SHOW statement_timeout")
        assert (await cur.fetchone())[0] != "100ms"
    async with rdb.connection() as conn:
        with pytest.raises(psycopg.errors.QueryCanceled):
            await progress_summary(conn, pool_obj=None, catalogue=store.get(), session_id=None,
                                   target=5)
```

- [ ] **Step 4: Pin the page-bounded frame ids:**

```python
async def test_browse_points_each_box_at_the_frame_saved_at_its_own_time(rdb, tmp_path, store):
    """Phase-1 triage 211: a wrong saved_frame_id sends a volunteer into the
    wrong frame and claims its boxes. Assert the values, not the count."""
    one = await make_user(rdb, "one")
    box = [{"bbox": [10, 10, 90, 90], "species": "Seriola zonata"}]
    f = await seed_frame(rdb, tmp_path, ts=T0, frame_id=7, boxes=box)
    # frame_id restarts with every worker run: the same camera and frame_id
    # an hour later is another frame
    g = await seed_frame(rdb, tmp_path, ts=T0 + timedelta(hours=1), frame_id=7, boxes=box,
                         color=(200, 60, 90))
    # and f's frame saved a second time, under another reason: the lower id wins
    async with rdb.connection() as conn:
        await conn.execute(
            """INSERT INTO saved_frames (ts, frame_id, source_name, reason, image_path, num_fish)
               VALUES (%s, 7, %s, 'interesting:new_species', %s, 1)""",
            (T0, f["source"], f["image_path"]))
    for frame in (f, g):
        await add_verdict(rdb, frame["event_ids"][0], one, name="Seriola zonata")
    async with rdb.connection() as conn:
        result = await browse(conn, catalogue=store.get(), user=as_user(one),
                              filters=BrowseFilters(), page=0)
    by_event = {c["event_id"]: c["saved_frame_id"] for c in result["cells"]
                if c["kind"] == "box"}
    assert by_event == {f["event_ids"][0]: f["saved_frame_id"],
                        g["event_ids"][0]: g["saved_frame_id"]}
```

- [ ] **Step 5: Run the tests.**
  - Run: `… -m pytest tests/test_review_labels.py tests/test_review_browse.py -q; echo exit=$?`
  - Expected: all dots, `exit=0`. These tests pin behaviour that already exists, so they pass at once. Check that each one can fail:
    - comment out the `_set_statement_timeout` call in `browse()` and watch the timeout test FAIL;
    - in the browse lookup, swap `ORDER BY id` for `ORDER BY id DESC` and watch the frame-id test FAIL;
    - revert both.

- [ ] **Step 6: Commit** — "review tests: the box rule, browse timeouts and page-bounded frame ids pinned"

```bash
git add services/dashboard/app/review_labels.py tests/test_review_labels.py tests/test_review_browse.py
git commit -F .superpowers/sdd/commit-msg.txt
```

---

## After the last task (controller)

1. Run the full suite once:
   `TEST_DATABASE_URL_FILE=.superpowers/test_dsn timeout 1800 /raid/scratch/dzimmerman2021/miniconda3/envs/wahoobay/bin/python -m pytest -q; echo exit=$?`
2. Final whole-branch review, then one fix wave.
3. Deploy, with the owner's go:
   - fast-forward `reports-panel-and-weather` to `review-tool`;
   - restart the dashboard (a few seconds; there is no migration).
4. Remap:
   - run `scripts/dev/remap_legacy_corrections.py --env .env` (dry run) from the main checkout and show the owner the output;
   - run `--apply` only after they say so.

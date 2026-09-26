# Frame-first review — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make whole-frame review the default at `/review`. The queue still picks a box, and the page opens that box's frame with it selected. Add "Next frame", release claims when a volunteer leaves a frame, and show per-box progress in the frame view.

**Architecture:**
- **Server:** three small changes in `review_frames.py` and `review_queue.py`: claim refresh on open, frame release and frame skip, and progress fields in `frame_state`. Plus two routes.
- **Page:** one shared router, `RV.next()` in `review-core.js`, asks `/api/review/next` once and sends the box to the right view. Both views stop fetching the queue themselves.
- **Unchanged:** the queue, the verdict paths, the completion rules and the spot-check blindness rules.

**Tech Stack:** FastAPI + psycopg 3 (async) + PostgreSQL 18; classic browser scripts sharing a global `RV`; pytest with the `wahoobay_test` database; `node --check` for the JavaScript.

**Spec:** `docs/superpowers/specs/2026-09-25-review-frame-first-design.md`, which amends `docs/superpowers/specs/2026-09-24-reviewer-annotation-dataset-design.md`.

## Global Constraints

- **Work only in the worktree** `/raid/scratch/dzimmerman2021/wahoobay/.claude/worktrees/review-tool`, branch `review-tool`. Never touch the main checkout, the live `wahoobay` database, the running dashboard, the workers or the tunnel.
- **No new Python or JavaScript dependencies.**
- **Python style:**
  - `from __future__ import annotations` first in every new module.
  - ruff line-length 100, rules `E,F,W,I,B,UP`.
  - Run `/raid/scratch/dzimmerman2021/miniconda3/envs/wahoobay/bin/ruff check <files>` before every commit.
- **Run pytest as the direct child of `timeout`,** against the test database:
  `TEST_DATABASE_URL_FILE=.superpowers/test_dsn timeout 900 /raid/scratch/dzimmerman2021/miniconda3/envs/wahoobay/bin/python -m pytest <files> -q; echo exit=$?`
  pyproject already adds `-q`, so there is no summary line. Evidence is the dots, any `F`/`E` characters, and `exit=`.
- **JavaScript:**
  - classic scripts sharing a global `RV`;
  - never `innerHTML` with data;
  - `/raid/scratch/dzimmerman2021/miniconda3/envs/node/bin/node --check <file>` must pass.
- **The busy-guard pattern.** Every JS function that sends a mutating request starts with `if (busy) return;`, sets `busy = true;` and resets `busy = false;` in a `finally` block.
- **Every mutating route** takes `Depends(require_csrf)` (header `X-Requested-With: wahoobay`) and the `reviewer` dependency. Admins get no exception from spot-check blindness.
- **Transactions.** Pooled connections may arrive with `autocommit=True`. Any multi-statement write runs inside `async with conn.transaction():`.
- **A spot-check (pass 2) never shows the frame view.** Its payload never carries the model's guess, the hints, the species it counts toward, or the earlier verdict.
- **Git:**
  - Stage explicit paths only; never `git add -A`, never bare `git stash`.
  - Never stage `assets/celebrities.json` or `assets/species.xlsx`.
  - The harness refuses `$(...)` and multi-line `-m`. Write each commit message to `.superpowers/sdd/commit-msg.txt` and use `git commit -F .superpowers/sdd/commit-msg.txt`. The message is the quoted subject shown after `#` in the task's commit step, a blank line, then the trailer.
  - End each message with the `Co-Authored-By:` line your own environment specifies.

## Review Focus

1. **A served box whose frame won't open** (missing on disk: 404). The page must show that box on its own, or the error, and must never ask the queue again from that failure path, or it would loop. Covered by Task 3's static check.
2. **"Next frame" or "Mark frame complete" calling `RV.next()` while their own busy flag is still set.** The frame view's `enter()` returns early when busy, so the next frame would never load. Covered by Task 3 (complete) and Task 4 (next frame).
3. **Switching lanes while holding a frame.** `/next` can run before the release lands and hand the just-left frame back as "held". The lane buttons must await the release first. Covered by Task 3.
4. **Blocked browser storage** (private windows, strict settings). The mode helpers must not throw, and must fall back to whole frame. Covered by Task 3.
5. **A volunteer working one busy frame for longer than the claim window.** Each answer reloads the frame; the reload must refresh the claims, not just read. Covered by Task 1 (server refresh) and Task 3 (reload uses `open`).

---

### Task 1: Frame claims on the server — refresh, release, skip

**Files:**
- Modify: `services/dashboard/app/review_frames.py` (`open_frame`; new `release_frame`, `skip_frame`)
- Modify: `services/dashboard/app/review_queue.py` (`next_item`'s held-item read)
- Modify: `services/dashboard/app/routes_review.py` (import block; two routes after `frame_complete`)
- Test: `tests/test_review_frames.py`, `tests/test_review_queue.py`

**Interfaces:**
- Produces:
  - `release_frame(conn, *, saved_frame_id: int, user_id: int) -> None`
  - `skip_frame(conn, *, saved_frame_id: int, user_id: int) -> None`
  - `POST /api/review/frames/{saved_frame_id}/release` → `{"ok": true}`
  - `POST /api/review/frames/{saved_frame_id}/skip` → `{"ok": true}`
  - `open_frame` now also refreshes the caller's own live claims.
  - `next_item` refreshes `claimed_until` when it returns a held item.

- [ ] **Step 1: Write the failing server tests**

In `tests/test_review_frames.py`, extend the imports:

```python
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
```

Append these tests at the end of the file. `_status` is already defined in this module and returns `(status, claimed_by)`.

```python
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
    rclient.post(f"/api/review/frames/{sf}/open", headers=CSRF)
    assert await _status(rdb, item) == ("claimed", me)
    assert rclient.post(f"/api/review/frames/{sf}/release", headers=CSRF).json() == {"ok": True}
    assert await _status(rdb, item) == ("open", None)
    rclient.post(f"/api/review/frames/{sf}/open", headers=CSRF)
    assert rclient.post(f"/api/review/frames/{sf}/skip", headers=CSRF).json() == {"ok": True}
    assert await _status(rdb, item) == ("open", None)
    reopened = rclient.post(f"/api/review/frames/{sf}/open", headers=CSRF).json()
    assert reopened["boxes"][0]["review_item_id"] is None      # skipped: not claimed again
```

In `tests/test_review_queue.py`, add after `test_asking_again_returns_the_box_you_already_hold`:

```python
async def test_asking_again_refreshes_the_claim_on_the_box_you_hold(rdb, tmp_path):
    """Final review Minor 5: a reload at minute 9 used to leave one minute
    on the claim."""
    [(_f, item)] = await _boxes(rdb, tmp_path, n=1)
    one = await make_user(rdb, "one")
    async with rdb.connection() as conn:
        await conn.execute(
            """UPDATE review_items SET status = 'claimed', claimed_by = %s,
                      claimed_until = now() + interval '30 seconds' WHERE id = %s""", (one, item))
    got = await _next(rdb, one)
    assert got["id"] == item
    left = (await _row(rdb, "SELECT claimed_until - now() FROM review_items WHERE id = %s",
                       item))[0]
    assert left > timedelta(minutes=5)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `TEST_DATABASE_URL_FILE=.superpowers/test_dsn timeout 900 /raid/scratch/dzimmerman2021/miniconda3/envs/wahoobay/bin/python -m pytest tests/test_review_frames.py tests/test_review_queue.py -q; echo exit=$?`
Expected: `ImportError: cannot import name 'release_frame'` at collection, so `test_review_frames.py` errors. Once the functions exist, `test_opening_keeps_and_refreshes…` and `test_asking_again_refreshes…` fail on their expiry assertions.

- [ ] **Step 3: Implement in `review_frames.py`**

Replace `open_frame` with:

```python
async def open_frame(conn, *, saved_frame_id: int, user_id: int, claim_s: int) -> None:
    """Claim the frame's open (or lapsed) first-pass boxes for this
    volunteer, and refresh the claims they already hold here -- including
    the box the queue just served them, which the page no longer releases
    before opening its frame (spec amendment 2026-09-25). Boxes the
    volunteer skipped stay unclaimed; someone else's live claim is left
    alone."""
    async with conn.transaction(), conn.cursor() as cur:
        await refuse_during_second_opinion(conn, saved_frame_id=saved_frame_id, user_id=user_id)
        await cur.execute("""
            UPDATE review_items
               SET status = 'claimed', claimed_by = %(uid)s,
                   claimed_until = now() + make_interval(secs => %(claim_s)s)
             WHERE saved_frame_id = %(sf)s AND pass = 1
               AND (status = 'open'
                    OR (status = 'claimed' AND claimed_until < now())
                    OR (status = 'claimed' AND claimed_by = %(uid)s))
               AND NOT EXISTS (SELECT 1 FROM review_skips s
                                WHERE s.item_id = review_items.id AND s.user_id = %(uid)s)""",
                          {"uid": user_id, "claim_s": claim_s, "sf": saved_frame_id})


async def release_frame(conn, *, saved_frame_id: int, user_id: int) -> None:
    """Give back every first-pass box this volunteer holds in the frame,
    skipping none: leaving the frame view any way but "Next frame" (spec
    amendment 2026-09-25, flow step 5). Mirrors review_queue.release_item,
    including a claim the sweep has already moved back to 'open' (the
    sweep keeps claimed_by). Other volunteers' claims, answered boxes and
    spot-checks are untouched."""
    async with conn.transaction(), conn.cursor() as cur:
        await cur.execute("""
            UPDATE review_items SET status = 'open', claimed_by = NULL, claimed_until = NULL
             WHERE saved_frame_id = %s AND pass = 1 AND claimed_by = %s
               AND status IN ('claimed', 'open')""", (saved_frame_id, user_id))


async def skip_frame(conn, *, saved_frame_id: int, user_id: int) -> None:
    """"Next frame" (spec amendment 2026-09-25, flow step 3): every
    first-pass box in the frame still waiting for an answer is skipped for
    this volunteer, so neither the queue nor open_frame hands it back to
    them, and their own claims among those boxes are released at once.
    A box someone else holds is skipped for this volunteer too but stays
    the holder's. Spot-checks are never touched."""
    params = {"uid": user_id, "sf": saved_frame_id}
    async with conn.transaction(), conn.cursor() as cur:
        await cur.execute("""
            INSERT INTO review_skips (item_id, user_id)
            SELECT id, %(uid)s FROM review_items
             WHERE saved_frame_id = %(sf)s AND pass = 1 AND status IN ('open', 'claimed')
            ON CONFLICT DO NOTHING""", params)
        await cur.execute("""
            UPDATE review_items SET status = 'open', claimed_by = NULL, claimed_until = NULL
             WHERE saved_frame_id = %(sf)s AND pass = 1 AND claimed_by = %(uid)s
               AND status IN ('claimed', 'open')""", params)
```

- [ ] **Step 4: Implement in `review_queue.py`**

In `next_item`, replace the `held = await fetch_one(conn, """SELECT * FROM review_items …""", params)` read with an update that refreshes the claim it returns:

```python
        # Handing back a box the volunteer already holds refreshes its
        # claim like a fresh one (final review Minor 5): a reload late in
        # the claim window would otherwise leave them only seconds.
        held = await fetch_one(conn, """
            UPDATE review_items
               SET claimed_until = now() + make_interval(secs => %(claim_s)s)
             WHERE id = (SELECT id FROM review_items
                          WHERE status = 'claimed' AND claimed_by = %(uid)s
                            AND (lane = %(lane)s OR pass = 2)
                          ORDER BY id LIMIT 1)
            RETURNING *""", params)
```

- [ ] **Step 5: Add the routes in `routes_review.py`**

Add `release_frame` and `skip_frame` to the `from .review_frames import (...)` block, keeping it alphabetical. Then add these routes directly after `frame_complete`:

```python
    @router.post("/api/review/frames/{saved_frame_id}/release",
                 dependencies=[Depends(require_csrf)])
    async def frame_release(saved_frame_id: int, user=Depends(reviewer)):
        async with _db().connection() as conn:
            await release_frame(conn, saved_frame_id=saved_frame_id, user_id=user.id)
        return JSONResponse({"ok": True})

    @router.post("/api/review/frames/{saved_frame_id}/skip",
                 dependencies=[Depends(require_csrf)])
    async def frame_skip(saved_frame_id: int, user=Depends(reviewer)):
        async with _db().connection() as conn:
            await skip_frame(conn, saved_frame_id=saved_frame_id, user_id=user.id)
        return JSONResponse({"ok": True})
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `TEST_DATABASE_URL_FILE=.superpowers/test_dsn timeout 900 /raid/scratch/dzimmerman2021/miniconda3/envs/wahoobay/bin/python -m pytest tests/test_review_frames.py tests/test_review_queue.py -q; echo exit=$?`
Expected: all dots, `exit=0`. Then run the whole `tests` directory once (307 before this task), then `ruff check` on the four touched Python files.

- [ ] **Step 7: Commit**

```bash
git add services/dashboard/app/review_frames.py services/dashboard/app/review_queue.py services/dashboard/app/routes_review.py tests/test_review_frames.py tests/test_review_queue.py
git commit -F .superpowers/sdd/commit-msg.txt   # "review: frame claims -- refresh on open, release, skip for Next frame"
```

---

### Task 2: Per-box progress in the frame state

**Files:**
- Modify: `services/dashboard/app/review_frames.py` (`frame_state`)
- Modify: `services/dashboard/app/routes_review.py` (`frame_open`, `frame_get`)
- Test: `tests/test_review_frames.py`

**Interfaces:**
- Consumes:
  - `build_payload(conn, item, *, catalogue, counts, target)` (review_queue), whose `counts_toward` is `{"name", "common", "count", "target"}`.
  - The route helpers `_counts` and `cfg.review_target`.
- Produces:
  - `frame_state(conn, *, saved_frame_id, user, catalogue, counts=None, target=None)`.
  - Every box dict gains `"counts_toward"`. It is `None` except on `needs_answer` boxes with a catalogue guess, and only when `counts` is given.
  - Task 4 reads `box.counts_toward`.

- [ ] **Step 1: Write the failing tests**

Add `build_payload` and `fetch_one` to the `from app.review_queue import (...)` block in `tests/test_review_frames.py`, then append:

```python
async def test_each_open_box_carries_the_progress_of_the_species_it_counts_toward(rdb, tmp_path,
                                                                                    cat):
    f = await seed_frame(rdb, tmp_path, boxes=BOXES)
    me, other = await make_user(rdb, "me"), await make_user(rdb, "other")
    e = f["event_ids"]
    await add_verdict(rdb, e[0], other, name="Seriola zonata")                    # answered
    first = await add_verdict(rdb, e[1], other, name="Pomatomus saltatrix")
    await add_item(rdb, f, 1, pass_=2, exclude_user_id=other, first_correction_id=first)  # hidden
    item = await add_item(rdb, f, 3, predicted="Seriola zonata", route="Seriola zonata")

    def counts(name):
        return {"Seriola zonata": 7}.get(name, 0)

    async with rdb.connection() as conn:
        state = await frame_state(conn, saved_frame_id=f["saved_frame_id"], user=as_user(me),
                                  catalogue=cat, counts=counts, target=200)
        row = await fetch_one(conn, "SELECT * FROM review_items WHERE id = %s", (item,))
        single = await build_payload(conn, row, catalogue=cat, counts=counts, target=200)
        bare = await frame_state(conn, saved_frame_id=f["saved_frame_id"], user=as_user(me),
                                 catalogue=cat)
    by_n = {b["n"]: b for b in state["boxes"]}
    assert by_n[4]["counts_toward"] == {"name": "Seriola zonata",
                                        "common": cat.common_name("Seriola zonata"),
                                        "count": 7, "target": 200}
    assert by_n[4]["counts_toward"] == single["counts_toward"]   # the same numbers as single view
    assert by_n[1]["counts_toward"] is None          # answered
    assert by_n[2]["counts_toward"] is None          # answer hidden behind a spot-check
    assert by_n[3]["counts_toward"] is None          # the model made no guess
    assert all(b["counts_toward"] is None for b in bare["boxes"])   # no counts given


async def test_the_frame_routes_send_the_progress(rdb, rclient, tmp_path):
    await make_user(rdb, "rev")
    login(rclient, "rev")
    f = await seed_frame(rdb, tmp_path, boxes=BOXES[:1])
    sf = f["saved_frame_id"]
    for r in (rclient.post(f"/api/review/frames/{sf}/open", headers=CSRF),
              rclient.get(f"/api/review/frames/{sf}")):
        ct = r.json()["boxes"][0]["counts_toward"]
        assert ct["name"] == "Seriola zonata" and isinstance(ct["count"], int)
        assert ct["target"] > 0
```

- [ ] **Step 2: Run them to verify they fail**

Run: `TEST_DATABASE_URL_FILE=.superpowers/test_dsn timeout 900 /raid/scratch/dzimmerman2021/miniconda3/envs/wahoobay/bin/python -m pytest tests/test_review_frames.py -q -k "progress"; echo exit=$?`
Expected: `TypeError: frame_state() got an unexpected keyword argument 'counts'`, and `KeyError: 'counts_toward'` in the route test.

- [ ] **Step 3: Implement**

In `review_frames.py`:
- Change the signature to `async def frame_state(conn, *, saved_frame_id: int, user, catalogue, counts=None, target: int | None = None) -> dict:`.
- Add `"counts_toward": None` to the `box = {...}` literal, after `"hints": None`.
- In the `elif state == "needs_answer":` branch, extend the `if guess:` block:

```python
            if guess:
                box["model"] = {"name": guess, "p": e["best_accuracy"],
                                "common": catalogue.common_name(guess)}
                if counts is not None:
                    # the same numbers the single-box payload shows (build_payload)
                    box["counts_toward"] = {"name": guess, "common": catalogue.common_name(guess),
                                            "count": counts(guess), "target": target}
```

In `routes_review.py`, pass the numbers from both frame routes:
- in `frame_open`: `await frame_state(conn, saved_frame_id=saved_frame_id, user=user, catalogue=_catalogue(), counts=_counts, target=cfg.review_target)`;
- in `frame_get`: `return await _frame_call(frame_state, saved_frame_id=saved_frame_id, user=user, catalogue=_catalogue(), counts=_counts, target=cfg.review_target)`.

`complete_frame` keeps calling `frame_state` without counts.

- [ ] **Step 4: Run the tests to verify they pass**

Run the Step 2 command without `-k`, then the whole `tests` directory once, then `ruff check` on the touched Python files. Expected: all dots, `exit=0`.

- [ ] **Step 5: Commit**

```bash
git add services/dashboard/app/review_frames.py services/dashboard/app/routes_review.py tests/test_review_frames.py
git commit -F .superpowers/sdd/commit-msg.txt   # "review: the frame view gets each box's species progress"
```

---

### Task 3: One router for the queue, the mode preference, and frame-first landing

**Files:**
- Modify: `services/dashboard/app/static/review-core.js` (the `RV` object; everything from `RV.releaseHeld` to the end of the file)
- Modify: `services/dashboard/app/static/review-single.js` (`enter`, `loadNext`, `submit`, `skip`, `openFrame`)
- Modify: `services/dashboard/app/static/review-frame.js` (`enter`, new `selectBox`, `reload`, new `toSingle`, `complete`, `render`'s top bar, the F key)
- Test: `tests/test_review_page.py`

**Interfaces:**
- Consumes (Task 1): `open` refreshes claims and `POST /api/review/frames/{id}/release`.
- Produces:
  - `RV.held` (`{itemId, savedFrameId}` or `null`) replaces `RV.heldItemId`.
  - `RV.openFrameId`.
  - `RV.releaseHeld(keepFrameId?)` and `RV.releaseFrame()` each return a promise or `undefined`.
  - `async RV.releaseAll()`.
  - `RV.mode()` returns `"frame" | "single"`; `RV.setMode(value)`.
  - `async RV.next()`.
  - `RV.views.frame.enter({savedFrameId, selectEventId?, payload?})`.
  - `RV.views.single.enter({payload} | {result} | {reopenEventId} | {})`.

- [ ] **Step 1: Write the failing static checks**

Append to `tests/test_review_page.py`:

```python
# ---- spec amendment 2026-09-25: frame-first review -----------------------------

def _read(name: str) -> str:
    return (STATIC / name).read_text()


def test_the_queue_is_asked_in_one_place_and_spot_checks_stay_single():
    for name in ("review-single.js", "review-frame.js", "review-browse.js"):
        assert "/api/review/next" not in _read(name), (
            f"{name} must go through RV.next(), not call /api/review/next itself")
    body = _function_body(_read("review-core.js"), "next")
    assert re.search(r"\.item\.pass\s*!==\s*2", body), "RV.next() must send pass 2 to single view"
    assert 'RV.mode() === "frame"' in body, "RV.next() must honour the chosen mode"


def test_held_item_id_is_gone():
    for name in SCRIPTS:
        assert "heldItemId" not in _read(name), f"{name} still uses RV.heldItemId"


def test_the_mode_preference_survives_blocked_storage():
    core = _read("review-core.js")
    for name in ("mode", "setMode"):
        body = _function_body(core, name)
        assert "localStorage" in body and "try" in body and "catch" in body, (
            f"RV.{name}() must guard every storage access")
    assert 'return "frame"' in _function_body(core, "mode"), "whole frame is the fallback"


def test_opening_a_frame_keeps_the_box_the_queue_just_served():
    assert re.search(r"RV\.releaseHeld\(\s*id\s*\)", _function_body(_read("review-frame.js"),
                                                                      "enter"))
    assert "held.savedFrameId === keepFrameId" in _function_body(_read("review-core.js"),
                                                                   "releaseHeld")


def test_a_frame_that_will_not_open_does_not_ask_the_queue_again():
    body = _function_body(_read("review-frame.js"), "enter")
    at = body.index("if (!r.ok)")
    failure = _braces_from(body, body.index("{", at))
    assert "RV.next(" not in failure and "arg.payload" in failure


def test_reloading_a_frame_refreshes_the_claims():
    body = _function_body(_read("review-frame.js"), "reload")
    assert "RV.post(`/api/review/frames/${state.saved_frame_id}/open`)" in body


def test_completing_a_frame_asks_for_the_next_one_outside_the_busy_guard():
    body = _function_body(_read("review-frame.js"), "complete")
    fin = body.index("finally")
    assert "RV.next()" not in body[:fin]
    tail = body[fin:]
    assert "RV.next()" in tail[tail.index("}") + 1:], (
        "complete() must call RV.next() after busy is reset, or enter() refuses it")


def test_leaving_the_frame_view_releases_its_boxes():
    body = _function_body(_read("review-core.js"), "show")
    assert re.search(r'if \(view !== "frame"\) RV\.releaseFrame\(\)', body)


def test_switching_lanes_releases_before_asking_the_queue():
    core = _read("review-core.js")
    marker = 'document.addEventListener("DOMContentLoaded"'
    block = _braces_from(core, core.index("{", core.index(marker)))
    assert "await RV.releaseAll()" in block and "RV.next()" in block


def test_closing_the_tab_releases_the_frame_too():
    core = _read("review-core.js")
    marker = 'window.addEventListener("pagehide"'
    block = _braces_from(core, core.index("{", core.index(marker)))
    assert "keepalive: true" in block and "RV.openFrameId" in block and "RV.held" in block
```

- [ ] **Step 2: Run them to verify they fail**

Run: `TEST_DATABASE_URL_FILE=.superpowers/test_dsn timeout 900 /raid/scratch/dzimmerman2021/miniconda3/envs/wahoobay/bin/python -m pytest tests/test_review_page.py -q; echo exit=$?`
Expected: the ten new tests fail (`AssertionError: no function named 'next' found`, `heldItemId` still present, and so on). The existing checks still pass.

- [ ] **Step 3: `review-core.js`**

In the `RV` object literal, replace `heldItemId: null,` with:

```js
  held: null,          // the single box being answered: {itemId, savedFrameId}
  openFrameId: null,   // the frame whose boxes this volunteer holds, if any
```

Replace everything from `RV.releaseHeld = function releaseHeld() {` to the end of the file with the code below. `RV.refreshMine` is carried over unchanged.

```js
// ---- claims: the single box held, and the frame held --------------------------
// Spec amendment 2026-09-25. A box the queue served is recorded with its
// frame, so opening that same frame keeps the claim instead of releasing it
// and re-claiming it a moment later -- a gap in which someone else could
// take it. Leaving a frame any way but "Next frame" gives its boxes back
// without skipping them.

RV.releaseHeld = function releaseHeld(keepFrameId) {
  const held = RV.held;
  if (!held || (keepFrameId && held.savedFrameId === keepFrameId)) return undefined;
  RV.held = null;
  return RV.post(`/api/review/items/${held.itemId}/release`);
};

RV.releaseFrame = function releaseFrame() {
  const id = RV.openFrameId;
  RV.openFrameId = null;
  return id ? RV.post(`/api/review/frames/${id}/release`) : undefined;
};

RV.releaseAll = async function releaseAll() {
  await Promise.all([RV.releaseHeld(), RV.releaseFrame()]);
};

// ---- the mode: whole frame (the default) or single boxes ------------------------
// Remembered in this browser. Storage can be blocked (private windows,
// strict settings), so every access is guarded and any failure means the
// default.

RV.MODE_KEY = "rv.mode";

RV.mode = function mode() {
  try {
    if (window.localStorage.getItem(RV.MODE_KEY) === "single") return "single";
  } catch (err) {
    /* storage blocked: fall through to the default */
  }
  return "frame";
};

RV.setMode = function setMode(value) {
  try {
    window.localStorage.setItem(RV.MODE_KEY, value);
  } catch (err) {
    /* storage blocked: the default applies next time */
  }
};

// ---- the next piece of work ------------------------------------------------------
// The one place /api/review/next is called, so a box is never claimed
// twice. A spot-check (item.pass 2; its item.lane is null) always goes to
// the blind single-box view; any other box opens its whole frame with the
// box selected, unless the volunteer chose single boxes.

RV.next = async function next() {
  const r = await RV.get(`/api/review/next?lane=${encodeURIComponent(RV.lane)}`);
  if (!r.ok || !r.data.item) {
    RV.held = null;
    await RV.show("single", { result: r });
    return;
  }
  const payload = r.data;
  RV.held = { itemId: payload.item.id, savedFrameId: payload.box.saved_frame_id };
  if (payload.item.pass !== 2 && RV.views.frame && RV.mode() === "frame") {
    await RV.show("frame", { savedFrameId: payload.box.saved_frame_id,
      selectEventId: payload.item.event_id, payload });
  } else {
    await RV.show("single", { payload });
  }
};

RV.show = async function show(view, arg) {
  if (!RV.views[view]) return;
  if (view !== "frame") RV.releaseFrame();
  for (const id of ["single", "frame", "browse"]) {
    const section = document.getElementById("rv-" + id);
    if (section) section.hidden = id !== view;
  }
  RV.view = view;
  for (const b of document.querySelectorAll("#rv-nav button")) {
    const on = b.dataset.view ? b.dataset.view === view
      : view !== "browse" && b.dataset.lane === RV.lane;
    b.classList.toggle("on", on);
    b.setAttribute("aria-pressed", on ? "true" : "false");
  }
  document.getElementById("rv-recent").hidden = view !== "single";
  await RV.views[view].enter(arg || {});
};

RV.refreshMine = async function refreshMine() {
  const r = await RV.get("/api/review/mine/recent");
  if (!r.ok) return;
  RV.mine = r.data;
  document.getElementById("rv-session-count").textContent = String(r.data.session_count || 0);
  if (RV.views.single && RV.views.single.renderRecent) RV.views.single.renderRecent();
};

window.addEventListener("pagehide", () => {
  const urls = [];
  if (RV.held) urls.push(`/api/review/items/${RV.held.itemId}/release`);
  if (RV.openFrameId) urls.push(`/api/review/frames/${RV.openFrameId}/release`);
  for (const url of urls) {
    fetch(url, { method: "POST", keepalive: true, credentials: "same-origin",
      headers: RV.HEADERS, body: "{}" });
  }
});

document.addEventListener("DOMContentLoaded", async () => {
  for (const b of document.querySelectorAll("#rv-nav button")) {
    const view = b.dataset.view || "single";
    b.hidden = !RV.views[view];
    b.addEventListener("click", async () => {
      // Released -- and awaited -- before the queue is asked again, or
      // /next could hand back a box from the frame just left as "held".
      await RV.releaseAll();
      if (b.dataset.lane) {
        RV.lane = b.dataset.lane;
        await RV.next();
      } else {
        await RV.show(view);
      }
    });
  }
  if (!(await RV.loadCatalogue())) {
    RV.banner("Couldn't load the species list. Reload the page to try again.");
    return;
  }
  await RV.refreshMine();
  await RV.next();
});
```

- [ ] **Step 4: `review-single.js`**

Replace `enter` and `loadNext` with:

```js
  async function enter(arg) {
    if (arg.reopenEventId) { await reopen(arg.reopenEventId); return; }
    if (arg.payload) { showPayload(arg.payload); return; }
    if (arg.result) { showResult(arg.result); return; }
    await loadNext();
  }

  function showPayload(payload) {
    reopened = false;
    current = payload;
    render();
  }

  function showResult(r) {
    current = null;
    if (!r.ok) { message(r.data.error || "Couldn't load a box."); return; }
    renderEmpty(r.data.pool);
  }

  // Everything that moves on goes through the shared router, which asks
  // the queue once and picks the view (spec amendment 2026-09-25).
  async function loadNext() {
    reopened = false;
    message("Loading the next box…");
    await RV.next();
  }
```

In `submit()` and `skip()`, change every `RV.heldItemId = null;` to `RV.held = null;`. Replace `openFrame` with:

```js
  function openFrame(eventId) {
    if (frameAllowed(current)) {
      RV.setMode("frame");
      RV.show("frame", { savedFrameId: current.box.saved_frame_id,
        selectEventId: eventId || current.item.event_id });
    }
  }
```

Keep the `onclick: () => openFrame()` button and the `k === "f" && frameAllowed(current)` key as they are. The existing `test_whole_frame_is_not_offered_on_a_spot_check` pins them.

- [ ] **Step 5: `review-frame.js`**

Replace `enter` and `reload`, and add `selectBox` and `toSingle` next to them:

```js
  async function enter(arg) {
    const id = arg.savedFrameId || (state && state.saved_frame_id);
    if (!id) { await RV.next(); return; }
    if (busy) return;
    busy = true;
    try {
      // Keep the claim on a box held from this same frame (the one the
      // queue just served, or the single box the volunteer was on); a box
      // held from any other frame is released as before (spec amendment
      // 2026-09-25).
      RV.releaseHeld(id);
      if (RV.openFrameId && RV.openFrameId !== id) RV.releaseFrame();
      const r = await RV.post(`/api/review/frames/${id}/open`);
      if (!r.ok) {
        RV.toast(r.data.error || "Couldn't open that frame.");
        // Never ask the queue again from here: a frame that can't be
        // opened would be served straight back. Show the served box on
        // its own instead, or the error.
        RV.show("single", arg.payload ? { payload: arg.payload } : { result: r });
        return;
      }
      state = r.data;
      RV.held = null;            // the frame's claims now include it
      RV.openFrameId = id;
      drawing = false;
      if (!(arg.selectEventId && selectBox(arg.selectEventId))) selectFirst();
      render();
    } finally {
      busy = false;
    }
  }

  function selectBox(eventId) {
    const b = modelBox(eventId);
    if (!b) return false;
    selected = { kind: "model", id: b.event_id, bbox: b.bbox.slice(), original: b.bbox.slice() };
    hintName = null;
    notSure = false;
    return true;
  }

  // Re-opening (not a plain GET) refreshes this volunteer's claims on the
  // frame each time an answer reloads it, so they don't lapse while
  // someone works through a busy frame (spec amendment 2026-09-25).
  async function reload(keepSelection) {
    const r = await RV.post(`/api/review/frames/${state.saved_frame_id}/open`);
    if (!r.ok) { RV.toast(r.data.error || "Couldn't refresh the frame."); return; }
    state = r.data;
    if (!keepSelection) selectFirst();
    render();
  }

  // F / "Single boxes": switch modes for good (remembered in this browser),
  // give this frame's boxes back without skipping them, and let the queue
  // pick the next single box.
  async function toSingle() {
    RV.setMode("single");
    await RV.releaseFrame();
    await RV.next();
  }
```

Replace `complete` with:

```js
  async function complete() {
    if (busy) return;
    busy = true;
    let done = false;
    try {
      const r = await RV.post(`/api/review/frames/${state.saved_frame_id}/complete`);
      if (!r.ok) { RV.toast(r.data.error || "The frame isn't complete yet."); await reload(true); return; }
      RV.toast("Frame marked complete. Thank you!");
      RV.openFrameId = null;     // every box is answered: nothing left to release
      done = true;
    } finally {
      busy = false;
    }
    // Outside the busy guard: RV.next() may re-enter this view, and
    // enter() refuses while busy.
    if (done) await RV.next();
  }
```

In `render()`, change the top bar's first button to `RV.el("button", { type: "button", class: "rv-link", onclick: toSingle }, "Single boxes ", kbd("F"))`. In the key handler, change `if (k === "f") { RV.show("single"); return true; }` to `if (k === "f") { toSingle(); return true; }`. Update the file's header comment: entering a frame keeps a held box from the same frame and releases one from any other frame.

- [ ] **Step 6: Run the checks to verify they pass**

Run: `/raid/scratch/dzimmerman2021/miniconda3/envs/node/bin/node --check` on all three files, then the Step 2 pytest command, then the whole `tests` directory once. Expected: all dots, `exit=0`.

- [ ] **Step 7: Commit**

```bash
git add services/dashboard/app/static/review-core.js services/dashboard/app/static/review-single.js services/dashboard/app/static/review-frame.js tests/test_review_page.py
git commit -F .superpowers/sdd/commit-msg.txt   # "review: whole frame is the default; one router asks the queue"
```

---

### Task 4: "Next frame", per-box progress, clickable preview boxes, and the docs

**Files:**
- Modify: `services/dashboard/app/static/review-core.js` (new `RV.progressBar`)
- Modify: `services/dashboard/app/static/review-frame.js` (new `nextFrame`, the top bar, `modelPanel`, the S key)
- Modify: `services/dashboard/app/static/review-single.js` (drop `progressBar`; `frameThumb`)
- Modify: `services/dashboard/app/static/review.css` (append)
- Modify: `docs/operations.md` (Review tool section)
- Test: `tests/test_review_page.py`

**Interfaces:**
- Consumes:
  - `POST /api/review/frames/{id}/skip` (Task 1).
  - `box.counts_toward` (Task 2).
  - `RV.next()`, `RV.openFrameId`, `openFrame(eventId)` and `frameAllowed(c)` (Task 3).
- Produces: `RV.progressBar(countsToward) -> Element | null`.

- [ ] **Step 1: Write the failing static checks**

In `tests/test_review_page.py`, add `"nextFrame"` to `FRAME_MUTATING_HANDLERS`, then append:

```python
def test_next_frame_skips_and_asks_for_the_next_one_outside_the_busy_guard():
    src = _read("review-frame.js")
    body = _function_body(src, "nextFrame")
    assert "/skip`" in body
    fin = body.index("finally")
    assert "RV.next()" not in body[:fin]
    tail = body[fin:]
    assert "RV.next()" in tail[tail.index("}") + 1:]
    assert re.search(r'k === "s"\) \{ nextFrame\(\)', src), "S must be Next frame"


def test_the_progress_bar_is_shared_by_both_views():
    assert "RV.progressBar = function progressBar" in _read("review-core.js")
    single = _read("review-single.js")
    assert "function progressBar" not in single and "RV.progressBar(" in single
    assert "RV.progressBar(b.counts_toward)" in _read("review-frame.js")


def test_preview_boxes_open_the_frame_except_on_a_spot_check():
    body = _function_body(_read("review-single.js"), "frameThumb")
    assert "const clickable = frameAllowed(c)" in body
    assert "openFrame(o.event_id)" in body
```

- [ ] **Step 2: Run them to verify they fail**

Run: `TEST_DATABASE_URL_FILE=.superpowers/test_dsn timeout 900 /raid/scratch/dzimmerman2021/miniconda3/envs/wahoobay/bin/python -m pytest tests/test_review_page.py -q; echo exit=$?`
Expected: the three new tests and the `nextFrame` busy-guard parameter fail (`no function named 'nextFrame' found`).

- [ ] **Step 3: Move the progress bar into `review-core.js`**

Add after `RV.pct`:

```js
// "Counts toward <species>" with its progress bar, shared by the single-box
// and whole-frame views (spec amendment 2026-09-25).
RV.progressBar = function progressBar(t) {
  if (!t) return null;
  const fill = Math.min(100, Math.round((100 * t.count) / t.target));
  return RV.el("div", { class: "rv-progress" },
    RV.el("span", { class: "rv-label", text: "Counts toward" }),
    RV.el("span", { text: t.common || t.name }),
    RV.el("span", { class: "rv-bar", "aria-hidden": "true" }, RV.el("i", { style: `width:${fill}%` })),
    RV.el("span", { class: "rv-muted", text: `${t.count} / ${t.target}` }));
};
```

In `review-single.js`, delete `function progressBar(c) {...}`. In `render()`, change `[progressBar(c), guessCard(c)]` to `[RV.progressBar(c.counts_toward), guessCard(c)]`.

- [ ] **Step 4: Clickable preview boxes in `review-single.js`**

Replace the `for (const o of c.other_boxes) {...}` loop in `frameThumb` with:

```js
    // Clicking another fish opens the whole frame with that box selected
    // (spec amendment 2026-09-25) -- never on a spot-check, where the frame
    // view would show the other boxes' answers.
    const clickable = frameAllowed(c);
    for (const o of c.other_boxes) {
      const rect = RV.svg("rect", { x: o.bbox[0], y: o.bbox[1], width: o.bbox[2] - o.bbox[0],
        height: o.bbox[3] - o.bbox[1], class: clickable ? "rv-other rv-other-link" : "rv-other" });
      if (clickable) rect.addEventListener("click", () => openFrame(o.event_id));
      svg.append(rect);
    }
```

Append to `review.css`:

```css
/* spec amendment 2026-09-25: other fish in the single-box preview open the frame */
.rv-other-link { fill: rgba(0, 0, 0, 0.001); cursor: pointer; }
.rv-other-link:hover { stroke: #ffc400; stroke-width: 2px; }
```

- [ ] **Step 5: "Next frame" and per-box progress in `review-frame.js`**

Add next to `complete`:

```js
  // "Next frame" (spec amendment 2026-09-25, flow step 3): every box here
  // this volunteer hasn't answered is skipped for them -- the queue won't
  // bring them back -- and freed for everyone else at once.
  async function nextFrame() {
    if (busy) return;
    busy = true;
    let left = false;
    try {
      const r = await RV.post(`/api/review/frames/${state.saved_frame_id}/skip`);
      if (!r.ok) { RV.toast(r.data.error || "Couldn't leave this frame."); return; }
      RV.openFrameId = null;     // the skip already released this frame's boxes
      left = true;
    } finally {
      busy = false;
    }
    // Outside the busy guard: RV.next() re-enters this view.
    if (left) await RV.next();
  }
```

In `render()`'s top bar, add after the "Single boxes" button: `RV.el("button", { type: "button", onclick: nextFrame }, "Next frame ", kbd("S")),`.

In the key handler, add `if (k === "s") { nextFrame(); return true; }` directly after the `k === "d"` line, before `if (!selected) return false;`.

In `modelPanel(b)`, directly before `if (b.model) {`, add:

```js
    const progress = RV.progressBar(b.counts_toward);
    if (progress) out.push(progress);
```

Update the caption under the frame to: `"Click a box to answer it. Yellow dashed: needs an answer. Green: answered. Blue: drawn by a volunteer. S: next frame."`

- [ ] **Step 6: Document the flow in `docs/operations.md`**

In `## Review tool (/review)`, after the paragraph that ends "…`docs/superpowers/specs/2026-09-24-reviewer-annotation-dataset-design.md`.", insert:

```markdown
The page opens in **whole-frame mode** (spec amendment
`docs/superpowers/specs/2026-09-25-review-frame-first-design.md`). The queue
still picks a box (the volunteer's lane, thinnest species first), and the
page opens that box's frame with it selected. Volunteers click any box to
answer it; **S / "Next frame"** skips the boxes they didn't answer, for them
only, and frees them for everyone else; "Mark frame complete" stays
optional. **F** switches to single boxes, and the choice is remembered in
that browser. Spot-checks always appear as blind single boxes, and
"change your answer" opens the single-box view. Leaving a frame any other
way (another tab, closing the page) releases its claims without skipping
them (`POST /api/review/frames/{id}/release`).
```

- [ ] **Step 7: Run everything**

Run `node --check` on the three JS files, the Step 2 pytest command, the whole `tests` directory once, and `ruff check tests/test_review_page.py`. Expected: all dots, `exit=0`.

- [ ] **Step 8: Commit**

```bash
git add services/dashboard/app/static/review-core.js services/dashboard/app/static/review-frame.js services/dashboard/app/static/review-single.js services/dashboard/app/static/review.css docs/operations.md tests/test_review_page.py
git commit -F .superpowers/sdd/commit-msg.txt   # "review: Next frame, per-box progress, clickable preview boxes"
```

- [ ] **Step 9: Check it by hand in the demo**

Start the demo on the existing data (`timeout 7200 …/python scripts/dev/seed_review_demo.py --serve`, bound to 127.0.0.1:18090). Walk through the list below, then stop the demo (plain `kill`, then confirm nothing listens on :18090):

1. `/review` lands in a whole frame, with the served box selected and zoomed.
2. Clicking another box moves the zoom, controls and progress to it.
3. S loads a different frame.
4. Completing a frame loads the next one.
5. F switches to single boxes, and a reload stays single.
6. In single boxes, clicking another fish in the preview opens the frame with it selected.
7. A second browser signed in as the other demo account is never handed a box the first holds.

Record anything that fails in the report.

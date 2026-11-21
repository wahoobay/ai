# Whole-frame mark the rest + fix in place Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** In whole-frame review, one action answers every unanswered box with the species just given (tagged `bulk`), and a volunteer can change their own answers without leaving the frame.

**Architecture:** A `bulk` flag on `submit_verdict` stores `label_source='bulk'` (migration 0005 widens the CHECK). A new `mark_rest()` in `review_frames.py` recomputes the frame and calls `submit_verdict` once per `needs_answer` box. Each call is its own transaction, so a failure skips that box and the loop continues. `frame_state` gains per-box fields that let the page offer in-place re-answers. `review-frame.js` adds the button, the M key and its confirmation, and the inline re-answer panel.

**Tech Stack:** FastAPI, psycopg 3 (async), Alembic, plain browser JS (no build), pytest (+ textual JS tests via `tests/js_source.py`).

**Spec:** `docs/superpowers/specs/2026-11-21-frame-mark-all-design.md`

## Global Constraints

- The public verdict endpoint keeps refusing `label_source="bulk"`. Only `mark_rest` can set it.
- Mark-rest answers only boxes whose `frame_state` state is `needs_answer`. It never touches `locked`, `answered` or `answered_hidden` boxes.
- The species, `life_stage` and `not_sure` come from the volunteer's last species answer in this frame. They are forgotten when the volunteer enters a different frame.
- The M key or a click asks once ("Mark N boxes as \<name\>?"). M or Enter confirms, Esc cancels.
- A re-answer posts to `/api/review/verdicts` with `review_item_id: null`, keeps the volunteer's earlier box adjustment, and stays in the frame with that box selected.
- A box the volunteer answered as a blind second opinion keeps today's "Change my answer" link to the single view.
- Copy: the browse agreement panel says "marked with the rest" for `bulk`. The export doc lists `bulk`.
- Run tests with `TEST_DATABASE_URL_FILE=.superpowers/test_dsn` and `/Users/dan/wahoobay/ai/.venv/bin/python -m pytest`. Also run `ruff check .` and `ruff format --check .`.

## Review Focus

1. **A box claimed by someone else between the page load and the mark.** That box is skipped, the others are saved, and the response says how many were skipped. Owned by Task 2, test `test_mark_rest_skips_a_box_that_fails_and_keeps_going`.
2. **A species with several life stages.** The stage the volunteer chose carries over to every box, and a stage-less name for a staged species is refused before anything is written. Owned by Task 2, test `test_mark_rest_refuses_an_unknown_species_or_stage_before_writing`.
3. **The volunteer resized a box, then changes its answer in place.** The new verdict keeps the resized box. Owned by Task 3 (`my_bbox`) and Task 4 (the selection starts from `my_bbox`).
4. **Someone else has also answered the box, for example a finished spot-check.** No change controls are shown, matching the server's 403. Owned by Task 3, test `test_answered_box_offers_change_only_when_the_server_would_take_it`.
5. **A double press of M, or M while a request is in flight.** One request at most, through the busy guard. Owned by Task 4: add `markRest` to `FRAME_MUTATING_HANDLERS`.

---

### Task 1: `bulk` label source (migration 0005 + submit_verdict flag)

**Files:**
- Create: `db/migrations/versions/0005_label_source_bulk.py`
- Modify: `services/dashboard/app/review_queue.py` (`_validate`, `submit_verdict`)
- Modify: `services/dashboard/app/static/review-browse.js` (`how` map in `agreementPanel`)
- Modify: `services/dashboard/app/data/README.md` (the `label_source` row)
- Test: `tests/test_migrations.py`, `tests/test_review_queue.py`

**Interfaces:**
- Produces: `submit_verdict(conn, *, user, body, catalogue, spot_rate, rng, adjudication=False, bulk: bool = False) -> dict` (same return shape). With `bulk=True` and `outcome="species"`, it stores `label_source='bulk'` and ignores `body.label_source`.
- Produces: `_validate(..., adjudication: bool, bulk: bool = False)`. With `bulk` and an outcome other than `species`, it raises `Invalid("only a species can be marked with the rest")`. With `bulk` and `blind`, it raises `Invalid("a second opinion can't be marked with the rest")`.

- [ ] **Step 1: Write the failing tests.**
  - `test_migrations.py`: set `HEAD = "0005"`.
  - Add `test_label_source_accepts_bulk_at_head(pg_url)`. After `upgrade head`, insert a `detection_corrections` row with `label_source='bulk'` (on a seeded event), which succeeds. One with `'other'` raises `psycopg.errors.CheckViolation`.
  - `test_review_queue.py`: add `test_a_bulk_verdict_is_stored_as_bulk`. A species verdict through `submit_verdict(..., bulk=True)` stores `label_source == 'bulk'`, and `is_endorsement` is true when the label equals the box's guess.
  - Add `test_bulk_is_refused_from_the_public_body`: `VerdictIn(event_id=1, outcome="species", label="x", label_source="bulk")` raises `pydantic.ValidationError`.
  - Add `test_bulk_only_for_a_species`: `submit_verdict(..., body=VerdictIn(outcome="accept"), bulk=True)` raises `Invalid`.
- [ ] **Step 2: Run them and watch them fail.**
  - Command: `pytest tests/test_migrations.py tests/test_review_queue.py -k "bulk or head" -q`
  - Expected: FAIL.
- [ ] **Step 3: Implement.**
  - Migration `0005` (revision `"0005"`, down `"0004"`): `ALTER TABLE detection_corrections DROP CONSTRAINT IF EXISTS detection_corrections_label_source_check, ADD CONSTRAINT detection_corrections_label_source_check CHECK (label_source IN ('model_guess','hint','search','bulk'))`.
  - Downgrade restores the three-value CHECK. It fails while `bulk` rows exist; say so in the docstring.
  - Add the `bulk` kwarg to `_validate` and `submit_verdict` as specified. `VerdictIn` stays unchanged.
  - `review-browse.js`: `how.bulk = "marked with the rest"`.
  - README row: add `bulk`, meaning "marked with the rest in whole-frame review".
- [ ] **Step 4: Run the tests.**
  - Command: the same as Step 2.
  - Expected: PASS. Then run the full `pytest tests/test_review_queue.py tests/test_migrations.py -q` and expect PASS.
- [ ] **Step 5: Commit** with the message "Review: a 'bulk' label source for answers marked with the rest".

### Task 2: `mark_rest` and its route

**Files:**
- Modify: `services/dashboard/app/review_frames.py` (new `MarkRestIn` and `mark_rest`)
- Modify: `services/dashboard/app/routes_review.py` (new route)
- Test: `tests/test_review_frames.py`

**Interfaces:**
- Consumes: `submit_verdict(..., bulk=True)` from Task 1.
- Produces: `class MarkRestIn(BaseModel)` with fields `label: str = Field(..., max_length=200)`, `life_stage: str | None = Field(None, max_length=40)` and `not_sure: bool = False`.
- Produces: `async def mark_rest(conn, *, saved_frame_id: int, user, body: MarkRestIn, catalogue, spot_rate: float, rng) -> dict`, returning `{"marked": int, "skipped": int, "verdicts": [submit_verdict output, ...]}`.
- Produces: `POST /api/review/frames/{saved_frame_id}/mark-rest`, behind CSRF and the `reviewer` dependency. The JSON body is a `MarkRestIn`. The response is `{"marked", "skipped"}`, without `verdicts`. For each verdict with `first_pass and counts and not duplicate`, it calls `pool_obj.note_verified(label)`, exactly as `post_verdict` does.

- [ ] **Step 1: Write the failing tests** in `test_review_frames.py`.
  - `test_mark_rest_answers_only_the_unanswered_boxes`:
    - Use the 4-box `BOXES`: box 0 answered by `other`; box 1 live-claimed by `other` (locked); boxes 2 and 3 open.
    - `mark_rest` with label `"Seriola zonata"`, `spot_rate=0` gives `marked == 2` and `skipped == 0`.
    - Boxes 2 and 3 have one verdict each by me, with `label_source='bulk'` and `corrected_name='Seriola zonata'`. Boxes 0 and 1 have no new rows.
    - Their pass-1 items are `done`.
  - `test_mark_rest_carries_life_stage_and_not_sure`: a staged species with `life_stage` set and `not_sure=True` stores that `life_stage` and `confidence == 'uncertain'` on every row.
  - `test_mark_rest_refuses_an_unknown_species_or_stage_before_writing`: `label="Not a species"` raises `Invalid("pick a species from the list")`, and so does a staged species with `life_stage=None`. Zero rows are written.
  - `test_mark_rest_skips_a_box_that_fails_and_keeps_going`:
    - Monkeypatch `app.review_frames.submit_verdict` with a wrapper that raises `Conflict("another volunteer is answering this box")` for the first target and otherwise calls through.
    - Expect `marked == n-1` and `skipped == 1`.
  - `test_mark_rest_samples_spot_checks`: with `spot_rate=1.0`, every marked box gets a pass-2 `review_items` row whose `first_correction_id` is that box's verdict.
  - `test_mark_rest_refuses_while_you_hold_a_second_opinion_in_the_frame`: using the `_held_spot_check` helper, raises `Conflict(SECOND_OPINION_FIRST)`.
  - `test_mark_rest_over_http`:
    - Without CSRF the route returns 403.
    - With CSRF it returns 200 and `{"marked": 1, "skipped": 0}` on a one-box frame.
    - The frame then has `blocking == []`.
- [ ] **Step 2: Run them and watch them fail.**
  - Command: `pytest tests/test_review_frames.py -k mark_rest -q`
  - Expected: FAIL (ImportError).
- [ ] **Step 3: Implement `mark_rest`.**
  - Validate first: `catalogue.entry(body.label, body.life_stage)`, or raise `Invalid("pick a species from the list")`.
  - Then call `frame_state(conn, saved_frame_id=..., user=user, catalogue=catalogue)` and take its boxes with `state == "needs_answer"`.
  - For each box, `submit_verdict` with `VerdictIn(event_id=b["event_id"], review_item_id=b["review_item_id"], outcome="species", label=body.label, life_stage=body.life_stage, not_sure=body.not_sure)` and `bulk=True`.
  - Catch `ReviewError` per box, count it as skipped, and continue.
  - Import `submit_verdict` and `ReviewError` from `.review_queue`.
  - Add the route next to the other frame routes, using `cfg.review_spotcheck_rate` and the module's `rng`.
- [ ] **Step 4: Run the tests.**
  - Command: `pytest tests/test_review_frames.py -q`
  - Expected: PASS.
- [ ] **Step 5: Commit** with the message "Review: mark the rest of a frame's boxes in one request".

### Task 3: `frame_state` fields for changing an answer in place

**Files:**
- Modify: `services/dashboard/app/review_frames.py` (`frame_state`)
- Test: `tests/test_review_frames.py`

**Interfaces:**
- Produces: three more keys on every box dict.
  - `own_spot: bool`: this user has a correction on the event linked to a pass-2 item, the same query as `reopen_payload`'s `own_spot_answer`.
  - `my_bbox: list[int] | None`: the `corrected_bbox` of this user's latest correction on the event.
  - `can_change: bool`, true only when all of these hold:
    - `state == "answered"` and `mine` and `not own_spot`;
    - the user is an admin, or no correction on the event comes from another user (`user_id IS NOT NULL AND user_id <> me`);
    - no live claim on the event is held by another user.
- Produces: when `can_change`, the box also carries `model` (the guess from `catalogue.accepted(best_species_id)`, same shape as for `needs_answer`) and `hints`. The hints come from the event's latest pass-1 item, `ORDER BY added_at DESC, id DESC`, the same source `submit_verdict` checks a hint against; the guess is filtered out and the list capped at 3.

- [ ] **Step 1: Write the failing tests.**
  - `test_answered_box_offers_change_only_when_the_server_would_take_it`:
    - My answered box gives `can_change` true, with `model` set when the event has a guess.
    - The same box after `other` also answers it gives `can_change` false.
    - A box I answered as a pass-2 spot-check (my correction with `review_item_id` = a pass-2 item) gives `own_spot` true and `can_change` false.
    - A box only `other` answered gives `can_change` false.
  - `test_my_bbox_is_my_latest_corrected_box`: after my verdict with `bbox=[12,12,100,100]`, the box's `my_bbox == [12,12,100,100]`. Boxes I didn't answer have `my_bbox is None`.
  - `test_a_change_from_the_frame_keeps_the_frame_view`: a second `submit_verdict` from me with no item id on a `can_change` box succeeds, and `frame_state` shows the new label. This guards the no-item path the page relies on.
- [ ] **Step 2: Run them and watch them fail.**
  - Command: `pytest tests/test_review_frames.py -k "change or my_bbox" -q`
  - Expected: FAIL (KeyError).
- [ ] **Step 3: Implement.** Use one extra query each for my latest corrections, the pass-2-linked corrections, and other users' corrections, all `event_id = ANY(%s)`. Batch them as `mine` already is; no per-box queries.
- [ ] **Step 4: Run the tests.**
  - Command: `pytest tests/test_review_frames.py -q`
  - Expected: PASS.
- [ ] **Step 5: Commit** with the message "Review: the frame tells the page which answers can change in place".

### Task 4: The frame page (button, M key, confirmation, inline re-answer)

**Files:**
- Modify: `services/dashboard/app/static/review-frame.js`
- Modify: `services/dashboard/app/static/review.css` (only if the confirmation needs a style; reuse `rv-note`/`rv-row` first)
- Test: `tests/test_review_page.py`

**Interfaces:**
- Consumes: the `POST .../mark-rest` route (Task 2) and the box fields `can_change`, `own_spot`, `my_bbox`, `model` and `hints` (Task 3).
- Produces these module-level names in `review-frame.js`:
  - `lastAnswer`: `{label, life_stage, not_sure, common}` or null.
  - `confirmingMark`: boolean.
  - `boxTarget(b)`: the selection object, starting from `b.my_bbox || b.bbox`.
  - `async markRest()`.
  - `changeable(b)`: `b.state === "needs_answer" || (b.state === "answered" && b.can_change)`.

- [ ] **Step 1: Write the failing tests** in `test_review_page.py`.
  - Add `"markRest"` to `FRAME_MUTATING_HANDLERS`.
  - `test_mark_rest_posts_to_the_frame_route`: the `markRest` body contains `` `/api/review/frames/${state.saved_frame_id}/mark-rest` `` and the stale-frame guard `RV.openFrameId !== state.saved_frame_id`.
  - `test_last_answer_is_per_frame`:
    - The `enter` body sets `lastAnswer = null` when the frame id changes.
    - The `answerModel` body assigns `lastAnswer` only for `accept`/`species`.
  - `test_m_asks_before_marking`: the key handler's `m` branch sets `confirmingMark`, and only a second `m` or `Enter` while `confirmingMark` calls `markRest()`.
  - `test_reanswer_keeps_the_frame`: the `answerModel` body calls `reload(true)` when the box was already answered.
- [ ] **Step 2: Run them and watch them fail.**
  - Command: `pytest tests/test_review_page.py -q`
  - Expected: FAIL.
- [ ] **Step 3: Implement in `review-frame.js`.**
  - **Selections.** Use `boxTarget` in `selectBox`, `selectFirst`, `hit` and `checklist`. `drawBoxes` draws `b.my_bbox || b.bbox` for unselected boxes.
  - **`editableSelection` and the key handler** use `changeable(b)` in place of the `needs_answer`-only checks.
  - **`modelPanel(b)` for `answered`:**
    - When `can_change`: the "Answered: … (by you)." line, then a "Change it" label, the "Looks right" block (when `b.model`) and `pickerAndButtons(b.hints, …)`, wired to `answerModel` as for an unanswered box.
    - When `own_spot`: today's "Change my answer" button.
    - Otherwise: a `rv-muted` line, "This answer can't be changed here any more."
  - **`answerModel`:**
    - On success with `accept`/`species`, set `lastAnswer`. For accept, `label` is `b.model.name` and `common` is `b.model.common`; for species, use `extra.label` and `RV.entry(extra.label, extra.life_stage)?.common`. Carry `life_stage` and `not_sure`.
    - Remember whether the box was already answered before posting. If it was, call `await reload(true)` and re-select it with `selectBox(id)` after the reload. Otherwise call `reload(false)` as today.
  - **`enter`:** reset `lastAnswer` and `confirmingMark` when `id !== (state && state.saved_frame_id)`.
  - **`panelContent`:**
    - Before the checklist, when `lastAnswer` is set, at least one box has `state === "needs_answer"`, and the frame is not complete: show the button "Mark the N unanswered boxes as \<common or label\>" plus `kbd("M")`, which sets `confirmingMark` and re-renders the panel.
    - While `confirmingMark`, show instead the line "Mark N boxes as \<name\>?" with the buttons "Yes, mark them" (`kbd("M")`) and "Cancel" (`kbd("Esc")`).
  - **`markRest()`:**
    - Run behind the stale-frame guard and the busy guard.
    - POST `{label, life_stage, not_sure}`.
    - On success, toast "Marked N boxes as \<name\>." and append " K were taken by someone else." when skipped > 0.
    - Then `await RV.refreshMine()` and `reload(false)`. Clear `confirmingMark` in `finally`.
  - **Keys:**
    - `m` with nothing to mark returns false.
    - `m` when not confirming sets `confirmingMark` and re-renders the panel. `m` or `Enter` while confirming calls `markRest()`.
    - `Escape` while confirming clears it, and that check runs before the existing Escape handling.
    - Any `select()` or `reload` clears `confirmingMark`.
  - **`legend`:** add `kbd("M"), " mark the rest"`.
- [ ] **Step 4: Run the tests and lint.**
  - Command: `pytest tests/test_review_page.py tests/test_booth_tours.py -q && node --check services/dashboard/app/static/review-frame.js`
  - Expected: PASS, no syntax errors.
- [ ] **Step 5: Run it in a browser.**
  - Run the dashboard locally against the scratch DB, seeding with `make seed`/`app.seed` and a reviewer account.
  - Walk the flow: answer a box, press M, then M again; boxes are answered. Click one and pick another species; it stays in the frame with the new label.
  - Capture a screenshot for the PR.
- [ ] **Step 6: Commit** with the message "Review page: mark the rest (M) and change your answers in the frame".

### Task 5: Full suite, PR, review, merge, staging check

- [ ] **Step 1: Run the whole suite.** Command: `ruff check . && ruff format --check . && pytest -q`. Expected: all pass, no new skips beyond those on `main`.
- [ ] **Step 2: Open the PR.** Push `feat/frame-mark-all` and open it with the spec link, the screenshot, and the migration note ("0005 widens a CHECK; instant on staging's table").
- [ ] **Step 3: Review the branch.** Run one fresh whole-branch review on the most capable model, fix the confirmed findings, and re-run Step 1.
- [ ] **Step 4: Merge** when CI is green (Dan said "merge"). Watch the staging deploy finish.
- [ ] **Step 5: Verify on staging.**
  - Confirm the Alembic head is 0005 (SSM read on the GPU host, the worker's `DATABASE_URL`).
  - Confirm `/static/review-frame.js` on staging contains `mark-rest`.
  - Tell Dan, and draft a two-line note to Roy.

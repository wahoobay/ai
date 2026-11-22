# Whole-frame review: mark the rest, fix in place

2026-11-21 · short design (small change) · approach B, chosen by Dan

## Why

Roy (volunteer reviewer) is checking clear archive frames that hold schools of 20–30+ Smallmouth
Grunts with a few other grunts mixed in. Today every box is one answer, so a school of 25 costs 25
answers. He asked for a way to "mark all, then just amend the 4–5 that are not Smallmouth". Model
training is the current priority (Katie), so reviewer throughput on these frames matters.

## Goals

- One action marks every unanswered box in the frame as the species the volunteer just gave.
- Changing an answer you gave keeps you in the frame, so "mark all, then amend" works in that order.
- Bulk answers stay distinguishable in the data, so their accuracy can be measured on their own.

## Non-goals

- No bulk action across frames, and no "accept every model guess" button (a different feature).
- No change to the single-box view, the queue, the pool, or how labels resolve.

## Anti-goals

- A bulk answer must never land on a box someone else holds, a box already answered, or a pending
  second opinion.
- A stray key press must not mark 25 boxes.
- Bulk answers must not pass for individually checked ones in the stats or exports.

## Approaches

- **A. Mark the rest only.** Smallest change, but Roy would have to answer the odd boxes first.
  Marking first and then fixing would send him out of the frame on every fix, because "Change my
  answer" opens the single view and moves on to the next item after saving.
- **B. Mark the rest, plus fix in place (chosen).** Same button, and re-answering one of your own
  boxes happens in the frame's side panel. It matches Roy's own order, and in-frame fixing also
  helps outside schools.

## Design

**Marking (my calls).**
- The species comes from the volunteer's last species answer in this frame: "Looks right" or a
  picked species. Its life stage and "not sure" carry over.
- That answer is forgotten when the volunteer enters a different frame, so a new frame needs one
  answer first.
- Once that answer exists and at least one box is unanswered, the side panel shows "Mark the N
  unanswered boxes as \<common name\>" with key **M**.
- Clicking it, or pressing M, asks once in the panel ("Mark 23 boxes as Smallmouth grunt?"), and
  M or Enter confirms. This is the guard against a stray key.

**Server.**
- New `POST /api/review/frames/{id}/mark-rest` with body `{label, life_stage, not_sure}`, behind
  CSRF like the other frame routes.
- It recomputes the frame server-side with `frame_state`, so live second-opinion claims are still
  refused. It answers only boxes in state `needs_answer`; locked, answered and hidden boxes are
  left alone.
- Each box goes through `submit_verdict`, with the volunteer's claimed item id when they hold one
  and with no item otherwise, exactly as the frame page sends it today. Every existing rule
  applies: claims, catalogue check, duplicates, marking items done, and spot-check sampling.
- Spot checks are sampled at about 1 in 10, so a 25-box frame yields about 2–3 blind re-checks of
  the bulk answers.
- A box that fails (someone took it meanwhile) is skipped, not fatal. The response is
  `{marked, skipped}`.

**The `bulk` tag.**
- These verdicts store `label_source = 'bulk'`. The public verdict endpoint keeps refusing `bulk`,
  so only mark-rest can set it.
- Migration 0005 widens the `detection_corrections.label_source` CHECK to include `'bulk'`.
- The browse page's agreement panel labels it "marked with the rest". The export column doc
  (`app/data/README.md`) lists it.
- A box fixed afterwards gets a new verdict with its own source (hint/search/model_guess), so only
  the boxes left as marked keep `bulk`.

**Fix in place (my calls).**
- Clicking a box you answered shows your answer plus the same controls an unanswered box has:
  Looks right (when the model has a guess), the picker and hints, Can't identify, No animal, and
  Not sure. It no longer shows only "Change my answer".
- A new answer posts to `/api/review/verdicts` with no item id. That is the path single-view
  reopening already uses, so the server rules are unchanged.
- After saving, the frame reloads with that box still selected.
- An earlier box adjustment is kept: `frame_state` returns your own corrected box for boxes you
  answered, the frame draws it, and a re-answer sends it. The box handles stay draggable on your
  own answered boxes.
- One exception: a box you answered as someone's blind second opinion keeps today's "Change my
  answer" to the single view, which already applies the blind rules.

## Constraints

- Tests first (`ruff check`, `ruff format --check`, `pytest` with `TEST_DATABASE_URL` on a scratch
  database).
- `test_migrations` pins the Alembic head and moves to 0005.
- The JS is tested textually or under node (`tests/js_source.py`). There is no browser harness.
- Merging to `main` redeploys staging and runs the migration while Roy may be reviewing. Dan picks
  the merge time.
- Open PR ai #45 (label archive) touches `exports.py` but not `label_source`, so there is no
  conflict expected.

## Left to the builder

- The exact panel wording and placement, the confirm's look, and the toast text.
- Whether mark-rest runs one transaction per box (as written) or one with savepoints. The
  behaviour must stay skip-and-continue.
- Test split across `test_review_frames.py`, `test_review_queue.py` and `test_review_routes.py`.

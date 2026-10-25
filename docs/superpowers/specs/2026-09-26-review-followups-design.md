# Review tool: follow-up round (decisions and fix-soon items)

*2026-09-26. Amends `docs/superpowers/specs/2026-09-24-reviewer-annotation-dataset-design.md` and `docs/superpowers/specs/2026-09-25-review-frame-first-design.md`, which stay binding except where this document changes them. The review tool has been in production since 2026-09-26 11:22 EDT. This round ships the owner's decisions from that day, plus the "fix soon after" items from the two final reviews (phase-1 `final-review.md` and frame-first `final-rereview.md` in the SDD workspaces).*

## A. The owner's decisions

**A1. The dashboard's old correction form goes through the review rules.** `POST /api/corrections` keeps its buttons and its request shape. It saves through `submit_verdict`'s no-item path with `spot_rate = 0`, because verdicts from the older form are not spot-checked (spec §8). A thin adapter maps the form's fields:

| Old form | Review verdict |
|---|---|
| endorse | `accept`, guess = `catalogue.accepted(best_species_id)` |
| correction (`corrected_name` or `corrected_species_id`) | `species`, label = `catalogue.accepted(...)`, `label_source = "search"`; **400** if the name isn't in the catalogue |
| not a fish | `no_animal` |
| confidence "probable" or "certain" / "uncertain" | certain / `not_sure` |
| celebrity, notes, bbox | passed through; bbox checked with `validate_bbox` |

The request then gets everything `submit_verdict` enforces: the event lock, the guards for live claims and pending spot-checks, closing the box's open pool items, and accepted names. The form therefore behaves differently in two cases:
- **403** when another volunteer already answered the box;
- **400** for a name outside the catalogue.

The page shows both as its usual error message.

**A1b. Existing legacy rows.** There are 53 of them, none since May, some with raw or misspelled names such as "Pomatomus saltarix". A one-off script, `scripts/dev/remap_legacy_corrections.py`, maps their `corrected_name` to accepted names:
- by default it prints what would change (a dry run);
- `--apply` does it in one transaction;
- names it can't map are listed and left alone.

Running `--apply` on production needs the owner's go.

**A2. Skips expire after 30 days** (`REVIEW_SKIP_TTL_DAYS`, default 30).
- **Where it applies:** a skip only counts while `review_skips.created_at` is less than that many days old. That covers the pass-1 queue, the spot-check queue and `open_frame`'s re-claim filter.
- **Skipping again restarts the clock:** `ON CONFLICT DO UPDATE SET created_at = now()`.
- **No schema change:** the column already exists.

**A3. A preview click keeps single-box mode.** In single-box view, clicking another fish in the frame preview opens the whole frame with that box selected, **without** changing the saved mode. F, or "Single boxes", returns to single-box view. Only F in single-box view and the whole-frame toggle change the saved mode.

## B. Volunteer-facing page fixes

- **B1. Browse mid-answer no longer bounces you back.**
  - **Today:** a Browse or lane click made while a single-box answer or skip is still saving is honoured, but the action's trailing `RV.next()` then starts with a newer token and pulls the volunteer out.
  - **Fix:** the action captures `RV.navToken` when it starts, and passes it to `RV.next(token)`. `RV.next` doesn't start if the token has moved on. The same applies to the landing call on page load.
- **B2. Browse box cells respect a busy single-box view.** The single view exposes `isBusy()`. A Browse cell clicked while it's busy is ignored, instead of leaving "Loading the next box…".
- **B3. Page requests time out.** `RV.api` aborts after 20 s (`AbortController`) and returns `{ok: false, status: 0, data: {error: "the server took too long; try again"}}` with the banner. A stalled request can no longer block `RV.next()`'s queue forever.
- **B4. A superseded next-box release lands before a frame open.** When a superseded `RV.next()` gives its box back, the release is awaited before a frame view's POST `/open` can run for the same frame. Today the release can strip the frame's fresh claim and cost one redone answer.
- **B5. Reopening shows your previous answer.** "Changing your answer" shows what you answered, with species, stage and box, using the payload's `mine` field.
- **B6. Admins can edit a completed frame.**
  - A completed frame shows admins a "Reopen frame for edits" button, which calls `DELETE /api/review/frames/{id}/complete`.
  - `editableSelection()` and `addedPanel()` no longer refuse admins on a completed frame.
  - Volunteers are unchanged.

## C. Server correctness

- **C1. An admin can't read a pending spot-check through the reopen URL.** `reopen_payload` returns a blind payload when a pass-2 item for the box is open or claimed and the caller has no verdict on it (phase-1 triage 155).
- **C2. An admin's re-answer to their own spot-check stays a second opinion.** A no-item re-answer is linked to the caller's own pass-2 item, or refused, so the resolved pair doesn't become admin versus admin (phase-1 Minor 4).
- **C3. The right message for a retired item.** A verdict on a retired item says the box was retired and why (`frame_missing`, `outside_window`), not "already answered" (Minor 14).
- **C4. The pool never queues an answered box.** The pool's insert is conditional: `INSERT … SELECT … WHERE NOT EXISTS (a verdict for that event)` (Minor 10).
- **C5. The frame-size cache notices a changed file.** `frame_size`'s cache is keyed on `(path, st_mtime_ns)`, so a frame deleted or replaced by hand is seen as missing (triage 146).
- **C6. File reads off the event loop.** `safe_frame_size` and `CatalogueStore.get()`'s rebuild run in `asyncio.to_thread` from async callers (Minor 7).

## D. Public outputs (privacy and counting)

- **D1. The public species gallery matches frames exactly.** It joins frames on camera + `frame_id` + `ts` (`AND e.ts = sf.ts`, `main.py` about 2173; triage 75).
- **D2. Public sighting counts count events.** They use `COUNT(DISTINCT event_id)`, so spot-checks and reopened answers don't inflate counts or duplicate thumbnails (Minor 2).
- **D3. A spot-check's first answer stays private while the check is pending.** The anonymous `/api/corrections` list and the public corrections CSV leave out events with an open or claimed pass-2 item (Minor 1).
- **D4. "Can't identify" shows as such in public outputs,** not as a nameless species correction (Minor 15).
- **D5. Admin names stay off the public alerts feed.** The anonymous `/api/alerts/active` no longer returns `acknowledged_by` (admins still get it), and `app.js` inserts alert text as text, never as markup (frame-first out-of-scope note).

## E. Test hardening

Tests to add:
- a reviewer session is never treated as admin (triage 77);
- `CatalogueStore` keeps the previous catalogue when a bad file arrives (97);
- the resolved-box rule, which is phase 3's crop rule (103);
- the browse and progress statement timeouts, via an injected `pg_sleep` (210);
- the page-bounded `saved_frame_id` values (211).

Tests to fix:
- the concurrency test polls `pg_blocking_pids` with a deadline instead of `sleep(0.3)`;
- pin `pointer-events: auto` on `.rv-other-link`;
- make the static busy-guard regexes accept braced guards and check that the reset sits inside `finally`.

## F. Main dashboard times in Eastern

`app.js`'s 13 time displays go through one Eastern formatter (America/New_York, marked EDT or EST), the same rule as the review page's `RV.fmtTime`.

## Out of scope (still recorded in the ledgers)

- **A pool reset callback, or restoring autocommit after the matview refresher** (triage 128). It changes every older handler, so it gets its own reviewed change.
- **Browse at 30k-crop scale** (Minor 9). This needs per-event metadata; do it before volunteers scale up.
- **Re-adjudication** (Minor 3). This is a feature; `operations.md` already says an adjudication is final.
- **Measuring spot-check exposure under frame-first.** This is accepted for the pilot: measure it once there are enough spot-checks, and revisit before publishing agreement numbers.

## Testing and rollout

Each item gets tests: database tests for server behaviour, static checks for page code (there is no browser harness), and the full suite before merging.

**Rollout:**
1. Fast-forward `reports-panel-and-weather`.
2. Restart the dashboard, a few seconds. No migration.
3. Run A1b's `--apply` only after its dry run has been shown to the owner.

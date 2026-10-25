# Review tool: frame-first review (amendment)

*2026-09-25. Amends `docs/superpowers/specs/2026-09-24-reviewer-annotation-dataset-design.md`, which remains binding except where this document changes it. Status: approved in conversation; to be built before the review tool is deployed (phase 1, Task 14).*

## Why

The original spec made per-box review the main mode ("fish-first"), with whole-frame mode as an option (§87, §329, §370). In a hands-on walkthrough of the demo, the owner found the whole-frame view the better default. It shows every fish in context, and clicking any box moves the zoomed view and the answer controls to it. It also turns more of each visit into labels. The owner also asked that clicking a box in the frame should switch the zoomed box, which the whole-frame view already does.

## Decisions

| Question | Decision |
|---|---|
| Default mode | **Whole frame.** Single-box view remains an option, switched with F. The choice is remembered in the browser. |
| Must a frame be finished? | **No.** "Next frame" is always available. "Mark frame complete" stays optional; it is what makes a frame usable for detector training. |
| How is the next frame chosen? | **The queue picks a box, as today.** That is the volunteer's lane, thinnest species first. The page opens that box's frame with the box selected. |
| Spot-checks | **Unchanged: blind, single-box.** They are served between frames by the same queue. |
| Where single-box view is required | Spot-checks, and "change your answer" (from the recent strip and from Browse). |

## Flow

1. **Next.** The page asks `/api/review/next?lane=…` for a box. The queue, its ordering and its spot-check serving are unchanged.
   - **No box:** show the existing "the pool is filling" state.
   - **A spot-check** (`item.pass === 2`): show the blind single-box view, exactly as today.
   - **A regular box, whole-frame mode** (the default): open that box's frame with the box selected.
   - **A regular box, single-box mode:** show it in the single-box view, as today.
2. **In a frame,** clicking any box selects it. The side panel shows its zoom and controls. The "counts toward" line and the species progress bar follow the selected box, using its model guess's species.
3. **"Next frame"** (a button, and the S key, matching Skip in single-box view) is always available:
   - Every pass-1 box on the frame that the volunteer hasn't answered is marked **skipped for that volunteer**, so the queue won't serve it to them again. Their claims on those boxes are released, so the boxes are free for everyone else at once.
   - The page then runs step 1.
4. **"Mark frame complete"** keeps today's rules. On success the page runs step 1; today it returns to single-box view instead.
5. **Leaving a frame any other way** releases the volunteer's claims on it but skips nothing. That covers switching to single-box view with F, opening Browse or an admin tab, and closing the tab. When the tab closes, the release is sent as a `fetch(…, {keepalive: true})` request carrying the CSRF header. If it never arrives, the claims expire after the usual claim window.
6. **Single-box view as the chosen mode:** clicking a box in its frame preview opens the whole frame with that box selected. That is disabled on spot-checks, following the same rule that already hides "Whole frame" there.

## Server changes

- **`open_frame`** also refreshes the caller's own live pass-1 claims in the frame, not only open or lapsed items. This covers the box the queue just served. Because every answer reloads the frame, claims stay fresh while the volunteer is working. The spot-check guard (`refuse_during_second_opinion`) is unchanged.
- **New `POST /api/review/frames/{id}/release`** (`reviewer` role, `require_csrf`): sets the caller's claimed pass-1 items in that frame back to `open`, with no claimant. Items claimed by other volunteers, answered items and pass-2 items are not touched.
- **New `POST /api/review/frames/{id}/skip`** (`reviewer` role, `require_csrf`):
  - Records a `review_skips` row for the caller on every pass-1 item in the frame that is `open` or `claimed` (anyone's claim).
  - Releases the caller's own claims among them.
  - Never touches pass-2 items.
  - `open_frame` already won't claim items the caller has skipped (Task 8), and `next_item` already won't serve them.
- **`next_item`**, when it hands back an item the caller already holds, refreshes `claimed_until` (final review, Minor 5).
- **`frame_state`** gains, for each model box with a catalogue guess, the species it counts toward and that species' verified count and target. These are the same numbers the single-box payload uses. Nothing is added for boxes whose answer is hidden by a pending spot-check.

`/api/review/next`, the verdict paths, the completion rules and the blindness rules are otherwise unchanged.

## Page changes

- **A shared `RV.next()` in `review-core.js`** fetches `/next` once and routes the payload to the right view (Flow, step 1). The single-box view no longer fetches `/next` itself, so a box is never claimed twice.
- **The held item is recorded as `{itemId, savedFrameId}`.** Opening a frame doesn't release a held item that belongs to that frame. A held item from another frame is released as today.
- **`RV.views.frame.enter({savedFrameId, selectEventId})`** selects that box after opening, falling back to the first unanswered box. It gains "Next frame" with the busy guard (`if (busy) return;`, reset in `finally`), and it runs `RV.next()` after a successful completion.
- **The mode preference** is stored under `localStorage` key `rv.mode` (`"frame"` | `"single"`, default `"frame"`), with every read and write wrapped in `try/catch`. If storage is blocked, the page falls back to whole frame.
- **Switching views or tabs** releases the frame claims (Flow, step 5).

## Out of scope

- A server-side "next frame" endpoint that picks frames itself.
- Merging the two views.
- Showing the recent-answers strip in the frame view. It stays in single-box view, and Browse's "mine" filter covers the same need.
- Changes to spot-check rates, completion rules or the queue's ordering.

## Testing

- **Database tests:**
  - `release` affects only the caller's claims in that frame. Others' claims, answered items and pass-2 items are untouched. CSRF and role are enforced.
  - `skip` records skips for every open or claimed pass-1 item in the frame and releases only the caller's claims. After a skip, `next_item` doesn't serve those items to the caller but does serve them to another volunteer.
  - `open_frame` keeps and refreshes the caller's live claim on the served box; there's no window in which another volunteer can claim it.
  - `open_frame` still returns 409 while the caller holds a live pass-2 claim in the frame.
  - `next_item` refreshes `claimed_until` when it returns a held item.
  - `frame_state`'s per-box progress matches the single-box payload's numbers for the same species, and is absent on hidden boxes.
- **Static page checks** (`tests/test_review_page.py`):
  - Routing: pass 2 goes to single-box view, anything else goes to the frame by default.
  - `rv.mode` storage access is wrapped in `try/catch`.
  - The served item isn't released before its own frame opens.
  - "Next frame" has the busy guard.
  - Tab close sends a keepalive release.
  - Preview boxes in single-box view open the frame and are disabled on pass 2.
- **By hand:** walk the frame-first flow in the demo. That means landing, clicking between boxes, "Next frame", completion, a spot-check arriving between frames, switching to single-box view and back, and a second volunteer never being handed a box the first holds.

"""The review page's shell and scripts. There is no browser-test harness
in this repo, so the JavaScript gets a syntax check here and a manual
checklist in Task 12."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest
from js_source import NODE, STATIC, function_source, needs_node, run_node
from js_source import braces_from as _braces_from
from js_source import function_body as _function_body
from js_source import read as _read
from review_fixtures import login, make_user

SCRIPTS = [
    "review-core.js",
    "review-worms.js",
    "review-single.js",
    "review-frame.js",
    "review-browse.js",
]


@pytest.mark.skipif(not Path(NODE).exists(), reason="node is not available")
@pytest.mark.parametrize("name", SCRIPTS)
def test_script_parses(name):
    r = subprocess.run(
        [NODE, "--check", str(STATIC / name)], capture_output=True, text=True, timeout=60
    )
    assert r.returncode == 0, r.stderr


def test_scripts_never_write_markup_from_data():
    for name in SCRIPTS:
        assert "innerHTML" not in (STATIC / name).read_text(), name


# ---- fix round 1: re-entrancy guards -------------------------------------
# No JS runtime test harness exists in this repo (see the module
# docstring), so these are static, textual checks rather than an actual
# double-click/key-repeat simulation. They still fail if the busy guard or
# the ev.repeat check is removed from the source, which is what a
# regression test needs to do.

# The busy-guard pattern: an early return while busy (braced or not), busy =
# true, and busy = false inside the function's finally block.
BUSY_RETURN = re.compile(r"\bbusy\b\s*\)\s*(?:\{\s*)?return\b")


def _assert_busy_guarded(body: str, name: str) -> None:
    """The busy-guard pattern: an early return while busy (braced or not),
    busy = true, and busy = false inside the function's finally block."""
    assert BUSY_RETURN.search(body), f"{name}() must return early when busy is already true"
    assert re.search(r"\bbusy\s*=\s*true\b", body), f"{name}() must set busy = true"
    m = re.search(r"\bfinally\s*\{", body)
    assert m, f"{name}() must reset busy in a finally block"
    assert re.search(
        r"\bbusy\s*=\s*false\b", _braces_from(body, m.end() - 1)
    ), f"{name}() must reset busy inside its finally block"


def test_the_busy_guard_check_accepts_braces_and_wants_the_reset_in_finally():
    good = "async function f() { if (busy) { return; } busy = true; try { x(); } finally { busy = false; } }"
    _assert_busy_guarded(_function_body(good, "f"), "f")
    bad = "async function f() { if (busy) return; busy = true; try { x(); } finally { y(); } busy = false; }"
    with pytest.raises(AssertionError):
        _assert_busy_guarded(_function_body(bad, "f"), "f")


def test_preview_boxes_take_clicks_though_the_overlay_does_not():
    rule = re.search(r"\.rv-other-link\s*\{([^}]*)\}", _read("review.css")).group(1)
    assert re.search(r"pointer-events:\s*auto", rule)


# submit(), skip() and reopen() are review-single.js's three mutating
# (POST-issuing) actions a volunteer can trigger repeatedly by key or
# click; each must gate on the shared `busy` flag so a held-down key or a
# fast double click can't fire two overlapping requests for the same box
# (task-10-findings-r1.md #1 and #2).
MUTATING_HANDLERS = ("submit", "skip", "reopen")


@pytest.mark.parametrize("name", MUTATING_HANDLERS)
def test_mutating_handlers_share_the_busy_guard(name):
    body = _function_body((STATIC / "review-single.js").read_text(), name)
    _assert_busy_guarded(body, name)


# review-frame.js's own actions that send a request (Task 11): entering the
# view claims the frame's boxes, and each answer/draw/complete action
# mutates state, so the same re-entrancy guard applies to all of them.
FRAME_MUTATING_HANDLERS = (
    "enter",
    "answerModel",
    "saveAddedBox",
    "removeAddedBox",
    "complete",
    "toSingle",
    "nextFrame",
    "reopenFrame",
    "markRest",
)


@pytest.mark.parametrize("name", FRAME_MUTATING_HANDLERS)
def test_frame_mutating_handlers_share_the_busy_guard(name):
    body = _function_body((STATIC / "review-frame.js").read_text(), name)
    _assert_busy_guarded(body, name)


# review-browse.js's own mutating admin action (Task 12): settling a
# dispute. (The date window used to be saved here too; since 2026-10-27 it
# is each reviewer's own, kept in the browser session, and the server has
# no setting.) Loading the grid, the progress KPIs and the admin panels are
# plain re-reads (same class as review-single.js's loadNext() and
# review-frame.js's reload(), neither of which is in the lists above), so
# only this one gets the guard. Since 2026-11-21 the new-species panel's
# approve, reject and per-box "Not this species" share it too, and so does
# removing a species a reviewer added.
BROWSE_MUTATING_HANDLERS = (
    "adjudicate",
    "approveSuggestion",
    "rejectSuggestion",
    "detachBox",
    "removeAddition",
)


@pytest.mark.parametrize("name", BROWSE_MUTATING_HANDLERS)
def test_browse_mutating_handlers_share_the_busy_guard(name):
    body = _function_body((STATIC / "review-browse.js").read_text(), name)
    _assert_busy_guarded(body, name)


# Fix round 1, Important finding: review-single.js's and review-frame.js's
# acceptGuess() were a near-verbatim duplication of the same life-stage
# check (stage-less entry -> auto-accept; one remaining stage -> auto-accept
# with it; two or more -> a chip picker). Both must now delegate to one
# shared RV.acceptGuessFlow() in review-core.js instead of reimplementing
# the stage enumeration inline, so the rule can't drift between the views.
def test_accept_guess_uses_the_shared_helper():
    assert (
        "RV.acceptGuessFlow" in (STATIC / "review-core.js").read_text()
    ), "the shared life-stage-picking helper must live in review-core.js"
    for name in ("review-single.js", "review-frame.js"):
        body = _function_body((STATIC / name).read_text(), "acceptGuess")
        assert "RV.acceptGuessFlow(" in body, f"{name}'s acceptGuess() must call the shared helper"
        assert (
            "RV.catalogue.filter" not in body
        ), f"{name}'s acceptGuess() must not reimplement the life-stage check inline"


# Final review I1: whole-frame mode shows the other boxes' labels and model
# guesses, so a spot-check (pass 2) must not offer it. The server refuses
# too (409 "finish the second opinion first"); the page just shouldn't lead
# there. item.pass is what tells a spot-check apart: item.lane is null for
# pass 2, so it cannot be used.
def test_whole_frame_is_not_offered_on_a_spot_check():
    src = (STATIC / "review-single.js").read_text()
    gate = _function_body(src, "frameAllowed")
    assert re.search(r"\.item\.pass\s*!==\s*2", gate), "frameAllowed() must refuse pass 2"
    assert "lane" not in gate, "frameAllowed() must not rely on item.lane"
    # every way into the frame view from the single view goes through it:
    # the button, the F key, and openFrame() itself
    assert re.search(
        r"frameAllowed\(c\)\s*\?\s*RV\.el\(\"button\"[^\n]*Whole frame", src
    ), "the Whole-frame button must only be built when frameAllowed(c)"
    assert re.search(
        r'k === "f" && frameAllowed\(current\)', src
    ), "the F key must be ignored unless frameAllowed(current)"
    assert re.search(
        r"if \(frameAllowed\(current\)\)", _function_body(src, "openFrame")
    ), "openFrame() must check frameAllowed"
    assert src.count('RV.show("frame"') == 1, "the frame view is only entered via openFrame()"


def test_key_dispatcher_ignores_held_down_keys():
    src = (STATIC / "review-core.js").read_text()
    marker = 'document.addEventListener("keydown"'
    block = _braces_from(src, src.index("{", src.index(marker)))
    assert "ev.repeat" in block, (
        "the shared keydown dispatcher must ignore ev.repeat, or a held-down "
        "key re-fires its action every few milliseconds"
    )


@pytest.mark.anyio
async def test_the_page_carries_the_role_and_loads_the_scripts(rdb, rclient):
    await make_user(rdb, "boss", role="admin")
    login(rclient, "boss")
    html = rclient.get("/review", headers={"Accept": "text/html"}).text
    assert 'data-role="admin"' in html and 'id="rv-single"' in html
    assert "/static/review.css?v=" in html
    for name in SCRIPTS:
        assert f"/static/{name}?v=" in html


# ---- spec amendment 2026-09-25: frame-first review -----------------------------


def test_the_queue_is_asked_in_one_place_and_spot_checks_stay_single():
    for name in ("review-single.js", "review-frame.js", "review-browse.js"):
        assert "/api/review/next" not in _read(
            name
        ), f"{name} must go through RV.next(), not call /api/review/next itself"
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
        assert (
            "localStorage" in body and "try" in body and "catch" in body
        ), f"RV.{name}() must guard every storage access"
    # Fix round 1, Minor B: a blocked write must still be remembered for
    # this session, or F in the frame view can never reach single boxes
    # (setMode("single") silently drops and mode() falls back to "frame").
    assert re.search(
        r"RV\.modeValue\s*=\s*value", _function_body(core, "setMode")
    ), "setMode() must keep an in-memory copy before the storage write can fail"
    # Fix round 2, Minor (carried): RV.modeValue is only ever set by setMode
    # in this page's lifetime, so it must win over whatever storage holds --
    # otherwise a readable-but-unwritable store (a stale "frame" read
    # succeeds while the "single" write silently fails) overrides the
    # in-memory choice instead of the other way around.
    mode_body = _function_body(core, "mode")
    assert re.search(
        r"if \(RV\.modeValue\) return RV\.modeValue;", mode_body
    ), "mode() must prefer the in-memory value over a stored one"
    # Anchored on the literal `if (RV.modeValue)` check, not the first
    # mention of "RV.modeValue" anywhere in the function -- the preceding
    # comment also says "RV.modeValue", which would satisfy `.index()`
    # regardless of where the real check sits relative to the storage read.
    assert mode_body.index("if (RV.modeValue)") < mode_body.index(
        "localStorage"
    ), "the in-memory check must run before the storage read, not after it"
    assert 'return "frame"' in mode_body, "whole frame is still the ultimate fallback"


def test_opening_a_frame_keeps_the_box_the_queue_just_served():
    assert re.search(
        r"RV\.releaseHeld\(\s*id\s*\)", _function_body(_read("review-frame.js"), "enter")
    )
    assert "held.savedFrameId === keepFrameId" in _function_body(
        _read("review-core.js"), "releaseHeld"
    )


def test_a_frame_that_will_not_open_does_not_ask_the_queue_again():
    body = _function_body(_read("review-frame.js"), "enter")
    at = body.index("if (!r.ok)")
    failure = _braces_from(body, body.index("{", at))
    assert "RV.next(" not in failure and "arg.payload" in failure


def test_reloading_a_frame_refreshes_the_claims():
    body = _function_body(_read("review-frame.js"), "reload")
    # Fix round 1, Important 1(a): reload() now captures the frame id it
    # started with (`const id = state.saved_frame_id;`) so it can tell a
    # leave-while-in-flight apart from still being on the same frame; the
    # claiming POST uses that captured id rather than re-reading state.
    assert "RV.post(`/api/review/frames/${id}/open`)" in body


def test_completing_a_frame_asks_for_the_next_one_outside_the_busy_guard():
    body = _function_body(_read("review-frame.js"), "complete")
    fin = body.index("finally")
    assert "RV.next()" not in body[:fin]
    tail = body[fin:]
    assert (
        "RV.next()" in tail[tail.index("}") + 1 :]
    ), "complete() must call RV.next() after busy is reset, or enter() refuses it"


def test_leaving_the_frame_view_releases_its_boxes():
    body = _function_body(_read("review-core.js"), "show")
    assert re.search(r'if \(view !== "frame"\) RV\.releaseFrame\(\)', body)


def test_switching_lanes_releases_before_asking_the_queue():
    core = _read("review-core.js")
    marker = 'document.addEventListener("DOMContentLoaded"'
    block = _braces_from(core, core.index("{", core.index(marker)))
    # Fix round 1, Minor C: scanning the whole DOMContentLoaded block let the
    # page-load `await RV.next();` satisfy this on its own, without ever
    # looking at the click listener or the order within it.
    click_marker = 'addEventListener("click"'
    click_block = _braces_from(block, block.index("{", block.index(click_marker)))
    assert "await RV.releaseAll()" in click_block and "RV.next()" in click_block
    assert click_block.index("await RV.releaseAll()") < click_block.index(
        "RV.next()"
    ), "the lane button must release before it asks the queue again"


def test_closing_the_tab_releases_the_frame_too():
    core = _read("review-core.js")
    marker = 'window.addEventListener("pagehide"'
    block = _braces_from(core, core.index("{", core.index(marker)))
    assert "keepalive: true" in block and "RV.openFrameId" in block and "RV.held" in block
    # Fix round 1, Minor D: carried item (c) is correct in the code but was
    # unprotected -- dropping the header would 403 both releases silently.
    assert "headers: RV.HEADERS" in block, "the release calls must send the CSRF header"


# ---- fix round 1 (opus review of f614479..dc65ba2) ------------------------------


# Important 1(a): reload() started POSTing /open (a claim, not a plain read) so
# it can refresh this volunteer's claims mid-frame. But if the volunteer has
# already left the frame by the time a late reload's request lands -- F, a
# lane button, Browse, or "Change my answer" raced an in-flight answer -- that
# late reload must not re-claim the frame's boxes with nothing left to
# release them. It must check before sending the claiming POST, and again
# after, in case the leave happened while the request was in flight.
def test_reload_does_not_reclaim_a_frame_the_volunteer_already_left():
    body = _function_body(_read("review-frame.js"), "reload")
    pre, sep, post = body.partition("await RV.post")
    assert sep, "reload() must still claim via RV.post"
    assert re.search(
        r"if \(RV\.openFrameId !== id\) return;", pre
    ), "reload() must skip the re-open once the volunteer has already left this frame"
    assert re.search(
        r"if \(RV\.openFrameId !== id\)", post
    ), "reload() must notice a leave that happened while its POST was in flight"
    assert (
        "/api/review/frames/${id}/release" in post
    ), "a late reload must give back the claims it just re-acquired, not strand them"


# Important 1(b): the fix above stops a late reload from re-claiming a frame
# the volunteer already left, but the lane/Browse buttons must also not
# start a leave while an answer is still in flight in the frame view --
# otherwise /next claims a fresh box for a view that then refuses to render
# it (frame.enter()'s own busy guard), and the served box is held but never
# shown. review-frame.js exposes isBusy() so the nav handler can check it.
# Fix round 2: the RV.view === "frame" condition was dropped from the
# guard -- a lane click while the frame view is idle (busy already false,
# but RV.view may since have flipped to something else via "Change my
# answer") must still be caught by checking RV.views.frame.isBusy() on its
# own, not gated on which view happens to be showing right now.
def test_leaving_the_frame_view_waits_for_an_in_flight_action():
    frame_src = _read("review-frame.js")
    assert re.search(
        r"return\s*\{\s*enter\s*,\s*isBusy\b", frame_src
    ), "review-frame.js must expose isBusy alongside enter"
    core = _read("review-core.js")
    marker = 'document.addEventListener("DOMContentLoaded"'
    block = _braces_from(core, core.index("{", core.index(marker)))
    click_marker = 'addEventListener("click"'
    click_block = _braces_from(block, block.index("{", block.index(click_marker)))
    guard = re.search(r"if \([^\n]*isBusy\(\)\s*\)\s*return;", click_block)
    assert guard, "the nav click handler must ignore a click while the frame view is busy"
    assert re.search(
        r"RV\.views\.frame\s*&&\s*RV\.views\.frame\.isBusy\s*&&\s*" r"RV\.views\.frame\.isBusy\(\)",
        guard.group(0),
    ), "the guard must check the frame view's busy state directly"
    assert "RV.view ===" not in guard.group(0), (
        'the guard must not also require RV.view === "frame" -- a stale, no-longer-current '
        "frame view can still be busy and must still be caught"
    )


# Minor A (carried, fix round 1): pressing F in single view passed no
# payload, so if the frame it tries to open then fails, the volunteer sees
# a bare error and loses the box they were on (though it stays held). It
# must fall back to showing that same box again -- unless it's a reopened
# (read-only "change my answer") box, which was never a live queue claim
# and has no payload of its own to fall back to. The mode should only be
# remembered as "frame" once the frame has actually opened.
def test_f_in_single_view_falls_back_to_the_held_box_if_the_frame_wont_open():
    body = _function_body(_read("review-single.js"), "openFrame")
    assert re.search(r"if \(frameAllowed\(current\)\)", body), "openFrame() must check frameAllowed"
    assert re.search(
        r"if \(!reopened\)\s*arg\.payload\s*=\s*current;", body
    ), "openFrame() must offer the current box as a fallback payload, unless it's reopened"
    assert 'RV.setMode("frame")' in body, "F must still remember the frame preference"
    assert body.index('RV.show("frame"') < body.index(
        'RV.setMode("frame")'
    ), "the mode must only be remembered once the frame has actually opened"


# ---- fix round 2 (re-review of dc65ba2..6eb069c) ---------------------------------

# Important 1, still open after round 1: round 1's isBusy guard only closed
# "answer in flight, then the volunteer leaves". The reverse still failed --
# a lane button clicked while the frame view is idle releases RV.openFrameId,
# but the stale frame stays on screen and keeps taking keys; an answer
# started there sets busy; RV.next() then claims a box in a new frame and
# that frame's enter() drops it at its own busy early-return, so the served
# box is held but never shown. Every frame action that sends a request must
# refuse once RV.openFrameId no longer names the frame it belongs to -- a
# leave that has already released the frame (even if the old view is still
# what's on screen) must stop any of these from starting. The check must
# come before the busy check so the existing busy-guard tests still match
# (FRAME_MUTATING_HANDLERS parametrizes over the same function names).
FRAME_LEFT_GUARD = r"if \(!state \|\| RV\.openFrameId !== state\.saved_frame_id\) return;"
FRAME_LEAVE_AWARE_ACTIONS = (
    "answerModel",
    "saveAddedBox",
    "removeAddedBox",
    "complete",
    "toSingle",
    "nextFrame",
    "reopenFrame",
)


@pytest.mark.parametrize("name", FRAME_LEAVE_AWARE_ACTIONS)
def test_frame_actions_refuse_once_the_frame_has_been_left(name):
    body = _function_body((STATIC / "review-frame.js").read_text(), name)
    left_guard = re.search(FRAME_LEFT_GUARD, body)
    assert left_guard, f"{name}() must refuse once RV.openFrameId no longer matches this frame"
    busy_check = re.search(r"\bbusy\b\s*\)\s*return", body)
    assert busy_check, f"{name}() must still return early when busy is already true"
    assert (
        left_guard.start() < busy_check.start()
    ), f"{name}()'s openFrameId guard must come before its busy check"


# ---- Task 4: "Next frame", per-box progress, clickable preview boxes -------------


# S / "Next frame" (spec amendment 2026-09-25, flow step 3): skip the boxes
# this volunteer hasn't answered and ask the queue for the next one, outside
# the busy guard so RV.next() can re-enter this view (same shape as
# complete(), above). Controller ruling 1 (Task 3's fix rounds landed after
# this brief was written): nextFrame() also needs the leave guard every
# other request-sending frame action has, checked in
# test_frame_actions_refuse_once_the_frame_has_been_left via
# FRAME_LEAVE_AWARE_ACTIONS above.
def test_next_frame_skips_and_asks_for_the_next_one_outside_the_busy_guard():
    src = _read("review-frame.js")
    body = _function_body(src, "nextFrame")
    assert "/skip`" in body
    fin = body.index("finally")
    assert "RV.next()" not in body[:fin]
    tail = body[fin:]
    assert "RV.next()" in tail[tail.index("}") + 1 :]
    assert re.search(r'k === "s"\) \{ nextFrame\(\)', src), "S must be Next frame"


# The progress bar ("Counts toward <species>") moves into review-core.js so
# both views render it the same way instead of each keeping its own copy.
def test_the_progress_bar_is_shared_by_both_views():
    assert "RV.progressBar = function progressBar" in _read("review-core.js")
    single = _read("review-single.js")
    assert "function progressBar" not in single and "RV.progressBar(" in single
    assert "RV.progressBar(b.counts_toward)" in _read("review-frame.js")


# The single-box view's frame preview shows the other fish in the frame;
# clicking one now opens the whole frame with that box selected -- except on
# a spot-check, where the frame view would leak the other boxes' answers
# into a supposedly blind second opinion (frameAllowed(c) is the same gate
# the "Whole frame" button and the F key already use).
def test_preview_boxes_open_the_frame_except_on_a_spot_check():
    body = _function_body(_read("review-single.js"), "frameThumb")
    assert "const clickable = frameAllowed(c)" in body
    assert "openFrame(o.event_id)" in body


# Controller ruling 2: "Change my answer" (in review-frame.js's modelPanel)
# is the one way out of a frame with no re-entrancy guard on it -- it calls
# RV.show("single", ...) directly from an onclick, bypassing every other
# frame action's busy/left checks. Pinned here rather than folded into
# FRAME_LEAVE_AWARE_ACTIONS because it is not a named top-level function:
# it is an inline handler inside modelPanel().
def test_change_my_answer_refuses_once_the_frame_has_been_left():
    body = _function_body(_read("review-frame.js"), "modelPanel")
    at = body.index('RV.show("single", { reopenEventId: b.event_id })')
    onclick_start = body.rindex("onclick:", 0, at)
    onclick_body = body[onclick_start : body.index("}", at) + 1]
    assert re.search(
        r"if \(busy \|\| !state \|\| RV\.openFrameId !== state\.saved_frame_id\) return;",
        onclick_body,
    ), (
        '"Change my answer" must refuse once this frame has been left (busy, or '
        "RV.openFrameId no longer names it)"
    )


# ---- final review fix wave (final-findings.md) -----------------------------------


# M1: pagehide also fires when the page goes into the back/forward cache, so
# its keepalive releases have already given the claims back by the time Back
# restores the page -- with RV.held and RV.openFrameId still naming them, and
# the first answer then refused with "this box was not served to you". A page
# restored from that cache must reload and ask the queue afresh.
def test_a_page_restored_from_the_back_forward_cache_starts_afresh():
    core = _read("review-core.js")
    marker = 'window.addEventListener("pageshow"'
    assert marker in core, "the page must listen for pageshow"
    block = _braces_from(core, core.index("{", core.index(marker)))
    assert re.search(
        r"if \(ev\.persisted\) window\.location\.reload\(\);", block
    ), "a page restored from the back/forward cache must reload, not act on released claims"


# M2: "answered" in frame_state means any verdict on the box, and the older
# /api/corrections form writes verdicts without closing pool items, so a
# completed frame can still hold one of this volunteer's claims. complete()
# must release the frame, not just forget it, and await the release before the
# finally (and so before RV.next()), or /next can hand that claim straight back
# as "held" -- Review Focus 3, the same reason toSingle() awaits its release.
def test_completing_a_frame_releases_it_rather_than_forgetting_it():
    body = _function_body(_read("review-frame.js"), "complete")
    assert "RV.openFrameId = null" not in body, "complete() must release the frame, not forget it"
    fin = body.index("} finally {")
    assert re.search(
        r"await RV\.releaseFrame\(\);", body[:fin]
    ), "complete() must await RV.releaseFrame() inside the busy guard, before RV.next()"
    # A second click on "Mark frame complete" is refused by the leave guard
    # only because releaseFrame() nulls RV.openFrameId before its request.
    release = _function_body(_read("review-core.js"), "releaseFrame")
    assert release.index("RV.openFrameId = null") < release.index(
        "RV.post("
    ), "releaseFrame() must forget the frame before it sends the release"


# M3: every frame action's leave guard compares RV.openFrameId with this
# frame's id, so the one assignment in enter()'s success path is what keeps
# the whole frame view working: lose it and every answer, draw, complete, S and
# F silently does nothing. It must sit after the failure path's early return:
# set before the request, a failed open would leave RV.openFrameId naming the
# frame, and the fallback's next leave would release the served box it keeps.
def test_opening_a_frame_records_it_as_the_open_frame():
    body = _function_body(_read("review-frame.js"), "enter")
    at = body.index("if (!r.ok)")
    brace = body.index("{", at)
    failure = _braces_from(body, brace)
    success = body[brace + len(failure) : body.index("finally")]
    for statement in ("state = r.data;", "RV.held = null;", "RV.openFrameId = id;"):
        assert statement in success, f"enter()'s success path must run `{statement}`"
    assert (
        "RV.openFrameId = id" not in body[:at]
    ), "enter() must not record the frame as open before it has actually opened"


# I1 (F1, as ruled): a navigation made while RV.next() is in flight must win.
# Every view switch takes a new RV.navToken, so a /next that comes back after
# the volunteer moved on (Browse, another lane, a reopened box) neither
# switches the view nor keeps its box.
def test_every_view_switch_supersedes_a_pending_next():
    body = _function_body(_read("review-core.js"), "show")
    assert re.search(
        r"if \(!RV\.views\[view\]\) return;\s*RV\.navToken \+= 1;", body
    ), "RV.show() must take a new navToken right after its view-exists check"


# ...and only one /next is out at a time. Each call waits for the one before it
# to hand over its turn, which a superseded call does only once its release has
# landed: otherwise the server's held path hands the same box to both calls (the
# same lane, or a spot-check from any lane) and the stale release takes it away
# from the call that shows it. The turn is handed on in a finally before any
# RV.show(), because a view's enter() may call RV.next() itself.
def test_a_superseded_next_gives_its_box_back_and_shows_nothing():
    core = _read("review-core.js")
    assert re.search(r"^RV\.navToken = 0;$", core, re.M), "RV.navToken must start at 0"
    assert re.search(
        r"^RV\.nextTurn = Promise\.resolve\(\);$", core, re.M
    ), "RV.nextTurn must start settled"
    body = _function_body(core, "next")
    get = body.index("RV.get(")
    first_show = body.index("RV.show(")
    take = body.index("const token = ++RV.navToken;")
    turn = body.index("const turn = RV.nextTurn;")
    chain = body.index("RV.nextTurn = new Promise(")
    wait = body.index("await turn;")
    assert (
        take < turn < chain < wait < get
    ), "RV.next() must take its token and queue behind the previous call before it asks"
    assert re.search(
        r"if \(token !== RV\.navToken\) return;", body[wait:get]
    ), "a call superseded while it waited its turn must not ask the queue at all"
    at = body.index("if (token !== RV.navToken) {", get)
    block = _braces_from(body, body.index("{", at))
    assert (
        "await RV.post(`/api/review/items/${r.data.item.id}/release`)" in block
    ), "a call superseded while its GET was out must give back -- and await -- the box it got"
    assert "return;" in block and at < first_show, "...and return before RV.show()"
    fin = body.index("} finally {")
    handoff = body.index("handOff();", fin)
    assert (
        get < fin and handoff < first_show
    ), "the turn must be handed on in a finally, before any RV.show()"


# H2: RV.show() counts every call as a navigation, so a reopen the single view
# would refuse (its busy flag is set while submit()/skip() await their trailing
# RV.next()) must not reach RV.show() at all -- or it supersedes that RV.next()
# and leaves the page on "Loading the next box…" with nothing held.
def test_a_recent_box_is_not_reopened_while_an_answer_is_in_flight():
    body = _function_body(_read("review-single.js"), "renderRecent")
    at = body.index('RV.show("single", { reopenEventId: r.event_id })')
    onclick = body[body.rindex("onclick:", 0, at) : at]
    assert re.search(
        r"if \(busy\) return;", onclick
    ), "a recent-strip click must be ignored while the single view is busy"


# Demo walk-through 2026-09-26: the dashboard's style.css gives every <main> a
# two-column grid (3fr 2fr), so the review page sat in the left 3/5 of the
# screen beside an empty column. The review page's own main is one centred
# column instead.
def test_the_review_page_is_one_centred_column():
    css = _read("review.css")
    m = re.search(r"\.rv-main\s*\{([^}]*)\}", css)
    assert m, "review.css must style .rv-main"
    rule = m.group(1)
    assert re.search(r"display:\s*block", rule), ".rv-main must drop the dashboard's main grid"
    assert re.search(r"margin:\s*0 auto", rule), ".rv-main must be centred"
    assert re.search(r"max-width:\s*\d+px", rule), ".rv-main needs a width cap to centre"


# Demo walk-through 2026-09-26: times were formatted in the viewer's own zone
# (a frame at 10:40:56 EDT showed as 7:40:56 AM on a Pacific-time laptop).
# Every time on the review page goes through RV.fmtTime, which always uses the
# site's zone (<html data-tz>, from TZ; the browser applies daylight saving).
def test_every_time_is_shown_in_the_site_zone():
    core = _read("review-core.js")
    body = _function_body(core, "fmtTime")
    assert "timeZone: RV.TIME_ZONE" in body
    assert "document.documentElement.dataset.tz" in core
    outside = core.replace(body, "")
    for name in ("review-core.js", "review-single.js", "review-frame.js", "review-browse.js"):
        src = outside if name == "review-core.js" else _read(name)
        assert not re.search(
            r"toLocale(String|TimeString|DateString)\(", src
        ), f"{name} formats a time without RV.fmtTime"


# Demo walk-through 2026-09-26: review-page colours outside style.css's :root
# palette are variables on .rv-page (dark defaults), so a theme class can swap
# them; only colours drawn over the camera frames stay fixed.
def test_review_colours_are_theme_variables():
    css = _read("review.css")
    page = re.search(r"\.rv-page\s*\{([^}]*)\}", css).group(1)
    assert "--rv-line: var(--line, #1f3b4f)" in page
    assert css.count("#1f3b4f") == 1, "border colours must use var(--rv-line)"
    for literal in ("#173049", "#5a2a14", "#ffe2cf", "#ff9a9a", "#062a14"):
        assert css.count(literal) == 1, f"{literal} must only appear as a .rv-page default"


# Demo walk-through 2026-09-26: the whole-frame instructions were a faint grey
# caption. They are now a call-out under the frame with a legend whose swatches
# use the same colours and line styles as the boxes drawn on the frame.
def test_the_frame_instructions_are_a_callout_with_a_legend():
    src = _read("review-frame.js")
    body = _function_body(src, "legend")
    assert 'class: "rv-callout"' in body and 'role: "note"' in body
    assert "rv-swatch rv-swatch-${state}" in body
    for state in ("pending", "done", "added", "locked"):
        assert f'swatch("{state}", ' in body, f"the legend needs the {state} box style"
    assert 'kbd("S")' in body and 'kbd("D")' in body
    assert "legend()" in _function_body(src, "render")
    css = _read("review.css")
    for state, colour in (
        ("pending", "#ffc400"),
        ("done", "#34c759"),
        ("added", "#3ad1ff"),
        ("locked", "#8aa3b5"),
    ):
        rule = re.search(r"\.rv-swatch-" + state + r"\s*\{([^}]*)\}", css)
        assert rule and colour in rule.group(1), f"the {state} swatch must match its frame box"


# ---- Task 6: navigation waits its turn and gives way, and a stalled request
# times out (B1, B3, B4) -----------------------------------------------------


@needs_node
def test_next_gives_way_once_the_volunteer_has_moved_on(tmp_path):
    out = run_node(
        [_read("review-core.js")],
        """
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
    """,
        tmp_path,
    )
    assert out == {"stale": {"asked": 0, "moved": 1}, "asked": 1}


@needs_node
def test_a_request_that_never_answers_times_out_and_frees_the_queue(tmp_path):
    out = run_node(
        [_read("review-core.js")],
        """
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
    """,
        tmp_path,
    )
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
    out = run_node(
        [_read("review-core.js"), _read("review-frame.js")],
        """
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
    """,
        tmp_path,
    )
    assert out == {"before": [], "after": ["/api/review/frames/5/open"]}


@needs_node
def test_a_frame_the_volunteer_navigated_away_from_while_waiting_is_never_claimed(tmp_path):
    out = run_node(
        [_read("review-core.js"), _read("review-frame.js")],
        """
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
    """,
        tmp_path,
    )
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


# ---- Task 7: preview clicks keep the mode, Browse waits, reopening shows your
# answer, admins reopen a completed frame (A3, B2, B5, B6) --------------------


def test_a_preview_click_keeps_single_box_mode():
    body = _function_body(_read("review-single.js"), "openFrame")
    assert 'if (!eventId && RV.openFrameId === savedFrameId) RV.setMode("frame");' in body


def test_browse_cells_wait_for_a_view_that_is_still_saving():
    body = _function_body(_read("review-browse.js"), "open")
    guard = (
        'for (const v of ["single", "frame"]) '
        "if (RV.views[v] && RV.views[v].isBusy && RV.views[v].isBusy()) return;"
    )
    assert guard in body and body.index(guard) < body.index("RV.show(")
    assert "isBusy: () => busy" in _read("review-single.js")


@needs_node
def test_reopening_reads_back_what_you_answered(tmp_path):
    src = _read("review-single.js")
    assert "mineText(c.mine)" in _function_body(src, "render")
    out = run_node(
        [
            "RV = { entry: (n) => (n === 'Acanthurus coeruleus' ? { common: 'Blue Tang' } : null) };",
            function_source(src, "mineText"),
        ],
        """
      return [
        mineText({ outcome: "species", label: "Acanthurus coeruleus", life_stage: null,
                   not_sure: true, bbox: [1, 2, 30, 40] }),
        mineText({ outcome: "species", label: "Pomacanthus paru", life_stage: "juvenile",
                   not_sure: false, bbox: null }),
        mineText({ outcome: "no_animal" }),
        mineText({ outcome: "unidentifiable" })];
    """,
        tmp_path,
    )
    assert out == [
        "Blue Tang, not sure; the box shows where you put it",
        "Pomacanthus paru (juvenile)",
        "no animal here",
        "can't identify",
    ]


def test_admins_can_edit_and_reopen_a_completed_frame():
    src = _read("review-frame.js")
    assert (
        'if (!selected || (state.complete && RV.role !== "admin")) return false;'
        in _function_body(src, "editableSelection")
    )
    assert '(state.complete && RV.role !== "admin")' in _function_body(src, "addedPanel")
    assert 'if (state.complete && RV.role !== "admin") return;' in _function_body(src, "toggleDraw")
    panel = _function_body(src, "panelContent")
    assert '"Reopen frame for edits"' in panel and 'RV.role === "admin"' in panel
    reopen = _function_body(src, "reopenFrame")
    assert "RV.del(`/api/review/frames/${state.saved_frame_id}/complete`)" in reopen
    assert "await reload(true)" in reopen


# ---- Task 7, ruling T6a: every entry point into RV.show("single", { reopenEventId })
# must refuse while the single view is still saving, the way renderRecent's click
# already does -- otherwise RV.show has already bumped the token before reopen()
# refuses, and the trailing loadNext(nav) from the answer/skip in flight gives way
# too, stranding the just-answered box on screen with nothing held.


@pytest.mark.parametrize(
    "single_busy, expected",
    [
        (True, {"entered": False, "moved": 0}),
        (False, {"entered": True, "moved": 1}),
    ],
)
@needs_node
def test_browse_open_refuses_a_reopen_while_the_single_view_is_busy(
    single_busy, expected, tmp_path
):
    out = run_node(
        [
            _read("review-core.js"),
            f'RV.views.single = {{ isBusy: () => {"true" if single_busy else "false"}, '
            "enter: async () => { globalThis.entered = true; } };",
            function_source(_read("review-browse.js"), "open"),
        ],
        """
      globalThis.entered = false;
      const before = RV.navToken;
      open({ kind: "box", mine: true, event_id: 99 });
      await new Promise((r) => setTimeout(r, 10));
      return { entered: globalThis.entered, moved: RV.navToken - before };
    """,
        tmp_path,
    )
    assert out == expected


def test_change_my_answer_also_refuses_while_the_single_view_is_busy():
    body = _function_body(_read("review-frame.js"), "modelPanel")
    at = body.index('RV.show("single", { reopenEventId: b.event_id })')
    onclick_start = body.rindex("onclick:", 0, at)
    between = body[onclick_start:at]
    assert "RV.views.single.isBusy()" in between


# ---- 2026-11-21: mark the rest (M) and changing an answer in the frame ----------

FRAME_STUB = r"""
  globalThis.Node = Object;
  document.createTextNode = (t) => ({ text: t });
  document.createElementNS = (ns, tag) => document.createElement(tag);
  const settle = async () => { for (let i = 0; i < 30; i++) await new Promise((r) => setTimeout(r, 0)); };
  const box = (n, state, extra) => Object.assign({ n, event_id: 100 + n, bbox: [10 * n, 10, 10 * n + 8, 18],
    state, mine: false, own_spot: false, my_bbox: null, can_change: false, review_item_id: null,
    zoom: { url: "/z.jpg" }, outcome: null, label: null, common: null, model: null, hints: null,
    counts_toward: null }, extra);
  const GRUNT = { name: "Haemulon chrysargyreum", common: "Smallmouth grunt", p: 0.8 };
  const frame = (id) => ({ saved_frame_id: id, source_name: "seahive", ts: "2026-06-16T12:00:00Z",
    frame: { url: "/f.jpg", width: 1280, height: 720 }, added: [], complete: null, blocking: [1, 2, 4],
    boxes: [box(1, "needs_answer", { model: GRUNT, review_item_id: 11 }), box(2, "needs_answer"),
      box(3, "answered", { mine: true, can_change: true, outcome: "species", label: "Haemulon aurolineatum",
        common: "Tomtate", model: GRUNT, my_bbox: [31, 11, 40, 19] }),
      box(4, "locked")] });
  RV.catalogue = [{ key: "Haemulon chrysargyreum|", name: "Haemulon chrysargyreum",
    common: "Smallmouth grunt", life_stage: null }];
  RV.byKey = new Map(RV.catalogue.map((e) => [e.key, e]));
  const calls = [];
  globalThis.fetch = async (url, opts) => {
    const body = opts && opts.body ? JSON.parse(opts.body) : null;
    calls.push({ url, body });
    let data = { ok: true };
    const m = url.match(/^\/api\/review\/frames\/(\d+)\/open$/);
    if (m) data = frame(Number(m[1]));
    else if (url.endsWith("/mark-rest")) data = globalThis.markReply || { marked: 2, skipped: 0 };
    else if (url === "/api/review/mine/recent") data = { session_count: 0, recent: [] };
    return { status: 200, ok: true, json: async () => data };
  };
  const key = (k) => RV.keyHandlers.frame(k);
  const posted = () => calls.filter((c) => !c.url.endsWith("/open") && c.url !== "/api/review/mine/recent");
"""


@needs_node
def test_m_marks_the_rest_with_the_last_answer_after_asking_once(tmp_path):
    out = run_node(
        [_read("review-core.js"), _read("review-frame.js")],
        FRAME_STUB
        + """
      await RV.views.frame.enter({ savedFrameId: 7 });
      const before = key("m");                 // nothing answered in this frame yet
      key("a"); await settle();                // "Looks right" on box 1
      key("m"); await settle();                // asks first ...
      const asked = posted().length;
      key("Escape"); key("Enter"); await settle();   // ... cancelled, and Enter alone does nothing
      key("m"); key("m"); await settle();      // asked again, and confirmed
      key("m"); key("Enter"); await settle();  // Enter confirms too
      return { before, asked, posted: posted() };
    """,
        tmp_path,
    )
    accept, mark, mark2 = out["posted"]
    assert out["before"] is False
    assert accept == {
        "url": "/api/review/verdicts",
        "body": {"event_id": 101, "review_item_id": 11, "outcome": "accept", "not_sure": False},
    }
    assert out["asked"] == 1  # the first M only asked
    body = {"label": "Haemulon chrysargyreum", "life_stage": None, "not_sure": False}
    assert mark == mark2 == {"url": "/api/review/frames/7/mark-rest", "body": body}


@needs_node
def test_the_last_answer_is_forgotten_in_another_frame(tmp_path):
    out = run_node(
        [_read("review-core.js"), _read("review-frame.js")],
        FRAME_STUB
        + """
      await RV.views.frame.enter({ savedFrameId: 7 });
      key("a"); await settle();
      await RV.views.frame.enter({ savedFrameId: 8 });
      const handled = key("m"); key("m"); await settle();
      return { handled, marks: posted().filter((c) => c.url.endsWith("/mark-rest")).length };
    """,
        tmp_path,
    )
    assert out == {"handled": False, "marks": 0}


@needs_node
def test_changing_an_answer_stays_on_that_box_in_the_frame(tmp_path):
    out = run_node(
        [_read("review-core.js"), _read("review-frame.js")],
        FRAME_STUB
        + """
      await RV.views.frame.enter({ savedFrameId: 7, selectEventId: 103 });
      key("c"); await settle();                // change box 3 to "can't identify" ...
      key("n"); await settle();                // ... and again: still box 3, still this frame
      const shown = RV.view;
      return { posted: posted(), opens: calls.filter((c) => c.url.endsWith("/open")).length };
    """,
        tmp_path,
    )
    first, second = out["posted"]
    assert first["url"] == "/api/review/verdicts"
    assert first["body"]["event_id"] == 103 and first["body"]["review_item_id"] is None
    assert first["body"]["outcome"] == "unidentifiable"
    assert first["body"]["bbox"] == [31, 11, 40, 19]  # the box drawn on the earlier answer is kept
    assert second["body"]["event_id"] == 103 and second["body"]["outcome"] == "no_animal"
    assert "bbox" not in second["body"]  # a box goes with no 'no animal' answer
    assert out["opens"] == 3  # entered once, reloaded after each change


def test_a_second_opinion_you_gave_keeps_the_single_view_change():
    panel = _function_body(_read("review-frame.js"), "modelPanel")
    assert "b.own_spot" in panel and "reopenEventId: b.event_id" in panel


@needs_node
def test_enter_on_a_focused_button_never_confirms_the_mark(tmp_path):
    """Final review, Important 1: with the question up, Enter pressed while a
    button has focus (Cancel, Next frame, a chip) is that button's own key."""
    out = run_node(
        [_read("review-core.js"), _read("review-frame.js")],
        FRAME_STUB
        + """
      await RV.views.frame.enter({ savedFrameId: 7 });
      key("a"); await settle();
      key("m"); await settle();
      const handled = RV.keyHandlers.frame("Enter", { target: { tagName: "BUTTON" } });
      await settle();
      return { handled, marks: posted().filter((c) => c.url.endsWith("/mark-rest")).length };
    """,
        tmp_path,
    )
    assert out == {"handled": False, "marks": 0}


@needs_node
def test_the_toast_explains_boxes_the_mark_did_not_reach(tmp_path):
    """Final review, 2 and 3: the question named two boxes; one was taken by
    another volunteer before the mark landed (the server never tried it, so it
    is not in `skipped`). The toast says so, without guessing at blame."""
    out = run_node(
        [_read("review-core.js"), _read("review-frame.js")],
        FRAME_STUB
        + """
      globalThis.markReply = { marked: 1, skipped: 0 };
      await RV.views.frame.enter({ savedFrameId: 7 });
      key("a"); await settle();
      key("m"); key("m"); await settle();
      return document.getElementById("rv-toast").textContent;
    """,
        tmp_path,
    )
    assert out == (
        "Marked 1 box as Smallmouth grunt. 1 couldn't be marked; "
        "it may be taken by another volunteer. See the frame."
    )


HINT = "Answer one box with a species, then press M to mark the rest the same way."


@needs_node
def test_the_frame_says_how_to_mark_the_rest_until_a_species_is_given(tmp_path):
    """Dan, 2026-11-21: two "No animal" answers and no sign the feature
    existed. Until a species is given in the frame, the panel says how."""
    out = run_node(
        [_read("review-core.js"), _read("review-frame.js")],
        FRAME_STUB
        + """
      const text = (n) => (!n || typeof n !== "object") ? ""
        : (n.textContent || n.text || "") + (n.children || []).map(text).join(" ");
      const shown = () => text(document.getElementById("rv-frame"));
      await RV.views.frame.enter({ savedFrameId: 7 });
      const first = shown();
      key("n"); await settle();                 // "No animal" is not a species
      const afterNone = shown();
      key("a"); await settle();                 // "Looks right" is
      const afterSpecies = shown();
      return { first, afterNone, afterSpecies };
    """,
        tmp_path,
    )
    assert HINT in out["first"] and HINT in out["afterNone"]
    assert HINT not in out["afterSpecies"]
    assert "Mark the 2 unanswered boxes as Smallmouth grunt" in out["afterSpecies"]


# ---- reviewers add a species, checked with WoRMS (spec 2026-11-21-species-worms-check) -------
# "Not on the list? Add a species" opens a form under every picker -- on blind second opinions
# and boxes a volunteer drew too. "Check with WoRMS" asks WoRMS from the browser
# (review-worms.js); the record chosen is posted, and the box answered with the new species.
# SUGGEST_STUB goes after FRAME_STUB: elements keep their listeners and attributes (ELEMENT_STUB,
# shared with the Browse admin tests below), so a test can click the link, fill the form, check
# and add. WoRMS is stubbed by URL, never called: `worms[path]` is { status, data } (204, nothing
# found, when unset), "reject" (a transport error) or "hang" (no answer until aborted).

ELEMENT_STUB = r"""
  const plainEl = document.createElement;
  document.createElement = (tag) => Object.assign(plainEl(tag), {
    on: {}, attrs: {}, focus() {}, select() {}, blur() {},
    addEventListener(type, fn) { this.on[type] = fn; },
    setAttribute(k, v) { this.attrs[k] = v; } });
  const kids = (n) => (n && n.children) || [];
  const textOf = (n) => (!n || typeof n !== "object") ? ""
    : (n.textContent || n.text || "") + kids(n).map(textOf).join("");
  const find = (n, pred) => {
    if (!n || typeof n !== "object") return null;
    if (pred(n)) return n;
    for (const c of kids(n)) { const f = find(c, pred); if (f) return f; }
    return null;
  };
  const attr = (root, k, v) => find(root, (n) => n.attrs && n.attrs[k] === v);
  // What the picker in `root` offers for `query`.
  const offered = (root, query) => {
    const input = attr(root, "type", "search");
    input.value = query;
    input.on.input();
    return textOf(attr(root, "role", "listbox"));
  };
"""

SUGGEST_STUB = (
    ELEMENT_STUB
    + r"""
  const all = (n, pred, out = []) => {
    if (!n || typeof n !== "object") return out;
    if (pred(n)) out.push(n);
    for (const c of kids(n)) all(c, pred, out);
    return out;
  };
  const button = (root, text) => find(root, (n) => n.tag === "button" && textOf(n) === text);
  const addLink = (root) => button(root, "Not on the list? Add a species");
  const checkButton = (root) => button(root, "Check with WoRMS");
  const addButton = (root) => button(root, "Add and answer this box");
  const radios = (root) => all(root, (n) => n.attrs && n.attrs.type === "radio");
  const choices = (root) => all(root, (n) => n.className === "rv-worms-choice").map(textOf);
  const uses = (root) => all(root, (n) => n.tag === "button" && textOf(n).startsWith("Use ")).map(textOf);
  // Fill fields in, as typing (or choosing a stage) would.
  const fill = (root, fields) => {
    for (const [name, value] of Object.entries(fields)) {
      const field = attr(root, "name", name);
      field.value = value;
      (field.on.input || field.on.change)({});
    }
  };
  // Open the form under the picker in `root` and fill it in; Enter in a field checks with WoRMS.
  const openForm = (root, fields) => { addLink(root).on.click({}); fill(root, fields); };
  const checkForm = (root) => find(root, (n) => n.tag === "form").on.submit({ preventDefault() {} });
  const WORMS = "https://www.marinespecies.org/rest";
  globalThis.worms = {};
  const record = (id, name, extra) => Object.assign({ AphiaID: id, scientificname: name,
    authority: "(Bloch, 1793)", status: "accepted", rank: "Species", valid_AphiaID: id, valid_name: name,
    valid_authority: "(Bloch, 1793)", family: "Mullidae", match_type: "exact",
    url: `https://www.marinespecies.org/aphia.php?p=taxdetails&id=${id}` }, extra);
  const matchNames = (name) =>
    `/AphiaRecordsByMatchNames?scientificnames[]=${encodeURIComponent(name)}&marine_only=false`;
  const GOATFISH = record(126994, "Pseudupeneus maculatus");
  // The species added: the server's answer is its key and suggestion_id; the list reloaded
  // afterwards has it.
  const ADDED = { key: "Pseudupeneus maculatus|juvenile", name: "Pseudupeneus maculatus",
    life_stage: "juvenile", common: "Spotted Goatfish", aphia_id: 126994, rank: "Species",
    family: "Mullidae", resolved: true, in_species_log: false, in_classifier: false, celebrities: [] };
  // A species suggested under ai #49, still waiting for an admin: only its suggester picks it.
  const ENTRY = { key: "suggestion:4", name: "Pseudupeneus maculatus", life_stage: "juvenile",
    common: "Spotted Goatfish (waiting for approval)", suggestion_id: 4, pending: true,
    in_species_log: false, in_classifier: false };
  const listed = RV.catalogue.slice();
  globalThis.suggestReply = { status: 200, data: { key: ADDED.key, name: ADDED.name,
    life_stage: ADDED.life_stage, common: ADDED.common, suggestion_id: 9 } };
  globalThis.mine = [];
  globalThis.species = null;           // the list /api/review/species answers: [...listed, ADDED]
  const pageFetch = globalThis.fetch;
  globalThis.fetch = async (url, opts) => {
    const reply = (status, data) => ({ status, ok: status < 400, json: async () => data });
    const body = opts && opts.body ? JSON.parse(opts.body) : null;
    if (url.startsWith(WORMS)) {
      calls.push({ url, body, headers: (opts && opts.headers) || null,
        credentials: (opts && opts.credentials) || null });
      const answer = globalThis.worms[url.slice(WORMS.length)];
      if (answer === "reject") throw new TypeError("Failed to fetch");
      if (answer === "hang") {
        return new Promise((_, reject) =>
          opts.signal.addEventListener("abort", () => reject(new Error("aborted"))));
      }
      return answer ? reply(answer.status || 200, answer.data) : reply(204, null);
    }
    if (url === "/api/review/species-suggestions") {
      calls.push({ url, body });
      await globalThis.gate;               // a test may hold the reply back
      return reply(globalThis.suggestReply.status, globalThis.suggestReply.data);
    }
    if (url === "/api/review/species-suggestions/mine") {
      calls.push({ url, body });
      return reply(200, { suggestions: globalThis.mine });
    }
    if (url === "/api/review/species") {
      calls.push({ url, body });
      return reply(200, { species: globalThis.species || [...listed, ADDED] });
    }
    if (url === "/api/review/verdicts" && globalThis.verdictReply) {
      calls.push({ url, body });
      return reply(globalThis.verdictReply.status, globalThis.verdictReply.data);
    }
    return pageFetch(url, opts);
  };
  const sent = () => calls.filter((c) => c.body && !c.url.endsWith("/open"));
  // What the page asked for, from call `from` on: WoRMS (by path), our species and answer routes.
  const asked = (from) => calls.slice(from || 0).map((c) => c.url)
    .filter((u) => u.startsWith(WORMS) || u.startsWith("/api/review/species") || u === "/api/review/verdicts")
    .map((u) => (u.startsWith(WORMS) ? "WoRMS " + u.slice(WORMS.length) : u));
  // A box the queue served to the single view.
  const payload = (pass) => ({ item: { id: 11, event_id: 101, pass },
    box: { bbox: [10, 10, 50, 50], model_bbox: [10, 10, 50, 50], saved_frame_id: 7 },
    frame: { url: "/f.jpg", width: 1280, height: 720 }, zoom: { url: "/z.jpg", region: [0, 0, 200, 200] },
    other_boxes: [], model: null, hints: [], counts_toward: null, editable: true });
"""
)


def _page(*views: str) -> list[str]:
    """review-core.js, the WoRMS check, and the views named."""
    return [_read("review-core.js"), _read("review-worms.js"), *(_read(v) for v in views)]


GOATFISH_FORM = (
    '{ common_name: " Spotted Goatfish ", scientific_name: " Pseudupeneus  maculatus ", '
    'life_stage: "juvenile" }'
)
MATCH_GOATFISH = (
    "WoRMS /AphiaRecordsByMatchNames?scientificnames[]=Pseudupeneus%20maculatus&marine_only=false"
)
ADDITION = {
    "aphia_id": 126994,
    "scientific_name": "Pseudupeneus maculatus",
    "family": "Mullidae",
    "common_name": "Spotted Goatfish",
    "life_stage": "juvenile",
    "note": None,
}
ADDED_ANSWER = {
    "label": "Pseudupeneus maculatus",
    "life_stage": "juvenile",
    "label_source": "suggested",
    "species_suggestion_id": 9,
}
BOX_101 = {"event_id": 101, "review_item_id": 11, "outcome": "species"}
DOWN = "WoRMS isn't answering right now; try again in a minute, or answer Can't identify for now."
UNKNOWN = "WoRMS doesn't know this name. Check the spelling, or answer Can't identify for now."


@needs_node
def test_an_exact_worms_match_adds_and_answers_the_box(tmp_path):
    """Box 1 is answered with a listed species, then (the stub's frame never changes) with one
    WoRMS knows exactly: the form shows the record, adds it and answers the box, and "mark the
    rest" then offers the new species, not the earlier one."""
    out = run_node(
        _page("review-frame.js"),
        FRAME_STUB
        + SUGGEST_STUB
        + f"""
      worms[matchNames("Pseudupeneus maculatus")] = {{ data: [[GOATFISH]] }};
      await RV.views.frame.enter({{ savedFrameId: 7 }});    // box 1 is selected
      key("a"); await settle();                            // a listed species: M would mark with it
      const from = calls.length;
      const root = document.getElementById("rv-frame");
      openForm(root, {GOATFISH_FORM});
      const before = addButton(root).disabled;
      checkForm(root); await settle();
      const shown = textOf(root);
      addButton(root).on.click({{}}); addButton(root).on.click({{}});   // a double click adds it once
      await settle();
      const wormsCalls = calls.filter((c) => c.url.startsWith(WORMS));
      return {{ before, shown, asked: asked(from), sent: sent().slice(1),
        plain: wormsCalls.every((c) => !c.headers && !c.credentials),
        text: textOf(root), m: key("m") }};
    """,
        tmp_path,
    )
    assert out["before"] is True  # nothing to add until WoRMS has answered
    assert "✓ Pseudupeneus maculatus (Bloch, 1793) · Mullidae WoRMS ↗" in out["shown"]
    assert out["asked"] == [
        "/api/review/species-suggestions/mine",
        MATCH_GOATFISH,
        "/api/review/species-suggestions",
        "/api/review/species",  # the list is reloaded before answering
        "/api/review/verdicts",
    ]
    assert out["plain"]  # no custom header or cookie, so the browser sends WoRMS no preflight
    suggestion, verdict = out["sent"]
    assert suggestion == {"url": "/api/review/species-suggestions", "body": ADDITION}
    assert verdict == {
        "url": "/api/review/verdicts",
        "body": {**BOX_101, **ADDED_ANSWER, "not_sure": False},
    }
    assert "Mark the 2 unanswered boxes as Spotted Goatfish" in out["text"]
    assert out["m"] is True


@needs_node
def test_a_misspelling_offers_did_you_mean(tmp_path):
    """On a blind second opinion, which may add a species now that nothing waits for an admin.
    TAXAMATCH's fuzzy matches are offered as "Did you mean?": species only, each accepted species
    once, accepted ones first. Picking one enables Add once the form has filled the empty common
    name with the record's English name from WoRMS, in Title Case."""
    out = run_node(
        _page("review-single.js"),
        FRAME_STUB
        + SUGGEST_STUB
        + """
      worms[matchNames("Pseudupeneus maculatis")] = { data: [[
        record(500, "Upeneus maculatus", { match_type: "near_3", status: "unaccepted",
          valid_AphiaID: 126994, valid_name: "Pseudupeneus maculatus" }),
        record(126994, "Pseudupeneus maculatus", { match_type: "phonetic" }),
        record(126995, "Pseudupeneus prayensis", { match_type: "near_2", authority: "(Cuvier, 1829)",
          valid_authority: "(Cuvier, 1829)" }),
        record(126993, "Pseudupeneus", { match_type: "near_1", rank: "Genus" })]] };
      worms["/AphiaVernacularsByAphiaID/126994"] = { data: [
        { vernacular: "rouget-souris tacheté", language_code: "fra", language: "French" },
        { vernacular: "spotted goatfish", language_code: "eng", language: "English" }] };
      await RV.views.single.enter({ payload: payload(2) });   // a blind second opinion
      const root = document.getElementById("rv-single");
      openForm(root, { scientific_name: "Pseudupeneus maculatis", life_stage: "juvenile" });
      checkForm(root); await settle();
      const found = { text: textOf(root), choices: choices(root), ready: !addButton(root).disabled };
      radios(root)[0].on.change({}); await settle();
      const ready = !addButton(root).disabled;
      const named = attr(root, "name", "common_name").value;
      addButton(root).on.click({}); await settle();
      return { found, ready, named, asked: asked(), sent: sent() };
    """,
        tmp_path,
    )
    assert "Did you mean?" in out["found"]["text"]
    assert out["found"]["choices"] == [
        "Pseudupeneus maculatus (Bloch, 1793) · Mullidae WoRMS ↗",
        "Pseudupeneus prayensis (Cuvier, 1829) · Mullidae WoRMS ↗",
    ]
    assert out["found"]["ready"] is False and out["ready"] is True
    assert out["named"] == "Spotted Goatfish"
    assert out["asked"] == [
        "/api/review/species-suggestions/mine",
        "WoRMS /AphiaRecordsByMatchNames?scientificnames[]=Pseudupeneus%20maculatis&marine_only=false",
        "WoRMS /AphiaVernacularsByAphiaID/126994",
        "/api/review/species-suggestions",
        "/api/review/species",
        "/api/review/verdicts",
    ]
    suggestion, verdict = out["sent"]
    assert suggestion["body"] == ADDITION
    assert verdict["body"] == {**BOX_101, **ADDED_ANSWER, "not_sure": False}


@needs_node
def test_a_common_name_only_uses_the_vernacular_search(tmp_path):
    """A common name alone asks WoRMS's vernacular search, exactly first, then by prefix; the
    candidates are offered to confirm even when there is one. A new name clears the result."""
    out = run_node(
        _page("review-single.js"),
        FRAME_STUB
        + SUGGEST_STUB
        + """
      worms["/AphiaRecordsByVernacular/spotted%20goatfish?like=false"] = { data: [GOATFISH] };
      worms["/AphiaRecordsByVernacular/goatfish?like=true"] = { data: [
        record(1, "Mullus auratus"), record(2, "Mulloidichthys martinicus"), record(3, "Mullus surmuletus"),
        record(4, "Parupeneus barberinus"), record(5, "Upeneus parvus"), record(6, "Mullus barbatus"),
        record(7, "Mullidae", { rank: "Family" })] };
      await RV.views.single.enter({ payload: payload(1) });
      const root = document.getElementById("rv-single");
      openForm(root, { common_name: "spotted goatfish", life_stage: "juvenile" });
      checkForm(root); await settle();
      const one = { text: textOf(root), choices: choices(root), ready: !addButton(root).disabled };
      fill(root, { common_name: "goatfish" });
      const cleared = choices(root).length;
      checkForm(root); await settle();
      const many = choices(root).length;
      fill(root, { common_name: "spotted goatfish" });
      checkForm(root); await settle();
      radios(root)[0].on.change({}); await settle();   // WoRMS has no English name for it here
      addButton(root).on.click({}); await settle();
      return { one, cleared, many, asked: asked(), sent: sent() };
    """,
        tmp_path,
    )
    assert "Did you mean?" in out["one"]["text"]
    assert out["one"]["choices"] == ["Pseudupeneus maculatus (Bloch, 1793) · Mullidae WoRMS ↗"]
    assert out["one"]["ready"] is False  # confirmed by a click, even for one candidate
    assert out["cleared"] == 0 and out["many"] == 5
    assert out["asked"] == [
        "/api/review/species-suggestions/mine",
        "WoRMS /AphiaRecordsByVernacular/spotted%20goatfish?like=false",
        "WoRMS /AphiaRecordsByVernacular/goatfish?like=false",
        "WoRMS /AphiaRecordsByVernacular/goatfish?like=true",
        "WoRMS /AphiaRecordsByVernacular/spotted%20goatfish?like=false",
        "WoRMS /AphiaVernacularsByAphiaID/126994",
        "/api/review/species-suggestions",
        "/api/review/species",
        "/api/review/verdicts",
    ]
    suggestion, verdict = out["sent"]
    assert suggestion["body"] == {**ADDITION, "common_name": "spotted goatfish"}
    assert verdict["body"] == {**BOX_101, **ADDED_ANSWER, "not_sure": False}


@needs_node
def test_a_synonym_of_a_listed_species_offers_use_it(tmp_path):
    """Review Focus 2: WoRMS lists Scarus croicensis as Scarus iseri, which is on the list. The
    form says so, offers the listed species, and adds nothing."""
    out = run_node(
        _page("review-frame.js"),
        FRAME_STUB
        + SUGGEST_STUB
        + """
      RV.catalogue = [...listed, { key: "Scarus iseri|", name: "Scarus iseri", life_stage: null,
        common: "Striped Parrotfish", aphia_id: 276002, celebrities: [] }];
      RV.byKey = new Map(RV.catalogue.map((e) => [e.key, e]));
      worms[matchNames("Scarus croicensis")] = { data: [[record(126000, "Scarus croicensis", {
        status: "unaccepted", authority: "Bloch, 1790", valid_AphiaID: 276002, valid_name: "Scarus iseri",
        valid_authority: "(Bloch, 1789)", family: "Scaridae" })]] };
      await RV.views.frame.enter({ savedFrameId: 7 });
      const root = document.getElementById("rv-frame");
      openForm(root, { scientific_name: "Scarus croicensis" });
      checkForm(root); await settle();
      const text = textOf(root);
      const ready = !addButton(root).disabled;
      const useButtons = uses(root);
      button(root, "Use Striped Parrotfish").on.click({}); await settle();
      return { text, ready, useButtons, sent: sent() };
    """,
        tmp_path,
    )
    assert "WoRMS lists this as Scarus iseri" in out["text"]
    assert "✓ Scarus iseri (Bloch, 1789) · Scaridae WoRMS ↗" in out["text"]
    assert "already on the list as Striped Parrotfish" in out["text"]
    assert out["ready"] is False and out["useButtons"] == ["Use Striped Parrotfish"]
    assert out["sent"] == [  # the listed species answers the box; nothing is added
        {
            "url": "/api/review/verdicts",
            "body": {
                **BOX_101,
                "label": "Scarus iseri",
                "life_stage": None,
                "label_source": "search",
                "not_sure": False,
            },
        }
    ]


@needs_node
def test_a_conflict_never_shows_the_word_null(tmp_path):
    """Browser walk with the real WoRMS (Task 5): "already on the list as Striped Parrotfish"
    had the word "null" under its Use button. DOM replaceChildren() turns a null argument (here,
    no hint) into that text; this stub keeps it as a null child instead."""
    out = run_node(
        _page("review-frame.js"),
        FRAME_STUB
        + SUGGEST_STUB
        + """
      RV.catalogue = [...listed, { key: "Scarus iseri|", name: "Scarus iseri", life_stage: null,
        common: "Striped Parrotfish", aphia_id: 276052, celebrities: [] }];
      RV.byKey = new Map(RV.catalogue.map((e) => [e.key, e]));
      worms[matchNames("Scarus croicensis")] = { data: [[record(308924, "Scarus croicensis", {
        status: "unaccepted", authority: "Bloch, 1790", valid_AphiaID: 276052, valid_name: "Scarus iseri",
        valid_authority: "(Bloch, 1789)", family: "Labridae" })]] };
      await RV.views.frame.enter({ savedFrameId: 7 });
      const root = document.getElementById("rv-frame");
      openForm(root, { scientific_name: "Scarus croicensis" });
      checkForm(root); await settle();
      const holes = all(root, (n) => kids(n).some((c) => c === null || c === undefined));
      return { text: textOf(root), holes: holes.map((n) => n.className || n.tag) };
    """,
        tmp_path,
    )
    assert "already on the list as Striped Parrotfish" in out["text"]
    assert out["holes"] == []


@needs_node
def test_stage_none_for_a_staged_only_species_offers_its_stages(tmp_path):
    """Dan's option a: a plain entry for a species listed only by phase is refused, so the form
    offers its listed phases; a new stage of it may still be added."""
    out = run_node(
        _page("review-single.js"),
        FRAME_STUB
        + SUGGEST_STUB
        + """
      const parrot = (stage) => ({ key: `Scarus iseri|${stage}`, name: "Scarus iseri", life_stage: stage,
        common: "Striped Parrotfish", aphia_id: 276002, celebrities: [] });
      RV.catalogue = [...listed, parrot("initial phase"), parrot("terminal phase")];
      RV.byKey = new Map(RV.catalogue.map((e) => [e.key, e]));
      worms[matchNames("Scarus iseri")] = { data: [[record(276002, "Scarus iseri",
        { authority: "(Bloch, 1789)", valid_authority: "(Bloch, 1789)", family: "Scaridae" })]] };
      await RV.views.single.enter({ payload: payload(1) });
      const root = document.getElementById("rv-single");
      openForm(root, { scientific_name: "Scarus iseri" });
      checkForm(root); await settle();
      const none = { text: textOf(root), offered: uses(root), ready: !addButton(root).disabled };
      fill(root, { life_stage: "juvenile" });     // a new stage of a species listed by phase
      const juvenile = { text: textOf(root), offered: uses(root), ready: !addButton(root).disabled };
      fill(root, { life_stage: "" });
      button(root, "Use Striped Parrotfish (terminal phase)").on.click({}); await settle();
      return { none, juvenile, sent: sent() };
    """,
        tmp_path,
    )
    assert "pick a stage: initial phase, terminal phase, or add a new one" in out["none"]["text"]
    assert out["none"]["offered"] == [
        "Use Striped Parrotfish (initial phase)",
        "Use Striped Parrotfish (terminal phase)",
    ]
    assert out["none"]["ready"] is False
    assert "pick a stage" not in out["juvenile"]["text"]
    assert out["juvenile"]["offered"] == [] and out["juvenile"]["ready"] is True
    assert [c["body"] for c in out["sent"]] == [
        {
            **BOX_101,
            "label": "Scarus iseri",
            "life_stage": "terminal phase",
            "label_source": "search",
            "not_sure": False,
        }
    ]


@needs_node
def test_worms_down_says_so_and_adds_nothing(tmp_path):
    """Review Focus 3: WoRMS can't be reached, answers 503, or takes longer than the page waits.
    Each says so; Check works again (it asked once while busy); nothing is added."""
    out = run_node(
        _page("review-frame.js"),
        FRAME_STUB
        + SUGGEST_STUB
        + f"""
      RV.worms.TIMEOUT_MS = 50;
      await RV.views.frame.enter({{ savedFrameId: 7 }});
      const root = document.getElementById("rv-frame");
      openForm(root, {{ scientific_name: "Pseudupeneus maculatus" }});
      const seen = [];
      for (const answer of ["reject", {{ status: 503, data: {{ error: "unavailable" }} }}, "hang"]) {{
        worms[matchNames("Pseudupeneus maculatus")] = answer;
        checkForm(root);
        const busy = checkButton(root).disabled;
        checkForm(root);                                  // pressed again while asking
        await new Promise((r) => setTimeout(r, 120)); await settle();
        seen.push({{ busy, down: textOf(root).includes({json.dumps(DOWN)}),
          check: !checkButton(root).disabled, add: !addButton(root).disabled }});
      }}
      return {{ seen, asked: asked(), sent: sent() }};
    """,
        tmp_path,
    )
    assert out["seen"] == [{"busy": True, "down": True, "check": True, "add": False}] * 3
    assert out["asked"] == ["/api/review/species-suggestions/mine"] + [MATCH_GOATFISH] * 3
    assert out["sent"] == []


@needs_node
def test_a_genus_is_refused(tmp_path):
    out = run_node(
        _page("review-single.js"),
        FRAME_STUB
        + SUGGEST_STUB
        + """
      worms[matchNames("Pseudupeneus")] = { data: [[record(126993, "Pseudupeneus",
        { rank: "Genus", authority: "Bleeker, 1862", valid_authority: "Bleeker, 1862" })]] };
      await RV.views.single.enter({ payload: payload(1) });
      const root = document.getElementById("rv-single");
      openForm(root, { scientific_name: "Pseudupeneus" });
      checkForm(root); await settle();
      return { text: textOf(root), ready: !addButton(root).disabled, choices: choices(root),
        sent: sent() };
    """,
        tmp_path,
    )
    assert "pick a species, not a genus" in out["text"]
    assert out["ready"] is False and out["choices"] == [] and out["sent"] == []


@needs_node
def test_a_record_worms_does_not_accept_is_no_species_to_add(tmp_path):
    """Final review M1: the spec asks for an exact match WoRMS accepts. A record that stands for
    itself (no valid name, or itself as its valid name) yet isn't accepted -- a nomen dubium, a
    taxon inquirendum, one TAXAMATCH finds quarantined -- is no species to add: matched exactly,
    WoRMS doesn't know the name; near, it isn't offered. A synonym still stands for the species
    it points to."""
    out = run_node(
        _page("review-single.js"),
        FRAME_STUB
        + SUGGEST_STUB
        + """
      worms[matchNames("Pseudupeneus dubius")] = { data: [[record(700, "Pseudupeneus dubius",
        { status: "nomen dubium", valid_AphiaID: null, valid_name: null, valid_authority: null })]] };
      worms[matchNames("Pseudupeneus maculatis")] = { data: [[
        record(701, "Pseudupeneus maculatis", { match_type: "near_1", status: "taxon inquirendum" }),
        record(702, "Pseudupeneus maculosus", { match_type: "near_2", status: "quarantined",
          valid_AphiaID: null, valid_name: null }),
        record(126994, "Pseudupeneus maculatus", { match_type: "phonetic" }),
        record(500, "Upeneus prayensis", { match_type: "near_3", status: "unaccepted",
          valid_AphiaID: 126995, valid_name: "Pseudupeneus prayensis" })]] };
      await RV.views.single.enter({ payload: payload(1) });
      const root = document.getElementById("rv-single");
      openForm(root, { scientific_name: "Pseudupeneus dubius" });
      checkForm(root); await settle();
      const exact = { text: textOf(root), ready: !addButton(root).disabled, choices: choices(root) };
      fill(root, { scientific_name: "Pseudupeneus maculatis" });
      checkForm(root); await settle();
      return { exact, near: choices(root), asked: asked() };
    """,
        tmp_path,
    )
    assert UNKNOWN in out["exact"]["text"] and "✓" not in out["exact"]["text"]
    assert out["exact"]["ready"] is False and out["exact"]["choices"] == []
    assert out["near"] == [
        "Pseudupeneus maculatus (Bloch, 1793) · Mullidae WoRMS ↗",
        "Pseudupeneus prayensis (Bloch, 1793) · Mullidae WoRMS ↗",
    ]
    assert out["asked"] == [
        "/api/review/species-suggestions/mine",
        "WoRMS /AphiaRecordsByMatchNames?scientificnames[]=Pseudupeneus%20dubius&marine_only=false",
        MATCH_GOATFISH.replace("maculatus", "maculatis"),
    ]


@needs_node
def test_a_name_taxamatch_misses_falls_back_to_names_that_start_the_same(tmp_path):
    out = run_node(
        _page("review-single.js"),
        FRAME_STUB
        + SUGGEST_STUB
        + """
      worms["/AphiaRecordsByName/Pseudupeneus%20mac?like=true&marine_only=false"] = { data: [
        record(126994, "Pseudupeneus maculatus", { match_type: undefined })] };
      await RV.views.single.enter({ payload: payload(1) });
      const root = document.getElementById("rv-single");
      openForm(root, { scientific_name: "Pseudupeneus mac" });
      checkForm(root); await settle();
      const started = { text: textOf(root), choices: choices(root) };
      fill(root, { scientific_name: "Nofishus atall" });
      checkForm(root); await settle();
      return { started, unknown: textOf(root), ready: !addButton(root).disabled, asked: asked() };
    """,
        tmp_path,
    )
    assert "Did you mean?" in out["started"]["text"]
    assert out["started"]["choices"] == ["Pseudupeneus maculatus (Bloch, 1793) · Mullidae WoRMS ↗"]
    assert UNKNOWN in out["unknown"] and out["ready"] is False
    assert out["asked"] == [
        "/api/review/species-suggestions/mine",
        "WoRMS /AphiaRecordsByMatchNames?scientificnames[]=Pseudupeneus%20mac&marine_only=false",
        "WoRMS /AphiaRecordsByName/Pseudupeneus%20mac?like=true&marine_only=false",
        "WoRMS /AphiaRecordsByMatchNames?scientificnames[]=Nofishus%20atall&marine_only=false",
        "WoRMS /AphiaRecordsByName/Nofishus%20atall?like=true&marine_only=false",
    ]


@needs_node
def test_the_form_mirrors_the_list_rules(tmp_path):
    """The server is the authority; the form only mirrors its rules to offer "Use …" instead of
    a refusal: the same choice by name or AphiaID, and Dan's option a on stages. A species
    still waiting for approval (an ai #49 leftover) is not on the list."""
    out = run_node(
        [_read("review-core.js")],
        """
      const e = (name, stage, common, aphia) => ({ key: `${name}|${stage || ""}`, name,
        life_stage: stage, common, aphia_id: aphia });
      RV.catalogue = [e("Abudefduf saxatilis", null, "Sergeant Major", 1),
        e("Scarus iseri", "initial phase", "Striped Parrotfish (initial phase)", 2),
        e("Scarus iseri", "terminal phase", "Striped Parrotfish (terminal phase)", 2),
        e("Charybdis hellerii", null, "Spiny Hands Crab", 3),
        { key: "suggestion:4", name: "Pseudupeneus maculatus", life_stage: null, pending: true,
          suggestion_id: 4, common: "Spotted Goatfish (waiting for approval)" }];
      const judge = (id, name, stage) => {
        const c = RV.listConflict({ aphia_id: id, name }, stage);
        return c && { message: c.message, uses: c.uses.map((u) => u.key) };
      };
      return [judge(1, "Abudefduf saxatilis", null), judge(1, "Abudefduf saxatilis", "adult"),
        judge(2, "Scarus iseri", null), judge(2, "Scarus iseri", "juvenile"),
        judge(2, "Scarus iseri", "terminal phase"),
        judge(3, "Charybdis (Charybdis) hellerii", null),
        judge(4, "Pseudupeneus maculatus", null), judge(5, "Lutjanus apodus", "juvenile")];
    """,
        tmp_path,
    )
    major = {"message": "already on the list as Sergeant Major", "uses": ["Abudefduf saxatilis|"]}
    assert out == [
        major,
        major,  # a stage for a species listed without one
        {
            "message": "pick a stage: initial phase, terminal phase, or add a new one",
            "uses": ["Scarus iseri|initial phase", "Scarus iseri|terminal phase"],
        },
        None,  # a new stage of a species listed by phase
        {
            "message": "already on the list as Striped Parrotfish (terminal phase)",
            "uses": ["Scarus iseri|terminal phase"],
        },
        {"message": "already on the list as Spiny Hands Crab", "uses": ["Charybdis hellerii|"]},
        None,
        None,
    ]


@needs_node
def test_the_add_link_is_on_blind_items_and_drawn_boxes_but_hidden_in_booth_mode(tmp_path):
    """Nothing waits for an admin any more, so a blind second opinion and a box a volunteer drew
    may add a species too; the pickers there still never offer an ai #49 leftover waiting for
    approval. A drawn box is saved with the new species. Booth mode hides the whole block."""
    hidden = [
        sel
        for sel, body in re.findall(r"([^{}]*)\{([^}]*)\}", _read("tour.css"))
        if "display: none" in body
    ]
    assert any(re.search(r'\[data-booth="1"\] \.rv-add\b', sel) for sel in hidden)
    assert "RV.addSpeciesBlock(" in _function_body(_read("review-frame.js"), "addedPanel")
    out = run_node(
        _page("review-single.js", "review-frame.js"),
        FRAME_STUB
        + SUGGEST_STUB
        + f"""
      RV.catalogue = [...listed, ENTRY];
      RV.byKey = new Map(RV.catalogue.map((e) => [e.key, e]));
      await RV.views.single.enter({{ payload: payload(2) }});     // a blind second opinion
      const single = document.getElementById("rv-single");
      const blind = {{ link: Boolean(addLink(single)), offers: offered(single, "spotted") }};
      const frameFetch = globalThis.fetch;
      globalThis.fetch = async (url, opts) => {{
        const r = await frameFetch(url, opts);
        if (!url.endsWith("/open")) return r;
        const data = await r.json();
        data.added = [{{ id: 5, bbox: [400, 300, 480, 360], mine: true, by: "you", outcome: "species",
          label: "Haemulon chrysargyreum", common: "Smallmouth grunt", life_stage: null,
          not_sure: false, zoom_url: "/a.jpg" }}];
        return {{ status: 200, ok: true, json: async () => data }};
      }};
      await RV.views.frame.enter({{ savedFrameId: 7 }});
      const root = document.getElementById("rv-frame");
      find(root, (n) => n.tag === "li" && textOf(n).startsWith("+ drawn by you")).on.click({{}});
      const drawn = {{ link: Boolean(addLink(root)), offers: offered(root, "spotted") }};
      worms[matchNames("Pseudupeneus maculatus")] = {{ data: [[GOATFISH]] }};
      openForm(root, {GOATFISH_FORM});
      checkForm(root); await settle();
      addButton(root).on.click({{}}); await settle();
      return {{ blind, drawn, sent: sent() }};
    """,
        tmp_path,
    )
    assert out["blind"] == {"link": True, "offers": ""}
    assert out["drawn"] == {"link": True, "offers": ""}
    assert out["sent"] == [
        {"url": "/api/review/species-suggestions", "body": ADDITION},
        {
            "url": "/api/review/frames/7/boxes/5",
            "body": {
                "bbox": [400, 300, 480, 360],
                "outcome": "species",
                "label": "Pseudupeneus maculatus",
                "life_stage": "juvenile",
                "not_sure": False,
            },
        },
    ]


def test_the_worms_check_loads_after_core_and_busts_the_cache():
    page = (STATIC.parent / "templates" / "review.html").read_text()
    core = page.index("/static/review-core.js?v=")
    assert core < page.index("/static/review-worms.js?v=") < page.index("/static/review-single.js")
    assert 'BASE_DIR / "static" / "review-worms.js"' in (STATIC.parent / "main.py").read_text()


@needs_node
def test_an_entry_with_a_suggestion_answers_with_it(tmp_path):
    """The answer that adds a species names the addition, as one with an ai #49 leftover still
    waiting for approval does; a species picked from the list is an ordinary answer."""
    out = run_node(
        [_read("review-core.js")],
        """
      return [RV.answerExtra({ pending: true, suggestion_id: 4, name: "x", life_stage: null }),
        RV.answerExtra({ key: "Pseudupeneus maculatus|juvenile", name: "Pseudupeneus maculatus",
          life_stage: "juvenile", suggestion_id: 9 }),
        RV.answerExtra({ key: "Haemulon plumierii|", name: "Haemulon plumierii", life_stage: null },
          "hint")];
    """,
        tmp_path,
    )
    assert out == [
        {"label": "x", "life_stage": None, "label_source": "suggested", "species_suggestion_id": 4},
        ADDED_ANSWER,
        {"label": "Haemulon plumierii", "life_stage": None, "label_source": "hint"},
    ]
    assert "RV.answerExtra(entry, source)" in _function_body(
        _read("review-single.js"), "pickSpecies"
    )
    assert "RV.answerExtra(entry, source)" in _function_body(
        _read("review-frame.js"), "answerControls"
    )


@needs_node
def test_a_refusal_after_the_check_is_shown_with_use_it(tmp_path):
    """Review Focus 1, on the page: the list was read before another reviewer added the same
    species, so the check passes and the server refuses. The form reloads the list and offers
    the species now listed; nothing is answered until it's used. Without a name nothing is
    asked."""
    out = run_node(
        _page("review-frame.js"),
        FRAME_STUB
        + SUGGEST_STUB
        + f"""
      globalThis.suggestReply = {{ status: 409, data: {{ error: "already on the list as Spotted Goatfish" }} }};
      worms[matchNames("Pseudupeneus maculatus")] = {{ data: [[GOATFISH]] }};
      await RV.views.frame.enter({{ savedFrameId: 7 }});
      const root = document.getElementById("rv-frame");
      openForm(root, {{ common_name: "  ", scientific_name: " " }});
      checkForm(root); await settle();                     // no name: WoRMS isn't asked
      const alert = attr(root, "role", "alert");
      const empty = {{ said: alert.textContent, shown: !alert.hidden, asked: asked().length }};
      fill(root, {GOATFISH_FORM});
      checkForm(root); await settle();
      addButton(root).on.click({{}}); await settle();
      const refused = {{ text: textOf(root), offered: uses(root), check: !checkButton(root).disabled,
        add: !addButton(root).disabled }};
      button(root, "Use Spotted Goatfish").on.click({{}}); await settle();
      return {{ empty, refused, asked: asked(), sent: sent() }};
    """,
        tmp_path,
    )
    assert out["empty"] == {
        "said": "write the fish's common name or its scientific name",
        "shown": True,
        "asked": 1,  # only "Species you added"
    }
    assert "already on the list as Spotted Goatfish" in out["refused"]["text"]
    assert out["refused"]["offered"] == ["Use Spotted Goatfish"]
    assert out["refused"]["check"] is True and out["refused"]["add"] is False
    assert out["asked"] == [
        "/api/review/species-suggestions/mine",
        MATCH_GOATFISH,
        "/api/review/species-suggestions",
        "/api/review/species",  # reloaded after the refusal
        "/api/review/verdicts",
    ]
    suggestion, verdict = out["sent"]
    assert suggestion["body"] == ADDITION
    assert verdict["body"] == {
        **BOX_101,
        "label": "Pseudupeneus maculatus",
        "life_stage": "juvenile",
        "label_source": "search",
        "not_sure": False,
    }


@needs_node
def test_a_refusal_the_form_cannot_foresee_leaves_add_usable(tmp_path):
    """The server also refuses a common name another choice already has at that stage, which
    the form doesn't mirror. That choice is another species, so it is named, never offered as
    "Use …" (one click would label a goatfish a bluefish); Add stays usable, so the reviewer can
    change the common name and add the species after all."""
    out = run_node(
        _page("review-frame.js"),
        FRAME_STUB
        + SUGGEST_STUB
        + """
      const BLUEFISH = { key: "Pomatomus saltatrix|", name: "Pomatomus saltatrix", life_stage: null,
        common: "Bluefish", aphia_id: 151, celebrities: [] };
      RV.catalogue = [...listed, BLUEFISH];
      RV.byKey = new Map(RV.catalogue.map((e) => [e.key, e]));
      globalThis.species = RV.catalogue;
      globalThis.suggestReply = { status: 409, data: { error: "already on the list as Bluefish" } };
      worms[matchNames("Pseudupeneus maculatus")] = { data: [[GOATFISH]] };
      await RV.views.frame.enter({ savedFrameId: 7 });
      const root = document.getElementById("rv-frame");
      openForm(root, { common_name: "Bluefish", scientific_name: "Pseudupeneus maculatus",
        life_stage: "juvenile" });
      checkForm(root); await settle();
      addButton(root).on.click({}); await settle();
      const refused = { text: textOf(root), offered: uses(root), add: !addButton(root).disabled };
      globalThis.species = null;
      globalThis.suggestReply = { status: 200, data: { key: ADDED.key, name: ADDED.name,
        life_stage: ADDED.life_stage, common: ADDED.common, suggestion_id: 9 } };
      fill(root, { common_name: "Spotted Goatfish" });
      addButton(root).on.click({}); await settle();
      return { refused, sent: sent() };
    """,
        tmp_path,
    )
    assert "already on the list as Bluefish (Pomatomus saltatrix)" in out["refused"]["text"]
    assert "Give this species another common name to add it." in out["refused"]["text"]
    assert out["refused"]["offered"] == [] and out["refused"]["add"] is True
    first, second, verdict = out["sent"]
    assert first["body"] == {**ADDITION, "common_name": "Bluefish"}
    assert second["body"] == ADDITION
    assert verdict["body"] == {**BOX_101, **ADDED_ANSWER, "not_sure": False}


@needs_node
def test_a_refusal_naming_the_same_species_offers_use_it(tmp_path):
    """A refusal the form didn't foresee that names a listed choice of the very species WoRMS
    matched (same AphiaID) offers that choice."""
    out = run_node(
        _page("review-frame.js"),
        FRAME_STUB
        + SUGGEST_STUB
        + """
      const ADULT = { key: "Pseudupeneus maculatus|adult", name: "Pseudupeneus maculatus",
        life_stage: "adult", common: "Spotted Goatfish", aphia_id: 126994, celebrities: [] };
      RV.catalogue = [...listed, ADULT];
      RV.byKey = new Map(RV.catalogue.map((e) => [e.key, e]));
      globalThis.species = RV.catalogue;
      globalThis.suggestReply = { status: 409, data: { error: "already on the list as Spotted Goatfish" } };
      worms[matchNames("Pseudupeneus maculatus")] = { data: [[GOATFISH]] };
      await RV.views.frame.enter({ savedFrameId: 7 });
      const root = document.getElementById("rv-frame");
      openForm(root, {GOATFISH_FORM});           // a juvenile: a new stage, as far as the form knows
      checkForm(root); await settle();
      addButton(root).on.click({}); await settle();
      const useButtons = uses(root);
      button(root, "Use Spotted Goatfish").on.click({}); await settle();
      return { useButtons, sent: sent() };
    """.replace("{GOATFISH_FORM}", GOATFISH_FORM),
        tmp_path,
    )
    assert out["useButtons"] == ["Use Spotted Goatfish"]
    suggestion, verdict = out["sent"]
    assert suggestion["url"] == "/api/review/species-suggestions"
    assert verdict["body"] == {
        **BOX_101,
        "label": "Pseudupeneus maculatus",
        "life_stage": "adult",
        "label_source": "search",
        "not_sure": False,
    }


@needs_node
def test_a_chosen_record_fills_in_its_english_common_name(tmp_path):
    """Controller ruling (fix round 1, I2): nothing reviews an addition, so a search fragment
    must not become its name on everyone's list. Once a record is chosen, the form fills the
    common name with WoRMS's English one, in Title Case, when the reviewer gave none or searched
    by common name; Add waits for it. A common name typed beside a scientific search is kept;
    with no English name from WoRMS the field is left as it is, and Add still works."""
    out = run_node(
        _page("review-single.js"),
        FRAME_STUB
        + SUGGEST_STUB
        + """
      const field = (root) => attr(root, "name", "common_name").value || "";
      const root = document.getElementById("rv-single");
      // Each case starts from the list before the goatfish was added.
      const fresh = () => { RV.catalogue = listed.slice(); RV.byKey = new Map(RV.catalogue.map((e) => [e.key, e])); };
      worms["/AphiaRecordsByVernacular/spotted%20goat?like=true"] = { data: [GOATFISH] };
      worms["/AphiaVernacularsByAphiaID/126994"] = { data: [
        { vernacular: "spotted goatfish", language_code: "eng", language: "English" }] };
      worms[matchNames("Pseudupeneus maculatus")] = { data: [[GOATFISH]] };
      // A common-name search: the fragment typed is replaced by the English name.
      fresh();
      await RV.views.single.enter({ payload: payload(1) });
      openForm(root, { common_name: "spotted goat", life_stage: "juvenile" });
      checkForm(root); await settle();
      radios(root)[0].on.change({});
      const waiting = addButton(root).disabled;
      await settle();
      const searched = { field: field(root), ready: !addButton(root).disabled };
      addButton(root).on.click({}); await settle();
      // A scientific search with a common name typed: it is kept, and WoRMS isn't asked.
      fresh();
      await RV.views.single.enter({ payload: payload(1) });
      openForm(root, { common_name: "goatie", scientific_name: "Pseudupeneus maculatus",
        life_stage: "juvenile" });
      checkForm(root); await settle();
      const typed = { field: field(root), ready: !addButton(root).disabled };
      addButton(root).on.click({}); await settle();
      // No English name to be had: the field stays empty, and the scientific name is listed.
      worms["/AphiaVernacularsByAphiaID/126994"] = "reject";
      fresh();
      await RV.views.single.enter({ payload: payload(1) });
      openForm(root, { scientific_name: "Pseudupeneus maculatus", life_stage: "juvenile" });
      checkForm(root); await settle();
      const failed = { field: field(root), ready: !addButton(root).disabled };
      addButton(root).on.click({}); await settle();
      return { waiting, searched, typed, failed,
        names: calls.filter((c) => c.url === "/api/review/species-suggestions").map((c) => c.body.common_name),
        lookups: asked().filter((u) => u.includes("AphiaVernacularsByAphiaID")).length };
    """,
        tmp_path,
    )
    assert out["waiting"] is True  # Add waits for the English name
    assert out["searched"] == {"field": "Spotted Goatfish", "ready": True}
    assert out["typed"] == {"field": "goatie", "ready": True}
    assert out["failed"] == {"field": "", "ready": True}
    assert out["names"] == ["Spotted Goatfish", "goatie", "Pseudupeneus maculatus"]
    assert out["lookups"] == 2  # not for the typed common name, nor on any Add


# A misspelt name WoRMS offers two species for: the goatfish, which has an English name, and
# Pseudupeneus prayensis, which (unless a test says otherwise) has none.
TWO_GOATFISH = """
      worms[matchNames("Pseudupeneus maculatis")] = { data: [[
        record(126994, "Pseudupeneus maculatus", { match_type: "phonetic" }),
        record(126995, "Pseudupeneus prayensis", { match_type: "near_2", authority: "(Cuvier, 1829)",
          valid_authority: "(Cuvier, 1829)" })]] };
      worms["/AphiaVernacularsByAphiaID/126994"] = { data: [
        { vernacular: "spotted goatfish", language_code: "eng", language: "English" }] };
      const field = (root) => attr(root, "name", "common_name").value || "";
      const added = () => calls.filter((c) => c.url === "/api/review/species-suggestions")
        .map((c) => c.body.common_name);
"""
PRAYENSIS = {
    **ADDITION,
    "aphia_id": 126995,
    "scientific_name": "Pseudupeneus prayensis",
    "common_name": "Pseudupeneus prayensis",
}


@needs_node
@pytest.mark.parametrize("english", ["none", "reject", "hang"])
def test_choosing_another_record_drops_the_common_name_the_form_filled(english, tmp_path):
    """Final review I1: the form fills in record A's English name, then the reviewer picks B,
    for which WoRMS has no English name, or doesn't answer in time. A's name must not be listed
    for everyone as B's: the name the form filled is cleared, so B is added under its scientific
    name."""
    out = run_node(
        _page("review-single.js"),
        FRAME_STUB
        + SUGGEST_STUB
        + TWO_GOATFISH
        + f"""
      RV.worms.TIMEOUT_MS = 50;
      const english = {json.dumps(english)};
      if (english !== "none") worms["/AphiaVernacularsByAphiaID/126995"] = english;
      await RV.views.single.enter({{ payload: payload(1) }});
      const root = document.getElementById("rv-single");
      openForm(root, {{ scientific_name: "Pseudupeneus maculatis", life_stage: "juvenile" }});
      checkForm(root); await settle();
      radios(root)[0].on.change({{}}); await settle();
      const a = field(root);
      radios(root)[1].on.change({{}});
      await new Promise((r) => setTimeout(r, 120)); await settle();
      const b = {{ field: field(root), ready: !addButton(root).disabled }};
      addButton(root).on.click({{}}); await settle();
      return {{ a, b, sent: sent() }};
    """,
        tmp_path,
    )
    assert out["a"] == "Spotted Goatfish"
    assert out["b"] == {"field": "", "ready": True}
    assert out["sent"][0] == {"url": "/api/review/species-suggestions", "body": PRAYENSIS}


@needs_node
def test_a_new_check_drops_the_common_name_the_form_filled(tmp_path):
    """Final review I1, across checks: the form filled in the first species' English name; the
    reviewer types another scientific name and checks it, and WoRMS has no English name for the
    match. The first species' name must not be listed as the second's. A filled name the
    reviewer then searches WoRMS by is their search text, and stays in the field."""
    out = run_node(
        _page("review-single.js"),
        FRAME_STUB
        + SUGGEST_STUB
        + TWO_GOATFISH
        + """
      worms[matchNames("Pseudupeneus maculatus")] = { data: [[GOATFISH]] };
      worms[matchNames("Pseudupeneus prayensis")] = { data: [[record(126995, "Pseudupeneus prayensis",
        { authority: "(Cuvier, 1829)", valid_authority: "(Cuvier, 1829)" })]] };
      worms["/AphiaRecordsByVernacular/spotted%20goat?like=true"] = { data: [GOATFISH] };
      worms["/AphiaRecordsByVernacular/Spotted%20Goatfish?like=false"] = { data: [GOATFISH] };
      await RV.views.single.enter({ payload: payload(1) });
      const root = document.getElementById("rv-single");
      openForm(root, { scientific_name: "Pseudupeneus maculatus", life_stage: "juvenile" });
      checkForm(root); await settle();
      const first = field(root);
      fill(root, { scientific_name: "Pseudupeneus prayensis" });
      checkForm(root); await settle();
      const second = { field: field(root), ready: !addButton(root).disabled };
      addButton(root).on.click({}); await settle();
      // A common-name search: the name filled in is searched by in turn.
      await RV.views.single.enter({ payload: payload(1) });
      openForm(root, { common_name: "spotted goat", life_stage: "juvenile" });
      checkForm(root); await settle();
      radios(root)[0].on.change({}); await settle();
      checkForm(root); await settle();
      const searched = { field: field(root), choices: choices(root) };
      return { first, second, searched, sent: sent() };
    """,
        tmp_path,
    )
    assert out["first"] == "Spotted Goatfish"
    assert out["second"] == {"field": "", "ready": True}
    assert out["sent"][0] == {"url": "/api/review/species-suggestions", "body": PRAYENSIS}
    assert out["searched"] == {
        "field": "Spotted Goatfish",
        "choices": ["Pseudupeneus maculatus (Bloch, 1793) · Mullidae WoRMS ↗"],
    }


@needs_node
def test_a_common_name_the_reviewer_typed_survives_choosing_another_record(tmp_path):
    """Final review I1: only the form's own fill is cleared. A common name typed beside the
    scientific search, or typed over the one the form filled, stays through another choice."""
    out = run_node(
        _page("review-single.js"),
        FRAME_STUB
        + SUGGEST_STUB
        + TWO_GOATFISH
        + """
      const root = document.getElementById("rv-single");
      const chooseBoth = async (fields, retype) => {
        await RV.views.single.enter({ payload: payload(1) });
        openForm(root, { scientific_name: "Pseudupeneus maculatis", life_stage: "juvenile", ...fields });
        checkForm(root); await settle();
        radios(root)[0].on.change({}); await settle();
        if (retype) fill(root, { common_name: retype });
        radios(root)[1].on.change({}); await settle();
        const shown = field(root);
        addButton(root).on.click({}); await settle();
        return shown;
      };
      const typed = await chooseBoth({ common_name: "goatie" });
      const retyped = await chooseBoth({}, "Prayensis Goatfish");
      return { typed, retyped, added: added(),
        lookups: asked().filter((u) => u.includes("AphiaVernacularsByAphiaID")) };
    """,
        tmp_path,
    )
    assert out["typed"] == "goatie" and out["retyped"] == "Prayensis Goatfish"
    assert out["added"] == ["goatie", "Prayensis Goatfish"]
    # only for the empty field's first choice: never for a name the reviewer typed
    assert out["lookups"] == ["WoRMS /AphiaVernacularsByAphiaID/126994"]


@pytest.mark.parametrize("name", ["checkName", "addSpecies"])
def test_the_add_species_form_is_busy_guarded(name):
    _assert_busy_guarded(_function_body(_read("review-core.js"), name), name)


@needs_node
def test_species_you_added_say_what_became_of_each(tmp_path):
    out = run_node(
        _page(),
        FRAME_STUB
        + SUGGEST_STUB
        + """
      RV.catalogue = [...listed, { key: "Coryphopterus glaucofraenum|", name: "Coryphopterus glaucofraenum",
        common: "Bridled Goby", life_stage: null }];
      RV.byKey = new Map(RV.catalogue.map((e) => [e.key, e]));
      const none = { scientific_name: null, life_stage: null, decision_note: null, rejected_to_name: null,
        rejected_to_stage: null, approved_common: null, aphia_id: null };
      globalThis.mine = [
        { ...none, id: 5, common_name: "Spotted Goatfish", life_stage: "juvenile", status_text: "added",
          approved_common: "Spotted Goatfish", aphia_id: 126994 },
        { ...none, id: 4, common_name: "Banded goby", status_text: "removed",
          rejected_to_name: "Coryphopterus glaucofraenum", decision_note: "It's a bridled goby." },
        { ...none, id: 3, common_name: "Blur", status_text: "removed" },
        // ai #49 leftovers: one an admin approved under another name, one still waiting.
        { ...none, id: 2, common_name: "spotted goatfish", status_text: "added",
          approved_common: "Spotted Goatfish" },
        { ...none, id: 1, common_name: "Yellow goatfish", status_text: "waiting for approval" },
      ];
      const holder = document.createElement("div");
      RV.suggestSpecies(holder, () => {}, () => {});
      await settle();
      const mine = find(holder, (n) => n.className === "rv-suggest-mine");
      return { heading: textOf(mine.children[0]), items: find(mine, (n) => n.tag === "ul").children.map(textOf) };
    """,
        tmp_path,
    )
    assert out["heading"] == "Species you added"
    assert out["items"] == [
        "Spotted Goatfish (juvenile) · added",
        "Banded goby · removed; its answers now say Bridled GobyIt's a bridled goby.",
        "Blur · removed; its answers now say can't identify",
        "spotted goatfish · added as Spotted Goatfish",
        "Yellow goatfish · waiting for approval",
    ]


# The volunteer moved on while the species was being added: the frame selects box 2, or the
# single view shows the next box. The new species answers neither.
MOVE_ON = {
    "single": (
        "await RV.views.single.enter({ payload: payload(1) });",
        "await RV.views.single.enter({ payload: { ...payload(1), item: { id: 12, event_id: 102, pass: 1 } } });",
    ),
    "frame": (
        "await RV.views.frame.enter({ savedFrameId: 7 });",
        "await RV.views.frame.enter({ savedFrameId: 7, selectEventId: 102 });",
    ),
}


@needs_node
@pytest.mark.parametrize("view", ["single", "frame"])
def test_a_box_left_while_the_species_is_being_added_is_not_answered_with_it(view, tmp_path):
    enter, move_on = MOVE_ON[view]
    out = run_node(
        _page(f"review-{view}.js"),
        FRAME_STUB
        + SUGGEST_STUB
        + f"""
      let reply;
      globalThis.gate = new Promise((r) => {{ reply = r; }});
      worms[matchNames("Pseudupeneus maculatus")] = {{ data: [[GOATFISH]] }};
      {enter}
      const root = document.getElementById("rv-{view}");
      openForm(root, {GOATFISH_FORM});
      checkForm(root); await settle();
      addButton(root).on.click({{}}); await settle();
      {move_on}
      reply(); await settle();
      return {{ sent: sent().map((c) => c.url), toast: document.getElementById("rv-toast").textContent }};
    """,
        tmp_path,
    )
    assert out["sent"] == ["/api/review/species-suggestions"]
    assert (
        out["toast"]
        == "The new species was added. Pick it from the search to answer a box with it."
    )


@needs_node
@pytest.mark.parametrize(
    "error",
    [
        "that species suggestion was already decided; pick from the list",
        "that species was removed from the list; pick from the list",
    ],
)
@pytest.mark.parametrize("view", ["single", "frame"])
def test_a_new_species_decided_meanwhile_reloads_the_list_and_keeps_the_box(view, error, tmp_path):
    """The species list loaded before an admin decided the volunteer's new species, or removed
    a species a reviewer added: the verdict's 409 names that, and the page reloads the list
    instead of moving on from a box that is still theirs."""
    enter = (
        "await RV.views.single.enter({ payload: payload(1) });"
        if view == "single"
        else "await RV.views.frame.enter({ savedFrameId: 7 });"
    )
    out = run_node(
        [_read("review-core.js"), _read(f"review-{view}.js")],
        FRAME_STUB
        + SUGGEST_STUB
        + f"""
      RV.catalogue = [...listed, ENTRY];
      globalThis.verdictReply = {{ status: 409, data: {{ error: {json.dumps(error)} }} }};
      {enter}
      const root = document.getElementById("rv-{view}");
      offered(root, "spotted");
      find(root, (n) => n.tag === "li" && textOf(n).startsWith("Spotted Goatfish")).on.mousedown(
        {{ preventDefault() {{}} }});
      await settle();
      return calls.map((c) => c.url.split("?")[0]).filter((u) => !u.endsWith("/open"));
    """,
        tmp_path,
    )
    assert out[:2] == ["/api/review/verdicts", "/api/review/species"]
    assert "/api/review/next" not in out


HINTED_ENTER = {
    "single": """
      await RV.views.single.enter({ payload: { item: { id: 11, event_id: 101, pass: 1 },
        box: { bbox: [10, 10, 50, 50], model_bbox: [10, 10, 50, 50], saved_frame_id: 7 },
        frame: { url: "/f.jpg", width: 1280, height: 720 }, zoom: { url: "/z.jpg", region: [0, 0, 200, 200] },
        other_boxes: [], model: null, hints: HINTS, counts_toward: null, editable: true } });""",
    "frame": """
      const frameFetch = globalThis.fetch;
      globalThis.fetch = async (url, opts) => {
        const r = await frameFetch(url, opts);
        if (!url.endsWith("/open")) return r;
        const data = await r.json();
        data.boxes[0].hints = HINTS;
        return { status: 200, ok: true, json: async () => data };
      };
      await RV.views.frame.enter({ savedFrameId: 7 });    // box 1 is selected""",
}


@needs_node
@pytest.mark.parametrize("view", ["single", "frame"])
def test_a_hint_chip_answers_in_one_click_beside_a_waiting_stage(view, tmp_path):
    """Final review (promoted minor): a new stage of the hinted species waits for approval (an
    admin sees every one). The chip still answers with the listed species in one click,
    rather than sending the volunteer to the picker."""
    out = run_node(
        [_read("review-core.js"), _read(f"review-{view}.js")],
        FRAME_STUB
        + ELEMENT_STUB
        + f"""
      RV.catalogue = [{{ key: "Haemulon chrysargyreum|", name: "Haemulon chrysargyreum",
          common: "Smallmouth grunt", life_stage: null }},
        {{ key: "suggestion:5", name: "Haemulon chrysargyreum", life_stage: "juvenile", suggestion_id: 5,
          pending: true, common: "Juvenile Smallmouth grunt (waiting for approval)" }}];
      RV.byKey = new Map(RV.catalogue.map((e) => [e.key, e]));
      const HINTS = [{{ name: "Haemulon chrysargyreum", common: "Smallmouth grunt", p: 0.3 }}];
      {HINTED_ENTER[view]}
      find(document.getElementById("rv-{view}"), (n) => n.className === "rv-chip").on.click({{}});
      await settle();
      return calls.filter((c) => c.url === "/api/review/verdicts").map((c) => c.body);
    """,
        tmp_path,
    )
    [verdict] = out
    assert verdict["event_id"] == 101 and verdict["outcome"] == "species"
    assert verdict["label"] == "Haemulon chrysargyreum" and verdict["life_stage"] is None
    assert verdict["label_source"] == "hint" and "species_suggestion_id" not in verdict


@needs_node
def test_looks_right_never_offers_a_stage_waiting_for_approval(tmp_path):
    """A new stage of a listed species (a striped parrotfish juvenile) waits for approval;
    "Looks right" on that species asks only for the listed stages, which the server takes."""
    out = run_node(
        [_read("review-core.js")],
        FRAME_STUB
        + SUGGEST_STUB
        + """
      const parrot = (stage) => ({ key: `Scarus iseri|${stage}`, name: "Scarus iseri",
        life_stage: stage, common: "Striped Parrotfish" });
      RV.catalogue = [parrot("initial phase"), parrot("terminal phase"),
        { key: "suggestion:3", name: "Scarus iseri", life_stage: "juvenile", pending: true,
          suggestion_id: 3, common: "Striped Parrotfish juvenile (waiting for approval)" }];
      RV.byKey = new Map(RV.catalogue.map((e) => [e.key, e]));
      RV.acceptGuessFlow({ name: "Scarus iseri", common: "Striped Parrotfish" }, "rv-stage", () => {});
      const chips = find(document.getElementById("rv-stage"), (n) => n.className === "rv-chips");
      return chips.children.map(textOf);
    """,
        tmp_path,
    )
    assert out == ["initial phase", "terminal phase"]


def test_the_form_offers_the_servers_stages():
    from app.species_suggestions import STAGES

    m = re.search(r"RV\.STAGES = \[([^\]]*)\];", _read("review-core.js"))
    assert m and re.findall(r'"([^"]+)"', m.group(1)) == [s for s in STAGES if s]
    assert None in STAGES  # the form's "none"


@needs_node
def test_a_species_waiting_for_approval_still_marks_nothing(tmp_path):
    """An ai #49 leftover waiting for an admin is not on the list, so "mark the rest" can't use
    it -- and must not offer the species given before it either (Task 7 review, Important 1).
    An addition, by contrast, marks the rest like any species
    (test_an_exact_worms_match_adds_and_answers_the_box)."""
    out = run_node(
        [_read("review-core.js"), _read("review-frame.js")],
        FRAME_STUB
        + SUGGEST_STUB
        + """
      RV.catalogue = [...listed, ENTRY];
      RV.byKey = new Map(RV.catalogue.map((e) => [e.key, e]));
      await RV.views.frame.enter({ savedFrameId: 7 });
      key("a"); await settle();                     // a listed species: M would mark with it
      const root = document.getElementById("rv-frame");
      offered(root, "spotted");
      find(root, (n) => n.tag === "li" && textOf(n).startsWith("Spotted Goatfish")).on.mousedown(
        { preventDefault() {} });
      await settle();
      const verdicts = calls.filter((c) => c.url === "/api/review/verdicts").map((c) => c.body);
      return { verdict: verdicts[1], m: key("m"), text: textOf(root) };
    """,
        tmp_path,
    )
    assert out["verdict"]["label_source"] == "suggested"
    assert out["verdict"]["species_suggestion_id"] == 4
    assert out["m"] is False and "Mark the" not in out["text"]


# ---- 2026-11-21: an admin decides new species from Browse ---------------------------
# Spec 2026-11-21, "Admin panel": every waiting suggestion with the boxes answered with it,
# a per-box "Not this species", Approve under the admin's names and stage, Reject as the
# listed species it really is or as can't-identify; decided ones fold away, and the Browse
# button counts the waiting ones for admins. Since the WoRMS check (spec
# 2026-11-21-species-worms-check) nothing new waits: the panel is shown only while an ai #49
# leftover does, and "Species added by reviewers" (tests at the end) is the main one.

ADMIN_STUB = (
    ELEMENT_STUB
    + r"""
  globalThis.Node = Object;
  document.createTextNode = (t) => ({ text: t });
  const settle = async () => { for (let i = 0; i < 30; i++) await new Promise((r) => setTimeout(r, 0)); };
  const all = (n, pred, out = []) => {
    if (!n || typeof n !== "object") return out;
    if (pred(n)) out.push(n);
    for (const c of kids(n)) all(c, pred, out);
    return out;
  };
  RV.role = "admin";
  RV.catalogue = [{ key: "Haemulon chrysargyreum|", name: "Haemulon chrysargyreum",
      common: "Smallmouth grunt", life_stage: null },
    { key: "suggestion:3", name: "Pseudupeneus maculatus", life_stage: "juvenile", suggestion_id: 3,
      pending: true, common: "Spotted Goatfish (waiting for approval)" }];
  RV.byKey = new Map(RV.catalogue.map((e) => [e.key, e]));
  const none = { decided_at: null, decision_note: null, approved_name: null, approved_common: null,
    approved_stage: null, rejected_to_name: null, rejected_to_stage: null };
  globalThis.waiting = [{ ...none, id: 3, common_name: "Spotted Goatfish",
    scientific_name: "Pseudupeneus maculatus", life_stage: "juvenile", note: "Two barbels under the chin",
    suggested_by_name: "Roy", answers: 2, created_at: "2026-11-21T15:00:00+00:00", status: "pending" }];
  // Species reviewers added since the WoRMS check (spec 2026-11-21-species-worms-check): one on
  // the list, one an admin removed. The server's decided list holds them too.
  const worms = (id) => `https://www.marinespecies.org/aphia.php?p=taxdetails&id=${id}`;
  const PARROTFISH = { ...none, id: 7, common_name: "Striped Parrotfish", scientific_name: "Scarus iseri",
    life_stage: "initial phase", note: "Seen on the pier", suggested_by_name: "Roy", answers: 3,
    created_at: "2026-11-21T15:00:00+00:00", status: "approved", aphia_id: 276052, family: "Labridae",
    approved_name: "Scarus iseri", approved_common: "Striped Parrotfish", approved_stage: "initial phase",
    decided_at: "2026-11-21T15:00:00+00:00", decision_note: "WoRMS AphiaID 276052", worms_url: worms(276052) };
  const SNAPPER = { ...none, id: 5, common_name: "Yellowtail Snapper", scientific_name: "Ocyurus chrysurus",
    life_stage: null, note: null, suggested_by_name: "Roy", answers: 0, created_at: "2026-11-20T15:00:00+00:00",
    status: "rejected", aphia_id: 159801, family: "Lutjanidae", approved_name: "Ocyurus chrysurus",
    approved_common: "Yellowtail Snapper", decided_at: "2026-11-20T18:00:00+00:00",
    decision_note: "Not a snapper: a grunt.", rejected_to_name: "Haemulon chrysargyreum", worms_url: worms(159801) };
  globalThis.additions = [PARROTFISH, SNAPPER];
  globalThis.addedBoxes = [201, 202, 203];
  RV.catalogue.push({ key: "Scarus iseri|initial phase", name: "Scarus iseri", life_stage: "initial phase",
    common: "Striped Parrotfish", aphia_id: 276052 });
  RV.byKey = new Map(RV.catalogue.map((e) => [e.key, e]));
  const DECIDED = [{ ...none, id: 2, common_name: "Banded goby", scientific_name: null, life_stage: null,
    note: null, suggested_by_name: "Roy", answers: 1, created_at: "2026-11-20T15:00:00+00:00",
    status: "rejected", decided_at: "2026-11-20T16:00:00+00:00", rejected_to_name: "Haemulon chrysargyreum",
    decision_note: "A grunt, seen side on." }, PARROTFISH, SNAPPER];
  globalThis.boxes = [101, 102];
  const PROGRESS = { kpis: { verified_boxes: 0, species_with_verified: 0, complete_frames: 0,
    session_verified: 0, open: { check: 0, identify: 0 } }, species: [], target: 100, stale: false };
  const calls = [];
  globalThis.fetch = async (url, opts) => {
    const body = opts && opts.body ? JSON.parse(opts.body) : null;
    calls.push({ url, body });
    const reply = (status, data) => ({ status, ok: status < 400, json: async () => data });
    const m = url.match(/^\/api\/review\/species-suggestions\/3\/boxes\?page=(\d+)$/);
    if (m) {
      const at = 24 * Number(m[1]);
      return reply(200, { total: globalThis.boxes.length, boxes: globalThis.boxes.slice(at, at + 24)
        .map((id) => ({ event_id: id, zoom_url: `/api/review/boxes/${id}/zoom.jpg`, by: "Roy" })) });
    }
    if (url === "/api/review/species-suggestions?status=pending") {
      return reply(200, { suggestions: globalThis.waiting, pending: globalThis.waiting.length });
    }
    if (url === "/api/review/species-suggestions?status=decided") {
      return reply(200, { suggestions: DECIDED, pending: globalThis.waiting.length });
    }
    if (url.endsWith("/3/approve") || url.endsWith("/3/reject")) {
      globalThis.waiting = [];
      return reply(200, { answers: 2 });
    }
    if (url.endsWith("/3/detach")) {
      globalThis.boxes = globalThis.boxes.filter((id) => id !== body.event_id);
      return reply(200, { answers: 1 });
    }
    if (url === "/api/review/species-additions") return reply(200, { additions: globalThis.additions });
    const added = url.match(/^\/api\/review\/species-additions\/7\/boxes\?page=(\d+)$/);
    if (added) {
      const at = 24 * Number(added[1]);
      return reply(200, { total: globalThis.addedBoxes.length, boxes: globalThis.addedBoxes.slice(at, at + 24)
        .map((id) => ({ zoom_url: `/api/review/boxes/${id}/zoom.jpg`, by: id === 201 ? "Dan" : "Roy" })) });
    }
    if (url === "/api/review/species-additions/7/remove") {
      globalThis.additions = globalThis.additions.map((s) => (s.id !== 7 ? s : { ...s, status: "rejected",
        decided_at: "2026-11-21T16:00:00+00:00", decision_note: body.note, rejected_to_name: body.as_label,
        rejected_to_stage: body.as_life_stage }));
      return reply(200, { answers: 4, drawn: 1 });
    }
    if (url === "/api/review/progress") return reply(200, PROGRESS);
    if (url.startsWith("/api/review/browse?")) return reply(200, { cells: [], has_more: false });
    if (url === "/api/review/disputes") return reply(200, { disputes: [] });
    if (url === "/api/review/species") return reply(200, { species: RV.catalogue });
    return reply(404, { error: "not stubbed" });
  };
  const browseButton = document.createElement("button");
  document.querySelector = (sel) => (sel === '#rv-nav button[data-view="browse"]' ? browseButton : null);
  const badge = () => browseButton.children.map((b) => (b.hidden ? "" : textOf(b))).join("");
  const admin = () => document.getElementById("rv-admin");
  const button = (text) => find(admin(), (n) => n.tag === "button" && textOf(n) === text);
  const field = (name) => attr(admin(), "name", name);
  const radio = (value) => find(admin(), (n) => n.attrs && n.attrs.type === "radio" && n.attrs.value === value);
  const alertOf = () => attr(admin(), "role", "alert");
  const notThis = () => all(admin(), (n) => n.tag === "button" && textOf(n) === "Not this species");
  const card = () => textOf(find(admin(), (n) => n.className === "rv-sg"));
  // ai #49's "New species" panel: "" once nothing waits, as it is then left out.
  const waitingPanel = () => find(admin(), (n) => (n.className || "").includes("rv-species-waiting"));
  const heading = () => textOf(find(waitingPanel(), (n) => n.tag === "h4"));
  // "Species added by reviewers": a card per addition on the list, removed ones folded away.
  const additionsPanel = () => find(admin(), (n) => (n.className || "").includes("rv-additions"));
  const liveCards = () => all(additionsPanel(), (n) => n.className === "rv-sg rv-addition");
  const removedFold = () => find(additionsPanel(), (n) => n.tag === "details"
    && (n.className || "").includes("rv-sg-history"));
  const within = (root, text) => find(root, (n) => n.tag === "button" && textOf(n) === text);
  const radioIn = (root, value) => find(root, (n) => n.attrs && n.attrs.type === "radio" && n.attrs.value === value);
  const alertIn = (root) => attr(root, "role", "alert");
  const figures = (root) => all(root, (n) => n.tag === "figure");
  const posted = () => calls.filter((c) => c.body);
  // Whether a request matching `wanted` went out after the first request for `url`.
  const askedAfter = (url, wanted) => {
    const at = calls.findIndex((c) => c.url === url);
    return at >= 0 && calls.slice(at + 1).some((c) => wanted(c.url));
  };
  const catalogueReload = (u) => u === "/api/review/species";
  const gridReload = (u) => u.startsWith("/api/review/browse?");
"""
)


@needs_node
def test_the_panel_lists_pending_species_and_approves(tmp_path):
    out = run_node(
        [_read("review-core.js"), _read("review-browse.js")],
        ADMIN_STUB
        + """
      await RV.views.browse.enter(); await settle();
      const history = find(admin(), (n) => n.tag === "details");
      const shown = { heading: heading(), badge: badge(), card: card(), thumbs: notThis().length,
        prefilled: ["scientific_name", "common_name", "life_stage"].map((k) => field(k).value),
        filter: find(document.getElementById("rv-browse"), (n) => n.tag === "datalist").children
          .map((o) => o.attrs.value),
        history: { open: "open" in history.attrs, text: textOf(history) } };
      field("common_name").value = " Spotted goatfish ";
      field("life_stage").value = "adult";
      field("note").value = "Checked the barbels";
      button("Approve").on.click({}); button("Approve").on.click({});   // a double click sends it once
      await settle();
      const approved = "/api/review/species-suggestions/3/approve";
      return { shown, posted: posted(), heading: heading(), badge: badge(),
        reloaded: [askedAfter(approved, catalogueReload), askedAfter(approved, gridReload)] };
    """,
        tmp_path,
    )
    shown = out["shown"]
    assert shown["heading"] == "New species (1 waiting)"
    assert shown["badge"] == "1"
    for text in (
        "Spotted Goatfish",
        "Pseudupeneus maculatus · juvenile",
        "Two barbels under the chin",
        "Suggested by Roy",
        "2 answers",
    ):
        assert text in shown["card"], text
    assert shown["thumbs"] == 2
    assert shown["prefilled"] == ["Pseudupeneus maculatus", "Spotted Goatfish", "juvenile"]
    # no species waiting for approval
    assert shown["filter"] == ["Haemulon chrysargyreum", "Scarus iseri"]
    assert shown["history"]["open"] is False
    for text in ("Banded goby", "not added", "Smallmouth grunt", "A grunt, seen side on."):
        assert text in shown["history"]["text"], text
    for text in ("Striped Parrotfish", "Yellowtail Snapper"):  # additions have their own panel
        assert text not in shown["history"]["text"], text
    assert out["posted"] == [
        {
            "url": "/api/review/species-suggestions/3/approve",
            "body": {
                "scientific_name": "Pseudupeneus maculatus",
                "common_name": "Spotted goatfish",
                "life_stage": "adult",
                "note": "Checked the barbels",
            },
        }
    ]
    assert out["heading"] == ""  # nothing waits: the panel is left out
    assert out["badge"] == ""
    assert out["reloaded"] == [True, True]  # the pickers' list and the grid's names


@needs_node
def test_not_this_species_posts_detach(tmp_path):
    out = run_node(
        [_read("review-core.js"), _read("review-browse.js")],
        ADMIN_STUB
        + """
      globalThis.boxes = Array.from({ length: 26 }, (_, i) => 101 + i);
      globalThis.waiting[0].answers = 26;
      await RV.views.browse.enter(); await settle();
      const first = notThis().length;
      button("Next →").on.click({}); await settle();
      const second = notThis().length;
      notThis()[0].on.click({}); notThis()[0].on.click({});   // a double click sends it once
      await settle();
      const after = { thumbs: notThis().length, card: card() };
      notThis()[0].on.click({}); await settle();             // the page's last box: back a page
      return { first, second, after, back: notThis().length, posted: posted(),
        pages: calls.filter((c) => c.url.includes("/boxes?")).map((c) => c.url.split("?")[1]),
        grid: askedAfter("/api/review/species-suggestions/3/detach", gridReload) };
    """,
        tmp_path,
    )
    assert (out["first"], out["second"]) == (24, 2)
    assert out["posted"] == [
        {"url": "/api/review/species-suggestions/3/detach", "body": {"event_id": 125}},
        {"url": "/api/review/species-suggestions/3/detach", "body": {"event_id": 126}},
    ]
    assert out["after"]["thumbs"] == 1
    assert "25 answers" in out["after"]["card"] and "26 answers" not in out["after"]["card"]
    assert out["back"] == 24
    assert out["pages"] == ["page=0", "page=1", "page=1", "page=1", "page=0"]
    assert out["grid"]  # the box's cell now says can't identify


REJECT_NEITHER = "choose “It's actually…” or “Can't identify”"


@needs_node
@pytest.mark.parametrize(
    "choice, body",
    [
        (
            "species",
            {
                "as_label": "Haemulon chrysargyreum",
                "as_life_stage": None,
                "unidentifiable": False,
                "note": None,
            },
        ),
        ("cant", {"as_label": None, "as_life_stage": None, "unidentifiable": True, "note": None}),
    ],
)
def test_reject_needs_a_choice(choice, body, tmp_path):
    out = run_node(
        [_read("review-core.js"), _read("review-browse.js")],
        ADMIN_STUB
        + f"""
      await RV.views.browse.enter(); await settle();
      const labels = ["It's actually…", "Can't identify"].map((t) =>
        Boolean(find(admin(), (n) => n.tag === "label" && textOf(n).startsWith(t))));
      button("Reject").on.click({{}}); await settle();       // neither chosen: nothing is sent
      const neither = {{ said: alertOf().textContent, shown: !alertOf().hidden, sent: posted().length }};
      radio("as").checked = true; radio("as").on.change({{}});
      const cleared = alertOf().hidden;                      // a choice clears the message
      button("Reject").on.click({{}}); await settle();       // "It's actually…" with no species
      const noSpecies = {{ said: alertOf().textContent, sent: posted().length }};
      const waitingOffered = offered(admin(), "spotted");    // never a species waiting for approval
      offered(admin(), "grunt");
      find(admin(), (n) => n.tag === "li" && textOf(n).startsWith("Smallmouth grunt")).on.mousedown(
        {{ preventDefault() {{}} }});
      const picked = {{ as: radio("as").checked, card: card(), cleared: alertOf().hidden }};
      if ("{choice}" === "cant") {{ radio("as").checked = false; radio("cant").checked = true; }}
      button("Reject").on.click({{}}); button("Reject").on.click({{}});
      await settle();
      return {{ labels, neither, cleared, noSpecies, waitingOffered, picked, posted: posted(),
        heading: heading(), reloaded: [askedAfter("/api/review/species-suggestions/3/reject", catalogueReload),
          askedAfter("/api/review/species-suggestions/3/reject", gridReload)] }};
    """,
        tmp_path,
    )
    assert out["labels"] == [True, True]
    assert out["neither"] == {"said": REJECT_NEITHER, "shown": True, "sent": 0}
    assert out["cleared"] is True
    assert out["noSpecies"] == {"said": "pick the species it actually is", "sent": 0}
    assert out["waitingOffered"] == ""
    assert out["picked"]["as"] is True and "Smallmouth grunt" in out["picked"]["card"]
    assert out["picked"]["cleared"] is True
    assert out["posted"] == [{"url": "/api/review/species-suggestions/3/reject", "body": body}]
    assert out["heading"] == ""  # nothing waits: the panel is left out
    assert out["reloaded"] == [True, True]


DECIDED_ALREADY = {
    "approve": 'button("Approve").on.click({});',
    "reject": 'radio("cant").checked = true; button("Reject").on.click({});',
    "detach": "notThis()[0].on.click({});",
}


@needs_node
@pytest.mark.parametrize("action", ["approve", "reject", "detach"])
def test_a_suggestion_decided_by_another_admin_reloads_the_lists(action, tmp_path):
    """Final review (promoted minor): another admin decided it first, so the server answers
    409 and the card is stale. The lists are reloaded, which drops it; the reason is a toast,
    since the card that would have shown it is gone."""
    out = run_node(
        [_read("review-core.js"), _read("review-browse.js")],
        ADMIN_STUB
        + f"""
      const stubbed = globalThis.fetch;
      globalThis.fetch = async (url, opts) => {{
        if (!url.endsWith("/3/{action}")) return stubbed(url, opts);
        calls.push({{ url, body: JSON.parse(opts.body) }});
        globalThis.waiting = [];                       // the other admin's decision
        return {{ status: 409, ok: false,
          json: async () => ({{ error: "this suggestion was already decided" }}) }};
      }};
      await RV.views.browse.enter(); await settle();
      {DECIDED_ALREADY[action]}
      await settle();
      return {{ heading: heading(), card: Boolean(find(admin(), (n) => n.className === "rv-sg")),
        reloaded: askedAfter("/api/review/species-suggestions/3/{action}",
          (u) => u === "/api/review/species-suggestions?status=pending"),
        toast: document.getElementById("rv-toast").textContent }};
    """,
        tmp_path,
    )
    assert out == {
        "heading": "",  # nothing waits: the panel is left out
        "card": False,
        "reloaded": True,
        "toast": "this suggestion was already decided",
    }


@needs_node
def test_admins_see_the_waiting_count_on_the_browse_button(tmp_path):
    core = _read("review-core.js")
    at = core.index('document.addEventListener("DOMContentLoaded"')
    handler = _braces_from(core, core.index("{", core.index("=>", at)))
    assert re.search(r'RV\.role === "admin" \? RV\.refreshSpeciesBadge\(\)', handler)
    assert handler.index("RV.refreshSpeciesBadge()") < handler.index("await RV.next(landing);")
    out = run_node(
        [core],
        ADMIN_STUB
        + """
      await RV.refreshSpeciesBadge();
      const one = badge();
      RV.setSpeciesBadge(0);
      const zero = badge();
      RV.setSpeciesBadge(3);
      return { one, zero, three: badge(), spans: browseButton.children.map((b) => b.className),
        asked: calls.map((c) => c.url) };
    """,
        tmp_path,
    )
    assert out == {
        "one": "1",
        "zero": "",
        "three": "3",
        "spans": ["rv-badge"],
        "asked": ["/api/review/species-suggestions?status=pending"],
    }


@needs_node
def test_a_box_waiting_on_a_new_species_is_not_badged_verified(tmp_path):
    """Final review I1: its own status, and the spec's words, not "✓ verified"."""
    out = run_node(
        [_read("review-core.js"), _read("review-browse.js")],
        ADMIN_STUB
        + """
      RV.role = "reviewer";
      const cellOf = (event_id, status) => ({ kind: "box", event_id, added_id: null, saved_frame_id: 7,
        zoom_url: "/z.jpg", status, outcome: "species", label: "Seriola zonata",
        common: "Juvenile Banded Rudderfish (new, waiting for approval)", camera: "seahive",
        ts: "2026-06-16T12:00:00Z", lane: "check", mine: true, reviewers: ["Roy"],
        last_at: "2026-11-21T15:00:00Z" });
      const stubbed = globalThis.fetch;
      globalThis.fetch = async (url, opts) => (url.startsWith("/api/review/browse?")
        ? { status: 200, ok: true, json: async () => ({ has_more: false,
            cells: [cellOf(101, "awaiting_species"), cellOf(102, "single")] }) }
        : stubbed(url, opts));
      await RV.views.browse.enter(); await settle();
      return document.getElementById("rv-grid").children.map((c) =>
        textOf(find(c, (n) => (n.className || "").startsWith("rv-badge"))));
    """,
        tmp_path,
    )
    assert out == ["waiting for approval", "✓ verified"]


@needs_node
@pytest.mark.parametrize("role", ["reviewer", "admin"])
def test_browse_never_shows_the_word_null(role, tmp_path):
    """Browser walk 2026-11-21: Browse put the word "null" under its counts (DOM
    replaceChildren() turns a null argument into that text) when the numbers were fresh,
    and again at the bottom for a reviewer, who has no admin section."""
    out = run_node(
        [_read("review-core.js"), _read("review-browse.js")],
        ADMIN_STUB
        + f"""
      RV.role = "{role}";
      await RV.views.browse.enter(); await settle();
      return document.getElementById("rv-browse").children.map((c) => (c === null ? "null" : c.tag));
    """,
        tmp_path,
    )
    assert "null" not in out
    assert out[-1] == ("section" if role == "admin" else "div")


# ---- spec 2026-11-21-species-worms-check: "Species added by reviewers" -----------------------
# The admin's main species panel: each species a reviewer added, with its names and stage, its
# WoRMS record, who added it and when, how many answers use it and its boxes (24 a page, asked for
# when the admin opens them); Remove "as" a listed species (never an ai #49 one still waiting) or
# can't identify, with a note. Removed ones fold away.

REMOVE_URL = "/api/review/species-additions/7/remove"


@needs_node
def test_the_panel_lists_additions_and_removes_one(tmp_path):
    out = run_node(
        [_read("review-core.js"), _read("review-browse.js")],
        ADMIN_STUB
        + """
      globalThis.waiting = [];
      globalThis.addedBoxes = Array.from({ length: 26 }, (_, i) => 201 + i);
      await RV.views.browse.enter(); await settle();
      const live = liveCards()[0];
      const shown = { headings: all(admin(), (n) => n.tag === "h4").map(textOf), cards: liveCards().length,
        card: textOf(live), link: find(live, (n) => n.tag === "a").attrs,
        boxesAsked: calls.some((c) => c.url.includes("/species-additions/7/boxes")),
        removed: { open: "open" in removedFold().attrs, text: textOf(removedFold()) } };
      // Its boxes, 24 at a time, asked for once the admin opens them.
      const used = find(live, (n) => n.tag === "details" && (n.className || "").includes("rv-sg-used"));
      used.open = true; used.on.toggle({}); await settle();
      const first = figures(live).length;
      within(live, "Next →").on.click({}); await settle();
      const second = { thumbs: figures(live).length, by: textOf(find(live, (n) => n.tag === "figcaption")) };
      used.open = false; used.on.toggle({}); used.open = true; used.on.toggle({}); await settle();
      // Remove it as a listed species.
      const waitingOffered = offered(live, "spotted");
      offered(live, "grunt");
      find(live, (n) => n.tag === "li" && textOf(n).startsWith("Smallmouth grunt")).on.mousedown(
        { preventDefault() {} });
      const picked = { as: radioIn(live, "as").checked,
        chosen: textOf(find(live, (n) => n.className === "rv-sg-chosen")) };
      attr(live, "name", "note").value = " A grunt, seen side on ";
      within(live, "Remove").on.click({}); within(live, "Remove").on.click({});   // a double click sends it once
      await settle();
      return { shown, first, second, waitingOffered, picked, posted: posted(),
        pages: calls.filter((c) => c.url.includes("/species-additions/7/boxes")).map((c) => c.url.split("?")[1]),
        toast: document.getElementById("rv-toast").textContent,
        after: { cards: liveCards().length, panel: textOf(additionsPanel()), removed: textOf(removedFold()) },
        reloaded: [askedAfter("REMOVE", catalogueReload),
          askedAfter("REMOVE", (u) => u === "/api/review/species-additions"), askedAfter("REMOVE", gridReload)] };
    """.replace("REMOVE", REMOVE_URL),
        tmp_path,
    )
    shown = out["shown"]
    # ai #49's panel is left out while nothing waits.
    assert shown["headings"] == [
        "Species added by reviewers",
        "Disputes (0)",
        "Agreement on spot-checks",
        "Pool status",
    ]
    assert shown["cards"] == 1
    for text in (
        "Striped Parrotfish",
        "Scarus iseri · initial phase",
        "Labridae",
        "AphiaID 276052",
        "Added by Roy",
        "3 answers",
        "Seen on the pier",
    ):
        assert text in shown["card"], text
    # Once, with the link: the row's automatic note ("WoRMS AphiaID 276052") is not the reviewer's.
    assert shown["card"].count("AphiaID 276052") == 1
    assert shown["link"] == {
        "href": "https://www.marinespecies.org/aphia.php?p=taxdetails&id=276052",
        "target": "_blank",
        "rel": "noopener noreferrer",
    }
    assert shown["boxesAsked"] is False  # not until the admin opens them
    assert shown["removed"]["open"] is False
    for text in (
        "Removed (1)",
        "Yellowtail Snapper",
        "removed; its answers now say Smallmouth grunt",
        "Not a snapper: a grunt.",
    ):
        assert text in shown["removed"]["text"], text
    assert out["first"] == 24
    assert out["second"] == {"thumbs": 2, "by": "Roy"}
    assert out["pages"] == ["page=0", "page=1"]  # opened again: not asked again
    assert out["waitingOffered"] == ""  # never a species still waiting for approval
    assert out["picked"] == {"as": True, "chosen": "Smallmouth grunt"}
    assert out["posted"] == [
        {
            "url": REMOVE_URL,
            "body": {
                "as_label": "Haemulon chrysargyreum",
                "as_life_stage": None,
                "unidentifiable": False,
                "note": "A grunt, seen side on",
            },
        }
    ]
    assert out["toast"] == "Removed Striped Parrotfish; its answers now say Smallmouth grunt."
    assert out["after"]["cards"] == 0
    assert "Removed (2)" in out["after"]["removed"]
    assert "Striped Parrotfish" in out["after"]["removed"]
    assert out["reloaded"] == [True, True, True]  # the pickers' list, this panel and the grid


@needs_node
def test_remove_needs_a_choice(tmp_path):
    out = run_node(
        [_read("review-core.js"), _read("review-browse.js")],
        ADMIN_STUB
        + """
      globalThis.waiting = [];
      await RV.views.browse.enter(); await settle();
      const live = liveCards()[0];
      const labels = ["It's actually…", "Can't identify"].map((t) =>
        Boolean(find(live, (n) => n.tag === "label" && textOf(n).startsWith(t))));
      within(live, "Remove").on.click({}); await settle();      // neither chosen: nothing is sent
      const neither = { said: alertIn(live).textContent, shown: !alertIn(live).hidden, sent: posted().length };
      radioIn(live, "as").checked = true; radioIn(live, "as").on.change({});
      const cleared = alertIn(live).hidden;                     // a choice clears the message
      within(live, "Remove").on.click({}); await settle();      // "It's actually…" with no species
      const noSpecies = { said: alertIn(live).textContent, sent: posted().length };
      offered(live, "parrot");                                   // the species being removed itself
      find(live, (n) => n.tag === "li" && textOf(n).startsWith("Striped Parrotfish")).on.mousedown(
        { preventDefault() {} });
      within(live, "Remove").on.click({}); await settle();
      const itself = { said: alertIn(live).textContent, sent: posted().length };
      radioIn(live, "as").checked = false; radioIn(live, "cant").checked = true;
      within(live, "Remove").on.click({}); await settle();
      return { labels, neither, cleared, noSpecies, itself, posted: posted(),
        toast: document.getElementById("rv-toast").textContent };
    """,
        tmp_path,
    )
    assert out["labels"] == [True, True]
    assert out["neither"] == {"said": REJECT_NEITHER, "shown": True, "sent": 0}
    assert out["cleared"] is True
    assert out["noSpecies"] == {"said": "pick the species it actually is", "sent": 0}
    # Browser walk: its own picker lists it; the server's refusal would say "pick a species from
    # the list".
    assert out["itself"] == {"said": "pick the species it actually is, not this one", "sent": 0}
    assert out["posted"] == [
        {
            "url": REMOVE_URL,
            "body": {"as_label": None, "as_life_stage": None, "unidentifiable": True, "note": None},
        }
    ]
    assert out["toast"] == "Removed Striped Parrotfish; its answers now say can't identify."


@needs_node
def test_a_remove_409_reloads_the_lists(tmp_path):
    """Another admin removed it first (or the Species Log lists it now): the server answers 409
    and the card is stale. The panel and the species list are reloaded, and the reason is a
    toast, since the card that would have shown it is gone."""
    out = run_node(
        [_read("review-core.js"), _read("review-browse.js")],
        ADMIN_STUB
        + """
      globalThis.waiting = [];
      const stubbed = globalThis.fetch;
      globalThis.fetch = async (url, opts) => {
        if (url !== "REMOVE") return stubbed(url, opts);
        calls.push({ url, body: JSON.parse(opts.body) });
        globalThis.additions = globalThis.additions.map((s) => (s.id !== 7 ? s   // the other admin's removal
          : { ...s, status: "rejected", decided_at: "2026-11-21T16:00:00+00:00", decision_note: null }));
        return { status: 409, ok: false, json: async () => ({ error: "this species was already removed" }) };
      };
      await RV.views.browse.enter(); await settle();
      const live = liveCards()[0];
      radioIn(live, "cant").checked = true;
      within(live, "Remove").on.click({}); await settle();
      return { cards: liveCards().length, removed: textOf(removedFold()),
        toast: document.getElementById("rv-toast").textContent,
        reloaded: [askedAfter("REMOVE", (u) => u === "/api/review/species-additions"),
          askedAfter("REMOVE", catalogueReload)] };
    """.replace("REMOVE", REMOVE_URL),
        tmp_path,
    )
    assert out["cards"] == 0
    assert "Striped Parrotfish" in out["removed"]
    assert out["toast"] == "this species was already removed"
    assert out["reloaded"] == [True, True]

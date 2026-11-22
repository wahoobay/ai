"""The review page's shell and scripts. There is no browser-test harness
in this repo, so the JavaScript gets a syntax check here and a manual
checklist in Task 12."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
from js_source import NODE, STATIC, function_source, needs_node, run_node
from js_source import braces_from as _braces_from
from js_source import function_body as _function_body
from js_source import read as _read
from review_fixtures import login, make_user

SCRIPTS = ["review-core.js", "review-single.js", "review-frame.js", "review-browse.js"]


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
# only this one gets the guard.
BROWSE_MUTATING_HANDLERS = ("adjudicate",)


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


# ---- 2026-11-21: reviewers add a species from the picker and answer with it ----------
# Spec 2026-11-21 (reviewer species suggestions), "Answering with a suggestion": POST the
# suggestion, then the verdict. SUGGEST_STUB goes after FRAME_STUB: elements keep their
# listeners and attributes, so a test can click the link, fill the form and submit it.

SUGGEST_STUB = r"""
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
  const ADD = "Not on the list? Add a species";
  const addLink = (root) => find(root, (n) => n.tag === "button" && textOf(n) === ADD);
  const submitForm = (root) => find(root, (n) => n.tag === "form").on.submit({ preventDefault() {} });
  // Open the form under the picker in `root` and fill it in.
  const openForm = (root, fields) => {
    addLink(root).on.click({});
    for (const [name, value] of Object.entries(fields || {})) attr(root, "name", name).value = value;
  };
  // What the picker in `root` offers for `query`.
  const offered = (root, query) => {
    const input = attr(root, "type", "search");
    input.value = query;
    input.on.input();
    return textOf(attr(root, "role", "listbox"));
  };
  const ENTRY = { key: "suggestion:9", name: "Pseudupeneus maculatus", life_stage: "juvenile",
    common: "Spotted Goatfish (waiting for approval)", suggestion_id: 9, pending: true,
    in_species_log: false, in_classifier: false };
  const listed = RV.catalogue.slice();
  globalThis.suggestReply = { status: 200, data: ENTRY };
  globalThis.mine = [];
  const pageFetch = globalThis.fetch;
  globalThis.fetch = async (url, opts) => {
    const reply = (status, data) => ({ status, ok: status < 400, json: async () => data });
    const body = opts && opts.body ? JSON.parse(opts.body) : null;
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
      return reply(200, { species: [...listed, ENTRY] });
    }
    if (url === "/api/review/verdicts" && globalThis.verdictReply) {
      calls.push({ url, body });
      return reply(globalThis.verdictReply.status, globalThis.verdictReply.data);
    }
    return pageFetch(url, opts);
  };
  const sent = () => calls.filter((c) => c.body && !c.url.endsWith("/open"));
  // A box the queue served to the single view.
  const payload = (pass) => ({ item: { id: 11, event_id: 101, pass },
    box: { bbox: [10, 10, 50, 50], model_bbox: [10, 10, 50, 50], saved_frame_id: 7 },
    frame: { url: "/f.jpg", width: 1280, height: 720 }, zoom: { url: "/z.jpg", region: [0, 0, 200, 200] },
    other_boxes: [], model: null, hints: [], counts_toward: null, editable: true });
"""

SUGGESTION = {
    "common_name": "Spotted Goatfish",
    "scientific_name": "Pseudupeneus maculatus",
    "life_stage": "juvenile",
    "note": None,
}
SUGGESTED_ANSWER = {
    "label": "Pseudupeneus maculatus",
    "life_stage": "juvenile",
    "label_source": "suggested",
    "species_suggestion_id": 9,
}
FILLED = (
    '{ common_name: " Spotted Goatfish ", scientific_name: "Pseudupeneus maculatus", '
    'life_stage: "juvenile" }'
)


@needs_node
def test_adding_a_species_answers_the_frame_box_with_it(tmp_path):
    out = run_node(
        [_read("review-core.js"), _read("review-frame.js")],
        FRAME_STUB
        + SUGGEST_STUB
        + f"""
      await RV.views.frame.enter({{ savedFrameId: 7 }});    // box 1 is selected
      const root = document.getElementById("rv-frame");
      openForm(root, {FILLED});
      submitForm(root); submitForm(root);                  // a double click sends it once
      await settle();
      const asked = calls.map((c) => c.url).filter((u) => u.startsWith("/api/review/species")
        || u === "/api/review/verdicts");
      return {{ sent: sent(), asked, m: key("m") }};
    """,
        tmp_path,
    )
    suggestion, verdict = out["sent"]
    assert suggestion == {"url": "/api/review/species-suggestions", "body": SUGGESTION}
    assert verdict == {
        "url": "/api/review/verdicts",
        "body": {
            "event_id": 101,
            "review_item_id": 11,
            "outcome": "species",
            **SUGGESTED_ANSWER,
            "not_sure": False,
        },
    }
    # Your suggestions are read when the form opens; the list is reloaded before answering.
    assert out["asked"] == [
        "/api/review/species-suggestions/mine",
        "/api/review/species-suggestions",
        "/api/review/species",
        "/api/review/verdicts",
    ]
    assert out["m"] is False  # a species waiting for approval never marks the rest


@needs_node
def test_drawn_boxes_have_no_add_a_species_link(tmp_path):
    src = _read("review-frame.js")
    assert "RV.addSpeciesBlock(" in _function_body(src, "answerControls")
    for name in ("addedPanel", "pickerAndButtons"):
        body = _function_body(src, name)
        assert "suggestSpecies" not in body and "addSpeciesBlock" not in body, name
    out = run_node(
        [_read("review-core.js"), _read("review-frame.js")],
        FRAME_STUB
        + SUGGEST_STUB
        + """
      RV.catalogue = [...listed, ENTRY];
      const frameFetch = globalThis.fetch;
      globalThis.fetch = async (url, opts) => {
        const r = await frameFetch(url, opts);
        if (!url.endsWith("/open")) return r;
        const data = await r.json();
        data.added = [{ id: 5, bbox: [400, 300, 480, 360], mine: true, by: "you", outcome: "species",
          label: "Haemulon chrysargyreum", common: "Smallmouth grunt", life_stage: null,
          not_sure: false, zoom_url: "/a.jpg" }];
        return { status: 200, ok: true, json: async () => data };
      };
      await RV.views.frame.enter({ savedFrameId: 7 });    // box 1, drawn by the model
      const root = document.getElementById("rv-frame");
      const model = { link: Boolean(addLink(root)), offers: offered(root, "spotted") };
      find(root, (n) => n.tag === "li" && textOf(n).startsWith("+ drawn by you")).on.click({});
      const drawnBox = { link: Boolean(addLink(root)), offers: offered(root, "spotted") };
      return { model, drawnBox };
    """,
        tmp_path,
    )
    assert out["model"] == {
        "link": True,
        "offers": "Spotted Goatfish (waiting for approval)Pseudupeneus maculatus · juvenile",
    }
    assert out["drawnBox"] == {"link": False, "offers": ""}


@needs_node
def test_picking_a_pending_entry_answers_with_its_suggestion(tmp_path):
    out = run_node(
        [_read("review-core.js")],
        """
      return [RV.answerExtra({ pending: true, suggestion_id: 4, name: "x", life_stage: null }),
        RV.answerExtra({ key: "Haemulon plumierii|", name: "Haemulon plumierii", life_stage: null },
          "hint")];
    """,
        tmp_path,
    )
    assert out == [
        {"label": "x", "life_stage": None, "label_source": "suggested", "species_suggestion_id": 4},
        {"label": "Haemulon plumierii", "life_stage": None, "label_source": "hint"},
    ]
    assert "RV.answerExtra(entry, source)" in _function_body(
        _read("review-single.js"), "pickSpecies"
    )
    assert "RV.answerExtra(entry, source)" in _function_body(
        _read("review-frame.js"), "answerControls"
    )


@needs_node
def test_the_form_shows_the_server_refusal(tmp_path):
    out = run_node(
        [_read("review-core.js"), _read("review-frame.js")],
        FRAME_STUB
        + SUGGEST_STUB
        + """
      globalThis.suggestReply = { status: 409, data: { error: "already on the list as Bluefish" } };
      await RV.views.frame.enter({ savedFrameId: 7 });
      const root = document.getElementById("rv-frame");
      openForm(root, { common_name: "   " });
      submitForm(root); await settle();                    // no common name: nothing is sent
      const alert = attr(root, "role", "alert");
      const empty = { said: alert.textContent, shown: !alert.hidden, sent: sent().length };
      attr(root, "name", "common_name").value = "Bluefish";
      submitForm(root); await settle();
      const button = find(root, (n) => n.tag === "button" && textOf(n) === "Add and answer this box");
      return { empty, said: alert.textContent, shown: !alert.hidden, sent: sent(),
        ready: !button.disabled };
    """,
        tmp_path,
    )
    assert out["empty"] == {"said": "write the fish's common name", "shown": True, "sent": 0}
    assert out["said"] == "already on the list as Bluefish" and out["shown"]
    assert [c["url"] for c in out["sent"]] == ["/api/review/species-suggestions"]  # no verdict
    assert out["ready"]  # the button works again


def test_the_add_species_form_is_busy_guarded():
    _assert_busy_guarded(_function_body(_read("review-core.js"), "addSpecies"), "addSpecies")


@needs_node
def test_your_suggestions_say_what_became_of_each(tmp_path):
    out = run_node(
        [_read("review-core.js")],
        FRAME_STUB
        + SUGGEST_STUB
        + """
      RV.catalogue = [...listed, { key: "Coryphopterus glaucofraenum|", name: "Coryphopterus glaucofraenum",
        common: "Bridled Goby", life_stage: null }];
      RV.byKey = new Map(RV.catalogue.map((e) => [e.key, e]));
      const none = { scientific_name: null, life_stage: null, decision_note: null, rejected_to_name: null,
        rejected_to_stage: null, approved_common: null };
      globalThis.mine = [
        { ...none, id: 4, common_name: "Spotted Goatfish", life_stage: "juvenile",
          status_text: "waiting for approval" },
        { ...none, id: 3, common_name: "spotted goatfish", status_text: "added",
          approved_common: "Spotted Goatfish" },
        { ...none, id: 2, common_name: "Banded goby", status_text: "not added",
          rejected_to_name: "Coryphopterus glaucofraenum", decision_note: "It's a bridled goby." },
        { ...none, id: 1, common_name: "Blur", status_text: "not added" },
      ];
      const holder = document.createElement("div");
      RV.suggestSpecies(holder, () => {});
      await settle();
      return find(holder, (n) => n.tag === "ul").children.map(textOf);
    """,
        tmp_path,
    )
    assert out == [
        "Spotted Goatfish (juvenile) · waiting for approval",
        "spotted goatfish · added as Spotted Goatfish",
        "Banded goby · not added; your answers now say Bridled GobyIt's a bridled goby.",
        "Blur · not added; your answers now say can't identify",
    ]


@needs_node
def test_adding_a_species_answers_the_single_box_but_never_a_second_opinion(tmp_path):
    out = run_node(
        [_read("review-core.js"), _read("review-single.js")],
        FRAME_STUB
        + SUGGEST_STUB
        + f"""
      RV.catalogue = [...listed, ENTRY];
      const root = document.getElementById("rv-single");
      await RV.views.single.enter({{ payload: payload(2) }});     // a blind second opinion
      const blind = {{ link: Boolean(addLink(root)), offers: offered(root, "spotted") }};
      await RV.views.single.enter({{ payload: payload(1) }});
      const first = {{ link: Boolean(addLink(root)), offers: offered(root, "spotted") }};
      openForm(root, {FILLED});
      submitForm(root); await settle();
      return {{ blind, first, sent: sent() }};
    """,
        tmp_path,
    )
    assert out["blind"] == {"link": False, "offers": ""}
    assert (
        out["first"]["link"] and "Spotted Goatfish (waiting for approval)" in out["first"]["offers"]
    )
    suggestion, verdict = out["sent"]
    assert suggestion == {"url": "/api/review/species-suggestions", "body": SUGGESTION}
    assert verdict == {
        "url": "/api/review/verdicts",
        "body": {
            "event_id": 101,
            "review_item_id": 11,
            "outcome": "species",
            **SUGGESTED_ANSWER,
            "not_sure": False,
        },
    }


# The volunteer moved on while the suggestion was being saved: the frame selects box 2,
# or the single view shows the next box. The new species answers neither.
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
        [_read("review-core.js"), _read(f"review-{view}.js")],
        FRAME_STUB
        + SUGGEST_STUB
        + f"""
      let reply;
      globalThis.gate = new Promise((r) => {{ reply = r; }});
      {enter}
      const root = document.getElementById("rv-{view}");
      openForm(root, {FILLED});
      submitForm(root); await settle();
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
@pytest.mark.parametrize("view", ["single", "frame"])
def test_a_new_species_decided_meanwhile_reloads_the_list_and_keeps_the_box(view, tmp_path):
    """The species list loaded before an admin approved (or rejected) the volunteer's new
    species: the verdict's 409 names that, and the page reloads the list instead of
    moving on from a box that is still theirs."""
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
      globalThis.verdictReply = {{ status: 409,
        data: {{ error: "that species suggestion was already decided; pick from the list" }} }};
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


NEW_SPECIES_HINT = "A new species can't be marked on the rest until it's approved."


@needs_node
def test_m_marks_nothing_right_after_a_new_species(tmp_path):
    """Task 7 review, Important 1: box 1 answered with a listed species, then a box with a
    new one. M must not offer the earlier species, and the panel says why; answering a
    listed species again brings M back."""
    out = run_node(
        [_read("review-core.js"), _read("review-frame.js")],
        FRAME_STUB
        + SUGGEST_STUB
        + f"""
      const shown = () => textOf(document.getElementById("rv-frame"));
      await RV.views.frame.enter({{ savedFrameId: 7 }});
      key("a"); await settle();                     // a listed species: M would mark with it
      const root = document.getElementById("rv-frame");
      openForm(root, {FILLED});
      submitForm(root); await settle();             // then a new one
      const afterNew = {{ m: key("m"), text: shown() }};
      key("a"); await settle();                     // a listed species again
      return {{ afterNew, again: key("m"), againText: shown(), marks: posted().filter((c) =>
        c.url.endsWith("/mark-rest")).length }};
    """,
        tmp_path,
    )
    assert out["afterNew"]["m"] is False
    assert NEW_SPECIES_HINT in out["afterNew"]["text"]
    assert HINT not in out["afterNew"]["text"] and "Mark the" not in out["afterNew"]["text"]
    assert out["again"] is True and NEW_SPECIES_HINT not in out["againText"]
    assert out["marks"] == 0  # M only asked

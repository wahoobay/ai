"""The review page's shell and scripts. There is no browser-test harness
in this repo, so the JavaScript gets a syntax check here and a manual
checklist in Task 12."""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
from review_fixtures import login, make_user

STATIC = Path(__file__).resolve().parents[1] / "services" / "dashboard" / "app" / "static"
NODE = shutil.which("node") or "/raid/scratch/dzimmerman2021/miniconda3/envs/node/bin/node"
SCRIPTS = ["review-core.js", "review-single.js", "review-frame.js", "review-browse.js"]


@pytest.mark.skipif(not Path(NODE).exists(), reason="node is not available")
@pytest.mark.parametrize("name", SCRIPTS)
def test_script_parses(name):
    r = subprocess.run([NODE, "--check", str(STATIC / name)], capture_output=True, text=True,
                       timeout=60)
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

def _braces_from(text: str, open_index: int) -> str:
    """The `{...}` block starting at `open_index` (which must point at an
    opening brace), found by counting braces. Good enough for the specific
    functions and listener this module inspects: none of them hide a
    brace inside a string or template literal, so a naive counter finds
    the true matching close."""
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


def _function_body(text: str, name: str) -> str:
    pattern = r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{"
    m = re.search(pattern, text)
    assert m, f"no function named {name!r} found"
    return _braces_from(text, m.end() - 1)


# submit(), skip() and reopen() are review-single.js's three mutating
# (POST-issuing) actions a volunteer can trigger repeatedly by key or
# click; each must gate on the shared `busy` flag so a held-down key or a
# fast double click can't fire two overlapping requests for the same box
# (task-10-findings-r1.md #1 and #2).
MUTATING_HANDLERS = ("submit", "skip", "reopen")


@pytest.mark.parametrize("name", MUTATING_HANDLERS)
def test_mutating_handlers_share_the_busy_guard(name):
    body = _function_body((STATIC / "review-single.js").read_text(), name)
    assert re.search(r"\bbusy\b\s*\)\s*return", body), (
        f"{name}() must return early when busy is already true")
    assert re.search(r"\bbusy\s*=\s*true\b", body), f"{name}() must set busy = true"
    assert "finally" in body and re.search(r"busy\s*=\s*false", body), (
        f"{name}() must reset busy in a finally block")


# review-frame.js's own actions that send a request (Task 11): entering the
# view claims the frame's boxes, and each answer/draw/complete action
# mutates state, so the same re-entrancy guard applies to all of them.
FRAME_MUTATING_HANDLERS = ("enter", "answerModel", "saveAddedBox", "removeAddedBox", "complete",
                            "toSingle")


@pytest.mark.parametrize("name", FRAME_MUTATING_HANDLERS)
def test_frame_mutating_handlers_share_the_busy_guard(name):
    body = _function_body((STATIC / "review-frame.js").read_text(), name)
    assert re.search(r"\bbusy\b\s*\)\s*return", body), (
        f"{name}() must return early when busy is already true")
    assert re.search(r"\bbusy\s*=\s*true\b", body), f"{name}() must set busy = true"
    assert "finally" in body and re.search(r"busy\s*=\s*false", body), (
        f"{name}() must reset busy in a finally block")


# review-browse.js's own mutating admin actions (Task 12): settling a
# dispute and saving or clearing the date window. Loading the grid, the
# progress KPIs and the admin panels are plain re-reads (same class as
# review-single.js's loadNext() and review-frame.js's reload(), neither of
# which is in the lists above), so only these two get the guard.
BROWSE_MUTATING_HANDLERS = ("adjudicate", "save")


@pytest.mark.parametrize("name", BROWSE_MUTATING_HANDLERS)
def test_browse_mutating_handlers_share_the_busy_guard(name):
    body = _function_body((STATIC / "review-browse.js").read_text(), name)
    assert re.search(r"\bbusy\b\s*\)\s*return", body), (
        f"{name}() must return early when busy is already true")
    assert re.search(r"\bbusy\s*=\s*true\b", body), f"{name}() must set busy = true"
    assert "finally" in body and re.search(r"busy\s*=\s*false", body), (
        f"{name}() must reset busy in a finally block")


# Fix round 1, Important finding: review-single.js's and review-frame.js's
# acceptGuess() were a near-verbatim duplication of the same life-stage
# check (stage-less entry -> auto-accept; one remaining stage -> auto-accept
# with it; two or more -> a chip picker). Both must now delegate to one
# shared RV.acceptGuessFlow() in review-core.js instead of reimplementing
# the stage enumeration inline, so the rule can't drift between the views.
def test_accept_guess_uses_the_shared_helper():
    assert "RV.acceptGuessFlow" in (STATIC / "review-core.js").read_text(), (
        "the shared life-stage-picking helper must live in review-core.js"
    )
    for name in ("review-single.js", "review-frame.js"):
        body = _function_body((STATIC / name).read_text(), "acceptGuess")
        assert "RV.acceptGuessFlow(" in body, f"{name}'s acceptGuess() must call the shared helper"
        assert "RV.catalogue.filter" not in body, (
            f"{name}'s acceptGuess() must not reimplement the life-stage check inline")


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
    assert re.search(r"frameAllowed\(c\)\s*\?\s*RV\.el\(\"button\"[^\n]*Whole frame", src), (
        "the Whole-frame button must only be built when frameAllowed(c)")
    assert re.search(r'k === "f" && frameAllowed\(current\)', src), (
        "the F key must be ignored unless frameAllowed(current)")
    assert re.search(r"if \(frameAllowed\(current\)\)",
                     _function_body(src, "openFrame")), "openFrame() must check frameAllowed"
    assert src.count('RV.show("frame"') == 1, "the frame view is only entered via openFrame()"


def test_key_dispatcher_ignores_held_down_keys():
    src = (STATIC / "review-core.js").read_text()
    marker = 'document.addEventListener("keydown"'
    block = _braces_from(src, src.index("{", src.index(marker)))
    assert "ev.repeat" in block, (
        "the shared keydown dispatcher must ignore ev.repeat, or a held-down "
        "key re-fires its action every few milliseconds")


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
    # Fix round 1, Minor B: a blocked write must still be remembered for
    # this session, or F in the frame view can never reach single boxes
    # (setMode("single") silently drops and mode() falls back to "frame").
    assert re.search(r"RV\.modeValue\s*=\s*value", _function_body(core, "setMode")), (
        "setMode() must keep an in-memory copy before the storage write can fail")
    # Fix round 2, Minor (carried): RV.modeValue is only ever set by setMode
    # in this page's lifetime, so it must win over whatever storage holds --
    # otherwise a readable-but-unwritable store (a stale "frame" read
    # succeeds while the "single" write silently fails) overrides the
    # in-memory choice instead of the other way around.
    mode_body = _function_body(core, "mode")
    assert re.search(r"if \(RV\.modeValue\) return RV\.modeValue;", mode_body), (
        "mode() must prefer the in-memory value over a stored one")
    assert mode_body.index("RV.modeValue") < mode_body.index("localStorage"), (
        "the in-memory check must run before the storage read, not after it")
    assert 'return "frame"' in mode_body, "whole frame is still the ultimate fallback"


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
    assert "RV.next()" in tail[tail.index("}") + 1:], (
        "complete() must call RV.next() after busy is reset, or enter() refuses it")


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
    assert click_block.index("await RV.releaseAll()") < click_block.index("RV.next()"), (
        "the lane button must release before it asks the queue again")


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
    assert re.search(r"if \(RV\.openFrameId !== id\) return;", pre), (
        "reload() must skip the re-open once the volunteer has already left this frame")
    assert re.search(r"if \(RV\.openFrameId !== id\)", post), (
        "reload() must notice a leave that happened while its POST was in flight")
    assert "/api/review/frames/${id}/release" in post, (
        "a late reload must give back the claims it just re-acquired, not strand them")


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
    assert re.search(r"return\s*\{\s*enter\s*,\s*isBusy\b", frame_src), (
        "review-frame.js must expose isBusy alongside enter")
    core = _read("review-core.js")
    marker = 'document.addEventListener("DOMContentLoaded"'
    block = _braces_from(core, core.index("{", core.index(marker)))
    click_marker = 'addEventListener("click"'
    click_block = _braces_from(block, block.index("{", block.index(click_marker)))
    guard = re.search(r'if \([^\n]*isBusy\(\)\s*\)\s*return;', click_block)
    assert guard, "the nav click handler must ignore a click while the frame view is busy"
    assert re.search(r"RV\.views\.frame\s*&&\s*RV\.views\.frame\.isBusy\s*&&\s*"
                      r"RV\.views\.frame\.isBusy\(\)", guard.group(0)), (
        "the guard must check the frame view's busy state directly")
    assert "RV.view ===" not in guard.group(0), (
        "the guard must not also require RV.view === \"frame\" -- a stale, no-longer-current "
        "frame view can still be busy and must still be caught")


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
    assert re.search(r"if \(!reopened\)\s*arg\.payload\s*=\s*current;", body), (
        "openFrame() must offer the current box as a fallback payload, unless it's reopened")
    assert "RV.setMode(\"frame\")" in body, "F must still remember the frame preference"
    assert body.index("RV.show(\"frame\"") < body.index("RV.setMode(\"frame\")"), (
        "the mode must only be remembered once the frame has actually opened")


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
FRAME_LEAVE_AWARE_ACTIONS = ("answerModel", "saveAddedBox", "removeAddedBox", "complete", "toSingle")


@pytest.mark.parametrize("name", FRAME_LEAVE_AWARE_ACTIONS)
def test_frame_actions_refuse_once_the_frame_has_been_left(name):
    body = _function_body((STATIC / "review-frame.js").read_text(), name)
    left_guard = re.search(FRAME_LEFT_GUARD, body)
    assert left_guard, (
        f"{name}() must refuse once RV.openFrameId no longer matches this frame")
    busy_check = re.search(r"\bbusy\b\s*\)\s*return", body)
    assert busy_check, f"{name}() must still return early when busy is already true"
    assert left_guard.start() < busy_check.start(), (
        f"{name}()'s openFrameId guard must come before its busy check")

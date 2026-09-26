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
FRAME_MUTATING_HANDLERS = ("enter", "answerModel", "saveAddedBox", "removeAddedBox", "complete")


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

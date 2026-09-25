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
SCRIPTS = ["review-core.js", "review-single.js", "review-frame.js"]        # Task 12 adds its own


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

"""Reading and running the dashboard's browser scripts in tests.

There is no browser harness in this repo. Most page checks are textual:
function_body() finds one function's source. Where timing matters,
run_node() runs the real script under node with a minimal DOM stub."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "services" / "dashboard" / "app" / "static"
NODE = shutil.which("node") or "/raid/scratch/dzimmerman2021/miniconda3/envs/node/bin/node"
needs_node = pytest.mark.skipif(not Path(NODE).exists(), reason="node is not available")


def read(name: str) -> str:
    return (STATIC / name).read_text()


def braces_from(text: str, open_index: int) -> str:
    """The `{...}` block starting at `open_index` (which must point at an
    opening brace), found by counting braces. Good enough for the functions
    these tests inspect: none hides a brace inside a string or template
    literal."""
    assert text[open_index] == "{", "open_index must point at '{'"
    depth = 0
    for j in range(open_index, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return text[open_index : j + 1]
    raise AssertionError("unbalanced braces")


def _declaration(text: str, name: str) -> re.Match:
    m = re.search(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{", text)
    assert m, f"no function named {name!r} found"
    return m


def function_body(text: str, name: str) -> str:
    m = _declaration(text, name)
    return braces_from(text, m.end() - 1)


def function_source(text: str, name: str) -> str:
    """The whole declaration, `function name(...) { ... }`, ready to run."""
    m = _declaration(text, name)
    return text[m.start() : m.end() - 1] + braces_from(text, m.end() - 1)


DOM_STUB = r"""
const __els = {};
function __el(tag) {
  return { tag, hidden: true, textContent: "", className: "", dataset: {}, children: [],
    classList: { add() {}, remove() {}, toggle() {} }, setAttribute() {}, addEventListener() {},
    append(...c) { this.children.push(...c); }, replaceChildren(...c) { this.children = c; } };
}
globalThis.document = { body: { dataset: {} }, getElementById: (id) => (__els[id] ||= __el("div")),
  createElement: (tag) => __el(tag), addEventListener() {}, querySelectorAll: () => [] };
globalThis.window = { addEventListener() {}, location: { href: "" } };
"""


def run_node(sources: list[str], body: str, tmp_path: Path):
    """Run browser-script sources under node after DOM_STUB, then `body` as
    the body of an async function. Returns its return value, via JSON. Exits
    as soon as the value is printed, so page timers (toasts) don't hold the
    run open."""
    script = tmp_path / "page-test.js"
    script.write_text(
        DOM_STUB
        + "\n".join(sources)
        + "\n(async () => {\n"
        + body
        + "\n})().then(\n  (v) => { console.log(JSON.stringify(v)); process.exit(0); },"
        "\n  (e) => { console.error(e); process.exit(1); });\n"
    )
    r = subprocess.run([NODE, str(script)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])

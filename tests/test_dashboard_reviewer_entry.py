"""The header's account chip. Signed in as an active reviewer or admin, a
"Review fish" link to /review (demo walk-through 2026-09-26); admins also
get "Accounts". Since 2026-09-30 the whole site is behind sign-in, so the
chip has nothing to show a signed-out visitor. Static checks: no browser
harness in this repo."""

from __future__ import annotations

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "services" / "dashboard" / "app" / "static"


def _chip() -> str:
    js = (STATIC / "auth.js").read_text()
    m = re.search(r"async function renderIdentityChip\(\)\s*\{", js)
    assert m, "auth.js must define renderIdentityChip()"
    depth, i = 0, m.end() - 1
    for j in range(i, len(js)):
        depth += {"{": 1, "}": -1}.get(js[j], 0)
        if depth == 0:
            return js[i : j + 1]
    raise AssertionError("unbalanced braces")


def test_signed_out_the_chip_shows_nothing():
    body = _chip()
    assert "if (!me.authenticated) return;" in body
    assert body.index("if (!me.authenticated) return;") < body.index("slot.hidden = false")
    assert '"/register"' not in body


def test_admins_get_a_link_to_the_accounts_page():
    body = _chip()
    assert re.search(
        r'active && me\.role === "admin"\) parts\.push\(link\("/admin/users\.html", "Accounts"',
        body,
    )


def test_active_reviewers_and_admins_get_a_review_link_except_on_review():
    body = _chip()
    assert re.search(r'me\.status === "active"', body)
    assert 'me.role === "reviewer"' in body and 'me.role === "admin"' in body
    assert 'pathname.startsWith("/review")' in body
    assert '"/review"' in body


def test_the_chip_builds_nodes_not_markup():
    assert "innerHTML" not in (STATIC / "auth.js").read_text()


def test_the_chip_is_styled_like_the_dashboard():
    css = (STATIC / "style.css").read_text()
    for selector in ("#identity-chip .chip-review", "#identity-chip .chip-signout"):
        assert re.search(re.escape(selector) + r"\s*\{", css), f"style.css needs {selector}"

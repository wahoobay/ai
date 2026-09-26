"""The main dashboard's way in for reviewers (demo walk-through 2026-09-26):
signed out, the header offers "Reviewers: Sign up · Log in"; signed in as an
active reviewer or admin, a "Review fish" link to /review. Static checks: no
browser harness in this repo."""
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
            return js[i:j + 1]
    raise AssertionError("unbalanced braces")


def test_signed_out_visitors_get_sign_up_and_log_in():
    body = _chip()
    signed_out = body[body.index("if (!me.authenticated)"):body.index("return;", body.index("if (!me.authenticated)"))]
    assert '"/register"' in signed_out and '"/login?next=/review"' in signed_out
    assert '"Reviewers:"' in signed_out


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
    for selector in ("#identity-chip.signed-out", "#identity-chip .chip-review",
                     "#identity-chip .chip-signout"):
        assert re.search(re.escape(selector) + r"\s*\{", css), f"style.css needs {selector}"

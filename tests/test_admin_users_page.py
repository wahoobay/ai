"""The accounts page (/admin/users.html) shares the rest of the site's look:
the dashboard's header, the palette picker and light/dark toggle, and each
table in a card (owner's request 2026-09-28). Static checks plus one render
of the page: there is no browser harness in this repo."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from review_fixtures import login, make_user

APP = Path(__file__).resolve().parents[1] / "services" / "dashboard" / "app"
PAGE = (APP / "templates" / "users_admin.html").read_text()


def _rule(css: str, selector: str) -> str:
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert m, f"style.css has no rule for {selector}"
    return m.group(1)


def _script() -> str:
    return PAGE[PAGE.index("<script>") : PAGE.index("</script>", PAGE.index("<script>"))]


def test_the_page_has_the_sites_header_with_a_way_back():
    header = PAGE[PAGE.index("<header") : PAGE.index("</header>")]
    assert re.search(
        r'<header class="auth-top">\s*<h1><a href="/">Wahoo Bay · Live Fish ID</a>', PAGE
    )
    assert 'href="/"' in header and 'href="/review"' in header
    assert "{{ me }}" in header


def test_each_table_sits_in_a_card_in_one_centred_column():
    css = (APP / "static" / "style.css").read_text()
    page = _rule(css, ".admin-page")
    assert re.search(r"display:\s*block", page), "escape style.css's two-column <main> grid"
    assert re.search(r"margin:[^;]*auto", page)
    assert "var(--auth-card-bg)" in _rule(css, ".admin-card")
    for table in ("pending-table", "users-table", "sessions-table"):
        at = PAGE.index(f'id="{table}"')
        assert PAGE.rfind('class="admin-card"', 0, at) > PAGE.rfind("</section>", 0, at)


def test_data_is_set_as_text_and_times_are_eastern():
    script = _script()
    assert "innerHTML" not in script
    assert "replaceChildren(" in script
    assert "fmtET(" in script
    head = PAGE[: PAGE.index("</head>")]
    assert "/static/time-et.js?v={{ asset_version }}" in head


def test_status_is_a_badge_and_empty_tables_say_so():
    script = _script()
    assert "admin-pill" in script and "status-${" in script
    assert "No one is waiting for approval." in script
    css = (APP / "static" / "style.css").read_text()
    for status in ("active", "pending", "disabled"):
        assert f".admin-pill.status-{status}" in css


@pytest.mark.anyio
async def test_the_page_renders_with_a_cache_busting_asset_version(rdb, rclient):
    await make_user(rdb, "boss", role="admin")
    login(rclient, "boss")
    r = rclient.get("/admin/users.html")
    assert r.status_code == 200
    assert re.search(r"/static/theme\.css\?v=[0-9a-f]+", r.text), "asset_version must be passed"
    assert "Signed in as" in r.text and "boss" in r.text

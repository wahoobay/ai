"""The sign-in, register, forgot-password and reset pages share the
dashboard's look (demo walk-through 2026-09-26). Static checks: there is no
browser harness in this repo."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1] / "services" / "dashboard" / "app"
PAGES = ["login.html", "register.html", "forgot_password.html", "reset_password.html"]


def _rule(css: str, selector: str) -> str:
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert m, f"style.css has no rule for {selector}"
    return m.group(1)


def test_the_card_escapes_the_dashboards_two_column_main_grid():
    # style.css gives every <main> `grid-template-columns: 3fr 2fr`, which
    # split the card into two columns (heading left, text right).
    css = (APP / "static" / "style.css").read_text()
    assert re.search(r"display:\s*block", _rule(css, ".auth-card"))


def test_hidden_messages_stay_hidden():
    # auth.js shows and hides the error/done messages and the forms with the
    # `hidden` attribute; an author `display:` rule would override it.
    css = (APP / "static" / "style.css").read_text()
    assert re.search(r"\.auth-card \[hidden\]\s*\{\s*display:\s*none\s*!important",
                     css)


def test_inputs_buttons_and_links_are_styled_like_the_dashboard():
    # Colours are variables on .auth-page whose defaults are the dashboard's
    # own dark palette; a theme class only swaps the values.
    css = (APP / "static" / "style.css").read_text()
    page = _rule(css, ".auth-page")
    assert "--auth-card-bg: var(--panel)" in page and "--auth-accent: var(--accent)" in page
    assert "var(--auth-card-bg)" in _rule(css, ".auth-card")
    assert "var(--auth-accent)" in _rule(css, ".auth-card button")
    assert "var(--auth-accent)" in _rule(css, ".auth-card a")
    assert "var(--auth-accent)" in _rule(css, ".auth-card input:focus")


@pytest.mark.parametrize("page", PAGES)
def test_each_page_has_the_dashboard_header(page):
    html = (APP / "templates" / page).read_text()
    assert re.search(r'<header class="auth-top">\s*<h1><a href="/">Wahoo Bay · Live Fish ID</a>',
                     html), f"{page} needs the dashboard's header bar"
    assert 'class="auth-card"' in html and 'class="auth-eyebrow"' in html


# A lighter, softer theme (demo walk-through 2026-09-26), switched on by one
# class so dropping it restores the dark look; ?theme=dark|light previews
# either on any auth page.
def test_the_light_theme_only_swaps_the_variables():
    css = (APP / "static" / "style.css").read_text()
    light = _rule(css, ".auth-page.auth-light")
    for var in ("--auth-bg", "--auth-card-bg", "--auth-ink", "--auth-accent", "--auth-button-ink"):
        assert f"{var}:" in light, f"the light theme must set {var}"
    assert "var(--bg)" not in light and "var(--panel)" not in light


@pytest.mark.parametrize("page", PAGES)
def test_each_page_uses_the_light_theme(page):
    html = (APP / "templates" / page).read_text()
    assert '<body class="auth-page auth-light">' in html


def test_either_theme_can_be_previewed():
    js = (APP / "static" / "auth.js").read_text()
    assert 'get("theme")' in js
    assert 'classList.add("auth-light")' in js and 'classList.remove("auth-light")' in js

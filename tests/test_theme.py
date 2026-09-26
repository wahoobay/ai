"""Colour palettes for the review and sign-in pages (demo walk-through
2026-09-26): four palettes, each light and dark, picked in the page header
and remembered per browser. Static checks: no browser harness in this repo."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1] / "services" / "dashboard" / "app"
PALETTES = ("blue", "green", "reef", "sand")
MODES = ("light", "dark")
THEMED_PAGES = ["review.html", "login.html", "register.html", "forgot_password.html",
                "reset_password.html"]
# (text, background) token pairs that carry text somewhere on these pages
PAIRS = [("ink", "bg"), ("ink", "panel"), ("ink", "note-bg"), ("ink", "input-bg"),
         ("muted", "bg"), ("muted", "panel"), ("accent", "panel"), ("accent", "bg"),
         ("on-accent", "accent"), ("on-accent", "accent-hover"), ("on-good", "good"),
         ("good", "panel"), ("warn", "panel"), ("bad-ink", "panel"),
         ("banner-ink", "banner-bg"), ("error-ink", "error-bg"), ("done-ink", "done-bg")]


def _blocks() -> dict[tuple[str, str], dict[str, str]]:
    css = (APP / "static" / "theme.css").read_text()
    out = {}
    for pal, mode, body in re.findall(
            r':root\[data-palette="(\w+)"\]\[data-mode="(\w+)"\]\s*\{([^}]*)\}', css):
        out[(pal, mode)] = dict(re.findall(r"--([\w-]+):\s*([^;]+);", body))
    return out


def _lum(hexcolor: str) -> float:
    c = [int(hexcolor[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def _ratio(a: str, b: str) -> float:
    la, lb = sorted([_lum(a), _lum(b)], reverse=True)
    return (la + 0.05) / (lb + 0.05)


def test_every_palette_has_a_light_and_a_dark_block_with_the_same_tokens():
    blocks = _blocks()
    assert set(blocks) == {(p, m) for p in PALETTES for m in MODES}
    tokens = set(blocks[("blue", "light")])
    for key, block in blocks.items():
        assert set(block) == tokens, f"{key} defines a different token set"
    for needed in ("bg", "panel", "ink", "muted", "accent", "on-accent", "line", "focus-ring"):
        assert needed in tokens


@pytest.mark.parametrize("palette", PALETTES)
@pytest.mark.parametrize("mode", MODES)
def test_every_text_colour_meets_wcag_aa(palette, mode):
    t = _blocks()[(palette, mode)]
    low = [(fg, bg, round(_ratio(t[fg], t[bg]), 2)) for fg, bg in PAIRS
           if _ratio(t[fg], t[bg]) < 4.5]
    assert not low, f"{palette}/{mode} pairs below 4.5:1: {low}"


@pytest.mark.parametrize("page", THEMED_PAGES)
def test_each_themed_page_loads_the_palettes_early_and_has_the_controls(page):
    html = (APP / "templates" / page).read_text()
    assert '<html lang="en" data-palette="blue" data-mode="light">' in html
    head = html[:html.index("</head>")]
    assert "/static/theme.css?v={{ asset_version }}" in head
    assert '<script src="/static/theme.js?v={{ asset_version }}"></script>' in head, (
        "theme.js must load in <head>, undeferred, so there is no flash of the wrong theme")
    header = html[html.index("<header"):html.index("</header>")]
    assert 'id="theme-controls"' in header


def test_theme_js_guards_storage_and_builds_a_picker_and_toggle():
    js = (APP / "static" / "theme.js").read_text()
    for pal in PALETTES:
        assert f'["{pal}", ' in js
    assert js.count("try {") >= 2 and "localStorage.getItem" in js and "localStorage.setItem" in js
    assert 'createElement("select")' in js and 'createElement("button")' in js
    assert "innerHTML" not in js


def test_the_pages_read_the_palette_tokens():
    assert "--rv-line: var(--line, #1f3b4f)" in (APP / "static" / "review.css").read_text()
    style = (APP / "static" / "style.css").read_text()
    assert "--auth-line: var(--line, #1f3b4f)" in style
    assert "--auth-button-ink: var(--on-accent, #0b1a24)" in style


def test_the_asset_version_covers_the_theme_files():
    main = (APP / "main.py").read_text()
    assert 'BASE_DIR / "static" / "theme.css"' in main and 'BASE_DIR / "static" / "theme.js"' in main

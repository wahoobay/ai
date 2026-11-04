"""The dashboard's header shows the Wahoo Bay tree in white, before the title: a copy of the brand's
mark (brand/wahoobay-mark.svg) whose only change is the colour."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "services" / "dashboard" / "app"


def test_the_header_shows_the_white_mark_before_the_title():
    html = (APP / "templates" / "index.html").read_text()
    mark = '<img class="brand-mark" src="/static/brand/wahoobay-mark-white.svg?v=1" alt=""'
    assert mark in html
    assert html.index(mark) < html.index("<h1>Wahoo Bay")
    assert ".brand-mark {" in (APP / "static" / "style.css").read_text()


def test_the_white_mark_is_the_brand_mark_in_white():
    svg = (APP / "static" / "brand" / "wahoobay-mark-white.svg").read_text()
    assert 'style="color:#ffffff" fill="currentColor"' in svg
    assert "#0088D6" not in svg
    assert svg.count("<path") > 10 and 'viewBox="20.5 288 2020 2020"' in svg

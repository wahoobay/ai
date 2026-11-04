"""The guided tours (static/tour.js on driver.js) and booth mode (static/booth.js): both pages load them,
booth mode is switched by ?booth=1 / ?booth=0 before the page's own scripts run, every element a tour
points at exists in the page or the review tool's views, and the vendored driver.js is the published one."""

from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from review_fixtures import TEST_DSN, login, make_user

pytestmark = pytest.mark.anyio
ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "services" / "dashboard" / "app"
STATIC = APP / "static"


def _client(monkeypatch, env, tmp_path, **extra):
    from app.main import build_app

    for key, value in env.items():
        monkeypatch.setenv(key, value)
    conf = tmp_path / "replay.conf"
    conf.write_text("staging=replay/pier.mp4\nprod=\n")
    monkeypatch.setenv("DATABASE_URL", TEST_DSN)
    monkeypatch.setenv("DASHBOARD_COOKIE_SECURE", "false")
    monkeypatch.setenv("FEATURE_REVIEW_TOOL", "true")
    monkeypatch.setenv("REPLAY_CONF", str(conf))
    for key, value in extra.items():
        monkeypatch.setenv(key, value)
    return TestClient(build_app())


async def test_both_pages_load_booth_mode_first_and_the_tour_last(
    rdb, review_assets, monkeypatch, tmp_path
):
    await make_user(rdb, "rev", role="reviewer")
    with _client(
        monkeypatch, review_assets["env"], tmp_path, ENV="staging", WORKER_URL="http://worker:8081"
    ) as client:
        login(client, "rev")
        home = client.get("/").text
        review = client.get("/review").text
    for html in (home, review):
        head, body = html.split("</head>", 1)
        assert "/static/booth.js" in head  # before anything paints or review-core.js boots
        assert "/static/vendor/driver.css?v=1.10.2" in head and "/static/tour.css" in head
        assert body.index("/static/vendor/driver.js.iife.js?v=1.10.2") < body.index(
            "/static/tour.js"
        )
        assert 'id="tour-start"' in body and 'id="tour-tools"' in body
        assert 'data-env="staging"' in head
    assert home.index("/static/app.js") < home.index("/static/tour.js")
    assert review.index("/static/review-browse.js") < review.index("/static/tour.js")
    assert 'data-replaying="true"' in home
    assert "🌊 Pier, 2024" in home and "🪸 SeaHIVE" not in home  # the recording is the pier


async def test_the_camera_button_says_seahive_when_the_camera_plays(
    rdb, review_assets, monkeypatch, tmp_path
):
    await make_user(rdb, "rev", role="reviewer")
    with _client(
        monkeypatch, review_assets["env"], tmp_path, ENV="prod", WORKER_URL="http://worker:8081"
    ) as client:
        login(client, "rev")
        home = client.get("/").text
    assert "🪸 SeaHIVE" in home and 'data-replaying="false"' in home and 'data-env="prod"' in home


def test_booth_mode_is_switched_by_the_query_and_remembered():
    js = (STATIC / "booth.js").read_text()
    assert 'const KEY = "wb.booth"' in js
    assert 'if (asked === "1") window.localStorage.setItem(KEY, "1")' in js
    assert 'else if (asked === "0") window.localStorage.removeItem(KEY)' in js
    assert 'document.documentElement.dataset.booth = "1"' in js
    # The review tour is written for the whole-frame view: review-core.js's own key and value.
    core = (STATIC / "review-core.js").read_text()
    assert 'RV.MODE_KEY = "rv.mode"' in core
    assert 'window.localStorage.setItem("rv.mode", "frame")' in js


def test_booth_mode_resets_to_the_welcome_card_and_keeps_the_video_playing():
    js = (STATIC / "tour.js").read_text()
    assert "const BOOTH_IDLE_MS = 90 * 1000" in js
    assert 'window.location.href = "/"' in js and "sessionStorage.removeItem(WELCOMED)" in js
    # app.js's four-hour stop shows #live-resume; booth mode presses it.
    assert 'getElementById("live-resume")' in js and "resume.click()" in js
    assert 'id="live-resume"' in (APP / "templates" / "index.html").read_text()
    # The demo-copy note is staging's only: only production's answers train the AI.
    assert 'root.dataset.env === "staging"' in js


def test_the_home_tour_leaves_the_visitor_on_the_dashboard_and_nudges_later():
    """The home tour ends on the dashboard; the review page is offered by a card under the review button,
    NUDGE_DELAY_MS after the tour or the welcome card, once per visitor, without an overlay."""
    js = (STATIC / "tour.js").read_text()
    css = (STATIC / "tour.css").read_text()
    assert "Now it's yours" in js and "Your turn" not in js and "onDoneClick" not in js
    assert "const NUDGE_DELAY_MS = 30 * 1000" in js
    assert "Want to see how our experts confirm that the AI is correctly labeling our fish?" in js
    assert "run(homeSteps(), scheduleNudge)" in js
    assert 'addEventListener("click", () => { close(); scheduleNudge(); })' in js
    assert "if (!BOOTH || REVIEW_PAGE || seen(NUDGED)) return;" in js
    assert (
        "window.sessionStorage.removeItem(NUDGED)" in js
    )  # the reset offers it to the next visitor
    assert 'link.classList.add("wb-pulse")' in js and ".tour-review-link.wb-pulse" in css
    nudge = css[css.index(".wb-nudge {") :]
    nudge = nudge[: nudge.index("}")]
    assert "position: absolute" in nudge and "inset" not in nudge  # covers only itself
    assert "prefers-reduced-motion" in css


def test_every_element_a_tour_points_at_exists():
    js = (STATIC / "tour.js").read_text()
    home = (APP / "templates" / "index.html").read_text()
    review = (APP / "templates" / "review.html").read_text()
    views = "".join(
        (STATIC / f).read_text() for f in ("review-frame.js", "review-single.js", "review-core.js")
    )
    app_js = (STATIC / "app.js").read_text()
    for cls in ("stream-wrap", "current", "water-quality"):
        assert f'class="{cls}"' in home, cls
    for el_id in (
        "fe-top-species",
        "celeb-grid",
        "reports-body",
        "reports-toggle",
        "reports-window",
        "tab-fish-explorer",
        "tab-celebrities",
    ):
        assert f'id="{el_id}"' in home, el_id
    assert '<option value="168">7d</option>' in home
    assert 'data-tab="fish-explorer"' in home and 'data-tab="celebrities"' in home
    assert 'class="celeb-card"' in app_js and 'classList.contains("collapsed")' in app_js
    for cls in (
        "rv-frame-canvas",
        "rv-guess",
        "rv-row",
        "rv-checklist",
        "rv-fb-pending",
        "rv-next-frame",
        "rv-to-single",
        "rv-draw",
        "rv-legend-keys",
        "rv-side",
        "rv-layout",
    ):
        assert cls in views, cls
    assert 'id: "rv-frame-panel"' in views
    assert 'id="rv-session-count"' in review and 'class="rv-meta"' in review
    assert 'class="rv-main"' in review and 'id="rv-single"' in review
    # Every selector the tours hand to first() or querySelector is one of the above.
    selectors = set(re.findall(r'"([#.][\w-]+(?: [#.][\w-]+)*)"', js))
    known = {
        ".stream-wrap",
        ".live-embed",
        ".current",
        ".water-quality",
        "#fe-top-species",
        "#celeb-grid",
        "#celeb-grid .celeb-card",
        "#reports-panel",
        ".rv-frame-canvas",
        "#rv-single .rv-layout",
        "#rv-frame-panel .rv-guess",
        "#rv-single .rv-guess",
        "#rv-frame-panel",
        "#rv-single .rv-side",
        "#rv-frame-panel .rv-row",
        "#rv-single .rv-row",
        "#rv-frame-panel .rv-checklist .rv-fb-pending",
        ".rv-next-frame",
        ".rv-meta",
        ".wb-go",
        ".wb-look",
        ".wb-nudge",
        ".wb-nudge-go",
        ".wb-nudge-close",
        ".rv-main",
    }
    assert selectors <= known, selectors - known


def test_the_tour_words_avoid_jargon():
    """Written for conservationists: no "model", "confidence", "bounding box", "inference" or keyboard keys."""
    js = (STATIC / "tour.js").read_text()
    words = " ".join(re.findall(r'(?:title|description): (?:\(REPLAYING\s*\?\s*)?"([^"]+)"', js))
    words += " ".join(re.findall(r'^\s+(?:\+ |: )?"([^"]{20,})"', js, flags=re.M))
    assert words
    for jargon in ("model", "confidence", "bounding", "inference", "press S", "press F"):
        assert jargon not in words.lower(), jargon


def _sri(path: Path) -> str:
    return "sha512-" + base64.b64encode(hashlib.sha512(path.read_bytes()).digest()).decode()


def test_the_vendored_driver_js_is_the_published_release():
    readme = (STATIC / "vendor" / "README.md").read_text()
    for name in ("driver.js.iife.js", "driver.css"):
        assert f"`{_sri(STATIC / 'vendor' / name)}`" in readme, name
    assert "Version: 1.10.2" in readme

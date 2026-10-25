"""The main dashboard's script (app.js)."""

from __future__ import annotations

import re
from pathlib import Path

from js_source import function_body, function_source, needs_node, read, run_node

TEMPLATES = Path(__file__).resolve().parents[1] / "services" / "dashboard" / "app" / "templates"


def test_the_old_form_shows_the_review_rules_reason_for_a_refusal():
    body = function_body(read("app.js"), "postCorrection")
    assert "resp.status === 403 && !data.error" in body
    assert "alert(data.error ||" in body


@needs_node
def test_alert_text_is_set_as_text_never_as_markup(tmp_path):
    app = read("app.js")
    assert "innerHTML" not in function_body(app, "renderAlerts")
    out = run_node(
        [
            'const AGE_FMT = () => "5m"; function ackAlert() {}',
            function_source(app, "renderAlerts"),
        ],
        """
      renderAlerts([{ id: 1, name: "<img src=x onerror=alert(1)>", message: "<b>loud</b>",
                      severity: "warning", first_seen: "2026-09-26T12:00:00Z",
                      acknowledged_at: "2026-09-26T12:01:00Z" }]);
      const texts = [];
      const walk = (n) => {
        if (typeof n === "string") { texts.push(n); return; }
        if (n.textContent) texts.push(n.textContent);
        (n.children || []).forEach(walk);
      };
      walk(document.getElementById("alerts-banner"));
      return texts;
    """,
        tmp_path,
    )
    assert "<img src=x onerror=alert(1)>" in out
    assert ": <b>loud</b>" in out
    assert "ack'd" in out  # no acknowledger name reaches a public viewer


def test_the_dashboard_formats_every_time_through_the_eastern_helpers():
    app = read("app.js")
    assert not re.search(r"toLocale(String|TimeString|DateString)\(", app)
    assert not re.search(r"\.get(Hours|Minutes|Month|Date)\(", app)
    pages = [p for p in TEMPLATES.glob("*.html") if "static/app.js" in p.read_text()]
    assert pages
    for page in pages:
        html = page.read_text()
        assert "static/time-et.js" in html, page.name
        assert html.index("static/time-et.js") < html.index("static/app.js"), page.name


@needs_node
def test_eastern_times_follow_daylight_saving(tmp_path):
    out = run_node(
        [read("time-et.js")],
        """
      return {
        summer: fmtET("2026-07-01T14:40:56Z", "time"),
        winter: fmtET("2026-01-15T15:05:00Z", "time"),
        full: fmtET("2026-07-01T14:40:56Z"),
        short: fmtET("2026-07-01T14:40:56Z", "short"),
        day: fmtET("2026-07-01T03:30:00Z", "day"),
        date: fmtET("2026-07-01T03:30:00Z", "date"),
        parts: etParts("2026-07-01T03:30:00Z"),
        midnight: etParts("2026-07-01T04:05:00Z").hour,
      };
    """,
        tmp_path,
    )
    # newer ICU puts a narrow no-break space before AM/PM
    out = {k: v.replace(" ", " ") if isinstance(v, str) else v for k, v in out.items()}
    assert out["summer"] == "10:40:56 AM EDT"
    assert out["winter"] == "10:05:00 AM EST"
    assert out["full"] == "7/1/2026, 10:40:56 AM EDT"
    assert out["short"] == "Jul 1, 10:40 AM EDT"
    assert out["day"] == "Jun 30"
    assert out["date"] == "6/30/2026"
    assert out["parts"] == {"month": 6, "day": 30, "hour": 23, "minute": 30}
    assert out["midnight"] == 0


def test_the_old_forms_buttons_need_a_saved_frame():
    """Final review F3: eventActionsHtml (shared by the list card's
    renderEventCard and the ROI modal's openRoiModal) offers the old
    form's endorse/correct buttons only on a detection with a saved frame
    -- POST /api/corrections refuses anything else outright."""
    app = read("app.js")
    body = function_body(app, "eventActionsHtml")
    assert "r?.image_path" in body or "r.image_path" in body
    assert "no saved frame, so it can't be reviewed" in body


@needs_node
def test_the_old_forms_buttons_render_only_with_a_saved_frame(tmp_path):
    app = read("app.js")
    out = run_node(
        ["const EVENT_CACHE = new Map();", function_source(app, "eventActionsHtml")],
        """
      EVENT_CACHE.set(1, { id: 1, image_path: "frames/1.jpg", best_name: "Seriola zonata" });
      EVENT_CACHE.set(2, { id: 2, image_path: null, best_name: "Seriola zonata" });
      return { withFrame: eventActionsHtml(1), withoutFrame: eventActionsHtml(2) };
    """,
        tmp_path,
    )
    assert "ev-btn" in out["withFrame"]
    assert "ev-btn" not in out["withoutFrame"]
    assert "no saved frame, so it can't be reviewed" in out["withoutFrame"]

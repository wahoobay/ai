"""The main dashboard's script (app.js)."""
from __future__ import annotations

from js_source import function_body, function_source, needs_node, read, run_node


def test_the_old_form_shows_the_review_rules_reason_for_a_refusal():
    body = function_body(read("app.js"), "postCorrection")
    assert "resp.status === 403 && !data.error" in body
    assert "alert(data.error ||" in body


@needs_node
def test_alert_text_is_set_as_text_never_as_markup(tmp_path):
    app = read("app.js")
    assert "innerHTML" not in function_body(app, "renderAlerts")
    out = run_node(['const AGE_FMT = () => "5m"; function ackAlert() {}',
                    function_source(app, "renderAlerts")], """
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
    """, tmp_path)
    assert "<img src=x onerror=alert(1)>" in out
    assert ": <b>loud</b>" in out
    assert "ack'd" in out        # no acknowledger name reaches a public viewer

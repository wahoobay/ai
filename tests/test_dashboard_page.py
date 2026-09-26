"""The main dashboard's script (app.js)."""
from __future__ import annotations

from js_source import function_body, read


def test_the_old_form_shows_the_review_rules_reason_for_a_refusal():
    body = function_body(read("app.js"), "postCorrection")
    assert "resp.status === 403 && !data.error" in body
    assert "alert(data.error ||" in body

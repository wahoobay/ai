"""Entry point of the jobs and migrate Lambdas (image CMD app.lambda_entry.handler).

The event names the job: {"job": "refresh_matviews" | "alert_rules" | "migrate"};
the EventBridge rules and the stack's first-deploy migrate call send exactly that.
"""

from __future__ import annotations

from typing import Any

from .jobs import run_job


def handler(event: dict[str, Any] | None, context: Any) -> dict[str, Any]:
    return run_job(str((event or {}).get("job", "")))

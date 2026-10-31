"""Entry point of the jobs and migrate Lambdas (image CMD app.lambda_entry.handler).

The event names the job: {"job": "refresh_matviews" | "alert_rules" | "migrate"};
the EventBridge rules and the stack's first-deploy migrate call send exactly that.
The result is returned and also printed as one JSON line: an asynchronous invocation
(the run-job-staging workflow) has no caller to return it to, and the runtime keeps
the root logger at WARNING, so stdout is what reaches the function's log group.
"""

from __future__ import annotations

import json
from typing import Any

from .jobs import run_job


def handler(event: dict[str, Any] | None, context: Any) -> dict[str, Any]:
    result = run_job(str((event or {}).get("job", "")))
    print(json.dumps(result, default=str), flush=True)
    return result

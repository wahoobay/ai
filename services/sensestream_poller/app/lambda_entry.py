"""Entry points of the fetch and write Lambdas.

fetch_handler  one poll of every source -> messages on the SQS queue (QUEUE_URL);
               no database (the stack runs it outside the VPC)
write_handler  SQS batches -> upserts (write_readings) and poller_status; a record
               that cannot be written is reported back as a batch item failure, so
               only it is retried and, after three tries, lands in the dead-letter
               queue while the rest of the batch is acknowledged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

from .config import settings
from .main import STATUS_NAMES, Config, DatabaseSink, fetch_once

log = logging.getLogger("wahoobay.sensestream.lambda")


def fetch_handler(event: Any, context: Any, *, sqs: Any = None, client: Any = None) -> dict:
    cfg = Config.from_env(require_database=False)
    messages = asyncio.run(fetch_once(cfg, client=client))
    queue_url = os.environ["QUEUE_URL"]
    if sqs is None:
        import boto3  # only the Lambda image carries it

        sqs = boto3.client("sqs")
    rows: dict[str, int] = {}
    for i in range(0, len(messages), 10):  # SQS takes ten entries a batch
        batch = messages[i : i + 10]
        entries = [
            {"Id": str(n), "MessageBody": json.dumps(m, default=str)} for n, m in enumerate(batch)
        ]
        result = sqs.send_message_batch(QueueUrl=queue_url, Entries=entries)
        if result.get("Failed"):
            raise RuntimeError(
                f"SQS refused {len(result['Failed'])} message(s): {result['Failed'][0]}"
            )
        for message in batch:
            rows[message["kind"]] = rows.get(message["kind"], 0) + len(message["rows"])
    log.info("fetch: %d message(s) sent, rows=%s", len(messages), rows)
    return {"messages": len(messages), "rows": rows}


def write_handler(event: Any, context: Any) -> dict:
    site = settings()
    failures: list[dict] = []
    status: dict[str, tuple[bool, str]] = {}
    for record in (event or {}).get("Records", []):
        message_id = record.get("messageId", "?")
        kind = None
        try:
            message = json.loads(record["body"])
            kind = message["kind"]
            sink = DatabaseSink(
                site.database_url, location_id=message.get("location_id") or site.location_id
            )
            n = sink.write(kind, message["rows"])
            log.info("write: %s %d row(s) for %s", kind, n, sink.location_id)
            status.setdefault(STATUS_NAMES.get(kind, kind), (True, ""))
        except Exception as e:  # noqa: BLE001 - reported back to SQS, not raised
            log.exception("write: record %s not written", message_id)
            failures.append({"itemIdentifier": message_id})
            if kind:
                status[STATUS_NAMES.get(kind, kind)] = (False, f"{type(e).__name__}: {e}")
    reporter = DatabaseSink(site.database_url, location_id=site.location_id)
    for name, (ok, error) in status.items():
        try:
            reporter.record_status(name, ok=ok, error=error)
        except Exception:  # noqa: BLE001
            log.exception("write: poller_status for %s not recorded", name)
    return {"batchItemFailures": failures}

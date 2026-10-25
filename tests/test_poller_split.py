"""The poller is two halves (Plan 2a, Task 5).

`fetch` asks the sources and produces messages (`{"kind", "location_id",
"rows"}`) without a database; `write` upserts them and reports poller_status.
Locally and on the DGX they run back to back in one process; on AWS the fetch
Lambda sends to SQS and the write Lambda drains it.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import httpx
import psycopg
from test_migrations import alembic, query

TS_MS = int(datetime(2026, 10, 24, 12, tzinfo=UTC).timestamp() * 1000)  # 2026-10-24T12:00:00Z
SONDE = {"time": TS_MS, "readings": [28.1, 8.05, 101.2, 0.9, 0.4, 1.3, 0.0, 53.2]}
STATION = {"time": TS_MS, "readings": [1013.2, 90, 80, 100, 3.1, 2.0, 4.5, 29.5, 70.0] + [0.0] * 15}


def fake_sources(request: httpx.Request) -> httpx.Response:
    """api.wahoobay.net answers; Open-Meteo, weather.gov and SenseStream are down."""
    if request.url.path == "/api/v1/sitedata":
        site = request.url.params.get("site")
        return httpx.Response(200, json=[SONDE if site == "3" else STATION])
    return httpx.Response(503, text="down")


def fake_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(fake_sources))


def _env(monkeypatch, **extra):
    base = {
        "DATABASE_URL": "postgresql://nobody@127.0.0.1:1/none",
        "LOCATION_ID": "reef_b",
        "SENSESTREAM_DEPLOYMENT_URI": "site_x",
        "WAHOOBAY_API_ENABLED": "1",
        "WAHOOBAY_API_URL": "https://api.example.test",
        "SENSESTREAM_AUTH_TOKEN": "",
        "QUEUE_URL": "https://sqs.example.test/123/readings",
    }
    for key, value in {**base, **extra}.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    monkeypatch.delenv("SENSESTREAM_SYNTHETIC", raising=False)


def sqs_record(message: dict, message_id: str) -> dict:
    return {"messageId": message_id, "body": json.dumps(message)}


class FakeSQS:
    def __init__(self):
        self.sent: list[tuple[str, list[dict]]] = []

    def send_message_batch(self, QueueUrl, Entries):  # noqa: N803 - boto3's spelling
        self.sent.append((QueueUrl, Entries))
        return {"Successful": [{"Id": e["Id"]} for e in Entries], "Failed": []}

    def messages(self) -> list[dict]:
        return [json.loads(e["MessageBody"]) for _, entries in self.sent for e in entries]


def test_fetch_produces_messages_without_touching_the_database(monkeypatch):
    from services.sensestream_poller.app.lambda_entry import fetch_handler

    _env(monkeypatch, DATABASE_URL=None)  # no database at all: the fetch side never needs one
    sqs = FakeSQS()
    out = fetch_handler({}, None, sqs=sqs, client=fake_client())
    messages = sqs.messages()
    assert {m["kind"] for m in messages} == {"sensor", "weather"}
    assert all(m["location_id"] == "reef_b" for m in messages)
    assert all(url == "https://sqs.example.test/123/readings" for url, _ in sqs.sent)
    sensor = next(m for m in messages if m["kind"] == "sensor")
    row = sensor["rows"][0]
    assert row["deployment_uri"] == "site_x" and row["source"] == "live"
    assert row["water_temp_c"] == 28.1 and row["ts"].startswith("2026-10-24T12:00:00")
    weather = next(m for m in messages if m["kind"] == "weather")
    assert weather["rows"][0]["air_temp_c"] == 29.5
    assert out["messages"] == len(messages) >= 2


def test_write_is_idempotent_for_duplicate_deliveries(pg_url, monkeypatch):
    from services.sensestream_poller.app.lambda_entry import write_handler

    alembic("upgrade", "head", url=pg_url)
    _env(monkeypatch, DATABASE_URL=pg_url)
    row = {
        "ts": "2026-10-24T12:00:00+00:00",
        "deployment_uri": "site_x",
        "source": "live",
        "water_temp_c": 28.1,
        "ph": 8.05,
    }
    record = sqs_record({"kind": "sensor", "location_id": "reef_b", "rows": [row]}, "m1")
    assert write_handler({"Records": [record, record]}, None) == {"batchItemFailures": []}
    write_handler({"Records": [record]}, None)
    assert query(pg_url, "SELECT count(*), min(location_id) FROM sensor_readings") == [
        (1, "reef_b")
    ]


def test_write_reports_poller_status_and_bad_records(pg_url, monkeypatch):
    from services.sensestream_poller.app.lambda_entry import write_handler

    alembic("upgrade", "head", url=pg_url)
    _env(monkeypatch, DATABASE_URL=pg_url)
    weather = {
        "ts": "2026-10-24T12:00:00+00:00",
        "deployment_uri": "site_x",
        "source": "live",
        "air_temp_c": 29.5,
        "bar_press_hpa": 1013.2,
    }
    good = sqs_record({"kind": "weather", "location_id": "reef_b", "rows": [weather]}, "good")
    bad = {"messageId": "bad", "body": "not json at all"}
    out = write_handler({"Records": [good, bad]}, None)
    assert out == {"batchItemFailures": [{"itemIdentifier": "bad"}]}
    assert query(pg_url, "SELECT count(*), min(location_id) FROM weather_readings") == [
        (1, "reef_b")
    ]
    status = query(pg_url, "SELECT name, last_ok IS NOT NULL FROM poller_status ORDER BY name")
    assert status == [("weather", True)]


def test_synthetic_rows_need_an_explicit_opt_in(monkeypatch):
    from services.sensestream_poller.app.main import Config, fetch_once

    # No public API, no token, every source down: nothing is fabricated ...
    _env(monkeypatch, WAHOOBAY_API_ENABLED="0")
    assert asyncio.run(fetch_once(Config.from_env(), client=fake_client())) == []
    # ... unless a local demo asks for it in so many words.
    monkeypatch.setenv("SENSESTREAM_SYNTHETIC", "true")
    messages = asyncio.run(fetch_once(Config.from_env(), client=fake_client()))
    assert {m["kind"] for m in messages} >= {"sensor"}
    assert all(r["source"] == "synthetic" for m in messages for r in m["rows"])


def test_the_local_loop_writes_and_reports_status(pg_url, monkeypatch):
    from services.sensestream_poller.app.main import Config, DatabaseSink, Poller

    alembic("upgrade", "head", url=pg_url)
    _env(monkeypatch, DATABASE_URL=pg_url)
    cfg = Config.from_env()
    poller = Poller(cfg, sink=DatabaseSink(pg_url, location_id="reef_b"))

    async def one_tick():
        async with fake_client() as client:
            await poller.run_once(client)

    asyncio.run(one_tick())
    assert query(pg_url, "SELECT count(*), min(location_id) FROM sensor_readings") == [
        (1, "reef_b")
    ]
    assert query(pg_url, "SELECT count(*) FROM weather_readings") == [(1,)]
    status = query(pg_url, "SELECT name, last_ok IS NOT NULL FROM poller_status ORDER BY name")
    assert status == [("sensestream", True), ("weather", True)]
    with psycopg.connect(pg_url) as conn:
        assert conn.execute(
            "SELECT count(*) FROM sensor_readings WHERE source = 'live'"
        ).fetchone() == (1,)

"""A committed sample dataset and a seed job (Plan 2a, Task 7): `make seed` fills
an empty database so the dashboard has something to show without a camera, and
the `seed` job does the same for staging from SAMPLE_URL."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from test_migrations import alembic, query

REPO = Path(__file__).resolve().parents[1]
SAMPLE = REPO / "data" / "sample"


def _rows(name: str) -> int:
    with (SAMPLE / f"{name}.jsonl").open() as fh:
        return sum(1 for line in fh if line.strip())


def test_the_committed_sample_is_the_size_the_dashboard_needs():
    assert _rows("detection_events") >= 300 and _rows("saved_frames") >= 40
    assert _rows("sensor_readings") >= 500 and _rows("weather_readings") >= 100
    assert _rows("alerts") >= 3
    frames = list((SAMPLE / "frames").glob("*.jpg"))
    assert len(frames) >= 40
    assert sum(p.stat().st_size for p in SAMPLE.rglob("*") if p.is_file()) < 3_000_000
    first = json.loads((SAMPLE / "detection_events.jsonl").read_text().splitlines()[0])
    assert {"ts", "frame_id", "source_name", "det_conf", "bbox", "topk", "track_id"} <= set(first)


def test_seed_loads_the_sample_into_an_empty_database(pg_url, tmp_path):
    from app.seed import seed

    alembic("upgrade", "head", url=pg_url)
    frames_url = f"file://{tmp_path}/frames"
    out = seed(pg_url, f"file://{SAMPLE}", location_id="reef_b", frames_url=frames_url)
    assert out["detection_events"] >= 300 and out["saved_frames"] >= 40
    assert out["sensor_readings"] >= 500 and out["weather_readings"] >= 100 and out["alerts"] >= 3
    assert query(pg_url, "SELECT count(*) FROM detection_events")[0][0] >= 300
    assert query(pg_url, "SELECT DISTINCT location_id FROM saved_frames") == [("reef_b",)]
    key = query(pg_url, "SELECT image_path FROM saved_frames ORDER BY id LIMIT 1")[0][0]
    assert key.startswith("reef_b/") and (tmp_path / "frames" / key).is_file()
    # Timestamps are shifted so the newest row is now: the page's windows show it.
    newest = query(pg_url, "SELECT max(ts) FROM detection_events")[0][0]
    assert (datetime.now(UTC) - newest).total_seconds() < 3600
    # The fish counter the page reads is refreshed.
    assert query(pg_url, "SELECT count(*) FROM species_sightings_mat")[0][0] > 0
    # A second seed does not duplicate.
    again = seed(pg_url, f"file://{SAMPLE}", location_id="reef_b", frames_url=frames_url)
    assert again == {"skipped": "the database already has detection_events"}
    assert query(pg_url, "SELECT count(*) FROM detection_events")[0][0] == out["detection_events"]


def test_seed_is_a_job(pg_url, tmp_path, monkeypatch):
    from app.jobs import run_job

    alembic("upgrade", "head", url=pg_url)
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("LOCATION_ID", "reef_b")
    monkeypatch.setenv("FRAMES_URL", f"file://{tmp_path}/frames")
    monkeypatch.setenv("SAMPLE_URL", f"file://{SAMPLE}")
    out = run_job("seed")
    assert out["ok"] is True and out["detail"]["saved_frames"] >= 40
    assert query(pg_url, "SELECT job, ok FROM job_runs") == [("seed", True)]

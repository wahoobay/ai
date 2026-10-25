"""The committed sample must read as fish on the dashboard. The first sign-in to
staging showed 320 seeded detections and "0 fish spotted today": the sightings
view counts a track only when its frames agree on one species three or more
times, and the generator used to pick a species per detection and barely let a
track linger."""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SAMPLE = REPO / "data" / "sample"


def _tracks(path: Path) -> dict[int, list[dict]]:
    by: dict[int, list[dict]] = defaultdict(list)
    for line in path.read_text().splitlines():
        if line.strip():
            row = json.loads(line)
            by[row["track_id"]].append(row)
    return by


def _reads_as_sightings(by: dict[int, list[dict]]) -> None:
    assert len(by) >= 40
    for track_id, rows in by.items():
        assert len({r["best_species_id"] for r in rows}) == 1, f"track {track_id} changes species"
        assert len({r["source_name"] for r in rows}) == 1, f"track {track_id} spans cameras"
    long_enough = sum(1 for rows in by.values() if len(rows) >= 3)
    assert (
        long_enough / len(by) >= 0.7
    ), f"{long_enough} of {len(by)} tracks have three or more frames"


def test_the_committed_sample_tracks_read_as_sightings():
    _reads_as_sightings(_tracks(SAMPLE / "detection_events.jsonl"))


def test_the_generator_reproduces_the_committed_sample(tmp_path):
    pytest.importorskip("numpy")
    pytest.importorskip("PIL")
    sys.path.insert(0, str(REPO / "scripts"))
    from make_sample_dataset import make

    counts = make(tmp_path)
    committed = (SAMPLE / "detection_events.jsonl").read_text()
    assert counts["detection_events"] == sum(1 for line in committed.splitlines() if line.strip())
    _reads_as_sightings(_tracks(tmp_path / "detection_events.jsonl"))
    assert (tmp_path / "detection_events.jsonl").read_text() == committed

"""Every row a writer inserts carries the site's location_id (Plan 2a, Task 3's
interface: "new rows are stamped from LOCATION_ID by the writers")."""

from __future__ import annotations

import sys
import types
from datetime import UTC, datetime

import numpy as np
import pytest
from test_migrations import alembic, query

pytestmark = pytest.mark.anyio


def _stub_cv2(monkeypatch):
    cv2 = types.ModuleType("cv2")
    cv2.IMWRITE_JPEG_QUALITY = 1
    cv2.imencode = lambda ext, img, params=None: (True, np.frombuffer(b"JPEG", dtype=np.uint8))
    monkeypatch.setitem(sys.modules, "cv2", cv2)


def test_the_worker_stamps_its_location_on_every_table(pg_url, monkeypatch):
    _stub_cv2(monkeypatch)
    from services.worker.app.persistence import PgWriter

    alembic("upgrade", "head", url=pg_url)
    pg = PgWriter(pg_url, provenance=None, location_id="reef_b")
    ts = datetime(2026, 10, 24, 15, 4, 5, tzinfo=UTC)
    try:
        pg.record_saved_frame(
            ts, 42, "seahivecam", "timelapse", "reef_b/x.jpg", "reef_b/x.coco.json", num_fish=1
        )
        pg.record_frame_stats([(ts, "seahivecam", 42, 100.0, 90.0, 95.0, 110.0, 12.0, 1, 0.9)])
        pg.record_ptz_state(ts, "seahivecam", 1.0, 2.0, 3.0, {"raw": True}, "vapix")
    finally:
        pg.close()
    for table in ("saved_frames", "frame_stats", "ptz_states"):
        assert query(pg_url, f"SELECT DISTINCT location_id FROM {table}") == [("reef_b",)], table


async def test_the_review_tool_stamps_the_location(rdb, review_assets, tmp_path, monkeypatch):
    from app.review_catalogue import CatalogueStore
    from review_fixtures import as_user, catalogue_paths, make_user, seed_frame
    from test_review_queue import _verdict

    monkeypatch.setenv("LOCATION_ID", "reef_b")
    catalogue = CatalogueStore(catalogue_paths(review_assets)).get()
    uid = await make_user(rdb, "rev")
    frame = await seed_frame(rdb, tmp_path, boxes=[{"bbox": [10, 10, 90, 90]}])
    # spot=1.0: the verdict also schedules a pass-2 review item through the same writer.
    await _verdict(
        rdb, catalogue, as_user(uid), spot=1.0, event_id=frame["event_ids"][0], outcome="no_animal"
    )
    assert query(rdb_dsn(), "SELECT DISTINCT location_id FROM detection_corrections") == [
        ("reef_b",)
    ]
    assert query(rdb_dsn(), "SELECT DISTINCT location_id FROM review_items") == [("reef_b",)]


def rdb_dsn() -> str:
    import os

    return os.environ["TEST_DATABASE_URL"]

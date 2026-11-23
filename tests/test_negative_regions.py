"""The no-fish zones reach the worker wherever it runs. They live behind one storage URL, NEGATIVE_REGIONS_URL
(on AWS the frames bucket's config/camera_negative_regions.json; on a laptop a file:// URL, or the file at
NEGATIVE_REGIONS_PATH): the dashboard's admin page writes it, the worker reads it and reads it again every
minute, so a save applies without a restart. Before this, the admin page on AWS saved into App Runner's own
container, which the worker never reads, and the worker image had no regions file at all."""

from __future__ import annotations

import json
import logging
import threading
import time

import pytest
from common import storage
from review_fixtures import CSRF, login, make_user
from test_storage import FakeS3

from services.worker.app import negative_regions
from services.worker.app.negative_regions import NegativeRegionFilter
from services.worker.app.pipeline_types import FishDetection

pytestmark = pytest.mark.anyio

URL = "s3://frames-bucket/config/camera_negative_regions.json"
KEY = ("frames-bucket", "config/camera_negative_regions.json")
HOLE = [[100, 100], [200, 100], [200, 180], [100, 180]]
HOLE_BOX = FishDetection((100, 100, 200, 180), 0.9, [])  # the detector's box on the hole itself
FISH_BOX = FishDetection((140, 120, 160, 140), 0.9, [])  # a fish swimming through the hole


def _config(*polygons, source="seahivecam") -> bytes:
    regions = [{"polygon": p} for p in polygons]
    return json.dumps(
        {"sources": {source: {"frame_w": 1280, "frame_h": 720, "regions": regions}}}
    ).encode()


def _kept(f: NegativeRegionFilter) -> list:
    kept, _ = f.filter_detections("seahivecam", [HOLE_BOX, FISH_BOX], 1280, 720)
    return kept


@pytest.fixture
def s3(monkeypatch):
    fake = FakeS3()
    monkeypatch.setattr(storage, "_s3_client", lambda: fake)
    return fake


@pytest.fixture(autouse=True)
def _regions_url(monkeypatch):
    # Autouse fixtures come first, so the dashboard rclient builds already reads this URL.
    monkeypatch.setenv("NEGATIVE_REGIONS_URL", URL)


def test_the_filter_reads_its_regions_from_a_storage_url(s3):
    storage.put(URL, _config(HOLE), "application/json")
    f = NegativeRegionFilter(URL)
    assert f.has_regions and _kept(f) == [FISH_BOX]


def test_a_plain_path_still_names_a_file_and_empty_still_disables(tmp_path):
    path = tmp_path / "camera_negative_regions.json"
    path.write_bytes(_config(HOLE))
    assert _kept(NegativeRegionFilter(str(path))) == [FISH_BOX]
    assert not NegativeRegionFilter("").has_regions and not NegativeRegionFilter(None).has_regions


def test_reload_applies_a_saved_change_and_says_so(s3):
    f = NegativeRegionFilter(URL)  # nothing saved yet: pass-through
    assert not f.has_regions and f.reload() is False
    storage.put(URL, _config(HOLE), "application/json")
    assert f.reload() is True and _kept(f) == [FISH_BOX]
    assert f.reload() is False  # unchanged
    storage.put(URL, _config(), "application/json")  # the admin cleared them
    assert f.reload() is True and not f.has_regions


def test_a_store_that_cannot_be_read_or_parsed_keeps_the_regions_in_force(s3, monkeypatch):
    storage.put(URL, _config(HOLE), "application/json")
    f = NegativeRegionFilter(URL)

    def unreachable(url):
        raise RuntimeError("S3 unreachable")

    monkeypatch.setattr(negative_regions, "open_read", unreachable)
    assert f.reload() is False and _kept(f) == [FISH_BOX]
    monkeypatch.setattr(negative_regions, "open_read", storage.open_read)
    storage.put(URL, b"{not json", "application/json")
    assert f.reload() is False and _kept(f) == [FISH_BOX]
    storage.put(URL, _config(), "application/json")  # a good save after the bad one still applies
    assert f.reload() is True and not f.has_regions


def test_every_applied_change_is_logged_and_a_failure_only_once(s3, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=negative_regions.__name__)
    storage.put(URL, _config(HOLE), "application/json")
    f = NegativeRegionFilter(URL)
    caplog.clear()
    storage.put(URL, _config([[110, 100], [210, 100], [210, 180], [110, 180]]), "application/json")
    assert f.reload() is True  # the polygon moved, the counts did not: still logged
    storage.put(URL, b"{not json", "application/json")
    assert f.reload() is False and f.reload() is False  # polled twice, logged once
    loaded = [r for r in caplog.records if "filter loaded" in r.getMessage()]
    failed = [r for r in caplog.records if "failed to parse" in r.getMessage()]
    assert len(loaded) == 1 and len(failed) == 1 and failed[0].levelname == "WARNING"


def test_a_config_that_is_gone_turns_the_filter_off_as_at_startup(s3):
    storage.put(URL, _config(HOLE), "application/json")
    f = NegativeRegionFilter(URL)
    del s3.objects[KEY]
    assert f.reload() is True and not f.has_regions


def test_watch_applies_a_save_without_a_restart(s3):
    f = NegativeRegionFilter(URL)
    stop = threading.Event()
    watcher = threading.Thread(target=f.watch, args=(stop, 0.01), daemon=True)
    watcher.start()
    storage.put(URL, _config(HOLE), "application/json")
    deadline = time.monotonic() + 5
    while not f.has_regions and time.monotonic() < deadline:
        time.sleep(0.01)
    stop.set()
    watcher.join(2)
    assert f.has_regions and not watcher.is_alive()


def test_a_region_saved_while_the_worker_runs_drops_the_box_from_the_next_frame(
    monkeypatch, tmp_path
):
    from test_worker_persist import _run

    url = f"file://{tmp_path}/camera_negative_regions.json"
    applied = threading.Event()
    real_reload = NegativeRegionFilter.reload

    def reload(self):
        changed = real_reload(self)
        if changed:
            applied.set()
        return changed

    monkeypatch.setattr(NegativeRegionFilter, "reload", reload)
    monkeypatch.setattr(negative_regions, "RELOAD_INTERVAL_S", 0.01)

    def on_frame(n):
        if n == 2:  # the admin saves a region over the fake detector's box between frames 1 and 2
            applied.clear()  # the first read, at the worker's start, counted as a change
            storage.put(url, _config([[1, 2], [30, 2], [30, 40], [1, 40]]), "application/json")
            assert applied.wait(5)

    calls = _run(monkeypatch, persist_enabled=False, regions_url=url, on_frame=on_frame)
    assert [o.n_detections for o in calls["out"]] == [1, 0, 0]


async def test_the_admin_page_saves_where_the_worker_reads(s3, monkeypatch, rdb, rclient):
    s3.put_object(
        Bucket=KEY[0],
        Key=KEY[1],
        Body=b'{"_about": "notes", "sources": {}}',
        ContentType="application/json",
    )
    await make_user(rdb, "boss", role="admin")
    login(rclient, "boss")
    assert rclient.get("/api/admin/negative_regions").json() == {"_about": "notes", "sources": {}}
    body = json.loads(_config(HOLE))
    r = rclient.post("/api/admin/negative_regions", json=body, headers=CSRF)
    assert r.status_code == 200, r.text
    saved, content_type = s3.objects[KEY]
    assert json.loads(saved) == {"_about": "notes", **body} and content_type == "application/json"
    assert rclient.get("/api/admin/negative_regions").json()["sources"] == body["sources"]
    assert _kept(NegativeRegionFilter(URL)) == [FISH_BOX]  # what the worker reads

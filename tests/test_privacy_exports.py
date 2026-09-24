"""Public exports must not publish who labelled what (spec §12.1), and a
box's frame is the one captured at the box's own timestamp (spec §12.3)."""
from __future__ import annotations

import io
from datetime import timedelta

import pytest
from PIL import Image
from review_fixtures import T0, add_verdict, login, make_user, seed_frame

pytestmark = pytest.mark.anyio

IDENTITY = {"reviewer", "notes", "user_id", "session_id"}


async def _seed_named_verdict(rdb, tmp_path):
    frame = await seed_frame(rdb, tmp_path, boxes=[{"bbox": [100, 100, 300, 250],
                                                    "species": "Seriola zonata"}])
    uid = await make_user(rdb, "alice")
    await add_verdict(rdb, frame["event_ids"][0], uid, name="Seriola zonata",
                      reviewer="Alice Example", notes="secret note")
    return frame


def _header(text: str) -> set[str]:
    return set(text.splitlines()[0].split(","))


async def test_anonymous_labeled_corrections_has_labels_but_no_identity(rdb, rclient, tmp_path):
    await _seed_named_verdict(rdb, tmp_path)
    r = rclient.get("/api/export/labeled_corrections.csv")
    assert r.status_code == 200
    header = _header(r.text)
    assert not (IDENTITY & header)
    assert {"corrected_name", "corrected_bbox", "unidentifiable", "life_stage",
            "label_source", "review_item_id", "is_adjudication", "is_endorsement"} <= header
    assert "Alice Example" not in r.text and "secret note" not in r.text


async def test_admin_labeled_corrections_includes_identity(rdb, rclient, tmp_path):
    await _seed_named_verdict(rdb, tmp_path)
    await make_user(rdb, "boss", role="admin")
    login(rclient, "boss")
    r = rclient.get("/api/export/labeled_corrections.csv")
    assert IDENTITY <= _header(r.text)
    assert "Alice Example" in r.text


async def test_anonymous_corrections_csv_and_list_hide_identity(rdb, rclient, tmp_path):
    await _seed_named_verdict(rdb, tmp_path)
    csv_text = rclient.get("/api/export/corrections.csv").text
    assert not (IDENTITY & _header(csv_text)) and "Alice Example" not in csv_text
    rows = rclient.get("/api/corrections").json()
    assert rows and all("reviewer" not in row and "notes" not in row for row in rows)


async def test_filtering_by_reviewer_is_admin_only(rdb, rclient, tmp_path):
    await _seed_named_verdict(rdb, tmp_path)
    assert rclient.get("/api/corrections?reviewer=Alice%20Example").status_code == 403
    assert rclient.get("/api/export/corrections.csv?reviewer=x").status_code == 403
    await make_user(rdb, "boss", role="admin")
    login(rclient, "boss")
    rows = rclient.get("/api/corrections?reviewer=Alice%20Example").json()
    assert [r["reviewer"] for r in rows] == ["Alice Example"]


async def test_roi_uses_the_frame_saved_at_the_detections_own_timestamp(rdb, rclient, tmp_path):
    # A worker restart reused frame_id 500: the red frame is from before the
    # restart, the green one from after. The detection belongs to the red one.
    before = await seed_frame(rdb, tmp_path, ts=T0, frame_id=500, color=(220, 10, 10),
                              boxes=[{"bbox": [100, 100, 300, 250]}])
    await seed_frame(rdb, tmp_path, ts=T0 + timedelta(hours=2), frame_id=500,
                     color=(10, 220, 10), boxes=[{"bbox": [100, 100, 300, 250]}])
    r = rclient.get(f"/api/detections/{before['event_ids'][0]}/roi.jpg")
    assert r.status_code == 200
    red, green, _ = Image.open(io.BytesIO(r.content)).convert("RGB").resize((1, 1)).getpixel((0, 0))
    assert red > 150 and green < 80


async def test_detection_whose_own_frame_was_not_saved_gets_no_image(rdb, rclient, tmp_path):
    await seed_frame(rdb, tmp_path, ts=T0, frame_id=500, boxes=[{"bbox": [1, 1, 50, 50]}])
    orphan = await seed_frame(rdb, tmp_path, ts=T0 + timedelta(hours=1), frame_id=500,
                              save=False, boxes=[{"bbox": [1, 1, 50, 50]}])
    assert rclient.get(f"/api/detections/{orphan['event_ids'][0]}/roi.jpg").status_code == 404
    assert rclient.get(f"/api/detections/{orphan['event_ids'][0]}/frame.jpg").status_code == 404


async def test_labeled_corrections_frame_path_matches_the_timestamp(rdb, rclient, tmp_path):
    before = await seed_frame(rdb, tmp_path, ts=T0, frame_id=500,
                              boxes=[{"bbox": [1, 1, 50, 50]}])
    after = await seed_frame(rdb, tmp_path, ts=T0 + timedelta(hours=2), frame_id=500,
                             boxes=[{"bbox": [1, 1, 50, 50]}])
    uid = await make_user(rdb, "alice")
    await add_verdict(rdb, before["event_ids"][0], uid, not_a_fish=True)
    text = rclient.get("/api/export/labeled_corrections.csv").text
    assert before["image_path"] in text and after["image_path"] not in text

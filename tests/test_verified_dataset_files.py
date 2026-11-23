"""The verified dataset's files: crops, detector frames, COCO, the manifest and the report, built
from a selection without the database (spec
docs/superpowers/specs/2026-11-22-verified-dataset-training-design.md §2)."""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app import verified_dataset
from app.verified_dataset_files import build_files, crop_box
from common import taxonomy
from PIL import Image

T = datetime(2026, 6, 1, 14, 0, tzinfo=UTC)
ZONE = ZoneInfo("America/New_York")
APHIA = {"Brachygenys chrysargyrea": 1841028}
MANIFEST_KEYS = {
    "box_id",
    "saved_frame_id",
    "camera",
    "ts",
    "local_date",
    "local_ts",
    "bbox",
    "bbox_source",
    "outcome",
    "accepted_name",
    "aphia_id",
    "life_stage",
    "confidence",
    "status",
    "label_source",
    "user_ids",
    "model_name",
    "model_score",
    "crop",
    "frame",
    "held_back",
}


def _jpeg(size=(1280, 720), color=(30, 60, 90)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG", quality=90)
    return buf.getvalue()


FRAME = _jpeg()


def _box(box_id, frame=7, **overrides) -> verified_dataset.Box:
    fields = dict(
        box_id=box_id,
        saved_frame_id=frame,
        camera="seahivecam",
        ts=T,
        bbox=(100, 100, 300, 200),
        bbox_source="model",
        outcome="species",
        accepted_name="Brachygenys chrysargyrea",
        life_stage=None,
        confidence="certain",
        status="single",
        label_source="search",
        user_ids=(3,),
        model_name="Brachygenys chrysargyrea",
        model_score=0.62,
        held_back=None,
    )
    return verified_dataset.Box(**(fields | overrides))


def _build(boxes, *, model=None, completed=True, frames=None):
    model = tuple(b.box_id for b in boxes if b.box_id.startswith("ev")) if model is None else model
    frame = verified_dataset.Frame(
        7, "seahivecam", T, "/f/7.jpg", completed, model, completed, 0, False
    )
    sel = verified_dataset.Selection(tuple(boxes), {7: frame}, 2, None, None)
    plan = verified_dataset.detector_frames(sel)
    store = {"/f/7.jpg": FRAME} if frames is None else frames

    def read(path):
        if path not in store:
            raise FileNotFoundError(path)
        return store[path]

    files, counts = build_files(sel, plan, read, APHIA, ZONE)
    manifest = json.loads(files["manifest.json"])
    coco = json.loads(files["detector/annotations.coco.json"])
    return files, counts, manifest, coco, files["report.txt"].decode()


def test_crop_box_pads_five_percent_and_clamps():
    assert crop_box((100, 100, 300, 200), 1280, 720) == (90, 95, 310, 205)
    assert crop_box((0, 0, 100, 100), 1280, 720) == (0, 0, 105, 105)
    assert crop_box((1300, 10, 1400, 50), 1280, 720) is None
    assert crop_box((50, 50, 50, 90), 1280, 720) is None


def test_species_and_no_animal_crops_are_filed_by_name():
    boxes = [
        _box("ev1"),
        _box("ev2", outcome="no_animal", accepted_name=None),
        _box("ev3", outcome="unidentifiable", accepted_name=None),
        _box("ev4", held_back="disputed"),
    ]
    files, counts, *_ = _build(boxes)

    crop = Image.open(io.BytesIO(files["classifier/Brachygenys_chrysargyrea/ev1.jpg"]))
    assert crop.size == (220, 110)
    assert "classifier/no_animal/ev2.jpg" in files
    assert not [k for k in files if k.endswith(("/ev3.jpg", "/ev4.jpg"))]
    assert counts["crops"] == {"Brachygenys_chrysargyrea": 1, "no_animal": 1}


def test_a_name_with_unsafe_characters_makes_one_folder():
    files, *_ = _build([_box("ev1", accepted_name="Kyphosus sectatrix/bigibbus")])

    assert [k for k in files if k.startswith("classifier/")] == [
        "classifier/Kyphosus_sectatrix_bigibbus/ev1.jpg"
    ]


def test_the_manifest_has_one_record_per_box():
    _, counts, manifest, *_ = _build([_box("ev1"), _box("add2", bbox_source="drawn")])

    rec = manifest["records"][0]
    assert [r["box_id"] for r in manifest["records"]] == ["ev1", "add2"]
    assert set(rec) == MANIFEST_KEYS
    assert (rec["ts"], rec["local_date"], rec["local_ts"]) == (
        "2026-06-01T14:00:00Z",
        "2026-06-01",
        "2026-06-01T10:00:00-04:00",
    )
    assert (rec["aphia_id"], rec["crop"], rec["frame"]) == (
        1841028,
        "classifier/Brachygenys_chrysargyrea/ev1.jpg",
        "detector/images/7.jpg",
    )
    assert (rec["bbox"], rec["user_ids"], rec["model_score"], rec["held_back"]) == (
        [100, 100, 300, 200],
        [3],
        0.62,
        None,
    )
    assert manifest["counts"] == counts and counts["sample_answers"] == 2


def test_coco_lists_detector_frames_with_xywh_boxes():
    boxes = [_box("ev1"), _box("ev2", outcome="no_animal", accepted_name=None)]
    files, counts, _, coco, _ = _build(boxes)

    assert coco["images"] == [
        {
            "id": 7,
            "file_name": "images/7.jpg",
            "width": 1280,
            "height": 720,
            "date_captured": "2026-06-01T14:00:00Z",
            "camera": "seahivecam",
        }
    ]
    assert coco["annotations"] == [
        {
            "id": 1,
            "image_id": 7,
            "bbox": [100, 100, 200, 100],
            "area": 20000,
            "iscrowd": 0,
            "category_id": 2,
            "box_id": "ev1",
            "bbox_source": "model",
            "label_source": "search",
        }
    ]
    assert coco["categories"] == [
        {"id": 1, "name": "unidentified animal", "aphia_id": None},
        {"id": 2, "name": "Brachygenys chrysargyrea", "aphia_id": 1841028},
    ]
    assert files["detector/images/7.jpg"] == FRAME
    assert (counts["detector_frames"], counts["detector_annotations"]) == (1, 1)


def test_a_missing_frame_holds_its_boxes_and_frame_back():
    boxes = [_box("ev1"), _box("ev2", outcome="unidentifiable", accepted_name=None)]
    files, counts, manifest, coco, report = _build(boxes, frames={})

    assert {r["held_back"] for r in manifest["records"]} == {"frame image missing"}
    assert coco["images"] == [] and "frame 7: frame image missing" in report
    assert not [k for k in files if k.startswith(("classifier/", "detector/images/"))]
    assert (counts["boxes_held_back"], counts["frames_held_back"]) == (2, 1)


def test_a_box_outside_the_frame_is_held_back():
    files, _, manifest, coco, report = _build([_box("ev1", bbox=(1300, 10, 1400, 50))])

    assert manifest["records"][0]["held_back"] == "box outside the frame"
    assert manifest["records"][0]["crop"] is None and manifest["records"][0]["frame"] is None
    assert coco["images"] == [] and "frame 7: box ev1 held back: box outside the frame" in report
    assert "ev1 (frame 7): box outside the frame" in report


def test_the_same_selection_builds_the_same_bytes():
    boxes = [_box("ev1"), _box("ev2", outcome="no_animal", accepted_name=None)]

    assert _build(boxes)[0] == _build(boxes)[0]


def test_taxonomy_maps_raw_names_to_accepted_names_and_aphia_ids(tmp_path):
    csv_path = tmp_path / "taxonomy.csv"
    csv_path.write_text(
        "raw_name,aphia_id,accepted_name,authority,rank\r\n"
        'Haemulon chrysargyreum,1841028,Brachygenys chrysargyrea,"(Günther, 1859)",Species\r\n'
        'Kyphosus sectatrixbigibbus,126015,Kyphosus,"Lacepède, 1801",Genus\r\n'
        "Unresolved thing,,,,\r\n",
        encoding="utf-8",
    )
    rows = taxonomy.read(csv_path)

    assert taxonomy.accepted(rows, "Haemulon_chrysargyreum") == "Brachygenys chrysargyrea"
    assert taxonomy.accepted(rows, "Not listed") == "Not listed"
    assert taxonomy.accepted(rows, "Unresolved thing") == "Unresolved thing"
    assert taxonomy.aphia_by_accepted(rows) == {
        "Brachygenys chrysargyrea": 1841028,
        "Kyphosus": 126015,
    }
    assert taxonomy.read(tmp_path / "missing.csv") == {}


def test_a_box_just_past_the_edge_is_outside_even_with_padding():
    assert crop_box((1281, 10, 1381, 50), 1280, 720) is None
    assert crop_box((-120, 10, -5, 50), 1280, 720) is None
    assert crop_box((1270, 10, 1300, 50), 1280, 720) == (1268, 8, 1280, 52)


def test_an_unreadable_frame_holds_its_boxes_and_frame_back():
    for data in (b"not a jpeg", FRAME[: len(FRAME) // 2]):
        boxes = [_box("ev1"), _box("ev2", outcome="no_animal", accepted_name=None)]
        files, counts, manifest, coco, report = _build(boxes, frames={"/f/7.jpg": data})

        assert {r["held_back"] for r in manifest["records"]} == {"frame image unreadable"}
        assert coco["images"] == [] and "frame 7: frame image unreadable" in report
        assert not [k for k in files if k.startswith(("classifier/", "detector/images/"))]

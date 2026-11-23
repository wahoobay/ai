"""scripts/assemble_training_set.py builds the classifier's training folder from the base crops
and a verified snapshot, in the layout scripts/train_baseline.py reads (spec
docs/superpowers/specs/2026-11-22-verified-dataset-training-design.md §3)."""

from __future__ import annotations

import io
import json
import sys
import tarfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from assemble_training_set import main  # noqa: E402

BASE = {
    "Haemulon_chrysargyreum": ("a", "Haemulon chrysargyreum"),
    "Pomacanthus_paru": ("b", "Pomacanthus paru"),
    "Kyphosus_sectatrixbigibbus": ("c", "Kyphosus sectatrix/bigibbus"),
}
TAXONOMY = [
    ("Haemulon chrysargyreum", "1841028", "Brachygenys chrysargyrea"),
    ("Brachygenys chrysargyrea", "1841028", "Brachygenys chrysargyrea"),
    ("Kyphosus sectatrixbigibbus", "126015", "Kyphosus"),
    ("Kyphosus", "126015", "Kyphosus"),
    ("Pomacanthus paru", "159587", "Pomacanthus paru"),
]
STAMP = "2026-11-23T120000Z"


def _base(root: Path, folders=BASE) -> Path:
    base = root / "base"
    records, tax = [], {}
    for folder, (crop_id, label) in folders.items():
        (base / folder).mkdir(parents=True)
        (base / folder / f"{crop_id}.jpg").write_bytes(f"jpeg {crop_id}".encode())
        records.append(
            {
                "crop_id": crop_id,
                "label": label,
                "drive_id": f"drive-{crop_id}",
                "source_kind": "drive:video",
                "frame_index": 0,
                "crop_path": f"data/fine_tune/{folder}/{crop_id}.jpg",
            }
        )
        tax[label] = {"common_name": label}
    records.append(
        {
            "crop_id": "gone",
            "label": "Pomacanthus paru",
            "crop_path": "data/fine_tune/Pomacanthus_paru/gone.jpg",
        }
    )
    (base / "_dataset_manifest.json").write_text(json.dumps(records))
    (base / "_taxonomy.json").write_text(json.dumps(tax))
    return base


def _taxonomy(root: Path) -> Path:
    path = root / "taxonomy.csv"
    lines = ["raw_name,aphia_id,accepted_name"] + [",".join(row) for row in TAXONOMY]
    path.write_text("\r\n".join(lines) + "\r\n")
    return path


def _record(box_id, outcome="species", name=None, crop=True, held_back=None):
    folder = "no_animal" if outcome == "no_animal" else (name or "").replace(" ", "_")
    return {
        "box_id": box_id,
        "camera": "seahivecam",
        "local_date": "2026-06-01",
        "local_ts": "2026-06-01T10:00:00-04:00",
        "bbox": [100, 100, 300, 200],
        "outcome": outcome,
        "accepted_name": name,
        "crop": f"classifier/{folder}/{box_id}.jpg" if crop else None,
        "held_back": held_back,
    }


SNAPSHOT_RECORDS = [
    _record("ev5", name="Brachygenys chrysargyrea"),
    _record("ev6", outcome="no_animal"),
    _record("ev7", name="Brachygenys chrysargyrea", crop=False, held_back="disputed"),
    _record("ev8", outcome="unidentifiable", crop=False),
    _record("ev9", name="Lutjanus apodus"),
]


def _snapshot(folder: Path, records=SNAPSHOT_RECORDS) -> Path:
    folder.mkdir(parents=True)
    (folder / "snapshot.json").write_text(json.dumps({"snapshot": STAMP, "content_hash": "abc"}))
    (folder / "manifest.json").write_text(json.dumps({"records": records, "counts": {}}))
    for r in records:
        if r["crop"]:
            (folder / r["crop"]).parent.mkdir(parents=True, exist_ok=True)
            (folder / r["crop"]).write_bytes(f"crop {r['box_id']}".encode())
    return folder


def manifest(out: Path) -> list[dict]:
    return json.loads((out / "_dataset_manifest.json").read_text())


def merge(out: Path) -> dict:
    return json.loads((out / "_review_merge.json").read_text())


def tree(out: Path) -> dict[str, bytes]:
    return {
        p.relative_to(out).as_posix(): p.read_bytes()
        for p in out.rglob("*")
        if p.is_file() and p.suffix == ".jpg"
    }


def _run(tmp_path, *extra, base=None, out=None) -> tuple[int, Path]:
    out = out or tmp_path / "out"
    args = ["--base", str(base or _base(tmp_path)), "--out", str(out)]
    code = main([*args, "--taxonomy", str(_taxonomy(tmp_path)), *extra])
    return code, out


def test_renamed_base_folders_take_the_new_names(tmp_path):
    code, out = _run(tmp_path)

    assert code == 0
    assert sorted(p.name for p in out.iterdir() if p.is_dir()) == [
        "Brachygenys_chrysargyrea",
        "Kyphosus",
        "Pomacanthus_paru",
    ]
    rec = next(r for r in manifest(out) if r["crop_id"] == "a")
    assert rec["label"] == "Brachygenys chrysargyrea"
    assert rec["crop_path"].endswith("Brachygenys_chrysargyrea/a.jpg")
    assert (REPO / rec["crop_path"]).resolve() == (out / "Brachygenys_chrysargyrea/a.jpg").resolve()
    assert next(r for r in manifest(out) if r["crop_id"] == "c")["label"] == "Kyphosus"
    assert next(r for r in manifest(out) if r["crop_id"] == "b")["label"] == "Pomacanthus paru"
    taxonomy_json = json.loads((out / "_taxonomy.json").read_text())
    assert sorted(taxonomy_json) == ["Brachygenys chrysargyrea", "Kyphosus", "Pomacanthus paru"]
    assert merge(out)["base_records_dropped"] == 1 and "gone" not in {
        r["crop_id"] for r in manifest(out)
    }


def test_review_crops_join_the_folder_of_their_accepted_name(tmp_path):
    code, out = _run(tmp_path, "--snapshot", str(_snapshot(tmp_path / "snap")))

    assert code == 0
    assert (out / "Brachygenys_chrysargyrea/review_ev5.jpg").read_bytes() == b"crop ev5"
    rec = next(r for r in manifest(out) if r["crop_id"] == "review_ev5")
    assert (rec["drive_id"], rec["source_kind"], rec["frame_index"]) == (
        "review:seahivecam:2026-06-01",
        "review:video",
        36000,
    )
    assert (rec["label"], rec["source_path"], rec["bbox"]) == (
        "Brachygenys chrysargyrea",
        f"{STAMP}/classifier/Brachygenys_chrysargyrea/ev5.jpg",
        [100, 100, 300, 200],
    )
    assert merge(out)["copied"] == {"Brachygenys_chrysargyrea": 1, "no_animal": 1}


def test_no_animal_crops_are_a_class_of_their_own(tmp_path):
    _, out = _run(tmp_path, "--snapshot", str(_snapshot(tmp_path / "snap")))

    assert (out / "no_animal/review_ev6.jpg").is_file()
    assert next(r for r in manifest(out) if r["crop_id"] == "review_ev6")["label"] == "no_animal"


def test_held_back_and_cropless_records_are_not_copied(tmp_path):
    _, out = _run(tmp_path, "--snapshot", str(_snapshot(tmp_path / "snap")))

    assert not list(out.rglob("review_ev7.jpg")) and not list(out.rglob("review_ev8.jpg"))


def test_a_species_without_a_folder_is_skipped_unless_allowed(tmp_path):
    snap, base = _snapshot(tmp_path / "snap"), _base(tmp_path)
    _, out = _run(tmp_path, "--snapshot", str(snap), base=base)
    code, out2 = _run(
        tmp_path, "--snapshot", str(snap), "--allow-new-classes", base=base, out=tmp_path / "out2"
    )

    assert merge(out)["skipped_new_classes"] == {"Lutjanus apodus": 1}
    assert not list(out.rglob("review_ev9.jpg"))
    assert code == 0 and (out2 / "Lutjanus_apodus/review_ev9.jpg").is_file()
    assert merge(out2)["skipped_new_classes"] == {}


def test_latest_json_is_followed(tmp_path):
    parent = tmp_path / "datasets"
    _snapshot(parent / STAMP)
    (parent / "latest.json").write_text(json.dumps({"snapshot": STAMP, "sha256": "abc"}))

    code, out = _run(tmp_path, "--snapshot", str(parent))

    assert code == 0 and merge(out)["snapshot"] == STAMP
    assert (out / "no_animal/review_ev6.jpg").is_file()


def test_the_tar_and_its_directory_build_the_same_folder(tmp_path):
    base = _base(tmp_path)
    tar_path = tmp_path / "fine_tune.tar"
    with tarfile.open(tar_path, "w") as tar:
        tar.add(base, arcname="fine_tune")

    _run(tmp_path, base=base, out=tmp_path / "from_dir")
    code, from_tar = _run(tmp_path, base=tar_path, out=tmp_path / "from_tar")

    assert code == 0 and tree(from_tar) == tree(tmp_path / "from_dir") != {}
    assert [r["label"] for r in manifest(from_tar)] == [
        r["label"] for r in manifest(tmp_path / "from_dir")
    ]


def test_a_non_empty_out_is_refused(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "keep.txt").write_text("mine")

    code, _ = _run(tmp_path, out=out)

    assert code == 2 and sorted(p.name for p in out.iterdir()) == ["keep.txt"]


def test_a_tar_member_outside_fine_tune_is_refused(tmp_path):
    tar_path = tmp_path / "evil.tar"
    with tarfile.open(tar_path, "w") as tar:
        for name in ("fine_tune/_dataset_manifest.json", "fine_tune/../evil.txt"):
            data = b"[]"
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))

    code, out = _run(tmp_path, base=tar_path)

    assert code == 2 and not out.exists() and not (tmp_path / "evil.txt").exists()


def test_two_folders_for_one_accepted_name_stop_the_build(tmp_path):
    folders = dict(BASE) | {"Brachygenys_chrysargyrea": ("d", "Brachygenys chrysargyrea")}

    code, out = _run(tmp_path, base=_base(tmp_path, folders))

    assert code == 2 and not out.exists()


def test_other_folders_in_the_tar_are_ignored(tmp_path):
    """The published tar also carries the run that trained dgx-2026-10-24 (baseline_runs/)."""
    base = _base(tmp_path)
    tar_path = tmp_path / "fine_tune.tar"
    with tarfile.open(tar_path, "w") as tar:
        tar.add(base, arcname="fine_tune")
        info = tarfile.TarInfo("baseline_runs/run/config.json")
        info.size = 2
        tar.addfile(info, io.BytesIO(b"{}"))

    code, out = _run(tmp_path, base=tar_path)

    assert code == 0 and not (out / "baseline_runs").exists() and not (out / "run").exists()

"""The training machine's run (scripts/train_job.py) and its torch-free helpers
(scripts/finetune_init.py); spec docs/superpowers/specs/2026-11-23-training-machine-design.md.
Assembling and training are stood in for: CI has no torch, and both have their own tests."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from finetune_init import head_rows, is_review_crop, read_sums, verify_sums  # noqa: E402
from train_job import main, parse, run  # noqa: E402

# ---------- finetune_init ----------


def test_head_rows_follow_the_class_names_not_their_positions():
    old = ["Abudefduf_saxatilis", "Brachygenys_chrysargyrea", "Kyphosus"]
    new = ["Abudefduf_saxatilis", "Kyphosus", "Brachygenys_chrysargyrea", "no_animal"]
    assert head_rows(new, old) == [0, 2, 1, None]


def test_head_rows_refuse_a_class_named_twice():
    with pytest.raises(ValueError):
        head_rows(["a"], ["a", "a"])
    with pytest.raises(ValueError):
        head_rows(["a", "a"], ["a"])


def test_review_crops_are_the_assemblers_review_files():
    assert is_review_crop("/app/data/fine_tune/Kyphosus/review_ev123.jpg")
    assert is_review_crop("review_add7.jpg")
    assert not is_review_crop("/app/data/fine_tune/Kyphosus/drive_abc_f12_d0.jpg")


def _sums(folder: Path, names: list[str]) -> None:
    lines = [f"{hashlib.sha256((folder / n).read_bytes()).hexdigest()}  {n}" for n in names]
    (folder / "SHA256SUMS").write_text("\n".join(lines) + "\n")


def test_verify_sums_accepts_a_published_bundle_and_names_a_changed_file(tmp_path):
    (tmp_path / "a.bin").write_bytes(b"a")
    (tmp_path / "b.json").write_text("[]")
    _sums(tmp_path, ["a.bin", "b.json"])
    assert set(verify_sums(tmp_path)) == {"a.bin", "b.json"}
    (tmp_path / "a.bin").write_bytes(b"changed")
    with pytest.raises(ValueError, match="a.bin"):
        verify_sums(tmp_path)


def test_read_sums_refuses_a_line_that_is_not_a_sum():
    with pytest.raises(ValueError):
        read_sums("not a sum line\n")


# ---------- train_job ----------


def _channels(root: Path) -> Path:
    channels = root / "channels"
    base = channels / "base"
    base.mkdir(parents=True)
    (base / "fine_tune.tar").write_bytes(b"tar bytes")
    _sums(base, ["fine_tune.tar"])
    snap = channels / "snapshot"
    snap.mkdir()
    (snap / "snapshot.json").write_text(
        json.dumps(
            {"snapshot": "2026-11-23T200000Z", "content_hash": "c" * 64, "git_sha": "7619c24"}
        )
    )
    init = channels / "init"
    init.mkdir()
    (init / "checkpoint_best.pt").write_bytes(b"weights")
    (init / "classes.json").write_text('["Kyphosus"]')
    _sums(init, ["checkpoint_best.pt", "classes.json"])
    return channels


def _args(root: Path, *extra: str):
    return parse(
        [
            "train",
            "--run-id",
            "ai-wahoobay-staging-clf-20261123T200000Z",
            "--channels",
            str(root / "channels"),
            "--model-dir",
            str(root / "model"),
            "--failure-file",
            str(root / "output" / "failure"),
            "--repo-root",
            str(root / "repo"),
            *extra,
        ]
    )


class Fakes:
    def __init__(self, root: Path, assemble_exit: int = 0):
        self.root = root
        self.assemble_exit = assemble_exit
        self.assembled: list[str] = []
        self.trained: list[str] = []

    def assemble(self, argv: list[str]) -> int:
        self.assembled = argv
        out = Path(argv[argv.index("--out") + 1])
        out.mkdir(parents=True)
        (out / "_review_merge.json").write_text('{"snapshot": "2026-11-23T200000Z"}')
        return self.assemble_exit

    def train(self, argv: list[str]) -> dict:
        self.trained = argv
        run_dir = self.root / "repo" / "data" / "baseline_runs" / argv[argv.index("--run-id") + 1]
        run_dir.mkdir(parents=True)
        for name in ("checkpoint_best.pt", "checkpoint_last.pt", "classes.json", "metrics.json"):
            (run_dir / name).write_text(name)
        (run_dir / "metrics_before.json").write_text("{}")
        return {"best_macro_f1": 0.5}


def test_a_run_leaves_a_checked_bundle_and_what_it_trained_on(tmp_path):
    _channels(tmp_path)
    fakes = Fakes(tmp_path)
    run(_args(tmp_path), assemble=fakes.assemble, train=fakes.train)

    channels = tmp_path / "channels"
    assert fakes.assembled == [
        "--base",
        str(channels / "base" / "fine_tune.tar"),
        "--snapshot",
        str(channels / "snapshot"),
        "--out",
        str(tmp_path / "repo" / "data" / "fine_tune"),
    ]
    t = fakes.trained
    assert t[t.index("--init-checkpoint") + 1] == str(channels / "init" / "checkpoint_best.pt")
    assert t[t.index("--init-classes") + 1] == str(channels / "init" / "classes.json")
    assert t[t.index("--split-mode") + 1] == "temporal_within_source"
    assert (t[t.index("--epochs") + 1], t[t.index("--lr") + 1]) == ("20", "0.0001")

    model = tmp_path / "model"
    files = sorted(p.name for p in model.iterdir())
    assert "checkpoint_last.pt" not in files
    assert {"checkpoint_best.pt", "classes.json", "metrics.json", "metrics_before.json"} <= set(
        files
    )
    assert {"_review_merge.json", "inputs.json", "SHA256SUMS"} <= set(files)
    assert set(verify_sums(model)) == set(files) - {"SHA256SUMS"}
    inputs = json.loads((model / "inputs.json").read_text())
    assert inputs["snapshot"] == {
        "snapshot": "2026-11-23T200000Z",
        "content_hash": "c" * 64,
        "git_sha": "7619c24",
    }
    assert inputs["base"]["sha256"] == hashlib.sha256(b"tar bytes").hexdigest()
    assert set(inputs["init"]) == {"checkpoint_best.pt", "classes.json"}


def test_changed_starting_weights_stop_the_run_before_anything_is_trained(tmp_path):
    """Checked before the assembler is even imported: main() runs for real here."""
    _channels(tmp_path)
    (tmp_path / "channels" / "init" / "checkpoint_best.pt").write_bytes(b"other weights")
    assert main(["train", *_raw(tmp_path)]) == 1
    reason = (tmp_path / "output" / "failure").read_text()
    assert "checkpoint_best.pt" in reason and len(reason) <= 1000
    assert not (tmp_path / "repo").exists() and not (tmp_path / "model").exists()


def test_a_base_channel_without_its_sums_stops_the_run(tmp_path):
    _channels(tmp_path)
    (tmp_path / "channels" / "base" / "SHA256SUMS").unlink()
    assert main(_raw(tmp_path)) == 1
    assert "SHA256SUMS" in (tmp_path / "output" / "failure").read_text()


def test_an_assembler_refusal_stops_the_run_before_training(tmp_path):
    from train_job import Refused

    _channels(tmp_path)
    fakes = Fakes(tmp_path, assemble_exit=2)
    with pytest.raises(Refused):
        run(_args(tmp_path), assemble=fakes.assemble, train=fakes.train)
    assert fakes.trained == []


def _raw(root: Path) -> list[str]:
    return [
        "--run-id",
        "r",
        "--channels",
        str(root / "channels"),
        "--model-dir",
        str(root / "model"),
        "--failure-file",
        str(root / "output" / "failure"),
        "--repo-root",
        str(root / "repo"),
    ]


def test_a_reviewers_camera_day_is_split_by_whole_frames():
    from finetune_init import split_review_source

    # 5 frames (seconds of day), several boxes each: the last frame (20%) goes to validation whole.
    items = [(100, 0), (100, 1), (200, 2), (300, 3), (400, 4), (500, 5), (500, 6), (500, 7)]
    train, val = split_review_source(items, 0.2)
    assert val == [5, 6, 7] and train == [0, 1, 2, 3, 4]


def test_a_reviewers_day_with_few_frames_stays_in_training():
    from finetune_init import split_review_source

    one_frame = [(100, i) for i in range(30)]  # a school of grunts in one frame
    assert split_review_source(one_frame, 0.2) == (list(range(30)), [])
    two = [(100, 0), (200, 1)]
    assert split_review_source(two, 0.2) == ([0, 1], [])
    assert split_review_source([(1, 0), (2, 1), (3, 2)], 0.2) == ([0, 1], [2])

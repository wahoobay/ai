#!/usr/bin/env python3
"""One fine-tuning run on a SageMaker training machine (the trainer image's entrypoint).

    python scripts/train_job.py [train] --run-id <job name> [--epochs 20 --lr 1e-4 --warmup-epochs 2]

Spec: docs/superpowers/specs/2026-11-23-training-machine-design.md §2. Before this starts,
SageMaker copies three input channels onto the machine (/opt/ml/input/data/<channel>/):

  base      the base crops: fine_tune.tar and its SHA256SUMS (datasets/fine_tune/<version>/)
  snapshot  one verified snapshot (datasets/verified/<env>/<stamp>/)
  init      the starting weights: checkpoint_best.pt, classes.json, SHA256SUMS (classifier_v2/<version>/)

The run checks both SHA256SUMS files, assembles data/fine_tune from the base tar and the snapshot
(scripts/assemble_training_set.py), fine-tunes from the init bundle (scripts/train_baseline.py),
and leaves the run's files in /opt/ml/model with inputs.json (what it trained on) and a new
SHA256SUMS; SageMaker copies that folder to S3. A failure's reason goes to /opt/ml/output/failure,
which SageMaker shows as the job's failure reason. SageMaker runs the image as `<entrypoint> train`;
the leading "train" is accepted and ignored.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import traceback
from collections.abc import Callable
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from finetune_init import SUMS, sums_text, verify_sums  # noqa: E402

SM = Path("/opt/ml")
LEFT_BEHIND = {
    "checkpoint_last.pt"
}  # resume weights: the size of the model, of no use after the run
FAILURE_LIMIT = 1000  # SageMaker keeps the first 1,024 characters of the failure reason


class Refused(Exception):
    """The inputs are not what the run expects; nothing was trained."""


def parse(argv: list[str]) -> argparse.Namespace:
    if argv[:1] == ["train"]:
        argv = argv[1:]
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--run-id", required=True)
    p.add_argument("--epochs", type=int, default=20)
    p.add_argument("--lr", type=float, default=1e-4)
    p.add_argument("--warmup-epochs", type=int, default=2)
    p.add_argument("--batch-size", type=int, default=32)
    p.add_argument("--num-workers", type=int, default=4)
    p.add_argument("--device", default="cuda:0")
    p.add_argument(
        "--split-mode",
        choices=("stratified", "by_source", "temporal_within_source"),
        default="temporal_within_source",  # the live model's split: see the spec §1
    )
    p.add_argument("--smoke", action="store_true", help="3 epochs, for a check of the machine")
    p.add_argument("--channels", type=Path, default=SM / "input" / "data")
    p.add_argument("--model-dir", type=Path, default=SM / "model")
    p.add_argument("--failure-file", type=Path, default=SM / "output" / "failure")
    p.add_argument("--repo-root", type=Path, default=REPO_ROOT, help=argparse.SUPPRESS)
    return p.parse_args(argv)


def _base_tar(base: Path) -> tuple[Path, str]:
    sums = verify_sums(base)
    tars = [n for n in sums if n.endswith(".tar")]
    if len(tars) != 1:
        raise Refused(f"the base channel should hold one checked .tar, found {tars}")
    return base / tars[0], sums[tars[0]]


def _snapshot(snapshot: Path) -> dict:
    try:
        snap = json.loads((snapshot / "snapshot.json").read_text())
    except FileNotFoundError as e:
        raise Refused(f"no snapshot.json in {snapshot}") from e
    return snap


def _init(init: Path) -> dict[str, str]:
    sums = verify_sums(init)
    for name in ("checkpoint_best.pt", "classes.json"):
        if name not in sums:
            raise Refused(f"the init channel's {SUMS} does not list {name}")
    return sums


def _assemble(argv: list[str]) -> int:
    from assemble_training_set import main

    return main(argv)


def _train(argv: list[str]) -> dict:
    from train_baseline import main

    return main(argv)


def run(
    args: argparse.Namespace,
    assemble: Callable[[list[str]], int] = _assemble,
    train: Callable[[list[str]], dict] = _train,
) -> dict:
    base_tar, base_sha = _base_tar(args.channels / "base")
    snapshot_dir = args.channels / "snapshot"
    snap = _snapshot(snapshot_dir)
    init_dir = args.channels / "init"
    init_sums = _init(init_dir)

    data = args.repo_root / "data" / "fine_tune"
    code = assemble(["--base", str(base_tar), "--snapshot", str(snapshot_dir), "--out", str(data)])
    if code != 0:
        raise Refused(f"assemble_training_set refused (exit {code}); see the log above")

    train_argv = [
        "--run-id",
        args.run_id,
        "--epochs",
        str(args.epochs),
        "--lr",
        str(args.lr),
        "--warmup-epochs",
        str(args.warmup_epochs),
        "--batch-size",
        str(args.batch_size),
        "--num-workers",
        str(args.num_workers),
        "--device",
        args.device,
        "--split-mode",
        args.split_mode,
        "--init-checkpoint",
        str(init_dir / "checkpoint_best.pt"),
        "--init-classes",
        str(init_dir / "classes.json"),
    ] + (["--smoke"] if args.smoke else [])
    metrics = train(train_argv)

    run_dir = args.repo_root / "data" / "baseline_runs" / args.run_id
    out = args.model_dir
    out.mkdir(parents=True, exist_ok=True)
    names: list[str] = []
    for f in sorted(run_dir.iterdir()):
        if f.is_file() and f.name not in LEFT_BEHIND:
            shutil.copy2(f, out / f.name)
            names.append(f.name)
    shutil.copy2(data / "_review_merge.json", out / "_review_merge.json")
    names.append("_review_merge.json")
    inputs = {
        "run_id": args.run_id,
        "git_sha": os.environ.get("GIT_SHA", "unknown"),
        "base": {"file": base_tar.name, "sha256": base_sha},
        "snapshot": {k: snap.get(k) for k in ("snapshot", "content_hash", "git_sha")},
        "init": init_sums,
        "train_args": train_argv,
    }
    (out / "inputs.json").write_text(json.dumps(inputs, indent=2) + "\n")
    names.append("inputs.json")
    (out / SUMS).write_text(sums_text(out, names))
    print(
        f"run {args.run_id}: {len(names)} files in {out}; best macro F1 {metrics.get('best_macro_f1')}"
    )
    return inputs


def main(argv: list[str] | None = None) -> int:
    args = parse(sys.argv[1:] if argv is None else argv)
    try:
        run(args)
    except Exception as e:
        traceback.print_exc()
        reason = f"{type(e).__name__}: {e}"[:FAILURE_LIMIT]
        try:
            args.failure_file.parent.mkdir(parents=True, exist_ok=True)
            args.failure_file.write_text(reason)
        except OSError:
            pass
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

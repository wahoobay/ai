"""Pieces of a training run that need no torch, so CI (which has none) tests them.

Used by scripts/train_baseline.py (fine-tuning from published weights, scoring the reviewers' crops
alone) and scripts/train_job.py (checking and writing SHA256SUMS). Spec:
docs/superpowers/specs/2026-11-23-training-machine-design.md.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from pathlib import Path

SUMS = "SHA256SUMS"
# scripts/assemble_training_set.py names the reviewers' crops review_<box_id>.jpg
REVIEW_PREFIX = "review_"


def head_rows(new_classes: Sequence[str], old_classes: Sequence[str]) -> list[int | None]:
    """For each class of the new run, the row of the starting model's head that holds it, or None for
    a class the starting model never had (it keeps the fresh head's initialisation)."""
    if len(set(old_classes)) != len(old_classes):
        raise ValueError("the starting model's class list names a class twice")
    if len(set(new_classes)) != len(new_classes):
        raise ValueError("the new class list names a class twice")
    row = {c: i for i, c in enumerate(old_classes)}
    return [row.get(c) for c in new_classes]


def is_review_crop(path: str | Path) -> bool:
    return Path(path).name.startswith(REVIEW_PREFIX)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def read_sums(text: str) -> dict[str, str]:
    """SHA256SUMS as infra's publish_models writes it: `<hex>  <relative path>` per line."""
    sums: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        digest, sep, name = line.partition("  ")
        if not sep or len(digest) != 64 or not name:
            raise ValueError(f"not a SHA256SUMS line: {line!r}")
        sums[name] = digest
    return sums


def verify_sums(directory: Path) -> dict[str, str]:
    """Check every file a directory's SHA256SUMS lists; the listed sums, or ValueError naming the
    first file that is missing or differs."""
    sums = read_sums((directory / SUMS).read_text())
    if not sums:
        raise ValueError(f"{directory / SUMS} lists no files")
    for name, digest in sums.items():
        path = directory / name
        if not path.is_file():
            raise ValueError(f"{path} is listed in {SUMS} but missing")
        if sha256_file(path) != digest:
            raise ValueError(f"{path} does not match its {SUMS} entry")
    return sums


def sums_text(directory: Path, names: Iterable[str]) -> str:
    return "".join(f"{sha256_file(directory / n)}  {n}\n" for n in sorted(names))

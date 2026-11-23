#!/usr/bin/env python3
"""Build the classifier's training folder from the base crops and a verified snapshot.

    python scripts/assemble_training_set.py --base fine_tune.tar \\
        --snapshot s3://platform-wahoobay-models/datasets/verified/staging/ --out data/fine_tune

Run where training runs (spec docs/superpowers/specs/2026-11-22-verified-dataset-training-design.md
§3). It writes a fresh folder in the layout scripts/train_baseline.py reads and never edits its
inputs: undoing a merge is building again without --snapshot.

* The base crops (the dgx-2026-10-24 tar, or a directory holding _dataset_manifest.json), with
  the folders and manifest labels of renamed classes moved to their new names
  (common/class_renames.py).
* Each verified crop of a species in the folder whose name has the same WoRMS accepted name in
  taxonomy.csv (the review catalogue's rule), as review_<box_id>.jpg; "no animal here" crops in
  no_animal/, a class of their own. A species with no folder is reported and skipped, unless
  --allow-new-classes gives it one. Each camera-day counts as one clip for the trainer's splits.

Exit 2: refused before anything was written (--out not empty, an unsafe tar member, two folders
for one accepted name, no snapshot at the location).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tarfile
from collections import Counter
from collections.abc import Callable
from datetime import datetime
from pathlib import Path, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "services") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "services"))

from common import taxonomy  # noqa: E402
from common.class_renames import class_names, folder  # noqa: E402
from common.storage import join, open_read  # noqa: E402

NO_ANIMAL = "no_animal"
MANIFEST = "_dataset_manifest.json"
TAXONOMY_JSON = "_taxonomy.json"


class Refused(Exception):
    """Nothing was written."""


def _tar_entries(tar: tarfile.TarFile) -> dict[str, Callable[[], bytes]]:
    """The files under fine_tune/, by path inside it. Other top-level folders (the published tar
    also carries baseline_runs/, the run that trained dgx-2026-10-24) are ignored; a member that
    climbs out (absolute, or with ..) or a link under fine_tune/ refuses the whole tar."""
    entries: dict[str, Callable[[], bytes]] = {}
    for member in tar.getmembers():
        path = PurePosixPath(member.name)
        if path.is_absolute() or ".." in path.parts:
            raise Refused(f"tar member {member.name!r} climbs out of the tar")
        if path.parts[:1] != ("fine_tune",) or member.isdir():
            continue
        if not member.isfile():
            raise Refused(f"tar member {member.name!r} is not a regular file")
        rel = PurePosixPath(*path.parts[1:]).as_posix()
        entries[rel] = lambda m=member: tar.extractfile(m).read()
    if MANIFEST not in entries:
        raise Refused(f"no fine_tune/{MANIFEST} in the tar")
    return entries


def _dir_entries(base: Path) -> dict[str, Callable[[], bytes]]:
    root = base if (base / MANIFEST).is_file() else base / "fine_tune"
    if not (root / MANIFEST).is_file():
        raise Refused(f"no {MANIFEST} in {base}")
    return {
        p.relative_to(root).as_posix(): p.read_bytes
        for p in sorted(root.rglob("*"))
        if p.is_file() and len(p.relative_to(root).parts) <= 2
    }


def _snapshot(location: str) -> tuple[str, dict, list[dict]]:
    url = location if "://" in location else "file://" + str(Path(location).resolve())
    try:
        snap = json.loads(open_read(join(url, "snapshot.json")))
    except FileNotFoundError:
        try:
            latest = json.loads(open_read(join(url, "latest.json")))
        except FileNotFoundError as e:
            raise Refused(f"no snapshot.json or latest.json at {location}") from e
        url = join(url, latest["snapshot"])
        snap = json.loads(open_read(join(url, "snapshot.json")))
    records = json.loads(open_read(join(url, "manifest.json")))["records"]
    return url, snap, records


def _seconds(local_ts: str) -> int:
    t = datetime.fromisoformat(local_ts)
    return t.hour * 3600 + t.minute * 60 + t.second


def _relpath(path: Path) -> str:
    """A crop's path as the trainer joins it to the repository root."""
    return Path(os.path.relpath(Path(path).resolve(), REPO_ROOT.resolve())).as_posix()


def build(args: argparse.Namespace, entries: dict[str, Callable[[], bytes]]) -> dict:
    out = Path(args.out)
    base_manifest: list[dict] = json.loads(entries[MANIFEST]())
    base_folders = sorted({rel.split("/")[0] for rel in entries if "/" in rel})
    new_label = {f: class_names([f])[0] for f in base_folders}
    new_folder = {f: folder(new_label[f]) for f in base_folders}
    clash = [n for n, k in Counter(new_folder.values()).items() if k > 1]
    if clash:
        raise Refused(f"two base folders become {clash[0]!r}")
    rows = taxonomy.read(Path(args.taxonomy))
    by_accepted: dict[str, str] = {}
    for f in sorted(new_folder.values()):
        name = taxonomy.accepted(rows, f)
        if name in by_accepted:
            raise Refused(f"{by_accepted[name]!r} and {f!r} are both {name!r}")
        by_accepted[name] = f

    snapshot = _snapshot(args.snapshot) if args.snapshot else None
    if out.exists() and any(out.iterdir()):
        raise Refused(f"{out} is not empty")

    out.mkdir(parents=True, exist_ok=True)
    for rel, load in entries.items():
        if "/" in rel:
            f, name = rel.split("/", 1)
            dst = out / new_folder[f] / name
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(load())

    records, dropped, label_map = [], 0, {}
    for r in base_manifest:
        parts = PurePosixPath(r["crop_path"]).parts
        f, name = parts[-2], parts[-1]
        if f"{f}/{name}" not in entries:
            dropped += 1
            continue
        renamed = new_folder[f] != f
        if renamed:
            label_map[r["label"]] = new_label[f]
        records.append(
            r
            | {
                "label": new_label[f] if renamed else r["label"],
                "crop_path": _relpath(out / new_folder[f] / name),
            }
        )
    tax_json = json.loads(entries[TAXONOMY_JSON]()) if TAXONOMY_JSON in entries else {}
    tax_json = {label_map.get(k, k): v for k, v in tax_json.items()}

    copied: Counter = Counter()
    skipped: Counter = Counter()
    merge = {"snapshot": None, "location": None, "content_hash": None}
    if snapshot:
        url, snap, snap_records = snapshot
        merge = {
            "snapshot": snap["snapshot"],
            "location": url,
            "content_hash": snap.get("content_hash"),
        }
        for r in snap_records:
            if r.get("held_back") or not r.get("crop"):
                continue
            if r["outcome"] == NO_ANIMAL:
                f = NO_ANIMAL
            else:
                f = by_accepted.get(taxonomy.accepted(rows, r["accepted_name"]))
                if f is None and args.allow_new_classes:
                    f = r["crop"].split("/")[1]  # the snapshot's folder for the name
                if f is None:
                    skipped[r["accepted_name"]] += 1
                    continue
            dst = out / f / f"review_{r['box_id']}.jpg"
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(open_read(join(url, r["crop"])))
            copied[f] += 1
            records.append(
                {
                    "crop_id": f"review_{r['box_id']}",
                    "label": f.replace("_", " ") if f != NO_ANIMAL else NO_ANIMAL,
                    "display_label": None,
                    "family": None,
                    "genus": None,
                    "species": None,
                    "drive_id": f"review:{r['camera']}:{r['local_date']}",
                    "source_path": f"{snap['snapshot']}/{r['crop']}",
                    "source_kind": "review:video",
                    "frame_index": _seconds(r["local_ts"]),
                    "bbox": r["bbox"],
                    "det_conf": None,
                    "crop_path": _relpath(dst),
                }
            )

    merge |= {
        "copied": dict(sorted(copied.items())),
        "skipped_new_classes": dict(sorted(skipped.items())),
        "base_records_dropped": dropped,
    }
    (out / MANIFEST).write_text(json.dumps(records, indent=1))
    (out / TAXONOMY_JSON).write_text(json.dumps(tax_json, indent=1, sort_keys=True))
    (out / "_review_merge.json").write_text(json.dumps(merge, indent=1))
    return merge


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--base", required=True, help="fine_tune.tar, or a directory of class folders")
    p.add_argument("--out", default=str(REPO_ROOT / "data" / "fine_tune"))
    p.add_argument("--snapshot", help="a snapshot, or the folder whose latest.json names one")
    p.add_argument("--taxonomy", default=str(REPO_ROOT / "assets" / "taxonomy.csv"))
    p.add_argument("--allow-new-classes", action="store_true")
    args = p.parse_args(argv)
    base = Path(args.base)
    try:
        if base.is_file():
            with tarfile.open(base) as tar:
                merge = build(args, _tar_entries(tar))
        else:
            merge = build(args, _dir_entries(base))
    except Refused as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    print(
        f"built {args.out}: {sum(merge['copied'].values())} review crops "
        f"({merge['copied']}); skipped new classes {merge['skipped_new_classes']}; "
        f"base records dropped {merge['base_records_dropped']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

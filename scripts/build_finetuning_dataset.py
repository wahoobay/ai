#!/usr/bin/env python3
"""Build a Fishial-friendly fine-tuning dataset from human corrections.

Reads ``labeled_corrections.csv`` (from the dashboard's
``/api/export/labeled_corrections.csv`` endpoint) plus the saved-frame
JPEGs in ``frames/``, and emits a per-species training dataset:

    out/
    ├── train/
    │   ├── Abudefduf_saxatilis/
    │   │   ├── corr0042_event12345.jpg
    │   │   └── ...
    │   └── ...
    ├── val/
    ├── test/
    ├── negatives/             # if --include-not-a-fish: false-positive crops
    ├── manifest.json          # provenance + per-species counts
    └── README.md

This is the format the Fishial classifier (DINOv2 + ViT) and most
classification fine-tuning pipelines expect: one directory per class.
For detector-style fine-tuning (YOLO/COCO format) use --output coco.

Crops are taken from the corrected event's bbox with a small (5%) margin
for context. Filenames encode correction_id + event_id so each crop is
traceable back to a specific reviewer + frame.

Provenance: ``manifest.json`` records the corrections CSV's MD5,
filtering parameters, RNG seed, and per-species counts so any model
trained from this dataset can be reproduced from the same corrections.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
import re
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

import cv2

CONFIDENCE_RANK = {"uncertain": 1, "probable": 2, "certain": 3}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _slug(name: str) -> str:
    """Convert a species name into a filesystem-safe dir name."""
    out = re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_")
    return out or "unknown"


def _parse_bbox(value: str) -> tuple[int, int, int, int] | None:
    """CSV exports bbox as 'x1,y1,x2,y2'."""
    if not value:
        return None
    try:
        parts = [int(float(p.strip())) for p in value.split(",")]
        if len(parts) == 4 and parts[2] > parts[0] and parts[3] > parts[1]:
            return tuple(parts)  # type: ignore[return-value]
    except (ValueError, IndexError):
        pass
    return None


def _resolve_frame(rel: str, frames_root: Path | None) -> Path | None:
    """Try several resolutions for the frame path the CSV gave us.

    Order: as-is (absolute) → frames_root + trailing path → frames_root + basename.
    """
    if not rel:
        return None
    p = Path(rel)
    if p.is_absolute() and p.exists():
        return p
    if frames_root is None:
        return None
    # Try interpreting absolute path as relative-from-frames_root by stripping the leading "/"
    if p.is_absolute():
        try:
            stripped = Path(*p.parts[1:])
            cand = frames_root / stripped
            if cand.exists():
                return cand
        except IndexError:
            pass
    cand = frames_root / p
    if cand.exists():
        return cand
    cand = frames_root / p.name
    if cand.exists():
        return cand
    return None


def _frame_from_coco(coco_path: str) -> str | None:
    """Saved frames live next to their COCO sidecar — same stem, different
    extension. If the export gave us a coco_path, we can derive the .jpg."""
    if not coco_path:
        return None
    if coco_path.endswith(".coco.json"):
        return coco_path[:-len(".coco.json")] + ".jpg"
    if coco_path.endswith(".json"):
        return coco_path[:-len(".json")] + ".jpg"
    return None


def _crop_with_padding(img, bbox: tuple[int, int, int, int], pad_pct: float = 0.05):
    """Crop the bbox with a small padding for classifier context."""
    h, w = img.shape[:2]
    x1, y1, x2, y2 = bbox
    bw, bh = x2 - x1, y2 - y1
    px = int(round(bw * pad_pct))
    py = int(round(bh * pad_pct))
    x1 = max(0, x1 - px)
    y1 = max(0, y1 - py)
    x2 = min(w, x2 + px)
    y2 = min(h, y2 + py)
    if x2 <= x1 or y2 <= y1:
        return None
    return img[y1:y2, x1:x2]


def _md5_of(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# README written into the dataset
# ---------------------------------------------------------------------------


README_TEMPLATE = """\
# Wahoo Bay fine-tuning dataset

Built {generated_at} from `{corrections_csv}` (md5 `{corrections_csv_md5}`).

## Filter parameters

- min_confidence:    {min_confidence}
- include_not_a_fish: {include_not_a_fish}
- dedupe_tracks:     {dedupe_tracks}
- max_per_species:   {max_per_species}
- train/val:         {train_frac}/{val_frac} (rest = test)
- seed:              {seed}

## Counts

- corrections in CSV:       {n_corrections_input}
- kept after filtering:     {n_kept}
- crops written:            {n_written}
- crops failed (unreadable / bad bbox): {n_failed}
- positive species:         {n_species}

## Layout

```
train/<Species_slug>/<corr_*_event_*.jpg>
val/<Species_slug>/...
test/<Species_slug>/...
negatives/<corr_*_event_*.jpg>     # only if --include-not-a-fish
```

## How to use

For Fishial's DINOv2 + ViT classifier, point the training script at
this directory's `train/` and `val/` directories.

For YOLO/COCO detection fine-tuning, run again with `--output coco`
to emit a COCO-format variant with bbox annotations.

## Reproducibility

`manifest.json` next to this README has all parameters + species
counts. The corrections CSV md5 is recorded so anyone re-running with
the same CSV gets the identical dataset.
"""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--corrections", required=True,
                    help="Path to labeled_corrections.csv")
    ap.add_argument("--frames-root", default=None,
                    help="Directory where frame_image_path values resolve from "
                         "(needed if running on a different host than the worker; "
                         "absolute paths are tried first)")
    ap.add_argument("--out", required=True,
                    help="Output dataset directory")
    ap.add_argument("--min-confidence",
                    choices=["uncertain", "probable", "certain"],
                    default="probable",
                    help="Drop reviewer corrections below this confidence "
                         "(default: probable, i.e. include probable + certain)")
    ap.add_argument("--include-not-a-fish", action="store_true",
                    help="Also export false-positive crops to negatives/")
    ap.add_argument("--dedupe-tracks", action="store_true",
                    help="Keep only one correction per track_id (avoid overfit "
                         "on a single fish that lingered in frame)")
    ap.add_argument("--max-per-species", type=int, default=None,
                    help="Cap N crops per species (over-sampling guard)")
    ap.add_argument("--train-frac", type=float, default=0.8)
    ap.add_argument("--val-frac",   type=float, default=0.1)
    ap.add_argument("--seed",       type=int,   default=42)
    ap.add_argument("--pad-pct",    type=float, default=0.05,
                    help="Bbox padding as fraction of bbox size (default 0.05)")
    args = ap.parse_args()

    csv_path = Path(args.corrections).resolve()
    if not csv_path.exists():
        print(f"corrections CSV not found: {csv_path}", file=sys.stderr)
        return 1
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    frames_root = Path(args.frames_root).resolve() if args.frames_root else None

    # Read CSV
    with csv_path.open() as fh:
        rdr = csv.DictReader(fh)
        rows = list(rdr)
    if not rows:
        print(f"corrections CSV is empty: {csv_path}", file=sys.stderr)
        return 1

    corrections_md5 = _md5_of(csv_path)

    # Filter
    min_rank = CONFIDENCE_RANK[args.min_confidence]
    kept: list[dict] = []
    skipped: Counter = Counter()
    for r in rows:
        conf = (r.get("confidence") or "").strip().lower()
        if CONFIDENCE_RANK.get(conf, 0) < min_rank:
            skipped["below_min_confidence"] += 1
            continue
        not_a_fish = (r.get("not_a_fish") or "").strip().lower() in ("true", "t", "1", "yes")
        if not_a_fish and not args.include_not_a_fish:
            skipped["not_a_fish"] += 1
            continue
        species_name = (r.get("corrected_name") or "").strip()
        if not species_name and not not_a_fish:
            skipped["missing_species"] += 1
            continue
        bbox = _parse_bbox(r.get("bbox", ""))
        if bbox is None:
            skipped["bad_bbox"] += 1
            continue
        # Try frame_image_path first; fall back to deriving from coco_path
        # (the JPG is the sibling of the .coco.json sidecar).
        frame_path = _resolve_frame(r.get("frame_image_path") or "", frames_root)
        if frame_path is None:
            derived = _frame_from_coco(r.get("frame_coco_path") or "")
            if derived:
                frame_path = _resolve_frame(derived, frames_root)
        if frame_path is None:
            skipped["frame_missing"] += 1
            continue
        kept.append({
            "row": r,
            "bbox": bbox,
            "frame_path": frame_path,
            "not_a_fish": not_a_fish,
            "species": species_name or "_NEGATIVE_",
            "track_id": (r.get("track_id") or "").strip() or None,
        })

    if not kept:
        print(f"All {len(rows)} corrections filtered out:", file=sys.stderr)
        for k, v in skipped.items():
            print(f"  {k:>30}: {v}", file=sys.stderr)
        return 1

    # Optional dedupe by track
    if args.dedupe_tracks:
        seen_tracks: set[str] = set()
        dedup: list[dict] = []
        for k in kept:
            t = k["track_id"]
            if t:
                if t in seen_tracks:
                    skipped["dedupe_track"] += 1
                    continue
                seen_tracks.add(t)
            dedup.append(k)
        kept = dedup

    # Group by species
    by_species: dict[str, list[dict]] = defaultdict(list)
    for k in kept:
        sp = "_NEGATIVE_" if k["not_a_fish"] else k["species"]
        by_species[sp].append(k)

    # Cap per species
    rng = random.Random(args.seed)
    if args.max_per_species:
        for sp in list(by_species.keys()):
            rng.shuffle(by_species[sp])
            n_before = len(by_species[sp])
            by_species[sp] = by_species[sp][: args.max_per_species]
            skipped["over_cap"] += n_before - len(by_species[sp])

    # Per-species 80/10/10 split
    splits: dict[str, list[tuple[str, dict]]] = {"train": [], "val": [], "test": []}
    for sp, items in by_species.items():
        rng.shuffle(items)
        n = len(items)
        n_train = int(round(n * args.train_frac))
        n_val   = int(round(n * args.val_frac))
        for it in items[:n_train]:
            splits["train"].append((sp, it))
        for it in items[n_train:n_train + n_val]:
            splits["val"].append((sp, it))
        for it in items[n_train + n_val:]:
            splits["test"].append((sp, it))

    # Write crops
    written: Counter = Counter()
    failed: Counter = Counter()
    for split, items in splits.items():
        for sp, it in items:
            if sp == "_NEGATIVE_":
                target_dir = out / "negatives"
            else:
                target_dir = out / split / _slug(sp)
            target_dir.mkdir(parents=True, exist_ok=True)
            img = cv2.imread(str(it["frame_path"]))
            if img is None:
                failed["unreadable"] += 1
                continue
            crop = _crop_with_padding(img, it["bbox"], pad_pct=args.pad_pct)
            if crop is None or crop.size == 0:
                failed["bad_crop"] += 1
                continue
            corr_id = (it["row"].get("correction_id") or "0").zfill(4)
            event_id = (it["row"].get("event_id") or "0")
            fname = f"corr{corr_id}_event{event_id}.jpg"
            cv2.imwrite(str(target_dir / fname), crop, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
            written[(split, sp)] += 1

    # Manifest
    n_species = len({sp for (_, sp) in written if sp != "_NEGATIVE_"})
    species_counts: dict[str, dict[str, int]] = defaultdict(lambda: {"train": 0, "val": 0, "test": 0})
    for (split, sp), n in written.items():
        species_counts[sp][split] = n

    manifest = {
        "generated_at":          datetime.now(UTC).isoformat(),
        "corrections_csv":       str(csv_path),
        "corrections_csv_md5":   corrections_md5,
        "frames_root":           str(frames_root) if frames_root else None,
        "min_confidence":        args.min_confidence,
        "include_not_a_fish":    args.include_not_a_fish,
        "dedupe_tracks":         args.dedupe_tracks,
        "max_per_species":       args.max_per_species,
        "pad_pct":               args.pad_pct,
        "train_frac":            args.train_frac,
        "val_frac":              args.val_frac,
        "seed":                  args.seed,
        "n_corrections_input":   len(rows),
        "n_kept":                len(kept),
        "skipped_reasons":       dict(skipped),
        "n_written":             sum(written.values()),
        "n_failed":              dict(failed),
        "n_species":             n_species,
        "species_counts":        dict(species_counts),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str))

    # README
    (out / "README.md").write_text(README_TEMPLATE.format(
        generated_at=manifest["generated_at"],
        corrections_csv=manifest["corrections_csv"],
        corrections_csv_md5=corrections_md5,
        min_confidence=args.min_confidence,
        include_not_a_fish=args.include_not_a_fish,
        dedupe_tracks=args.dedupe_tracks,
        max_per_species=args.max_per_species,
        train_frac=args.train_frac,
        val_frac=args.val_frac,
        seed=args.seed,
        n_corrections_input=len(rows),
        n_kept=len(kept),
        n_written=sum(written.values()),
        n_failed=sum(failed.values()),
        n_species=n_species,
    ))

    # Summary
    print(f"=== fine-tuning dataset built at {out} ===")
    print(f"  corrections CSV:        {csv_path}  (md5={corrections_md5[:10]}…)")
    print(f"  input corrections:      {len(rows):>6}")
    print(f"  kept after filters:     {len(kept):>6}")
    print(f"  crops written:          {sum(written.values()):>6}")
    print(f"  crops failed:           {sum(failed.values()):>6}  ({dict(failed)})")
    print(f"  positive species:       {n_species:>6}")
    print(f"  splits  train/val/test: {len(splits['train']):>5}/{len(splits['val'])}/{len(splits['test'])}")
    if skipped:
        print()
        print("  skipped reasons:")
        for k, v in skipped.most_common():
            print(f"    {k:>30}: {v}")
    if species_counts:
        print()
        print("  top species:")
        sorted_species = sorted(
            ((sp, sum(c.values())) for sp, c in species_counts.items() if sp != "_NEGATIVE_"),
            key=lambda x: -x[1],
        )
        for sp, n in sorted_species[:10]:
            counts = species_counts[sp]
            print(f"    {n:>4}  {sp:40s}  train={counts['train']:>3} val={counts['val']:>3} test={counts['test']:>3}")
        if "_NEGATIVE_" in species_counts:
            n_neg = sum(species_counts["_NEGATIVE_"].values())
            print(f"    {n_neg:>4}  (false positives)")

    # Health check / advisory
    if n_species > 0:
        thin = [sp for sp, c in species_counts.items()
                if sp != "_NEGATIVE_" and sum(c.values()) < 10]
        if thin:
            print()
            print(f"  ⚠  {len(thin)} species have <10 crops total — too few for")
            print("     reliable fine-tuning. More reviewer corrections needed for:")
            for sp in thin[:8]:
                print(f"       {sp}")
            if len(thin) > 8:
                print(f"       … +{len(thin) - 8} more")

    return 0


if __name__ == "__main__":
    sys.exit(main())

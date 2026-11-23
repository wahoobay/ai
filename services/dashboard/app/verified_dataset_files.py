"""The verified dataset's files, built from a selection without touching the database (spec
docs/superpowers/specs/2026-11-22-verified-dataset-training-design.md §2).

Classifier crops are the box plus 5% padding, clamped to the frame, not resized, in a folder per
accepted name (`no_animal` for "no animal here"); detector frames are copied byte for byte with
their COCO boxes. Every frame is read and decoded once for all its boxes. The output is
deterministic (fixed order, sorted JSON keys), so an unchanged database hashes the same.
"""

from __future__ import annotations

import io
import json
from collections.abc import Callable, Mapping
from datetime import UTC
from typing import Any
from zoneinfo import ZoneInfo

from PIL import Image, UnidentifiedImageError

from .review_catalogue import slug
from .verified_dataset import NO_ANIMAL, Box, DetectorPlan, Selection

PAD = 0.05
JPEG_QUALITY = 95
UNIDENTIFIED = "unidentified animal"


def crop_box(bbox, width: int, height: int, pad: float = PAD) -> tuple[int, int, int, int] | None:
    """`bbox` (x1, y1, x2, y2) grown by `pad` of its width and height on each side and clamped to
    the frame; None when the box itself has no area inside the frame (the padding alone would
    make a crop with none of the animal)."""
    x1, y1, x2, y2 = (int(v) for v in bbox)
    if min(width, x2) <= max(0, x1) or min(height, y2) <= max(0, y1):
        return None
    dx, dy = round(pad * (x2 - x1)), round(pad * (y2 - y1))
    x1, y1 = max(0, x1 - dx), max(0, y1 - dy)
    x2, y2 = min(width, x2 + dx), min(height, y2 + dy)
    if x2 <= x1 or y2 <= y1:
        return None
    return (x1, y1, x2, y2)


def class_folder(box: Box) -> str | None:
    """The classifier folder a box is cropped into: `no_animal`, the accepted name as one flat
    folder name, or None ("can't identify" names no class)."""
    if box.outcome == NO_ANIMAL:
        return NO_ANIMAL
    if box.outcome == "species" and box.accepted_name:
        return slug(box.accepted_name)
    return None


def _utc(ts) -> str:
    return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _json(data: Any) -> bytes:
    return json.dumps(data, sort_keys=True, indent=1).encode()


def _jpeg(image: Image.Image) -> bytes:
    buf = io.BytesIO()
    image.save(buf, "JPEG", quality=JPEG_QUALITY)
    return buf.getvalue()


def build_files(
    sel: Selection,
    plan: DetectorPlan,
    read_frame: Callable[[str], bytes],
    aphia: Mapping[str, int],
    zone: ZoneInfo,
) -> tuple[dict[str, bytes], dict[str, Any]]:
    """Every file of a snapshot except snapshot.json, and the counts."""
    included = set(plan.frames)
    frame_held = dict(plan.held_back)
    held: dict[str, str] = {}  # box_id -> reason found here
    crop_paths: dict[str, str] = {}
    files: dict[str, bytes] = {}
    sizes: dict[int, tuple[int, int]] = {}
    by_frame: dict[int, list[Box]] = {}
    for b in sel.boxes:
        if b.saved_frame_id is not None:
            by_frame.setdefault(b.saved_frame_id, []).append(b)

    for frame_id, frame in sorted(sel.frames.items()):
        boxes = by_frame.get(frame_id, [])
        wanted = [b for b in boxes if not b.held_back and class_folder(b)]
        if not wanted and frame_id not in included:
            continue
        try:
            data = read_frame(frame.image_path)
        except FileNotFoundError:
            for b in boxes:
                if not b.held_back:
                    held[b.box_id] = "frame image missing"
            if frame_id in included:
                included.discard(frame_id)
                frame_held[frame_id] = "frame image missing"
            continue
        try:
            with Image.open(io.BytesIO(data)) as raw:
                image = raw.convert("RGB")
        except (UnidentifiedImageError, OSError):  # not an image, or truncated
            for b in boxes:
                if not b.held_back:
                    held[b.box_id] = "frame image unreadable"
            if frame_id in included:
                included.discard(frame_id)
                frame_held[frame_id] = "frame image unreadable"
            continue
        width, height = image.size
        for b in boxes:
            if b.held_back:
                continue
            area = crop_box(b.bbox, width, height)
            if area is None:
                held[b.box_id] = "box outside the frame"
                continue
            folder = class_folder(b)
            if folder:
                path = f"classifier/{folder}/{b.box_id}.jpg"
                files[path] = _jpeg(image.crop(area))
                crop_paths[b.box_id] = path
        if frame_id in included:
            late = next((b for b in boxes if b.box_id in held), None)
            if late:
                included.discard(frame_id)
                frame_held[frame_id] = f"box {late.box_id} held back: {held[late.box_id]}"
            else:
                files[f"detector/images/{frame_id}.jpg"] = data
                sizes[frame_id] = (width, height)

    records = []
    for b in sel.boxes:
        reason = b.held_back or held.get(b.box_id)
        local = b.ts.astimezone(zone)
        records.append(
            {
                "box_id": b.box_id,
                "saved_frame_id": b.saved_frame_id,
                "camera": b.camera,
                "ts": _utc(b.ts),
                "local_date": local.date().isoformat(),
                "local_ts": local.isoformat(timespec="seconds"),
                "bbox": list(b.bbox),
                "bbox_source": b.bbox_source,
                "outcome": b.outcome,
                "accepted_name": b.accepted_name,
                "aphia_id": aphia.get(b.accepted_name) if b.accepted_name else None,
                "life_stage": b.life_stage,
                "confidence": b.confidence,
                "status": b.status,
                "label_source": b.label_source,
                "user_ids": list(b.user_ids),
                "model_name": b.model_name,
                "model_score": b.model_score,
                "crop": None if reason else crop_paths.get(b.box_id),
                "frame": (
                    f"detector/images/{b.saved_frame_id}.jpg" if b.saved_frame_id in sizes else None
                ),
                "held_back": reason,
            }
        )

    coco = _coco(sel, plan, sizes, aphia)
    files["detector/annotations.coco.json"] = _json(coco)
    crops: dict[str, int] = {}
    for path in crop_paths.values():
        folder = path.split("/")[1]
        crops[folder] = crops.get(folder, 0) + 1
    counts = {
        "boxes": len(records),
        "crops": dict(sorted(crops.items())),
        "detector_frames": len(coco["images"]),
        "detector_annotations": len(coco["annotations"]),
        "boxes_held_back": sum(1 for r in records if r["held_back"]),
        "frames_held_back": len(frame_held),
        "sample_answers": sel.sample_answers,
    }
    files["manifest.json"] = _json({"records": records, "counts": counts})
    files["report.txt"] = _report(records, frame_held, counts).encode()
    return files, counts


def _coco(
    sel: Selection,
    plan: DetectorPlan,
    sizes: dict[int, tuple[int, int]],
    aphia: Mapping[str, int],
) -> dict[str, Any]:
    names = sorted(
        {
            b.accepted_name
            for frame_id in sizes
            for b in plan.annotations[frame_id]
            if b.outcome == "species"
        }
    )
    category = {name: i for i, name in enumerate(names, 2)}
    images, annotations = [], []
    for frame_id in sorted(sizes):
        width, height = sizes[frame_id]
        frame = sel.frames[frame_id]
        images.append(
            {
                "id": frame_id,
                "file_name": f"images/{frame_id}.jpg",
                "width": width,
                "height": height,
                "date_captured": _utc(frame.ts),
                "camera": frame.camera,
            }
        )
        for b in plan.annotations[frame_id]:
            x1, y1, x2, y2 = b.bbox
            x1, y1, x2, y2 = max(0, x1), max(0, y1), min(width, x2), min(height, y2)
            w, h = x2 - x1, y2 - y1
            annotations.append(
                {
                    "id": len(annotations) + 1,
                    "image_id": frame_id,
                    "bbox": [x1, y1, w, h],
                    "area": w * h,
                    "iscrowd": 0,
                    "category_id": category[b.accepted_name] if b.outcome == "species" else 1,
                    "box_id": b.box_id,
                    "bbox_source": b.bbox_source,
                    "label_source": b.label_source,
                }
            )
    categories = [{"id": 1, "name": UNIDENTIFIED, "aphia_id": None}] + [
        {"id": i, "name": name, "aphia_id": aphia.get(name)} for name, i in category.items()
    ]
    return {"images": images, "annotations": annotations, "categories": categories}


def _report(records: list[dict], frame_held: dict[int, str], counts: dict[str, Any]) -> str:
    crops = counts["crops"]
    lines = [
        "Verified dataset",
        f"boxes: {counts['boxes']} (held back: {counts['boxes_held_back']})",
        f"classifier crops: {sum(crops.values())} (no animal: {crops.get(NO_ANIMAL, 0)})",
        f"detector frames: {counts['detector_frames']} "
        f"(held back: {counts['frames_held_back']}), annotations: {counts['detector_annotations']}",
        f"answers on sample events left out: {counts['sample_answers']}",
        "",
        "Boxes held back:",
    ]
    for r in records:
        if r["held_back"]:
            where = f"frame {r['saved_frame_id']}" if r["saved_frame_id"] else "no frame"
            lines.append(f"{r['box_id']} ({where}): {r['held_back']}")
    lines += ["", "Frames held back from the detector:"]
    lines += [f"frame {frame_id}: {reason}" for frame_id, reason in sorted(frame_held.items())]
    return "\n".join(lines) + "\n"

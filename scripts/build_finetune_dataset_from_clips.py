"""Run the YOLO fish detector across the downloaded community clips and
emit per-species bbox crops ready for fine-tuning.

Reads:
  * ``assets/species_manifest.json`` (built by build_species_manifest.py)
  * ``assets/community_clips/_download_report.json`` (built by
    download_species_clips.py)

Writes:
  * ``data/fine_tune/<binomial-slug>/<crop_id>.jpg`` — one cropped fish
    per detection, organised by class. Folder-per-class layout works
    out of the box with ``torchvision.datasets.ImageFolder`` and most
    open-source training recipes.
  * ``data/fine_tune/_dataset_manifest.json`` — one record per crop:
    ``crop_id``, ``label`` (binomial), ``display_label`` (the
    "Common name (G. species)" format), source provenance (drive_id,
    source_path, frame index for videos, original bbox), and detector
    confidence.
  * ``data/fine_tune/_taxonomy.json`` — species → {family, genus,
    species, common_name} lookup so the taxonomically-aware loss
    function has one place to read tree structure from.

For images, the detector runs once and every detected fish is cropped.
For videos, frames are sampled at SAMPLE_HZ Hz (default 2 Hz) and the
detector runs on each sampled frame; we cap at MAX_VIDEO_CROPS per
clip so a long video can't dominate the dataset. All detections in a
clip get the volunteer's labelled binomial — that's noisier than
single-fish-per-clip but more useful for class balance.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

try:
    import cv2
    import numpy as np
    from ultralytics import YOLO
except ImportError as e:
    sys.exit(f"missing dependency: {e}")


REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = REPO_ROOT / "assets" / "species_manifest.json"
DOWNLOADS_DIR = REPO_ROOT / "assets" / "community_clips"
DOWNLOAD_REPORT = DOWNLOADS_DIR / "_download_report.json"
OUT_DIR = REPO_ROOT / "data" / "fine_tune"
DETECTOR_DEFAULT = REPO_ROOT / "data" / "models" / "detector_v26" / "model.pt"

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi"}


@dataclass
class CropRecord:
    crop_id: str
    label: str                  # binomial — canonical class key
    display_label: str          # "Common name (G. species)" — review-friendly
    family: str | None
    genus: str | None
    species: str | None
    drive_id: str
    source_path: str            # relative to repo root
    source_kind: str            # "image" | "video"
    frame_index: int            # 0 for images; non-zero for videos
    bbox: list[int]             # [x1, y1, x2, y2] in source pixel coords
    det_conf: float
    crop_path: str              # relative to repo root


def slugify(s: str) -> str:
    """Filesystem-safe folder name from a binomial. ``Pomacanthus paru``
    becomes ``Pomacanthus_paru``; punctuation is stripped, runs of
    non-word characters collapse to single underscores."""
    s = re.sub(r"[^\w\s-]", "", s).strip()
    s = re.sub(r"[\s-]+", "_", s)
    return s or "unknown"


def crop_with_padding(
    img: np.ndarray, bbox: tuple[int, int, int, int], pad_frac: float = 0.10,
) -> np.ndarray:
    """Crop with a fractional margin around the bbox, clamped to image bounds."""
    H, W = img.shape[:2]
    x1, y1, x2, y2 = bbox
    w, h = max(1, x2 - x1), max(1, y2 - y1)
    pad = int(round(max(w, h) * pad_frac))
    cx1 = max(0, x1 - pad)
    cy1 = max(0, y1 - pad)
    cx2 = min(W, x2 + pad)
    cy2 = min(H, y2 + pad)
    return img[cy1:cy2, cx1:cx2].copy()


def detect_in_frame(
    detector: YOLO,
    frame_bgr: np.ndarray,
    imgsz: int,
    conf: float,
    device: str,
) -> list[tuple[tuple[int, int, int, int], float]]:
    r = detector.predict(
        source=frame_bgr, imgsz=imgsz, conf=conf, iou=0.5,
        device=device, verbose=False,
    )[0]
    if r.boxes is None or len(r.boxes) == 0:
        return []
    arr = r.boxes.data.cpu().numpy()
    out = []
    for row in arr:
        x1, y1, x2, y2, c, _cls = row[:6]
        out.append(((int(x1), int(y1), int(x2), int(y2)), float(c)))
    return out


def process_image(
    detector: YOLO,
    path: Path,
    rec: dict,
    out_dir: Path,
    imgsz: int,
    conf: float,
    device: str,
) -> list[CropRecord]:
    img = cv2.imread(str(path))
    if img is None:
        return []
    crops: list[CropRecord] = []
    detections = detect_in_frame(detector, img, imgsz, conf, device)
    if not detections:
        # No fish detected — fall back to the full image so the volunteer's
        # label still contributes a positive sample. Note bbox=full frame
        # so the manifest reflects "no bbox cropping happened here."
        H, W = img.shape[:2]
        detections = [((0, 0, W, H), 0.0)]
    for det_idx, ((x1, y1, x2, y2), c) in enumerate(detections):
        crop = crop_with_padding(img, (x1, y1, x2, y2))
        if crop.size == 0:
            continue
        crop_id = f"{rec['drive_id']}_d{det_idx:02d}"
        crop_path = out_dir / f"{crop_id}.jpg"
        cv2.imwrite(str(crop_path), crop, [cv2.IMWRITE_JPEG_QUALITY, 90])
        crops.append(CropRecord(
            crop_id=crop_id,
            label=rec["binomial"],
            display_label=rec["display_label"],
            family=rec.get("family"),
            genus=rec.get("genus"),
            species=rec.get("species"),
            drive_id=rec["drive_id"],
            source_path=str(path.relative_to(REPO_ROOT)),
            source_kind="image",
            frame_index=0,
            bbox=[x1, y1, x2, y2],
            det_conf=round(c, 4),
            crop_path=str(crop_path.relative_to(REPO_ROOT)),
        ))
    return crops


def process_video(
    detector: YOLO,
    path: Path,
    rec: dict,
    out_dir: Path,
    sample_hz: float,
    max_crops: int,
    imgsz: int,
    conf: float,
    device: str,
) -> list[CropRecord]:
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        return []
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    stride = max(1, int(round(fps / max(0.1, sample_hz))))
    n_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    crops: list[CropRecord] = []
    frame_idx = 0
    while True:
        ok, frame_bgr = cap.read()
        if not ok:
            break
        if frame_idx % stride == 0:
            detections = detect_in_frame(detector, frame_bgr, imgsz, conf, device)
            for det_idx, ((x1, y1, x2, y2), c) in enumerate(detections):
                crop = crop_with_padding(frame_bgr, (x1, y1, x2, y2))
                if crop.size == 0:
                    continue
                crop_id = f"{rec['drive_id']}_f{frame_idx:05d}_d{det_idx:02d}"
                crop_path = out_dir / f"{crop_id}.jpg"
                cv2.imwrite(str(crop_path), crop, [cv2.IMWRITE_JPEG_QUALITY, 88])
                crops.append(CropRecord(
                    crop_id=crop_id,
                    label=rec["binomial"],
                    display_label=rec["display_label"],
                    family=rec.get("family"),
                    genus=rec.get("genus"),
                    species=rec.get("species"),
                    drive_id=rec["drive_id"],
                    source_path=str(path.relative_to(REPO_ROOT)),
                    source_kind="video",
                    frame_index=frame_idx,
                    bbox=[x1, y1, x2, y2],
                    det_conf=round(c, 4),
                    crop_path=str(crop_path.relative_to(REPO_ROOT)),
                ))
                if len(crops) >= max_crops:
                    break
        frame_idx += 1
        if len(crops) >= max_crops or (n_frames > 0 and frame_idx >= n_frames):
            break
    cap.release()
    return crops


def build_dataset(
    detector_path: Path,
    sample_hz: float,
    max_crops: int,
    imgsz: int,
    conf: float,
    device: str,
    limit: int | None,
) -> None:
    manifest_records = json.loads(MANIFEST_PATH.read_text())
    # Map drive_id → manifest record for fast lookup. If multiple manifest
    # rows share a drive_id (rare — usually adult/juvi pairs would have
    # separate links), pick the one with the most-specific common name
    # (longest = juvenile/adult prefix usually).
    by_drive: dict[str, dict] = {}
    for r in manifest_records:
        d = r.get("drive_id")
        if not d:
            continue
        prev = by_drive.get(d)
        if prev is None or len(r.get("common_name", "")) > len(prev.get("common_name", "")):
            by_drive[d] = r

    report = json.loads(DOWNLOAD_REPORT.read_text())
    downloads = [x for x in report["results"] if x.get("status") in ("ok", "skipped")]
    if limit:
        downloads = downloads[:limit]

    print(f"loading detector: {detector_path}")
    detector = YOLO(str(detector_path))
    detector.to(device)
    # warm-up to take JIT/CUDA init off the per-file timing
    dummy = np.zeros((imgsz, imgsz, 3), dtype=np.uint8)
    detector.predict(source=dummy, imgsz=imgsz, conf=conf, device=device, verbose=False)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    all_crops: list[CropRecord] = []
    taxonomy: dict[str, dict] = {}
    by_label_count: dict[str, int] = {}
    skipped_unsupported = 0
    skipped_no_label = 0
    failed = []

    t0 = time.monotonic()
    for i, dl in enumerate(downloads):
        local = dl.get("local_path")
        drive_id = dl.get("drive_id")
        if not local or not drive_id:
            continue
        rec = by_drive.get(drive_id)
        if not rec or not rec.get("binomial"):
            skipped_no_label += 1
            continue
        src = REPO_ROOT / local
        if not src.exists():
            failed.append((drive_id, "missing local file"))
            continue
        ext = src.suffix.lower()
        binomial = rec["binomial"]
        class_dir = OUT_DIR / slugify(binomial)
        class_dir.mkdir(parents=True, exist_ok=True)

        try:
            if ext in IMAGE_EXTS:
                crops = process_image(detector, src, rec, class_dir, imgsz, conf, device)
            elif ext in VIDEO_EXTS:
                crops = process_video(detector, src, rec, class_dir, sample_hz, max_crops, imgsz, conf, device)
            else:
                skipped_unsupported += 1
                continue
        except Exception as e:
            failed.append((drive_id, f"{type(e).__name__}: {e}"))
            continue

        all_crops.extend(crops)
        # Only register taxonomy for classes that actually produced crops —
        # otherwise the manifest claims a class exists that has zero training
        # data on disk, which trips up loaders that scan the folder tree.
        if crops:
            by_label_count[binomial] = by_label_count.get(binomial, 0) + len(crops)
            taxonomy.setdefault(binomial, {
                "family": rec.get("family"),
                "genus": rec.get("genus"),
                "species": rec.get("species"),
                "common_name": rec.get("common_name"),
                "display_label": rec.get("display_label"),
            })
        else:
            # Class dir was created pre-detection but produced no crops; drop
            # it so the dataset stays self-consistent.
            try:
                class_dir.rmdir()
            except OSError:
                pass

        if (i + 1) % 10 == 0 or i + 1 == len(downloads):
            elapsed = time.monotonic() - t0
            print(f"  [{i+1:3d}/{len(downloads)}] crops so far: {len(all_crops)}  elapsed: {elapsed:.0f}s")

    # Write manifest + taxonomy
    manifest_path = OUT_DIR / "_dataset_manifest.json"
    taxonomy_path = OUT_DIR / "_taxonomy.json"
    manifest_path.write_text(json.dumps([asdict(c) for c in all_crops], indent=2))
    taxonomy_path.write_text(json.dumps(taxonomy, indent=2, sort_keys=True))

    elapsed = time.monotonic() - t0
    print()
    print(f"done in {elapsed:.0f}s")
    print(f"crops: {len(all_crops)}  unique classes: {len(by_label_count)}")
    print(f"skipped (unsupported ext): {skipped_unsupported}")
    print(f"skipped (no label): {skipped_no_label}")
    print(f"failed: {len(failed)}")
    for d, err in failed[:5]:
        print(f"  {d}: {err}")
    print()
    print("top 10 classes by crop count:")
    for label, n in sorted(by_label_count.items(), key=lambda kv: -kv[1])[:10]:
        print(f"  {n:4d}  {label}")
    print()
    print(f"manifest: {manifest_path.relative_to(REPO_ROOT)}")
    print(f"taxonomy: {taxonomy_path.relative_to(REPO_ROOT)}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--detector", default=str(DETECTOR_DEFAULT))
    ap.add_argument("--sample-hz", type=float, default=2.0,
                    help="frames-per-second sampling rate for videos (default 2 Hz)")
    ap.add_argument("--max-crops", type=int, default=120,
                    help="cap crops per video clip (default 120)")
    ap.add_argument("--imgsz", type=int, default=640)
    ap.add_argument("--conf", type=float, default=0.15,
                    help="detector confidence threshold — lower than production to "
                         "accept marginal detections, since the volunteer's label "
                         "is more trustworthy than the model's confidence here")
    ap.add_argument("--device", default="cuda:0")
    ap.add_argument("--limit", type=int, default=None,
                    help="cap source files (for smoke testing)")
    args = ap.parse_args()
    build_dataset(
        detector_path=Path(args.detector),
        sample_hz=args.sample_hz,
        max_crops=args.max_crops,
        imgsz=args.imgsz,
        conf=args.conf,
        device=args.device,
        limit=args.limit,
    )


if __name__ == "__main__":
    main()

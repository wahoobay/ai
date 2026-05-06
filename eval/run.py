#!/usr/bin/env python3
"""Frozen evaluation harness.

Reads eval/manifest.json, runs each listed clip through the production
Fishial pipeline, compares to labels, writes a dated report to
eval/reports/.

Exits 1 with a clear message if:
  - manifest empty (no clips yet — expected during bootstrap)
  - any clip or label file is missing / malformed
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "services" / "worker"))

# Worker-side imports
from app.config import Config  # noqa: E402
from app.fishial import FishialPipeline  # noqa: E402
from app.provenance import compute as compute_prov  # noqa: E402

from eval import metrics  # noqa: E402

# ---------------------------------------------------------------------------
# Manifest + label schema
# ---------------------------------------------------------------------------


@dataclass
class ClipSpec:
    id: str
    clip_path: str
    labels_path: str
    note: str = ""


def _load_manifest(path: Path) -> list[ClipSpec]:
    data = json.loads(path.read_text())
    return [ClipSpec(**c) for c in data.get("clips", [])]


def _load_labels(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text())
    # Expected shape: minimal COCO with an extra "frame_bbox_labels" mapping:
    # {
    #   "images":    [{"id": int, "frame_index": int, "width": int, "height": int}, ...],
    #   "annotations":[{"image_id": int, "bbox": [x,y,w,h], "category_id": int}, ...],
    #   "categories":[{"id": int, "name": str}, ...]
    # }
    required = {"images", "annotations", "categories"}
    if not required.issubset(data.keys()):
        raise ValueError(f"labels at {path} missing keys {required - data.keys()}")
    return data


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def _evaluate_clip(pipeline: FishialPipeline, spec: ClipSpec) -> dict:
    import cv2  # local import so metrics.py stays numpy-only

    clip_path = (REPO_ROOT / spec.clip_path).resolve()
    labels_path = (REPO_ROOT / spec.labels_path).resolve()
    if not clip_path.exists():
        raise FileNotFoundError(clip_path)
    if not labels_path.exists():
        raise FileNotFoundError(labels_path)

    labels = _load_labels(labels_path)
    # index annotations + categories
    cat_by_id = {c["id"]: c["name"] for c in labels["categories"]}
    anns_by_frame: dict[int, list[dict]] = {}
    image_frame_idx: dict[int, int] = {}
    for img in labels["images"]:
        image_frame_idx[img["id"]] = int(img.get("frame_index", img["id"]))
    for ann in labels["annotations"]:
        fi = image_frame_idx.get(ann["image_id"])
        if fi is None:
            continue
        anns_by_frame.setdefault(fi, []).append(ann)

    cap = cv2.VideoCapture(str(clip_path))
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    det_tp = det_fp = det_fn = 0
    top1_correct: list[int] = []
    top3_correct: list[int] = []
    top5_correct: list[int] = []
    pred_top1: list[str] = []
    truth_classes: list[str] = []
    latencies_ms: list[float] = []

    for frame_idx in sorted(anns_by_frame.keys()):
        if frame_idx >= total_frames:
            continue
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
        ok, frame = cap.read()
        if not ok:
            continue

        t0 = time.time()
        detections = pipeline.process_frame(frame)
        latencies_ms.append((time.time() - t0) * 1000)

        # build bbox arrays
        pred_boxes = np.array([d.bbox for d in detections], dtype=np.float32) \
            if detections else np.zeros((0, 4), dtype=np.float32)
        gt_anns = anns_by_frame[frame_idx]
        gt_boxes = np.array([
            [a["bbox"][0], a["bbox"][1], a["bbox"][0] + a["bbox"][2], a["bbox"][1] + a["bbox"][3]]
            for a in gt_anns
        ], dtype=np.float32)

        tp, fp, fn = metrics.match_detections(pred_boxes, gt_boxes)
        det_tp += tp
        det_fp += fp
        det_fn += fn

        # class metrics: only over matched detections
        ious = metrics.iou_xyxy(pred_boxes, gt_boxes)
        if ious.size:
            matched_gt: set[int] = set()
            local_ious = ious.copy()
            while True:
                if local_ious.size == 0:
                    break
                idx = int(np.argmax(local_ious))
                pi, gi = np.unravel_index(idx, local_ious.shape)
                if local_ious[pi, gi] < 0.5:
                    break
                if gi in matched_gt:
                    local_ious[pi, gi] = -1
                    continue
                matched_gt.add(int(gi))
                truth_name = cat_by_id.get(gt_anns[gi]["category_id"], "unknown")
                tk_names = [p.name for p in detections[pi].topk]
                top1_correct.append(1 if (tk_names[:1] == [truth_name]) else 0)
                top3_correct.append(1 if truth_name in tk_names[:3] else 0)
                top5_correct.append(1 if truth_name in tk_names[:5] else 0)
                pred_top1.append(tk_names[0] if tk_names else "unknown")
                truth_classes.append(truth_name)
                local_ious[pi, :] = -1
                local_ious[:, gi] = -1

    cap.release()

    return {
        "clip_id": spec.id,
        "detector": metrics.prf1(det_tp, det_fp, det_fn),
        "classifier": {
            "top1": {
                **asdict(metrics.top_k_accuracy([[p] for p in pred_top1], truth_classes, 1)),
                "ci": metrics.bootstrap_scalar_ci(top1_correct),
            },
            "top3_ci": metrics.bootstrap_scalar_ci(top3_correct),
            "top5_ci": metrics.bootstrap_scalar_ci(top5_correct),
            "per_class": metrics.per_class_prf1(pred_top1, truth_classes),
        },
        "latency_ms": metrics.latency_stats(latencies_ms),
    }


def _git_sha(short: bool = True) -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", ("--short=12" if short else "HEAD")],
            cwd=REPO_ROOT, stderr=subprocess.DEVNULL, text=True,
        ).strip()
        return out or "unknown"
    except Exception:
        return "unknown"


def _write_report(out_dir: Path, payload: dict) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "metrics.json").write_text(json.dumps(payload, indent=2, default=str))
    (out_dir / "provenance.json").write_text(json.dumps(payload["provenance"], indent=2, default=str))

    # simple markdown summary
    prov = payload["provenance"]
    lines = [
        f"# Wahoo Bay eval report — {payload['timestamp']}",
        "",
        "## Provenance",
        f"- model_version: `{prov['model_version']}`",
        f"- detector_sha: `{prov['detector_sha256']}`",
        f"- classifier_sha: `{prov['classifier_sha256']}`",
        f"- config_hash: `{prov['config_hash']}`",
        f"- git: `{prov['pipeline_git_sha']}`",
        f"- eval_git: `{payload['eval_git_sha']}`",
        "",
        "## Results",
        "| clip | det P | det R | det F1 | top-1 | top-1 CI | top-3 CI | top-5 CI | p95 latency |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    def ci_s(x):
        return f"[{x['lo']:.2f}, {x['hi']:.2f}]" if x.get("lo") is not None else "—"

    for r in payload["clips"]:
        d = r["detector"]
        c = r["classifier"]
        t1 = c["top1"]
        t1ci = t1["ci"]
        t3 = c["top3_ci"]
        t5 = c["top5_ci"]
        lines.append(
            f"| {r['clip_id']} | {d['precision']:.2f} | {d['recall']:.2f} | {d['f1']:.2f} "
            f"| {t1['hits']}/{t1['total']} ({t1['acc']*100:.1f}%) | {ci_s(t1ci)} | {ci_s(t3)} | {ci_s(t5)} "
            f"| {r['latency_ms'].get('p95', 0):.1f} ms |"
        )
    (out_dir / "report.md").write_text("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--manifest", default=str(REPO_ROOT / "eval" / "manifest.json"))
    ap.add_argument("--out", default=str(REPO_ROOT / "eval" / "reports"))
    ap.add_argument("--clips", nargs="*", help="Restrict to these clip ids")
    args = ap.parse_args()

    manifest_path = Path(args.manifest)
    if not manifest_path.exists():
        print(f"manifest not found: {manifest_path}", file=sys.stderr)
        return 1
    clips = _load_manifest(manifest_path)
    if args.clips:
        wanted = set(args.clips)
        clips = [c for c in clips if c.id in wanted]
    if not clips:
        print(
            "no clips to evaluate. Populate eval/manifest.json and drop\n"
            "videos + COCO-format labels under eval/clips + eval/labels.\n"
            "Example entry:\n"
            '  {"id": "sergeant_major_1", "clip_path": "eval/clips/sm1.mp4", "labels_path": "eval/labels/sm1.json"}',
            file=sys.stderr,
        )
        return 1

    cfg = Config.from_env()
    pipeline = FishialPipeline(cfg)
    prov = compute_prov(cfg)

    per_clip: list[dict] = []
    for spec in clips:
        print(f"→ {spec.id}", flush=True)
        per_clip.append(_evaluate_clip(pipeline, spec))

    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    payload = {
        "timestamp": ts,
        "eval_git_sha": _git_sha(),
        "provenance": prov.as_dict(),
        "clips": per_clip,
    }
    out_dir = Path(args.out) / ts
    _write_report(out_dir, payload)
    print(f"wrote {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

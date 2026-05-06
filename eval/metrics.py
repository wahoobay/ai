"""Evaluation metrics, numpy-only.

Kept separate from services/ so the harness has no runtime dependency on the
dashboard or worker modules — import the worker's fishial.FishialPipeline
directly when running.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

# ---------------------------------------------------------------------------
# Detector metrics (bbox matching at fixed IoU threshold)
# ---------------------------------------------------------------------------


def iou_xyxy(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """IoU between M boxes in `a` and N boxes in `b`, returns (M, N)."""
    if a.size == 0 or b.size == 0:
        return np.zeros((a.shape[0], b.shape[0]), dtype=np.float32)
    ax1, ay1, ax2, ay2 = a[:, 0, None], a[:, 1, None], a[:, 2, None], a[:, 3, None]
    bx1, by1, bx2, by2 = b[:, 0][None, :], b[:, 1][None, :], b[:, 2][None, :], b[:, 3][None, :]
    inter_w = np.clip(np.minimum(ax2, bx2) - np.maximum(ax1, bx1), 0, None)
    inter_h = np.clip(np.minimum(ay2, by2) - np.maximum(ay1, by1), 0, None)
    inter = inter_w * inter_h
    area_a = (ax2 - ax1) * (ay2 - ay1)
    area_b = (bx2 - bx1) * (by2 - by1)
    union = area_a + area_b - inter + 1e-9
    return inter / union


def match_detections(
    preds: np.ndarray,
    gts:   np.ndarray,
    iou_threshold: float = 0.5,
) -> tuple[int, int, int]:
    """Greedy matching. Returns (tp, fp, fn)."""
    if len(gts) == 0:
        return 0, len(preds), 0
    if len(preds) == 0:
        return 0, 0, len(gts)
    ious = iou_xyxy(preds, gts)
    matched_gt: set[int] = set()
    tp = 0
    # match in order of IoU; for eval we do greedy pairing from highest IoU down
    while True:
        if ious.size == 0:
            break
        idx = int(np.argmax(ious))
        pi, gi = np.unravel_index(idx, ious.shape)
        if ious[pi, gi] < iou_threshold:
            break
        tp += 1
        matched_gt.add(int(gi))
        ious[pi, :] = -1
        ious[:, gi] = -1
    fp = len(preds) - tp
    fn = len(gts) - len(matched_gt)
    return tp, fp, fn


def prf1(tp: int, fp: int, fn: int) -> dict:
    p = tp / (tp + fp) if (tp + fp) else 0.0
    r = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * p * r / (p + r) if (p + r) else 0.0
    return {"tp": int(tp), "fp": int(fp), "fn": int(fn),
            "precision": float(p), "recall": float(r), "f1": float(f1)}


# ---------------------------------------------------------------------------
# Classifier metrics
# ---------------------------------------------------------------------------


@dataclass
class TopKResult:
    k: int
    hits: int
    total: int

    @property
    def acc(self) -> float:
        return self.hits / self.total if self.total else 0.0


def top_k_accuracy(
    predictions_top_k: Sequence[Sequence[str]],
    truths: Sequence[str],
    k: int,
) -> TopKResult:
    hits = 0
    for topk, y in zip(predictions_top_k, truths, strict=False):
        if y in list(topk)[:k]:
            hits += 1
    return TopKResult(k=k, hits=hits, total=len(truths))


def per_class_prf1(
    pred_top1: Sequence[str],
    truths:    Sequence[str],
) -> list[dict]:
    classes = sorted(set(truths) | set(pred_top1))
    out: list[dict] = []
    for c in classes:
        tp = sum(1 for p, t in zip(pred_top1, truths, strict=False) if p == c and t == c)
        fp = sum(1 for p, t in zip(pred_top1, truths, strict=False) if p == c and t != c)
        fn = sum(1 for p, t in zip(pred_top1, truths, strict=False) if p != c and t == c)
        support = sum(1 for t in truths if t == c)
        row = prf1(tp, fp, fn)
        row.update({"class": c, "support": support})
        out.append(row)
    return out


# ---------------------------------------------------------------------------
# Latency
# ---------------------------------------------------------------------------


def latency_stats(timings_ms: Sequence[float]) -> dict:
    if not timings_ms:
        return {"n": 0}
    a = np.asarray(timings_ms, dtype=np.float64)
    return {
        "n": int(a.size),
        "mean": float(a.mean()),
        "p50":  float(np.percentile(a, 50)),
        "p95":  float(np.percentile(a, 95)),
        "p99":  float(np.percentile(a, 99)),
        "max":  float(a.max()),
    }


# ---------------------------------------------------------------------------
# Bootstrap CIs on scalars
# ---------------------------------------------------------------------------


def bootstrap_scalar_ci(
    values_01: Sequence[int | float],
    B: int = 2000,
    alpha: float = 0.05,
    seed: int = 0,
) -> dict:
    """Bootstrap a mean (e.g. accuracy over per-sample 0/1 correctness)."""
    arr = np.asarray(values_01, dtype=np.float64)
    n = arr.size
    if n == 0:
        return {"point": None, "lo": None, "hi": None, "n": 0}
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, n, size=(B, n))
    means = arr[idx].mean(axis=1)
    lo, hi = np.quantile(means, [alpha / 2, 1 - alpha / 2])
    return {"point": float(arr.mean()), "lo": float(lo), "hi": float(hi), "n": int(n)}

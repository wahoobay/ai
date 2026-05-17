"""Train a DINOv2 ViT-S/14 classifier from random init on the volunteer-
labelled crop dataset.

This is the baseline "what we get from our data alone, no pretraining"
training run. The architecture matches DINOv2's ViT-S/14 spec, but every
weight starts from a fresh init — none of Meta's self-supervised
pretraining and none of Fishial's 866-class checkpoint is loaded.
Expectation: macro F1 will be poor (single-digit %) because a 22 M-param
ViT trained from scratch on ~2.7 k crops across 131 classes is severely
under-resourced. The point of the run is to fix a numerical baseline —
"this is how far our community-labelled dataset alone goes" — so any
later experiment (pretrained backbone, more data, taxonomically-aware
loss, …) has something to beat.

Inputs:
  data/fine_tune/<binomial_slug>/*.jpg  + _dataset_manifest.json + _taxonomy.json

Outputs:
  data/baseline_runs/<run_id>/
      config.json               — exact hyperparams + class list, for replay
      train_history.json        — per-epoch train/val loss + accuracy
      metrics.json              — final top-1 / top-5 / macro F1
      classification_report.txt — sklearn-style per-class P/R/F1
      checkpoint_best.pt        — weights of the best-macro-F1 epoch
      checkpoint_last.pt        — weights of the final epoch (for resume)

Conventions:
  - 80/20 stratified split. Classes with only 1 crop go to train only (no
    val example exists; we report them as 0 recall in the per-class
    report).
  - Class-balanced WeightedRandomSampler at training time so the long
    tail isn't starved. Validation pass is unweighted.
  - Augmentation: random resized crop, hflip, RandAugment, ColorJitter,
    Mixup (alpha=0.2), label smoothing 0.1. Standard ViT-from-scratch
    recipe.
  - AdamW + cosine LR with warmup. Mixed-precision (bf16 on Ampere+).
"""
from __future__ import annotations

import argparse
import json
import math
import random
import time
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import timm
import torch
import torch.nn as nn
import torch.nn.functional as F
from PIL import Image
from sklearn.metrics import classification_report, f1_score, top_k_accuracy_score
from sklearn.model_selection import train_test_split
from torch.utils.data import DataLoader, Dataset, WeightedRandomSampler
from torchvision import transforms

REPO_ROOT = Path(__file__).resolve().parent.parent
DATASET_DIR = REPO_ROOT / "data" / "fine_tune"
MANIFEST_PATH = DATASET_DIR / "_dataset_manifest.json"
TAXONOMY_PATH = DATASET_DIR / "_taxonomy.json"
RUNS_DIR = REPO_ROOT / "data" / "baseline_runs"


@dataclass
class TrainConfig:
    run_id: str
    arch: str = "vit_small_patch14_dinov2"
    img_size: int = 224         # native is 518 for DINOv2; 224 is faster + standard ViT-S
    num_classes: int = 0
    batch_size: int = 32
    val_batch_size: int = 64
    epochs: int = 60
    warmup_epochs: int = 5
    lr: float = 3e-4
    weight_decay: float = 0.05
    label_smoothing: float = 0.1
    mixup_alpha: float = 0.2
    drop_path: float = 0.1
    seed: int = 42
    val_split: float = 0.20
    device: str = "cuda:0"
    amp_dtype: str = "bf16"     # bf16 needs Ampere+; falls back to fp16 elsewhere
    num_workers: int = 4
    # "stratified" = random 80/20 with class balance (frames from one video
    # leak across splits → optimistic numbers). "by_source" = 80/20 over
    # source drive_ids; honest generalisation test, but most classes here
    # have only one source so they end up entirely train- or val-only.
    split_mode: str = "stratified"


# ---------- dataset ----------


class CropDataset(Dataset):
    """ImageFolder-style dataset reading from data/fine_tune/<class>/*.jpg.
    Stores (path, class_idx) pairs; transform happens at __getitem__."""

    def __init__(self, items: list[tuple[Path, int]], transform):
        self.items = items
        self.transform = transform

    def __len__(self) -> int:
        return len(self.items)

    def __getitem__(self, idx: int):
        path, label = self.items[idx]
        img = Image.open(path).convert("RGB")
        return self.transform(img), label


def build_splits(cfg: TrainConfig) -> tuple[list[tuple[Path, int]],
                                             list[tuple[Path, int]],
                                             list[str]]:
    """Discover crops on disk, build the class list, do an 80/20 split.

    Two split modes (selected via cfg.split_mode):
      * "stratified" (default) — random 80/20 stratified by class. Tends
        to overstate generalisation because random frames from the same
        source video land in both train and val.
      * "by_source" — hold out 20% of the source drive_ids; all crops
        from a held-out source go to val. The honest "did the model
        learn the species or just the individual fish?" eval. Most
        classes have only one source file in this dataset, so under
        this split they'll be entirely train OR entirely val — the
        latter become effectively zero-shot tests.
    """
    classes = sorted(p.name for p in DATASET_DIR.iterdir()
                     if p.is_dir() and not p.name.startswith("_"))
    cls_to_idx = {c: i for i, c in enumerate(classes)}

    samples: list[tuple[Path, int]] = []
    for c in classes:
        for f in sorted((DATASET_DIR / c).iterdir()):
            if f.suffix.lower() in (".jpg", ".jpeg", ".png"):
                samples.append((f, cls_to_idx[c]))

    paths = [s[0] for s in samples]
    labels = np.array([s[1] for s in samples])

    if cfg.split_mode == "by_source":
        train_idx, val_idx = _split_by_source(paths, labels, cfg)
        kind = "by_source"
    elif cfg.split_mode == "temporal_within_source":
        train_idx, val_idx = _split_temporal_within_source(paths, labels, cfg)
        kind = "temporal_within_source"
    else:
        train_idx, val_idx = _split_stratified(labels, cfg)
        kind = "stratified"

    train = [(paths[i], int(labels[i])) for i in train_idx]
    val   = [(paths[i], int(labels[i])) for i in val_idx]
    cls_counts = Counter(labels.tolist())
    only_one_count = sum(1 for c, n in cls_counts.items() if n == 1)
    print(f"split[{kind}]: train={len(train)}  val={len(val)}  "
          f"classes={len(classes)}  singletons={only_one_count}")
    return train, val, classes


def _split_stratified(labels: np.ndarray, cfg: TrainConfig) -> tuple[np.ndarray, np.ndarray]:
    """Class-stratified random split. Singleton classes go to train only."""
    cls_counts = Counter(labels.tolist())
    stratifiable_mask = np.array([cls_counts[lbl] >= 2 for lbl in labels])
    if stratifiable_mask.any():
        train_idx_strat, val_idx = train_test_split(
            np.where(stratifiable_mask)[0],
            test_size=cfg.val_split,
            random_state=cfg.seed,
            stratify=labels[stratifiable_mask],
        )
    else:
        train_idx_strat, val_idx = np.array([], dtype=int), np.array([], dtype=int)
    singleton_idx = np.where(~stratifiable_mask)[0]
    train_idx = np.concatenate([train_idx_strat, singleton_idx])
    return train_idx, val_idx


def _split_by_source(
    paths: list[Path], labels: np.ndarray, cfg: TrainConfig,
) -> tuple[np.ndarray, np.ndarray]:
    """Hold out 20% of source drive_ids. All crops from a held-out source
    land in val; train sees none of those clips. The drive_id for each
    crop comes from _dataset_manifest.json (joined on the crop file
    path)."""
    manifest = json.loads((DATASET_DIR / "_dataset_manifest.json").read_text())
    crop_to_drive = {(REPO_ROOT / r["crop_path"]).resolve(): r["drive_id"] for r in manifest}
    groups = np.array([crop_to_drive.get(p.resolve(), "unknown") for p in paths])

    unique_drives = np.array(sorted(set(groups)))
    rng = np.random.default_rng(cfg.seed)
    perm = rng.permutation(len(unique_drives))
    n_val_drives = max(1, int(round(len(unique_drives) * cfg.val_split)))
    val_drive_set = set(unique_drives[perm[:n_val_drives]])

    is_val = np.array([g in val_drive_set for g in groups])
    train_idx = np.where(~is_val)[0]
    val_idx = np.where(is_val)[0]
    print(f"  source-split: {len(unique_drives) - n_val_drives} train sources / "
          f"{n_val_drives} val sources")
    return train_idx, val_idx


def _split_temporal_within_source(
    paths: list[Path], labels: np.ndarray, cfg: TrainConfig,
) -> tuple[np.ndarray, np.ndarray]:
    """Within each video source, hold out the last val_split fraction of
    frames as val. Image sources (single frame) stay in train. Same fish
    individual on both sides, but the val frames are a temporally
    separate segment of the clip — tests within-clip generalisation
    (pose changes, lighting drift, motion blur) without requiring more
    independent source clips per species.

    crop_id encodes both drive_id and frame_index for videos (the
    cropper builds them as `<drive_id>_f<idx>_d<det>`); we sort frames
    by their numeric index and split the trailing val_split of each
    video's frames into val. Image crop_ids look like
    `<drive_id>_d<det>` (no frame index) and always land in train.
    """
    manifest = json.loads((DATASET_DIR / "_dataset_manifest.json").read_text())
    crop_to_rec: dict[str, dict] = {
        (REPO_ROOT / r["crop_path"]).resolve().as_posix(): r for r in manifest
    }

    # Group video crops by drive_id, sorted by frame_index
    by_drive: dict[str, list[tuple[int, int]]] = {}    # drive_id → [(frame_idx, sample_idx)]
    image_only: list[int] = []                          # indices of image-sourced crops
    for i, p in enumerate(paths):
        rec = crop_to_rec.get(p.resolve().as_posix())
        if rec is None or rec.get("source_kind") != "video":
            image_only.append(i)
            continue
        by_drive.setdefault(rec["drive_id"], []).append((int(rec.get("frame_index", 0)), i))

    val_idx_list: list[int] = []
    train_idx_list: list[int] = list(image_only)
    for drive_id, items in by_drive.items():
        items.sort(key=lambda kv: kv[0])
        n = len(items)
        n_val = max(1, int(round(n * cfg.val_split)))
        train_part = [s for _, s in items[: n - n_val]]
        val_part   = [s for _, s in items[n - n_val:]]
        train_idx_list.extend(train_part)
        val_idx_list.extend(val_part)

    print(f"  temporal-within-source: {len(by_drive)} videos contribute val frames; "
          f"{len(image_only)} image-only crops stay in train")
    return np.array(train_idx_list), np.array(val_idx_list)


# ---------- training utilities ----------


def cosine_lr(epoch: int, cfg: TrainConfig) -> float:
    """Cosine schedule with linear warmup."""
    if epoch < cfg.warmup_epochs:
        return cfg.lr * (epoch + 1) / max(1, cfg.warmup_epochs)
    progress = (epoch - cfg.warmup_epochs) / max(1, cfg.epochs - cfg.warmup_epochs)
    return cfg.lr * 0.5 * (1.0 + math.cos(math.pi * progress))


def mixup_pair(x: torch.Tensor, y: torch.Tensor, alpha: float) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor, float]:
    if alpha <= 0:
        return x, y, y, 1.0
    lam = float(np.random.beta(alpha, alpha))
    perm = torch.randperm(x.size(0), device=x.device)
    return x * lam + x[perm] * (1 - lam), y, y[perm], lam


def mixup_loss(logits: torch.Tensor, y_a: torch.Tensor, y_b: torch.Tensor,
               lam: float, smoothing: float) -> torch.Tensor:
    loss_fn = nn.CrossEntropyLoss(label_smoothing=smoothing)
    return lam * loss_fn(logits, y_a) + (1 - lam) * loss_fn(logits, y_b)


def set_seed(seed: int) -> None:
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)


# ---------- model ----------


def build_model(cfg: TrainConfig) -> nn.Module:
    """DINOv2 ViT-S/14 architecture with random init.

    timm exposes the architecture spec under ``vit_small_patch14_dinov2``;
    passing ``pretrained=False`` gives us a freshly-initialised network
    with no DINOv2 self-supervised weights loaded. The head is replaced
    with a fresh ``nn.Linear`` matching our class count.
    """
    model = timm.create_model(
        cfg.arch,
        pretrained=False,
        num_classes=cfg.num_classes,
        img_size=cfg.img_size,
        drop_path_rate=cfg.drop_path,
    )
    return model


# ---------- main training loop ----------


def train(cfg: TrainConfig) -> dict:
    set_seed(cfg.seed)
    run_dir = RUNS_DIR / cfg.run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    train_items, val_items, classes = build_splits(cfg)
    cfg.num_classes = len(classes)

    # Augmentation. From-scratch ViTs need heavy aug to avoid collapse.
    train_tf = transforms.Compose([
        transforms.RandomResizedCrop(cfg.img_size, scale=(0.7, 1.0)),
        transforms.RandomHorizontalFlip(),
        transforms.RandAugment(num_ops=2, magnitude=9),
        transforms.ColorJitter(0.2, 0.2, 0.2, 0.05),
        transforms.ToTensor(),
        transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
        transforms.RandomErasing(p=0.1, scale=(0.02, 0.10)),
    ])
    val_tf = transforms.Compose([
        transforms.Resize(int(cfg.img_size * 1.14)),
        transforms.CenterCrop(cfg.img_size),
        transforms.ToTensor(),
        transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
    ])

    train_ds = CropDataset(train_items, train_tf)
    val_ds   = CropDataset(val_items, val_tf)

    # Class-balanced sampling: weight each sample by 1/sqrt(count) so the
    # long tail isn't starved but capped-at-120 classes don't overwhelm.
    train_labels = np.array([t[1] for t in train_items])
    cls_count = np.bincount(train_labels, minlength=cfg.num_classes).astype(np.float64)
    cls_count = np.maximum(cls_count, 1.0)
    sample_w = (1.0 / np.sqrt(cls_count))[train_labels]
    sampler = WeightedRandomSampler(weights=sample_w.tolist(),
                                    num_samples=len(train_ds), replacement=True)

    train_loader = DataLoader(train_ds, batch_size=cfg.batch_size, sampler=sampler,
                              num_workers=cfg.num_workers, pin_memory=True, drop_last=True)
    val_loader = DataLoader(val_ds, batch_size=cfg.val_batch_size, shuffle=False,
                            num_workers=cfg.num_workers, pin_memory=True)

    model = build_model(cfg).to(cfg.device)
    optim = torch.optim.AdamW(model.parameters(), lr=cfg.lr,
                              weight_decay=cfg.weight_decay, betas=(0.9, 0.999))
    amp_dtype = (torch.bfloat16 if cfg.amp_dtype == "bf16" and torch.cuda.is_bf16_supported()
                 else torch.float16)

    # Save config + class list up front so partial runs are inspectable.
    (run_dir / "config.json").write_text(json.dumps(asdict(cfg), indent=2))
    (run_dir / "classes.json").write_text(json.dumps(classes, indent=2))

    best_f1 = -1.0
    history: list[dict] = []
    t0 = time.monotonic()
    for epoch in range(cfg.epochs):
        for g in optim.param_groups:
            g["lr"] = cosine_lr(epoch, cfg)
        model.train()
        running_loss, running_n = 0.0, 0
        for x, y in train_loader:
            x = x.to(cfg.device, non_blocking=True)
            y = y.to(cfg.device, non_blocking=True)
            x, y_a, y_b, lam = mixup_pair(x, y, cfg.mixup_alpha)
            optim.zero_grad(set_to_none=True)
            with torch.amp.autocast("cuda", dtype=amp_dtype):
                logits = model(x)
                loss = mixup_loss(logits, y_a, y_b, lam, cfg.label_smoothing)
            loss.backward()
            optim.step()
            running_loss += float(loss.item()) * x.size(0)
            running_n += x.size(0)
        train_loss = running_loss / max(1, running_n)

        # Validation
        model.eval()
        all_logits: list[np.ndarray] = []
        all_labels: list[np.ndarray] = []
        val_loss, val_n = 0.0, 0
        with torch.no_grad():
            for x, y in val_loader:
                x = x.to(cfg.device, non_blocking=True)
                y = y.to(cfg.device, non_blocking=True)
                with torch.amp.autocast("cuda", dtype=amp_dtype):
                    logits = model(x)
                    loss = F.cross_entropy(logits, y, label_smoothing=cfg.label_smoothing)
                val_loss += float(loss.item()) * x.size(0)
                val_n += x.size(0)
                all_logits.append(logits.float().cpu().numpy())
                all_labels.append(y.cpu().numpy())
        val_loss = val_loss / max(1, val_n)

        if val_n > 0:
            logits = np.concatenate(all_logits)
            labels = np.concatenate(all_labels)
            preds = logits.argmax(axis=1)
            top1 = float((preds == labels).mean())
            try:
                top5 = float(top_k_accuracy_score(labels, logits, k=5, labels=list(range(cfg.num_classes))))
            except ValueError:
                top5 = float("nan")
            macro_f1 = float(f1_score(labels, preds, average="macro", zero_division=0))
        else:
            top1 = top5 = macro_f1 = 0.0

        elapsed = time.monotonic() - t0
        row = {
            "epoch": epoch, "train_loss": round(train_loss, 4),
            "val_loss": round(val_loss, 4),
            "val_top1": round(top1, 4), "val_top5": round(top5, 4),
            "val_macro_f1": round(macro_f1, 4),
            "lr": round(optim.param_groups[0]["lr"], 6),
            "elapsed_s": round(elapsed, 1),
        }
        history.append(row)
        print(f"  ep{epoch:02d}  loss={train_loss:.3f}/{val_loss:.3f}  "
              f"top1={top1:.3f}  top5={top5:.3f}  macroF1={macro_f1:.3f}  "
              f"lr={row['lr']:.4f}  elapsed={elapsed:.0f}s")

        # Checkpoint best + last
        torch.save({"model": model.state_dict(), "cfg": asdict(cfg),
                    "classes": classes, "epoch": epoch}, run_dir / "checkpoint_last.pt")
        if macro_f1 > best_f1:
            best_f1 = macro_f1
            torch.save({"model": model.state_dict(), "cfg": asdict(cfg),
                        "classes": classes, "epoch": epoch,
                        "val_macro_f1": macro_f1},
                       run_dir / "checkpoint_best.pt")

        (run_dir / "train_history.json").write_text(json.dumps(history, indent=2))

    # Final eval: load best, dump per-class classification report
    best_ckpt = torch.load(run_dir / "checkpoint_best.pt", map_location=cfg.device,
                          weights_only=False)
    model.load_state_dict(best_ckpt["model"])
    model.eval()
    final_logits: list[np.ndarray] = []
    final_labels: list[np.ndarray] = []
    with torch.no_grad():
        for x, y in val_loader:
            x = x.to(cfg.device, non_blocking=True)
            with torch.amp.autocast("cuda", dtype=amp_dtype):
                logits = model(x)
            final_logits.append(logits.float().cpu().numpy())
            final_labels.append(y.numpy())

    metrics = {"best_macro_f1": best_f1, "best_epoch": best_ckpt.get("epoch")}
    if final_labels:
        labels = np.concatenate(final_labels)
        logits = np.concatenate(final_logits)
        preds = logits.argmax(axis=1)
        metrics.update({
            "val_top1": float((preds == labels).mean()),
            "val_macro_f1": float(f1_score(labels, preds, average="macro", zero_division=0)),
            "val_weighted_f1": float(f1_score(labels, preds, average="weighted", zero_division=0)),
        })
        try:
            metrics["val_top5"] = float(top_k_accuracy_score(
                labels, logits, k=5, labels=list(range(cfg.num_classes))))
        except ValueError:
            pass
        report = classification_report(
            labels, preds,
            labels=list(range(cfg.num_classes)),
            target_names=classes,
            digits=3, zero_division=0,
        )
        (run_dir / "classification_report.txt").write_text(report)

    (run_dir / "metrics.json").write_text(json.dumps(metrics, indent=2))
    print()
    print(f"done. best macro F1 = {best_f1:.4f}")
    print(f"run dir: {run_dir.relative_to(REPO_ROOT)}")
    return metrics


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", default=time.strftime("baseline_%Y%m%dT%H%M%S"))
    ap.add_argument("--epochs", type=int, default=60)
    ap.add_argument("--batch-size", type=int, default=32)
    ap.add_argument("--lr", type=float, default=3e-4)
    ap.add_argument("--img-size", type=int, default=224)
    ap.add_argument("--device", default="cuda:0")
    ap.add_argument("--num-workers", type=int, default=4)
    ap.add_argument("--smoke", action="store_true", help="run 3 epochs as a sanity check")
    ap.add_argument("--split-mode",
                    choices=("stratified", "by_source", "temporal_within_source"),
                    default="stratified",
                    help="stratified: random 80/20 with class balance "
                         "(frame-level leakage → optimistic). "
                         "by_source: 80/20 over source drive_ids; brutally "
                         "honest but mostly zero-shot given our dataset. "
                         "temporal_within_source: hold out the last 20% of "
                         "frames from each video clip — same fish, different "
                         "temporal segment, tests within-clip generalisation.")
    args = ap.parse_args()

    cfg = TrainConfig(
        run_id=args.run_id,
        epochs=3 if args.smoke else args.epochs,
        batch_size=args.batch_size,
        lr=args.lr,
        img_size=args.img_size,
        device=args.device,
        num_workers=args.num_workers,
        split_mode=args.split_mode,
    )
    train(cfg)


if __name__ == "__main__":
    main()

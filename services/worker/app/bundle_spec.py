"""Which network a classifier bundle needs. Torch-free, so CI tests it.

scripts/train_baseline.py saves its TrainConfig in every checkpoint as `cfg`, so the checkpoint
names its own arch and input size: a ViT-B bundle loads into a ViT-B without a change to
CLASSIFIER_V2_ARCH. A checkpoint without them falls back to the configured values (spec
docs/superpowers/specs/2026-11-23-pretrained-dino-design.md §4).
"""

from __future__ import annotations


def model_spec(ckpt: dict, arch: str, img_size: int) -> tuple[str, int]:
    """(arch, img_size) for the checkpoint: its own `cfg` where it has them, else the given ones."""
    cfg = ckpt.get("cfg") or {}
    return str(cfg.get("arch") or arch), int(cfg.get("img_size") or img_size)

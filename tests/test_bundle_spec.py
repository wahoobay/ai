"""The worker builds the network a bundle was trained with (services/worker/app/bundle_spec.py).

A checkpoint from scripts/train_baseline.py records its TrainConfig as `cfg` (arch, img_size), so a
ViT-B bundle loads into a ViT-B without anyone changing CLASSIFIER_V2_ARCH; a checkpoint without it
(an older or foreign one) falls back to the configured arch and size. Spec
docs/superpowers/specs/2026-11-23-pretrained-dino-design.md §4."""

from __future__ import annotations

from services.worker.app.bundle_spec import model_spec

DEFAULT = ("vit_small_patch14_dinov2", 224)


def test_a_checkpoint_names_its_own_arch_and_size():
    ckpt = {"model": {}, "cfg": {"arch": "vit_base_patch14_dinov2", "img_size": 224}}
    assert model_spec(ckpt, *DEFAULT) == ("vit_base_patch14_dinov2", 224)


def test_a_checkpoint_without_a_config_uses_the_configured_one():
    assert model_spec({"model": {}}, *DEFAULT) == DEFAULT
    assert model_spec({"state_dict": {}, "cfg": None}, *DEFAULT) == DEFAULT


def test_a_config_missing_one_field_keeps_the_configured_value_for_it():
    assert model_spec({"cfg": {"arch": "vit_base_patch14_dinov2"}}, *DEFAULT) == (
        "vit_base_patch14_dinov2",
        224,
    )
    assert model_spec({"cfg": {"img_size": 336}}, *DEFAULT) == ("vit_small_patch14_dinov2", 336)

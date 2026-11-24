# A classifier from Meta's pretrained DINOv2, with nothing from the DGX

2026-11-23 · design (project) · Dan: "let's fine-tune a DINO ViT with all of our training data, not
using anything from the DGX", then "go ahead with your plan"

## Why

The live classifier (`classifier_v2/aws-2026-11-23`) is our own DINOv2-shaped ViT-S/14 trained from
random weights on the DGX, then fine-tuned on AWS. It never had DINOv2's pretraining, and on the
SeaHIVE camera the DGX model scored 4.7% top-1 on Roy's crops. A backbone pretrained by Meta on
142 M images should carry over to a new camera far better, and a model started from Meta's weights
has no DGX ancestry (the IP question).

## Choices (Claude's recommendation, Dan's go)

- **DINOv2, not DINOv3.** DINOv2's weights are Apache-2.0. DINOv3's own licence makes derivative
  weights distributable only under it, and Meta may change or end it. For whole-crop
  classification at a given size, DINOv3 gains little over DINOv2. DINOv2 also needs no change to
  the images (timm 1.0.15 already has it; DINOv3 would need a newer timm and patch size 16).
- **ViT-B/14** (`vit_base_patch14_dinov2.lvd142m`, 86 M) is the candidate to ship. **ViT-S/14**
  (`vit_small_patch14_dinov2.lvd142m`) runs alongside it for about $0.30: same size as the live
  model, so the pair separates "pretraining" from "size". ViT-L waits until B's rare-species scores
  say it is needed.
- **The data** is the base crops (`datasets/fine_tune/dgx-2026-10-24`: iNaturalist photos and the
  community's clips; they were cut on the DGX, but they are data, not DGX weights) plus a verified
  snapshot. The split stays `temporal_within_source`, seed 42, so validation is the same 586 items
  the first fine-tune was judged on, and its scores compare directly.

## Design

1. **Meta's weights in the models bucket.** infra `scripts/publish_models.py` gains a `"url"`
   source: files downloaded from a pinned Hugging Face revision, published as
   `pretrained/<timm tag>/` (`model.safetensors`, the model card `README.md` for the licence, and
   SHA256SUMS). The training role may read `pretrained/*`.
2. **The run.** A fourth channel, `pretrained`, replaces `init`: the job uses exactly one of them.
   `train_job.py` checks its SHA256SUMS and runs `train_baseline.py --arch <arch> --pretrained
   <file>`. The trainer builds the model with timm from that file (no network: the job runs
   isolated); timm resizes the position grid from 518 to 224. **Layer-wise learning-rate decay**
   (`--layer-decay`, timm's `param_groups_layer_decay`) gives the new head the full rate and each
   layer below 0.75 of the one above, so the pretrained features are adjusted, not overwritten.
3. **The launcher.** `python -m scripts.train_classifier staging --pretrained
   vit_base_patch14_dinov2.lvd142m --compare <job> --wait`: the arch is the tag's name before the
   dot. Defaults for a pretrained run: 30 epochs, learning rate 1e-4, 3 warm-up epochs, layer decay
   0.75. With no starting classifier there is no `metrics_before.json`; `--compare` prints the run
   against an earlier job's `metrics.json` (same snapshot, same validation set).
4. **The worker loads whatever arch a bundle was trained with.** The checkpoint already records
   its `cfg` (`arch`, `img_size`); the worker reads them before building the model and falls back
   to `CLASSIFIER_V2_ARCH` / `CLASSIFIER_V2_INPUT_SIZE` for a checkpoint without them. Shipping a
   B model is then the usual publish and pin.

## Not in scope

ViT-L and larger (later, with a longer run limit); DINOv3; rebuilding the base crops on AWS; a
change to what the worker does with a box (the `no_animal` rule stays).

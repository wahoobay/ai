# The training machine: fine-tuning the classifier on AWS

2026-11-23 · design (project) · approach A (Claude's call, stated to Dan)

## Why

Dan, 2026-11-23: "just fine-tune. build the training machine." The verified dataset (wahoobay/ai#55,
spec `2026-11-22-verified-dataset-training-design.md`) now hands training a snapshot of Roy's labels,
and `scripts/assemble_training_set.py` turns base crops plus a snapshot into the folder
`scripts/train_baseline.py` reads. Nothing runs that trainer since the DGX was shut down
(2026-11-15). Dan's standing decision (2026-11-22): training runs on AWS, one single-GPU machine per
run. "Just fine-tune" sets this round's scope: the classifier only, continued from the live weights;
the detector retrain waits.

## Goals

- One command from Dan's shell starts a run; the machine exists only while the run does and
  bills by the second.
- The run fine-tunes the live classifier (`classifier_v2/dgx-2026-10-24-r1`) on the base crops plus
  a verified snapshot, with the new `no_animal` class.
- Every run records exactly what it trained on (base tar, snapshot id and hash, starting weights,
  image digest, git sha, arguments) and how the starting model scored on the same validation set,
  so "did it get better?" has one honest answer.
- Results land in S3 as loose files in the layout of a published bundle. Nothing goes live.

## Non-goals

- The detector retrain (later, on the same machine type).
- Shipping a fine-tuned model. Before a model with `no_animal` ships, the worker must keep a box it
  calls `no_animal` out of the sightings (2026-11-22 spec, "Handed to the classifier project"), and
  publishing stays `infra/scripts/publish_models.py` plus a pin, on Dan's word.
- Hyperparameter search, spot capacity, several GPUs.

## Approaches

- **A. A SageMaker training job (chosen).** Built for "one machine per run": it pulls our image
  from ECR, copies the S3 inputs onto the machine, runs, copies `/opt/ml/model` back to S3 and
  terminates; a maximum runtime stops a stuck run. ml.g6.xlarge (the same L4 GPU as the camera
  worker's host) costs $1.127 an hour on demand (us-east-1, checked 2026-11-23), and the account's
  quota allows one at a time. No VPC, no instance to look after.
- **B. An EC2 instance per run from a launch template.** The pattern the GPU host uses, but the
  lifecycle is ours to write (user data, log shipping, a watchdog that terminates a hung run), and
  it draws on the same 8-vCPU G-instance quota as the worker's host (4 used).
- **C. AWS Batch with a GPU compute environment.** A queue, a compute environment and a job
  definition for a job that runs a few times a year.

## Design

### 1. Fine-tuning in `scripts/train_baseline.py`

- `--init-checkpoint` / `--init-classes`: start from a published bundle instead of random weights.
  The backbone loads whole; the head's rows are copied by class name
  (`scripts/finetune_init.py`, torch-free so CI tests it), so a class the starting model lacks
  (`no_animal`) starts from the fresh head's initialisation and a class it had keeps its row
  whatever its position.
- Before the first epoch, the starting model is scored on the validation set and written to
  `metrics_before.json`: the live model's score on the very items the fine-tuned one is judged on.
- Every evaluation also scores the reviewers' crops alone (`review_*.jpg`, the assembler's names):
  `review_val_top1`, `review_val_macro_f1`, `review_val_n`. The base set is 78% iNaturalist photos;
  the reviewers' crops are the camera's own view, where the model works.
- `--warmup-epochs` joins the command line. The split stays `temporal_within_source` with seed 42,
  the live model's: the base crops' validation items are the ones it never trained on (482, checked
  in review), so starting from its weights leaks nothing into validation.
- A reviewers' camera-day is split by whole frames: the last 20% of its frames go to validation, and
  a day with fewer than 3 frames stays in training. Many of Roy's frames hold a whole school (up to
  61 boxes) and 21 of his 34 camera-days are a single frame; splitting box by box would put
  neighbours from one frame on both sides. The base clips keep the original rule.
- Macro F1 averages over the classes validation holds, not over every class that was predicted,
  so one stray prediction no longer moves it. A second subset, `review_species`, leaves out
  `no_animal`, which the starting model cannot score.
- Defaults for a fine-tune, set by the launcher, not the trainer: 20 epochs, learning rate 1e-4,
  2 warm-up epochs.

### 2. The run: `scripts/train_job.py` in a trainer image

The image is the worker's CUDA stage plus scikit-learn 1.8.0 (the DGX environment's version), the
three training scripts and `assets/taxonomy.csv`, laid out so `/app` is the repository root the
scripts expect. Dockerfile target `trainer`; CI builds it in the worker's job straight after the
worker image (the builder still holds the CUDA layers) and on `main` pushes it to its own
repository, `ai-wahoobay-trainer`, as `sha-<12>` and `main`. Its own repository keeps the worker's
20-image rollback window whole.

`train_job.py` reads SageMaker's three input channels:

| channel | S3 | on the machine |
|---|---|---|
| `base` | `datasets/fine_tune/dgx-2026-10-24/` | the tar and its SHA256SUMS |
| `snapshot` | `datasets/verified/<env>/<stamp>/` | one verified snapshot |
| `init` | `classifier_v2/dgx-2026-10-24-r1/` | checkpoint, classes.json, SHA256SUMS |

It checks both SHA256SUMS files, assembles `/app/data/fine_tune`, trains, and copies the run's
files (not `checkpoint_last.pt`) plus `_review_merge.json` and `inputs.json` into `/opt/ml/model`
with a new SHA256SUMS. A failure writes its reason to `/opt/ml/output/failure`, which SageMaker
shows as the job's failure reason. The container needs no network: channels and output are
SageMaker's to move, so the job runs with network isolation.

### 3. Infra (wahoobay/infra)

- `ai-wahoobay-trainer` joins the AI ECR repositories (last 20 images, scan on push); the image-push
  role covers it like the others. **Merge order: infra first**, or the ai CI's push has no
  repository.
- Per environment where `ai.training` is on (staging only for now): role
  `ai-wahoobay-<env>-training`, trusted by SageMaker from this account only. It may pull the
  trainer image, read `datasets/fine_tune/*`, `datasets/verified/<env>/*` and `classifier_v2/*`,
  add (never delete) under `training/<env>/*`, and write its logs and metrics. Its ARN in SSM at
  `/ai/<location>/<env>/training-role-arn`.
- `python -m scripts.train_classifier staging` (AWS_PROFILE=cdk) resolves the newest snapshot
  (`latest.json`) or a given stamp, the `main` image's digest and git sha, and the role; prints the
  request; creates job `ai-wahoobay-<env>-clf-<UTC stamp>` (ml.g6.xlarge, 50 GB, 4-hour limit,
  output uncompressed to `training/<env>/`); `--wait` follows it and prints the before/after
  metrics. `--dry-run` prints and stops.

## Anti-goals

- A run that changes what the worker serves.
- A role that can delete a dataset, a model or an earlier run.
- A run whose inputs cannot be named afterwards.

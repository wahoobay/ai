# Phase 3, training half: the verified dataset

2026-11-22 · design (project) · approach A (Claude's call, stated to Dan)

Builds part of §11 of `2026-09-24-reviewer-annotation-dataset-design.md` (the "September spec"), moved
from the DGX to AWS. The September spec stays the reference for anything this document does not
change; where they differ, this one wins.

## Why

Roy's work in the labeling tool cannot reach training today. The current trainer
(`scripts/train_baseline.py`, which produced the live classifier) reads only `data/fine_tune/<class>/`
and its `_dataset_manifest.json`. The May builder (`scripts/build_finetuning_dataset.py`) writes a
layout that trainer never reads, ignores drawn boxes and corrected boxes, and turns every answer
into a crop, even when two answers on one box disagree. The September spec's phase 3, which would
have closed this, was never built.

Dan wants to retrain the detector, then fine-tune the classifier (2026-11-22), on AWS, with Roy's
labels. He chose to build the training half of phase 3 first; the sharing half (Darwin Core,
water and weather matching, volunteer credit, public release) comes later.

What there is to train on (staging, 2026-11-22): Roy's 1,255 answers on model boxes and 30 boxes he
drew, on 188 frames. He completed 179 of those frames (every box answered, missed fish drawn); all
30 drawn boxes are on completed frames. 1,209 of his labels are on frames with more than one fish;
421 are "mark the rest" answers. 119 boxes wait for a second opinion that nobody can give (Roy
cannot answer his own), so nearly every label counts on his answer alone ("single"). His frames
are real SEAHIVECAM captures with their capture times (20 May to 3 November 2026, 34 camera-days,
most from 26 October on), saved by the worker as "interesting" frames; none are the seed job's
synthetic rows (`model_version = 'sample'`, 320 events on staging).

## Goals

- One command on AWS turns the labeling tool's answers into a dated, never-changing snapshot in S3:
  classifier crops, complete frames with COCO boxes for the detector, a manifest, and a report of
  everything held back and why.
- A second command, run where training runs, assembles the classifier's training folder from the
  base crops (`s3://platform-wahoobay-models/datasets/fine_tune/dgx-2026-10-24/fine_tune.tar`) and a
  snapshot, in the layout and manifest format `train_baseline.py` already reads.
- The same rule decides which answer counts everywhere: `services/dashboard/app/review_labels.py`.
- Deterministic: the same database state and the same files give the same content hash, so a run
  with nothing new writes nothing.

## Non-goals

- Training runs, the detector's training format (YOLO conversion, splits), and the GPU machine that
  runs them. Those are the next two projects; this one hands them files.
- Darwin Core, environmental matching, credit, public release (the sharing half).
- Reviewing the base crops. Dan, 2026-11-22: the base set stays as it is.
- Any change to the labeling tool.

## Anti-goals

- A label no person gave: a model guess nobody answered never enters a snapshot.
- A fish left unboxed in a detector frame: it would teach the detector that the fish is background.
- A volunteer's name or email in any file. Accounts appear as numeric ids only.
- A snapshot that changes after it is written, or a base crop changed in S3.
- The live dashboard slowed: the build reads in a read-only transaction and never takes locks a
  reader waits on.

## Constraints

- Runs as a job in the dashboard image's jobs Lambda (512 MB, 900 s, in the VPC), like
  `archive_labels`. No new Python dependency.
- Storage through `common.storage` and URLs from the environment, as everywhere in this repo.
- Staging only for now: `ai-wahoobay-prod` is defined but not deployed.
- Depends on wahoobay/ai#52 (class renames): class folders use the renamed names, and the base crops'
  two old folders are renamed through `common.class_renames` when assembled.

## Approaches

- **A. A job on AWS that reads the database (chosen).** Same shape as the label archive: it has the
  database, the frames and the one resolution rule. The catch is Lambda's 15-minute limit, ample for
  179 frames, which would need a container task once Roy has done thousands.
- **B. Build from the label archive's copies in S3.** No new job, but the archive covers listed
  accounts only, does not record which frames are complete (the detector needs exactly that), and
  the resolution rule would exist twice.
- **C. A script run by hand against the database on the GPU host.** Dropped: it runs where the live
  stream is served, and nothing about it is repeatable.

## Design

### 1. The job: `build_dataset`

A job in `app.jobs` (`{"job": "build_dataset"}`), invoked by hand; no schedule (a later infra
change can add one). New module `services/dashboard/app/verified_dataset.py`.

- **Whose answers.** Only the accounts in `DATASET_ACCOUNTS` (JSON list of usernames; infra sets it to
  the label archive's list, today Roy's account). Everyone else's answers are dropped before the
  resolution rule runs. Dan, 2026-11-18: only Roy's staging answers are useful; the admin's and the
  booth account's are tests.
- **Which answer counts.** `review_labels.resolve_all` over the remaining answers. Statuses `single`,
  `agreed` and `adjudicated` count; `disputed` is held back. Boxes whose answers name a species still
  waiting for approval are held back (`exports.hide_pending_species`). Confidence at least `probable`
  (all of Roy's are `certain`). "Mark the rest" answers count like any other (Dan, 2026-11-22); every
  record keeps its `label_source` so training can tell them apart.
- **The box.** The resolved box (`Resolved.bbox`), else the model's. Drawn boxes
  (`review_added_boxes`, not removed) count as `single`.
- **Real footage only.** Events with `model_version = 'sample'` (the seed job's synthetic rows) are
  left out whatever the account.
- **No cutoff parameter.** The database is not append-only (drawn boxes are edited and removed, a
  frame can be marked incomplete again, an admin removing a reviewer-added species rewrites
  answers), so an earlier state cannot be rebuilt from it. A snapshot is the record of the state
  it was built from, and snapshots are kept for good (the job may add files, never delete them).

### 2. What a snapshot holds

`<DATASET_URL>/<stamp>/` (infra: `s3://platform-wahoobay-models/datasets/verified/<env>/`), and
`<DATASET_URL>/latest.json` naming the newest. A new snapshot is written only when its content hash
differs from the latest one; an unchanged run writes nothing and says so.

```
snapshot.json                    provenance: snapshot id, git sha, newest answer and drawn-box ids, content hash
manifest.json                    one record per box, and counts
report.txt                       every box and frame held back, with the reason
classifier/<Accepted_name>/ev<event_id>.jpg | add<box_id>.jpg
negatives/ev<event_id>.jpg       "no animal" answers
detector/images/<saved_frame_id>.jpg
detector/annotations.coco.json   complete frames only
```

- **Classifier crops.** For each verified box whose outcome is a species: the box plus 5% padding,
  clamped to the frame, not resized, JPEG, in a folder named after the WoRMS accepted name (spaces
  as underscores). The snapshot knows nothing of any model's class list (the jobs Lambda has none,
  and a later model may have other classes); the assembler maps names to training folders. "Can't
  identify" answers make no crop. "No animal" answers are cropped into `negatives/` for the detector
  project to use or ignore; they never become a classifier class.
- **Detector frames.** A frame completed in the tool (`review_frames`) in which every model box
  (detection events with the frame's exact camera, `frame_id` and `ts`) has a counting answer and
  none is disputed or held back. Its annotations: every box whose outcome is a species or "can't
  identify", plus its drawn boxes. "No animal" boxes are left out, so they count as background. A
  frame that fails any of this is held back whole, with the reason.
- **COCO.** Standard COCO: `images` (`id` = `saved_frame_id`, `file_name`, `width`, `height`,
  `date_captured`, `camera`); `annotations` (`bbox` as `[x, y, w, h]`, `area`, `iscrowd` 0,
  `category_id`, `box_id` = the manifest's, `bbox_source` = model, corrected or drawn,
  `label_source`); `categories`: one per species (with its AphiaID) plus `unidentified animal`.
- **Manifest record** (one per box, in or held back): `box_id` (`ev<id>` or `add<id>`),
  `saved_frame_id`, `camera`, `ts` (UTC), `local_date` (the site's zone), `bbox` (`[x1, y1, x2, y2]`),
  `bbox_source`, `outcome`, `accepted_name`, `aphia_id`, `life_stage`, `confidence`, `status`,
  `label_source`, `user_ids`, the model's guess and score, `crop`, `frame`, `held_back` (reason or
  null).
- **Deterministic.** Records and files in a fixed order; the content hash is a SHA-256 over every
  file's path and hash in sorted order (the archive's `content_hash`), every file except
  `snapshot.json`, which carries what changes from run to run.
- **Read-only.** All reads in one `READ ONLY` transaction (`label_archive.read_only_transaction`).

### 3. Assembling the classifier's training folder: `scripts/assemble_training_set.py`

Run where training runs:
`assemble_training_set.py --base <fine_tune.tar or dir> --snapshot <dir or URL> --out data/fine_tune`.
It builds a fresh directory and never edits its inputs:

1. The base crops, with the folders and manifest `label`s of renamed classes moved to their new
   names (`common.class_renames`). The base folders are exactly the dgx-2026-10-24 class list (223,
   checked 2026-11-22), so after the renames they are the r1 list.
2. Each snapshot species mapped to a base folder: the folder whose name, as a binomial, has the same
   accepted name in `assets/taxonomy.csv` (the review catalogue's rule). Its crops are copied to
   `<out>/<folder>/review_ev<id>.jpg` (or
   `review_add<id>.jpg`), with manifest records in the existing format: `drive_id` =
   `review:<camera>:<local date>`, `source_kind` = `review:video`, `frame_index` = seconds since
   local midnight. The `by_source` and `temporal_within_source` splits then treat each camera-day
   as one clip, unchanged.
3. `_taxonomy.json` from the base, and `_review_merge.json` naming the snapshot and the counts.

A species with no base folder (none of Roy's 26 today) is reported and skipped, unless
`--allow-new-classes` gives it a folder of its own. Negatives are never copied. The September spec's
in-place merge and unmerge are replaced by building a new folder: the base tar never changes, so
undoing is rebuilding without `--snapshot`.

### 4. Infra (wahoobay/infra)

The jobs Lambda gets `DATASET_URL` and `DATASET_ACCOUNTS`, and read and put (never delete) on
`datasets/verified/<env>/*` in the models bucket, like the archive's grants.

## Left to the builder

- How frames are read and crops cut within 512 MB and 900 s (one decode per frame for all its boxes),
  and whether to raise the Lambda's memory instead.
- The report's wording and layout, and the exact COCO category ids.
- Test fixtures (the existing `review_fixtures` seed frames, boxes, users and verdicts).

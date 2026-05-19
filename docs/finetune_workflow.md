# Model training & fine-tuning workflow

End-to-end playbook from the Wahoo Bay Google Sheet to a deployed v2
classifier checkpoint, including how reviewer corrections fold back
into the next iteration.

The current production classifier is **DINOv2 ViT-S/14 trained from
random init** on a 223-class dataset assembled from three sources
(community Drive clips, iNaturalist photos, reviewer corrections — in
that order of provenance maturity). The legacy Fishial ArcFace + KNN
classifier remains bundled as the one-env-var rollback path.

---

## The big picture

```
┌────────────────────────────────────────────────────────────────────┐
│ DATASET ASSEMBLY (scripts/)                                        │
│                                                                    │
│   species.xlsx ────────────────────────────────────────┐           │
│   (Wahoo Bay sheet,                                    │           │
│    auto-refreshed                                      ▼           │
│    by sheet poller)             build_species_manifest.py          │
│                                       │                            │
│                                       ▼                            │
│                              species_manifest.json                 │
│                                       │                            │
│             ┌─────────────────────────┼──────────────────────┐     │
│             ▼                         ▼                      ▼     │
│   download_species_clips.py   enrich_taxonomy_from_worms.py        │
│       (Drive clips)                  │                             │
│             │                        ▼                             │
│             ▼                  taxonomy.csv                        │
│   community_clips/*.mp4                                            │
│             │                                                      │
│             │     download_inat_photos.py                          │
│             │       (iNat permissively-licensed photos)            │
│             │                  │                                   │
│             │                  ▼                                   │
│             │            inat_photos/*.jpg                         │
│             │            inat_manifest.json                        │
│             ▼                  │                                   │
│   build_finetune_dataset_from_clips.py ◀────────────────────┘      │
│       (YOLO-crop each clip + each photo → per-species dirs)        │
│             │                                                      │
│             ▼                                                      │
│   data/fine_tune/{train,val,test}/<species>/<crop>.jpg             │
│             │                                                      │
└─────────────┼──────────────────────────────────────────────────────┘
              │
              ▼
┌────────────────────────────────────────────────────────────────────┐
│ TRAINING (scripts/train_baseline.py)                              │
│                                                                    │
│   • DINOv2 ViT-S/14 from random init                               │
│   • 3 split modes: stratified / temporal_within_source / by_source │
│   • bf16, channels-last, mixup, RandAugment, drop_path             │
│                                                                    │
│             ▼                                                      │
│   data/baseline_runs/<run_id>/                                     │
│       checkpoint_best.pt                                           │
│       classes.json                                                 │
│       config.json                                                  │
│       metrics.json                                                 │
└────────────────────────┬───────────────────────────────────────────┘
                         │
                         ▼
┌────────────────────────────────────────────────────────────────────┐
│ DEPLOYMENT                                                         │
│   cp data/baseline_runs/<best>/{checkpoint_best.pt,classes.json,*} │
│      data/models/classifier_v2/                                    │
│   restart workers — v2 picks up the new files at boot              │
└────────────────────────┬───────────────────────────────────────────┘
                         │
                         ▼
┌────────────────────────────────────────────────────────────────────┐
│ REVIEWER FEEDBACK LOOP                                             │
│                                                                    │
│   detection_events (new model)                                     │
│        │                                                           │
│        ▼                                                           │
│   reviewer corrections via the dashboard                           │
│        │                                                           │
│        ▼                                                           │
│   detection_corrections rows accumulate                            │
│        │                                                           │
│        ▼                                                           │
│   build_finetuning_dataset.py reads labeled_corrections.csv        │
│        │                                                           │
│        ▼                                                           │
│   next training run: same recipe, more data, better accuracy       │
└────────────────────────────────────────────────────────────────────┘
```

---

## Where data comes from

Three streams contribute crops to the training set, with increasingly
authoritative provenance:

### 1. Community Drive clips (the foundation)

The "Species Log" tab of `assets/species.xlsx` lists every species the
Wahoo Bay team has identified, with hyperlinks to Google Drive videos
or stills. As of last refresh: ~302 rows / ~235 unique binomials, of
which ~140 records have Drive links (~133 unique species with at least
one clip).

These clips are filmed by Wahoo Bay program participants under a
program-internal use grant — they stay private, but we use them as
training inputs.

### 2. iNaturalist photos (the supplement)

iNat has photographs of most of the species we care about with
explicit Creative Commons licences. We harvest permissively-licensed
photos to thicken the long tail (species with few or zero Drive
clips).

Licence filter: `cc0`, `cc-by`, `cc-by-nc`, `cc-by-sa`, `cc-by-nc-sa`,
`cc-by-nd`, `cc-by-nc-nd` — anything except All Rights Reserved. The
`-nc` and `-nd` clauses are the relevant ones for downstream
constraints; see [`assets/LICENSES_AND_CREDITS.md`](../assets/LICENSES_AND_CREDITS.md).

### 3. Reviewer corrections (the feedback)

Once v2 is in production, every reviewer correction becomes a new
training example for the next iteration. The crop is taken from the
saved frame's bbox; the label is the reviewer's `corrected_name`.
"Not a fish" corrections become hard negatives for future detector
retraining.

---

## Phase A: assembling the dataset (one-time per training round)

Each script below is idempotent — re-running picks up where it left
off without re-downloading existing files. Run them in order; each
takes 5-60 minutes depending on which species are new.

### A1. Parse the spreadsheet

```bash
python scripts/build_species_manifest.py
# → assets/species_manifest.json + assets/species_manifest.csv
```

Reads `assets/species.xlsx` (which is refreshed daily by the
dashboard's sheet poller). For each row:

- Parses the "Scientific Name" cell into `genus` + `species`.
- Pulls the `family` from its own column.
- Walks every cell looking for `=HYPERLINK(...)` formulas pointing at
  Google Drive (`/file/d/{id}/...`) and extracts the 33-char Drive
  file IDs.
- Emits `display_label` (plain) and `display_label_html` (italicised
  parenthetical) — e.g. *Blue Angelfish (H. bermudensis)*.

The script does NOT download anything — it's purely a parsing pass.

### A2. Download the Drive clips

```bash
python scripts/download_species_clips.py
# → assets/community_clips/<species>/<file>.mp4 (or .jpg, .png, ...)
```

Anonymously downloads each Drive file ID from the manifest. Handles
Drive's "virus scan" interstitial that fires for files >100 MB or
video files: parses the hidden form (`id`, `export`, `confirm=t`,
`uuid`) and POSTs to `drive.usercontent.google.com/download` to get
the actual bytes. Sniffs MIME on the response to pick the right
extension.

Throttle: 1 request/sec by default. Resumes on partial downloads.

### A3. Harvest iNaturalist photos

```bash
python scripts/download_inat_photos.py
# → assets/inat_photos/<binomial>/<photo_id>.jpg
# → assets/inat_manifest.json (per-photo attribution + licence)
```

For each binomial in `species_manifest.json`, queries
`https://api.inaturalist.org/v1/observations` with:

- `photo_license=cc0,cc-by,cc-by-nc,cc-by-sa,cc-by-nc-sa,cc-by-nd,cc-by-nc-nd`
- Up to N photos per species (default 30; raise for thin classes).
- Photo URL size swapped from `square` to `large`.

Rate-limited to 60 req/min per iNat's published guideline. Manifest
records `observer`, `license`, `attribution`, `observed_on`,
`original_url`, `local_path` per photo so the per-photo licence is
attached to each training example.

### A4. Enrich with WoRMS taxonomy

```bash
python scripts/enrich_taxonomy_from_worms.py
# → assets/taxonomy.csv
```

Batches binomials through the World Register of Marine Species
`/AphiaRecordsByMatchNames` endpoint (50 per batch, `marine_only=false`).
Emits one row per species with columns
`species, genus, family, order, class, phylum, kingdom, lowest`. When
WoRMS only resolves to family (or lower confidence), the lowest known
rank is repeated down to the species column so every row is complete.

This is the source of truth for any future **taxonomy-aware loss**
(penalize confusions across distant ranks more than nearby ones — not
yet implemented in train_baseline.py).

### A5. YOLO-crop everything into per-species directories

```bash
python scripts/build_finetune_dataset_from_clips.py
# → data/fine_tune/{train,val,test}/<species_slug>/<crop>.jpg
```

The core dataset-builder. For every source (Drive clip OR iNat photo):

- **Image source**: single YOLO inference, write each detection's bbox
  crop as a JPEG.
- **Video source**: sample at 2 Hz, run YOLO on each sampled frame,
  write each detection's crop. Cap 120 crops per clip so a 10-minute
  video doesn't dominate a class.

Each crop is tagged with `(species, source_kind, source_id,
frame_idx)` in a sidecar JSON so we can group-by source at split time
and avoid the "same fish in train and val" trap.

The detector is the same YOLO v26 the production worker uses — so the
training distribution matches inference exactly.

### A6. (Optional) layer in reviewer corrections

Once reviewers have submitted corrections against v2's outputs:

```bash
curl -s "http://127.0.0.1:18080/api/export/labeled_corrections.csv" \
     -o labeled_corrections.csv

python scripts/build_finetuning_dataset.py \
  --corrections labeled_corrections.csv \
  --frames-root frames/ \
  --out data/fine_tune_v2/ \
  --merge-into data/fine_tune/   # layer corrections on top of the auto-crops
```

The script reads the CSV, filters by reviewer confidence, optionally
drops not-a-fish corrections (or routes them to `negatives/` for
future detector fine-tuning), and writes the corrected-label crops
into the per-species directories alongside the auto-generated ones.

---

## Phase B: training (one run = one v2 checkpoint)

```bash
python scripts/train_baseline.py \
  --data-root data/fine_tune \
  --run-id baseline_$(date +%Y%m%dT%H%M%S) \
  --arch vit_small_patch14_dinov2 \
  --epochs 60 \
  --batch-size 32 \
  --img-size 224 \
  --split-mode temporal_within_source \
  --device cuda:0
```

Output:

```
data/baseline_runs/baseline_20260518T...
├── checkpoint_best.pt        # best macro-F1 epoch (loaded by classifier_v2.py)
├── checkpoint_last.pt        # final epoch (for sanity / resume)
├── classes.json              # ["Abudefduf_saxatilis", "Acanthurus_chirurgus", ...]
├── config.json               # arch, hyperparams, split mode, seed
├── metrics.json              # best macro_f1, top1, top5, weighted_f1
├── train_history.json        # per-epoch loss + metrics
└── classification_report.txt # sklearn-style per-class P/R/F1
```

### Training recipe

- **Architecture**: timm `vit_small_patch14_dinov2` (22M params,
  patch=14, embed_dim=384, depth=12). Random init — **no DINOv2
  pretrained weights**. This matters for the licence picture: the
  trained checkpoint inherits no constraints from Meta's DINOv2
  release.
- **Image size**: 224×224 (native DINOv2 is 518×518; 224 is faster and
  standard ViT-S).
- **Optimiser**: AdamW with cosine schedule, 5-epoch warmup, base lr 3e-4,
  weight decay 0.05.
- **Augmentation**: RandomResizedCrop (0.7-1.0 scale), HFlip,
  RandAugment (2 ops, magnitude 9), ColorJitter (0.2/0.2/0.2/0.05),
  RandomErasing (p=0.1).
- **Regularisation**: label smoothing 0.1, mixup α=0.2, stochastic
  depth (drop_path) 0.1.
- **AMP**: bf16 autocast (preferred on H100/H200 — same throughput as
  fp16 but the dynamic range of fp32).
- **Sampler**: WeightedRandomSampler with weight 1/sqrt(class_count)
  to prevent long-tail starvation when one species dominates.

### Three split modes — pick the right one for the right question

The script's `--split-mode` flag is the most consequential knob.

| Mode | What it does | When it's the right answer |
|---|---|---|
| `stratified` | Random split within each species across all sources. Same clip's frames can land in both train and val. | Optimistic upper bound — gives you the model's ceiling under the assumption that train + val come from the same distribution. Most ML-paper benchmarks default to this. |
| `temporal_within_source` | Within each source clip, the earliest 80 % of frames go to train, latest 20 % to val. Different frames of the same clip end up on different splits. | **The closest stand-in for "what does this model do on tomorrow's footage from the same camera?"** Recommended for the headline metric you quote externally. |
| `by_source` | Whole sources land entirely in train or val. A clip never appears in both. | The cruel honest test: brand-new clips, no overlap. Reveals whether the model actually generalises across recording sessions vs. memorises lighting / context. |

Run all three; report all three. Last full sweep on the iNat-supplemented
dataset (May-17 checkpoint):

| split | best ep | macro F1 | top-1 | top-5 |
|---|---|---|---|---|
| stratified | 51 | 0.349 | 0.516 | 0.673 |
| temporal_within_source | 40 | **0.495** | **0.811** | 0.932 |
| by_source | 41 | 0.083 | 0.070 | 0.206 |

The by-source number is the floor; the temporal-within-source number
is the one we quote externally.

### Hardware

The current run takes about 7 minutes per split mode on a single
H200 (60 epochs, ~13k images, batch 32). All three sweeps in ~25
minutes. Smaller GPUs are fine — DINOv2 ViT-S/14 fits comfortably in
8 GB.

---

## Phase C: deployment (one-flag flip)

Drop the new artifacts into the `classifier_v2/` directory the worker
reads, then restart the workers.

```bash
# 1. Stage the new checkpoint
cp data/baseline_runs/<best_run>/checkpoint_best.pt  data/models/classifier_v2/
cp data/baseline_runs/<best_run>/classes.json         data/models/classifier_v2/
cp data/baseline_runs/<best_run>/config.json          data/models/classifier_v2/
cp data/baseline_runs/<best_run>/metrics.json         data/models/classifier_v2/

# 2. Restart the workers (see docs/operations.md "Restarting one service")
for port in 8081 8083; do
  ss -ltnp 2>/dev/null | grep ":$port" | grep -oP 'pid=\K\d+' | xargs -r kill -TERM
done
sleep 5
nohup ./scripts/dev/run_worker.sh      > logs/app/worker.log      2>&1 &
nohup ./scripts/dev/run_worker_pier.sh > logs/app/worker_pier.log 2>&1 &

# 3. Verify the new classifier_sha256 is live
sleep 30  # let torch.compile warmup finish
curl -s http://127.0.0.1:18080/api/provenance/current | python3 -m json.tool
```

The new `classifier_sha256` should appear on every detection_events
row written from then on. Old rows keep their old hash, so SQL filters
can easily separate pre-/post-cutover data.

### Rollback

If the new checkpoint underperforms in live use:

```bash
# Option 1: revert just the classifier_v2 files from git or backup
# Option 2: roll all the way back to Fishial
echo "CLASSIFIER_BACKEND=fishial" >> .env  # (or edit the existing line)
# Restart workers as above
```

The legacy Fishial bundle stays in `data/models/classifier_v0_10_2/`
permanently — it's the "known good" fallback.

---

## Phase D: validation (the eval harness)

```bash
make eval
# → eval/reports/YYYY-MM-DDTHH-MM-SSZ/{metrics.json,report.md,provenance.json}
```

Replays `eval/manifest.json` clips through the current production
checkpoints. Each scalar is reported with a 95 % bootstrap CI
(B=2000).

**The eval harness needs a labelled clip set to be useful.** As of
documentation date the manifest is empty; populating it is on the
roadmap.

For comparing checkpoints: store the eval reports in git so the
history is the regression-test record.

A reasonable first eval set:

1. Pick 10-30 short clips from `frames/` covering a range of species
   and lighting conditions.
2. Hand-label each with COCO-format ground truth (the
   labeled-corrections export is a starting point).
3. Add entries to `eval/manifest.json`.
4. `make eval` — the metrics will be honest.

---

## How much reviewer data is enough

Rule of thumb for fine-tuning a vision model on a 223-class problem:

| Per species | Effect |
|---|---|
| **< 10 examples** | Useless for fine-tuning that species. The classifier won't learn the new label well. |
| **10-50 examples** | Noticeable improvement on common-confusion neighbours. Risk of overfitting on small classes. |
| **50-200 examples** | Solid territory. Most reviewer-feedback rounds should target this band. |
| **> 200 examples** | Diminishing returns; reallocate reviewer hours to thinner species. |

For ~50 actively-confused species, that's **~5,000 corrections total**
to make a substantial improvement. At ~30 corrections per reviewer-hour,
that's ~150 hours of reviewer time. Spread across several volunteers
over a season, achievable.

The dashboard's correction-stats panel surfaces how many corrections
have been collected per reviewer + per species, so it's easy to spot
which species still need attention.

---

## Inter-rater reliability

Once corrections come from more than one reviewer, **two-rater
agreement is required** for any subset that ships to a publication. A
clean protocol:

1. After every M corrections, sample ~10 % of recent events into a
   "blind" pool — events that have been corrected by exactly one
   reviewer.
2. A second reviewer corrects those independently (no view of the
   first correction).
3. Compute Cohen's κ (binary "fish vs not") and Krippendorff's α
   (multi-class species).
4. κ ≥ 0.75 / α ≥ 0.7 is the working threshold for "trustworthy" in
   ecology literature.

The dashboard does not yet enforce or track this — it's a process
decision the reviewer team needs to make. Documented as deferred work
in `PLAN.md`.

---

## Common pitfalls

### "The from-scratch baseline numbers look low"

The from-scratch ViT-S/14 has no foundation-model priors — every
parameter learns from our ~13k crops. For a 223-class problem, that's
~58 examples per class on average (long-tailed: some have thousands,
some have ten). The temporal-within-source 0.81 top-1 is actually
surprisingly good given that math.

If you want better numbers, the most accessible move is to **start
from DINOv2 pretrained weights** instead of random init — pass
`--pretrained` to `train_baseline.py` (and adjust the licence picture
accordingly: the resulting checkpoint inherits Meta's licence terms).

### "Reviewer A and B label things differently"

Run inter-rater reliability (above). If κ is low for specific species
pairs (e.g., *Acanthurus chirurgus* vs *Acanthurus coeruleus*), that's
informative — those species genuinely look similar and need higher-
resolution training data or a feature-level intervention, not more
labels. A taxonomy-aware loss using `assets/taxonomy.csv` would let the
model express "I know it's an Acanthurus but I'm not sure which" with
less penalty than getting the family wrong.

### "All my crops are tiny"

The detector returns bbox in source-image coordinates. Some saved
frames are 1920×1080; some are 1280×720 (after MJPEG resolution
change); old historical events may differ. The crop scripts preserve
whatever size the bbox encloses — they do NOT resize. The training
pipeline's `ImageFolder` + `transforms.RandomResizedCrop(224)`
resizes at load time. If you're inspecting raw crop dirs, expect
heterogeneous sizes.

### "Same fish, hundreds of crops"

`build_finetune_dataset_from_clips.py` caps per-video crops at 120,
which mostly handles this for video sources. For reviewer corrections
(`build_finetuning_dataset.py`) pass `--dedupe-tracks` — one fish
lingering for 100 frames should not be 100 near-duplicate examples.
Use `track_id` as the dedup key.

### "Some species have 200, others have 3"

The `WeightedRandomSampler` in `train_baseline.py` already
class-balances on the fly during training (weight ∝ 1/√count). The
thin species still need more data — there's no shortcut around it.
The script logs a warning naming species with <10 examples.

### "I want detector fine-tuning, not classifier"

The current workflow trains only the classifier head — the detector
(YOLO v26) is unchanged. To fine-tune the detector with reviewer
"Not a fish" corrections as hard negatives, you'd need to:

1. Export not-a-fish corrections as YOLO-format empty-label images
   centred on the rejected bbox.
2. Mix into a YOLO fine-tune dataset alongside positive-labeled
   reviewer corrections (re-formatted from per-species classifier
   crops to YOLO bbox-format).
3. Run `ultralytics` fine-tune from the existing v26 weights.

A `--format yolo` flag for `build_finetuning_dataset.py` is a
1-day extension when reviewer not-a-fish data has accumulated to a
useful volume. Not yet implemented.

---

## Reproducibility checklist

For any training run that gets published or compared against in the
project, capture:

1. **Dataset manifest hash** — `sha256sum data/fine_tune/manifest.json`
2. **Training config** — `data/baseline_runs/<run>/config.json`
3. **Random seed** — recorded in config.json (default 42)
4. **Git SHA** — `git rev-parse HEAD` at training time, capture in the
   run dir
5. **Eval report SHA** — `data/baseline_runs/<run>/metrics.json`
6. **Provenance fingerprint at deploy time** — `/api/provenance/current`
   when the worker has loaded the new checkpoint

Stash all six in a commit message or a dated decision log so the run
can be regenerated end-to-end.

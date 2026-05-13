# Fine-tuning workflow

Step-by-step playbook from a human reviewer's first correction to a
fine-tuned classifier checkpoint going live.

## Why fine-tune

The Fishial.ai stock classifier (DINOv2 + ViT, 866 classes) is trained
on a globally-mixed dataset with limited Atlantic-reef coverage. On
Wahoo Bay species we measure mean top-1 cosine similarity in the
0.30–0.50 range and confident misidentifications are common (a Sergeant
Major sometimes labelled as a Mediterranean species, a Bermuda Chub as
something Pacific). The pipeline itself is correct; the model just
hasn't seen enough Wahoo Bay fish.

Fine-tuning on a Wahoo Bay-specific dataset of human-corrected crops
fixes this. The pipeline already produces every component we need —
the bottleneck is reviewer hours, not infrastructure.

## The five-step loop

```
   ┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
   │ 1. Reviewer     │    │ 2. Export       │    │ 3. Crop         │
   │    corrects an  │───▶│    labeled_     │───▶│    bboxes into  │
   │    event        │    │    corrections  │    │    per-species  │
   │                 │    │    .csv         │    │    train/val/   │
   │                 │    │                 │    │    test dirs    │
   └─────────────────┘    └─────────────────┘    └─────────┬───────┘
                                                            │
   ┌─────────────────┐    ┌─────────────────┐    ┌──────────▼──────┐
   │ 5. Switch       │    │ 4. Validate     │    │    Train        │
   │    worker to    │◀───│    against      │◀───│    classifier   │
   │    new          │    │    held-out     │    │    head         │
   │    checkpoint   │    │    eval clips   │    │                 │
   └─────────────────┘    └─────────────────┘    └─────────────────┘
```

Steps 1–3 are routine and repeatable. Step 4 needs the eval harness
to be populated with labelled clips first. Step 5 is a config flip on
the worker.

## Step 1 — Reviewers correct events

The dashboard's "Recent events" panel has a `correct` button on each
event row. Click → an inline form opens with:

- **Species name** field (free-text Latin binomial).
- **Confidence**: certain / probable / uncertain.
- **Not a fish** checkbox — for false-positive detections (e.g. the
  detector found a piece of biofouling).
- **Notes** (free text).

The reviewer's email is captured once (in localStorage) and tagged on
every correction they submit. Submission requires a write token (see
`docs/operations.md` for where the token lives) so anonymous viewers
can't pollute the dataset.

Each correction writes one row to `detection_corrections` linked to
the original `event_id`. From there it's joined to:

- The event's `bbox` and `frame_id`
- The saved-frame `image_path` (if the frame was saved to disk)
- The model that produced the original prediction (provenance columns)

That join is what the labeled-corrections export consumes.

## Step 2 — Export labeled corrections

```bash
curl -s "http://127.0.0.1:18080/api/export/labeled_corrections.csv" \
     -o labeled_corrections.csv
```

This is one of 13 export endpoints documented in
`docs/data_pipeline.md`. The CSV columns are:

| Column | What |
|---|---|
| `correction_id`, `created_at`, `reviewer`, `confidence`, `notes` | Who said what when |
| `corrected_name`, `corrected_species_id`, `not_a_fish` | The human label |
| `event_ts`, `source_name`, `frame_id`, `track_id`, `bbox`, `det_conf` | Original detection context |
| `original_name`, `original_species_id`, `original_accuracy`, `original_topk` | What the model said |
| `frame_image_path`, `frame_coco_path` | Where the saved JPG and COCO sidecar live on disk |
| `model_version`, `detector_sha256`, `classifier_sha256`, `config_hash`, `pipeline_git_sha` | Full provenance of the original prediction |

If you don't have public access to the dashboard, query the DB
directly:

```bash
psql -h 127.0.0.1 -U wahoobay -d wahoobay \
  -c "\copy (SELECT … FROM detection_corrections JOIN detection_events …) TO 'labeled_corrections.csv' CSV HEADER"
```

The exact SELECT is in `services/dashboard/app/exports.py`,
function `export_labeled_corrections`.

## Step 3 — Build the training dataset

```bash
python scripts/build_finetuning_dataset.py \
  --corrections labeled_corrections.csv \
  --frames-root /raid/scratch/dzimmerman2021/wahoobay/frames \
  --out finetune_v1 \
  --min-confidence probable \
  --include-not-a-fish \
  --dedupe-tracks \
  --max-per-species 200
```

The script:

1. Reads the CSV, filters by reviewer confidence, optionally drops
   not-a-fish corrections (or sends them to `negatives/`), and
   optionally dedupes by `track_id` (so one fish lingering for 100
   frames doesn't dominate).
2. Resolves `frame_image_path` (or derives the JPG sibling of
   `frame_coco_path` if the path column is empty).
3. Crops each correction's bbox with a 5 % padding margin.
4. Writes to `out/<split>/<species_slug>/<corrXXXX_eventYYYY>.jpg`,
   split deterministically (default 80/10/10) by RNG seed.
5. Writes a `manifest.json` capturing every parameter + the corrections
   CSV's MD5 (so any future re-run is reproducible).
6. Writes a `README.md` next to the data so a downstream user can read
   what's in the directory without consulting our docs.

Output structure:

```
finetune_v1/
├── train/
│   ├── Abudefduf_saxatilis/
│   │   ├── corr0042_event12345.jpg
│   │   └── ...
│   ├── Lutjanus_griseus/
│   └── ...
├── val/
│   └── (same shape)
├── test/
│   └── (same shape)
├── negatives/                 # if --include-not-a-fish
│   └── corrXXXX_eventYYYY.jpg
├── manifest.json
└── README.md
```

### Filter knobs

| Flag | Default | When to use |
|---|---|---|
| `--min-confidence` | `probable` | Bump to `certain` for the most rigorous training set; drop to `uncertain` to maximise data when starved. |
| `--include-not-a-fish` | off | Include for joint detector + classifier fine-tuning, or to train a "not a fish" rejection head. |
| `--dedupe-tracks` | off | On by default for any *classification* training split — one fish lingering should not be a hundred near-duplicate examples. |
| `--max-per-species N` | unset | Class-balance the dataset. Important if reviewers preferentially correct one species. |
| `--train-frac` / `--val-frac` | 0.8 / 0.1 | Default 80/10/10 split (test = remainder). |
| `--seed` | 42 | Deterministic split; record in the manifest. |
| `--pad-pct` | 0.05 | 5 % margin around the bbox for classifier context. |

### Sanity check

The script prints a summary at the end and explicitly flags any species
with **fewer than 10 crops** as too thin for reliable fine-tuning, with
a per-species "needs more reviewer corrections" list. Use this to
prioritise reviewer attention.

## Step 4 — Validate against held-out clips

Once a fine-tuned checkpoint exists, run the eval harness:

```bash
make eval
```

This walks `eval/manifest.json`, replays each clip through the current
production checkpoints, and writes a dated report to
`eval/reports/YYYY-MM-DDTHH-MM-SSZ/`:

- `metrics.json` — raw numbers (detector P/R/F1, classifier top-1/3/5,
  per-class P/R/F1, latency p50/p95/p99).
- `report.md` — human-readable summary.
- `provenance.json` — full fingerprint so the run is reproducible.

Each scalar is reported with a 95 % bootstrap CI (B=2000).

**The eval harness needs a labelled clip set to be useful.** As of
documentation date the manifest is empty; the `eval/` directory has a
README explaining what to put there.

A reasonable first eval set:

1. Pick 10–30 short clips from `frames/` covering a range of species
   and lighting conditions.
2. Hand-label each with COCO-format ground truth (or use the
   labeled-corrections export as a starting point and fill in the
   gaps).
3. Add entries to `eval/manifest.json`.
4. `make eval` — the metrics will be honest.

For comparing checkpoints: store the eval reports in git so the
history is the regression-test record.

## Step 5 — Deploy a new checkpoint

When the fine-tuned classifier passes eval (whatever "passes" means in
the project's policy — typically: per-class precision ≥ X for the top
N most-common species), put it into production:

1. Copy the new TorchScript bundle to
   `data/models/classifier_v0_<NN>/model.pt`.
2. Update the worker config (env var `CLASSIFIER_PATH`) to point at it.
3. Restart the worker.
4. The provenance columns on every event written from that point will
   record the new SHA256, so it's easy to A/B compare event quality
   pre- and post-deployment via SQL:

```sql
SELECT classifier_sha256, count(*), avg(best_accuracy)::real
  FROM detection_events
 WHERE ts > NOW() - INTERVAL '7 days'
 GROUP BY 1
 ORDER BY count DESC;
```

If the new checkpoint underperforms, switch the env back to the
previous SHA and restart — instant rollback.

## How much data is enough

Rule of thumb for fine-tuning a vision foundation model with strong
priors (DINOv2 has them):

- **<10 examples per species**: too thin to fine-tune reliably; useful
  only for prompt tuning or a few-shot k-NN over the existing
  embeddings.
- **10–100 examples per species**: noticeable improvement on the
  fine-tuned species; risk of catastrophic forgetting on others
  without LoRA / careful learning-rate scheduling.
- **100–500 examples per species**: solid full fine-tune territory.
- **>500 examples per species**: diminishing returns; spend the
  reviewer hours on rare species instead.

For ~20 common Wahoo Bay species, the realistic target is
**~2,000–10,000 corrections total** — depending on how rigorous you
want the model to be. At a typical curation rate of ~30 corrections per
reviewer-hour, that's 60–300 hours of reviewer time. Not free, but
absolutely feasible with a few volunteers over a season.

## Inter-rater reliability

Once corrections come from more than one reviewer, **two-rater
agreement is required** for any subset that ships to a publication. A
clean protocol:

1. After every M corrections, sample ~10 % of recent events into a
   "blind" pool — events that have been corrected by exactly one
   reviewer.
2. A second reviewer corrects those independently (no view of the first
   correction).
3. Compute Cohen's κ (binary "fish vs not") and Krippendorff's α
   (multi-class species).
4. κ ≥ 0.75 / α ≥ 0.7 is the working threshold for "trustworthy" in
   ecology literature.

The dashboard does not yet enforce or track this — it's a process
decision the reviewer team needs to make. Documented as deferred work
in `PLAN.md`.

## Common pitfalls

### "Reviewer A and B label things differently"

Run inter-rater reliability (above). If κ is low for specific species
pairs (e.g., Acanthurus chirurgus vs Acanthurus coeruleus), that's
informative — those species genuinely look similar and need higher-
resolution training data or a feature-level intervention, not more
labels.

### "All my crops are tiny"

The detector returns bbox in source-image coordinates. Some saved
frames are 1920×1080; some are 1280×720 (after MJPEG resolution change);
old historical events may differ. The crop script preserves whatever
size the bbox encloses — it does NOT resize. If your training pipeline
needs uniform input, resize / pad as a post-process before training,
not in `build_finetuning_dataset.py`.

### "Same fish, hundreds of crops"

Use `--dedupe-tracks`. Or filter on `track_id` to keep only the
median-confidence frame per track if you want exactly-one-per-fish.

### "Some species have 200, others have 3"

Use `--max-per-species` to cap the over-represented species. The
under-represented species still need more corrections — there's no
shortcut around it. The script's "thin species" warning lists exactly
which.

### "I want detector fine-tuning, not classifier"

The current script outputs classification-style data (per-species
crops). For YOLO/COCO format with full bbox annotations, the input is
the same `labeled_corrections.csv` but the output structure is
different (images/ + labels/ with YOLO-format text files, or full
COCO JSON). Adding a `--format coco` flag to the script is a 1-day
extension when needed.

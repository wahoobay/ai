# Wahoo Bay live fish-ID data — README

This directory contains data exports from the Wahoo Bay real-time
fish-identification pipeline. All exports are **honest, raw outputs** of
the system — no manual curation, no result selection. Use them as you
would any other research dataset and treat the caveats below as binding.

The pipeline that produced this data is described at
`/raid/scratch/dzimmerman2021/wahoobay/PLAN.md` and the source code is
available on request.

---

## Quick model card

- **Detector**: YOLOv26-nano (Fishial.ai, MIT-licensed). Identifies "is
  there a fish here?" — answers about *whether* something is a fish,
  not what species.
- **Classifier**: DINOv2 backbone + ViT pooling-3 head with sub-center
  ArcFace, 866-class output, trained globally on Fishial.ai's open
  dataset. **Not yet fine-tuned on Wahoo Bay species.** Confidences are
  honest but accuracy on Atlantic-reef-specific species is modest;
  expect noticeable per-species bias until fine-tuning lands.
- **Smoothing**: a constant-velocity tracker associates raw per-frame
  detections into persistent "tracks" using IoU + motion prediction.
  Each track represents one persistent fish.
- **Sightings vs events**: an "event" is one detection in one frame.
  A "sighting" is a single fish across its full track lifetime. For
  ecological analysis you almost always want **sightings**, not events
  — the same fish lingering for 30 s at 15 fps produces ~450 events but
  is one fish.

---

## What's in each export

### `events.csv`

One row per detection per frame. The most granular dataset.

| Column | Type | Meaning |
|---|---|---|
| `id` | bigint | internal event id |
| `ts` | timestamptz | detection timestamp (UTC offset from server) |
| `frame_id` | bigint | source-internal frame counter |
| `source_name` | text | which camera/source produced the frame |
| `det_conf` | real (0..1) | detector confidence (is-a-fish) |
| `bbox` | int[4] | x1,y1,x2,y2 in source-pixel coordinates |
| `topk` | jsonb | array of `{name, species_id, accuracy}` candidates |
| `best_name` | text | classifier top-1 species name (Latin binomial) |
| `best_species_id` | text | Fishial UUID for the species |
| `best_accuracy` | real (0..1) | classifier top-1 cosine similarity / softmax |
| `image_path` | text | server-side path to the saved frame image, if any |
| `track_id` | bigint | persistent track id (NULL for events from before the smoother was deployed) |
| `model_version`, `detector_sha256`, `classifier_sha256`, `config_hash`, `pipeline_git_sha` | text | provenance — every row carries the full fingerprint of the code/weights/config that produced it |

### `sightings.csv`

One row per persistent fish track. Folds the flip-flop noise out of
events.csv. Use this for "how many fish of each species did we see".

| Column | Meaning |
|---|---|
| `track_id` | persistent fish id |
| `species_id`, `name` | dominant species over the track lifetime (weighted vote by summed accuracy) |
| `frame_count` | how many frames the track was visible |
| `duration_s` | last_seen − first_seen, in seconds |
| `first_seen`, `last_seen` | timestamps |
| `mean_accuracy`, `peak_accuracy` | classifier confidence stats |
| `top3_species` | up to 3 candidate species the classifier offered, with frame counts |

Filter via `?min_frames=N` (default 3) to drop single-frame blips.

### `hourly_summary.csv`

Time-aligned ecology table: detection counts and water quality, hourly,
joined on the hour bucket. Use this for "how does fish activity vary
with environmental conditions?".

| Column | Meaning |
|---|---|
| `hour` | hour bucket (UTC) |
| `source_name`, `species_id`, `name` | which species in which source |
| `event_count` | raw events that hour |
| `sighting_count` | distinct tracks that hour |
| `mean_accuracy` | mean classifier confidence |
| `water_temp_c`, `ph`, `do_pct`, `chlorophyll_rfu`, `phycoerythrin_rfu`, `turbidity_fnu`, `no3_mg_l`, `spcond_ms_cm` | hourly means from the sonde |

### `tracks_timeline.csv`

Per-detection rows sorted by `(track_id, ts)`. Lets you reconstruct each
fish's trajectory through the frame without reshaping.

Includes `bbox_cx/bbox_cy/bbox_w/bbox_h` derived from the bbox so you
can directly plot trajectories.

### `topk_long.csv`

Flat exploded top-K. One row per (event, candidate, rank). Use for:

- Confusion matrices (top-1 vs ground truth from corrections).
- Calibration curves (accuracy vs claimed confidence).
- Top-3 / top-5 accuracy under different filtering rules.

### `labeled_corrections.csv`

Human corrections joined to the original event context and the saved
crop path. Ready to filter into a fine-tuning training split.

| Column | Meaning |
|---|---|
| `correction_id`, `created_at`, `reviewer`, `confidence`, `notes` | who said what when |
| `corrected_name`, `corrected_species_id`, `not_a_fish` | the human label |
| `original_name`, `original_species_id`, `original_accuracy`, `original_topk` | what the model said |
| `frame_image_path`, `frame_coco_path` | where the saved crop lives on disk |
| `model_version`, `detector_sha256`, `classifier_sha256`, `config_hash`, `pipeline_git_sha` | provenance of the original prediction |

### `water_quality.csv`

Sonde readings (YSI EXO2 via SenseStream). Currently **synthetic
placeholder data** until live SenseStream credentials are configured —
look at the `source` column: `synthetic` vs `live`.

### `frame_stats.csv`

Drift sampler output, one row per ~1 s. Brightness, per-channel colour,
detection-rate signals for input-drift monitoring.

### `saved_frames.csv`

One row per frame saved to disk (timelapse / per-detection / interesting-only).

### `corrections.csv`

Compact corrections export (no joins). Use `labeled_corrections.csv` for
the fully-enriched version.

### `alerts.csv`

SLO alert log (pipeline silence, latency spikes, drift, poller staleness).

### `species_counts.csv` (legacy)

Aggregate counts only. Kept for compatibility; **prefer `sightings.csv`
or `hourly_summary.csv`** for any analysis.

---

## Caveats and known limitations

1. **Fish are anonymous.** A fish that swims out of frame and back
   counts as two sightings. We have no re-identification model.
2. **Species accuracy is modest** (~0.3–0.5 top-1 on Atlantic species)
   until fine-tuning lands. Use confidences and top-K, not just top-1.
3. **A "low-confidence" detection is honest.** Don't filter the dataset
   to high-confidence rows for ecological claims without thinking
   carefully — you'll bias toward easy/photogenic species.
4. **The pipeline samples at the camera's native frame rate** (15 fps
   for the pier cam, 30 fps for SEAHIVECAM when plumbed in). Hour-level
   counts are time-honest; sub-second counts may have aliasing from the
   smoother's ≥3-frame minimum.
5. **No explicit ground-truth split.** Once `labeled_corrections.csv`
   accumulates we'll define a held-out eval set; until then there is no
   "correct" answer in the data.
6. **Camera location precision.** The lat/lng in `camera_metadata.json`
   is the SenseStream sonde location, which is co-located with the
   camera but may be off by a few meters. Depth and heading are TODO
   pending field measurement.

---

## Coordinate / time conventions

- **Timestamps**: `timestamptz` (UTC offset attached). Convert to local
  with `AT TIME ZONE 'America/New_York'`.
- **Bboxes**: `[x1, y1, x2, y2]` in source-pixel coordinates of the
  un-rotated frame. The pier cam and SEAHIVECAM are mounted with
  rotation 180°; the pipeline doesn't pre-rotate, so `y` increases
  downward in the un-rotated frame as usual.
- **Confidences**: 0..1. Detector `det_conf` is YOLO's objectness ×
  class score. Classifier `accuracy` is cosine similarity to the
  nearest natural-centroid in 768-dim embedding space, **not a
  calibrated probability** — temperature scaling is on the roadmap.

---

## Citation / contact

If you use this data in a publication, please cite:

> Zimmerman, D. and the Wahoo Bay AI partnership (2026). Real-time fish
> identification at the Wahoo Bay SEAHIVE artificial reef. Internal
> dataset. Florida Atlantic University.

Updates and corrections: contact dzimmerman2021@fau.edu.

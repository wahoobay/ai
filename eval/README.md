# Frozen evaluation harness

A deterministic, CI-runnable evaluator for the production checkpoints.
Every run produces a timestamped, reproducible report that's intended to be
committed to git (small JSON/markdown) — so regressions over time are visible
in the commit history.

## Layout

```
eval/
├── clips/              test videos (checked in via git-lfs or pulled by fetch.sh)
├── labels/             COCO-format per-clip ground-truth JSON
├── manifest.json       which clips are in the eval set (mapping: clip id → label)
├── run.py              the runner
├── metrics.py          metric implementations (pure numpy)
└── reports/
    └── YYYY-MM-DDTHH-MM-SSZ/
        ├── report.md
        ├── metrics.json
        └── provenance.json
```

## Running

```bash
# from repo root, wahoobay conda env active:
make eval                       # default: all clips in manifest.json
make eval CLIPS="sergeant_major" # a single clip id
```

or directly:

```bash
python -m eval.run --manifest eval/manifest.json --out eval/reports
```

## What's reported

Per run:

- **Detector** — precision, recall, F1 over a bbox IoU ≥ 0.5 match.
- **Classifier** — top-1 / top-3 / top-5 species accuracy, per-species
  precision + recall + support.
- **Latency** — per-frame detector + classifier times (p50 / p95 / p99).
- **Provenance** — detector SHA, classifier SHA, config hash, git SHA.
- **Bootstrap CIs** on every scalar (95%, B=2000).

## Status

Skeleton only. `clips/` and `labels/` are empty — add labeled footage
(first candidates: re-use a handful of saved frames from `frames/`
once human corrections via the dashboard UI accumulate).

Once there are ≥1 labeled clip, `make eval` will produce real numbers.
Before that, the runner exits with a clear message describing what's
missing.

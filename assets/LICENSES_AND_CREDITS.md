# Wahoo Bay species dataset — licences, attributions, and credits

This document records, for every input that went into the v2 classifier's
training dataset, **where the data came from, who holds rights to it, and
what we can / cannot do with the derived model**. It is the authoritative
reference for any publication, public release, or commercial use of the
trained classifier checkpoint.

Last updated: 2026-05-17. Numbers below reflect the dataset as of that
date; re-run [`scripts/audit_dataset_licenses.py`](#regenerating) before
any release to refresh them.

---

## Dataset summary

| Source | Classes | Items | Licence(s) | Redistribution OK? |
|---|---|---|---|---|
| Wahoo Bay community clips (Google Drive) | 131 | ~140 source clips → ~7,500 crops | Internal-use grant from contributors | **No** (source clips) — derived crops only internally |
| iNaturalist photos | 217 | 4,310 photos | CC0, CC-BY, CC-BY-SA, CC-BY-NC, CC-BY-NC-SA, CC-BY-ND, CC-BY-NC-ND | Conditional — see below |
| WoRMS taxonomic ranks | 223 species | `assets/taxonomy.csv` | Free for non-commercial use; cite editorial board | Yes with attribution |
| Trained model weights | — | `data/models/classifier_v2/checkpoint_best.pt` | Project default (MIT) **plus** the inherited iNat constraints below | Conditional |

Total: **223 species** with at least one labelled image after the iNat
supplement (215 of which have multi-source coverage — i.e. at least one
Drive clip *and* at least one iNat photo).

---

## 1. Wahoo Bay community clips (Google Drive)

**What it is.** A volunteer-curated Google Sheet
(`1Git0Chs3pnEDngCVfDJmQ7noGM90h7uCu3AKecNgiOQ`) hyperlinks ~140 Drive
videos and stills filmed by Wahoo Bay program participants — divers,
volunteers, and the SEAHIVECAM operator — between roughly 2023 and 2026.
The original media was uploaded to Drive by the contributors themselves
and shared with the program for "training and outreach use."

**How we use it.** [`scripts/build_species_manifest.py`](../scripts/build_species_manifest.py)
parses the spreadsheet for `=HYPERLINK(...)` Drive IDs. [`scripts/download_species_clips.py`](../scripts/download_species_clips.py)
downloads the files anonymously (handling Drive's virus-scan
interstitial via the documented form-post). [`scripts/build_finetune_dataset_from_clips.py`](../scripts/build_finetune_dataset_from_clips.py)
runs the YOLO detector on each clip and writes per-detection JPEG crops
into `assets/community_clips/` with sidecar JSON tagging
`(species, source_kind, source_id, frame_idx)`.

**Rights posture.**
- **Original clips:** copyright stays with the contributing diver /
  filmer. We do **not** redistribute the source files — they remain
  on the program's Drive.
- **Cropped training inputs:** treated as program-internal training data
  under the same understanding. Crops are not published as a dataset
  release.
- **Model checkpoints derived from these crops:** for now, internal /
  research use only. Any public release of the checkpoint should be
  preceded by explicit per-clip permission from the contributors,
  documented in this file, or by retraining without the Drive sources.

**Attribution.** When publishing results that depend on this data:

> "Underwater footage contributed by Wahoo Bay program participants
> (Shipwreck Park, Pompano Beach, FL) including the SEAHIVECAM operator
> and volunteer divers. Used with permission for non-commercial
> ecological / educational purposes."

If a future paper warrants per-contributor credit, the manifest
preserves `source_id` per crop; the Drive owner of each file ID can be
recovered from the original sheet's "Created by" metadata. Reach out
through the Wahoo Bay program (`info@wahoobay.org`) to arrange.

---

## 2. iNaturalist photographs

**What it is.** 4,310 research-grade observations from
[iNaturalist](https://www.inaturalist.org/) covering 217 of our 223
species. [`scripts/download_inat_photos.py`](../scripts/download_inat_photos.py)
queries the iNaturalist REST API (`/v1/observations`) one species at a
time, rate-limited to 1 req/sec, with `photo_license` filtered to the
permissive set below. Photo URLs are size-swapped from `square` to
`large` before download, and each photo's `license`, `attribution`, and
`observer` fields are persisted in [`assets/inat_manifest.json`](inat_manifest.json).

**License breakdown** (as of 2026-05-17):

| Licence | Count | % | Commercial use? | Derivatives? |
|---|---:|---:|:---:|:---:|
| CC0 (public domain dedication) | 76 | 1.8% | yes | yes |
| CC-BY | 580 | 13.5% | yes | yes |
| CC-BY-SA | 31 | 0.7% | yes | yes (share-alike) |
| CC-BY-NC | 3,337 | 77.4% | **no** | yes |
| CC-BY-NC-SA | 56 | 1.3% | **no** | yes (share-alike) |
| CC-BY-ND | 34 | 0.8% | yes | **no derivatives** |
| CC-BY-NC-ND | 196 | 4.5% | **no** | **no derivatives** |
| **Total** | **4,310** | 100% | — | — |

**Rights posture.**

- **Training-time use of all of the above is defensible.** The mainstream
  US/EU legal commentary — and the position of every research lab
  shipping image classifiers today — is that the parameters of a trained
  ML model are not a "derivative work" of the training images in the
  copyright sense (transformative use, no expressive content of any
  single training photo is recoverable). This is not formally settled
  in court; we accept the residual risk for research / internal use.

- **Commercial use of the trained model** is constrained by the **NC**
  ("non-commercial") clause covering **83.2 %** of iNat photos
  (3,337 + 56 + 196 = 3,589). If Wahoo Bay or a partner organisation
  ever monetises a product built on this checkpoint, the cautious path
  is to retrain without those photos. The remaining ~720 permissive
  photos (CC0 + CC-BY + CC-BY-SA + CC-BY-ND) would form the commercial
  baseline, supplemented by additional non-NC sources.

- **The ND ("no derivatives") clause** covering 230 photos is the
  riskier subset for *any* downstream use, including non-commercial.
  We include them in v2 training because of the training-derivative
  argument above, but a strict-interpretation defence of model release
  would retrain without them.

- **The SA ("share-alike") clause** covering 87 photos in principle
  requires derivative works to be released under a compatible licence.
  We do not consider trained weights a derivative work (see above), so
  this does not affect our project's MIT licence. If that interpretation
  is challenged, the affected photos can be removed and the model
  retrained.

**Attribution.** When publishing results, include the canonical
acknowledgement:

> "Supplementary training photographs sourced from iNaturalist
> (inaturalist.org) under permissive Creative Commons licences. Photo
> contributors and per-photo licences are listed in
> `assets/inat_manifest.json`."

For paper publication, the manifest's `attribution` strings can be
concatenated into an appendix. If space permits, listing the top-N
photographers by photo count is a courtesy.

**Compliance with iNaturalist's terms.** Our retrieval respects
iNat's [API guidelines](https://www.inaturalist.org/pages/api+reference)
— rate limit ≤ 60 req/min (we use 1 req/sec = 60 req/min), no
authenticated bulk export, no scraping of non-API pages. We do not
re-host the photos publicly; they live on local storage at
`/raid/scratch/dzimmerman2021/wahoobay/assets/inat_photos/` and inside
the trained model only as learned parameters.

---

## 3. WoRMS taxonomy

**What it is.** The WoRMS AphiaID, accepted name and taxonomic ranks
(genus / family / order / class / phylum / kingdom) for every distinct
name in the Species Log and the classifier's class list — misspellings
and placeholders included, keyed by the name as typed (`raw_name`) —
used to build a tree-distance matrix for the planned taxonomy-aware
loss and for the dashboard's Fish Explorer roll-ups. Sourced from the
[World Register of Marine Species](https://www.marinespecies.org/)
via the `/AphiaRecordsByMatchNames` REST endpoint
([`scripts/enrich_taxonomy_from_worms.py`](../scripts/enrich_taxonomy_from_worms.py)),
output at [`assets/taxonomy.csv`](taxonomy.csv).

**Rights posture.** WoRMS data is freely usable for non-commercial
purposes per their [terms](https://www.marinespecies.org/about.php#terms);
commercial use requires permission. Citation is mandatory.

**Attribution.** Cite as:

> WoRMS Editorial Board (2026). World Register of Marine Species.
> Available from https://www.marinespecies.org at VLIZ. Accessed 2026-05-17.

---

## 4. Detector and classifier provenance

| Component | Source | Licence |
|---|---|---|
| YOLO detector (`data/models/detector_v26`) | Fishial.ai community model | MIT |
| Classifier **v1** (legacy, fallback) — `data/models/classifier_v0_10_2` | Fishial.ai community model | MIT |
| Classifier **v2** (default) — `data/models/classifier_v2` | This project (DINOv2 ViT-S/14 trained from random init) | MIT, **subject to the iNat constraints in §2** |

The v2 weights are **not** pre-trained or fine-tuned from any external
checkpoint. The architecture (timm's `vit_small_patch14_dinov2`) is
borrowed from DINOv2 in the sense that we use its Transformer topology,
but every parameter was randomly initialised and learned on our
dataset — no DINOv2 self-supervised weights are loaded. This means the
v2 checkpoint inherits **no licence obligations** from Meta's DINOv2
release; its constraints come entirely from the training data above.

---

## 5. Regenerating

If the dataset changes (more iNat photos, new Drive contributors, etc.):

```bash
# Refresh the per-source counts and licence breakdown
python scripts/audit_dataset_licenses.py > assets/LICENSES_AND_CREDITS.summary.md

# Then update the relevant sections of this file by hand and commit.
```

Per-photo attribution lines for paper appendices:

```bash
python -c "
import json
m = json.load(open('assets/inat_manifest.json'))
seen = set()
for r in sorted(m, key=lambda r: (r['binomial'], r['observer'])):
    line = r['attribution']
    if line not in seen:
        print(line); seen.add(line)
" > /tmp/inat_attribution.txt
```

---

## Contact

Questions about licensing, attribution, or commercial-use scenarios:
**dzimmerman2021@fau.edu**.

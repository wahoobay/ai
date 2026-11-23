# One name per class: smallmouth grunt and sea chub

2026-11-22 · short design (small change) · option 3, chosen by Dan

## Why

The live classifier names two of its classes the way the Species Log once spelled them, while the review
tool stores every answer under the WoRMS accepted name:

| Classifier class (`classes.json`) | What reviewers pick | Rank |
|---|---|---|
| `Haemulon_chrysargyreum` | *Brachygenys chrysargyrea* (smallmouth grunt) | species |
| `Kyphosus_sectatrixbigibbus` | *Kyphosus* (sea chubs) | genus |

The training set's class is its folder name, so the next retrain would learn two classes for one fish:
the base crops' folder under the old name and Roy's answers under the new one. Roy has 648 smallmouth
grunt answers (the base set has 178 crops) and 139 *Kyphosus* answers (the base set has 1).

Dan chose option 3: the model's own name becomes the reviewers' name, now, and the stored history follows.

## Goals

- New detections are stored under *Brachygenys chrysargyrea* and *Kyphosus*.
- Past detections are rewritten, so the species list, trends and exports show one row per fish.
- Predictions do not change: same weights, same class order, only two labels differ.
- wahoobay.org's smallmouth grunt page keeps working before its `fish.json` is next synced.
- The next training set puts the base crops and Roy's answers in one folder per fish.

## Non-goals

- Retraining (a later piece of work).
- The 24 other classes whose name differs from the accepted name. Some resolve to a genus or a disputed
  synonym and need a decision each.
- Editing the Species Log, Fishial's fallback label list, or the Reef Logbook's species bank.

## Anti-goals

- Changing a score or a prediction.
- Overwriting the model bundle that is live today: it stays as published, so a revert is one version pin.
- Two rows for one fish anywhere the public sees.

## Approaches

1. **Leave it.** Two classes for one fish at the next retrain; the model splits its confidence between them.
2. **Rename in training only.** One class after the retrain; the name changes the day the new model ships,
   and history splits then unless it is rewritten.
3. **Rename the live label now, rewrite history, rename the training folders (chosen).** Same predictions
   today, one name everywhere from now on, and the retrain inherits it.
4. **Merge under the old name.** Same model effect, but the system keeps a superseded name.

## Design

1. **One rename table.** `services/common/class_renames.py` holds `{"Haemulon chrysargyreum": "Brachygenys
   chrysargyrea", "Kyphosus sectatrixbigibbus": "Kyphosus"}` (names as the worker writes them: underscores
   become spaces). The migration, the public API alias and the dataset tooling read it; nothing else
   spells the old names.
2. **A new model bundle.** `classifier_v2/dgx-2026-10-24-r1` in the models bucket: the same
   `checkpoint_best.pt` (server-side copy, same sha256), and `classes.json` with the two entries renamed
   in place (`Brachygenys_chrysargyrea`, `Kyphosus`), order unchanged, plus `SHA256SUMS`. The repository
   copy `data/models/classifier_v2/classes.json` matches it. `fetch_models.py` defaults to the new version.
3. **Fetching a new version.** Today a model directory that holds files is never refetched, so a version
   change would not reach the GPU host. `fetch_models.py` writes a `.version` file with each fetch and
   replaces a directory whose `.version` names another version. A directory without one (a bind mount, or
   a fetch from before this change) is still left alone, so the GPU host's current directory is removed
   once by hand at deploy.
4. **Migration `0008`.** For each rename: `detection_events.best_name` and `best_species_id`, and the
   `name` and `species_id` of every `topk` entry; then both materialized views are refreshed.
   `review_items.predicted` is left alone: it already holds the catalogue's accepted names (750 rows
   of *Brachygenys chrysargyrea* on staging, none of the old name), so rewriting it would only
   corrupt the downgrade. Staging holds 3,231 + 12 events with the old name as the model's guess
   and 3,897 + 32 with it anywhere in the top-k (of 571,795). Downgrade reverses it; it is exact because
   neither new name occurs as a model output before the upgrade (the plan checks this).
5. **Names the catalogue knows.** `assets/taxonomy.csv` gains rows for the raw names
   `Brachygenys chrysargyrea` and `Kyphosus` (accepted name, AphiaID and ranks as today's rows for the old
   names, which stay for the Species Log). `common_names.py` gains `Brachygenys chrysargyrea` → Smallmouth
   Grunt and `Kyphosus` → Sea Chub.
6. **The site keeps working.** wahoobay.org's `fish.json` stores the species id `Haemulon chrysargyreum`
   and its smallmouth grunt page calls `GET /api/public/species/{id}` with it. That route answers an old
   id as the new one. Page URLs are built from the accepted name and do not change. The alias can go once
   `fish.json` has been synced.
7. **Training folders.** The base crops' copy in S3
   (`datasets/fine_tune/dgx-2026-10-24/fine_tune.tar`) stays as it was trained. Whatever unpacks it for
   training renames the two folders, and the `label` of their manifest records, through the rename table.
   Phase 3's merge is the first user.

## Left to the builder

- Where in the API the alias sits, and whether it redirects or answers in place.
- How the migration tests reach a scratch PostgreSQL (the existing `TEST_DATABASE_URL` pattern).

## Rollout

PR, then merge on Dan's word. CI builds the images; the deploy runs migration `0008`, then restarts
the GPU host's worker. The worker names its classes through the rename table whatever bundle is on
its volume (`class_names()`), so from that restart on it stores the new names. Detections written
between the migration and the restart can still carry old names; the `rename_classes` job renames
them (idempotent; it refreshes the sightings views only when it changed something). Then:

1. Invoke `rename_classes` once (`aws lambda invoke --function-name ai-wahoobay-staging-jobs
   --payload '{"job":"rename_classes"}'`), and check that no detection carries an old name.
2. On the GPU host (SSM): remove `/opt/wahoobay-data/models/classifier_v2` (fetched before `.version`
   existed) and restart the worker, so the volume holds `-r1` and later pins take effect by themselves.
3. Check that the species list and trends show one row for each fish, and that wahoobay.org's
   smallmouth grunt page still shows its live tiles.

The migration refreshes the views concurrently, so species and trend reads keep answering during it.
`ai-wahoobay-prod` is defined but not deployed; when it is, `0008` runs on a database with no
detections and the steps above do not apply.

# One name per class (smallmouth grunt, sea chub) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The live classifier's two old class names become the reviewers' names (*Brachygenys chrysargyrea*, *Kyphosus*) in the model bundle, the stored history and the public API, with predictions unchanged.

**Architecture:** One rename table (`services/common/class_renames.py`). Migration `0008` rewrites past detections with a frozen copy of it and refreshes the sightings views. A new bundle `classifier_v2/dgx-2026-10-24-r1` (same checkpoint, renamed `classes.json`) becomes the pinned default, and `fetch_models.py` learns to replace a model directory fetched for another version. The public species routes answer an old id as the new one, for wahoobay.org's `fish.json`.

**Tech Stack:** Python 3, FastAPI, psycopg 3, Alembic, pytest; AWS CLI (profile `cdk`) for the bundle.

**Spec:** `docs/superpowers/specs/2026-11-22-class-renames-design.md`

## Global Constraints

- Renames, exactly (worker spelling, spaces): `Haemulon chrysargyreum` → `Brachygenys chrysargyrea`; `Kyphosus sectatrixbigibbus` → `Kyphosus`. In `classes.json` (underscores): `Haemulon_chrysargyreum` → `Brachygenys_chrysargyrea`; `Kyphosus_sectatrixbigibbus` → `Kyphosus`.
- `classes.json` keeps 223 entries in the same order; only those two strings change.
- New bundle version string: `dgx-2026-10-24-r1`. `classifier_v2/dgx-2026-10-24/` in `s3://platform-wahoobay-models/` is never modified.
- Common names: `Brachygenys chrysargyrea` → `Smallmouth Grunt`; `Kyphosus` → `Sea Chub`.
- Tests: `TEST_DATABASE_URL_FILE=.superpowers/test_dsn .venv/bin/python -m pytest`, plus `ruff check .` and `ruff format --check .`.
- Merge and deploy only on Dan's word.

## Review Focus

1. **An old name below the top guess.** An event whose best guess is another species but whose `topk` lists the old name keeps its best guess; only that `topk` entry changes, with its `accuracy` and the list order intact. Task 1, `test_0008_renames_topk_entries_below_the_top`.
2. **The species list after the upgrade.** The sightings views hold the new id only, so the list shows one row. Task 1, `test_0008_refreshes_the_sightings_views`.
3. **A failed download of the new version.** The old model directory stays usable; nothing half-written replaces it. Task 2, `test_a_failed_fetch_of_a_new_version_keeps_the_old_one`.
4. **An old id in a URL, space-encoded, on the profile and the photo route.** Both answer as the new id; an unknown id is still a 404. Task 4, `test_an_old_species_id_answers_as_the_new_one`.
5. **The catalogue after the rename.** The renamed classes attach to the Species Log's entries (whose raw names are still the old ones) instead of making new, unresolved entries. Task 3, `test_renamed_classes_attach_to_their_species_log_entries`.

---

### Task 1: Rename table and migration 0008

**Files:**
- Create: `services/common/class_renames.py`
- Create: `db/migrations/versions/0008_class_renames.py`
- Modify: `tests/test_migrations.py` (`HEAD = "0008"`, new tests)

**Interfaces:**
- Produces: `CLASS_RENAMES: dict[str, str]` (old → new, worker spelling); `renamed(name: str | None) -> str | None` (the new name for an old one, anything else unchanged); `folder(name: str) -> str` (spaces → underscores, the `classes.json`/training-folder spelling).
- Produces: migration `0008` with a module-level frozen `RENAMES` dict (its own copy; migrations never import live code).

- [ ] **Step 1: Write the failing tests** in `tests/test_migrations.py`, each starting from `alembic("upgrade", "0007", url=pg_url)` and seeding `detection_events` rows with SQL:
  - `test_0008_renames_best_guess_and_topk`: an event with `best_name = best_species_id = 'Haemulon chrysargyreum'` and `topk = [{"name": "Haemulon chrysargyreum", "species_id": "Haemulon chrysargyreum", "accuracy": 0.8}, {"name": "Scarus iseri", "species_id": "Scarus iseri", "accuracy": 0.1}]`; after `upgrade head`: best fields are `Brachygenys chrysargyrea`, `topk[0]` is `{"name": "Brachygenys chrysargyrea", "species_id": "Brachygenys chrysargyrea", "accuracy": 0.8}`, `topk[1]` unchanged. Same check for `Kyphosus sectatrixbigibbus` → `Kyphosus`.
  - `test_0008_renames_topk_entries_below_the_top`: best guess `Scarus iseri`, `topk[2]` is the old grunt name; after: best fields still `Scarus iseri`, only `topk[2]` renamed, order and accuracies equal.
  - `test_0008_refreshes_the_sightings_views`: three events of one `track_id` under the old grunt name; after: `SELECT species_id FROM species_sightings_mat` is `[('Brachygenys chrysargyrea',)]` and `species_sightings_hourly_mat` holds no old name.
  - `test_0008_downgrade_restores_the_old_names`: after `upgrade head` then `downgrade 0007`, the rows from the first test read exactly as seeded.
  - `test_0008_renames_are_the_live_table`: `0008`'s `RENAMES.items() <= CLASS_RENAMES.items()` (load the migration module with `importlib.util.spec_from_file_location`; put `REPO / "services"` on `sys.path` for `common`).

- [ ] **Step 2: Run them and see them fail**

Run: `TEST_DATABASE_URL_FILE=.superpowers/test_dsn .venv/bin/python -m pytest tests/test_migrations.py -k 0008 -v`
Expected: FAIL (no revision `0008`).

- [ ] **Step 3: Write `services/common/class_renames.py` and `0008_class_renames.py`** (`revision = "0008"`, `down_revision = "0007"`). For each `old → new` (reversed for the downgrade): `best_name` and `best_species_id` where equal to `old`; in `topk`, for rows where an element has `name` or `species_id` equal to `old` (`topk @> '[{"species_id": ...}]'` or the same for `name`), rebuild the array with `jsonb_array_elements(...) WITH ORDINALITY`, replacing each of those two keys only where it equals `old`, keeping every other key and the order; `review_items.predicted` where equal to `old`. Then `REFRESH MATERIALIZED VIEW species_sightings_mat` and then `species_sightings_hourly_mat` (not `CONCURRENTLY`; it is inside the migration's transaction). Use bound parameters (`sa.text`) for the names. The module docstring says why (this spec) and that the downgrade is exact only while neither new name was a model output before the upgrade.

- [ ] **Step 4: Run the migration tests and the whole suite**

Run: `TEST_DATABASE_URL_FILE=.superpowers/test_dsn .venv/bin/python -m pytest tests/test_migrations.py -v && ruff check . && ruff format --check .`
Expected: PASS, including the existing tests with `HEAD = "0008"`.

- [ ] **Step 5: Commit** `services/common/class_renames.py`, the migration, the test (`Migration 0008: past detections take the reviewers' names for two classes`).

### Task 2: Fetch a new model version over an old one

**Files:**
- Modify: `scripts/fetch_models.py` (`fetch_model`, `DEFAULT_VERSIONS`, docstring)
- Modify: `.env.example:20` (the commented `MODEL_VERSIONS` example)
- Test: `tests/test_fetch_models.py`

**Interfaces:**
- Produces: `fetch_model(models_url, dest, model, version) -> int` unchanged in signature; a fetched directory holds `.version` (the version string, no newline). `DEFAULT_VERSIONS["classifier_v2"] == "dgx-2026-10-24-r1"`.

- [ ] **Step 1: Write the failing tests** (use the file's `_publish` helper):
  - `test_a_fetch_records_its_version`: after fetching version `"1"`, `(dest / model / ".version").read_text() == "1"`.
  - `test_a_model_of_another_version_is_replaced`: fetch `"1"` (`model.pt = b"one"`), publish `"2"` (`b"two"`), fetch `"2"` → returns 2 files, `model.pt` reads `b"two"`, `.version` reads `"2"`.
  - `test_the_same_version_is_left_alone`: fetching `"1"` twice → second call returns 0.
  - `test_a_failed_fetch_of_a_new_version_keeps_the_old_one`: fetch `"1"`, publish `"2"` with a wrong digest in `SHA256SUMS`, fetching `"2"` raises `ValueError`, and `model.pt` still reads `b"one"` with `.version` `"1"`.
  - `test_the_classifier_default_is_the_renamed_bundle`: `versions("")["classifier_v2"] == "dgx-2026-10-24-r1"`.
  - The existing `test_a_complete_model_is_left_alone` (a directory without `.version`) must still pass unchanged.

- [ ] **Step 2: Run them and see them fail**

Run: `.venv/bin/python -m pytest tests/test_fetch_models.py -v`
Expected: the five new tests FAIL.

- [ ] **Step 3: Implement.** A directory with files and no `.version` is left alone (today's rule). One whose `.version` equals `version` is left alone. Any other is fetched into `<model>.partial` as today, `.version` written there last, and only then swapped for the old directory. Docstring: say why (`2026-11-22`, a version pin must reach the GPU host's persistent volume) and that a directory from before this change needs removing once by hand.

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_fetch_models.py tests/test_model_assets.py -v && ruff check . && ruff format --check .`
Expected: PASS.

- [ ] **Step 5: Commit** (`fetch_models: a new version replaces a model fetched for another one; classifier_v2 pins dgx-2026-10-24-r1`).

### Task 3: The renamed class list, taxonomy rows and common names

**Files:**
- Modify: `data/models/classifier_v2/classes.json` (two entries)
- Modify: `assets/taxonomy.csv` (two rows added)
- Modify: `services/dashboard/app/common_names.py` (two entries)
- Test: `tests/test_review_catalogue.py`

**Interfaces:**
- Consumes: `CLASS_RENAMES`, `folder()` from Task 1.

- [ ] **Step 1: Write the failing tests** against the repository's real `classes.json` and `taxonomy.csv`:
  - `test_class_list_uses_the_new_names`: the list has 223 entries; `folder(new)` is in it and `folder(old)` is not, for every pair in `CLASS_RENAMES`; `Brachygenys_chrysargyrea` is at index 85 and `Kyphosus` at 108 (the old names' indexes at tag `dgx-2026-10-24`), and every other entry equals that tag's list.
  - `test_renamed_classes_attach_to_their_species_log_entries`: `load_catalogue` with a species manifest of two records (`{"binomial": "Haemulon chrysargyreum", "common_name": "Smallmouth Grunt"}`, `{"binomial": "Kyphosus sectatrix/bigibbus", "common_name": "Bermuda/Gray Chub"}`), the real `classes.json` and `taxonomy.csv`, empty photo/celebrity files: exactly one entry named `Brachygenys chrysargyrea` with `class_dir == "Brachygenys_chrysargyrea"` and `in_classifier`, one named `Kyphosus` with `class_dir == "Kyphosus"`, both `resolved`; `catalogue.accepted("Brachygenys chrysargyrea") == "Brachygenys chrysargyrea"` and the same for `Haemulon chrysargyreum`.
  - `test_common_names_for_the_new_names`: `common("Brachygenys chrysargyrea") == "Smallmouth Grunt"`, `common("Kyphosus") == "Sea Chub"`.

- [ ] **Step 2: Run them and see them fail**

Run: `.venv/bin/python -m pytest tests/test_review_catalogue.py -v`

- [ ] **Step 3: Edit the three files.** `taxonomy.csv`: a row with `raw_name` `Brachygenys chrysargyrea` copying every other column of the `Haemulon chrysargyreum` row except `status`, which is `accepted` (the name is the accepted one); a row `Kyphosus` copying the `Kyphosus sectatrix/bigibbus` row with `match_type` `exact`. Keep the file's sort order. Keep the old rows.

- [ ] **Step 4: Run the tests and the suite**

Run: `TEST_DATABASE_URL_FILE=.superpowers/test_dsn .venv/bin/python -m pytest && ruff check . && ruff format --check .`
Expected: PASS (fixtures that spell `Haemulon chrysargyreum` are synthetic catalogues and stay).

- [ ] **Step 5: Commit** (`Class list, taxonomy and common names under the reviewers' names`).

### Task 4: Old ids on the public species routes

**Files:**
- Modify: `services/dashboard/app/routes_public.py` (`public_species_profile`, `public_species_photo`)
- Test: `tests/test_public_api.py`

**Interfaces:**
- Consumes: `renamed()` from Task 1.

- [ ] **Step 1: Write the failing test** `test_an_old_species_id_answers_as_the_new_one` (pattern of `test_species_profile_has_24_hours_12_months_and_recent`): seed a track under `Brachygenys chrysargyrea`, refresh; `GET /api/public/species/Haemulon%20chrysargyreum` is 200 with `species_id == "Brachygenys chrysargyrea"` and the same body as the new id's URL; `GET /api/public/species/Haemulon%20chrysargyreum/photo.jpg` gives the same status as the new id's photo URL; `/api/public/species/Nonesuch%20fish` is still 404.

- [ ] **Step 2: Run it and see it fail**

Run: `TEST_DATABASE_URL_FILE=.superpowers/test_dsn .venv/bin/python -m pytest tests/test_public_api.py -k old_species_id -v`

- [ ] **Step 3: Implement.** Both handlers start with `species_id = renamed(species_id)` and answer in place (no redirect: the site's island fetches JSON and should not follow a hop). A comment says the alias can go once wahoobay.org's `fish.json` has been synced.

- [ ] **Step 4: Run the public API tests**

Run: `TEST_DATABASE_URL_FILE=.superpowers/test_dsn .venv/bin/python -m pytest tests/test_public_api.py -v && ruff check . && ruff format --check .`
Expected: PASS.

- [ ] **Step 5: Commit** (`Public API: an old species id answers as its new one`).

### Task 5: Publish the bundle, open the PR (operator, with AWS access)

Not code. Run from the Mac with `AWS_PROFILE=cdk`, region `us-east-1`. `B=s3://platform-wahoobay-models/classifier_v2`.

- [ ] **Step 1: Check the downgrade precondition on staging** (read-only, SSM into the GPU host's worker as in the session's earlier count query): `count(*)` of `detection_events` with `best_species_id` or a `topk` entry equal to `Brachygenys chrysargyrea` or `Kyphosus` is 0.
- [ ] **Step 2: Publish.** `aws s3 cp $B/dgx-2026-10-24/checkpoint_best.pt $B/dgx-2026-10-24-r1/checkpoint_best.pt` (server-side copy); upload the repository's `data/models/classifier_v2/classes.json`; write `SHA256SUMS` with the old checkpoint line (`f0aacc54…`, unchanged) and the new `classes.json` digest.
- [ ] **Step 3: Verify.** `MODELS_URL=s3://platform-wahoobay-models/ python scripts/fetch_models.py` into a scratch `MODELS_DIR` fetches `classifier_v2@dgx-2026-10-24-r1` with checksums passing, and its `classes.json` equals the repository's.
- [ ] **Step 4: Push the branch and open the PR** (body: what changes, the rollout below, and that merge waits for Dan).

### Rollout (after Dan says merge)

Superseded by the spec's "Rollout" section after the final review (the worker now names its classes
through the table, and the `rename_classes` job closes the gap between the migration and the worker
restart). In short:

1. Merge. CI runs migration `0008`, then restarts the worker, which stores the new names from then on.
2. `aws lambda invoke --function-name ai-wahoobay-staging-jobs --payload '{"job":"rename_classes"}'`; check no detection carries an old name.
3. SSM on `ai-wahoobay-staging-gpu`: `rm -rf /opt/wahoobay-data/models/classifier_v2`, then `docker compose -f /opt/wahoobay/deploy/gpu-host/docker-compose.yml restart worker`; its log shows `classifier_v2@dgx-2026-10-24-r1`.
4. Check: `/api/public/species` lists smallmouth grunt once; `/api/public/species/Haemulon%20chrysargyreum` answers; wahoobay.org's smallmouth grunt page shows its live tiles.
5. Revert, if needed: revert the merge (the worker's mapping is code), pin `MODEL_VERSIONS=classifier_v2=dgx-2026-10-24`, and `alembic downgrade 0007`.

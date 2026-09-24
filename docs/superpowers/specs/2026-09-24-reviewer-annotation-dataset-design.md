# Design: Reviewer Annotation Tool and Verified Dataset

**Date:** 2026-09-24
**Status:** Draft for review
**Spec 2 of 2.** Builds on spec 1 (`2026-09-22-reviewer-auth-rbac-design.md`): accounts, roles, login sessions, `require_role`, `require_csrf`, and the `user_id` / `session_id` columns on `detection_corrections`.

---

## 1. Purpose

The species classifier is trained on a dataset that has not grown since the system went live. `data/fine_tune/` holds 12,531 crops across 223 species, all dated 2026-05-17. Of the 12,534 records in its manifest, 9,764 (78%) are iNaturalist photos and 2,770 (22%) come from 138 files the Wahoo Bay team shared on Google Drive. In a sample of eight of those files, seven were SEAHIVECAM livestream recordings with the camera's overlay. None of the crops come from the 15.5 million frames this system has saved since launch. 151 of the 223 species have fewer than 50 crops; bringing every species to 200 would take about 32,500 more.

Four months of reviewer corrections produced 53 rows: 49 "not a fish", none attributable to a person, the last on 2026-05-20.

This spec builds the tool that closes that loop, and the pipeline that turns its output into a dataset fit to train on and to share with other scientists.

### Intended outcome

Wahoo Bay's volunteers, who know the local species by sight but are not technical, review the model's detections in a browser. They accept a correct guess with one key, fix a box by dragging it, relabel it from the Species Log, or mark it as unidentifiable or not an animal. Every verdict is attributable to an account and a login session. Verdicts become a versioned, citable dataset in which every crop carries its date, time, position, depth, and water and weather conditions, in the formats ecologists (Darwin Core) and machine-learning researchers (COCO) use.

### Success criteria

- A reviewer opens `/review`, is served a box in under a second, and can give a verdict with one keystroke.
- The queue serves the species furthest below target first, and never serves the same box to two people at once.
- Every verdict carries `user_id` and `session_id`. Every species label also records how it was chosen.
- About one in ten first-pass verdicts is re-checked by a second volunteer who sees neither the first answer nor the model's guess. Agreement is reported.
- The browse page shows what is verified overall, what was verified in the current session, and what remains per species.
- `scripts/build_verified_dataset.py` produces a snapshot whose content hash is identical when rebuilt from the same cutoff. Its merge into `data/fine_tune/` can be undone, restoring the original crops and the same manifest records in the same order.
- No synthetic sensor reading appears in any export.
- No volunteer's name appears in a public export or a shared dataset unless that volunteer opted in.
- The public dashboard is not slowed: the pool task never overlaps itself, never runs a query longer than 5 seconds, and adds no view DDL.
- No new Python or JavaScript dependency.

### Out of scope

- Recovering Katie's red-ellipse annotations. It is a separate workstream; §19 records a timing fact it will need.
- Changing what the worker saves or stores, such as keeping low-confidence guesses on every detection. The scorer (§7) provides hints for pool boxes only.
- Training runs, and publication to OBIS, GBIF or FathomNet. The builder writes files in their formats; submission (EML metadata, IPT hosting, FathomNet upload) is a later, manual step.
- Fixing the sonde, the DGX clock, or the hung worker process (§19).
- Phone layouts. The page targets desktop browsers; tablets work but are not designed for.

---

## 2. What exists today

Measured on 2026-09-23 and 2026-09-24 against the live database and disk unless noted.

| Fact | Consequence for this design |
|---|---|
| `detection_events` holds about 668M rows; `saved_frames` about 15.56M. | The queue cannot search the live tables on each click (§6). |
| In a 0.002% page sample (14,574 detections), 31.3% had their frame saved. All of those matched on camera + `frame_id` + `ts` exactly, and no sampled `frame_id` belonged to two saved frames. | Boxes are bound to frames by exact timestamp, never by `frame_id` alone. |
| `frame_id` and `track_id` restart at 1 whenever a worker process restarts. The tracker produced 4,827 distinct tracks in ten daytime minutes. | Neither ID deduplicates on its own; the pool spaces candidates in time (§6). |
| 8.0% of the 21,770 detections in a ten-minute daytime window (2026-09-23, 12:00–12:10 EDT) carried a species guess. A broader sample in May found 11.4%. | Most boxes have to be identified from scratch, hence two lanes. |
| `MIN_ACCEPT_ACCURACY=0.55` filters the top-3 list, and two softmax probabilities cannot both exceed 0.55, so at most one guess is stored. | The current form's "alternatives" never appear. Hints are computed separately (§7). |
| `detection_events.image_path` is always NULL. Four code paths find a box's frame by `(frame_id, source_name)` alone and ignore `ts`: three take the newest match, the export takes any match. | Those lookups gain `ts` equality (§12). |
| `labeled_corrections.csv` exports the model's box, not `corrected_bbox`. `build_finetuning_dataset.py` crops that box, uses every verdict instead of the latest, and advertises a `--output coco` option it does not implement. | Box corrections have never reached training data. The new builder replaces this path (§11, §12). |
| The correction form's species autocomplete lists only species the model predicted in the last 30 days. | The picker uses the Species Log and the classifier's classes (§9). |
| The Species Log (`assets/species_manifest.json`, 281 entries, rebuilt daily by the sheet poller) has 239 distinct names, including misspellings and entries that are not species (`Pomatomus saltarix`, `Apylsia morio`, `Alligatoridae mississippiensis`, `Anilocra ./Renocila`). Seven classifier classes are not in it. | Labels are reconciled to WoRMS accepted names (§9). |
| `/api/corrections`, `/api/export/corrections.csv` and `/api/export/labeled_corrections.csv` are public and include `reviewer`, now an account's display name, and free-text `notes`. (Neither export has a Parquet variant; `PARQUET_RESOURCES` covers four other resources.) | Identity and notes become admin-only (§12). |
| `detection_corrections.event_id` is `ON DELETE CASCADE` (and was added `NOT VALID`). | Changed to `RESTRICT` (§5). |
| The Wahoo Bay API's latest sonde reading is 2026-06-25 10:30 UTC, and the database's last live sonde row matches it. SenseStream returned an error for both later windows tried (tonight, and a July window). The poller logs `sonde fetch HTTP 404` on every five-minute poll in its recent log, and the database holds synthetic sonde rows up to 2026-09-23 05:34 EDT. Weather has live readings every five minutes through today. | Real water conditions exist only from 2026-04-15 to 2026-06-25. Saved frames start 2026-05-17 (§11). |
| The DGX clock is 17 s slow against three external servers; `timedatectl` reports "System clock synchronized: no". Both cameras' burned-in overlays read 16–17 s later than the database timestamps. | Capture times carry a stated uncertainty (§11). |
| The pier camera is 2.84 km from SEAHIVECAM, where the sonde and weather station are. | Environmental values record their distance from the camera. |
| During camera-dark fallback the SEAHIVECAM worker plays `data/test_videos/` for the stream only. Nothing is written: no detections in the 90 minutes checked, and the most recent `pier_cam` saved frame is the real, dark pier camera. | Playlist footage cannot enter the dataset. |

---

## 3. Constraints

| Constraint | Consequence |
|---|---|
| No root on the shared DGX | No system packages. |
| One conda env runs every production process | No new dependencies. Agreement statistics and the Darwin Core and COCO writers use the standard library plus packages already installed (numpy, Pillow, OpenCV, psycopg). |
| The live database serves the public dashboard, and `CREATE OR REPLACE VIEW` takes an ACCESS EXCLUSIVE lock | No new views. Schema is applied with the procedure in `docs/operations.md`, "Applying a schema change to the live database". |
| The matview refresher already runs overlapping refreshes | The pool task refuses to overlap itself. |
| Single-process dashboard behind a public tunnel | Every write needs a role and the CSRF header (spec 1). Heavy work stays out of the web process. |
| Shared GPUs | The scorer runs on GPU 4, which the SEAHIVECAM worker already uses, and needs 1–2 GB. |
| Frames live on `/raid`, 13 TB free | Snapshots copy crops and complete frames. Nothing in the archive is moved. |

---

## 4. Decisions taken with the user

- **Reviewers** are Wahoo Bay's volunteer team: species-literate, not technical.
- **Two lanes.** *Check the model's guesses* serves boxes with a guess of 55% or more, thinnest species first. *Identify new fish* serves boxes with no guess.
- **Species-coverage-first** ordering, against a default target of 200 crops per species.
- **Per-box verification**, with an optional whole-frame mode that makes a frame usable for detector training.
- **One verdict plus a blind spot-check** of about one in ten.
- **Approach C:** a pre-built review pool, plus classifier hints for every pool box.
- **Layout B, "fish first":** the zoomed box is the work area; the frame is context.
- **Shared with both communities:** Darwin Core CSVs for ecologists, COCO plus a JSON manifest for machine-learning researchers.
- **Opt-in credit** for volunteers; stable pseudonyms otherwise.
- **Crop metadata** includes date, time, latitude, longitude, depth, and water and weather conditions.

---

## 5. Data model

Added to `db/init.sql` with the existing `IF NOT EXISTS` convention. Applied to the live database as new statements only (§14).

```sql
CREATE TABLE IF NOT EXISTS review_items (
    id                  BIGSERIAL PRIMARY KEY,
    event_id            BIGINT      NOT NULL REFERENCES detection_events(id) ON DELETE CASCADE,
    pass                SMALLINT    NOT NULL DEFAULT 1 CHECK (pass IN (1, 2)),
    lane                TEXT        NOT NULL CHECK (lane IN ('check', 'identify')),
    saved_frame_id      BIGINT      NOT NULL REFERENCES saved_frames(id),
    source_name         TEXT        NOT NULL,
    ts                  TIMESTAMPTZ NOT NULL,          -- = detection_events.ts = saved_frames.ts
    bbox                INTEGER[]   NOT NULL,          -- model box, [x1,y1,x2,y2]
    predicted           TEXT,                          -- model's >=55% guess, accepted name
    predicted_p         REAL,
    hints               JSONB,                         -- [{"name": ..., "p": ...}] x3; NULL until scored
    hints_model         TEXT,                          -- classifier_sha256 that produced the hints
    hinted_at           TIMESTAMPTZ,                   -- when the scorer wrote them (status page)
    route_species       TEXT,                          -- species this box counts toward for ordering
    status              TEXT        NOT NULL DEFAULT 'open'
                        CHECK (status IN ('open', 'claimed', 'done', 'retired')),
    retired_reason      TEXT,                          -- frame_missing | outside_window | ...
    claimed_by          BIGINT      REFERENCES users(id),
    claimed_until       TIMESTAMPTZ,
    exclude_user_id     BIGINT      REFERENCES users(id),               -- pass 2: the first reviewer
    first_correction_id BIGINT      REFERENCES detection_corrections(id), -- pass 2: verdict under check
    added_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    done_at             TIMESTAMPTZ,
    UNIQUE (event_id, pass)
);
CREATE INDEX IF NOT EXISTS review_items_open
    ON review_items (lane, pass, route_species) WHERE status = 'open';
CREATE INDEX IF NOT EXISTS review_items_claimed
    ON review_items (claimed_until) WHERE status = 'claimed';
CREATE INDEX IF NOT EXISTS review_items_unscored
    ON review_items (id) WHERE hints IS NULL AND status IN ('open', 'claimed');
CREATE INDEX IF NOT EXISTS review_items_camera_ts
    ON review_items (source_name, ts);

CREATE TABLE IF NOT EXISTS review_skips (
    item_id     BIGINT      NOT NULL REFERENCES review_items(id) ON DELETE CASCADE,
    user_id     BIGINT      NOT NULL REFERENCES users(id),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (item_id, user_id)
);

CREATE TABLE IF NOT EXISTS review_added_boxes (      -- fish the model missed
    id              BIGSERIAL PRIMARY KEY,
    saved_frame_id  BIGINT      NOT NULL REFERENCES saved_frames(id),
    bbox            INTEGER[]   NOT NULL,
    outcome         TEXT        NOT NULL CHECK (outcome IN ('species', 'unidentifiable')),
    label           TEXT,                            -- accepted name when outcome = 'species'
    life_stage      TEXT,
    confidence      TEXT        NOT NULL DEFAULT 'certain'
                    CHECK (confidence IN ('certain', 'probable', 'uncertain')),
    celebrity_id    TEXT,
    user_id         BIGINT      NOT NULL REFERENCES users(id),
    session_id      BIGINT      REFERENCES user_sessions(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ,
    removed_at      TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS review_added_boxes_frame
    ON review_added_boxes (saved_frame_id) WHERE removed_at IS NULL;

CREATE TABLE IF NOT EXISTS review_frames (           -- whole-frame completions
    saved_frame_id  BIGINT      PRIMARY KEY REFERENCES saved_frames(id),
    completed_by    BIGINT      NOT NULL REFERENCES users(id),
    session_id      BIGINT      REFERENCES user_sessions(id),
    completed_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS review_settings (         -- admin-editable, e.g. the date window
    key         TEXT        PRIMARY KEY,
    value       JSONB       NOT NULL,
    updated_by  BIGINT      REFERENCES users(id),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE detection_corrections
    ADD COLUMN IF NOT EXISTS review_item_id  BIGINT REFERENCES review_items(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS label_source    TEXT CHECK (label_source IN ('model_guess', 'hint', 'search')),
    ADD COLUMN IF NOT EXISTS unidentifiable  BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS life_stage      TEXT,
    ADD COLUMN IF NOT EXISTS is_adjudication BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS credit_opt_in BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS credit_name   TEXT,
    ADD COLUMN IF NOT EXISTS orcid         TEXT;
```

A `CHECK (NOT (not_a_fish AND unidentifiable))` constraint is added to `detection_corrections` under its own name, guarded by a `DO` block so re-running `init.sql` does not fail. The 53 existing rows all satisfy it.

**`ON DELETE RESTRICT` for verdicts.** Nothing prunes detections today, but the planned cloud migration includes a retention policy, and a cascade would silently delete human labels with their detections. A `DO` block replaces `detection_corrections_event_id_fkey` only while its `confdeltype` is still `'c'`: it adds the new constraint `NOT VALID`, validates it, then drops the old one. Adding a foreign key briefly locks `detection_events` against inserts. With `NOT VALID` that lasts milliseconds, and the migration's `lock_timeout` bounds the wait. The `CREATE TABLE` statement for `detection_corrections` in `init.sql` is changed to `RESTRICT` for fresh databases.

**How verdicts are stored.** The new tool writes into `detection_corrections`, the same table as the dashboard's existing buttons, so there is one kind of human label. The outcome is encoded in existing columns plus the new ones:

| Outcome | `corrected_name` | `not_a_fish` | `unidentifiable` | `is_endorsement` |
|---|---|---|---|---|
| Accept the model's guess | accepted name | false | false | true |
| Species (relabel) | accepted name | false | false | true only if it equals the guess |
| Can't identify | NULL | false | true | false |
| No animal | NULL | true | false | false |

`confidence` is `certain`, or `uncertain` when the volunteer ticks "not sure". `reviewer` keeps receiving the display name so existing queries keep working; identity comes from `user_id`.

**No `ON DELETE CASCADE` into `users`.** Accounts are disabled, not deleted (spec 1), so verdicts never lose their author.

**Changes to `data/camera_metadata.json`.** Each camera entry gains the following fields. `wahoo_2` is the existing key for SEAHIVECAM; the new `source_name` field ties it to `detection_events.source_name = 'seahivecam'`.

```jsonc
"source_name": "seahivecam",
"locality": null,                      // null until Wahoo Bay confirms the wording (§19);
                                       // exports fall back to display_name + "Pompano Beach"
"coord_uncertainty_m": 10,             // default; to be confirmed (§19)
"sharing": {"public": false, "license": null,
            "rights_holder": "Wahoo Bay (FAU partnership)",   // copied from the entry's `owner`
            "agreed_by": null, "agreed_on": null},
"environment": {
  "sonde":           {"deployment_uri": "wahoo_2", "lat": 26.260345, "lng": -80.083399},
  "weather_station": {"deployment_uri": "wahoo_2", "lat": 26.260345, "lng": -80.083399},
  "metar":           {"station": "KPMP", "lat": null, "lng": null}
}
```

The sonde and weather-station coordinates are SEAHIVECAM's, following the existing note that the sonde is co-located with the camera and the `init.sql` comment that the station is co-located with the sonde; Wahoo Bay is asked to confirm (§19). The KPMP coordinates are filled in during implementation from `api.weather.gov/stations/KPMP`. The pier camera's entry gets the same `environment` block; its distances therefore come out at about 2.84 km.

**Changes to `assets/taxonomy.csv`.** `scripts/enrich_taxonomy_from_worms.py` already calls WoRMS `AphiaRecordsByMatchNames`. It is extended to cover every Species Log name and every classifier class, and to keep `raw_name`, `aphia_id`, `accepted_name`, `authority`, `rank` and `match_type`. Names WoRMS cannot resolve, or resolves only fuzzily, are listed in `assets/taxonomy_unresolved.txt` for Wahoo Bay to correct in the sheet. The script is run by hand; nothing calls WoRMS at build or serve time.

---

## 6. The review pool

A background task in the dashboard, started in the lifespan like the other four, keeps `review_items` topped up.

**Scheduling.** It runs every `REVIEW_POOL_TOPUP_INTERVAL_S` (300 s). A run that finds the previous run still going exits immediately. Each query runs with `SET LOCAL statement_timeout = '5s'`, and each run is capped at `REVIEW_POOL_MAX_PROBES` (200) index probes and 30 seconds of wall time. `REVIEW_POOL_ENABLED=false` switches the task off without a code change.

**Date window.** Candidates come from `[window_start, now − 10 min]`. The window defaults to the whole saved-frame period, which starts 2026-05-17. An admin can narrow it on the browse page, for example to 2026-05-17 → 2026-06-25, the period with live sonde data. When the window changes, open items outside it are retired with `outside_window`. The last ten minutes are excluded so a detection is never picked before its frame has been written.

**`check` lane.** Species are taken in order of progress (below), lowest first. A species is eligible while its count is below target and it has fewer than `REVIEW_POOL_CHECK_OPEN_PER_SPECIES` (20) open items. For each eligible species, a probe:

1. Picks a random time `t` in the window.
2. Reads up to 50 events of that species at or before `t`, joined to `saved_frames` on camera + `frame_id` + `ts`. This uses the `detection_events_species_ts` index and early-terminates.
3. Keeps events whose box has a shorter side of at least `REVIEW_MIN_BOX_PX` (32), that have no verdict and no pool item, and that are at least `REVIEW_POOL_SPACING_S` (120 s) from any existing item for the same camera and species.
4. Inserts up to the open-item shortfall. The model's guess (a classifier class name) is mapped to its accepted name for `predicted` and `route_species`.

A species whose probes find nothing is skipped for a day.

**`identify` lane.** While fewer than `REVIEW_POOL_IDENTIFY_OPEN` (500) items are open and the run's budget lasts, a probe:

1. Picks a random time `t` in the window.
2. Reads up to 40 saved frames with `num_fish > 0` at or before `t`, using the `saved_frames_ts_desc` index.
3. For each frame, reads its unguessed detections by exact `(source_name, ts)` (the `detection_events_source_ts` index). It keeps boxes with `det_conf >= REVIEW_IDENTIFY_MIN_DET_CONF` (0.4), a shorter side of at least 32 px, and no verdict or pool item, and takes the largest.
4. Inserts it unless an `identify` item from the same camera lies within 120 s.

`route_species` stays NULL until the scorer fills it in (§7).

**Why spacing, not IDs.** `track_id` and `frame_id` restart with the workers, and one fish produces many short tracks. Spacing items in time per camera is what keeps near-duplicates out of the pool and, later, out of the dataset.

**Progress.** Kept in memory, refreshed on every run and incremented on every verdict:

- `baseline[S]`: records in `data/fine_tune/_dataset_manifest.json` labelled S, excluding records whose `source_kind` starts with `review:`, so crops merged from earlier snapshots are not counted twice.
- `verified[S]`: resolved boxes (§8) with outcome species S and status single, agreed or adjudicated, with confidence other than `uncertain`, plus added boxes labelled S.
- `progress[S] = (baseline[S] + verified[S]) / REVIEW_TARGET_PER_SPECIES` (200).

**Verification before enabling.** The two probe queries' `EXPLAIN` plans are checked against production before the task is switched on. They must be index scans with no sequential scan of `detection_events` or `saved_frames`. The plans are recorded in the implementation plan.

---

## 7. Machine hints

**What they are.** For each pool box, the live classifier's top three classes and their probabilities, with no 55% cutoff, stored as `review_items.hints` along with `hints_model`, the checkpoint's `classifier_sha256`.

**Shared classifier code.** The checkpoint loading, dtype handling and `_preprocess_crop` in `services/worker/app/classifier_v2.py` move into `services/worker/app/species_classifier.py`, as a `SpeciesClassifier` class with `class_names` and `probs(frame_bgr, boxes) -> ndarray`. `ClassifierV2Pipeline` uses it, with behaviour unchanged, including `torch.compile` and warm-up. A characterization test pins identical outputs. The live workers pick the refactor up only when next restarted.

**The scorer process.** `services/worker/app/review_scorer.py` runs as `python -m app.review_scorer` via `scripts/dev/run_review_scorer.sh`, with `PYTHONPATH=services/worker`, the `.env` classifier settings and `CUDA_VISIBLE_DEVICES=4`. Every `REVIEW_SCORER_INTERVAL_S` (60 s) it:

1. Selects up to `REVIEW_SCORER_BATCH` (256) open or claimed items whose `hints` are NULL or whose `hints_model` differs from the current checkpoint's hash, unscored first.
2. Groups them by frame, decodes each frame once, and classifies the boxes in one batch, eagerly (no compile).
3. Writes the top three as `[{"name": accepted name, "p": 0.5647}, …]`, mapping class names to accepted names through `assets/taxonomy.csv`. For `identify` items it sets `route_species` to the first hint.
4. Retires an item whose frame file is missing, with `frame_missing`.

The scorer is restart-safe: it holds no state beyond the rows it reads. It logs to `logs/app/review_scorer.log`.

**When it is down.** Nothing blocks. Unscored boxes are served without hint buttons and, in the `identify` lane, after scored boxes and in random order. Open boxes are re-scored after a new checkpoint is deployed.

---

## 8. Serving, verdicts and what counts as verified

### Serving the next box

`GET /api/review/next?lane=check|identify`, for user U:

1. Lapsed claims (`claimed_until < now()`) return to `open`.
2. If U already holds a claimed item in that lane (after a reload, say), return it.
3. **Spot-check.** With probability `REVIEW_SPOTCHECK_SERVE_RATE` (0.10), or always when the oldest eligible one has waited more than 48 hours, serve an open pass-2 item from either lane. Eligible means U is not its `exclude_user_id`, has no verdict on that box, and has not skipped it.
4. Otherwise, serve an open pass-1 item in the lane, ordered by `route_species` progress ascending (the in-memory order is passed as an array parameter), then randomly, excluding U's skips.
5. Claim it in one statement: `UPDATE … WHERE id = (SELECT … FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING …`, with `claimed_until = now() + REVIEW_CLAIM_S` (600 s).

The payload carries the box, the frame and zoom image URLs, the other model boxes in the frame, the species the box counts toward and its progress, and (pass 1 only) the model's guess and hints. **A pass-2 payload contains no guess, no hints and no earlier verdict.** The server builds it that way; the page does not hide anything.

An empty pool returns a "the pool is filling" state rather than an error.

### Verdicts

`POST /api/review/verdicts`:

```jsonc
{"event_id": 692005930, "review_item_id": 123,          // item id absent for whole-frame boxes
 "outcome": "accept",                                    // accept | species | unidentifiable | no_animal
 "label": null, "life_stage": null,                      // catalogue entry for "species"
 "bbox": null,                                           // [x1,y1,x2,y2] only if the volunteer moved it
 "label_source": "model_guess",                          // model_guess | hint | search
 "not_sure": false, "celebrity_id": null, "notes": null}
```

Server-side rules:

- The label must be a catalogue entry (§9). No free text.
- A box must lie within the frame (`0 <= x1 < x2 <= W`, `0 <= y1 < y2 <= H`) and be at least 4 × 4 px.
- `model_guess` is only valid with `accept`. `hint` is only valid if the label is among the item's hints. Neither is valid on a pass-2 item.
- The item must be claimed by U, or open and unclaimed. If someone else has finished it, the response is 409 and the page loads the next box.

On success the verdict row is written with `user_id` and `session_id`. Every open or claimed pass-1 item for that box is marked done. With probability `REVIEW_SPOTCHECK_RATE` (0.10), a first-pass verdict made in the tool (from the queue or whole-frame mode) creates a pass-2 item with `exclude_user_id = U` and `first_correction_id` set. Verdicts from the dashboard's older correction form are not spot-checked.

**Skip** inserts into `review_skips` and returns the item to `open`.

**Reopen.** The recent-boxes strip lists U's last 20 verdicts in the session. Reopening one serves the box again and a new verdict is appended; nothing is edited in place. A reviewer can reopen only boxes they have given a verdict on; admins can reopen any.

### Whole-frame mode

`GET /api/review/frames/{saved_frame_id}` returns every model box in the frame (exact `source_name`, `frame_id`, `ts` match), each with its resolved state, plus any added boxes. It has no side effects.

- Opening the mode (`POST /api/review/frames/{saved_frame_id}/open`) claims the frame's open pool items for U and returns the same state. Boxes held by someone else are shown locked.
- Boxes with a pending spot-check show as "answered" without revealing the answer, so viewing the frame cannot contaminate the second opinion.
- The volunteer gives each unanswered model box a verdict through the same endpoint, and draws boxes the model missed: `POST /api/review/frames/{id}/boxes`, with `PATCH` and `DELETE` for the box's creator before completion and for admins at any time. Removal sets `removed_at`.
- `POST /api/review/frames/{id}/complete` succeeds only when every model box has a first-pass verdict from anyone and every added box is labelled.

### Resolution: the label that counts

For each box, over its verdicts, excluding the 53 legacy rows that have no `user_id`:

1. If there is an adjudication, the latest one wins. Status: **adjudicated**.
2. Otherwise, take the latest pass-1 verdict. A verdict is pass 2 if it is linked to a pass-2 item; every other non-adjudication verdict is pass 1, including verdicts from whole-frame mode and from the dashboard's older form. If the box also has a pass-2 verdict, compare their outcomes. Outcomes are compared as `(species, accepted name)`, `unidentifiable` or `no_animal`; life stage and box differences do not count. Same outcome: **agreed**. Different: **disputed**, held out of the dataset until an admin adjudicates.
3. Otherwise the latest pass-1 verdict stands. Status: **single**.

The box that counts is the latest corrected box among the deciding verdicts, otherwise the model's. Added boxes are **single**. Admins may adjudicate disputes they took part in; the adjudicator's `user_id` is recorded.

This logic lives in one module (`review_labels.py`) used by the page, the progress counts, the admin export and the builder. There is exactly one definition of "verified".

### Agreement

Pairs are boxes with both a pass-1 and a pass-2 verdict, taking the latest of each. The browse page's admin panel reports raw percent agreement and **Krippendorff's alpha (nominal)**, overall and split by the first verdict's `label_source`. That split is how anchoring to the model's guesses or hints would show up. Alpha is displayed once there are at least 30 pairs.

During the design conversation this was called Cohen's kappa. Cohen's kappa assumes the same two raters rate every item; here each box's pair of volunteers differs. Krippendorff's alpha allows that, and it is the statistic `docs/finetune_workflow.md` already names for multi-class labels.

---

## 9. The review page

Layout B, approved on 2026-09-24 against mockups built from a real SEAHIVECAM frame (event 692005930).

**Single-box view.**
- The zoomed box is the work area: 3× zoom with half a box of margin on each side, and eight drag handles. `Esc` undoes a drag.
- The side panel shows the frame with every model box outlined; the species the box counts toward, with its n / 200 bar; the model's guess with a reference photo and a "Looks right" button; the species search; up to three hint buttons; "Can't identify", "No animal here", "Not sure", note, Skip and Whole frame.
- A strip along the bottom holds the volunteer's recent boxes, for changing an answer.
- In the `identify` lane there is no "Looks right". Hints are labelled "the model's weak guesses (under 55%, often wrong)". On a pass-2 box there is neither.

**Keyboard.** `A` accept, `/` search, `1`–`3` hints, `C` can't identify, `N` no animal, `S` skip, `F` whole frame, `D` draw a box, `Esc` undo a drag, `Enter` confirm a search choice. Every status is conveyed in text as well as colour.

**Whole-frame mode.** The frame becomes the work area. Boxes are numbered and outlined by state (answered, needs an answer, drawn by the volunteer). The side panel shows the selected box's zoom and controls, a checklist of model boxes and added boxes, and "Mark frame complete", disabled with the reason until the frame qualifies.

**Browse.**
- Headline counts: verified boxes, species with verified boxes, complete frames, verified this session, open in the queue.
- Filters: status, species, camera, who (anyone, me, this session), lane, date.
- A grid of boxes with status badges: verified, agreed, adjudicated, disputed, can't identify, no animal, in queue.
- A species-progress list, thinnest first. Grey is the training-set baseline and green is verified in the tool.
- Reviewers see everyone's verdicts but can change only their own. Admins additionally get disputes (both verdicts side by side, then choose one or relabel), agreement, the date window, pool status and the manifest download.

**The species catalogue** (`GET /api/review/species`). Built from the Species Log, the classifier's `classes.json` and `assets/taxonomy.csv`, and rebuilt when any of those files changes. One entry per accepted name and life stage, as the Species Log lists them (for example, juvenile and adult French Angelfish). Each entry carries:

- the accepted name, AphiaID, rank and family
- the common name and life stage
- `class_dir`, the training folder it maps to (typo'd class names map through `raw_name`)
- whether it is in the Species Log and whether it is in the classifier
- named individuals from `assets/celebrities.json`
- a reference photo from `assets/inat_photos/` with its attribution from `assets/inat_manifest.json`, shown on screen as the photo licenses require

Search runs in the browser over roughly 300 entries, matching common and scientific names, prefix matches first. Names WoRMS could not resolve stay selectable under their raw name and are flagged in exports.

**Images.**
- `GET /api/review/frames/{saved_frame_id}/image.jpg?size=` and `GET /api/review/boxes/{event_id}/zoom.jpg` read the file named in `saved_frames.image_path`. The zoom finds the box's frame by the exact camera + `frame_id` + `ts` match, never by `frame_id` alone. They are looked up by ID, never by a client-supplied path, and served with immutable cache headers.
- Reference photos come from `GET /api/review/species/{slug}/photo.jpg`.

**Code.** `templates/review.html`, `static/review.js` and `static/review.css`, in vanilla JavaScript like `app.js`, which is not extended (2,855 lines). The `/review` placeholder route is replaced.

---

## 10. API surface

All mutations require the `X-Requested-With: wahoobay` header (spec 1).

| Method | Path | Role |
|---|---|---|
| GET | `/review` | reviewer, admin (HTML; anonymous visitors are redirected to login) |
| GET | `/api/review/next?lane=` | reviewer, admin |
| POST | `/api/review/verdicts` | reviewer, admin |
| POST | `/api/review/items/{id}/skip` | reviewer, admin |
| POST | `/api/review/items/{id}/release` | reviewer, admin (sent when leaving the page) |
| GET | `/api/review/boxes/{event_id}` | reviewer, admin (reopen) |
| GET | `/api/review/mine/recent` | reviewer, admin |
| GET | `/api/review/frames/{saved_frame_id}` | reviewer, admin |
| POST | `/api/review/frames/{saved_frame_id}/open` | reviewer, admin (claims the frame's open boxes) |
| GET | `/api/review/added/{box_id}/zoom.jpg` | reviewer, admin |
| POST, PATCH, DELETE | `/api/review/frames/{id}/boxes[/{box_id}]` | reviewer (own boxes, before completion), admin |
| POST | `/api/review/frames/{id}/complete` | reviewer, admin |
| DELETE | `/api/review/frames/{id}/complete` | admin |
| GET | `/api/review/frames/{saved_frame_id}/image.jpg`, `/api/review/boxes/{event_id}/zoom.jpg` | reviewer, admin |
| GET | `/api/review/species`, `/api/review/species/{slug}/photo.jpg` | reviewer, admin |
| GET | `/api/review/browse`, `/api/review/progress` | reviewer, admin |
| GET | `/api/review/disputes`, `/api/review/agreement`, `/api/review/status` | admin |
| POST | `/api/review/disputes/{event_id}/adjudicate` | admin |
| PUT | `/api/review/settings/window` | admin |
| GET | `/api/review/export/manifest.jsonl` | admin |
| PUT | `/api/account/credit` | any authenticated user (own account) |

`/api/review/status` reports open items per lane and pass, unscored items, the scorer's last write time, and the last pool run's duration and outcome.

---

## 11. The verified dataset

### Snapshot layout

`scripts/build_verified_dataset.py`, run by an admin on the DGX, reads the database directly, applies the resolution rules (§8), and writes a dated snapshot that is never modified afterwards:

```
data/verified/2026-10-01T1500Z/
  README.md                        # description, methods, caveats, license, how to cite
  fields.csv                       # every field: file, definition, unit, source, verified/unverified
  manifest.json                    # snapshot provenance + one record per box
  occurrence.csv                   # Darwin Core occurrences
  measurements.csv                 # Darwin Core Extended Measurement or Fact
  classifier/<Accepted_name>/ev<event_id>.jpg | add<box_id>.jpg   # merge maps these to class_dir
  negatives/ev<event_id>.jpg       # "no animal" crops, with --include-negatives
  detector/images/<saved_frame_id>.jpg
  detector/annotations.coco.json   # complete frames only
  report.txt                       # everything held back, with the reason
```

Options: `--release internal|public` (default internal), `--min-confidence probable` (default; drops `uncertain`), `--include-negatives`, `--cutoff` (default now), `--merge-into PATH`, `--unmerge PATH`, `--allow-new-classes`.

**Crops.** The resolved box plus 5% padding, not resized (training does `RandomResizedCrop`), matching the current builder's default.

**Detector frames.** Complete frames, each copied in with every real animal on it: verified model boxes, added boxes, and "can't identify" boxes as an "unidentified animal" category. "No animal" boxes are left out, so they count as background. A complete frame containing a disputed box is held back until the dispute is settled; exporting it could otherwise teach the detector that a fish is background.

**Reproducibility.** The manifest records the cutoff, meaning the newest verdict id and time included. Rebuilding from the same cutoff yields the same files and the same `content_sha256`, a SHA-256 over every file's path and hash in sorted order.

### Per-crop metadata

Every box record carries, in addition to the label, box, model and verification blocks shown during design:

- **When.** Capture time in UTC and local time (America/New_York). Every time carries `uncertainty_s: 60`, and the README explains why: the processing server's clock was measured 17 s slow and not synchronised on 2026-09-24, and earlier offsets are unknown.
- **Where.** The camera's latitude and longitude (WGS84), `coord_uncertainty_m` (default 10, because the animal is somewhere in view), locality, state and country, all from `camera_metadata.json`.
- **Depth.** The camera's depth at mean tide (2 m for SEAHIVECAM, 3 m for the pier camera), explicitly labelled as camera depth, not animal depth.
- **Water.** The nearest reading within ±15 minutes from the camera's sonde with `source = 'live'`: temperature, pH, dissolved oxygen, turbidity, chlorophyll, phycoerythrin and specific conductance. Otherwise `null`, with a reason such as `no live sonde reading within 15 min (sonde offline since 2026-06-25)`.
- **Weather.** Measured: the nearest live station reading within ±15 minutes (air temperature, humidity, barometric pressure, wind speed and direction, rain intensity, solar radiation). Modeled: the Open-Meteo values within ±60 minutes (cloud cover, weather code, UV index, dew point, apparent temperature, precipitation probability). Observed at the airport: the KPMP METAR description within ±90 minutes.
- **Provenance of each environmental value.** Its reading's timestamp, its offset from capture, how it was obtained (measured, modeled, or observed elsewhere), and the station's distance from the camera, computed from the coordinates in `camera_metadata.json` (0 m for SEAHIVECAM; 2,840 m from the pier camera to the SEAHIVECAM station).

Synthetic rows are never matched: `source = 'synthetic'` rows are excluded in SQL, and a unit test holds that line. The tag matters in practice. The station row nearest the example frame (2026-09-23, 10:14:25 EDT) *measured* 1,171 W/m² of sunlight while the *modeled* layer on the same row said 100% cloud cover.

**Unverified fields.** The station's `water_level_mm` (it reads −457 mm against an unknown reference), `rain_accum_mm` and `rain_dur_frac_hr` (the intervals are undocumented), and the `hail_*` channels are marked `unverified` in `fields.csv`. They appear only in internal builds until Wahoo Bay confirms their meaning (§19).

Example, for the Banded Rudderfish box used in the mockups. Every value is real, from the database on 2026-09-24.

```jsonc
"site":    {"camera": "seahivecam", "lat": 26.260345, "lon": -80.083399, "coord_uncertainty_m": 10,
            "camera_depth_m": 2, "depth_reference": "camera depth at mean tide"},
"time":    {"utc": "2026-09-23T14:13:32.8Z", "local": "2026-09-23T10:13:32.8-04:00", "uncertainty_s": 60},
"water":   null,
"water_missing_reason": "no live sonde reading within 15 min (sonde offline since 2026-06-25)",
"weather": {"station": {"ts": "2026-09-23T14:14:25Z", "offset_s": 52, "distance_m": 0, "method": "measured",
                        "air_temp_c": 26.8, "rel_humidity_pct": 85.2, "pressure_hpa": 1015.0,
                        "wind_speed_ms": 1.9, "wind_dir_deg": 163, "solar_rad_wm2": 1171},
            "model":   {"method": "modeled (Open-Meteo, hourly)", "cloud_cover_pct": 100,
                        "weather_code": 3, "uv_index": 2.5}}
```

### Darwin Core

`occurrence.csv` has one row per verified box whose outcome is a species. "Can't identify" and "no animal" are not species records; they appear only in COCO and the manifest.

| Darwin Core term | Value |
|---|---|
| `occurrenceID` | `wahoobay:event:692005930` or `wahoobay:added:17` |
| `eventID` | `wahoobay:frame:<saved_frame_id>` |
| `basisOfRecord` | `MachineObservation` |
| `eventDate` | local ISO 8601 with offset, e.g. `2026-09-23T10:13:32-04:00` |
| `eventRemarks` | the clock caveat |
| `decimalLatitude`, `decimalLongitude`, `geodeticDatum` | camera position, `WGS84` |
| `coordinateUncertaintyInMeters`, `georeferenceRemarks` | `coord_uncertainty_m`; "camera position; animal within its field of view" |
| `minimumDepthInMeters`, `maximumDepthInMeters`, `locationRemarks` | camera depth; "camera depth at mean tide; animal depth not measured" |
| `locality`, `stateProvince`, `countryCode` | from `camera_metadata.json`; Florida; US |
| `scientificName`, `scientificNameID`, `scientificNameAuthorship`, `taxonRank` | accepted name; `urn:lsid:marinespecies.org:taxname:<AphiaID>`; WoRMS authority; rank |
| `kingdom` … `genus` | WoRMS classification |
| `vernacularName`, `lifeStage` | Species Log common name without a leading life-stage word; life stage |
| `individualCount`, `occurrenceStatus` | 1; `present` |
| `identifiedBy`, `identifiedByID` | credit name or pseudonym; ORCID URL if given |
| `dateIdentified`, `identificationVerificationStatus`, `identificationRemarks` | deciding verdict time; single, agreed or adjudicated; how the label was chosen |
| `associatedMedia` | the crop's path in the snapshot |
| `samplingProtocol` | fixed underwater PTZ camera; automated detection; volunteer verification with a blind 10% second opinion |
| `recordedBy` | camera system, from `camera_metadata.json` |
| `institutionCode`, `datasetName` | `WahooBay`; snapshot id |
| `license`, `rightsHolder` | from `sharing`; `internal use only, not for redistribution` in internal builds |

`measurements.csv` has one row per environmental value per frame, linked by `eventID`, with `measurementType`, `measurementValue`, `measurementUnit`, `measurementDeterminedDate` (the reading's time), `measurementMethod` (sonde, on-site station, Open-Meteo model, or NWS METAR KPMP) and `measurementRemarks` (offset and distance). `measurementTypeID` vocabulary URIs (NERC P01) are left for the OBIS submission step.

### COCO

`detector/annotations.coco.json` is standard COCO, extended where COCO allows:

- **images:** `id` (= `saved_frame_id`), `file_name`, `width`, `height`, `date_captured`, `camera`, `latitude`, `longitude`, `depth_m`, `license`
- **annotations:** `bbox` as `[x, y, width, height]`, `area`, `iscrowd`, `category_id`, `box_id` (the manifest's), `bbox_source`, `verification`
- **categories:** one per species (with AphiaID), plus `unidentified animal`

The README states that the manifest and CSVs use `[x1, y1, x2, y2]` while COCO uses its own convention.

### Credit, pseudonyms and release

- **Credit.** A volunteer who opts in under the name menu on the review page (`PUT /api/account/credit`) is credited as `credit_name`, with an ORCID if given. ORCIDs are validated for format and checksum. Everyone else appears as `Wahoo Bay identifier <user_id>`, and the manifest uses numeric ids only. Names never appear in internal builds either, except as opted-in credit.
- **Notes** are never exported.
- **Public builds.** `--release public` includes only cameras whose `sharing.public` is true and that have a license. It drops unverified fields and flags rows without a WoRMS ID in `report.txt`. It refuses to run if no included camera qualifies.
- **README caveats.** The README warns that repeated sightings of one individual across frames are not independent samples, even with the pool's spacing.

### Feeding training

`train_baseline.py` reads only `data/fine_tune/`. `--merge-into data/fine_tune`:

1. Removes every `review_*.jpg` under `data/fine_tune/` and every manifest record whose `source_kind` starts with `review:`.
2. Copies the snapshot's classifier crops to `data/fine_tune/<class_dir>/review_ev<id>.jpg`.
3. Appends records in the existing format: `drive_id` = `review:<camera>:<local date>`, `source_kind` = `review:video`, `frame_index` = seconds since local midnight. The existing `by_source` and `temporal_within_source` splits then treat each camera-day as a clip, unchanged.
4. Writes `_dataset_manifest.json` atomically (temporary file, then rename), saves the previous version in the snapshot folder, and records the snapshot id in `data/fine_tune/_review_merge.json`.

Labels without a `class_dir` are reported and skipped unless `--allow-new-classes` is given. `--unmerge` performs step 1, deletes `_review_merge.json`, and removes any class folder that `--allow-new-classes` created and that is now empty. Re-running a merge always reflects current labels. Undoing it restores the original crops and the same manifest records in the same order; the file's formatting may differ.

---

## 12. Changes to existing code

1. **Privacy.** `/api/corrections`, `/api/export/corrections.csv` and `/api/export/labeled_corrections.csv` return `reviewer`, `notes`, `user_id` and `session_id` only to admins. For everyone else those columns are removed, not blanked. The `?reviewer=` filter becomes admin-only. This ships in phase 1, before named verdicts accumulate.
2. **`labeled_corrections` export.** Adds `corrected_bbox`, `is_endorsement`, `unidentifiable`, `life_stage`, `label_source`, `review_item_id` and `is_adjudication`, and joins the frame on exact `ts`.
3. **Exact-timestamp frame lookup** in `/api/events`, `/api/detections/{id}/roi.jpg`, `/api/detections/{id}/frame.jpg` and the export `LATERAL` join. They gain `AND sf.ts = de.ts`.
4. **`build_finetuning_dataset.py`.** Its correction path is superseded. It prints a deprecation notice pointing to `build_verified_dataset.py`, and the docs change accordingly.
5. **`classifier_v2.py`** uses `SpeciesClassifier` (§7).
6. **`scripts/enrich_taxonomy_from_worms.py`**, **`data/camera_metadata.json`** and **`assets/taxonomy.csv`**, as described in §5.
7. **`.gitignore`** gains `data/verified/`.
8. **Docs.**
   - `docs/operations.md`: the scorer as a sixth long-running process, the pool task, its switch, and the schema step.
   - `docs/finetune_workflow.md`: the new builder. Its training example also passes `--data-root` and `--arch`, which `train_baseline.py` does not accept, and describes `train/val/test` subfolders that do not exist. That is corrected at the same time.
   - The README.

The dashboard's older correction form is otherwise left as it is.

---

## 13. Code organisation

**Dashboard** (`services/dashboard/app/`):

| File | Responsibility |
|---|---|
| `review_catalogue.py` | Species catalogue: Species Log, classes, taxonomy, photos, named individuals |
| `review_pool.py` | Top-up task, spacing, date window, progress counts |
| `review_queue.py` | Serving, claiming, skips, spot-check scheduling |
| `review_labels.py` | Resolution rules and agreement statistics; shared with the builder |
| `routes_review.py` | `APIRouter` for the page and the API in §10 |
| `dataset/environment.py` | Nearest-reading matching with tolerances, provenance and missing reasons |
| `dataset/records.py` | Per-box record assembly, used by the builder and the admin download |
| `dataset/darwin_core.py`, `dataset/coco.py` | Writers |
| `templates/review.html`, `static/review.js`, `static/review.css` | The page |

**Worker** (`services/worker/app/`): `species_classifier.py` and `review_scorer.py`.

**Scripts:** `build_verified_dataset.py`, `dev/run_review_scorer.sh` (with the conda check fixed in `4481713`), and the modified `enrich_taxonomy_from_worms.py`.

The builder imports the dashboard's `app` package the way `scripts/create_user.py` does.

---

## 14. Operations

**Configuration** (`.env`, with defaults):

| Variable | Default |
|---|---|
| `REVIEW_POOL_ENABLED` | `true` |
| `REVIEW_POOL_TOPUP_INTERVAL_S` | `300` |
| `REVIEW_POOL_MAX_PROBES` | `200` |
| `REVIEW_POOL_CHECK_OPEN_PER_SPECIES` | `20` |
| `REVIEW_POOL_IDENTIFY_OPEN` | `500` |
| `REVIEW_POOL_SPACING_S` | `120` |
| `REVIEW_MIN_BOX_PX` | `32` |
| `REVIEW_IDENTIFY_MIN_DET_CONF` | `0.4` |
| `REVIEW_TARGET_PER_SPECIES` | `200` |
| `REVIEW_CLAIM_S` | `600` |
| `REVIEW_SPOTCHECK_RATE`, `REVIEW_SPOTCHECK_SERVE_RATE` | `0.10`, `0.10` |
| `REVIEW_SCORER_INTERVAL_S`, `REVIEW_SCORER_BATCH` | `60`, `256` |

**Schema.** All of §5 is applied once, in phase 1. Apply only the new statements, as one transaction with `lock_timeout = '5s'` and `ON_ERROR_STOP`, following `docs/operations.md`. The new foreign keys from `review_items` briefly lock `detection_events` and `saved_frames` against inserts; the `lock_timeout` bounds the wait. The statement file is kept in the plan.

**Deploy.** Restart the dashboard with the documented recipe, announced beforehand. The pool fills over roughly the first 30 minutes. Then start the scorer with `nohup ./scripts/dev/run_review_scorer.sh >> logs/app/review_scorer.log 2>&1 &`.

**Watching it.** `/api/review/status` for pool size, scorer lag and pool-run duration; `logs/app/review_scorer.log`.

**Rollback.**
- Set `REVIEW_POOL_ENABLED=false` and restart the dashboard to stop the pool.
- Stop the scorer process.
- Review data lives in new tables and new nullable or defaulted columns, so the rest of the dashboard never depends on it.
- Snapshots are independent folders. A merge is undone with `--unmerge`.

---

## 15. Error handling

| Situation | Behaviour |
|---|---|
| Frame file missing on disk | The item is retired as `frame_missing` and never served; the builder reports it. |
| Claim expired and another volunteer finished the box | 409; the page loads the next box. |
| Claim expired, nobody else touched it | The verdict is accepted. |
| Session expired mid-review | 401; the page sends the volunteer to `/login?next=/review`, and the claim keeps the box for them for up to 10 minutes. |
| Database unavailable | 503; the page shows a retry banner and keeps the unsent verdict. |
| Pool empty | "The pool is filling" state, with the time of the last pool run. |
| Scorer down | Boxes are served without hints (§7). |
| Pool run exceeds budget or a query times out | The run stops, logs, and the next run starts fresh. |
| Species name no longer in the catalogue after a sheet edit | Existing verdicts keep their stored name; the builder reports names without a WoRMS match. |

---

## 16. Security

- Every endpoint follows spec 1: `require_role`, and `require_csrf` on every mutation. Admin-only endpoints are listed in §10.
- Blindness of spot-checks is enforced when the server builds the payload.
- Volunteers can only claim, skip and verdict boxes served to them, reopen their own verdicts, and edit their own added boxes before completion.
- Images are looked up by database ID; no endpoint accepts a path.
- The public-export redaction (§12) removes identity columns rather than blanking them, so column-count differences between roles are expected and tested.
- A public dataset build is gated on recorded owner permission (§11).

---

## 17. Testing

Uses spec 1's setup: a pytest test database, `TestClient`, and the autouse fixture that keeps tests away from the live workers and from mail.

- **Resolution:** single, agreed, disputed, adjudicated, reopened after a spot-check, legacy rows ignored, added boxes.
- **Agreement:** percent agreement and Krippendorff's alpha against hand-computed fixtures, including the split by label source.
- **Pool:** exact-timestamp join with a deliberate `frame_id` collision across two worker restarts; spacing; minimum size; date-window retirement; progress excludes `review:` baseline records; the no-overlap guard.
- **Queue:** thinnest-first order; the spot-check serve rate; pass-2 never served to the first reviewer or to anyone with a verdict on the box; two clients requesting at once never receive the same item; lapsed claims return.
- **Verdict validation:** catalogue-only labels, box bounds, `label_source` consistency, 409 on a finished item, role and CSRF refusals.
- **Spot-check payloads** contain no guess, hints or earlier verdict.
- **Whole-frame mode:** completion rules, locked boxes, hidden answers on boxes pending a spot-check.
- **Environment matching:** nearest live reading inside the tolerance; synthetic rows never matched, even when closer; missing reasons; distances.
- **Writers:** Darwin Core required terms present; COCO boxes converted correctly; public builds omit unverified fields and uncleared cameras.
- **Builder:** the content hash is stable across two builds with the same cutoff; in a temporary copy of `data/fine_tune/`, merge then unmerge leaves the same files and the same manifest records in the same order; `train_baseline`'s split functions accept the merged records.
- **Privacy:** public exports have no `reviewer`, `notes` or user columns; admin exports do.
- **Classifier refactor:** `SpeciesClassifier` and the old code path give identical probabilities on a fixed input with a small randomly initialised model on CPU.
- **Manual checklist** for the page (drag handles, keyboard, whole-frame drawing, browse filters), since the repo has no browser-test harness.

---

## 18. Build order and rollout

1. **Phase 1, review tool:** all schema for every phase, in one migration; WoRMS enrichment of `assets/taxonomy.csv` (the catalogue needs accepted names); catalogue; pool; queue; verdicts and resolution; the page (single box, whole frame, browse, disputes); privacy fix; exact-timestamp lookups. Volunteers can start here.
2. **Phase 2, scorer:** `SpeciesClassifier` refactor; scorer process; hint ordering in the `identify` lane.
3. **Phase 3, dataset:** `camera_metadata.json` fields; environment matching; manifest, Darwin Core and COCO writers; credit settings; release gate; merge and unmerge; documentation.

Each phase is planned and reviewed on its own. Putting all schema in phase 1 means one live-database migration and one announced restart for the schema, instead of three. The columns phases 2 and 3 use sit unused until then.

**Rollout.** Apply the schema, restart the dashboard, and let the pool fill. Start the scorer once phase 2 lands. A one-week pilot with one or two volunteers checks speed and agreement before the team is invited. The first snapshot and training run follow phase 3.

---

## 19. Open items, and issues found during design

**Needs someone outside this codebase:**

- **Wahoo Bay (Katie, Rob):**
  - Confirm the sonde is offline, and whether it will return.
  - Confirm the weather station's position relative to SEAHIVECAM.
  - Confirm `coord_uncertainty_m` and the locality wording for both cameras.
  - Explain what `water_level_mm` (datum) and `rain_accum_mm` / `rain_dur_frac_hr` (interval) measure.
  - Correct the Species Log names in `assets/taxonomy_unresolved.txt`.
  - Agree a license for SEAHIVECAM data in public releases.
- **City of Pompano Beach:** permission and a license for pier-camera frames in public datasets. Current access covers internal and stakeholder use.
- **DGX administrators:** the clock is 17 s slow and not synchronising. The user has no root.
- **Volunteers:** opting in to credit is their choice, per account.

**Found during design, not fixed by this spec:**

1. The sonde has sent no data since 2026-06-25 10:30 UTC, and the dashboard's water panel has shown synthetic values since.
2. The DGX clock is 17 s slow. Every `ts` in the database inherits whatever offset applied at the time.
3. Both cameras' burned-in overlays read about 17 s later than the database timestamps. Recovering Katie's annotations by matching burned-in times against `saved_frames.ts` must allow for this, and for an unknown historical drift.
4. Worker PID 2719772, started 2026-05-05, is orphaned and idle: 66 s of CPU time in 141 days, no connections, no GPU memory. It can be killed.
5. The SEAHIVECAM worker's `SOURCE_LABEL_FALLBACK=pier_cam` would label playlist frames as the pier camera if the "no writes during fallback" rule ever regressed. The rule holds today, but the label should be changed to something unmistakable.
6. The matview refresher still overlaps its own refreshes, as recorded earlier.

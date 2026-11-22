# Reviewer-added species checked against WoRMS: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the admin approval step from reviewer-added species (ai #49). Instead:
- The reviewer's browser checks the name against WoRMS.
- A chosen WoRMS species goes onto everyone's list at once.
- Its answers get ordinary second-opinion sampling.
- An admin can remove an addition afterwards.

**Architecture:**
- **WoRMS client.** A new browser module, `review-worms.js`, asks the WoRMS REST API and classifies the result.
- **Form.** The "Add a species" form in `review-core.js` uses the client to pick a record. It then posts the record to the server.
- **Server.** The server applies the list rules and inserts the suggestion already `approved`, so the merged catalogue picks it up on the next request.
- **Answers.** `submit_verdict` treats an answer linked to an approved suggestion as an ordinary listed answer.
- **Admin.** A new admin "remove" moves every answer naming the choice, drawn boxes included.
- **Leftovers.** The #49 pending paths stay for leftover pending rows. None exist on staging.

**Tech Stack:** FastAPI, psycopg 3 (async), Alembic, plain browser JS, pytest. DB tests use `TEST_DATABASE_URL`; node-run page tests go through `tests/js_source.py`.

**Spec:** `docs/superpowers/specs/2026-11-21-species-worms-check-design.md` (builds on `2026-11-21-reviewer-species-suggestions-design.md`)

## Global Constraints

- **WoRMS.** Base URL `https://www.marinespecies.org/rest`, always with `marine_only=false`. At most 5 candidates are shown. Only rank `Species` can be added. Tests never call the real WoRMS: they stub `fetch` by URL.
- **Stages** are unchanged: none, `juvenile`, `intermediate`, `adult`, `initial phase`, `terminal phase`.
- **The server is the authority on the list rules.** These are the stage, the name shape, duplicates on species+stage (X1), the AphiaID duplicate, and the two staging refusals below. The page only mirrors them to offer "Use …" buttons.
- **The two staging refusals (Dan, option a):**
  - **Stage none for a species listed only with stages.** Refused with `Conflict(f"pick a stage: {', '.join(stages)}, or add a new one")`. Stages are in catalogue order.
  - **A stage for a species listed without one.** Refused with `Conflict(f"already on the list as {X}")`.
  - **Still allowed:** a new stage of a species listed with stages.
- **Name shape.** Accepted scientific names may carry a subgenus: `^[A-Z][a-z]+(?: \([A-Z][a-z]+\))? [a-z]+(?:-[a-z]+)?(?: [a-z]+(?:-[a-z]+)?)?$`. A bare genus is refused with "pick a species, not a genus".
- **Second opinions.** Answers linked to an approved suggestion are sampled for spot-checks, counted, allowed blind, and never retire a pass-2 item. The #49 rules for those cases apply only while a suggestion is `pending`.
- **Copy, verbatim:**
  - "Not on the list? Add a species";
  - "Check with WoRMS";
  - "Did you mean?";
  - "WoRMS lists this as {valid name}";
  - "WoRMS doesn't know this name. Check the spelling, or answer Can't identify for now.";
  - "WoRMS isn't answering right now; try again in a minute, or answer Can't identify for now.";
  - "pick a species, not a genus";
  - "Add and answer this box";
  - "already on the list as {X}";
  - button `Use {X}`;
  - "Species you added";
  - reviewer statuses "added" / "removed" (legacy pending still reads "waiting for approval");
  - panel heading "Species added by reviewers";
  - "Remove", "It's actually…", "Can't identify";
  - "this species is now on the Species Log";
  - "that species was removed from the list; pick from the list".
- **Booth mode** (`[data-booth="1"]`) hides the "Add a species" link.
- **Running tests.** Run `TEST_DATABASE_URL_FILE=.superpowers/test_dsn /Users/dan/wahoobay/ai/.venv/bin/python -m pytest -p no:cacheprovider`, then `ruff check .`, `ruff format --check .`, and `node --check` on every edited script. The 3 `tests/test_gpu_host.py` failures are local-environment only.

## Review Focus

1. **Two reviewers add the same species at nearly the same moment.** The second is refused "already on the list as …", even when its catalogue was read before the first committed. Otherwise removing one of two identical additions would leave the species listed with its answers moved. Test `test_a_second_addition_of_the_same_choice_is_refused_even_with_a_stale_catalogue` (Task 1).
2. **WoRMS answers with a synonym whose valid name is already listed** (e.g. *Scarus croicensis* → *Scarus iseri*). The form shows "WoRMS lists this as Scarus iseri", then "already on the list as …" with a `Use …` button, and posts nothing. Test `test_a_synonym_of_a_listed_species_offers_use_it` (Task 4).
3. **WoRMS is down, or answers 5xx.** The form shows the "isn't answering" text, posts nothing, and the Check button works again. Test `test_worms_down_says_so_and_adds_nothing` (Task 4).
4. **An admin removes a species while a reviewer still has it in their picker.** Answering with it is refused with the "removed from the list" text, which reloads the catalogue (`RV.STALE_SPECIES`). Test `test_a_removed_species_cannot_be_answered` (Task 2).
5. **A removed species was also picked by other reviewers from the list, and on boxes volunteers drew.** All those answers move. An answer a reviewer has since changed to another species is untouched. Test `test_removal_moves_every_answer_naming_the_choice_including_drawn_boxes` (Task 3).

---

### Task 1: Migration 0007, and adding a species already approved

**Files:**
- Create: `db/migrations/versions/0007_species_suggestion_worms.py`
- Modify: `services/dashboard/app/species_suggestions.py` (`SuggestionIn`, `clean_scientific`, `create_suggestion`, `STATUS_TEXT`, `list_mine`, `_approved_entries`, `merged_catalogue`)
- Modify: `services/dashboard/app/routes_review.py` (`POST /api/review/species-suggestions` response)
- Test: `tests/test_species_suggestions.py`, `tests/test_migrations.py` (HEAD "0007"), `tests/test_jobs.py` (heads "0007")

**Interfaces:**
- **Produces columns** `species_suggestions.aphia_id integer` and `species_suggestions.family text`. Downgrade drops them.
- **Produces `class SuggestionIn(BaseModel)`:**
  - `aphia_id: int = Field(..., gt=0)`;
  - `scientific_name: str = Field(..., min_length=1, max_length=160)`, the WoRMS accepted name;
  - `family: str | None = Field(None, max_length=120)`;
  - `common_name: str | None = Field(None, max_length=120)`;
  - `life_stage: str | None`;
  - `note: str | None` (≤500).
- **Produces `clean_scientific(raw) -> str | None`:**
  - It uses the subgenus-tolerant regex from Global Constraints.
  - A single capitalised word gives `Invalid("pick a species, not a genus")`. Anything else non-matching gives `Invalid("write the scientific name as Genus species")`.
- **Produces `async def create_suggestion(conn, *, user, body: SuggestionIn, catalogue) -> dict`.**
  - It returns `{"key": f"{name}|{stage or ''}", "name", "life_stage", "common", "suggestion_id"}`.
  - It decides in this order:
    1. **Check the stage.** `_stage(body.life_stage)`.
    2. **Clean the name.** `clean_scientific`.
    3. **Resolve the listed name.** `listed_name` is the name of a catalogue entry whose `aphia_id == body.aphia_id` if one exists, else `catalogue.accepted(scientific)`.
    4. **Exact choice.** If `catalogue.entry(listed_name, stage)` exists → `_listed_as(entry)`.
    5. **Staging refusals.** `stages = catalogue.stages(listed_name)`:
       - non-empty, `None` not in it, and `stage is None` → the "pick a stage" Conflict;
       - `None` in it and `stage is not None` → `_listed_as(catalogue.entry(listed_name, None))`.
    6. **Common name.** The common name is `body.common_name`, squashed, or `scientific` when blank. `_listed_by_common_name` must not find it.
    7. **Lock and re-check.** In one transaction, take `pg_advisory_xact_lock(hashtext('species-add:' || listed_name))`. Refuse with `_listed_as`-style text if an approved row has the same `approved_name`/`approved_stage`. Then insert the row with:
       - `status='approved'`;
       - `approved_name=listed_name`, `approved_common=common`, `approved_stage=stage`;
       - `scientific_name=scientific`, `common_name=common`, `life_stage=stage`;
       - `aphia_id`, `family`, `note`;
       - `decided_at=now()`, `decided_by=NULL`, `decision_note=f"WoRMS AphiaID {aphia_id}"`.
  - The #49 rule 4 ("you already suggested this…") and the per-suggester lock go away.
- **Produces `STATUS_TEXT`:** `{"pending": "waiting for approval", "approved": "added", "rejected": "removed"}`. `list_mine` adds `aphia_id` to its keys.
- **Produces `_approved_entries`.** An added entry not in the base taxonomy carries `aphia_id`, `family`, `rank="Species"` and `resolved=True` from the row. The stamp query and selected columns grow to include `aphia_id` and `family`.

- [ ] **Step 1: Write the failing tests.**
  - **Migration.** `HEAD = "0007"` and both `test_jobs.py` heads change. Add `test_suggestion_worms_columns_at_head`: an insert with `aphia_id`/`family` round-trips.
  - **`test_an_addition_is_listed_for_everyone_at_once`:**
    - Create with `aphia_id=159855`, `scientific_name="Pseudupeneus maculatus"`, `family="Mullidae"`, `common_name=None`.
    - The row is `approved`, `common == "Pseudupeneus maculatus"`, and `decision_note == "WoRMS AphiaID 159855"`.
    - `merged_catalogue` has `entry("Pseudupeneus maculatus", None)` with `aphia_id == 159855` and `family == "Mullidae"`.
    - A second reviewer's `GET /api/review/species` lists it.
  - **`test_staging_refusals`.** Use the fixture catalogue; add the entries needed through `write_review_assets` output if missing.
    - Stage none for a species listed only with stages (e.g. `Pomacanthus paru`) → `Conflict` starting "pick a stage: ".
    - Stage `adult` for a species listed only stage-less → `Conflict("already on the list as …")`.
    - A new stage of a species listed with stages is created.
  - **`test_an_aphia_id_of_a_listed_species_is_a_duplicate`.** A different spelling carrying a listed entry's `aphia_id`, at its stage → `Conflict` "already on the list as".
  - **`test_a_genus_and_a_subgenus_name`:**
    - `"Scarus"` → `Invalid("pick a species, not a genus")`.
    - `"Charybdis (Charybdis) hellerii"` is accepted.
  - **`test_a_second_addition_of_the_same_choice_is_refused_even_with_a_stale_catalogue`** (Review Focus 1). Create once. Create again with the same name and stage, passing the base (file) catalogue that lacks it → `Conflict` "already on the list as".
  - **Update existing tests.** Every #49 test that needs a **pending** suggestion now inserts one directly, with a new test helper:

    ```python
    async def insert_pending(db, *, user_id: int, common_name: str,
                             scientific_name: str | None = None,
                             life_stage: str | None = None) -> int
    ```

    Remove or rewrite tests of the deleted rule 4. Keep every #49 hiding, approve, reject and detach test green on pending rows.
- [ ] **Step 2: Run them and watch them fail.**
  - Command: `pytest tests/test_species_suggestions.py tests/test_migrations.py tests/test_jobs.py -q`
  - Expected: FAIL. The head is 0006; `SuggestionIn` lacks `aphia_id`; rows are pending.
- [ ] **Step 3: Implement** as specified.
- [ ] **Step 4: Run the tests.**
  - Command: same as Step 2, plus `tests/test_review_routes.py`.
  - Expected: PASS.
- [ ] **Step 5: Commit:** "Review: a WoRMS-checked species joins the list at once (migration 0007)".

### Task 2: Answers with an added species are ordinary answers

**Files:**
- Modify: `services/dashboard/app/review_queue.py` (`_validate`, `submit_verdict`, `_counts`, `_duplicate_verdict`)
- Modify: `services/dashboard/app/static/review-core.js` (`RV.STALE_SPECIES` gains the removed text)
- Test: `tests/test_species_suggestions.py`

**Interfaces:**
- **Consumes:** Task 1 rows (`status='approved'`, `approved_name`, `approved_stage`).
- **Produces in `submit_verdict`**, for a body with `species_suggestion_id`:
  - **A `rejected` suggestion** → `Conflict("that species was removed from the list; pick from the list")`.
  - **A `pending` one** → the #49 behaviour, unchanged.
  - **An `approved` one:**
    - The caller must be the suggester, or an admin. Otherwise `Forbidden("that suggestion isn't yours")`, unchanged.
    - The stored name and stage are `approved_name`/`approved_stage`, validated as a listed choice against `catalogue`.
    - `label_source='suggested'` and the link are kept.
    - Blind answers are allowed. Spot-checks are sampled as for any answer, and no pass-2 item is retired.
    - `counts` is true unless the answer is not-sure.
- **Produces `_counts(name, confidence, pending_suggestion: bool) -> bool`.** Its callers pass whether the linked suggestion is pending.

- [ ] **Step 1: Write the failing tests.**
  - **`test_an_added_species_answer_is_sampled_for_a_second_opinion`.** With `spot_rate=1.0`, answering with one's own approved addition creates a pass-2 item, and `post_verdict` notes it in the pool count.
  - **`test_a_blind_second_opinion_may_add_and_use_a_species`.** A pass-2 item answered with the reviewer's own approved addition is stored.
  - **`test_a_removed_species_cannot_be_answered`** (Review Focus 4). Set the row `rejected`:
    - Posting with its id → 409 with the exact removed text.
    - Posting its name as a plain label after the catalogue refreshes → "pick a species from the list".
  - **`test_pending_rules_still_hold_for_leftovers`.** With `insert_pending`, a blind answer is still refused and no spot-check is sampled.
- [ ] **Step 2: Run them and watch them fail.**
  - Command: `pytest tests/test_species_suggestions.py -k "sampled or blind_second or removed_species or leftovers" -q`
  - Expected: FAIL.
- [ ] **Step 3: Implement** as specified.
- [ ] **Step 4: Run the tests.**
  - Command: `pytest tests/test_species_suggestions.py tests/test_review_queue.py tests/test_review_frames.py -q`
  - Expected: PASS.
- [ ] **Step 5: Commit:** "Review: answers with an added species get second opinions like any other".

### Task 3: An admin removes an addition

**Files:**
- Modify: `services/dashboard/app/species_suggestions.py`, `services/dashboard/app/routes_review.py`
- Test: `tests/test_species_suggestions.py`

**Interfaces:**
- **Produces `async def remove_addition(conn, *, user, suggestion_id: int, body: RejectIn, catalogue, base) -> dict`.**
  - `catalogue` is merged and `base` is the file catalogue. It returns `{"answers": n, "drawn": m}`.
  - **Before the transaction:**
    - Exactly one of `as_label`/`unidentifiable`, else the #49 "say what it is…" Invalid.
    - `as_label` must be a merged-catalogue entry that is not the addition itself, else `Invalid("pick a species from the list")`.
  - **In one transaction**, with the suggestion locked `FOR UPDATE` and no event locks:
    - **Refusals:**
      - a missing row → NotFound "no such species suggestion";
      - not `approved` → `Conflict("this species was already removed")`;
      - `base.entry(approved_name, approved_stage)` exists → `Conflict("this species is now on the Species Log")`.
    - **Answers.** Rewrite every `detection_corrections` row with `corrected_name = approved_name AND life_stage IS NOT DISTINCT FROM approved_stage`, using the #49 rules: the name/stage of X, or `_CANT_IDENTIFY`; `is_endorsement` recomputed; the celebrity kept only for X. Generalise `_name_answers` to take a `WHERE` target rather than duplicating it.
    - **Drawn boxes.** Rewrite `review_added_boxes` rows with `removed_at IS NULL AND label = approved_name AND life_stage IS NOT DISTINCT FROM approved_stage`:
      - to X: `label`, `life_stage`, and `celebrity_id` kept only for X;
      - or to can't-identify: `outcome='unidentifiable'`, `label`/`life_stage`/`celebrity_id` NULL, `confidence='certain'`;
      - and set `updated_at=now()`.
    - **Status.** `_record_decision(status="rejected", rejected_to_*)`.
- **Produces `async def list_additions(conn) -> list[dict]`.** Rows with `aphia_id IS NOT NULL` (reviewer additions), newest first by `created_at`, capped at 200. Each has its columns, `suggested_by_name`, `answers` (DISTINCT events whose answers name the choice, plus live drawn boxes naming it), `worms_url = f"https://www.marinespecies.org/aphia.php?p=taxdetails&id={aphia_id}"`, and ISO times.
- **Produces `async def addition_boxes(conn, *, suggestion_id: int, page: int, per_page: int = 24) -> dict`.** It returns `{"total": n, "boxes": [{"zoom_url", "by"}]}`, with event boxes first (`/api/review/boxes/{event_id}/zoom.jpg`), then drawn boxes (`/api/review/added/{id}/zoom.jpg`), each in a stable order.
- **Produces routes** (all `admin_only`; the POST behind CSRF):
  - `GET /api/review/species-additions` → `{"additions": [...]}`;
  - `GET /api/review/species-additions/{id}/boxes?page=`;
  - `POST /api/review/species-additions/{id}/remove`. It takes a `RejectIn` and passes `catalogue=await _catalogue(conn)` and `base=await state["review_catalogue"].aget()`.
  - The #49 suggestion routes stay for leftover pending rows.

- [ ] **Step 1: Write the failing tests.**
  - **`test_removal_moves_every_answer_naming_the_choice_including_drawn_boxes`** (Review Focus 5). Set up:
    - the adder's linked answer;
    - another reviewer's plain-pick answer;
    - a drawn box with the label;
    - one answer that was the species but was since changed by its reviewer to *Blue Tang*.
    
    Remove "as" *Seriola zonata*. Expect:
    - every naming row moved;
    - the drawn box moved;
    - the changed box's latest answer still *Blue Tang* (`resolve_all`);
    - the choice gone from `merged_catalogue`;
    - `{"answers": 2, "drawn": 1}`.
  - **`test_removal_to_cant_identify_clears_not_sure_and_celebrity`.** `confidence == 'certain'` and `celebrity_id` is None, for both answer and drawn box.
  - **`test_removal_refusals`:**
    - removing twice → "this species was already removed";
    - a choice the file catalogue now lists → "this species is now on the Species Log", with nothing moved;
    - "as" itself → "pick a species from the list".
  - **`test_addition_routes_over_http`:**
    - a reviewer gets 403 on all three routes;
    - the admin lists the addition with `answers` and `worms_url`;
    - boxes page with `total`;
    - remove gives 200;
    - the suggester's `…/mine` shows `status_text == "removed"` and `rejected_to_name`.
- [ ] **Step 2: Run them and watch them fail.**
  - Command: `pytest tests/test_species_suggestions.py -k "removal or addition_routes" -q`
  - Expected: FAIL.
- [ ] **Step 3: Implement** as specified.
- [ ] **Step 4: Run the tests.** Command: `pytest tests/test_species_suggestions.py -q`. Expected: PASS.
- [ ] **Step 5: Commit:** "Review: an admin removes a species a reviewer added".

### Task 4: The page: checking with WoRMS in the "Add a species" form

**Files:**
- Create: `services/dashboard/app/static/review-worms.js`
- Modify: `services/dashboard/app/templates/review.html` (its script tag, after `review-core.js`); `services/dashboard/app/main.py` (the asset-version file list, ~:693)
- Modify: `services/dashboard/app/static/review-core.js`:
  - `RV.suggestSpecies(container, onCreated, onUse)`;
  - `RV.answerExtra`, for an entry carrying `suggestion_id` and not `pending`;
  - the "Species you added" list.
- Modify: `review-single.js` and `review-frame.js`:
  - the link on every item, blind ones included;
  - the link in `answerControls` and `addedPanel`;
  - `onUse` answers with the listed entry;
  - drop the `newSpeciesLast` line and treat a suggested answer like any species answer for `lastAnswer`.
- Modify: `services/dashboard/app/static/tour.css` (`[data-booth="1"]` hides the link); `services/dashboard/app/static/review.css`
- Test: `tests/test_review_page.py` (add `review-worms.js` to `SCRIPTS`, and the page stub loads it)

**Interfaces:**
- **Produces `RV.worms.check({scientific, common}) -> Promise<{status, records, note}>`.**
  - `status` is one of "match" | "choose" | "none" | "genus" | "down".
  - Each record is `{aphia_id, name, authority, family, rank, url}`, always the accepted (valid) record.
  - **A scientific name** calls `GET {BASE}/AphiaRecordsByMatchNames?scientificnames[]=…&marine_only=false`:
    - exact + accepted + rank Species → match;
    - exact + unaccepted → match on `valid_AphiaID`/`valid_name`, with `note = "WoRMS lists this as " + valid_name`;
    - exact + rank not Species → genus;
    - phonetic or near matches → choose (Species only, deduplicated by valid AphiaID, accepted first, ≤5);
    - 204 or empty → `GET {BASE}/AphiaRecordsByName/{name}?like=true&marine_only=false`, giving choose (≤5 Species) or none.
  - **A common name only** calls `GET {BASE}/AphiaRecordsByVernacular/{name}?like=false`, then `like=true` on 204, giving choose (≤5 Species) or none.
  - **A transport error, a non-2xx/204 status, or more than 10 s** (AbortController) → down.
- **Produces `RV.worms.englishName(aphiaId) -> Promise<string|null>`.** It calls `GET {BASE}/AphiaVernacularsByAphiaID/{id}` and returns the first `language_code == "eng"`.
- **The form:**
  - **Fields:** common name (optional), scientific name, the stage `<select>` (unchanged), and note.
  - **Buttons:** "Check with WoRMS" (Enter in a field runs it), then the result area.
  - **Results by status:**
    - match: "✓ *name* authority · family" with a "WoRMS ↗" link;
    - choose: "Did you mean?" radios;
    - none, genus or down: the exact copy.
  - **Pre-check.** For the chosen record and stage, against non-pending `RV.catalogue` entries, the form mirrors the server rules:
    - **already listed** (by AphiaID or name, at the stage, or a stage given for a stage-less species) → "already on the list as {X}" + `Use {X}`, which calls `onUse(entry)`;
    - **staged-only and stage none** → "pick a stage: …, or add a new one" + `Use {stage entry}` per listed stage.
  - **"Add and answer this box"** is enabled only for a chosen record that passes the pre-check. It POSTs:
    - `aphia_id`, `scientific_name: record.name`, `family`;
    - `common_name`: typed, else `englishName`, else the record name;
    - `life_stage`, `note`.
    
    Then it runs `await RV.loadCatalogue()`, then `onCreated(entry)` with the catalogue entry for the returned `key`, plus `suggestion_id`. A 400 or 409 error is shown inline.
- **Produces `RV.answerExtra(entry)`.** For a non-pending entry with `suggestion_id` it returns `{label, life_stage, label_source: "suggested", species_suggestion_id}`. Otherwise it behaves as before.

- [ ] **Step 1: Write the failing tests.** Node tests, with `fetch` stubbed for our API and the WoRMS URLs.
  - `test_an_exact_worms_match_adds_and_answers_the_box`:
    - Expect the WoRMS call, then the POST body with `aphia_id`, then `/api/review/verdicts` with `label_source: "suggested"` and the id.
    - Afterwards `key("m")` offers the new species (mark the rest).
  - `test_a_misspelling_offers_did_you_mean`: picking a radio enables Add.
  - `test_a_common_name_only_uses_the_vernacular_search`.
  - `test_a_synonym_of_a_listed_species_offers_use_it` (Review Focus 2):
    - The "WoRMS lists this as Scarus iseri" text appears.
    - Then "already on the list as …" with a Use button.
    - Clicking it answers with the listed entry, and no POST is made to `/api/review/species-suggestions`.
  - `test_stage_none_for_a_staged_only_species_offers_its_stages`.
  - `test_worms_down_says_so_and_adds_nothing` (Review Focus 3):
    - `fetch` rejects, then the API returns 503.
    - Both show the exact text, and Check is enabled again.
  - `test_a_genus_is_refused`.
  - `test_the_add_link_is_on_blind_items_and_drawn_boxes_but_hidden_in_booth_mode`: the link exists on a pass-2 item and in `addedPanel`, and the CSS rule exists.
  - Update or delete #49 page tests that assumed the old form, keeping their intent where it still applies.
- [ ] **Step 2: Run them and watch them fail.**
  - Command: `pytest tests/test_review_page.py -q`
  - Expected: FAIL.
- [ ] **Step 3: Implement** as specified.
- [ ] **Step 4: Run the tests.**
  - Command: `pytest tests/test_review_page.py tests/test_booth_tours.py -q && node --check services/dashboard/app/static/review-worms.js`
  - Expected: PASS.
- [ ] **Step 5: Commit:** "Review page: a new species is checked against WoRMS".

### Task 5: The page: the admin's "Species added by reviewers" panel

**Files:**
- Modify: `services/dashboard/app/static/review-browse.js` (`speciesPanel` → additions panel; `removeAddition` under the `busy` guard; leftover pending cards are shown only when any exist)
- Modify: `services/dashboard/app/static/review.css`
- Test: `tests/test_review_page.py` (`BROWSE_MUTATING_HANDLERS` gains `"removeAddition"`)

**Interfaces:**
- **Consumes:** Task 3 routes.
- **Produces the panel "Species added by reviewers".** Each addition shows:
  - names and stage, the WoRMS link and AphiaID, who and when, and `answers`;
  - thumbnails 24 at a time;
  - "Remove", with "It's actually…" (an `RV.SpeciesPicker` without pending entries) or "Can't identify", plus a note.
- **Removed additions** go in a collapsed `<details>`.
- **After a successful remove,** the panel reloads `loadAdmin()` and `RV.loadCatalogue()`. A 409 goes through the existing `reloadStale`.
- **The legacy badge** stays as is (it shows only for pending rows, which no longer arise).

- [ ] **Step 1: Write the failing tests.**
  - `test_the_panel_lists_additions_and_removes_one`: Remove "as" a picked species posts `/api/review/species-additions/7/remove` with `as_label`/`as_life_stage`.
  - `test_remove_needs_a_choice`: nothing is posted.
  - `test_a_remove_409_reloads_the_lists`.
- [ ] **Step 2: Run them and watch them fail.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the tests.**
  - Command: `pytest tests/test_review_page.py -q && node --check services/dashboard/app/static/review-browse.js`
  - Expected: PASS.
- [ ] **Step 5: Run it in a browser.** Use the local demo server and playwright, as for #49, with `SHEET_URL=""` so the Sheet poller can't rewrite tracked assets. The browser reaches the **real** WoRMS.
  1. Roy types "Pseudupeneus maculatis", checks, picks *Pseudupeneus maculatus*, and adds it.
  2. Dan's Browse panel lists it with the WoRMS link.
  3. Roy tries "Scarus croicensis" and gets "already on the list" / Use.
  4. Dan removes the goatfish "as" Can't identify.
  5. Roy's "Species you added" says "removed".
  
  Capture screenshots, and confirm there are no page or console errors.
- [ ] **Step 6: Commit:** "Review page: admins see and remove species reviewers added".

### Task 6: Full suite, PR, review, merge, staging check

- [ ] **Step 1: Run everything.** Command: `ruff check . && ruff format --check . && pytest -q`. Expected: all pass except the 3 local `test_gpu_host` failures.
- [ ] **Step 2: Open the PR** with the spec, the plan, the walk, and these notes:
  - the DGX needs `make migrate` (0007);
  - ai #45 must archive `species_suggestions`, including `aphia_id`.
- [ ] **Step 3: Review the branch.** Run a whole-branch review on the most capable model, then one fix wave.
- [ ] **Step 4: Merge on Dan's go.** Watch `deploy-staging`:
  - migrate reports `0007`;
  - staging serves `review-worms.js`;
  - from the laptop, a browser on staging can reach WoRMS (one check in DevTools or the walk script).

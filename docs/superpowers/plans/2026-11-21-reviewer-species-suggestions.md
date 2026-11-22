# Reviewers add species, an admin approves: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Any reviewer can add a species (or a new stage/phase of a listed one) from the picker and answer the box with it at once. An admin approves or rejects it from Browse. Until approval, those answers stay out of training, statistics and public outputs.

**Architecture:**
- A new table `species_suggestions` (migration 0006), linked from `detection_corrections.species_suggestion_id`, holds suggestions.
- A new module `services/dashboard/app/species_suggestions.py` owns creating, listing and deciding suggestions, and the merged catalogue: the file catalogue plus approved suggestions, cached on a per-request version stamp.
- `submit_verdict` learns to answer with a pending suggestion (`label_source='suggested'`, no spot-check).
- A box-level SQL filter `hide_pending_species` joins `hide_pending_spot_checks` everywhere public or training data is read.
- Page JS adds the "Add a species" form (single-box and whole-frame views) and the Browse admin panel.

**Tech Stack:** FastAPI, psycopg 3 (async), Alembic, plain browser JS, pytest (DB tests with `TEST_DATABASE_URL`; node-run page tests via `tests/js_source.py`).

**Spec:** `docs/superpowers/specs/2026-11-21-reviewer-species-suggestions-design.md`

## Global Constraints

- **Stages.** The life stage choices are exactly: none, `juvenile`, `intermediate`, `adult`, `initial phase`, `terminal phase` (`STAGES` below).
- **Duplicates.** Duplicates are judged on name **and** stage. A suggestion's scientific name is mapped with `catalogue.accepted()` before comparing.
- **Provisional name.** A pending answer stores the suggestion's provisional name in `corrected_name` and `corrected_species_id`. That is the accepted scientific name if one was given, otherwise the common name. Its stage is stored in `life_stage`.
- **Who sees pending species.** A pending suggestion is pickable only by its suggester; admins can pick all of them. It never appears in another reviewer's picker.
- **What answering with a suggestion forbids.** It is never a spot-check (no pass-2 sampling), never a blind answer, and never bulk.
- **Hiding is box-level.** A box with any answer linked to a pending suggestion is hidden everywhere `hide_pending_spot_checks` applies, and also:
  - in `labeled_corrections`, for admins too;
  - in agreement pairs;
  - in the pool's verified counts and browse progress.
- **The review UI still shows these answers.** It shows them as "\<common\> (new, waiting for approval)".
- **Not in this version.**
  - No suggestions on boxes a volunteer draws (`review_added_boxes`); the link is hidden there.
  - No mark-the-rest with a suggested species.
- **Settling a decision.** Approval and rejection rewrite the linked answers in place, in one transaction with the suggestion's status change. The suggestion row keeps what was suggested.
- **Copy, verbatim:**
  - link "Not on the list? Add a species";
  - form button "Add and answer this box";
  - picker suffix "(waiting for approval)";
  - review label suffix " (new, waiting for approval)";
  - panel heading "New species (N waiting)";
  - per-box "Not this species";
  - reviewer statuses "waiting for approval" / "added" / "not added".
- **Running tests.** `TEST_DATABASE_URL_FILE=.superpowers/test_dsn /Users/dan/wahoobay/ai/.venv/bin/python -m pytest`, then `ruff check .` and `ruff format --check .`.

## Review Focus

1. **Two reviewers suggest the same species, and the admin approves both.** The second approval is refused ("already listed as …"), so the admin rejects it as "it's actually …". Its answers move to the approved one. Test `test_approving_into_a_listed_choice_is_refused` (Task 5).
2. **The suggester changes a box away from the suggested species while it is pending, then it is approved.** Their newer answer stays the box's answer; only the old linked row is rewritten. Test `test_approval_leaves_a_newer_answer_alone` (Task 5).
3. **A suggestion's scientific name is a synonym that the taxonomy maps to a listed species, at a listed stage.** It is refused as a duplicate. Test `test_a_synonym_of_a_listed_choice_is_refused` (Task 2).
4. **A rejection says "it's actually X", but X is listed only under stages and none was chosen.** It is refused with "pick a species from the list" before anything is written. Test `test_reject_as_a_staged_species_needs_its_stage` (Task 5).
5. **A pending "not sure" answer turns into can't-identify (reject or "Not this species").** Its confidence becomes `certain`, as can't-identify answers require. Test `test_cant_identify_clears_not_sure` (Task 5).

---

### Task 1: Migration 0006 and the answer-identity columns

**Files:**
- Create: `db/migrations/versions/0006_species_suggestions.py`
- Modify: `services/dashboard/app/review_queue.py` (`_VERDICT_IDENTITY_COLUMNS`, `_verdict_identity`)
- Test: `tests/test_migrations.py` (HEAD = "0006"), `tests/test_jobs.py` (the two `"0005"` heads → `"0006"`), `tests/test_review_queue.py` (the existing identity-columns test)

**Interfaces:**
- Produces table `species_suggestions` with these columns:
  - `id bigserial PK`;
  - `common_name text NOT NULL`, `scientific_name text`, `life_stage text`, `note text`;
  - `suggested_by bigint NOT NULL REFERENCES users(id)`, `created_at timestamptz NOT NULL DEFAULT now()`;
  - `status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected'))`;
  - `decided_by bigint REFERENCES users(id)`, `decided_at timestamptz`, `decision_note text`;
  - `approved_name text`, `approved_common text`, `approved_stage text`;
  - `rejected_to_name text`, `rejected_to_stage text`;
  - `location_id text NOT NULL DEFAULT 'wahoobay'`, as 0002 gives the other tables.
- Produces the index `species_suggestions_status_idx (status, created_at)`.
- Produces `detection_corrections.species_suggestion_id bigint REFERENCES species_suggestions(id) ON DELETE RESTRICT`, with the partial index `WHERE species_suggestion_id IS NOT NULL`.
- Produces the `label_source` CHECK, widened to `('model_guess','hint','search','bulk','suggested')`, the same way 0005 widened it.
- Produces `species_suggestion_id` in `_VERDICT_IDENTITY_COLUMNS`. `_verdict_identity(v, outcome)` reads `v["species_suggestion_id"]`; `_validate` returns that key, `None` for every existing path.

- [ ] **Step 1: Write the failing tests.**
  - Set `HEAD = "0006"` and update the two `test_jobs.py` heads.
  - Add `test_species_suggestions_at_head(pg_url)`:
    - After `upgrade head`, a `species_suggestions` row inserts with defaults (`status == 'pending'`).
    - A `detection_corrections` row with `label_source='suggested'` and `species_suggestion_id` set inserts.
    - Deleting the suggestion raises `psycopg.errors.ForeignKeyViolation`.
    - `status='other'` raises `CheckViolation`.
- [ ] **Step 2: Run them and watch them fail.**
  - Command: `pytest tests/test_migrations.py tests/test_jobs.py tests/test_review_queue.py -q`
  - Expected: FAIL (head 0005; the existing identity-columns test fails once the column exists and is unclassified).
- [ ] **Step 3: Implement.**
  - Migration 0006 (revision `"0006"`, down `"0005"`). Its downgrade drops the column, the index and the table, and restores the 0005 CHECK.
  - Add `species_suggestion_id` to the identity columns. `_validate` includes `"species_suggestion_id": None` in its return dict.
- [ ] **Step 4: Run the tests.** Command: the same as Step 2. Expected: PASS.
- [ ] **Step 5: Commit** with the message "Review: species_suggestions table and the suggested label source (migration 0006)".

### Task 2: Suggesting a species, the reviewer's own list, and the merged catalogue

**Files:**
- Create: `services/dashboard/app/species_suggestions.py`
- Modify: `services/dashboard/app/routes_review.py` (`_catalogue()`, `/api/review/species`, two new routes)
- Test: `tests/test_species_suggestions.py` (new; reuse `review_fixtures`: `rdb`, `rclient`, `make_user`, `as_user`, `catalogue_paths`, `login`, `CSRF`)

**Interfaces:**
- Consumes: the Task 1 schema.
- Produces in `species_suggestions.py`:
  - `STAGES: tuple[str | None, ...] = (None, "juvenile", "intermediate", "adult", "initial phase", "terminal phase")`.
  - `class SuggestionIn(BaseModel)` with fields:
    - `common_name: str = Field(..., min_length=1, max_length=120)`;
    - `scientific_name: str | None = Field(None, max_length=120)`;
    - `life_stage: str | None = Field(None, max_length=40)`;
    - `note: str | None = Field(None, max_length=500)`.
  - `def clean_scientific(raw: str | None) -> str | None`. It normalises whitespace and refuses anything not matching `^[A-Z][a-z]+(?: [a-z]+(?:-[a-z]+)?){0,2}$` with `Invalid("write the scientific name as Genus species")`.
  - `def provisional_name(row: dict, catalogue) -> str`: `catalogue.accepted(row["scientific_name"])` if set, else `row["common_name"]`.
  - `async def create_suggestion(conn, *, user, body: SuggestionIn, catalogue) -> dict`. It returns the picker entry for the new suggestion (`entry_json` below) and refuses, in this order:
    1. a stage not in `STAGES`: `Invalid("pick a stage from the list")`;
    2. `catalogue.entry(accepted_sci, stage)` exists: `Conflict(f"already on the list as {entry.common_name or entry.name}")`;
    3. any entry whose `common_name.casefold() == body.common_name.strip().casefold()`: the same message;
    4. a pending suggestion by this user with the same casefolded common name and stage: `Conflict("you already suggested this; it is waiting for approval")`.
  - `def entry_json(row: dict, catalogue) -> dict`. It returns these keys:
    - `key=f"suggestion:{id}"`;
    - `name=provisional_name(row, catalogue)`, `life_stage`;
    - `common=f"{common_name} (waiting for approval)"`;
    - `suggestion_id`, `pending=True`, `in_species_log=False`, `in_classifier=False`.
  - `async def pending_entries(conn, *, user, catalogue) -> list[dict]`: the caller's pending suggestions (an admin gets every pending one), as `entry_json`.
  - `async def list_mine(conn, *, user) -> list[dict]`: newest first. Each has `id`, `common_name`, `scientific_name`, `life_stage`, `status_text` (one of "waiting for approval" | "added" | "not added"), `decision_note`, `rejected_to_name`, `rejected_to_stage`, `approved_common`.
  - `async def merged_catalogue(conn, base) -> Catalogue`. It returns `base` plus one `CatalogueEntry(name=approved_name, life_stage=approved_stage, common_name=approved_common)` per approved suggestion.
    - Taxonomy fields are copied from `base.taxon(approved_name)` when the name is listed.
    - It uses a module cache keyed by `(id(base), count, max(decided_at))` from `SELECT count(*), max(decided_at) FROM species_suggestions WHERE status = 'approved'`.
- Produces in `routes_review.py`:
  - `_catalogue()` returns `merged_catalogue` over the file catalogue. It opens its own connection; every existing caller keeps its signature.
  - `GET /api/review/species` returns `{"species": catalogue.to_json() + await pending_entries(...)}`.
  - `POST /api/review/species-suggestions` (CSRF, `reviewer`) takes a `SuggestionIn` and returns `create_suggestion`'s entry.
  - `GET /api/review/species-suggestions/mine` (`reviewer`) returns `{"suggestions": list_mine(...)}`.

- [ ] **Step 1: Write the failing tests** in `tests/test_species_suggestions.py`.
  - `test_a_suggestion_is_created_pending_with_its_provisional_name`:
    - With a scientific name: the entry's `name` is the accepted name, `pending` is true, and `common` ends "(waiting for approval)".
    - Without one: `name` is the common name.
  - `test_a_listed_choice_is_refused`: the same scientific name and stage as a catalogue entry raises `Conflict` "already on the list as …". The same common name (any case) does too.
  - `test_a_new_stage_of_a_listed_species_is_accepted`: `Pomacanthus paru`, with a stage the fixture catalogue lacks (`"initial phase"`), is created.
  - `test_a_synonym_of_a_listed_choice_is_refused` (Review Focus 3): a raw scientific name the fixture `taxonomy.csv` maps to a listed accepted name, at a listed stage, raises `Conflict`.
  - `test_the_same_reviewer_cannot_suggest_it_twice_while_waiting`: a second create raises `Conflict("you already suggested this; it is waiting for approval")`. Another reviewer's identical suggestion succeeds.
  - `test_stage_and_scientific_name_are_checked`: `life_stage="larva"` gives `Invalid("pick a stage from the list")`, and `scientific_name="seriola"` gives `Invalid("write the scientific name as Genus species")`.
  - `test_pending_entries_belong_to_their_suggester`: reviewer A sees their own pending entry in `GET /api/review/species`, reviewer B does not, and an admin sees both A's and B's.
  - `test_an_approved_suggestion_joins_everyones_list`:
    - Set the row to approved directly in SQL (`approved_name`, `approved_stage`, `approved_common`, `decided_at=now()`).
    - `merged_catalogue` now has `entry(approved_name, approved_stage)`.
    - A second call with the stamp unchanged returns the same object (`is`), and a further approval rebuilds it.
  - `test_suggestion_routes_over_http`:
    - POST without CSRF gives 403; with CSRF it gives 200 and the entry.
    - `GET …/mine` lists it with `status_text == "waiting for approval"`.
- [ ] **Step 2: Run them and watch them fail.**
  - Command: `pytest tests/test_species_suggestions.py -q`
  - Expected: FAIL (ImportError).
- [ ] **Step 3: Implement the module and the routes** as specified. If the fixture `taxonomy.csv` has no synonym row, the synonym test writes one into its own copy of the assets (`write_review_assets` output) before building the catalogue.
- [ ] **Step 4: Run the tests.**
  - Command: `pytest tests/test_species_suggestions.py tests/test_review_routes.py -q`
  - Expected: PASS.
- [ ] **Step 5: Commit** with the message "Review: reviewers suggest species; approved ones join the list".

### Task 3: Answering a box with a pending suggestion

**Files:**
- Modify: `services/dashboard/app/review_queue.py` (`VerdictIn`, `_validate`, `submit_verdict`)
- Test: `tests/test_species_suggestions.py`

**Interfaces:**
- Consumes: `create_suggestion` and `provisional_name` (Task 2).
- Produces: `VerdictIn.label_source: Literal["model_guess","hint","search","suggested"] | None` and `VerdictIn.species_suggestion_id: int | None = None`.
- Produces: `submit_verdict`, when `body.species_suggestion_id` is set, handles the answer as follows.
  - **Loads** the suggestion `FOR UPDATE` and passes it to `_validate(..., suggestion=row)`.
  - **Refuses:**
    - a missing suggestion: `NotFound("no such species suggestion")`;
    - one not pending: `Conflict("that species suggestion was already decided; pick from the list")`;
    - one that isn't the caller's, for a non-admin: `Forbidden("that suggestion isn't yours")`;
    - an outcome other than `species`, or `label_source != "suggested"`: `Invalid("a new species answer needs its suggestion")`, which `label_source="suggested"` without an id also gets;
    - a blind answer: `Invalid("a second opinion can't use a new species")`;
    - bulk: `Invalid("only a listed species can be marked with the rest")`.
  - **Stores:**
    - `name` = `provisional_name(row, catalogue)` and `life_stage` = the suggestion's stage, ignoring `body.label` / `body.life_stage`;
    - `label_source='suggested'` and `species_suggestion_id`.
  - **Skips** spot-check sampling for this answer.

- [ ] **Step 1: Write the failing tests.**
  - `test_answering_with_my_suggestion_stores_the_provisional_name`: the row has `label_source == 'suggested'`, `species_suggestion_id` set, the provisional name, and the suggestion's stage. The pass-1 item is `done`.
  - `test_a_suggested_answer_gets_no_spot_check`: with `spot_rate=1.0`, no pass-2 item is created.
  - `test_someone_elses_suggestion_is_refused_but_an_admin_may_use_it`: `Forbidden` for another reviewer; an admin succeeds.
  - `test_a_decided_suggestion_cannot_be_used`: after setting `status='approved'`, the answer raises `Conflict`.
  - `test_suggested_needs_its_suggestion_and_no_bulk`:
    - `label_source="suggested"` without an id raises `Invalid`.
    - `bulk=True` with an id raises `Invalid`.
  - `test_a_second_opinion_cannot_use_a_suggestion`: a pass-2 item served to the reviewer, answered with their suggestion, raises `Invalid("a second opinion can't use a new species")`.
  - `test_suggested_answer_over_http`: `POST /api/review/verdicts` with `{event_id, outcome:"species", label_source:"suggested", species_suggestion_id}` returns 200.
- [ ] **Step 2: Run them and watch them fail.**
  - Command: `pytest tests/test_species_suggestions.py -k "answer or suggested or second_opinion or decided" -q`
  - Expected: FAIL.
- [ ] **Step 3: Implement** as specified, keeping every existing path unchanged.
- [ ] **Step 4: Run the tests.**
  - Command: `pytest tests/test_species_suggestions.py tests/test_review_queue.py tests/test_review_frames.py -q`
  - Expected: PASS.
- [ ] **Step 5: Commit** with the message "Review: answer a box with a species you suggested".

### Task 4: Keeping pending answers out

**Files:**
- Modify: `services/dashboard/app/exports.py` (new helpers next to `hide_pending_spot_checks`; `labeled_corrections`; the corrections CSV)
- Modify: `services/dashboard/app/public_queries.py`, `services/dashboard/app/main.py` (each `hide_pending_spot_checks(...)` call becomes `hide_pending(...)`)
- Modify: `services/dashboard/app/review_pool.py` (`refresh_progress`)
- Modify: `services/dashboard/app/review_browse.py` (agreement and progress)
- Modify: `services/dashboard/app/data/README.md` (label_source row: `suggested`)
- Modify: `services/dashboard/app/static/review-browse.js` (`how.suggested = "added a new species"`)
- Test: `tests/test_species_suggestions.py`

**Interfaces:**
- Produces in `exports.py`:
  - `def hide_pending_species(alias: str) -> str`: `NOT EXISTS (SELECT 1 FROM detection_corrections pc JOIN species_suggestions ss ON ss.id = pc.species_suggestion_id WHERE pc.event_id = {alias}.event_id AND ss.status = 'pending')`.
  - `def hide_pending(alias: str) -> str`: both conditions, joined with AND.
- Produces in `species_suggestions.py`: `async def pending_species_events(conn, event_ids: list[int] | None = None) -> dict[int, str]`, mapping each event with a pending linked answer to that suggestion's common name.
- Changes:
  - `export_labeled_corrections` always applies `hide_pending_species("c")`, plus `hide_pending_spot_checks("c")` when `not include_identity`.
  - Every other `hide_pending_spot_checks` caller switches to `hide_pending`.
  - `refresh_progress`, browse progress and `agreement_summary`'s input skip events in `pending_species_events`.

- [ ] **Step 1: Write the failing tests.** The setup: a box answered with a pending suggestion whose provisional name is a **listed** species (a new stage of `Seriola zonata`), plus a control box answered normally.
  - `test_pending_boxes_are_left_out_of_the_training_export`: `export_labeled_corrections` rows (with `include_identity=True` too) have no row for the pending box; the control is present.
  - `test_pending_boxes_are_left_out_of_public_and_counts`:
    - The corrections export without identity omits the pending box.
    - `public_queries.LATEST_VERDICT`, run for that event, returns nothing.
    - After `refresh_progress()`, `pool.verified("Seriola zonata")` counts only the control.
  - `test_pending_boxes_are_left_out_of_agreement`: give the pending box a pass-2 answer (inserted directly); `agreement_summary` pairs do not include it.
  - `test_after_approval_the_box_is_back`: approve via SQL, and the box appears in the training export with the approved name (Task 5 does the real approve; here set status only and check the filter lifts).
- [ ] **Step 2: Run them and watch them fail.**
  - Command: `pytest tests/test_species_suggestions.py -k "left_out or back" -q`
  - Expected: FAIL.
- [ ] **Step 3: Implement** as specified.
- [ ] **Step 4: Run the tests.**
  - Command: `pytest tests/test_species_suggestions.py tests/test_privacy_exports.py tests/test_review_browse.py tests/test_public_api.py -q` (use whichever public-API test files exist: `ls tests | grep -i public`)
  - Expected: PASS.
- [ ] **Step 5: Commit** with the message "Review: answers waiting on a new species stay out of training, stats and public data".

### Task 5: Admin decisions (approve, reject, not this species) and the admin list

**Files:**
- Modify: `services/dashboard/app/species_suggestions.py`, `services/dashboard/app/routes_review.py`
- Test: `tests/test_species_suggestions.py`

**Interfaces:**
- Consumes: Tasks 2–4.
- Produces these request models:
  - `class ApproveIn(BaseModel)`: `scientific_name: str` (1–120), `common_name: str` (1–120), `life_stage: str | None`, `note: str | None` (≤500).
  - `class RejectIn(BaseModel)`: `as_label: str | None`, `as_life_stage: str | None`, `unidentifiable: bool = False`, `note: str | None` (≤500).
- Produces `async def approve(conn, *, user, suggestion_id: int, body: ApproveIn, catalogue) -> dict`.
  - It runs in one transaction, with the suggestion `FOR UPDATE`.
  - It refuses a decided suggestion with `Conflict("this suggestion was already decided")`, and a name and stage already listed with `Conflict(f"already listed as {x}; reject it as 'it's actually {x}' instead")`.
  - It updates every linked answer's `corrected_name`/`corrected_species_id`/`life_stage` and recomputes `is_endorsement` (the name equals the event's `catalogue.accepted(best_species_id)`).
  - It sets the status and the `approved_*`/`decided_*` fields, and returns `{"answers": n}`.
- Produces `async def reject(conn, *, user, suggestion_id: int, body: RejectIn, catalogue) -> dict`.
  - It requires exactly one of `as_label` and `unidentifiable`, else `Invalid("say what it is, or that it can't be identified")`.
  - `as_label` must satisfy `catalogue.entry(as_label, as_life_stage)`, else `Invalid("pick a species from the list")`.
  - Linked answers move to that name and stage. For can't-identify they get `unidentifiable=true`, `corrected_name`/`corrected_species_id`/`life_stage` NULL and `confidence='certain'`.
  - It sets `rejected_to_*` and the `decided_*` fields.
- Produces `async def detach(conn, *, user, suggestion_id: int, event_id: int) -> dict`. For a pending suggestion, the linked answers on that event become can't-identify (as above) with `species_suggestion_id = NULL`.
- Produces `async def list_for_admin(conn, *, status: Literal["pending","decided"]) -> list[dict]`. It returns each suggestion's fields, `suggested_by_name`, `answers` (count) and `created_at`, newest first, with decided ones capped at 50.
- Produces `async def suggestion_boxes(conn, *, suggestion_id: int, page: int, per_page: int = 24) -> dict`, returning `{"total": n, "boxes": [{"event_id", "zoom_url": f"/api/review/boxes/{event_id}/zoom.jpg", "by"}]}`.
- Produces these routes (all `admin_only`; the POSTs behind CSRF):
  - `GET /api/review/species-suggestions?status=pending|decided` returns `{"suggestions": [...], "pending": n}`;
  - `GET /api/review/species-suggestions/{id}/boxes?page=`;
  - `POST …/{id}/approve`, `POST …/{id}/reject`, `POST …/{id}/detach` (`{"event_id": int}`).
  - Approve and reject do not touch the pool's in-memory counts; its next `refresh_progress` picks the change up.

- [ ] **Step 1: Write the failing tests.**
  - `test_approve_settles_every_linked_answer`: two boxes answered with the suggestion. After approving with a corrected scientific name and stage, both rows carry them, the suggestion is `approved`, and `merged_catalogue` lists the choice.
  - `test_approving_into_a_listed_choice_is_refused` (Review Focus 1): two reviewers suggest the same new species. Approving the first succeeds and approving the second raises `Conflict` "already listed as …". Rejecting the second "as" the first moves its answers.
  - `test_approval_leaves_a_newer_answer_alone` (Review Focus 2): the suggester answers a box with the suggestion, then re-answers it with a listed species. After approval, `resolve_all` gives the listed species for that box.
  - `test_reject_as_a_listed_species_moves_the_answers` and `test_reject_as_cant_identify`.
  - `test_reject_as_a_staged_species_needs_its_stage` (Review Focus 4): `as_label="Pomacanthus paru"` with no stage raises `Invalid("pick a species from the list")`, and nothing changes.
  - `test_cant_identify_clears_not_sure` (Review Focus 5): a pending answer given with `not_sure=True` comes out with `confidence == 'certain'` after `reject(unidentifiable=True)` and after `detach`.
  - `test_not_this_species_unlinks_one_box`: detach one of two boxes; approving afterwards rewrites only the other.
  - `test_a_decided_suggestion_cannot_be_decided_again`.
  - `test_admin_routes_over_http`:
    - A reviewer gets 403 on all of them.
    - An admin lists `pending == 1`, pages boxes (`total`, `zoom_url`) and approves with 200.
    - `…/mine` for the suggester then shows `status_text == "added"`.
- [ ] **Step 2: Run them and watch them fail.**
  - Command: `pytest tests/test_species_suggestions.py -k "approve or reject or detach or decided or admin or not_this or newer or staged or not_sure" -q`
  - Expected: FAIL.
- [ ] **Step 3: Implement** as specified.
- [ ] **Step 4: Run the tests.**
  - Command: `pytest tests/test_species_suggestions.py -q`
  - Expected: PASS.
- [ ] **Step 5: Commit** with the message "Review: admins approve or reject suggested species".

### Task 6: Pending answers in the review UI's data

**Files:**
- Modify: `services/dashboard/app/review_frames.py` (`frame_state`), `services/dashboard/app/review_browse.py` (the browse grid's `label`/`common`)
- Test: `tests/test_species_suggestions.py`

**Interfaces:**
- Consumes: `pending_species_events` (Task 4).
- Produces:
  - In `frame_state`, an answered box whose event is in `pending_species_events` has `common = f"{common_name} (new, waiting for approval)"` and `pending_species = True`; every other box has `pending_species = False`.
  - The browse grid entries for such events carry the same `common` text.

- [ ] **Step 1: Write the failing tests.**
  - `test_the_frame_names_a_pending_species`.
  - `test_browse_names_a_pending_species`.
  - Each asserts the exact suffix " (new, waiting for approval)", and that it is gone after approval.
- [ ] **Step 2: Run them and watch them fail.** Expected: FAIL (KeyError, or a different `common`).
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the tests.**
  - Command: `pytest tests/test_species_suggestions.py tests/test_review_frames.py tests/test_review_browse.py -q`
  - Expected: PASS.
- [ ] **Step 5: Commit** with the message "Review: the frame and browse name a species waiting for approval".

### Task 7: The page: "Add a species" for reviewers

**Files:**
- Modify: `services/dashboard/app/static/review-core.js` (a shared `RV.suggestSpecies(container, onCreated)` form)
- Modify: `services/dashboard/app/static/review-single.js` (link under the picker on non-blind items; pick of a pending entry)
- Modify: `services/dashboard/app/static/review-frame.js` (link in `answerControls` only, so never for drawn boxes; pending picks; no `lastAnswer` for suggested answers)
- Modify: `services/dashboard/app/static/review.css` (the form)
- Test: `tests/test_review_page.py` (node-run, reuse `FRAME_STUB`; add a minimal single-view stub if needed)

**Interfaces:**
- Consumes the Task 2 and Task 3 routes.
- Produces `RV.suggestSpecies(container: Element, onCreated: (entry) => void)`. It renders:
  - the fields common name (required), scientific name, a stage `<select>` from the fixed list, and a note;
  - the button "Add and answer this box";
  - "Your suggestions", a list from `GET /api/review/species-suggestions/mine` with each `status_text` and, for "not added", what it was moved to and the note.
  - On submit it POSTs, then calls `await RV.loadCatalogue()` and `onCreated(entry)`. On a 409 or 400 it shows the server's error inline.
- Produces `RV.answerExtra(entry)`, used by both views when an entry is picked. It returns:
  - for a pending entry: `{label: entry.name, life_stage: entry.life_stage, label_source: "suggested", species_suggestion_id: entry.suggestion_id}`;
  - otherwise the existing `{label, life_stage, label_source}`.

- [ ] **Step 1: Write the failing tests.**
  - `test_adding_a_species_answers_the_frame_box_with_it`:
    - With FRAME_STUB plus fetch routes for `POST /api/review/species-suggestions` (returning an entry with `suggestion_id: 9`) and `…/mine`, open the "Add a species" form for box 1 and submit.
    - The posts are the suggestion, then `/api/review/verdicts` with `label_source: "suggested"` and `species_suggestion_id: 9`.
    - After it, `key("m")` returns false (no `lastAnswer`).
  - `test_drawn_boxes_have_no_add_a_species_link`: the `addedPanel` source does not reference `suggestSpecies`.
  - `test_picking_a_pending_entry_answers_with_its_suggestion`: `RV.answerExtra({pending: true, suggestion_id: 4, name: "x", life_stage: null})` has `species_suggestion_id: 4`.
  - `test_the_form_shows_the_server_refusal`: a 409 `{"error": "already on the list as Bluefish"}` leaves that text in the form, and no verdict is posted.
- [ ] **Step 2: Run them and watch them fail.**
  - Command: `pytest tests/test_review_page.py -q`
  - Expected: FAIL.
- [ ] **Step 3: Implement.**
  - The form opens from the link "Not on the list? Add a species" placed after the picker.
  - It is hidden on blind (pass-2) single-view items and in `addedPanel`.
- [ ] **Step 4: Run the tests.**
  - Command: `pytest tests/test_review_page.py tests/test_booth_tours.py -q && node --check services/dashboard/app/static/review-core.js`
  - Expected: PASS.
- [ ] **Step 5: Commit** with the message "Review page: add a species from the picker and answer with it".

### Task 8: The page: the admin's "New species" panel and badge

**Files:**
- Modify: `services/dashboard/app/static/review-browse.js` (`speciesPanel(list)` in `loadAdmin`, before disputes; approve/reject/detach under the existing `busy` guard)
- Modify: `services/dashboard/app/static/review-core.js` (on load, for admins, fetch the pending count and set the Browse button's badge)
- Modify: `services/dashboard/app/static/review.css`
- Test: `tests/test_review_page.py`

**Interfaces:**
- Consumes the Task 5 routes.
- Produces:
  - `speciesPanel` renders "New species (N waiting)". Each pending suggestion shows:
    - its names, stage, note, who and when, and the answer count;
    - thumbnails paged 24 at a time, each with a "Not this species" button;
    - an Approve form (scientific name prefilled from the suggestion, common name, stage `<select>`);
    - Reject, with "It's actually…" (an `RV.SpeciesPicker`) or "Can't identify", and a note.
  - Decided suggestions appear in a collapsed `<details>` list.
  - The Browse nav button gains `<span class="rv-badge">N</span>` when N > 0 (admins only).

- [ ] **Step 1: Write the failing tests** (node-run against `review-core.js` + `review-browse.js` with a stubbed fetch).
  - `test_the_panel_lists_pending_species_and_approves`: Approve posts `/api/review/species-suggestions/3/approve` with the form's values.
  - `test_not_this_species_posts_detach`.
  - `test_reject_needs_a_choice`: clicking Reject with neither option chosen posts nothing.
  - Add `"approveSuggestion"`, `"rejectSuggestion"` and `"detachBox"` to `BROWSE_MUTATING_HANDLERS`.
- [ ] **Step 2: Run them and watch them fail.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the tests.**
  - Command: `pytest tests/test_review_page.py -q && node --check services/dashboard/app/static/review-browse.js`
  - Expected: PASS.
- [ ] **Step 5: Run it in a browser** (playwright-core in the session scratchpad, local demo DB as for PR #46):
  1. Reviewer adds "Spotted Goatfish" while answering a box.
  2. The admin sees "New species (1 waiting)" and the badge.
  3. The admin approves it.
  4. The reviewer's picker now lists it without the suffix.
  - Take screenshots for the PR.
- [ ] **Step 6: Commit** with the message "Review page: admins approve new species from Browse".

### Task 9: Full suite, PR, review, merge, staging check

- [ ] **Step 1: Run everything.** Command: `ruff check . && ruff format --check . && pytest -q`. Expected: all pass, except the three local-environment `test_gpu_host` failures that also fail on main.
- [ ] **Step 2: Open the PR.** Push and open it with the spec, the plan, the screenshots, and the label-archive note (ai #45 must add `hide_pending_species` and archive `species_suggestions` if it merges after this).
- [ ] **Step 3: Review the branch.** Run a final whole-branch review and one fix pass.
- [ ] **Step 4: Merge on Dan's go.** Watch `deploy-staging`: the migrate step reports revision `0006`, and staging serves `review-browse.js` containing `species-suggestions`.

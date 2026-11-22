# Reviewers add species; an admin approves

2026-11-21 · design · approach B, chosen by Dan

## Why

Roy, a volunteer reviewer and CamOp moderator, found fish he could not label, because the
species wasn't on the pick list. He asked: "I can't see where I can add new species." The list
is built from the CamOps' Species Log spreadsheet plus our tracked additions file
(`assets/species_additions.csv`), baked into the dashboard image. Today only a code change and
a redeploy can add a species.

Dan's decisions:
- Any reviewer can add a species.
- An admin must approve it.
- **The reviewer answers the box with the new species straight away and keeps going (B).**
  Those answers stay out of training data and statistics until approved. If the species is
  rejected, the admin says what the fish is, and the answers move to that.

Model training is the current priority, so two things matter: the reviewer keeps their pace,
and nothing unapproved leaks into training data.

## Goals

- From the species picker (single-box and whole-frame views), "Not on the list? Add a species"
  opens a short form. Its fields:
  - common name, required;
  - scientific name, optional;
  - life stage or phase, optional, chosen from a fixed list: none, juvenile, intermediate,
    adult, initial phase, terminal phase;
  - a note, optional.
- A suggestion can be a new species, or a new stage or phase of a listed one (*my call*). For
  example "Striped Parrotfish Juvi": *Scarus iseri* is listed, but not its juvenile.
- Submitting the form creates a pending suggestion and answers the current box with it.
- The new species is pickable by the reviewer who suggested it (and by admins) for further boxes
  while it waits. It is marked "waiting for approval".
- On Browse's admin side, a "New species" panel lists pending suggestions. Each shows who
  suggested it and when, the note, and thumbnails of every box answered with it.
- The admin can **approve** it. They confirm or correct the scientific name, common name and
  stage. The species then joins everyone's list at once (no redeploy), and the waiting answers
  become ordinary answers.
- Or the admin can **reject** it with a short note, choosing either "it's actually \<a listed
  species\>", which moves the answers to that species, or "can't identify", which turns them
  into can't-identify answers.
- The suggester can see what happened to each of their suggestions.

## Non-goals

- No edits to the CamOps' spreadsheet, the additions file, or the classifier's classes.
  Approved species live in the database.
- No WoRMS lookup at approval in this version. The admin types the accepted scientific name; a
  link to WoRMS search is enough.
- No mark-the-rest with a species that is still waiting.
- No email or other push notification to admins.
- No suggestions on boxes a volunteer draws in the whole-frame view (*my call*). Those are saved
  in `review_added_boxes`, which would need its own link, rewrite and exclusions. The "Add a
  species" link is hidden there; a drawn box of an unlisted fish gets "Can't identify" for now.

## Anti-goals

- A pending answer must never reach any of these:
  - the training export (`labeled_corrections`) or the corrections CSV;
  - the label archive;
  - the public API and public pages;
  - the spot-check agreement statistics;
  - the review pool's per-species verified counts.
- A pending species must never appear in another reviewer's picker.
- The suggestion must never become a way around the blind second opinion. A blind
  (pass-2) answer cannot use or create a suggestion.
- Rejecting must never lose the boxes. Every answer given with a rejected suggestion ends up
  either moved to a listed species or turned into can't-identify.

## Approaches

- **A. Set the box aside until approval.** Simplest, and nothing unapproved becomes a label.
  The cost is that the reviewer must come back to each box.
- **B. Answer now, settle later (chosen).** The reviewer keeps going, and the answers are
  held as pending. It has more moving parts: the exclusions and the rejection remap.

## Design

### Data (migration 0006)

- **New table `species_suggestions`.** Columns:
  - `id`;
  - `common_name` (required), `scientific_name` (nullable), `life_stage` (nullable), `note`;
  - `suggested_by` (users), `created_at`;
  - `status`, one of `pending`, `approved` or `rejected`;
  - `decided_by`, `decided_at`, `decision_note`;
  - the approved `approved_name`, `approved_common` and `approved_stage`;
  - for a rejection, `rejected_to_name` and `rejected_to_stage` (both null = can't identify);
  - `location_id`.
- **`detection_corrections.species_suggestion_id`.** Nullable, references `species_suggestions`,
  `ON DELETE RESTRICT`.
- **`label_source` gains `'suggested'`.** This widens the CHECK again, as 0005 did.
- **The stored name while pending.** A pending answer stores the suggestion's scientific name in
  `corrected_name` and `corrected_species_id` if one was given, otherwise its common name. That
  is a provisional name, replaced on decision.

### Approving and rejecting (my call: settle in place)

- **On approval,** in one transaction:
  - every answer linked to the suggestion gets `corrected_name` / `corrected_species_id` = the
    approved scientific name and `life_stage` = the approved stage;
  - the suggestion becomes `approved`.
- **On rejection,** the linked answers get one of two outcomes:
  - "it's actually X": X's name and stage;
  - can't identify: `unidentifiable = true` and the name cleared.
  `label_source` stays `'suggested'` and the link stays. The suggestion row keeps what was
  originally suggested, so the audit trail is that row.
- **Why update in place.** Answers are otherwise append-only, but these rows were provisional
  by design. Adjudication rows would mean "an admin settled a disagreement", which is not this.

### The species list

- **How approved species join the list.** The catalogue becomes the file-built catalogue plus
  every approved suggestion, as an entry with name, stage and common name, marked as a reviewer
  addition.
- **Keeping every instance current.** The dashboard keeps no state between requests and may run
  several instances, so each request that needs the catalogue checks a cheap version stamp (the
  count and latest `decided_at` of approved suggestions). It rebuilds the merged catalogue when
  the stamp changes.
- **Every consumer gets the merged list.** That covers verdict validation, `/api/review/species`,
  mark-the-rest, the review pool's species order, and the frame and browse names. The builder
  routes them all through one accessor.
- **The suggester's picker.** For the suggester (and admins), `/api/review/species` also returns
  that person's own pending suggestions. They carry `suggestion_id` and `pending: true`, and the
  picker shows them with "waiting for approval".
- **Refusing duplicates.** Duplicates are judged on name and stage together, not name alone. A
  suggestion is refused with a clear message, naming the listed choice, in three cases:
  - its (scientific name after the taxonomy mapping, stage) is already a choice;
  - its common name (case-insensitive) is already a choice's common name;
  - it is already pending from the same person.
- **An approved stage joins a listed species.** It merges into that species: it becomes one
  more `stages()` value, so "Looks right" on that species asks for a stage when no stage-less
  entry exists, as for angelfish today.
- **Approving into an existing choice is refused.** If the admin's corrected name and stage
  turn out to be a choice already listed, approval is refused with "already listed as X; reject
  as 'it's actually X' instead".

### Answering with a suggestion

- **Request fields.** `VerdictIn` gains `species_suggestion_id: int | None`. With it, the answer
  must be `outcome="species"` and `label_source="suggested"`.
- **Server checks.**
  - The suggestion must be pending and the caller's own (admins may use anyone's).
  - The answer must not be blind.
  - `bulk` is not allowed.
- **Recording the answer.** `submit_verdict` stores the provisional name and the link, and
  closes the queue item as usual. It does **not** sample a spot-check for these answers: the
  admin's approval, with every box shown, is their check.
- **One-step flow.** The page can create the suggestion and answer the box in one action:
  POST the suggestion, then the verdict.

### Kept out while pending

- **One shared filter, box-level (*my call*).** A helper `hide_pending_species(alias)`, the
  sibling of `hide_pending_spot_checks` in `exports.py`, hides the whole box while any of its
  answers is linked to a pending suggestion, exactly as spot-checks hide the whole box. This is
  not answer-by-answer hiding, which would make every "latest answer" reader show the answer
  the reviewer replaced (`public_queries.LATEST_VERDICT`, `NOT_A_FISH_TRACKS`, `resolve()`).
  - The cost: a box whose pending answer was later superseded stays hidden until the decision.
    That is rare.
- **Where it applies.** Everywhere `hide_pending_spot_checks` is applied (public API,
  sightings, corrections CSV), and also:
  - in the training export (`labeled_corrections`), for admins too;
  - in agreement pairs (`review_labels`): boxes with a pending answer are left out;
  - in the pool's verified counts: the same rule, including `refresh_progress`.
- **Label archive.** Open PR ai #45 (the label archive) must use it too, and must also archive
  `species_suggestions` (*my call*). Staging's database is a demo copy that a reset loses; the
  archive is where Roy's work survives, and his archived answers name approved species. Whichever
  of #45 and this merges second adds both.
- **What the review UI shows.** It does include pending answers: the box counts as answered,
  the queue does not serve it again, and the frame and browse show "\<common name\> (new,
  waiting for approval)".

### Admin panel

- **A Browse admin panel, "New species (N waiting)".** Each suggestion shows:
  - the suggested names, stage and note, with who suggested it and when;
  - the number of answers and the thumbnails of **every** box answered with it, paged 24 at a
    time (`/api/review/boxes/{event_id}/zoom.jpg`; *my call*: the approval is these answers'
    only check, since they get no spot-check);
  - a per-box "Not this species" action on each thumbnail, which turns that one answer into
    can't-identify and unlinks it, before approving the rest (*my call*);
  - an Approve form with the names and stage prefilled and editable, the scientific name
    required;
  - Reject, with "It's actually" (the species picker) or "Can't identify", and a note.
- **Decided suggestions.** These collapse into a short history.
- **Badge.** For admins, the Browse button shows a count badge when any suggestion is waiting.
- **Admin routes.**
  - `GET /api/review/species-suggestions?status=pending|decided` (admin);
  - `POST /api/review/species-suggestions/{id}/approve`;
  - `POST /api/review/species-suggestions/{id}/reject`;
  - `POST /api/review/species-suggestions/{id}/detach` with `{event_id}` (the per-box action).
- **Reviewer routes.**
  - `POST /api/review/species-suggestions` creates one;
  - `GET /api/review/species-suggestions/mine` lists the caller's own suggestions with
    their status and decision note.
- All writes go behind CSRF; the admin routes use `admin_only`.

## Constraints

- Tests first: `ruff check`, `ruff format --check` and `pytest` with `TEST_DATABASE_URL`. The
  `test_migrations` head moves to 0006, as do the head checks in `test_jobs`.
- Browser JS has no harness. Page behaviour is tested by node-run tests with a stubbed fetch
  (`tests/js_source.py`, as in `test_review_page.py`).
- Merging to main deploys staging, where Roy reviews. The migration runs before the new
  dashboard serves.
- This builds on PR #48 (parrotfish phases) once it is merged.

## Left to the builder

- The exact form layout, wording, and where the "Add a species" link sits under the picker in
  each view.
- How the merged-catalogue cache is keyed and invalidated, within the per-request stamp rule
  above.
- Splitting the work into tasks, and the test layout.

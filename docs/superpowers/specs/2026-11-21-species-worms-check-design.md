# Reviewer-added species: checked against WoRMS, no admin approval

2026-11-21 · design · approach A, chosen by Dan · builds on ai #49 (`2026-11-21-reviewer-species-suggestions-design.md`)

## Why

ai #49 (merged 2026-11-21) lets any reviewer add a species from the picker. An admin must approve it before it joins everyone's list. Until then, its answers are held out of training, statistics and public data.

Dan wants the approval step gone. When a reviewer adds a species, the tool checks it against the WoRMS register instead:
- If WoRMS has the name, the species goes onto everyone's list at once.
- If WoRMS doesn't have it, the reviewer is shown the closest matches to choose from.

Dan's decisions:
- No admin approval. The WoRMS check replaces it.
- **Answers with a reviewer-added species go back into the normal second-opinion sampling.** A blind spot-check is sampled like any other answer. In #49 they skipped it, because the admin's approval was their check. WoRMS confirms only that the name is a real species. It cannot confirm that the fish in the box is that species; the second opinions do that.
- **Approach A:** the lookup runs in the reviewer's browser. Staging's dashboard has no internet access (its VPC has no NAT, by design), but WoRMS's REST API allows browser calls. CORS is open: `access-control-allow-origin: *`, checked 2026-11-21. No Content-Security-Policy on the dashboard blocks it.

## What WoRMS gives us (checked 2026-11-21)

Base URL: `https://www.marinespecies.org/rest`

- **`AphiaRecordsByMatchNames?scientificnames[]=…&marine_only=false`** (TAXAMATCH, fuzzy). `scripts/enrich_taxonomy_from_worms.py` already uses it.
  - A misspelling: "Pseudupeneus maculatis" → *Pseudupeneus maculatus*, accepted, `match_type` "phonetic".
  - A synonym: "Scarus croicensis" → status "unaccepted", `valid_name` *Scarus iseri*, `match_type` "exact".
- **`AphiaRecordsByVernacular/{name}?like=false`** (or `like=true`): "spotted goatfish" → *Pseudupeneus maculatus*.
- **`AphiaVernacularsByAphiaID/{id}`**: a record's common names, by language.
- **`AphiaRecordsByName/{name}?like=true`**: names that start with the text typed.
- Each record carries:
  - `AphiaID`, `scientificname`, `authority`;
  - `status`, `valid_AphiaID`, `valid_name`;
  - `rank`, `family`, `match_type`, `url`.

## Goals

### The form

The form appears wherever the species picker appears:
- the single-box view, including blind second opinions;
- the whole-frame view, including boxes a volunteer draws.

The link stays "Not on the list? Add a species". The fields are:
- **common name:** now optional;
- **scientific name;**
- **stage or phase:** the same fixed list;
- **note.**

At least one of the two names is required.

### "Check with WoRMS"

"Check with WoRMS" asks WoRMS from the browser, and the form shows the result.

- **A scientific name was given.** The form uses TAXAMATCH.
  - **Exact match, accepted.** The form shows "✓ *Name* Authority · Family", with a link to the record on WoRMS.
  - **Exact match, not accepted (a synonym).** The form says "WoRMS lists this as *valid name*" and uses the valid name's record.
  - **Fuzzy match** (phonetic or near). The form asks "Did you mean …?" and lists up to 5 candidates, accepted ones first. The reviewer clicks one.
  - **No match.** The form falls back to names that start the same way (`AphiaRecordsByName … like=true`, up to 5) and shows them as "Did you mean …?".
    - If that finds nothing too, the form says: "WoRMS doesn't know this name. Check the spelling, or answer Can't identify for now."
- **Only a common name was given.** The form tries the vernacular search, first `like=false`, then `like=true`. It shows up to 5 candidates as "Did you mean …?", even when there is exactly one, so the reviewer confirms the species.
- **Rank.** Only a record of rank Species can be added (*my call*). Picking a genus or a higher rank gives "pick a species, not a genus". "Can't identify" covers fish known only to genus, and the training classes are species.
- **Common name.** If the reviewer left it blank, the form fills it from the record's English common name (`AphiaVernacularsByAphiaID`). If WoRMS has none, the scientific name is used.

### "Add and answer this box"

"Add and answer this box" sends the chosen WoRMS record's details to the server:
- `aphia_id`, the accepted scientific name and `family`;
- the common name, stage and note.

The server runs its own list rules:
- the stage list;
- the name shape;
- duplicates judged on species and stage (X1, as in #49).

If they pass, the server adds the species to everyone's list at once, and the page answers the box with it as a normal pick.

If the species is already on the list, the server answers "already on the list as X", and the form offers "Use X" to answer with it (*my call*).

**How a listed species is staged can't be changed by one reviewer (Dan, option a).** Under #49 the admin caught these two cases; now the server refuses them:
- **A plain entry (stage "none") for a species that is listed only with stages.** This covers the parrotfish and wrasse phases from #48 and *Holacanthus ciliaris* (juvenile only), 19 species in all. The refusal is "pick a stage: \<its listed stages\>, or add a new one", and the form shows those stages as "Use …" buttons.
  - Why: a plain entry would make "Looks right" stop asking for the phase, for everyone.
- **A stage for a species that is listed without one.** For example "Sergeant Major" plus "adult". The refusal is "already on the list as \<X\>", with "Use X".
  - Why: it would split one fish's answers between two choices.

**Still allowed:** a new stage of a species that is already listed with stages, such as a juvenile of a parrotfish listed by phase. A brand-new species may carry any stage.

Changing how a listed species is staged goes through the CamOps Species Log or Dan.

**Booth mode** (`?booth=1`, a shared visitor account) hides "Add a species" in the browser, the same way it hides drawing and Browse (*my call*).

**Names are written the way WoRMS writes them.** That includes a subgenus in parentheses, e.g. *Charybdis (Charybdis) hellerii*, which the catalogue already uses. The server's name-shape check accepts it (*my call*).

**Duplicate check by AphiaID.** An addition whose `aphia_id` matches a listed entry's AphiaID is judged against that entry, as if the names matched (*my call*).

If WoRMS can't be reached, the form says "WoRMS isn't answering right now; try again in a minute, or answer Can't identify for now." Nothing is added without a WoRMS record.

### After the species is added

- The species is an ordinary choice for everyone.
- **Answers that use it.** It can be picked like any other choice, including:
  - on blind second opinions, which may now add species too, since nothing waits any more;
  - in "mark the rest" (M);
  - in an admin's dispute settlement.
- **Second opinions.** Spot-checks are sampled for these answers exactly as for any other.
- **"Species you added".** It replaces "Your suggestions" in the form. It lists each species the reviewer added, with its status:
  - "added";
  - or "removed", with what its answers were moved to and the admin's note.

### Admin: Browse panel "Species added by reviewers"

The panel replaces "New species (N waiting)". It lists additions newest first. Each shows:
- the names and stage, plus the WoRMS record (link and AphiaID);
- who added it and when;
- the number of answers that use it, and thumbnails 24 at a time.

The admin can **remove** an addition with "It's actually …" (a listed species) or "Can't identify", plus a note.
- Every answer with that species and stage moves, whoever gave it. This is the #49 reject rewrite, widened from "answers linked to the suggestion" to "answers naming this choice".
- **Boxes a volunteer drew (`review_added_boxes`) move too** (*my call*). They store `label`/`life_stage` with no link column, so they are matched by name and stage like the rest. The panel's count and thumbnails include them (`/api/review/added/{id}/zoom.jpg`). There is no new link column.
- The species leaves the list.

There is no badge, because nothing waits on the admin (*my call*). There is no per-box "Not this species" either. The ordinary review tools fix single boxes (*my call*).

## Non-goals

- **No server-side WoRMS verification (approach B).** The server trusts the record the browser sends. The form can be reached only by invited, logged-in reviewers, and the second opinions catch a wrong species.
- **No regional limit.** Any species WoRMS accepts can be added, including one that never comes near Florida. Dan accepted this when the point was raised.
- **No editing an added species' names.** Remove it and add it again instead.
- **No WoRMS lookup for choices already on the list.**

## Anti-goals

- No species is added without a WoRMS record the reviewer chose. The server requires `aphia_id` and a species-shaped accepted name.
- No duplicate of a listed choice, judged on species and stage.
- Answers with an added species never skip second-opinion sampling.
- Removing an addition never loses a box. Every answer that used it moves to the chosen species or to can't-identify.

## Design

### Data (migration 0007)

- **New columns on `species_suggestions`:**
  - `aphia_id integer`;
  - `family text`.
- **New rows** are inserted already `status = 'approved'`, with:
  - `approved_name` = the WoRMS accepted name;
  - `approved_common` and `approved_stage` from the form;
  - `decided_at = now()`, so the merged-catalogue stamp changes;
  - `decided_by` NULL;
  - `decision_note` "WoRMS AphiaID n".
- **Removal** sets:
  - `status = 'rejected'`;
  - `rejected_to_name` and `rejected_to_stage`;
  - `decided_by` = the admin, `decided_at`, `decision_note`.
  
  The approved count drops, so the stamp changes and every instance drops the species on its next request.
- **Catalogue entries** for additions carry `aphia_id`, `family`, `rank = Species` and `resolved = true`.
- **The answer that adds the species** keeps `label_source = 'suggested'` and its `species_suggestion_id`. Browse keeps showing "added a new species" for it. Later picks by anyone are ordinary `search`/`hint` answers.

### What changes from #49

**Server**
- **Creating a suggestion.**
  - `create_suggestion` takes `aphia_id` (required) and `family`, and stores the row as approved.
  - Its refusals (stage, name shape, duplicates X1) stay, and it gains the two staging refusals above and the AphiaID duplicate check. Rule 4, "you already suggested this; it is waiting for approval", goes away.
- **`submit_verdict`.**
  - The "no spot-check" and "retire pass-2 on a change to a suggested answer" rules apply only while the suggestion is pending. New rows are never pending.
  - "A second opinion can't use a new species" and "only a listed species can be marked with the rest" likewise apply only to pending suggestions.
- **Removal.** `reject` becomes removal of an approved addition:
  - It moves every `detection_corrections` row whose current `corrected_name` and `life_stage` equal the addition's.
  - It runs in one transaction with the status change, and keeps the #49 rewrite rules: can't-identify sets `confidence = 'certain'`; the celebrity is cleared unless it belongs to the new species; `is_endorsement` is recomputed.
  - **If the same choice has since appeared in the file catalogue** (the CamOps Species Log gained it), removal refuses with "this species is now on the Species Log", and moves nothing (*my call*).
- **What stays.** `approve` and `detach` stay only for pending rows left over from #49. None exist on staging (checked 2026-11-21: 0 suggestions, 0 suggested answers).
- **The pending filters stay as a safety net.** These are `hide_pending_species`, `pending_species_events` and `pending_shown_answers`, and new rows never trigger them.

**Page**
- The "(waiting for approval)" and "(new, waiting for approval)" texts no longer appear for new additions.
- The "A new species can't be marked on the rest until it's approved" line goes.
- The picker's `pending` handling stays only for leftovers.

### The form, sketched

```
Not on the list? Add a species
  Common name    [ spotted goatfish           ]  (optional)
  Scientific name[ Pseudupeneus maculatis     ]
  Stage or phase [ none ▾ ]     Note [                 ]
  [ Check with WoRMS ]

  Did you mean?
   ( ) Pseudupeneus maculatus (Bloch, 1793) · Mullidae   WoRMS ↗
   ( ) Pseudupeneus prayensis (Cuvier, 1829) · Mullidae  WoRMS ↗
  [ Add and answer this box ]
```

## Constraints

- **Tests first.** Run `ruff check`, `ruff format --check`, and `pytest` with `TEST_DATABASE_URL`. `test_migrations` and the `test_jobs` heads move to 0007.
- **Page tests.** Page JS is tested by node-run tests with a stubbed `fetch`, covering both our API and the WoRMS URLs. Tests never call the real WoRMS.
- **Merging to main deploys staging.** Migrate runs before App Runner rolls.
- **DGX production.** It needs `make migrate` (0007) before its dashboard restarts on this code, as for 0006.
- **The label archive (ai #45)** still has to archive `species_suggestions`, now with `aphia_id`.

## Approaches

- **A. The browser asks WoRMS (chosen).** This costs nothing and needs one ai PR. The cost is that the server trusts the browser's record.
- **B. An internet-facing relay Lambda that the server calls through a private endpoint.** The server would be the authority on names. It costs about $15 a month and needs an infra PR.
- **C. A NAT gateway.** Dropped. It costs about $32 a month and undoes the VPC's no-internet design.

## Left to the builder

- **Form details.** The exact layout and wording beyond the copy above. Whether "Check with WoRMS" also runs on Enter, or as the reviewer types (debounced).
- **Call budget.** The order and number of WoRMS calls, kept to a handful per check.
- **Removing dead code.** Whether to remove #49 code that only leftovers can reach. Remove it only once no pending row can exist; otherwise keep it.

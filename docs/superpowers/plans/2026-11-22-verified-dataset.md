# The verified dataset (phase 3, training half) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A hand-invoked job turns the listed reviewers' answers into a dated, never-changing snapshot in S3 (classifier crops including a `no_animal` class, complete frames with COCO boxes, a manifest and a report), and a script builds the classifier's training folder from the base crops plus a snapshot.

**Architecture:** `app/verified_dataset.py` selects boxes and frames from the database in one read-only transaction (the resolution rule is `review_labels.resolve`) and runs the `build_dataset` job; `app/verified_dataset_files.py` turns a selection into the snapshot's files without touching the database; `scripts/assemble_training_set.py` runs where training runs and writes `data/fine_tune/` in the layout `scripts/train_baseline.py` already reads. Infra gives the jobs Lambda `DATASET_URL`, `DATASET_ACCOUNTS` and read+put on the dataset prefix.

**Tech Stack:** Python 3.11, psycopg 3 (async), Pillow, `common.storage` (file:// and s3://), pytest + anyio, a scratch PostgreSQL via `TEST_DATABASE_URL`; infra: AWS CDK (Python).

**Spec:** `docs/superpowers/specs/2026-11-22-verified-dataset-training-design.md`

## Global Constraints

- Runs as a job in the dashboard image's jobs Lambda (512 MB, 900 s, in the VPC), like `archive_labels`. No new Python dependency.
- Storage through `common.storage` and URLs from the environment, as everywhere in this repo.
- The same rule decides which answer counts everywhere: `services/dashboard/app/review_labels.py`.
- A label no person gave never enters a snapshot; a fish left unboxed in a detector frame never does either.
- A volunteer's name or email in no file: accounts appear as numeric ids only (also in the job's result, which job_runs records).
- A snapshot never changes after it is written; the job adds files and never deletes any.
- All reads in one `READ ONLY` transaction (`label_archive.read_only_transaction`).
- Deterministic: the same database state and frames give the same content hash (`label_archive.content_hash`, over every file except `snapshot.json`).
- Classifier folder for "no animal here" answers: `no_animal`. Crops: the box plus 5% padding, clamped to the frame, not resized, JPEG.
- Tests: `ruff check .`, `ruff format --check .`, `pytest` with `TEST_DATABASE_URL` set to the scratch PostgreSQL (never staging's Aurora).

## Review Focus

1. A frame whose image is gone from storage: its boxes and the frame are held back as "frame image missing" and the rest of the snapshot is still written (Task 3).
2. A box at or past the frame's edge, or with no area: the crop is clamped, and a box with nothing left inside the frame is held back as "box outside the frame", never a crash (Task 3).
3. Two `saved_frames` rows with the same camera, `frame_id` and `ts`: the box belongs to the lowest id once, as `export_labeled_corrections` and the archive match it (Task 1).
4. A species name with characters unsafe in a path (`/`, `.`): one flat folder named with `review_catalogue.slug`, never a nested directory (Task 3).
5. A base tar member outside `fine_tune/` (absolute, or with `..`): the assembler refuses before writing anything (Task 5).

---

### Task 1: Select the boxes that count

**Files:**
- Create: `services/dashboard/app/verified_dataset.py`
- Test: `tests/test_verified_dataset.py`

**Interfaces:**
- Consumes: `review_labels.load_verdicts(conn, event_ids)`, `review_labels.resolve(verdicts)`, `review_labels.CONFIDENCE_RANK`, `species_suggestions.pending_species_events(conn, event_ids)`.
- Produces:
  ```python
  NO_ANIMAL = "no_animal"
  @dataclass(frozen=True)
  class Box:
      box_id: str                      # "ev<event_id>" | "add<review_added_boxes.id>"
      saved_frame_id: int | None
      camera: str                      # source_name
      ts: datetime                     # UTC
      bbox: tuple[int, int, int, int]  # x1, y1, x2, y2
      bbox_source: str                 # "model" | "corrected" | "drawn"
      outcome: str                     # "species" | "unidentifiable" | "no_animal"
      accepted_name: str | None
      life_stage: str | None
      confidence: str
      status: str                      # single | agreed | adjudicated | disputed
      label_source: str | None
      user_ids: tuple[int, ...]
      model_name: str | None           # detection_events.best_name
      model_score: float | None        # detection_events.best_accuracy
      held_back: str | None
  @dataclass(frozen=True)
  class Frame:
      saved_frame_id: int; camera: str; ts: datetime; image_path: str
      completed: bool                  # a review_frames row exists
      model_box_ids: tuple[str, ...]   # every detection event on the frame (exact camera, frame_id, ts), "ev<id>", ascending
  @dataclass(frozen=True)
  class Selection:
      boxes: tuple[Box, ...]           # sorted by (saved_frame_id or 0, ev before add, numeric id)
      frames: dict[int, Frame]         # every frame a box is on
      sample_answers: int              # listed accounts' answers on model_version='sample' events, left out
      newest_answer_id: int | None
      newest_drawn_box_id: int | None
  async def select(conn, user_ids: list[int]) -> Selection
  ```
- Held-back reasons set here (exact strings, in this order of precedence): `"disputed"`, `"species waiting for approval"`, `f"confidence {confidence}"` (rank below `probable`), `"no species named"` (species outcome with no name), `"no saved frame"`.

Rules: only answers whose `user_id` is in `user_ids` are passed to `resolve` (everyone else's are dropped first); a model box's bbox is `Resolved.bbox` (`bbox_source="corrected"`) else the event's (`"model"`); `label_source` is that of the verdict whose outcome counts (the adjudication if any, else `Resolved.first`); events with `model_version = 'sample'` are left out; drawn boxes are the listed accounts' `review_added_boxes` with `removed_at IS NULL` on frames whose `model_version IS DISTINCT FROM 'sample'`, status `"single"`, label = `label`, `user_ids=(user_id,)`; an event's frame is the lowest-id `saved_frames` row with its exact camera, `frame_id` and `ts`.

- [ ] **Step 1: Write the failing tests** (module docstring cites the spec; `pytestmark = pytest.mark.anyio`; fixtures from `review_fixtures`: `seed_frame`, `make_user`, `add_item`, `add_verdict`, `rdb`; local helpers `drawn(db, frame, uid, bbox, *, outcome="species", label=None, confidence="certain", removed=False) -> int` and `complete(db, frame, uid)` insert into `review_added_boxes` / `review_frames`)

  ```python
  async def test_a_single_answer_counts_with_its_label_source(rdb, tmp_path):
      # one box (bbox [100,100,300,250], species "Seriola zonata", p 0.62); roy answers "Seriola rivoliana", label_source "bulk"
      [box] = (await _select(rdb, [roy])).boxes
      assert (box.box_id, box.outcome, box.accepted_name, box.status) == (f"ev{eid}", "species", "Seriola rivoliana", "single")
      assert (box.label_source, box.bbox, box.bbox_source, box.held_back) == ("bulk", (100, 100, 300, 250), "model", None)
      assert (box.model_name, box.model_score, box.user_ids) == ("Seriola zonata", 0.62, (roy,))

  async def test_other_accounts_answers_are_dropped_before_resolution(rdb, tmp_path):
      # box 0: roy first pass "Seriola zonata"; admin spot-check (pass-2 item) "Caranx ruber" -> roy's, single
      # box 1: only the admin answered -> not in the selection
      assert [(b.box_id, b.status, b.accepted_name) for b in sel.boxes] == [(f"ev{e0}", "single", "Seriola zonata")]

  async def test_a_disputed_box_is_held_back(rdb, tmp_path):
      # roy and amy both listed; first pass "Seriola zonata", spot-check "Caranx ruber"
      assert (box.status, box.held_back) == ("disputed", "disputed")

  async def test_a_box_naming_a_pending_species_is_held_back(rdb, tmp_path):
      # species_suggestions row status 'pending'; roy's answer linked by species_suggestion_id
      assert box.held_back == "species waiting for approval"

  async def test_only_probable_or_certain_answers_count(rdb, tmp_path):
      assert [b.held_back for b in sel.boxes] == [None, None, "confidence uncertain"]  # certain, probable, uncertain

  async def test_a_corrected_box_replaces_the_model_box(rdb, tmp_path):
      assert (box.bbox, box.bbox_source) == ((110, 105, 290, 240), "corrected")

  async def test_no_animal_and_cant_identify_are_outcomes(rdb, tmp_path):
      assert [(b.outcome, b.accepted_name, b.held_back) for b in sel.boxes] == [("no_animal", None, None), ("unidentifiable", None, None)]

  async def test_drawn_boxes_count_and_removed_or_unlisted_ones_do_not(rdb, tmp_path):
      # roy draws one kept and one removed box; amy (not listed) draws one
      assert [(b.box_id, b.bbox_source, b.status, b.accepted_name) for b in sel.boxes] == [(f"add{kept}", "drawn", "single", "Seriola zonata")]

  async def test_sample_events_are_left_out(rdb, tmp_path):
      # UPDATE detection_events SET model_version='sample' for box 1; roy answered both
      assert [b.box_id for b in sel.boxes] == [f"ev{e0}"] and sel.sample_answers == 1

  async def test_frames_know_completion_and_every_model_box(rdb, tmp_path):
      # three boxes, roy answers one, complete(frame)
      f = sel.frames[frame["saved_frame_id"]]
      assert f.completed and f.model_box_ids == tuple(f"ev{e}" for e in frame["event_ids"])

  async def test_an_event_with_two_saved_frame_rows_is_one_box_on_the_lowest(rdb, tmp_path):
      # a second saved_frames row with the same camera, frame_id, ts
      assert [b.saved_frame_id for b in sel.boxes] == [frame["saved_frame_id"]] and list(sel.frames) == [frame["saved_frame_id"]]

  async def test_the_newest_answer_and_drawn_box_ids(rdb, tmp_path):
      assert (sel.newest_answer_id, sel.newest_drawn_box_id) == (v2, d1)  # roy's; an unlisted account's later ids ignored
  ```
  `_select(db, ids)` opens a connection and calls `verified_dataset.select(conn, ids)`.

- [ ] **Step 2: Run them to verify they fail**

  Run: `pytest tests/test_verified_dataset.py -q`
  Expected: FAIL (`ModuleNotFoundError: app.verified_dataset`), not skipped (`TEST_DATABASE_URL` is set).

- [ ] **Step 3: Implement `select` and the dataclasses in `services/dashboard/app/verified_dataset.py`**

  Four queries on the given connection: the listed accounts' answered event ids (and the count on sample events), the events with their lowest-id saved frame (a `LEFT JOIN LATERAL ... ORDER BY s.id LIMIT 1`, as `label_archive.VERDICTS_SQL`), the listed accounts' drawn boxes, and the frames (`review_frames` join, `ARRAY(SELECT e.id ...)` of the frame's events). `newest_answer_id` = max id over the listed accounts' answers on non-sample events.

- [ ] **Step 4: Run them to verify they pass**

  Run: `pytest tests/test_verified_dataset.py -q`
  Expected: 12 passed.

- [ ] **Step 5: Commit**

  ```bash
  git add services/dashboard/app/verified_dataset.py tests/test_verified_dataset.py
  git commit -m "Verified dataset: select the listed reviewers' boxes that count"
  ```

### Task 2: Decide the detector frames

**Files:**
- Modify: `services/dashboard/app/verified_dataset.py`
- Test: `tests/test_verified_dataset.py`

**Interfaces:**
- Consumes: `Box`, `Frame`, `Selection` (Task 1).
- Produces:
  ```python
  @dataclass(frozen=True)
  class DetectorPlan:
      frames: tuple[int, ...]                     # saved_frame_ids, ascending
      annotations: dict[int, tuple[Box, ...]]     # per included frame, in Selection order
      held_back: dict[int, str]                   # saved_frame_id -> reason, for every other frame in the selection
  def detector_frames(sel: Selection) -> DetectorPlan
  ```
- Frame reasons (exact): `"not marked complete"`; `f"box {box_id} has no answer from the listed accounts"`; `f"box {box_id} held back: {reason}"`. The first problem found wins, checked in this order: completion, the frame's `model_box_ids` in order, then its boxes in Selection order.

A frame goes in when it is completed, every id in `model_box_ids` has a box in the selection, and no box on it (model or drawn) is held back. Its annotations are its boxes whose outcome is `species` or `unidentifiable`; `no_animal` boxes are left out (background).

- [ ] **Step 1: Write the failing tests** (pure: build `Selection` by hand with a `_box(**overrides)` helper)

  ```python
  def test_a_complete_frame_goes_in_without_its_no_animal_boxes():
      # completed frame 7: ev1 species, ev2 unidentifiable, ev3 no_animal, add4 drawn species
      plan = detector_frames(sel)
      assert plan.frames == (7,) and [b.box_id for b in plan.annotations[7]] == ["ev1", "ev2", "add4"] and plan.held_back == {}

  def test_an_incomplete_frame_is_held_back():
      assert plan.held_back == {7: "not marked complete"} and plan.frames == ()

  def test_a_box_nobody_listed_answered_holds_the_frame_back():
      # model_box_ids ("ev1", "ev9"); only ev1 selected
      assert plan.held_back == {7: "box ev9 has no answer from the listed accounts"}

  def test_a_held_back_box_holds_the_frame_back():
      assert plan.held_back == {7: "box ev1 held back: disputed", 8: "box add5 held back: confidence uncertain"}
  ```

- [ ] **Step 2: Run them to verify they fail**

  Run: `pytest tests/test_verified_dataset.py -q -k "frame"`
  Expected: FAIL with `ImportError: cannot import name 'detector_frames'`.

- [ ] **Step 3: Implement `detector_frames(sel: Selection) -> DetectorPlan`**

- [ ] **Step 4: Run them to verify they pass**

  Run: `pytest tests/test_verified_dataset.py -q`
  Expected: 16 passed.

- [ ] **Step 5: Commit**

  ```bash
  git add services/dashboard/app/verified_dataset.py tests/test_verified_dataset.py
  git commit -m "Verified dataset: only complete frames with every box counting go to the detector"
  ```

### Task 3: Build the snapshot's files

**Files:**
- Create: `services/common/taxonomy.py`
- Create: `services/dashboard/app/verified_dataset_files.py`
- Test: `tests/test_verified_dataset_files.py`

**Interfaces:**
- Consumes: `Box`, `Frame`, `Selection`, `DetectorPlan`, `NO_ANIMAL` (Tasks 1–2); `review_catalogue.slug`.
- Produces:
  ```python
  # common/taxonomy.py: taxonomy.csv as the review catalogue reads it
  def normalize(raw: str) -> str                         # "Pomatomus_saltarix " -> "Pomatomus saltarix" (review_catalogue.normalize_raw's rule)
  def read(path: Path) -> dict[str, dict]                # normalized raw_name -> row; {} when the file is missing
  def accepted(rows: dict[str, dict], raw: str) -> str   # the row's accepted_name, else normalize(raw)
  def aphia_by_accepted(rows: dict[str, dict]) -> dict[str, int]

  # app/verified_dataset_files.py
  PAD = 0.05
  JPEG_QUALITY = 95
  UNIDENTIFIED = "unidentified animal"
  def crop_box(bbox, width: int, height: int, pad: float = PAD) -> tuple[int, int, int, int] | None
  def class_folder(box: Box) -> str | None               # NO_ANIMAL | slug(accepted_name) | None
  def build_files(sel: Selection, plan: DetectorPlan, read_frame: Callable[[str], bytes],
                  aphia: Mapping[str, int], zone: ZoneInfo) -> tuple[dict[str, bytes], dict[str, Any]]
  ```
- `build_files` returns `(files, counts)`. Files: `manifest.json`, `report.txt`, `classifier/<folder>/<box_id>.jpg`, `detector/images/<saved_frame_id>.jpg` (the frame's original bytes), `detector/annotations.coco.json` (always present). Counts: `{"boxes", "crops": {folder: n}, "detector_frames", "detector_annotations", "boxes_held_back", "frames_held_back", "sample_answers"}`.
- Reasons added here: `"frame image missing"` (a box, and its frame), `"box outside the frame"` (a box whose `crop_box` is None; every box on a frame that is read is checked). A box held back here holds its detector frame back too, with Task 2's `f"box {box_id} held back: {reason}"`; the frame then leaves COCO and `detector/images/`. A frame is read when it has a box to crop or is in `plan.frames`.
- Manifest: `{"records": [...], "counts": counts}`; one record per box in Selection order with exactly the keys `box_id, saved_frame_id, camera, ts, local_date, local_ts, bbox, bbox_source, outcome, accepted_name, aphia_id, life_stage, confidence, status, label_source, user_ids, model_name, model_score, crop, frame, held_back`. `ts` is UTC as `2026-06-01T14:00:00Z`; `local_date` and `local_ts` (ISO 8601 with offset) are in `zone`; `crop` is the crop's path in the snapshot or null; `frame` is `detector/images/<id>.jpg` when the frame is a detector frame, else null. `local_ts` is an addition to the spec's list: the assembler needs seconds since local midnight and has no site zone.
- COCO: `images` (`id` = saved_frame_id, `file_name` = `images/<id>.jpg`, `width`, `height`, `date_captured` = `ts`, `camera`); `annotations` (`id` from 1 in order, `image_id`, `bbox` `[x, y, w, h]` clamped to the frame, `area`, `iscrowd` 0, `category_id`, `box_id`, `bbox_source`, `label_source`); `categories` = `{"id": 1, "name": "unidentified animal", "aphia_id": None}` then the species used, sorted by name, ids from 2.
- Each frame is read and decoded once for all its boxes; JSON is written with `sort_keys=True, indent=1`; `report.txt` lists the counts, then every held-back box as `<box_id> (frame <id>): <reason>`, then every held-back frame as `frame <id>: <reason>`, each section sorted.

- [ ] **Step 1: Write the failing tests** (frames are PIL images saved to JPEG bytes in the test; `read_frame` is a dict lookup that raises `FileNotFoundError` for a missing path)

  ```python
  def test_crop_box_pads_five_percent_and_clamps():
      assert crop_box((100, 100, 300, 200), 1280, 720) == (90, 95, 310, 205)
      assert crop_box((0, 0, 100, 100), 1280, 720) == (0, 0, 105, 105)
      assert crop_box((1300, 10, 1400, 50), 1280, 720) is None
      assert crop_box((50, 50, 50, 90), 1280, 720) is None

  def test_species_and_no_animal_crops_are_filed_by_name():
      assert Image.open(io.BytesIO(files["classifier/Brachygenys_chrysargyrea/ev1.jpg"])).size == (220, 110)
      assert "classifier/no_animal/ev2.jpg" in files
      assert not [k for k in files if k.endswith(("/ev3.jpg", "/ev4.jpg"))]  # unidentifiable, held back

  def test_a_name_with_unsafe_characters_makes_one_folder():
      assert [k for k in files if k.startswith("classifier/")] == ["classifier/Kyphosus_sectatrix_bigibbus/ev1.jpg"]

  def test_the_manifest_has_one_record_per_box():
      rec = manifest["records"][0]
      assert set(rec) == MANIFEST_KEYS
      assert (rec["ts"], rec["local_date"], rec["local_ts"]) == ("2026-06-01T14:00:00Z", "2026-06-01", "2026-06-01T10:00:00-04:00")
      assert (rec["aphia_id"], rec["crop"], rec["frame"]) == (1841028, "classifier/Brachygenys_chrysargyrea/ev1.jpg", "detector/images/7.jpg")

  def test_coco_lists_detector_frames_with_xywh_boxes():
      assert coco["images"] == [{"id": 7, "file_name": "images/7.jpg", "width": 1280, "height": 720, "date_captured": "2026-06-01T14:00:00Z", "camera": "seahivecam"}]
      assert coco["annotations"][0] == {"id": 1, "image_id": 7, "bbox": [100, 100, 200, 100], "area": 20000, "iscrowd": 0,
                                             "category_id": 2, "box_id": "ev1", "bbox_source": "model", "label_source": "search"}
      assert coco["categories"][:2] == [{"id": 1, "name": "unidentified animal", "aphia_id": None},
                                        {"id": 2, "name": "Brachygenys chrysargyrea", "aphia_id": 1841028}]
      assert files["detector/images/7.jpg"] == frame_bytes

  def test_a_missing_frame_holds_its_boxes_and_frame_back():
      assert {r["held_back"] for r in manifest["records"]} == {"frame image missing"}
      assert coco["images"] == [] and "frame 7: frame image missing" in report and not [k for k in files if k.startswith("classifier/")]

  def test_a_box_outside_the_frame_is_held_back():
      assert manifest["records"][0]["held_back"] == "box outside the frame" and manifest["records"][0]["crop"] is None
      assert coco["images"] == [] and "frame 7: box ev1 held back: box outside the frame" in report

  def test_the_same_selection_builds_the_same_bytes():
      assert build_files(*args) == build_files(*args)

  def test_taxonomy_maps_raw_names_to_accepted_names_and_aphia_ids(tmp_path):
      assert taxonomy.accepted(rows, "Haemulon_chrysargyreum") == "Brachygenys chrysargyrea"
      assert taxonomy.accepted(rows, "Not listed") == "Not listed"
      assert taxonomy.aphia_by_accepted(rows)["Kyphosus"] == 126015
  ```

- [ ] **Step 2: Run them to verify they fail**

  Run: `pytest tests/test_verified_dataset_files.py -q`
  Expected: FAIL (`ModuleNotFoundError: app.verified_dataset_files`).

- [ ] **Step 3: Implement `common/taxonomy.py` and `app/verified_dataset_files.py`** with the interfaces above. Padding is `round(PAD * w)` and `round(PAD * h)` on each side.

- [ ] **Step 4: Run them to verify they pass**

  Run: `pytest tests/test_verified_dataset_files.py -q`
  Expected: 9 passed.

- [ ] **Step 5: Commit**

  ```bash
  git add services/common/taxonomy.py services/dashboard/app/verified_dataset_files.py tests/test_verified_dataset_files.py
  git commit -m "Verified dataset: crops, detector frames, COCO, manifest and report"
  ```

### Task 4: The `build_dataset` job

**Files:**
- Modify: `services/dashboard/app/verified_dataset.py`
- Modify: `services/dashboard/app/jobs.py` (wrapper, `JOBS`, the usage line in the module docstring)
- Test: `tests/test_verified_dataset.py`

**Interfaces:**
- Consumes: `select`, `detector_frames` (Tasks 1–2); `build_files`, `common.taxonomy` (Task 3); `label_archive.read_only_transaction`, `content_hash`, `UnknownAccount`; `frames.frame_url`; `config.site_tz`; `common.storage.open_read/put/exists/join`.
- Produces: `async def build_dataset(pool: AsyncConnectionPool) -> dict[str, Any]`, registered as `JOBS["build_dataset"]` (not in `ALL`, no schedule).
- Environment: `DATASET_ACCOUNTS` (JSON list of usernames; unset or `[]` → `{"skipped": "DATASET_ACCOUNTS is empty"}`), `DATASET_URL` (required when accounts are listed: `RuntimeError`), `GIT_SHA` (provenance, may be unset), `REVIEW_TAXONOMY_CSV` else `REVIEW_ASSETS_DIR/taxonomy.csv` else the repository's `assets/taxonomy.csv` (main.py's rule).
- Flow: usernames → ids (`UnknownAccount` naming the missing count, never the name); in one read-only transaction `select` plus the approved reviewer-added species' `approved_name → aphia_id` (taxonomy.csv wins); `build_files` in a thread with `read_frame = open_read(frame_url(path))`; digest = `content_hash(files)`; if `latest.json`'s `sha256` equals it, write nothing; else put every file under `<DATASET_URL>/<stamp>/`, then `snapshot.json` (`snapshot`, `git_sha`, `content_hash`, `account_ids`, `newest_answer_id`, `newest_drawn_box_id`, `counts`), then `latest.json` (`{"snapshot": stamp, "sha256": digest}`) last. Stamp format as the archive's (`%Y-%m-%dT%H%M%SZ`).
- Result: `{"snapshot": stamp | None, "unchanged": bool, "accounts": len(ids), "counts": counts}`.

- [ ] **Step 1: Write the failing tests** (`DATASET_URL=file://<tmp>/datasets`, `DATASET_ACCOUNTS='["roy tester"]'`, `GIT_SHA=abc1234`, `TZ=America/New_York` via monkeypatch; `_stamp` monkeypatched to a counter)

  ```python
  async def test_no_accounts_does_nothing(rdb, tmp_path, monkeypatch):
      assert await build_dataset(rdb) == {"skipped": "DATASET_ACCOUNTS is empty"} and not (tmp_path / "datasets").exists()

  async def test_accounts_without_a_url_fail(rdb, monkeypatch):
      with pytest.raises(RuntimeError, match="DATASET_URL"): await build_dataset(rdb)

  async def test_an_unknown_account_fails_without_naming_it(rdb, monkeypatch):
      with pytest.raises(UnknownAccount) as e: await build_dataset(rdb)
      assert "nobody" not in str(e.value)

  async def test_a_run_writes_a_snapshot_then_latest(rdb, tmp_path, monkeypatch):
      result = await build_dataset(rdb)
      snap = json.loads((tmp_path / f"datasets/{result['snapshot']}/snapshot.json").read_text())
      latest = json.loads((tmp_path / "datasets/latest.json").read_text())
      assert latest == {"snapshot": result["snapshot"], "sha256": snap["content_hash"]}
      assert (snap["git_sha"], snap["account_ids"], snap["newest_answer_id"]) == ("abc1234", [roy], verdict_id)
      assert (tmp_path / f"datasets/{result['snapshot']}/classifier/Seriola_zonata/ev{eid}.jpg").is_file()

  async def test_an_unchanged_run_writes_nothing(rdb, tmp_path, monkeypatch):
      first, second = await build_dataset(rdb), await build_dataset(rdb)
      assert (second["snapshot"], second["unchanged"]) == (None, True)
      assert sorted(p.name for p in (tmp_path / "datasets").iterdir()) == sorted([first["snapshot"], "latest.json"])

  async def test_a_new_answer_makes_a_new_snapshot_and_keeps_the_old(rdb, tmp_path, monkeypatch):
      assert second["snapshot"] != first["snapshot"] and (tmp_path / f"datasets/{first['snapshot']}/manifest.json").is_file()

  async def test_no_username_or_email_in_any_file_or_result(rdb, tmp_path, monkeypatch):
      blobs = [p.read_bytes() for p in (tmp_path / "datasets").rglob("*") if p.is_file()] + [json.dumps(result).encode()]
      assert not [b for b in blobs if b"roy tester" in b or b"Roy Tester" in b or b"@example.org" in b]

  def test_build_dataset_is_a_job_run_by_hand():
      assert jobs.JOBS["build_dataset"] is jobs.build_dataset and "build_dataset" not in jobs.ALL
  ```

- [ ] **Step 2: Run them to verify they fail**

  Run: `pytest tests/test_verified_dataset.py -q -k "run or account or job or file"`
  Expected: FAIL with `ImportError: cannot import name 'build_dataset'`.

- [ ] **Step 3: Implement `build_dataset` and register it in `jobs.py`** (the wrapper imports the module lazily, as `archive_labels` does).

- [ ] **Step 4: Run the module's and the jobs' tests**

  Run: `pytest tests/test_verified_dataset.py tests/test_jobs.py -q`
  Expected: all passed.

- [ ] **Step 5: Commit**

  ```bash
  git add services/dashboard/app/verified_dataset.py services/dashboard/app/jobs.py tests/test_verified_dataset.py
  git commit -m "Verified dataset: the build_dataset job writes a snapshot only when something changed"
  ```

### Task 5: Assemble the classifier's training folder

**Files:**
- Create: `scripts/assemble_training_set.py`
- Modify: `docs/finetune_workflow.md` (a section "Training on reviewers' labels": build_dataset, then the assembler, then `train_baseline.py --split-mode by_source`)
- Test: `tests/test_assemble_training_set.py`

**Interfaces:**
- Consumes: `common.class_renames.class_names`, `folder`; `common.taxonomy.read`, `accepted`; `common.storage.open_read`, `join`; the snapshot's `snapshot.json` and `manifest.json` (Tasks 3–4).
- Produces: `def main(argv: list[str] | None = None) -> int`, CLI `--base PATH --out DIR [--snapshot DIR_OR_URL] [--taxonomy PATH] [--allow-new-classes]`; `--out` defaults to `data/fine_tune`, `--taxonomy` to `assets/taxonomy.csv` (both under the repository root).
- Exit codes: 0 built; 2 refused before writing (`--out` exists and is not empty, an unsafe tar member, two output folders with one accepted name).

Behaviour:
1. `--base` is the tar (members under `fine_tune/`, read with `extractfile` and written by this script, never `extractall`) or a directory holding `_dataset_manifest.json` (or a `fine_tune/` holding it).
2. Every base folder is written under `folder(class_names([F])[0])`; the records of a renamed folder take the new name as `label`; every record's `crop_path` is its new file relative to the repository root; `_taxonomy.json` keys of renamed labels move to the new label. A record whose file is not in the base is dropped and counted.
3. With `--snapshot`: a location with `snapshot.json` is the snapshot; one with only `latest.json` is followed to `<location>/<snapshot>/`; a local path becomes a `file://` URL. For each manifest record with `crop` set and `held_back` null: species → the output folder whose name has the same `accepted` name (else skipped and counted, unless `--allow-new-classes`, which uses `slug(accepted_name)`); `no_animal` → `no_animal`. The crop goes to `<out>/<folder>/review_<box_id>.jpg` with the record `{"crop_id": "review_<box_id>", "label": folder with spaces, "display_label": None, "family": None, "genus": None, "species": None, "drive_id": "review:<camera>:<local_date>", "source_path": "<snapshot id>/<crop>", "source_kind": "review:video", "frame_index": <seconds since local midnight from local_ts>, "bbox": bbox, "det_conf": None, "crop_path": ...}`.
4. `_review_merge.json`: `{"snapshot", "location", "content_hash", "copied": {folder: n}, "skipped_new_classes": {accepted_name: n}, "base_records_dropped": n}`; a summary line is printed.

- [ ] **Step 1: Write the failing tests** (a tiny base directory with `Haemulon_chrysargyreum/a.jpg`, `Kyphosus_sectatrixbigibbus/c.jpg`, `Pomacanthus_paru/b.jpg` and their records; a test taxonomy.csv; a snapshot directory written by the test)

  ```python
  def test_renamed_base_folders_take_the_new_names(tmp_path):
      assert main(["--base", str(base), "--out", str(out), "--taxonomy", str(tax)]) == 0
      assert sorted(p.name for p in out.iterdir() if p.is_dir()) == ["Brachygenys_chrysargyrea", "Kyphosus", "Pomacanthus_paru"]
      rec = next(r for r in manifest(out) if r["crop_id"] == "a")
      assert rec["label"] == "Brachygenys chrysargyrea" and rec["crop_path"].endswith("Brachygenys_chrysargyrea/a.jpg")
      assert "Brachygenys chrysargyrea" in taxonomy_json(out) and "Haemulon chrysargyreum" not in taxonomy_json(out)

  def test_review_crops_join_the_folder_of_their_accepted_name(tmp_path):
      rec = next(r for r in manifest(out) if r["crop_id"] == "review_ev5")
      assert (out / "Brachygenys_chrysargyrea/review_ev5.jpg").is_file()
      assert (rec["drive_id"], rec["source_kind"], rec["frame_index"]) == ("review:seahivecam:2026-06-01", "review:video", 36000)

  def test_no_animal_crops_are_a_class_of_their_own(tmp_path):
      assert (out / "no_animal/review_ev6.jpg").is_file() and next(r for r in manifest(out) if r["crop_id"] == "review_ev6")["label"] == "no_animal"

  def test_held_back_and_cropless_records_are_not_copied(tmp_path):
      assert not list(out.rglob("review_ev7.jpg")) and not list(out.rglob("review_ev8.jpg"))

  def test_a_species_without_a_folder_is_skipped_unless_allowed(tmp_path):
      assert merge(out)["skipped_new_classes"] == {"Lutjanus apodus": 1}
      assert (out2 / "Lutjanus_apodus/review_ev9.jpg").is_file()  # with --allow-new-classes

  def test_latest_json_is_followed(tmp_path):
      assert merge(out)["snapshot"] == "2026-11-23T120000Z"

  def test_the_tar_and_its_directory_build_the_same_folder(tmp_path):
      assert tree(out_from_tar) == tree(out_from_dir)

  def test_a_non_empty_out_is_refused(tmp_path):
      assert main([...]) == 2 and sorted(p.name for p in out.iterdir()) == ["keep.txt"]

  def test_a_tar_member_outside_fine_tune_is_refused(tmp_path):
      assert main([...]) == 2 and not out.exists() and not (tmp_path / "evil.txt").exists()

  def test_two_folders_for_one_accepted_name_stop_the_build(tmp_path):
      # base has both Brachygenys_chrysargyrea/ and Haemulon_chrysargyreum/
      assert main([...]) == 2 and not out.exists()
  ```

- [ ] **Step 2: Run them to verify they fail**

  Run: `pytest tests/test_assemble_training_set.py -q`
  Expected: FAIL (`ModuleNotFoundError: assemble_training_set`).

- [ ] **Step 3: Implement `scripts/assemble_training_set.py`** (puts `services/` on `sys.path` as `fetch_models.py` does; checks every refusal before creating `--out`) and write the docs section.

- [ ] **Step 4: Run them to verify they pass**

  Run: `pytest tests/test_assemble_training_set.py -q`
  Expected: 10 passed.

- [ ] **Step 5: Run the whole suite and lint**

  Run: `ruff check . && ruff format --check . && pytest -q`
  Expected: clean, all passed except the four docker-dependent tests (`test_compose_profiles::test_web_profile_has_no_gpu_services`, three in `test_gpu_host`) when the docker daemon is not running.

- [ ] **Step 6: Commit**

  ```bash
  git add scripts/assemble_training_set.py tests/test_assemble_training_set.py docs/finetune_workflow.md
  git commit -m "assemble_training_set: base crops plus a verified snapshot, in the trainer's layout"
  ```

### Task 6: Infra: the jobs Lambda's dataset settings and grants (wahoobay/infra)

**Files:**
- Modify: `wbplatform/stacks/ai.py` (`_functions`: next to the label archive)
- Test: `tests/test_ai_stack.py`

**Interfaces:**
- Produces: jobs Lambda env `DATASET_URL = s3://<models bucket>/datasets/verified/<env>/`, `DATASET_ACCOUNTS = json.dumps(archive_accounts)` (the label archive's list); `models_bucket.grant_read` and `grant_put` (never `grant_read_write`) on `datasets/verified/<env>/*`.

- [ ] **Step 1: Write the failing tests** (branch `verified-dataset` from main in the infra checkout)

  ```python
  def test_jobs_get_the_dataset_url_and_accounts():
      variables = _jobs_fn(synth(Env.STAGING, False))["Environment"]["Variables"]
      assert variables["DATASET_ACCOUNTS"] == '["roy camop in germany"]'
      parts = variables["DATASET_URL"]["Fn::Join"][1]
      assert parts[0] == "s3://" and "ModelsBucket" in json.dumps(parts[1]) and parts[2] == "/datasets/verified/staging/"
      # prod: DATASET_ACCOUNTS == "[]"

  def test_jobs_can_add_to_the_dataset_but_never_delete():
      # statements on "/datasets/verified/staging/*" in the jobs role's policies
      assert "s3:PutObject" in actions and "s3:GetObject*" in actions
      assert not [a for a in actions if a.startswith("s3:DeleteObject")]
  ```

- [ ] **Step 2: Run them to verify they fail**

  Run: `pytest tests/test_ai_stack.py -q -k dataset`
  Expected: FAIL with `KeyError: 'DATASET_ACCOUNTS'`.

- [ ] **Step 3: Add the env and grants in `wbplatform/stacks/ai.py`**

- [ ] **Step 4: Run the infra suite and synth**

  Run: `pytest -q && npx --yes aws-cdk@2 synth -q`
  Expected: all passed; synth exits 0.

- [ ] **Step 5: Commit**

  ```bash
  git add wbplatform/stacks/ai.py tests/test_ai_stack.py
  git commit -m "ai stack: the jobs Lambda builds the verified dataset (DATASET_URL, DATASET_ACCOUNTS, read+put)"
  ```

## Rollout (after both PRs merge, on Dan's word)

Infra first (its merge deploys staging), then ai (its merge redeploys the images). Then invoke once:
`AWS_PROFILE=cdk AWS_MAX_ATTEMPTS=1 aws lambda invoke --cli-read-timeout 900 --function-name ai-wahoobay-staging-jobs --payload '{"job":"build_dataset"}' --cli-binary-format raw-in-base64-out out.json` (a run can outlast the CLI's 60 s default, after which it would retry and start a second run)
and check: `ok` true; `latest.json` names the snapshot; `report.txt`'s counts match Roy's data (about 1,100 species crops, 30 `no_animal`, about 179 detector frames, minus what the report holds back).

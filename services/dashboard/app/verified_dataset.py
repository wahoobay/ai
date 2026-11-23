"""The verified dataset: the listed reviewers' answers as a snapshot for training (phase 3,
training half; spec docs/superpowers/specs/2026-11-22-verified-dataset-training-design.md).

`select` reads which boxes count: only the listed accounts' answers go into the one resolution
rule (review_labels.resolve), so another account's test answer can neither replace nor dispute
theirs. `detector_frames` decides which completed frames the detector may learn from: every fish
boxed and every box counting, "no animal" boxes left unannotated as background. The
`build_dataset` job (run by hand) writes a snapshot under DATASET_URL only when its content
changed, and never deletes one: the database is not append-only, so a snapshot is the only
record of the state it was built from.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from common import taxonomy
from common.storage import exists, join, open_read, put
from psycopg_pool import AsyncConnectionPool

from .config import site_tz
from .frames import frame_url
from .label_archive import CONTENT_TYPES, UnknownAccount, content_hash, read_only_transaction
from .review_labels import CONFIDENCE_RANK, load_verdicts, resolve
from .review_queue import fetch_all
from .species_suggestions import pending_species_events

NO_ANIMAL = "no_animal"  # the review tool's outcome; also the classifier folder for it
MIN_CONFIDENCE = "probable"


@dataclass(frozen=True)
class Box:
    box_id: str  # "ev<event_id>" | "add<review_added_boxes.id>"
    saved_frame_id: int | None
    camera: str  # source_name
    ts: datetime  # UTC
    bbox: tuple[int, int, int, int]  # x1, y1, x2, y2
    bbox_source: str  # "model" | "corrected" | "drawn"
    outcome: str  # "species" | "unidentifiable" | "no_animal"
    accepted_name: str | None
    life_stage: str | None
    confidence: str
    status: str  # single | agreed | adjudicated | disputed
    label_source: str | None
    user_ids: tuple[int, ...]
    model_name: str | None  # detection_events.best_name
    model_score: float | None  # detection_events.best_accuracy, to 4 places
    held_back: str | None


@dataclass(frozen=True)
class Frame:
    saved_frame_id: int
    camera: str
    ts: datetime
    image_path: str
    completed: bool  # a review_frames row exists
    model_box_ids: tuple[str, ...]  # every detection event on the frame, "ev<id>", ascending


@dataclass(frozen=True)
class Selection:
    boxes: tuple[Box, ...]  # by (saved_frame_id or 0, model boxes before drawn, numeric id)
    frames: dict[int, Frame]  # every frame a box is on
    sample_answers: int  # the listed accounts' answers on the seed job's synthetic events
    newest_answer_id: int | None
    newest_drawn_box_id: int | None


_ANSWERED_SQL = """
    SELECT c.event_id, e.model_version IS NOT DISTINCT FROM 'sample' AS sample
      FROM detection_corrections c JOIN detection_events e ON e.id = c.event_id
     WHERE c.user_id = ANY(%s)
"""

# An event's frame: the lowest-id saved frame with its exact camera, frame_id and ts (frame_id
# restarts with the worker, so camera + frame_id alone can name another frame), as the label
# archive and export_labeled_corrections match it.
_EVENTS_SQL = """
    SELECT e.id, e.source_name, e.ts, e.bbox, e.best_name, e.best_accuracy, sf.id AS saved_frame_id
      FROM detection_events e
      LEFT JOIN LATERAL (
          SELECT s.id FROM saved_frames s
           WHERE s.source_name = e.source_name AND s.frame_id = e.frame_id AND s.ts = e.ts
           ORDER BY s.id LIMIT 1
      ) sf ON true
     WHERE e.id = ANY(%s)
"""

_DRAWN_SQL = """
    SELECT b.id, b.saved_frame_id, b.bbox, b.outcome, b.label, b.life_stage, b.confidence,
           b.user_id, s.source_name, s.ts
      FROM review_added_boxes b JOIN saved_frames s ON s.id = b.saved_frame_id
     WHERE b.user_id = ANY(%s) AND b.removed_at IS NULL
       AND s.model_version IS DISTINCT FROM 'sample'
"""

_FRAMES_SQL = """
    SELECT s.id, s.source_name, s.ts, s.image_path, rf.saved_frame_id IS NOT NULL AS completed,
           ARRAY(SELECT e.id FROM detection_events e
                  WHERE e.source_name = s.source_name AND e.frame_id = s.frame_id AND e.ts = s.ts
                  ORDER BY e.id) AS event_ids
      FROM saved_frames s LEFT JOIN review_frames rf ON rf.saved_frame_id = s.id
     WHERE s.id = ANY(%s)
"""


def _confidence_reason(confidence: str) -> str | None:
    if CONFIDENCE_RANK.get(confidence, 0) < CONFIDENCE_RANK[MIN_CONFIDENCE]:
        return f"confidence {confidence}"
    return None


def _first(*reasons: str | None) -> str | None:
    return next((r for r in reasons if r), None)


def _order(box: Box) -> tuple:
    kind, number = (0, box.box_id[2:]) if box.box_id.startswith("ev") else (1, box.box_id[3:])
    return (box.saved_frame_id or 0, kind, int(number))


async def select(conn, user_ids: list[int]) -> Selection:
    """The listed accounts' boxes and the frames they are on, read on `conn` (the caller holds
    the read-only transaction)."""
    listed = set(user_ids)
    answered = await fetch_all(conn, _ANSWERED_SQL, (list(user_ids),))
    event_ids = sorted({r["event_id"] for r in answered if not r["sample"]})
    sample_answers = sum(1 for r in answered if r["sample"])

    verdicts = await load_verdicts(conn, event_ids) if event_ids else {}
    pending = await pending_species_events(conn, event_ids) if event_ids else {}
    events = {r["id"]: r for r in await fetch_all(conn, _EVENTS_SQL, (event_ids,))}

    boxes: list[Box] = []
    newest_answer_id = None
    for event_id in event_ids:
        mine = [v for v in verdicts.get(event_id, []) if v.user_id in listed]
        r = resolve(mine)
        if r is None:
            continue
        newest_answer_id = max([newest_answer_id or 0, *(v.id for v in mine)])
        e = events[event_id]
        deciding = next(v for v in mine if v.id == r.deciding[0])
        outcome = r.outcome[0]
        score = e["best_accuracy"]
        boxes.append(
            Box(
                box_id=f"ev{event_id}",
                saved_frame_id=e["saved_frame_id"],
                camera=e["source_name"],
                ts=e["ts"],
                bbox=tuple(r.bbox or e["bbox"]),
                bbox_source="corrected" if r.bbox else "model",
                outcome=outcome,
                accepted_name=r.species,
                life_stage=r.life_stage,
                confidence=r.confidence,
                status=r.status,
                label_source=deciding.label_source,
                user_ids=tuple(r.user_ids),
                model_name=e["best_name"],
                model_score=None if score is None else round(float(score), 4),
                held_back=_first(
                    "disputed" if r.status == "disputed" else None,
                    "species waiting for approval" if event_id in pending else None,
                    _confidence_reason(r.confidence),
                    "no species named" if outcome == "species" and not r.species else None,
                    "no saved frame" if e["saved_frame_id"] is None else None,
                ),
            )
        )

    drawn = await fetch_all(conn, _DRAWN_SQL, (list(user_ids),))
    for b in drawn:
        species = b["outcome"] == "species"
        boxes.append(
            Box(
                box_id=f"add{b['id']}",
                saved_frame_id=b["saved_frame_id"],
                camera=b["source_name"],
                ts=b["ts"],
                bbox=tuple(b["bbox"]),
                bbox_source="drawn",
                outcome=b["outcome"],
                accepted_name=b["label"] if species else None,
                life_stage=b["life_stage"],
                confidence=b["confidence"],
                status="single",
                label_source=None,
                user_ids=(b["user_id"],),
                model_name=None,
                model_score=None,
                held_back=_first(
                    _confidence_reason(b["confidence"]),
                    "no species named" if species and not b["label"] else None,
                ),
            )
        )

    boxes.sort(key=_order)
    frame_ids = sorted({b.saved_frame_id for b in boxes if b.saved_frame_id is not None})
    frames = {
        r["id"]: Frame(
            saved_frame_id=r["id"],
            camera=r["source_name"],
            ts=r["ts"],
            image_path=r["image_path"],
            completed=r["completed"],
            model_box_ids=tuple(f"ev{i}" for i in r["event_ids"]),
        )
        for r in await fetch_all(conn, _FRAMES_SQL, (frame_ids,))
    }
    return Selection(
        boxes=tuple(boxes),
        frames=dict(sorted(frames.items())),
        sample_answers=sample_answers,
        newest_answer_id=newest_answer_id,
        newest_drawn_box_id=max((b["id"] for b in drawn), default=None),
    )


@dataclass(frozen=True)
class DetectorPlan:
    frames: tuple[int, ...]  # saved_frame_ids, ascending
    annotations: dict[int, tuple[Box, ...]]  # per included frame, in Selection order
    held_back: dict[int, str]  # saved_frame_id -> reason, for every other frame in the selection


def _frame_problem(frame: Frame, boxes: list[Box]) -> str | None:
    """Why the detector may not learn from `frame`, or None: a fish left unboxed would teach it
    that the fish is background, so every model box must have an answer that counts."""
    if not frame.completed:
        return "not marked complete"
    selected = {b.box_id for b in boxes}
    for box_id in frame.model_box_ids:
        if box_id not in selected:
            return f"box {box_id} has no answer from the listed accounts"
    for b in boxes:
        if b.held_back:
            return f"box {b.box_id} held back: {b.held_back}"
    return None


def detector_frames(sel: Selection) -> DetectorPlan:
    """The completed frames whose every box counts, with the boxes to annotate: species and
    "can't identify" (an animal is there), never "no animal" (that is background)."""
    by_frame: dict[int, list[Box]] = {}
    for b in sel.boxes:
        if b.saved_frame_id is not None:
            by_frame.setdefault(b.saved_frame_id, []).append(b)
    included: list[int] = []
    annotations: dict[int, tuple[Box, ...]] = {}
    held_back: dict[int, str] = {}
    for frame_id, frame in sorted(sel.frames.items()):
        boxes = by_frame.get(frame_id, [])
        problem = _frame_problem(frame, boxes)
        if problem:
            held_back[frame_id] = problem
            continue
        included.append(frame_id)
        annotations[frame_id] = tuple(b for b in boxes if b.outcome != NO_ANIMAL)
    return DetectorPlan(tuple(included), annotations, held_back)


# ----- the build_dataset job -------------------------------------------------------------

_APP_DIR = Path(__file__).resolve().parent
# main.py's rule: the repository root in a checkout, /app in the image.
_REPO_ROOT = _APP_DIR.parents[2] if _APP_DIR.parents[1].name == "services" else _APP_DIR.parent

# Species reviewers added carry the AphiaID they were checked against (0007).
_SUGGESTED_APHIA_SQL = """
    SELECT approved_name, aphia_id FROM species_suggestions
     WHERE status = 'approved' AND approved_name IS NOT NULL AND aphia_id IS NOT NULL
     ORDER BY id
"""


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H%M%SZ")


def _accounts() -> list[str]:
    raw = os.environ.get("DATASET_ACCOUNTS", "").strip()
    return [str(u) for u in json.loads(raw)] if raw else []


def _taxonomy_csv() -> Path:
    explicit = os.environ.get("REVIEW_TAXONOMY_CSV", "").strip()
    if explicit:
        return Path(explicit)
    assets = os.environ.get("REVIEW_ASSETS_DIR", "").strip()
    return (Path(assets) if assets else _REPO_ROOT / "assets") / "taxonomy.csv"


def _content_type(name: str) -> str:
    return CONTENT_TYPES.get(os.path.splitext(name)[1].lower(), "application/octet-stream")


def _read_frame(image_path: str) -> bytes:
    return open_read(frame_url(image_path))


def _latest(url: str) -> dict[str, Any]:
    latest = join(url, "latest.json")
    return json.loads(open_read(latest)) if exists(latest) else {}


def _write_snapshot(url: str, files: dict[str, bytes], provenance: dict[str, Any]) -> str:
    """The snapshot's files, then snapshot.json, then latest.json last: a run that dies part-way
    never leaves a half-written snapshot named as the latest. An existing snapshot is never
    written over."""
    stamp = _stamp()
    folder = join(url, stamp)
    if exists(join(folder, "snapshot.json")):
        raise RuntimeError(f"snapshot {stamp} already exists; run again in a second")
    for name in sorted(files):
        put(join(folder, name), files[name], _content_type(name))
    snapshot = {"snapshot": stamp, **provenance}
    put(join(folder, "snapshot.json"), json.dumps(snapshot, indent=1).encode(), "application/json")
    latest = {"snapshot": stamp, "sha256": provenance["content_hash"]}
    put(join(url, "latest.json"), json.dumps(latest).encode(), "application/json")
    return stamp


async def build_dataset(pool: AsyncConnectionPool) -> dict[str, Any]:
    """Snapshot DATASET_ACCOUNTS' verified boxes under DATASET_URL/<stamp>/ when anything
    changed since latest.json's. Accounts appear as ids only, here and in every file."""
    accounts = _accounts()
    if not accounts:
        return {"skipped": "DATASET_ACCOUNTS is empty"}
    url = os.environ.get("DATASET_URL", "").strip()
    if not url:
        raise RuntimeError("DATASET_ACCOUNTS is set but DATASET_URL is not")
    from .verified_dataset_files import build_files  # it imports this module

    async with pool.connection() as conn, read_only_transaction(conn):
        rows = await fetch_all(
            conn, "SELECT id, username FROM users WHERE username = ANY(%s)", (accounts,)
        )
        missing = len(set(accounts) - {r["username"] for r in rows})
        if missing:
            raise UnknownAccount(f"{missing} of the DATASET_ACCOUNTS match no review account")
        ids = sorted(r["id"] for r in rows)
        sel = await select(conn, ids)
        suggested = await fetch_all(conn, _SUGGESTED_APHIA_SQL)

    aphia = {r["approved_name"]: r["aphia_id"] for r in suggested}
    aphia |= taxonomy.aphia_by_accepted(taxonomy.read(_taxonomy_csv()))
    plan = detector_frames(sel)
    files, counts = await asyncio.to_thread(
        build_files, sel, plan, _read_frame, aphia, ZoneInfo(site_tz())
    )
    digest = content_hash(files)
    result: dict[str, Any] = {"snapshot": None, "unchanged": True, "accounts": len(ids)}
    if (await asyncio.to_thread(_latest, url)).get("sha256") != digest:
        provenance = {
            "git_sha": os.environ.get("GIT_SHA") or None,
            "content_hash": digest,
            "account_ids": ids,
            "newest_answer_id": sel.newest_answer_id,
            "newest_drawn_box_id": sel.newest_drawn_box_id,
            "counts": counts,
        }
        stamp = await asyncio.to_thread(_write_snapshot, url, files, provenance)
        result |= {"snapshot": stamp, "unchanged": False}
    return result | {"counts": counts}

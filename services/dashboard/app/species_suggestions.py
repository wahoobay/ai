"""Species a reviewer adds because the list lacks them (spec 2026-11-21, "The species list").

A suggestion waits ('pending') until an admin approves or rejects it. While it waits only its
suggester, and admins, can pick it (pending_entries). Once approved it joins everyone's list:
merged_catalogue() is the file-built catalogue plus every approved suggestion. The dashboard
keeps no state between requests and may run as several instances, so each request reads a
cheap stamp of the approved set and the merge is rebuilt only when the stamp changes.

Duplicates are judged on name and stage together, the scientific name mapped through the
taxonomy first; a new stage of a listed species is a new choice.

An admin's decision settles the answers linked to a suggestion in place ("Approving and
rejecting"): approval gives them the approved name and stage, rejection the listed species it
really is or can't-identify. They keep label_source 'suggested' and their link, and the row keeps
what was suggested, so the trail survives. "Not this species" (detach) turns one box's answers
into can't-identify and unlinks them before the rest are decided.
"""

from __future__ import annotations

import re
from typing import Literal

from psycopg.pq import TransactionStatus
from pydantic import BaseModel, Field

from .auth import effective_role
from .config import location_id
from .review_catalogue import Catalogue, CatalogueEntry
from .review_queue import Conflict, Invalid, NotFound, fetch_all, fetch_one

STAGES: tuple[str | None, ...] = (
    None,
    "juvenile",
    "intermediate",
    "adult",
    "initial phase",
    "terminal phase",
)

STATUS_TEXT = {"pending": "waiting for approval", "approved": "added", "rejected": "not added"}

_SCIENTIFIC = re.compile(r"^[A-Z][a-z]+(?: [a-z]+(?:-[a-z]+)?){0,2}$")

# One reviewer's suggestions are made one at a time, so a double submit cannot slip two
# identical pending rows past the duplicate check (namespaced, transaction-scoped, as
# review_queue.EVENT_LOCK_SQL).
_SUGGESTER_LOCK_SQL = (
    "SELECT pg_advisory_xact_lock(hashtextextended('species-suggestion:' || %s::text, 0))"
)

_ENTRY_COLUMNS = "id, common_name, scientific_name, life_stage"

_MINE_KEYS = (
    "id",
    "common_name",
    "scientific_name",
    "life_stage",
    "decision_note",
    "rejected_to_name",
    "rejected_to_stage",
    "approved_common",
)


DECIDED_SHOWN = 50  # the admin panel's history of decided suggestions

# A can't-identify answer names nothing: no species, stage or individual, and it is 'certain'
# (validate_label refuses 'not sure' and a named individual without a species).
_CANT_IDENTIFY = """unidentifiable = true, corrected_name = NULL, corrected_species_id = NULL,
                    life_stage = NULL, is_endorsement = false, confidence = 'certain',
                    celebrity_id = NULL"""


class SuggestionIn(BaseModel):
    common_name: str = Field(..., min_length=1, max_length=120)
    scientific_name: str | None = Field(None, max_length=120)
    life_stage: str | None = Field(None, max_length=40)
    note: str | None = Field(None, max_length=500)


class ApproveIn(BaseModel):
    scientific_name: str = Field(..., min_length=1, max_length=120)
    common_name: str = Field(..., min_length=1, max_length=120)
    life_stage: str | None = Field(None, max_length=40)
    note: str | None = Field(None, max_length=500)


class RejectIn(BaseModel):
    """Either "it's actually" a listed choice (`as_label`, `as_life_stage`) or can't identify."""

    as_label: str | None = Field(None, max_length=200)
    as_life_stage: str | None = Field(None, max_length=40)
    unidentifiable: bool = False
    note: str | None = Field(None, max_length=500)


class DetachIn(BaseModel):
    event_id: int


def _squash(raw: str | None) -> str | None:
    """Runs of whitespace made one space; None when nothing is left."""
    return " ".join((raw or "").split()) or None


def _stage(raw: str | None) -> str | None:
    stage = (_squash(raw) or "").lower() or None
    if stage not in STAGES:
        raise Invalid("pick a stage from the list")
    return stage


def _common(raw: str | None) -> str:
    common = _squash(raw)
    if common is None:
        raise Invalid("write the fish's common name")
    return common


def _note(raw: str | None) -> str | None:
    return (raw or "").strip() or None


def clean_scientific(raw: str | None) -> str | None:
    """'  Gymnothorax   moringa ' -> 'Gymnothorax moringa'; None for nothing given."""
    name = _squash(raw)
    if name is not None and not _SCIENTIFIC.fullmatch(name):
        raise Invalid("write the scientific name as Genus species")
    return name


def provisional_name(row: dict, catalogue) -> str:
    """The name a pending answer is stored under: the accepted scientific name when the
    suggester gave one, otherwise the common name."""
    if row["scientific_name"]:
        return catalogue.accepted(row["scientific_name"])
    return row["common_name"]


def entry_json(row: dict, catalogue) -> dict:
    """A pending suggestion as a species-picker entry."""
    return {
        "key": f"suggestion:{row['id']}",
        "name": provisional_name(row, catalogue),
        "life_stage": row["life_stage"],
        "common": f"{row['common_name']} (waiting for approval)",
        "suggestion_id": row["id"],
        "pending": True,
        "in_species_log": False,
        "in_classifier": False,
    }


def _is_admin(user) -> bool:
    return effective_role(user.status, user.role) == "admin"


def _listed_as(entry: CatalogueEntry) -> Conflict:
    return Conflict(f"already on the list as {entry.common_name or entry.name}")


async def create_suggestion(conn, *, user, body: SuggestionIn, catalogue) -> dict:
    """Record `user`'s suggestion, pending, and return its picker entry."""
    stage = _stage(body.life_stage)
    common = _common(body.common_name)
    scientific = clean_scientific(body.scientific_name)
    if scientific is not None:
        listed = catalogue.entry(catalogue.accepted(scientific), stage)
        if listed is not None:
            raise _listed_as(listed)
    folded = common.casefold()
    for e in catalogue.entries:
        if (_squash(e.common_name) or "").casefold() == folded:
            raise _listed_as(e)
    async with conn.transaction():
        await conn.execute(_SUGGESTER_LOCK_SQL, (user.id,))
        waiting = await fetch_all(
            conn,
            """SELECT common_name, life_stage FROM species_suggestions
                WHERE suggested_by = %s AND status = 'pending'""",
            (user.id,),
        )
        if any(r["common_name"].casefold() == folded and r["life_stage"] == stage for r in waiting):
            raise Conflict("you already suggested this; it is waiting for approval")
        row = await fetch_one(
            conn,
            f"""INSERT INTO species_suggestions (common_name, scientific_name, life_stage, note,
                                                 suggested_by, location_id)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING {_ENTRY_COLUMNS}""",
            (common, scientific, stage, _note(body.note), user.id, location_id()),
        )
    return entry_json(row, catalogue)


async def pending_entries(conn, *, user, catalogue) -> list[dict]:
    """The pending suggestions `user` may pick: their own; every one for an admin."""
    rows = await fetch_all(
        conn,
        f"""SELECT {_ENTRY_COLUMNS} FROM species_suggestions
             WHERE status = 'pending' AND (%(admin)s OR suggested_by = %(uid)s)
             ORDER BY created_at, id""",
        {"admin": _is_admin(user), "uid": user.id},
    )
    return [entry_json(r, catalogue) for r in rows]


PENDING_SUFFIX = " (new, waiting for approval)"  # what the review UI adds to such a name


async def pending_species_events(conn, event_ids: list[int] | None = None) -> dict[int, str]:
    """Each box (of `event_ids`, or every one) with an answer linked to a pending suggestion,
    mapped to that suggestion's common name (the latest such answer's). The Python sibling of
    exports.hide_pending_species: these boxes stay out of agreement and verified counts."""
    rows = await fetch_all(
        conn,
        """SELECT DISTINCT ON (c.event_id) c.event_id, ss.common_name
             FROM detection_corrections c
             JOIN species_suggestions ss ON ss.id = c.species_suggestion_id
            WHERE ss.status = 'pending'
              AND (%(ids)s::bigint[] IS NULL OR c.event_id = ANY(%(ids)s::bigint[]))
            ORDER BY c.event_id, c.created_at DESC, c.id DESC""",
        {"ids": None if event_ids is None else list(event_ids)},
    )
    return {r["event_id"]: r["common_name"] for r in rows}


async def list_mine(conn, *, user) -> list[dict]:
    """`user`'s own suggestions, newest first, each with what happened to it."""
    rows = await fetch_all(
        conn,
        f"""SELECT status, {", ".join(_MINE_KEYS)} FROM species_suggestions
             WHERE suggested_by = %s
             ORDER BY created_at DESC, id DESC""",
        (user.id,),
    )
    return [
        {**{k: r[k] for k in _MINE_KEYS}, "status_text": STATUS_TEXT[r["status"]]} for r in rows
    ]


# ---- an admin decides ----
#
# Each decision locks the suggestion row first and takes no event lock: submit_verdict locks
# the event, then its review item, then the suggestion, so an event lock taken after the
# suggestion's could deadlock against it. The suggestion's lock alone is enough here, because
# every answer that links to a suggestion is written under it.


async def _lock_pending(conn, suggestion_id: int) -> dict:
    row = await fetch_one(
        conn, "SELECT * FROM species_suggestions WHERE id = %s FOR UPDATE", (suggestion_id,)
    )
    if row is None:
        raise NotFound("no such species suggestion")
    if row["status"] != "pending":
        raise Conflict("this suggestion was already decided")
    return row


async def _name_answers(conn, suggestion_id: int, name: str, stage: str | None, catalogue) -> int:
    """Give every answer linked to the suggestion `name` at `stage`; is_endorsement is
    recomputed against each box's model guess. Returns how many answers changed."""
    rows = await fetch_all(
        conn,
        """SELECT c.id, de.best_species_id FROM detection_corrections c
             JOIN detection_events de ON de.id = c.event_id
            WHERE c.species_suggestion_id = %s""",
        (suggestion_id,),
    )
    endorsed = [r["id"] for r in rows if catalogue.accepted(r["best_species_id"]) == name]
    cur = await conn.execute(
        """UPDATE detection_corrections
              SET corrected_name = %(name)s, corrected_species_id = %(name)s,
                  life_stage = %(stage)s, is_endorsement = (id = ANY(%(endorsed)s::bigint[]))
            WHERE species_suggestion_id = %(sid)s""",
        {"name": name, "stage": stage, "endorsed": endorsed, "sid": suggestion_id},
    )
    return cur.rowcount


async def _record_decision(conn, suggestion_id: int, *, user, status: str, note, **columns):
    sets = "".join(f", {column} = %({column})s" for column in columns)
    await conn.execute(
        f"""UPDATE species_suggestions
               SET status = %(status)s, decided_by = %(uid)s, decided_at = now(),
                   decision_note = %(note)s{sets}
             WHERE id = %(sid)s""",
        {**columns, "status": status, "uid": user.id, "note": _note(note), "sid": suggestion_id},
    )


async def approve(conn, *, user, suggestion_id: int, body: ApproveIn, catalogue) -> dict:
    """Add the suggestion to the list as the admin's (corrected) names and stage, and settle
    every answer linked to it. `catalogue` is the merged one: a choice approved from another
    suggestion is already listed, and that suggestion is rejected "as" it instead."""
    scientific = clean_scientific(body.scientific_name)
    if scientific is None:
        raise Invalid("write the scientific name as Genus species")
    name = catalogue.accepted(scientific)
    common, stage = _common(body.common_name), _stage(body.life_stage)
    async with conn.transaction():
        await _lock_pending(conn, suggestion_id)
        listed = catalogue.entry(name, stage)
        if listed is not None:
            x = listed.common_name or listed.name
            raise Conflict(f"already listed as {x}; reject it as 'it's actually {x}' instead")
        answers = await _name_answers(conn, suggestion_id, name, stage, catalogue)
        await _record_decision(
            conn,
            suggestion_id,
            user=user,
            status="approved",
            note=body.note,
            approved_name=name,
            approved_common=common,
            approved_stage=stage,
        )
    return {"answers": answers}


async def reject(conn, *, user, suggestion_id: int, body: RejectIn, catalogue) -> dict:
    """Leave the suggestion off the list. Its answers become the listed choice it really is
    (`as_label` at `as_life_stage`, from the merged `catalogue`: Ruling P3) or can't-identify."""
    label = _squash(body.as_label)
    if (label is not None) == body.unidentifiable:  # both, or neither
        raise Invalid("say what it is, or that it can't be identified")
    entry = None
    if label is not None:
        entry = catalogue.entry(label, body.as_life_stage)
        if entry is None:
            raise Invalid("pick a species from the list")
    async with conn.transaction():
        await _lock_pending(conn, suggestion_id)
        if entry is not None:
            answers = await _name_answers(
                conn, suggestion_id, entry.name, entry.life_stage, catalogue
            )
        else:
            cur = await conn.execute(
                f"UPDATE detection_corrections SET {_CANT_IDENTIFY}"
                " WHERE species_suggestion_id = %s",
                (suggestion_id,),
            )
            answers = cur.rowcount
        await _record_decision(
            conn,
            suggestion_id,
            user=user,
            status="rejected",
            note=body.note,
            rejected_to_name=entry.name if entry else None,
            rejected_to_stage=entry.life_stage if entry else None,
        )
    return {"answers": answers}


async def detach(conn, *, user, suggestion_id: int, event_id: int) -> dict:
    """The per-box "Not this species": the answers on `event_id` that use the pending
    suggestion become can't-identify and no longer link to it, so its decision leaves them
    alone."""
    async with conn.transaction():
        await _lock_pending(conn, suggestion_id)
        cur = await conn.execute(
            f"""UPDATE detection_corrections SET {_CANT_IDENTIFY}, species_suggestion_id = NULL
                 WHERE species_suggestion_id = %s AND event_id = %s""",
            (suggestion_id, event_id),
        )
        if not cur.rowcount:
            raise NotFound("no answer on this box uses this suggestion")
    return {"answers": cur.rowcount}


async def list_for_admin(conn, *, status: Literal["pending", "decided"]) -> list[dict]:
    """The admin panel's suggestions, newest first (by decision, once decided): every pending
    one, or the last DECIDED_SHOWN decided ones. Each has its columns, `suggested_by_name` and
    `answers`, the number of answers linked to it."""
    rows = await fetch_all(
        conn,
        """SELECT ss.*, u.display_name AS suggested_by_name,
                  (SELECT count(*) FROM detection_corrections c
                    WHERE c.species_suggestion_id = ss.id) AS answers
             FROM species_suggestions ss
             JOIN users u ON u.id = ss.suggested_by
            WHERE (ss.status = 'pending') = %(pending)s
            ORDER BY COALESCE(ss.decided_at, ss.created_at) DESC, ss.id DESC
            LIMIT %(limit)s""",
        {"pending": status == "pending", "limit": None if status == "pending" else DECIDED_SHOWN},
    )
    for r in rows:
        for key in ("created_at", "decided_at"):
            r[key] = r[key].isoformat() if r[key] is not None else None
    return rows


async def pending_count(conn) -> int:
    row = await fetch_one(
        conn, "SELECT count(*) AS n FROM species_suggestions WHERE status = 'pending'"
    )
    return row["n"]


async def suggestion_boxes(conn, *, suggestion_id: int, page: int, per_page: int = 24) -> dict:
    """The boxes answered with the suggestion, `per_page` at a time, each with who answered
    it (the latest such answer's reviewer)."""
    total = await fetch_one(
        conn,
        """SELECT count(DISTINCT event_id) AS n FROM detection_corrections
            WHERE species_suggestion_id = %s""",
        (suggestion_id,),
    )
    rows = await fetch_all(
        conn,
        """SELECT * FROM (
               SELECT DISTINCT ON (c.event_id) c.event_id, COALESCE(u.display_name, c.reviewer) AS by
                 FROM detection_corrections c
                 LEFT JOIN users u ON u.id = c.user_id
                WHERE c.species_suggestion_id = %(sid)s
                ORDER BY c.event_id, c.created_at DESC, c.id DESC
           ) b
           ORDER BY b.event_id
           LIMIT %(limit)s OFFSET %(offset)s""",
        {"sid": suggestion_id, "limit": per_page, "offset": page * per_page},
    )
    return {
        "total": total["n"],
        "boxes": [
            {
                "event_id": r["event_id"],
                "zoom_url": f"/api/review/boxes/{r['event_id']}/zoom.jpg",
                "by": r["by"],
            }
            for r in rows
        ],
    }


# The last merge, as (key, base, merged). Holding `base` keeps its id(), part of the key,
# from being reused by a later file catalogue while this entry stands.
_merged: tuple | None = None


async def merged_catalogue(conn, base: Catalogue) -> Catalogue:
    """`base` plus one entry per approved suggestion, rebuilt only when the approved set's
    stamp (count, latest decided_at) or `base` changes.

    The reads run on `conn`, the connection the caller already holds (Ruling P1). If it was
    idle, they are rolled back afterwards: an implicit transaction left open would turn the
    caller's next conn.transaction() into a savepoint and move its commit."""
    global _merged
    idle = conn.info.transaction_status == TransactionStatus.IDLE
    try:
        stamp = await fetch_one(
            conn,
            """SELECT count(*) AS n, max(decided_at) AS latest
                 FROM species_suggestions WHERE status = 'approved'""",
        )
        if not stamp["n"]:
            return base
        key = (id(base), stamp["n"], stamp["latest"])
        if _merged is not None and _merged[0] == key:
            return _merged[2]
        rows = await fetch_all(
            conn,
            """SELECT approved_name, approved_stage, approved_common FROM species_suggestions
                WHERE status = 'approved' AND approved_name IS NOT NULL
                ORDER BY decided_at, id""",
        )
    finally:
        if idle:
            await conn.rollback()
    merged = base.with_entries(_approved_entries(base, rows))
    _merged = (key, base, merged)
    return merged


def _approved_entries(base: Catalogue, rows: list[dict]) -> list[CatalogueEntry]:
    added: dict[tuple, CatalogueEntry] = {}
    for r in rows:
        name, stage = r["approved_name"], r["approved_stage"]
        # A choice the files list too (a sheet edit after the approval) is theirs.
        if base.entry(name, stage) is not None or (name, stage) in added:
            continue
        e = CatalogueEntry(name=name, life_stage=stage, common_name=r["approved_common"])
        taxon = base.taxon(name)  # a new stage of a listed species shares its taxonomy
        if taxon is not None:
            e.aphia_id, e.rank, e.resolved = taxon.aphia_id, taxon.rank, taxon.resolved
            e.family, e.order = taxon.family, taxon.order
            e.class_, e.phylum = taxon.class_, taxon.phylum
        added[(name, stage)] = e
    return list(added.values())

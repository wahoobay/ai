"""Species a reviewer adds because the list lacks them (spec 2026-11-21, "The species list").

A suggestion waits ('pending') until an admin approves or rejects it. While it waits only its
suggester, and admins, can pick it (pending_entries). Once approved it joins everyone's list:
merged_catalogue() is the file-built catalogue plus every approved suggestion. The dashboard
keeps no state between requests and may run as several instances, so each request reads a
cheap stamp of the approved set and the merge is rebuilt only when the stamp changes.

Duplicates are judged on name and stage together, the scientific name mapped through the
taxonomy first; a new stage of a listed species is a new choice.
"""

from __future__ import annotations

import re

from psycopg.pq import TransactionStatus
from pydantic import BaseModel, Field

from .auth import effective_role
from .config import location_id
from .review_catalogue import Catalogue, CatalogueEntry
from .review_queue import Conflict, Invalid, fetch_all, fetch_one

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


class SuggestionIn(BaseModel):
    common_name: str = Field(..., min_length=1, max_length=120)
    scientific_name: str | None = Field(None, max_length=120)
    life_stage: str | None = Field(None, max_length=40)
    note: str | None = Field(None, max_length=500)


def _squash(raw: str | None) -> str | None:
    """Runs of whitespace made one space; None when nothing is left."""
    return " ".join((raw or "").split()) or None


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
    stage = (_squash(body.life_stage) or "").lower() or None
    if stage not in STAGES:
        raise Invalid("pick a stage from the list")
    common = _squash(body.common_name)
    if common is None:
        raise Invalid("write the fish's common name")
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
            (common, scientific, stage, (body.note or "").strip() or None, user.id, location_id()),
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

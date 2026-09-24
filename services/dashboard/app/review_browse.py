"""Progress, browse, disputes, agreement, the date window and pool status
(spec §9, Browse; §8, Agreement).

Browse follows the same rule as whole-frame mode: a box waiting for a
second opinion shows as "awaiting a second opinion" with no answer to
everyone except the volunteer who gave the first answer, so browsing
cannot contaminate a spot-check. There is no admin exception -- spec §7
makes none, because admins can be served a spot-check themselves.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime
from typing import Literal
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from .review_labels import agreement_summary, load_verdicts, outcome_of, resolve
from .review_pool import read_window, retire_outside_window
from .review_queue import Invalid, fetch_all, fetch_one

LOCAL_TZ = ZoneInfo("America/New_York")
STATUSES = ("any", "verified", "agreed", "adjudicated", "disputed", "unidentifiable",
            "no_animal", "in_queue")
WHO_VALUES = ("anyone", "me", "session")
LANE_VALUES = ("any", "check", "identify")
PAGE_SIZE = 48

# browse() and progress_summary() both resolve every verdict and are the
# heaviest queries in this module; bounded the same way review_pool.py
# bounds its own probes -- a transaction-local timeout set with
# set_config(..., true) inside an explicit transaction, never a
# session-level SET (that would outlive the request on a pooled
# connection). Fix round 1, findings 1 and 6.
STATEMENT_TIMEOUT_MS = 10_000

# A "sane" calendar range for any date a caller can send us (browse's
# date_from/date_to, the window's start/end). Anything outside it is
# almost certainly a typo, and 9999-12-31 + 1 day overflows Python's
# date/datetime range and would otherwise surface as a 500. Fix round 1,
# finding 5.
MIN_DATE = date(2000, 1, 1)
MAX_DATE = date(2100, 12, 31)


@dataclass
class BrowseFilters:
    status: str = "any"
    species: str | None = None
    camera: str | None = None
    who: str = "anyone"            # anyone | me | session
    lane: str = "any"              # any | check | identify
    date_from: date | None = None
    date_to: date | None = None


class WindowIn(BaseModel):
    start: date | None = None
    end: date | None = None


class AdjudicateIn(BaseModel):
    outcome: Literal["species", "unidentifiable", "no_animal"]
    label: str | None = Field(None, max_length=200)
    life_stage: str | None = Field(None, max_length=40)
    bbox: list[int] | None = None


def local_day_start(d: date) -> datetime:
    return datetime.combine(d, dtime.min, tzinfo=LOCAL_TZ)


def _check_date(d: date | None, label: str) -> None:
    if d is not None and not (MIN_DATE <= d <= MAX_DATE):
        raise Invalid(f"{label} must be between {MIN_DATE.isoformat()} and {MAX_DATE.isoformat()}")


async def _set_statement_timeout(conn) -> None:
    """Must run inside an explicit `conn.transaction()`: set_config's
    third argument (true = transaction-local) only means anything for the
    life of the current transaction, and pooled connections can arrive
    with autocommit=True, where a bare statement is its own transaction
    of one (global constraint; review_pool.py's own docstring)."""
    await conn.execute("SELECT set_config('statement_timeout', %s, true)",
                       (str(STATEMENT_TIMEOUT_MS),))


async def _resolved(conn) -> dict:
    out = {}
    for event_id, verdicts in (await load_verdicts(conn)).items():
        r = resolve(verdicts)
        if r is not None:
            out[event_id] = (r, verdicts)
    return out


async def _pending_spot_checks(conn) -> set[int]:
    return {r["event_id"] for r in await fetch_all(conn, """
        SELECT event_id FROM review_items
         WHERE pass = 2 AND status IN ('open', 'claimed')""")}


async def progress_summary(conn, *, pool_obj, catalogue, session_id, target: int) -> dict:
    """Resolves every verdict exactly once, under the same transaction-
    local timeout as browse (fix round 1, finding 6): the route also
    awaits pool_obj.refresh_progress() beforehand to freshen the
    per-species baseline/verified cache this reads below, and that call
    has its own independent 5s timeout and stale-flag handling (Task 6's
    fix rounds; the route still serves this summary with stale=true when
    only that refresh times out). If this function's own queries time
    out instead, QueryCanceled propagates so the route can answer with a
    503 rather than a hang or a 500 -- there is no cached data here to
    fall back to."""
    async with conn.transaction():
        await _set_statement_timeout(conn)
        resolved = await _resolved(conn)
        verified = [r for r, _v in resolved.values() if r.is_verified]
        added = await fetch_all(conn, """SELECT label, outcome FROM review_added_boxes
                                          WHERE removed_at IS NULL""")
        species_seen = ({r.species for r in verified if r.species}
                        | {a["label"] for a in added if a["outcome"] == "species"})
        frames = (await fetch_one(conn, "SELECT count(*) AS n FROM review_frames"))["n"]
        session = 0
        if session_id is not None:
            session = (await fetch_one(conn, """SELECT count(*) AS n FROM detection_corrections
                                                 WHERE session_id = %s""", (session_id,)))["n"]
        open_counts = {"check": 0, "identify": 0, "spot_checks": 0}
        for r in await fetch_all(conn, """SELECT lane, pass, count(*) AS n FROM review_items
                                           WHERE status IN ('open', 'claimed')
                                           GROUP BY lane, pass"""):
            open_counts["spot_checks" if r["pass"] == 2 else r["lane"]] += r["n"]
        species = [{"name": name, "common": catalogue.common_name(name),
                    "baseline": pool_obj.baseline(name) if pool_obj else 0,
                    "verified": pool_obj.verified(name) if pool_obj else 0,
                    "target": target} for name in catalogue.names()]
    species.sort(key=lambda s: (s["baseline"] + s["verified"], s["name"]))
    return {"kpis": {"verified_boxes": len(verified) + len(added),
                     "species_with_verified": len(species_seen),
                     "complete_frames": frames, "session_verified": session,
                     "open": open_counts},
            "species": species, "target": target}


def _status_matches(r, status: str) -> bool:
    if status == "any":
        return True
    if status == "verified":
        return r.is_verified
    if status in ("agreed", "adjudicated", "disputed"):
        return r.status == status
    return r.is_verified and r.outcome[0] == status       # unidentifiable | no_animal


async def browse(conn, *, catalogue, user, filters: BrowseFilters, page: int,
                 page_size: int = PAGE_SIZE) -> dict:
    if filters.status not in STATUSES:
        raise Invalid("unknown status filter")
    if filters.who not in WHO_VALUES:
        raise Invalid("unknown who filter")
    if filters.lane not in LANE_VALUES:
        raise Invalid("unknown lane filter")
    _check_date(filters.date_from, "date_from")
    _check_date(filters.date_to, "date_to")
    start = local_day_start(filters.date_from) if filters.date_from else None
    end = local_day_start(filters.date_to + timedelta(days=1)) if filters.date_to else None

    async with conn.transaction():
        await _set_statement_timeout(conn)

        if filters.status == "in_queue":
            rows = await fetch_all(conn, """
                SELECT event_id, lane, source_name, ts, route_species, saved_frame_id, added_at
                  FROM review_items
                 WHERE pass = 1 AND status IN ('open', 'claimed')
                   AND (%(lane)s = 'any' OR lane = %(lane)s)
                   AND (%(camera)s::text IS NULL OR source_name = %(camera)s)
                   AND (%(species)s::text IS NULL OR route_species = %(species)s)
                   AND (%(start)s::timestamptz IS NULL OR ts >= %(start)s)
                   AND (%(end)s::timestamptz IS NULL OR ts < %(end)s)
                 ORDER BY added_at DESC, id DESC
                 LIMIT %(limit)s OFFSET %(offset)s""", {
                "lane": filters.lane, "camera": filters.camera, "species": filters.species,
                "start": start, "end": end, "limit": page_size + 1, "offset": page * page_size})
            cells = [{"kind": "queue", "event_id": r["event_id"], "added_id": None,
                      "saved_frame_id": r["saved_frame_id"],
                      "zoom_url": f"/api/review/boxes/{r['event_id']}/zoom.jpg",
                      "status": "in_queue", "outcome": None, "label": r["route_species"],
                      "common": catalogue.common_name(r["route_species"]),
                      "camera": r["source_name"], "ts": r["ts"].isoformat(), "lane": r["lane"],
                      "mine": False, "reviewers": [], "last_at": r["added_at"].isoformat()}
                     for r in rows[:page_size]]
            return {"cells": cells, "page": page, "has_more": len(rows) > page_size}

        resolved = await _resolved(conn)
        pending = await _pending_spot_checks(conn)
        ids = list(resolved)
        # detection_events by primary key only, for the full candidate set:
        # the camera/date/lane filters need source_name/ts/best_species_id,
        # and the WHERE id = ANY(...) never scans the table. saved_frames
        # is untouched here -- see the page-only lookup below (fix round 1,
        # finding 1).
        meta = {r["id"]: r for r in await fetch_all(conn, """
            SELECT id, source_name, ts, frame_id, best_species_id FROM detection_events
             WHERE id = ANY(%s)""", (ids,))} if ids else {}
        names = {r["id"]: r["display_name"]
                 for r in await fetch_all(conn, "SELECT id, display_name FROM users")}
        cells = []
        for event_id, (r, verdicts) in resolved.items():
            m = meta.get(event_id)
            if m is None:
                continue
            mine = any(v.user_id == user.id for v in verdicts)
            # No admin exception: while a spot-check on this box is open or
            # claimed, its answer stays hidden from everyone but whoever gave
            # the first verdict, admins included (spec Sec7).
            hidden = event_id in pending and not mine
            if hidden and (filters.status != "any" or filters.species):
                continue
            if not hidden and not _status_matches(r, filters.status):
                continue
            if filters.species and r.species != filters.species:
                continue
            # A hidden cell's lane is nulled too: "check" would tell the
            # viewer the model had a species guess, which the blind pass-2
            # payload itself hides (review_queue.build_payload). A lane
            # filter therefore never matches a hidden cell, whatever its
            # real lane is (fix round 1, finding 3).
            lane = None if hidden else ("check" if m["best_species_id"] else "identify")
            if ((filters.camera and m["source_name"] != filters.camera)
                    or (filters.lane != "any" and (hidden or lane != filters.lane))
                    or (start and m["ts"] < start) or (end and m["ts"] >= end)):
                continue
            if filters.who == "me" and not mine:
                continue
            if filters.who == "session" and not (
                    user.session_id is not None
                    and any(v.session_id == user.session_id for v in verdicts)):
                continue
            cell = {"kind": "box", "event_id": event_id, "added_id": None,
                    "saved_frame_id": None,          # filled in below, page cells only
                    "zoom_url": f"/api/review/boxes/{event_id}/zoom.jpg",
                    "status": r.status, "outcome": r.outcome[0], "label": r.species,
                    "common": catalogue.common_name(r.species), "camera": m["source_name"],
                    "ts": m["ts"].isoformat(), "lane": lane, "mine": mine,
                    "reviewers": sorted({names.get(v.user_id, "?")
                                         for v in verdicts if v.user_id}),
                    "last_at": max(v.created_at for v in verdicts)}
            if hidden:
                cell.update(status="awaiting_second_opinion", outcome=None, label=None,
                            common=None, reviewers=[])
            cells.append(cell)
        # Drawn boxes have a single, immediate answer -- never pending,
        # agreed, adjudicated or disputed -- so they belong under "any",
        # "verified" (any outcome) and their own specific outcome filter
        # ("unidentifiable"; "no_animal" naturally matches none, since a
        # drawn box can never carry that outcome). Fix round 1, finding 7.
        if (filters.status in ("any", "verified", "unidentifiable", "no_animal")
                and filters.lane == "any"):
            for a in await fetch_all(conn, """
                    SELECT a.id, a.saved_frame_id, a.label, a.outcome, a.user_id, a.session_id,
                           a.created_at, sf.source_name, sf.ts
                      FROM review_added_boxes a JOIN saved_frames sf ON sf.id = a.saved_frame_id
                     WHERE a.removed_at IS NULL"""):
                if filters.status not in ("any", "verified") and a["outcome"] != filters.status:
                    continue
                if ((filters.species and a["label"] != filters.species)
                        or (filters.camera and a["source_name"] != filters.camera)
                        or (start and a["ts"] < start) or (end and a["ts"] >= end)
                        or (filters.who == "me" and a["user_id"] != user.id)
                        or (filters.who == "session" and (
                            user.session_id is None or a["session_id"] != user.session_id))):
                    continue
                cells.append({"kind": "added", "event_id": None, "added_id": a["id"],
                              "saved_frame_id": a["saved_frame_id"],
                              "zoom_url": f"/api/review/added/{a['id']}/zoom.jpg",
                              "status": "single", "outcome": a["outcome"], "label": a["label"],
                              "common": catalogue.common_name(a["label"]),
                              "camera": a["source_name"], "ts": a["ts"].isoformat(),
                              "lane": "drawn", "mine": a["user_id"] == user.id,
                              "reviewers": [names.get(a["user_id"], "?")],
                              "last_at": a["created_at"]})
        cells.sort(key=lambda c: c["last_at"], reverse=True)
        page_cells = cells[page * page_size:(page + 1) * page_size]
        for c in page_cells:
            c["last_at"] = c["last_at"].isoformat()

        # saved_frames is queried only now, for the <= page_size "box"
        # cells on the page being returned -- one exact (source_name,
        # frame_id, ts) match each, tie-broken the same way as
        # review_queue.event_with_frame's _EVENT_FRAME_SQL (ORDER BY id
        # LIMIT 1), batched as a LATERAL join over the page's rows rather
        # than one round trip per cell (fix round 1, finding 1).
        box_ids = [c["event_id"] for c in page_cells if c["kind"] == "box"]
        if box_ids:
            rows = await fetch_all(conn, """
                SELECT t.event_id, sf.id AS saved_frame_id
                  FROM unnest(%s::bigint[], %s::text[], %s::bigint[], %s::timestamptz[])
                       AS t(event_id, source_name, frame_id, ts)
                  LEFT JOIN LATERAL (
                      SELECT id FROM saved_frames
                       WHERE frame_id = t.frame_id AND source_name = t.source_name
                         AND ts = t.ts
                       ORDER BY id LIMIT 1
                  ) sf ON true""",
                (box_ids, [meta[eid]["source_name"] for eid in box_ids],
                 [meta[eid]["frame_id"] for eid in box_ids],
                 [meta[eid]["ts"] for eid in box_ids]))
            frame_by_event = {r["event_id"]: r["saved_frame_id"] for r in rows}
            for c in page_cells:
                if c["kind"] == "box":
                    c["saved_frame_id"] = frame_by_event.get(c["event_id"])

    return {"cells": page_cells, "page": page, "total": len(cells),
            "has_more": len(cells) > (page + 1) * page_size}


async def list_disputes(conn, *, catalogue) -> list[dict]:
    names = {r["id"]: r["display_name"]
             for r in await fetch_all(conn, "SELECT id, display_name FROM users")}

    def side(v):
        kind, *rest = outcome_of(v)
        label = rest[0] if rest else None
        return {"outcome": kind, "label": label, "common": catalogue.common_name(label),
                "life_stage": v.life_stage, "by": names.get(v.user_id, "?"),
                "at": v.created_at.isoformat()}

    out = [{"event_id": event_id, "zoom_url": f"/api/review/boxes/{event_id}/zoom.jpg",
            "first": side(r.first), "second": side(r.second)}
           for event_id, (r, _v) in (await _resolved(conn)).items() if r.status == "disputed"]
    return sorted(out, key=lambda d: d["event_id"])


async def agreement(conn) -> dict:
    return agreement_summary({event_id: r for event_id, (r, _v) in (await _resolved(conn)).items()})


async def get_window(conn) -> dict:
    start, end = await read_window(conn)
    return {"start": start.isoformat() if start else None,
            "end": end.isoformat() if end else None}


async def set_window(conn, *, admin_id: int, start: date | None, end: date | None) -> dict:
    """Both days are local (America/New_York) and inclusive: the stored end
    is local midnight at the start of the day after `end` (Review Focus 4).
    Both bounds are stored as timezone-aware ISO strings (local_day_start
    always attaches LOCAL_TZ), because read_window feeds them straight to
    datetime.fromisoformat for the background pool to compare against
    now(UTC) -- a naive or date-only value would raise TypeError there on
    every run."""
    _check_date(start, "start")
    _check_date(end, "end")
    if start and end and start > end:
        raise Invalid("the window starts after it ends")
    if start and start > datetime.now(LOCAL_TZ).date():
        # The pool never re-adds an event that already has a review_items
        # row (review_pool.py's _free()/_FREE_EVENTS_SQL), so a future
        # start would retire every open pass-1 item and none of them
        # would ever come back, even once the window is corrected (fix
        # round 1, finding 4).
        raise Invalid("the window can't start in the future")
    async with conn.transaction():
        async with conn.cursor() as cur:
            if start is None and end is None:
                await cur.execute("DELETE FROM review_settings WHERE key = 'window'")
                return {"start": None, "end": None, "retired": 0}
            value = {"start": local_day_start(start).isoformat() if start else None,
                     "end": local_day_start(end + timedelta(days=1)).isoformat() if end else None}
            await cur.execute("""
                INSERT INTO review_settings (key, value, updated_by, updated_at)
                VALUES ('window', %s, %s, now())
                ON CONFLICT (key) DO UPDATE
                   SET value = EXCLUDED.value, updated_by = EXCLUDED.updated_by,
                       updated_at = now()""", (Jsonb(value), admin_id))
        retired = await retire_outside_window(
            conn,
            datetime.fromisoformat(value["start"]) if value["start"]
            else datetime.min.replace(tzinfo=UTC),
            datetime.fromisoformat(value["end"]) if value["end"]
            else datetime.max.replace(tzinfo=UTC))
    return {**value, "retired": retired}


async def pool_status(conn, *, pool_obj) -> dict:
    items = await fetch_all(conn, """SELECT lane, pass, status, count(*) AS n FROM review_items
                                      GROUP BY lane, pass, status ORDER BY lane, pass, status""")
    unscored = (await fetch_one(conn, """SELECT count(*) AS n FROM review_items
                                          WHERE hints IS NULL
                                            AND status IN ('open', 'claimed')"""))["n"]
    last = (await fetch_one(conn, "SELECT max(hinted_at) AS t FROM review_items"))["t"]
    return {"items": [{"lane": r["lane"], "pass": r["pass"], "status": r["status"],
                       "count": r["n"]} for r in items],
            "unscored": unscored, "last_hinted_at": last.isoformat() if last else None,
            "last_run": pool_obj.last_run if pool_obj else None,
            "window": await get_window(conn)}

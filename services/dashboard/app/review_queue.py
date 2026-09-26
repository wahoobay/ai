"""Serving boxes to volunteers and recording their verdicts (spec §8).

The rules live here, as functions taking a connection, so they can be
tested without HTTP; routes_review.py only turns the exceptions below
into status codes. Invariants:

* A box is claimed with one UPDATE ... FOR UPDATE SKIP LOCKED, so two
  volunteers asking at the same moment never get the same box.
* A spot-check (pass 2) is never served to the person who gave the
  first verdict or to anyone else who answered the box, and its payload
  carries no model guess, no hints, no species-it-counts-toward and no
  earlier verdict. The server leaves them out; the page hides nothing.
* Verdicts are appended, never edited. Reopening a box adds a new one.
"""
from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from typing import Literal

from psycopg.rows import dict_row
from pydantic import BaseModel, Field

from .auth import effective_role
from .review_images import frame_size, zoom_region

LANES = ("check", "identify")

# A same-user resubmit on the no-item path (a double click or a retried
# request) within this many seconds returns their own last verdict on this
# event instead of inserting a duplicate row -- but only when it matches
# that verdict in every column of _VERDICT_IDENTITY_COLUMNS. A test checks
# that tuple and _VERDICT_NON_IDENTITY_COLUMNS against the table's real
# columns, so a new column cannot be left out of the comparison unnoticed.
DUPLICATE_WINDOW_S = 10

# Follow-up spec A2: a skip hides a box from that volunteer for this many
# days (REVIEW_SKIP_TTL_DAYS), then it can be served to them again.
SKIP_TTL_DAYS = 30

# Every verdict on an event takes this lock first, and so does the pool
# before it queues the event (follow-up C4). Namespaced and
# transaction-scoped; hashtextextended, not the two-int4 form, because
# event ids will pass 2^31 within a year.
EVENT_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtextextended('review-event:' || %s::text, 0))"

# Follow-up C3: a verdict on a retired item says why it was retired.
RETIRED_MESSAGES = {
    "frame_missing": "this box was retired: its frame is missing on disk",
    "outside_window": "this box was retired: it is outside the review window",
}


def not_skipped(alias: str) -> str:
    """SQL: volunteer %(uid)s has not skipped item `alias` within the last
    %(skip_days)s days (follow-up A2). An older skip no longer counts."""
    return f"""NOT EXISTS (SELECT 1 FROM review_skips s
                        WHERE s.item_id = {alias}.id AND s.user_id = %(uid)s
                          AND s.created_at > now() - make_interval(days => %(skip_days)s))"""


class ReviewError(Exception):
    status = 400

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class Invalid(ReviewError):
    status = 400


class Forbidden(ReviewError):
    status = 403


class NotFound(ReviewError):
    status = 404


class FrameMissing(NotFound):
    """The box's saved frame is missing from the database or from disk."""


class Conflict(ReviewError):
    status = 409


class VerdictIn(BaseModel):
    event_id: int
    review_item_id: int | None = None
    outcome: Literal["accept", "species", "unidentifiable", "no_animal"]
    label: str | None = Field(None, max_length=200)
    life_stage: str | None = Field(None, max_length=40)
    bbox: list[int] | None = None
    label_source: Literal["model_guess", "hint", "search"] | None = None
    not_sure: bool = False
    celebrity_id: str | None = Field(None, max_length=80)
    notes: str | None = Field(None, max_length=2000)


async def fetch_one(conn, sql: str, params=None) -> dict | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(sql, params)
        return await cur.fetchone()


async def fetch_all(conn, sql: str, params=None) -> list[dict]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(sql, params)
        return await cur.fetchall()


_EVENT_FRAME_SQL = """
    SELECT de.id AS event_id, de.ts, de.source_name, de.frame_id, de.bbox,
           de.best_species_id, de.best_accuracy, sf.id AS saved_frame_id, sf.image_path
      FROM detection_events de
      LEFT JOIN LATERAL (
          SELECT id, image_path FROM saved_frames
           WHERE frame_id = de.frame_id AND source_name = de.source_name AND ts = de.ts
           ORDER BY id LIMIT 1
      ) sf ON true
     WHERE de.id = %s
"""


async def event_with_frame(conn, event_id: int) -> dict | None:
    """The detection and the frame saved at its exact timestamp, if any."""
    return await fetch_one(conn, _EVENT_FRAME_SQL, (event_id,))


# ---- serving ------------------------------------------------------------

_CLAIM = ("status = 'claimed', claimed_by = %(uid)s, "
          "claimed_until = now() + make_interval(secs => %(claim_s)s)")
_PASS1_SQL = f"""
    UPDATE review_items SET {_CLAIM}
     WHERE id = (
        SELECT ri.id FROM review_items ri
         WHERE ri.status = 'open' AND ri.pass = 1 AND ri.lane = %(lane)s
           AND {not_skipped("ri")}
         ORDER BY COALESCE(array_position(%(order)s::text[], ri.route_species), 1000000),
                  random()
         LIMIT 1
         FOR UPDATE SKIP LOCKED)
    RETURNING *
"""
_SPOT_ELIGIBLE = f"""
    ri.status = 'open' AND ri.pass = 2
    AND ri.exclude_user_id IS DISTINCT FROM %(uid)s
    AND NOT EXISTS (SELECT 1 FROM detection_corrections c
                     WHERE c.event_id = ri.event_id AND c.user_id = %(uid)s)
    AND {not_skipped("ri")}
"""
_SPOT_SQL = f"""
    UPDATE review_items SET {_CLAIM}
     WHERE id = (
        SELECT ri.id FROM review_items ri
         WHERE {_SPOT_ELIGIBLE}
         ORDER BY ri.added_at
         LIMIT 1
         FOR UPDATE SKIP LOCKED)
    RETURNING *
"""
_STALE_SPOT_SQL = f"""
    SELECT 1 FROM review_items ri
     WHERE {_SPOT_ELIGIBLE} AND ri.added_at < now() - interval '48 hours'
     LIMIT 1
"""


async def next_item(conn, *, user_id: int, lane: str, species_order: list[str],
                    spot_serve_rate: float, claim_s: int, rng: random.Random,
                    skip_ttl_days: int = SKIP_TTL_DAYS) -> dict | None:
    if lane not in LANES:
        raise Invalid("unknown lane")
    params = {"uid": user_id, "lane": lane, "order": list(species_order), "claim_s": claim_s,
              "skip_days": skip_ttl_days}
    async with conn.transaction():
        async with conn.cursor() as cur:
            # claimed_by is deliberately kept, not nulled: a volunteer may
            # only answer a box that was served to them (spec Sec 7/15/16),
            # and this is what lets submit_verdict tell "my own claim
            # lapsed" apart from "nobody ever served me this box" once the
            # status here has already moved on to 'open'. A fresh claim
            # overwrites it regardless; release_item and skip_item still
            # clear it explicitly, because giving a box up really does mean
            # nobody served it to you any more.
            await cur.execute(
                """UPDATE review_items SET status = 'open', claimed_until = NULL
                    WHERE id IN (SELECT id FROM review_items
                                  WHERE status = 'claimed' AND claimed_until < now()
                                  FOR UPDATE SKIP LOCKED)""")
        # Handing back a box the volunteer already holds refreshes its
        # claim like a fresh one (final review Minor 5): a reload late in
        # the claim window would otherwise leave them only seconds.
        #
        # The outer WHERE repeats the subquery's own predicates (fix round
        # 1, finding 1): under READ COMMITTED, if this row changes while
        # the UPDATE is blocked on a concurrent writer's lock -- another
        # volunteer's next_item re-claiming a lapsed row, or a release or
        # verdict from a second tab -- Postgres re-checks the WHERE clause
        # against the row's newest version before applying the write. An
        # outer WHERE of only `id = ...` would pass that re-check
        # regardless (id never changes), refreshing and returning a claim
        # that is no longer this volunteer's live one.
        held = await fetch_one(conn, """
            UPDATE review_items
               SET claimed_until = now() + make_interval(secs => %(claim_s)s)
             WHERE id = (SELECT id FROM review_items
                          WHERE status = 'claimed' AND claimed_by = %(uid)s
                            AND (lane = %(lane)s OR pass = 2)
                          ORDER BY id LIMIT 1)
               AND status = 'claimed' AND claimed_by = %(uid)s
            RETURNING *""", params)
        if held:
            return held
        want_spot = (rng.random() < spot_serve_rate
                     or await fetch_one(conn, _STALE_SPOT_SQL, params) is not None)
        if want_spot:
            item = await fetch_one(conn, _SPOT_SQL, params)
            if item:
                return item
        item = await fetch_one(conn, _PASS1_SQL, params)
        if item is None and not want_spot:
            # The lane is empty: a waiting spot-check is still useful work.
            item = await fetch_one(conn, _SPOT_SQL, params)
        return item


async def retire_item(conn, item_id: int, reason: str) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            """UPDATE review_items SET status = 'retired', retired_reason = %s,
                      claimed_by = NULL, claimed_until = NULL
                WHERE id = %s""", (reason, item_id))


def safe_frame_size(image_path: str, message: str = "this box's frame is missing on disk"
                    ) -> tuple[int, int]:
    """frame_size(), but any failure to read or decode the file -- missing,
    zero-byte, truncated or corrupt -- becomes FrameMissing here rather
    than an OSError/ValueError reaching a caller as a raw 500. PIL's
    UnidentifiedImageError is an OSError subclass, so catching OSError
    covers a zero-byte or non-image file along with a plain missing one;
    ValueError covers a header PIL parses but cannot size sensibly.

    Public and reused (not copied) by review_frames.frame_record, so a
    corrupt or zero-byte frame is treated the same way -- 404, never a
    500 -- whichever route reads it first. `message` defaults to this
    module's own wording (a box's frame); frame_record passes its own,
    so whole-frame mode's 404s keep saying "this frame", not "this box's
    frame"."""
    try:
        width, height = frame_size(image_path)
    except (OSError, ValueError) as e:
        raise FrameMissing(message) from e
    if width <= 0 or height <= 0:
        raise FrameMissing(message)
    return width, height


async def build_payload(conn, item: dict, *, catalogue, counts, target: int,
                        starting_bbox=None) -> dict:
    ev = await event_with_frame(conn, item["event_id"])
    if ev is None or ev["saved_frame_id"] is None:
        raise FrameMissing("this box's frame was not saved")
    width, height = safe_frame_size(ev["image_path"])
    others = await fetch_all(conn, """
        SELECT id AS event_id, bbox FROM detection_events
         WHERE source_name = %s AND ts = %s AND frame_id = %s AND id <> %s
         ORDER BY id""", (ev["source_name"], ev["ts"], ev["frame_id"], ev["event_id"]))
    is_blind = item.get("pass") == 2
    payload = {
        "item": {"id": item.get("id"), "pass": item.get("pass", 1),
                 "lane": None if is_blind else item["lane"], "event_id": ev["event_id"]},
        "box": {"bbox": list(starting_bbox or ev["bbox"]), "model_bbox": list(ev["bbox"]),
                "source_name": ev["source_name"], "ts": ev["ts"].isoformat(),
                "saved_frame_id": ev["saved_frame_id"]},
        "frame": {"url": f"/api/review/frames/{ev['saved_frame_id']}/image.jpg",
                  "width": width, "height": height},
        "zoom": {"url": f"/api/review/boxes/{ev['event_id']}/zoom.jpg",
                 "region": list(zoom_region(ev["bbox"], width, height))},
        "other_boxes": [{"event_id": o["event_id"], "bbox": list(o["bbox"])} for o in others],
        "model": None, "hints": None, "counts_toward": None,
    }
    if is_blind:
        return payload        # a second opinion comes from the picture alone
    guess = item.get("predicted")
    if guess:
        payload["model"] = {"name": guess, "p": item.get("predicted_p"),
                            "common": catalogue.common_name(guess)}
    hints = [h for h in (item.get("hints") or []) if h.get("name") and h["name"] != guess]
    if hints:
        payload["hints"] = [{"name": h["name"], "p": h.get("p"),
                             "common": catalogue.common_name(h["name"])} for h in hints[:3]]
    route = item.get("route_species")
    if route:
        payload["counts_toward"] = {"name": route, "common": catalogue.common_name(route),
                                    "count": counts(route), "target": target}
    return payload


# ---- verdicts -----------------------------------------------------------

def validate_bbox(bbox, width: int, height: int) -> list[int]:
    """The bounds check for a reviewer-drawn box, shared with Task 8's
    added-box validation (`review_frames._validate_added`): a box is
    four integers, inside the frame, and at least 4px on a side.

    Type-robust: Task 8 feeds this raw client JSON. Only a list or tuple
    of exactly four is accepted -- not None, a bare scalar, a string (whose
    characters would otherwise iterate as if it were a list of four), or a
    dict (whose keys can look numeric enough for int() to accept them,
    e.g. {"10": 1, "20": 1, "300": 1, "400": 1}, silently taking the keys
    as coordinates). A non-numeric or non-finite member (1e400, Infinity,
    NaN) raises the same Invalid rather than a TypeError, ValueError or
    OverflowError reaching the caller as a 500."""
    if not isinstance(bbox, list | tuple) or len(bbox) != 4:
        raise Invalid("a box is [x1, y1, x2, y2]")
    try:
        x1, y1, x2, y2 = (int(v) for v in bbox)
    except (TypeError, ValueError, OverflowError):
        raise Invalid("a box is [x1, y1, x2, y2]") from None
    if not (0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height):
        raise Invalid("the box must lie inside the frame")
    if x2 - x1 < 4 or y2 - y1 < 4:
        raise Invalid("the box is too small")
    return [x1, y1, x2, y2]


def validate_label(body, *, catalogue, name: str | None) -> dict:
    """'Not sure' and named-individual (celebrity) checks, plus the
    confidence they imply, shared by _validate and
    review_frames._validate_added.

    Both callers' own outcome branching decides `name` first -- _validate
    from the model's guess for 'accept' or from body.label for 'species'
    (each outcome resolving body.label against the catalogue itself, with
    its own message, since 'accept' and 'species' fail that check
    differently); review_frames._validate_added the same way for its own
    'species' outcome. This is the tail every outcome runs afterwards,
    unconditionally: 'not sure' only applies once a species was chosen,
    and a named individual must be recorded for that species. VerdictIn
    and AddedBoxIn both carry `not_sure`/`celebrity_id` under these exact
    names. Messages and behaviour are unchanged from before this was
    extracted."""
    if body.not_sure and name is None:
        raise Invalid("'not sure' only applies to a species")
    celebrity_id = (body.celebrity_id or "").strip() or None
    if celebrity_id and celebrity_id not in catalogue.celebrity_ids(name):
        raise Invalid("that named individual is not recorded for this species")
    return {"confidence": "uncertain" if body.not_sure else "certain",
            "celebrity_id": celebrity_id}


def _validate(body: VerdictIn, *, catalogue, guess, item, blind: bool, width: int,
              height: int, adjudication: bool) -> dict:
    bbox = None
    if body.bbox is not None:
        if body.outcome == "no_animal":
            raise Invalid("a box can't be adjusted on a 'no animal' answer")
        bbox = validate_bbox(body.bbox, width, height)
    name = life_stage = label_source = None
    if body.outcome == "accept":
        if blind:
            raise Invalid("a second opinion has no model guess to accept")
        if not guess:
            raise Invalid("this box has no model guess to accept")
        if not catalogue.entry(guess, body.life_stage):
            if catalogue.has_name(guess):
                if not body.life_stage:
                    raise Invalid(f"choose a life stage for {guess}")
                raise Invalid(f"{guess} has no '{body.life_stage}' entry in the species list")
            raise Invalid("the model's guess is not in the species list; "
                          "pick the species instead")
        name, life_stage, label_source = guess, body.life_stage, "model_guess"
    elif body.outcome == "species":
        if not body.label or not catalogue.entry(body.label, body.life_stage):
            raise Invalid("pick a species from the list")
        name, life_stage = body.label, body.life_stage
        if not adjudication:
            label_source = body.label_source
            if label_source not in ("hint", "search"):
                raise Invalid("say whether the species came from a hint or the search")
            hint_names = {h.get("name") for h in ((item or {}).get("hints") or [])}
            if label_source == "hint" and (blind or name not in hint_names):
                raise Invalid("that species was not one of the hints")
    elif body.label or body.label_source:
        raise Invalid("no species goes with this answer")
    extra = validate_label(body, catalogue=catalogue, name=name)
    return {"name": name, "life_stage": life_stage, "label_source": label_source, "bbox": bbox,
            "confidence": extra["confidence"], "celebrity_id": extra["celebrity_id"],
            "notes": (body.notes or "").strip() or None}


def legacy_verdict(body, *, catalogue) -> VerdictIn:
    """The dashboard's older correction form (POST /api/corrections;
    main.CorrectionIn) as a review verdict (follow-up spec A1), so it is
    saved under every rule submit_verdict enforces. That form has no
    life-stage picker, so a species with one catalogue entry takes that
    entry's stage, and a species with several is sent to the review page.
    A box on "not a fish" is dropped, and "uncertain" becomes "not sure"
    only where a species was chosen: the review rules refuse either
    otherwise."""
    common = {"event_id": body.event_id, "celebrity_id": body.celebrity_id,
              "notes": body.notes}
    if body.not_a_fish:
        return VerdictIn(outcome="no_animal", **common)
    raw = body.corrected_name or body.corrected_species_id
    if not raw:
        raise Invalid("provide corrected_name, corrected_species_id, or set not_a_fish")
    name = catalogue.accepted(raw)
    if not catalogue.has_name(name):
        raise Invalid(f"{raw!r} is not in the species list")
    stages = catalogue.stages(name)
    if len(stages) > 1:
        raise Invalid(f"choose a life stage for {name} on the review page")
    fields = {**common, "life_stage": stages[0], "bbox": body.corrected_bbox,
              "not_sure": body.confidence == "uncertain"}
    if body.is_endorsement:
        return VerdictIn(outcome="accept", **fields)
    return VerdictIn(outcome="species", label=name, label_source="search", **fields)


# Every stored column that records the volunteer's answer, the box
# included: a same-user resubmit within DUPLICATE_WINDOW_S is a duplicate
# only when all of them match their last verdict on the event. Both the
# SELECT and the comparison in _duplicate_verdict read this one tuple.
_VERDICT_IDENTITY_COLUMNS = ("not_a_fish", "unidentifiable", "corrected_name", "life_stage",
                             "label_source", "confidence", "celebrity_id", "notes",
                             "corrected_bbox")

# Every other detection_corrections column, and why a verdict that differs
# only there is still the same answer. The two tuples must be disjoint and
# together name every column of the table: a test reads the real columns
# from information_schema, so a new column fails it until it is classified.
_VERDICT_NON_IDENTITY_COLUMNS = (
    "id",                    # the row's own key; every insert gets a new one
    "created_at",            # when, not what: the window itself is measured on it
    "event_id",              # lookup key: only rows on this same box are compared
    "user_id",               # lookup key: only this user's own rows are compared
    "is_adjudication",       # filter: adjudications are never compared or deduplicated
    "review_item_id",        # which queue item served the box: a link (NULL on the dedup path)
    "session_id",            # which login recorded it, not what was answered
    "reviewer",              # who, not what: the user's display name (a legacy column)
    "corrected_species_id",  # derived: submit_verdict stores corrected_name here too
    "is_endorsement",        # derived: corrected_name == the model's guess for this box
)


def _verdict_identity(v: dict, outcome: str) -> dict:
    """The column values submit_verdict is about to store for this
    request, keyed exactly like _VERDICT_IDENTITY_COLUMNS, so the two are
    compared column for column rather than field by field by hand."""
    return {"not_a_fish": outcome == "no_animal", "unidentifiable": outcome == "unidentifiable",
            "corrected_name": v["name"], "life_stage": v["life_stage"],
            "label_source": v["label_source"], "confidence": v["confidence"],
            "celebrity_id": v["celebrity_id"], "notes": v["notes"], "corrected_bbox": v["bbox"]}


def _stored_outcome(row: dict) -> str:
    """The API-level outcome ('accept'/'species'/'unidentifiable'/
    'no_animal') a stored detection_corrections row represents. accept and
    species store identically -- a name, not_a_fish=False,
    unidentifiable=False -- except for label_source ('model_guess' vs
    'hint'/'search'), so a dedup response describing the stored row needs
    this rather than _outcome_word's three-way collapse (used elsewhere,
    for a reviewer's own history and reopening, where accept and species
    are shown the same way either way)."""
    if row["not_a_fish"]:
        return "no_animal"
    if row["unidentifiable"]:
        return "unidentifiable"
    return "accept" if row["label_source"] == "model_guess" else "species"


async def _duplicate_verdict(conn, *, event_id: int, user_id: int, v: dict, outcome: str,
                             first_pass: bool = True) -> dict | None:
    """A same-user resubmit within DUPLICATE_WINDOW_S whose stored columns
    -- every one of _VERDICT_IDENTITY_COLUMNS, the box included -- all match
    their own last verdict on this event: return that existing verdict's
    response, read from the stored row throughout, instead of inserting a
    second row. A double click or a retried request on the no-item path
    must not insert twice, double-count progress, or silently drop a
    change to any stored field -- an accept followed within the window by
    the same species chosen from search is a *different* verdict (a
    different label_source), not a duplicate of it. None means: insert
    normally, this is not a duplicate.

    first_pass is what the caller's verdict would have been: False for a
    re-answer linked to the caller's own spot-check."""
    cols = ", ".join(_VERDICT_IDENTITY_COLUMNS)
    recent = await fetch_one(conn, f"""
        SELECT id, created_at, {cols}
          FROM detection_corrections
         WHERE event_id = %s AND user_id = %s AND NOT is_adjudication
         ORDER BY created_at DESC, id DESC LIMIT 1""", (event_id, user_id))
    if recent is None or recent["created_at"] < datetime.now(UTC) - timedelta(
            seconds=DUPLICATE_WINDOW_S):
        return None
    # The stored box compares as a plain list, or None when there is none.
    stored = {**recent, "corrected_bbox": (list(recent["corrected_bbox"])
                                           if recent["corrected_bbox"] else None)}
    current = _verdict_identity(v, outcome)
    if any(stored[col] != current[col] for col in _VERDICT_IDENTITY_COLUMNS):
        return None
    spot = await fetch_one(conn, """SELECT id FROM review_items
                                     WHERE event_id = %s AND pass = 2
                                        AND first_correction_id = %s""",
                           (event_id, recent["id"]))
    # Every field below is read from the stored row, not the (now
    # known-identical) incoming one.
    return {"id": recent["id"], "created_at": recent["created_at"].isoformat(),
            "outcome": _stored_outcome(recent), "label": recent["corrected_name"],
            "first_pass": first_pass,
            "counts": recent["corrected_name"] is not None
            and recent["confidence"] != "uncertain",
            "spot_check_scheduled": spot is not None, "duplicate": True}


async def submit_verdict(conn, *, user, body: VerdictIn, catalogue, spot_rate: float,
                         rng: random.Random, adjudication: bool = False) -> dict:
    role = effective_role(user.status, user.role)
    async with conn.transaction():
        # Namespaced, transaction-scoped: serialises every verdict on this
        # event -- in particular the no-item path below, whose guards are
        # plain SELECTs that would otherwise race between two volunteers,
        # or between a double-submitted request and itself. Released
        # automatically at commit or rollback.
        async with conn.cursor() as cur:
            await cur.execute(EVENT_LOCK_SQL, (body.event_id,))

        ev = await event_with_frame(conn, body.event_id)
        if ev is None:
            raise NotFound("no such detection")
        if ev["saved_frame_id"] is None:
            raise Invalid("this detection's frame was not saved")
        width, height = safe_frame_size(ev["image_path"])

        item = None
        if body.review_item_id is not None and not adjudication:
            item = await fetch_one(conn, "SELECT * FROM review_items WHERE id = %s FOR UPDATE",
                                   (body.review_item_id,))
            if item is None or item["event_id"] != body.event_id:
                raise NotFound("no such review item")
            if item["status"] == "retired":
                raise Conflict(RETIRED_MESSAGES.get(item["retired_reason"],
                                                    "this box was retired from the queue"))
            if item["status"] == "done":
                raise Conflict("this box was already answered")
            # A volunteer may only answer a box that was served to them
            # (spec Sec 7/15/16): claimed_by = them, and status is either
            # 'claimed' (their claim, live or lapsed but not yet swept) or
            # 'open' (their claim lapsed and the sweep already ran, but the
            # sweep keeps claimed_by, and nobody has re-claimed it since).
            # Someone ELSE's live claim is a 409; every other case -- never
            # claimed, released, skipped, or someone else's lapsed claim --
            # is a 403, not a 409, because there is no volunteer actively
            # answering it right now to be "in conflict" with.
            live_claim_by_other = (item["status"] == "claimed"
                                   and item["claimed_by"] != user.id
                                   and item["claimed_until"] > datetime.now(UTC))
            if live_claim_by_other:
                raise Conflict("another volunteer is answering this box")
            served_to_me = item["claimed_by"] == user.id and item["status"] in ("claimed", "open")
            if not served_to_me:
                raise Forbidden("this box was not served to you")
            # Pass-2 eligibility, defence in depth: even a genuinely held
            # claim does not excuse the excluded first reviewer or anyone
            # who has already answered the box some other way.
            if item["pass"] == 2:
                if item["exclude_user_id"] == user.id:
                    raise Forbidden("you gave the first verdict on this box")
                if await fetch_one(conn, """SELECT 1 FROM detection_corrections
                                             WHERE event_id = %s AND user_id = %s
                                             LIMIT 1""", (body.event_id, user.id)):
                    raise Forbidden("you already answered this box")
        elif not adjudication:
            # No queue item: whole-frame mode, or changing your own answer.
            # The advisory lock above makes this check-then-insert atomic.
            if role != "admin" and await fetch_one(conn, """
                    SELECT 1 FROM detection_corrections
                     WHERE event_id = %s AND user_id IS NOT NULL AND user_id <> %s
                     LIMIT 1""", (body.event_id, user.id)):
                raise Forbidden("another volunteer already answered this box")
            if await fetch_one(conn, """
                    SELECT 1 FROM review_items
                     WHERE event_id = %s AND status = 'claimed' AND claimed_by <> %s
                       AND claimed_until > now()
                     LIMIT 1""", (body.event_id, user.id)):
                raise Conflict("another volunteer is answering this box")
            # Follow-up C2 (phase-1 Minor 4): re-answering a box you answered
            # as a spot-check -- only admins get here, by reopening it --
            # stays a second opinion. Linked to your own pass-2 item, so the
            # resolved pair is still the first reviewer against the second,
            # the answer is blind, and nothing new is scheduled.
            item = await fetch_one(conn, """
                SELECT ri.* FROM detection_corrections c
                  JOIN review_items ri ON ri.id = c.review_item_id
                 WHERE c.event_id = %s AND c.user_id = %s AND ri.pass = 2
                 ORDER BY c.created_at DESC, c.id DESC LIMIT 1""", (body.event_id, user.id))
            # Fix round 1 (ruling T2b): a pass-2 item open or claimed on this
            # box is a live second opinion. Someone with no answer of their
            # own is an outsider to it, not the first reviewer changing
            # their mind (who does have an answer here) or the spot-check
            # reviewer re-answering their own pass-2 item (caught above, by
            # `item` being set) -- refuse them rather than letting them
            # jump the queue through the whole-frame path.
            if item is None:
                have_answer = await fetch_one(conn, """
                    SELECT 1 FROM detection_corrections
                     WHERE event_id = %s AND user_id = %s LIMIT 1""",
                    (body.event_id, user.id))
                pending_spot = await fetch_one(conn, """
                    SELECT 1 FROM review_items
                     WHERE event_id = %s AND pass = 2 AND status IN ('open', 'claimed')
                     LIMIT 1""", (body.event_id,))
                if not have_answer and pending_spot:
                    raise Conflict("a second opinion is pending on this box")

        blind = item is not None and item["pass"] == 2
        guess = (item or {}).get("predicted") or catalogue.accepted(ev["best_species_id"])
        hint_item = item
        if item is None and not adjudication:
            # Whole-frame verdicts carry no item id (spec: item id absent
            # for whole-frame boxes), so a hint chip chosen there has no
            # `item` to check against -- look up the event's own latest
            # pass-1 item (any status) for its hints alone, without
            # disturbing `item` itself, which the code below still uses
            # to decide the review_items UPDATE and the stored
            # review_item_id. A blind (pass-2) answer is unaffected:
            # `blind` already forces label_source "hint" to be refused
            # below regardless of any hint list.
            hint_item = await fetch_one(conn, """
                SELECT hints FROM review_items
                 WHERE event_id = %s AND pass = 1
                 ORDER BY added_at DESC, id DESC LIMIT 1""", (body.event_id,))
        v = _validate(body, catalogue=catalogue, guess=guess, item=hint_item, blind=blind,
                      width=width, height=height, adjudication=adjudication)

        if body.review_item_id is None and not adjudication:
            dup = await _duplicate_verdict(conn, event_id=body.event_id, user_id=user.id, v=v,
                                           outcome=body.outcome, first_pass=item is None)
            if dup is not None:
                return dup

        row = await fetch_one(conn, """
            INSERT INTO detection_corrections
                (event_id, corrected_name, corrected_species_id, not_a_fish, unidentifiable,
                 is_endorsement, corrected_bbox, confidence, reviewer, notes, celebrity_id,
                 user_id, session_id, review_item_id, label_source, life_stage,
                 is_adjudication)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id, created_at""",
            (body.event_id, v["name"], v["name"], body.outcome == "no_animal",
             body.outcome == "unidentifiable", v["name"] is not None and v["name"] == guess,
             v["bbox"], v["confidence"], user.display_name, v["notes"], v["celebrity_id"],
             user.id, user.session_id, item["id"] if item else None, v["label_source"],
             v["life_stage"], adjudication))

        if item is not None and item["pass"] == 2:
            where, params = "id = %s", (item["id"],)
        else:
            where, params = "event_id = %s AND pass = 1", (body.event_id,)
        async with conn.cursor() as cur:
            await cur.execute(
                f"""UPDATE review_items SET status = 'done', done_at = now(),
                           claimed_by = NULL, claimed_until = NULL
                     WHERE {where} AND status IN ('open', 'claimed')""", params)

        first_pass = not adjudication and (item is None or item["pass"] == 1)
        spot = None
        if first_pass and rng.random() < spot_rate:
            lane = (item or {}).get("lane") or ("check" if ev["best_species_id"] else "identify")
            spot = await fetch_one(conn, """
                INSERT INTO review_items (event_id, pass, lane, saved_frame_id, source_name, ts,
                                          bbox, predicted, predicted_p, exclude_user_id,
                                          first_correction_id)
                VALUES (%s, 2, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (event_id, pass) DO NOTHING
                RETURNING id""",
                (body.event_id, lane, ev["saved_frame_id"], ev["source_name"], ev["ts"],
                 list(ev["bbox"]), guess, ev["best_accuracy"], user.id, row["id"]))
    return {"id": row["id"], "created_at": row["created_at"].isoformat(),
            "outcome": body.outcome, "label": v["name"], "first_pass": first_pass,
            "counts": v["name"] is not None and v["confidence"] != "uncertain",
            "spot_check_scheduled": spot is not None, "duplicate": False}


async def skip_item(conn, *, user_id: int, item_id: int) -> None:
    async with conn.transaction():
        item = await fetch_one(conn, """SELECT id, status, claimed_by FROM review_items
                                         WHERE id = %s FOR UPDATE""", (item_id,))
        if item is None:
            raise NotFound("no such review item")
        async with conn.cursor() as cur:
            # Skipping again restarts the clock (follow-up A2).
            await cur.execute("""INSERT INTO review_skips (item_id, user_id) VALUES (%s, %s)
                                 ON CONFLICT (item_id, user_id)
                                 DO UPDATE SET created_at = now()""", (item_id, user_id))
            # Clear the claimant whenever it's this user's, 'claimed' or
            # already swept to 'open': giving a box up must mean it was
            # never served to you again, not just while the claim looked
            # live (spec Sec 7/15/16; the sweep deliberately keeps
            # claimed_by on a lapse, so this is the one place besides
            # release that has to un-keep it).
            if item["status"] in ("claimed", "open") and item["claimed_by"] == user_id:
                await cur.execute(
                    """UPDATE review_items SET status = 'open', claimed_by = NULL,
                              claimed_until = NULL WHERE id = %s""", (item_id,))


async def release_item(conn, *, user_id: int, item_id: int) -> None:
    async with conn.transaction(), conn.cursor() as cur:
        await cur.execute(
            """UPDATE review_items SET status = 'open', claimed_by = NULL, claimed_until = NULL
                WHERE id = %s AND status IN ('claimed', 'open') AND claimed_by = %s""",
            (item_id, user_id))


def _outcome_word(row: dict) -> str:
    if row["not_a_fish"]:
        return "no_animal"
    return "unidentifiable" if row["unidentifiable"] else "species"


async def recent_verdicts(conn, *, user_id: int, session_id: int | None, catalogue,
                          limit: int = 20) -> dict:
    count = 0
    if session_id is not None:
        count = (await fetch_one(conn, """SELECT count(*) AS n FROM detection_corrections
                                           WHERE session_id = %s""", (session_id,)))["n"]
    rows = await fetch_all(conn, """
        SELECT id, event_id, corrected_name, not_a_fish, unidentifiable, life_stage, created_at
          FROM detection_corrections
         WHERE user_id = %s AND session_id IS NOT DISTINCT FROM %s AND NOT is_adjudication
         ORDER BY created_at DESC, id DESC
         LIMIT %s""", (user_id, session_id, limit))
    return {"session_count": count, "recent": [
        {"id": r["id"], "event_id": r["event_id"], "outcome": _outcome_word(r),
         "label": r["corrected_name"], "common": catalogue.common_name(r["corrected_name"]),
         "life_stage": r["life_stage"], "at": r["created_at"].isoformat(),
         "zoom_url": f"/api/review/boxes/{r['event_id']}/zoom.jpg"} for r in rows]}


async def reopen_payload(conn, *, user, event_id: int, catalogue, counts, target: int) -> dict:
    role = effective_role(user.status, user.role)
    mine = await fetch_one(conn, """
        SELECT corrected_name, not_a_fish, unidentifiable, life_stage, corrected_bbox,
               confidence, celebrity_id
          FROM detection_corrections
         WHERE event_id = %s AND user_id = %s
         ORDER BY created_at DESC, id DESC LIMIT 1""", (event_id, user.id))
    if mine is None and role != "admin":
        raise Forbidden("you can only reopen boxes you answered")
    ev = await event_with_frame(conn, event_id)
    if ev is None:
        raise NotFound("no such detection")
    item = await fetch_one(conn, "SELECT * FROM review_items WHERE event_id = %s AND pass = 1",
                           (event_id,))
    guess = catalogue.accepted(ev["best_species_id"])
    base = item or {"lane": "check" if guess else "identify", "predicted": guess,
                    "predicted_p": ev["best_accuracy"], "hints": None, "route_species": guess}
    # Follow-up C1 (phase-1 triage 155) and C2: the picture alone -- no
    # guess, hints or target -- while a spot-check on the box is waiting or
    # being answered and the caller has no answer on it (an admin typing the
    # URL), and when the caller has answered their own spot-check.
    pending_spot = await fetch_one(conn, """
        SELECT 1 FROM review_items
         WHERE event_id = %s AND pass = 2 AND status IN ('open', 'claimed')""", (event_id,))
    # Ruling T2a: mirror submit_verdict's own-spot lookup exactly -- ANY of
    # the caller's answers on this event linked to a pass-2 item, not only
    # their latest row. An admin's later adjudication (or an older,
    # unlinked re-answer) must not un-blind a spot-check they answered;
    # `mine` still shows their latest row, for display, regardless.
    own_spot_answer = await fetch_one(conn, """
        SELECT 1 FROM detection_corrections c
          JOIN review_items ri ON ri.id = c.review_item_id
         WHERE c.event_id = %s AND c.user_id = %s AND ri.pass = 2
         LIMIT 1""", (event_id, user.id))
    blind = (mine is None and pending_spot is not None) or own_spot_answer is not None
    pseudo = {**base, "id": None, "pass": 2 if blind else 1, "event_id": event_id}
    payload = await build_payload(conn, pseudo, catalogue=catalogue, counts=counts,
                                  target=target,
                                  starting_bbox=(mine or {}).get("corrected_bbox"))
    if mine:
        payload["mine"] = {"outcome": _outcome_word(mine), "label": mine["corrected_name"],
                           "life_stage": mine["life_stage"],
                           "not_sure": mine["confidence"] == "uncertain",
                           "celebrity_id": mine["celebrity_id"],
                           "bbox": list(mine["corrected_bbox"]) if mine["corrected_bbox"]
                           else None}
    # A resubmit from here goes through submit_verdict's no-item path, whose
    # guards this mirrors, so the page learns up front whether the box is
    # still editable instead of only finding out from a 403/409 on submit.
    editable, reason = True, None
    if role != "admin" and await fetch_one(conn, """
            SELECT 1 FROM detection_corrections
             WHERE event_id = %s AND user_id IS NOT NULL AND user_id <> %s
             LIMIT 1""", (event_id, user.id)):
        editable, reason = False, "another volunteer already answered this box"
    elif await fetch_one(conn, """
            SELECT 1 FROM review_items
             WHERE event_id = %s AND status = 'claimed' AND claimed_by <> %s
               AND claimed_until > now()
             LIMIT 1""", (event_id, user.id)):
        editable, reason = False, "another volunteer is answering this box"
    elif mine is None and pending_spot is not None:
        # Ruling T2b: matches the new submit_verdict guard -- an outsider
        # (no answer of their own) can't jump in while a second opinion is
        # pending. An earlier refusal reason above still wins.
        editable, reason = False, "a second opinion is pending on this box"
    payload["editable"], payload["reason"] = editable, reason
    return payload

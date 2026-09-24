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

# A same-user resubmit that names the same outcome, label, life stage and
# box as their own last verdict on this event, within this many seconds,
# returns that existing verdict instead of inserting a duplicate row (a
# double click or a retried request on the no-item path).
DUPLICATE_WINDOW_S = 10


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
           AND NOT EXISTS (SELECT 1 FROM review_skips s
                            WHERE s.item_id = ri.id AND s.user_id = %(uid)s)
         ORDER BY COALESCE(array_position(%(order)s::text[], ri.route_species), 1000000),
                  random()
         LIMIT 1
         FOR UPDATE SKIP LOCKED)
    RETURNING *
"""
_SPOT_ELIGIBLE = """
    ri.status = 'open' AND ri.pass = 2
    AND ri.exclude_user_id IS DISTINCT FROM %(uid)s
    AND NOT EXISTS (SELECT 1 FROM detection_corrections c
                     WHERE c.event_id = ri.event_id AND c.user_id = %(uid)s)
    AND NOT EXISTS (SELECT 1 FROM review_skips s
                     WHERE s.item_id = ri.id AND s.user_id = %(uid)s)
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
                    spot_serve_rate: float, claim_s: int, rng: random.Random) -> dict | None:
    if lane not in LANES:
        raise Invalid("unknown lane")
    params = {"uid": user_id, "lane": lane, "order": list(species_order), "claim_s": claim_s}
    async with conn.transaction():
        async with conn.cursor() as cur:
            await cur.execute(
                """UPDATE review_items SET status = 'open', claimed_by = NULL,
                          claimed_until = NULL
                    WHERE id IN (SELECT id FROM review_items
                                  WHERE status = 'claimed' AND claimed_until < now()
                                  FOR UPDATE SKIP LOCKED)""")
        held = await fetch_one(conn, """
            SELECT * FROM review_items
             WHERE status = 'claimed' AND claimed_by = %(uid)s
               AND (lane = %(lane)s OR pass = 2)
             ORDER BY id LIMIT 1""", params)
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


def _safe_frame_size(image_path: str) -> tuple[int, int]:
    """frame_size(), but any failure to read or decode the file -- missing,
    zero-byte, truncated or corrupt -- becomes FrameMissing here rather
    than an OSError/ValueError reaching a caller as a raw 500. PIL's
    UnidentifiedImageError is an OSError subclass, so catching OSError
    covers a zero-byte or non-image file along with a plain missing one;
    ValueError covers a header PIL parses but cannot size sensibly."""
    try:
        width, height = frame_size(image_path)
    except (OSError, ValueError) as e:
        raise FrameMissing("this box's frame is missing on disk") from e
    if width <= 0 or height <= 0:
        raise FrameMissing("this box's frame is missing on disk")
    return width, height


async def build_payload(conn, item: dict, *, catalogue, counts, target: int,
                        starting_bbox=None) -> dict:
    ev = await event_with_frame(conn, item["event_id"])
    if ev is None or ev["saved_frame_id"] is None:
        raise FrameMissing("this box's frame was not saved")
    width, height = _safe_frame_size(ev["image_path"])
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

    Type-robust: Task 8 feeds this raw client JSON. None, a non-sequence,
    a bare string (whose characters would otherwise iterate as if it were
    a list of four), the wrong length, or a non-numeric member all raise
    the same Invalid rather than a TypeError or ValueError reaching the
    caller as a 500."""
    try:
        wrong_length = isinstance(bbox, str | bytes) or len(bbox) != 4
    except TypeError:
        wrong_length = True
    if wrong_length:
        raise Invalid("a box is [x1, y1, x2, y2]")
    try:
        x1, y1, x2, y2 = (int(v) for v in bbox)
    except (TypeError, ValueError):
        raise Invalid("a box is [x1, y1, x2, y2]") from None
    if not (0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height):
        raise Invalid("the box must lie inside the frame")
    if x2 - x1 < 4 or y2 - y1 < 4:
        raise Invalid("the box is too small")
    return [x1, y1, x2, y2]


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
    if body.not_sure and name is None:
        raise Invalid("'not sure' only applies to a species")
    celebrity_id = (body.celebrity_id or "").strip() or None
    if celebrity_id and celebrity_id not in catalogue.celebrity_ids(name):
        raise Invalid("that named individual is not recorded for this species")
    return {"name": name, "life_stage": life_stage, "label_source": label_source, "bbox": bbox,
            "confidence": "uncertain" if body.not_sure else "certain",
            "celebrity_id": celebrity_id, "notes": (body.notes or "").strip() or None}


async def _duplicate_verdict(conn, *, event_id: int, user_id: int, v: dict, outcome: str
                             ) -> dict | None:
    """A same-user resubmit within DUPLICATE_WINDOW_S naming the same
    outcome, label, life stage and box as their own last verdict on this
    event: return that existing verdict's response instead of inserting a
    second row. A double click or a retried request on the no-item path
    must not insert twice or double-count progress. None means: insert
    normally, this is not a duplicate."""
    recent = await fetch_one(conn, """
        SELECT id, created_at, corrected_name, not_a_fish, unidentifiable, life_stage,
               corrected_bbox
          FROM detection_corrections
         WHERE event_id = %s AND user_id = %s AND NOT is_adjudication
         ORDER BY created_at DESC, id DESC LIMIT 1""", (event_id, user_id))
    if recent is None or recent["created_at"] < datetime.now(UTC) - timedelta(
            seconds=DUPLICATE_WINDOW_S):
        return None
    same_bbox = (list(recent["corrected_bbox"]) if recent["corrected_bbox"] else None) == v["bbox"]
    if not (recent["not_a_fish"] == (outcome == "no_animal")
            and recent["unidentifiable"] == (outcome == "unidentifiable")
            and recent["corrected_name"] == v["name"]
            and recent["life_stage"] == v["life_stage"]
            and same_bbox):
        return None
    spot = await fetch_one(conn, """SELECT id FROM review_items
                                     WHERE event_id = %s AND pass = 2
                                        AND first_correction_id = %s""",
                           (event_id, recent["id"]))
    return {"id": recent["id"], "created_at": recent["created_at"].isoformat(),
            "outcome": outcome, "label": v["name"], "first_pass": True,
            "counts": v["name"] is not None and v["confidence"] != "uncertain",
            "spot_check_scheduled": spot is not None, "duplicate": True}


async def submit_verdict(conn, *, user, body: VerdictIn, catalogue, spot_rate: float,
                         rng: random.Random, adjudication: bool = False) -> dict:
    role = effective_role(user.status, user.role)
    async with conn.transaction():
        # Namespaced, transaction-scoped: serialises every verdict on this
        # event -- in particular the no-item path below, whose guards are
        # plain SELECTs that would otherwise race between two volunteers,
        # or between a double-submitted request and itself. Released
        # automatically at commit or rollback. hashtextextended, not the
        # two-int4 form, because event ids will pass 2^31 within a year.
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended('review-event:' || %s::text, 0))",
                (body.event_id,))

        ev = await event_with_frame(conn, body.event_id)
        if ev is None:
            raise NotFound("no such detection")
        if ev["saved_frame_id"] is None:
            raise Invalid("this detection's frame was not saved")
        width, height = _safe_frame_size(ev["image_path"])

        item = None
        if body.review_item_id is not None and not adjudication:
            item = await fetch_one(conn, "SELECT * FROM review_items WHERE id = %s FOR UPDATE",
                                   (body.review_item_id,))
            if item is None or item["event_id"] != body.event_id:
                raise NotFound("no such review item")
            if item["status"] in ("done", "retired"):
                raise Conflict("this box was already answered")
            live_claim = (item["status"] == "claimed"
                         and item["claimed_until"] > datetime.now(UTC))
            if live_claim and item["claimed_by"] != user.id:
                raise Conflict("another volunteer is answering this box")
            if not (live_claim and item["claimed_by"] == user.id):
                # No live claim of this user's own to trust -- never
                # claimed, or the claim lapsed: re-check eligibility
                # atomically before accepting, rather than trusting that
                # serving enforced it. Pass 1 has no extra eligibility rule
                # beyond "nobody else holds it", already checked above.
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

        blind = item is not None and item["pass"] == 2
        guess = (item or {}).get("predicted") or catalogue.accepted(ev["best_species_id"])
        v = _validate(body, catalogue=catalogue, guess=guess, item=item, blind=blind,
                      width=width, height=height, adjudication=adjudication)

        if item is None and not adjudication:
            dup = await _duplicate_verdict(conn, event_id=body.event_id, user_id=user.id, v=v,
                                           outcome=body.outcome)
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
            await cur.execute("""INSERT INTO review_skips (item_id, user_id) VALUES (%s, %s)
                                 ON CONFLICT DO NOTHING""", (item_id, user_id))
            if item["status"] == "claimed" and item["claimed_by"] == user_id:
                await cur.execute(
                    """UPDATE review_items SET status = 'open', claimed_by = NULL,
                              claimed_until = NULL WHERE id = %s""", (item_id,))


async def release_item(conn, *, user_id: int, item_id: int) -> None:
    async with conn.transaction(), conn.cursor() as cur:
        await cur.execute(
            """UPDATE review_items SET status = 'open', claimed_by = NULL, claimed_until = NULL
                WHERE id = %s AND status = 'claimed' AND claimed_by = %s""", (item_id, user_id))


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
    pseudo = {**base, "id": None, "pass": 1, "event_id": event_id}
    payload = await build_payload(conn, pseudo, catalogue=catalogue, counts=counts,
                                  target=target,
                                  starting_bbox=(mine or {}).get("corrected_bbox"))
    if mine:
        payload["mine"] = {"outcome": _outcome_word(mine), "label": mine["corrected_name"],
                           "life_stage": mine["life_stage"],
                           "not_sure": mine["confidence"] == "uncertain",
                           "celebrity_id": mine["celebrity_id"]}
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
    payload["editable"], payload["reason"] = editable, reason
    return payload

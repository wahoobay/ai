"""Whole-frame mode (spec §8).

A volunteer answers every box the model drew in one frame, draws boxes
around animals it missed, and marks the frame complete, which makes it
usable as detector training data.

* Opening the mode claims the frame's open pool items for the
  volunteer; boxes someone else holds are shown locked.
* A box with a pending spot-check shows as answered with the answer
  hidden (unless you gave it), so viewing the frame cannot contaminate
  the second opinion.
* Complete means every model box has a first-pass verdict from anyone.
  A drawn box always has an answer, because it cannot be saved without.
* Drawn boxes can be changed or removed by whoever drew them until the
  frame is complete; administrators can change them at any time.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field

from .auth import effective_role
from .review_images import zoom_region
from .review_labels import resolve_all
from .review_queue import (
    Conflict,
    Forbidden,
    Invalid,
    NotFound,
    fetch_all,
    fetch_one,
    safe_frame_size,
    validate_bbox,
    validate_label,
)


class AddedBoxIn(BaseModel):
    bbox: list[int]
    outcome: Literal["species", "unidentifiable"]
    label: str | None = Field(None, max_length=200)
    life_stage: str | None = Field(None, max_length=40)
    not_sure: bool = False
    celebrity_id: str | None = Field(None, max_length=80)


def _is_admin(user) -> bool:
    return effective_role(user.status, user.role) == "admin"


async def frame_record(conn, saved_frame_id: int) -> dict:
    row = await fetch_one(conn, """SELECT id, ts, source_name, frame_id, image_path
                                     FROM saved_frames WHERE id = %s""", (saved_frame_id,))
    if row is None:
        raise NotFound("no such frame")
    row["width"], row["height"] = safe_frame_size(row["image_path"],
                                                   message="this frame is missing on disk")
    return row


async def open_frame(conn, *, saved_frame_id: int, user_id: int, claim_s: int) -> None:
    async with conn.transaction(), conn.cursor() as cur:
        await cur.execute("""
            UPDATE review_items
               SET status = 'claimed', claimed_by = %(uid)s,
                   claimed_until = now() + make_interval(secs => %(claim_s)s)
             WHERE saved_frame_id = %(sf)s AND pass = 1
               AND (status = 'open' OR (status = 'claimed' AND claimed_until < now()))
               AND NOT EXISTS (SELECT 1 FROM review_skips s
                                WHERE s.item_id = review_items.id AND s.user_id = %(uid)s)""",
                          {"uid": user_id, "claim_s": claim_s, "sf": saved_frame_id})


async def frame_state(conn, *, saved_frame_id: int, user, catalogue) -> dict:
    fr = await frame_record(conn, saved_frame_id)
    events = await fetch_all(conn, """
        SELECT id, bbox, best_species_id, best_accuracy FROM detection_events
         WHERE source_name = %s AND ts = %s AND frame_id = %s ORDER BY id""",
                             (fr["source_name"], fr["ts"], fr["frame_id"]))
    ids = [e["id"] for e in events]
    resolved = await resolve_all(conn, ids) if ids else {}
    items = await fetch_all(conn, """
        SELECT id, event_id, pass, status, claimed_by, claimed_until, hints
          FROM review_items WHERE event_id = ANY(%s)""", (ids,)) if ids else []
    mine = {r["event_id"] for r in await fetch_all(conn, """
        SELECT DISTINCT event_id FROM detection_corrections
         WHERE event_id = ANY(%s) AND user_id = %s""", (ids, user.id))} if ids else set()
    now = datetime.now(UTC)
    boxes = []
    for n, e in enumerate(events, 1):
        its = [i for i in items if i["event_id"] == e["id"]]
        pending_check = any(i["pass"] == 2 and i["status"] in ("open", "claimed") for i in its)
        held_by_other = any(i["pass"] == 1 and i["status"] == "claimed"
                            and i["claimed_by"] != user.id and i["claimed_until"] > now
                            for i in its)
        my_item = next((i for i in its if i["pass"] == 1 and i["status"] == "claimed"
                        and i["claimed_by"] == user.id), None)
        r = resolved.get(e["id"])
        if r is None:
            state = "locked" if held_by_other else "needs_answer"
        elif pending_check and e["id"] not in mine:
            # Spec Sec7 makes no admin exception: admins can be served a
            # spot-check themselves, so the earlier answer stays hidden
            # from everyone but whoever gave it, admins included, while
            # the check is still open or claimed.
            state = "answered_hidden"
        else:
            state = "answered"
        box = {"n": n, "event_id": e["id"], "bbox": list(e["bbox"]), "state": state,
               "mine": e["id"] in mine, "review_item_id": my_item["id"] if my_item else None,
               "zoom": {"url": f"/api/review/boxes/{e['id']}/zoom.jpg",
                        "region": list(zoom_region(e["bbox"], fr["width"], fr["height"]))},
               "outcome": None, "label": None, "common": None, "status": None,
               "model": None, "hints": None}
        if state == "answered":
            box.update(outcome=r.outcome[0], label=r.species,
                       common=catalogue.common_name(r.species), status=r.status)
        elif state == "needs_answer":
            guess = catalogue.accepted(e["best_species_id"])
            if guess:
                box["model"] = {"name": guess, "p": e["best_accuracy"],
                                "common": catalogue.common_name(guess)}
            if my_item and my_item["hints"]:
                box["hints"] = [{"name": h["name"], "p": h.get("p"),
                                 "common": catalogue.common_name(h["name"])}
                                for h in my_item["hints"] if h.get("name") != guess][:3]
        boxes.append(box)
    added = await fetch_all(conn, """
        SELECT a.id, a.bbox, a.outcome, a.label, a.life_stage, a.confidence, a.celebrity_id,
               a.user_id, u.display_name
          FROM review_added_boxes a JOIN users u ON u.id = a.user_id
         WHERE a.saved_frame_id = %s AND a.removed_at IS NULL
         ORDER BY a.id""", (saved_frame_id,))
    done = await fetch_one(conn, """
        SELECT f.completed_at, u.display_name FROM review_frames f
          JOIN users u ON u.id = f.completed_by
         WHERE f.saved_frame_id = %s""", (saved_frame_id,))
    return {
        "saved_frame_id": saved_frame_id, "source_name": fr["source_name"],
        "ts": fr["ts"].isoformat(),
        "frame": {"url": f"/api/review/frames/{saved_frame_id}/image.jpg",
                  "width": fr["width"], "height": fr["height"]},
        "boxes": boxes,
        "added": [{"id": a["id"], "bbox": list(a["bbox"]), "outcome": a["outcome"],
                   "label": a["label"], "common": catalogue.common_name(a["label"]),
                   "life_stage": a["life_stage"], "not_sure": a["confidence"] == "uncertain",
                   "celebrity_id": a["celebrity_id"], "mine": a["user_id"] == user.id,
                   "by": a["display_name"], "zoom_url": f"/api/review/added/{a['id']}/zoom.jpg"}
                  for a in added],
        "complete": ({"at": done["completed_at"].isoformat(), "by": done["display_name"]}
                     if done else None),
        "blocking": [b["n"] for b in boxes if b["state"] not in ("answered", "answered_hidden")],
    }


def _validate_added(body: AddedBoxIn, *, catalogue, width: int, height: int) -> dict:
    bbox = validate_bbox(body.bbox, width, height)
    label = life_stage = None
    if body.outcome == "species":
        if not body.label or not catalogue.entry(body.label, body.life_stage):
            raise Invalid("pick a species from the list")
        label, life_stage = body.label, body.life_stage
    elif body.label:
        raise Invalid("no species goes with this answer")
    extra = validate_label(body, catalogue=catalogue, name=label)
    return {"bbox": bbox, "label": label, "life_stage": life_stage,
            "confidence": extra["confidence"], "celebrity_id": extra["celebrity_id"]}


async def _is_complete(conn, saved_frame_id: int) -> bool:
    return await fetch_one(conn, "SELECT 1 FROM review_frames WHERE saved_frame_id = %s",
                           (saved_frame_id,)) is not None


async def add_box(conn, *, saved_frame_id: int, user, body: AddedBoxIn, catalogue) -> dict:
    fr = await frame_record(conn, saved_frame_id)
    v = _validate_added(body, catalogue=catalogue, width=fr["width"], height=fr["height"])
    async with conn.transaction():
        if not _is_admin(user) and await _is_complete(conn, saved_frame_id):
            raise Conflict("this frame is already marked complete")
        row = await fetch_one(conn, """
            INSERT INTO review_added_boxes (saved_frame_id, bbox, outcome, label, life_stage,
                                            confidence, celebrity_id, user_id, session_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                              (saved_frame_id, v["bbox"], body.outcome, v["label"],
                               v["life_stage"], v["confidence"], v["celebrity_id"], user.id,
                               user.session_id))
    return {"id": row["id"]}


async def _own_box(conn, *, saved_frame_id: int, box_id: int, user) -> None:
    row = await fetch_one(conn, """SELECT saved_frame_id, user_id FROM review_added_boxes
                                    WHERE id = %s AND removed_at IS NULL FOR UPDATE""",
                          (box_id,))
    if row is None or row["saved_frame_id"] != saved_frame_id:
        raise NotFound("no such drawn box")
    if _is_admin(user):
        return
    if row["user_id"] != user.id:
        raise Forbidden("you can only change boxes you drew")
    if await _is_complete(conn, saved_frame_id):
        raise Conflict("this frame is already marked complete")


async def update_box(conn, *, saved_frame_id: int, box_id: int, user, body: AddedBoxIn,
                     catalogue) -> dict:
    fr = await frame_record(conn, saved_frame_id)
    v = _validate_added(body, catalogue=catalogue, width=fr["width"], height=fr["height"])
    async with conn.transaction():
        await _own_box(conn, saved_frame_id=saved_frame_id, box_id=box_id, user=user)
        async with conn.cursor() as cur:
            await cur.execute("""
                UPDATE review_added_boxes
                   SET bbox = %s, outcome = %s, label = %s, life_stage = %s, confidence = %s,
                       celebrity_id = %s, updated_at = now()
                 WHERE id = %s""", (v["bbox"], body.outcome, v["label"], v["life_stage"],
                                    v["confidence"], v["celebrity_id"], box_id))
    return {"id": box_id}


async def remove_box(conn, *, saved_frame_id: int, box_id: int, user) -> None:
    async with conn.transaction():
        await _own_box(conn, saved_frame_id=saved_frame_id, box_id=box_id, user=user)
        async with conn.cursor() as cur:
            await cur.execute("UPDATE review_added_boxes SET removed_at = now() WHERE id = %s",
                              (box_id,))


async def complete_frame(conn, *, saved_frame_id: int, user, catalogue) -> dict:
    state = await frame_state(conn, saved_frame_id=saved_frame_id, user=user,
                              catalogue=catalogue)
    if state["complete"]:
        return {"complete": True, "already": True}
    if state["blocking"]:
        raise Conflict("answer box " + ", ".join(str(n) for n in state["blocking"]) + " first")
    async with conn.transaction():
        # Racing another completer whose own pre-check also read
        # "not complete": read back whether THIS insert is the one that
        # actually won, rather than trusting the pre-check above, which
        # is stale the moment two completions overlap.
        row = await fetch_one(conn, """
            INSERT INTO review_frames (saved_frame_id, completed_by, session_id)
                 VALUES (%s, %s, %s) ON CONFLICT DO NOTHING RETURNING saved_frame_id""",
                              (saved_frame_id, user.id, user.session_id))
    return {"complete": True, "already": row is None}


async def uncomplete_frame(conn, *, saved_frame_id: int) -> None:
    async with conn.transaction(), conn.cursor() as cur:
        await cur.execute("DELETE FROM review_frames WHERE saved_frame_id = %s",
                          (saved_frame_id,))

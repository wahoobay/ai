"""The label archive: everything the listed review accounts did, kept outside this database.

Staging's database is a demo copy that a rebuild or reset loses, and Roy's verdicts are
the only reviewer work worth keeping (spec infra
docs/superpowers/specs/2026-11-18-staging-label-archive-design.md). `collect` reads one
account's verdicts, added boxes and skips in a read-only transaction; the files it builds
are deterministic (gzip mtime 0, rows in a fixed order) so unchanged work hashes the same
and the job writes a new snapshot only when something changed.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import os
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from common.storage import exists, join, open_read, put
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from .exports import export_labeled_corrections
from .frames import frame_url

ACCOUNT_COLUMNS = ("id", "username", "display_name", "role", "status", "created_at")

# A frame is matched on camera + frame_id + ts: frame_id restarts with every worker
# restart, so camera + frame_id alone can name another frame. Lowest id wins, as in
# export_labeled_corrections.
VERDICTS_SQL = """
    SELECT json_build_object('verdict', row_to_json(c), 'event', row_to_json(e),
                             'saved_frame', row_to_json(sf))::text
      FROM detection_corrections c
      JOIN detection_events e ON e.id = c.event_id
      LEFT JOIN LATERAL (
          SELECT * FROM saved_frames s
           WHERE s.source_name = e.source_name AND s.frame_id = e.frame_id AND s.ts = e.ts
           ORDER BY s.id
           LIMIT 1
      ) sf ON true
     WHERE c.user_id = %s
     ORDER BY c.id
"""

# The other tables of review work, by name: an allow-list, so a new table with a user_id
# column is never archived by default (the archive is versioned and never deleted, so a
# leaked email or credential would stay). tests/test_label_archive.py fails until every
# user_id table is in one of these two lists.
ARCHIVED_TABLES = ("review_added_boxes", "review_skips")
NOT_ARCHIVED = (
    "detection_corrections",  # archived as verdicts.jsonl.gz, with each verdict's detection and frame
    "user_sessions",  # credentials (token_hash), IP address, browser
    "user_tokens",  # credentials (token_hash)
)

# The species reviewers added (species_suggestions, ai #49/#50) that the account's work
# names: the ones it added, the ones its answers link to, and the ones another reviewer
# added that it picked from the list or drew a box as. Without them a restored archive
# would hold answers naming species no list knows. The table has no user_id column, so the
# allow-list test above does not cover it; this query is its own allow-list.
SPECIES_SQL = """
    SELECT row_to_json(s)::text AS j FROM species_suggestions s
     WHERE s.suggested_by = %(uid)s
        OR s.id IN (SELECT c.species_suggestion_id FROM detection_corrections c
                     WHERE c.user_id = %(uid)s AND c.species_suggestion_id IS NOT NULL)
        OR (s.approved_name IS NOT NULL AND (
                EXISTS (SELECT 1 FROM detection_corrections c
                         WHERE c.user_id = %(uid)s AND c.corrected_name = s.approved_name
                           AND c.life_stage IS NOT DISTINCT FROM s.approved_stage)
             OR EXISTS (SELECT 1 FROM review_added_boxes b
                         WHERE b.user_id = %(uid)s AND b.label = s.approved_name
                           AND b.life_stage IS NOT DISTINCT FROM s.approved_stage)))
     ORDER BY j
"""

# The frames behind the boxes the account drew, which a verdict may not cover.
ADDED_BOX_FRAMES_SQL = """
    SELECT s.image_path, s.coco_path FROM saved_frames s
     WHERE s.id IN (SELECT saved_frame_id FROM review_added_boxes WHERE user_id = %s)
"""


CONTENT_TYPES = {
    ".json": "application/json",
    ".gz": "application/gzip",
    ".csv": "text/csv",
    ".txt": "text/plain",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
}


class AccountChanged(RuntimeError):
    """The account with the listed username is not the one archived (a staging reset made a
    new account with the same name): latest.json is left as it is until someone decides."""


class ArchiveFailed(RuntimeError):
    """One or more accounts or frames failed; the rest were archived."""


@asynccontextmanager
async def read_only_transaction(conn):
    async with conn.transaction():
        await conn.execute("SET TRANSACTION READ ONLY")
        yield conn


class UnknownAccount(ValueError):
    """No users row has that username (a misspelled or renamed account in the list)."""


@dataclass(frozen=True)
class AccountSnapshot:
    username: str
    slug: str
    files: dict[str, bytes]
    frame_paths: list[str]
    counts: dict[str, int]


def slug(username: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", username.lower()).strip("-")


def content_hash(files: dict[str, bytes]) -> str:
    h = hashlib.sha256()
    for name in sorted(files):
        h.update(name.encode() + b"\0" + hashlib.sha256(files[name]).digest())
    return h.hexdigest()


def _jsonl_gz(lines: list[str]) -> bytes:
    return gzip.compress("".join(line + "\n" for line in lines).encode(), mtime=0)


async def collect(pool: AsyncConnectionPool, username: str) -> AccountSnapshot:
    files: dict[str, bytes] = {}
    counts: dict[str, int] = {}
    paths: set[str] = set()
    async with pool.connection() as conn, read_only_transaction(conn):
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {', '.join(ACCOUNT_COLUMNS)} FROM users WHERE username = %s", (username,)
            )
            account = await cur.fetchone()
        if account is None:
            raise UnknownAccount(f"no review account named {username!r}")
        uid = account["id"]
        files["account.json"] = json.dumps(account, default=str, indent=1, sort_keys=True).encode()

        cur = await conn.execute(VERDICTS_SQL, (uid,))
        verdicts = [row[0] for row in await cur.fetchall()]
        files["verdicts.jsonl.gz"] = _jsonl_gz(verdicts)
        counts["verdicts"] = len(verdicts)
        for line in verdicts:
            row: dict[str, Any] = json.loads(line)
            # A frame no saved_frames row matches is still the event's own image_path, as in
            # the CSV's COALESCE(sf.image_path, e.image_path).
            frame = row["saved_frame"] or {"image_path": row["event"].get("image_path")}
            paths.update(p for p in (frame.get("image_path"), frame.get("coco_path")) if p)

        for table in ARCHIVED_TABLES:
            cur = await conn.execute(
                f'SELECT row_to_json(x)::text AS j FROM "{table}" x WHERE user_id = %s ORDER BY j',
                (uid,),
            )
            rows = [row[0] for row in await cur.fetchall()]
            if rows:
                files[f"tables/{table}.jsonl.gz"] = _jsonl_gz(rows)
                counts[table] = len(rows)

        cur = await conn.execute(SPECIES_SQL, {"uid": uid})
        rows = [row[0] for row in await cur.fetchall()]
        if rows:
            files["tables/species_suggestions.jsonl.gz"] = _jsonl_gz(rows)
            counts["species_suggestions"] = len(rows)

        cur = await conn.execute(ADDED_BOX_FRAMES_SQL, (uid,))
        for image_path, coco_path in await cur.fetchall():
            paths.update(p for p in (image_path, coco_path) if p)

    # The CSV the training-set builder reads; the export streams on its own connection.
    stream = export_labeled_corrections(pool, include_identity=True, user_ids=[uid])
    files["labeled_corrections.csv"] = "".join([chunk async for chunk in stream]).encode()

    frame_paths = sorted(paths)
    files["frames.txt"] = "".join(p + "\n" for p in frame_paths).encode()
    return AccountSnapshot(username, slug(username), files, frame_paths, counts)


# ----- the archive_labels job ---------------------------------------------------------


def _content_type(name: str) -> str:
    return CONTENT_TYPES.get(os.path.splitext(name)[1].lower(), "application/octet-stream")


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H%M%SZ")


def _accounts() -> list[str]:
    raw = os.environ.get("LABEL_ARCHIVE_ACCOUNTS", "").strip()
    return [str(u) for u in json.loads(raw)] if raw else []


def _copy_frames(base: str, paths: list[str]) -> tuple[int, list[str], list[str]]:
    """Copy each frame not yet in the archive. A frame gone from storage is reported, not
    fatal (it would otherwise fail every run forever); a frame that fails another way is
    reported and fails the run, but never holds up the verdicts' snapshot."""
    copied, missing, failed = 0, [], []
    for path in paths:
        dst = join(base, "frames/" + path.lstrip("/"))
        try:
            if exists(dst):
                continue
            data = open_read(frame_url(path))
            put(dst, data, _content_type(path))
            copied += 1
        except FileNotFoundError:
            missing.append(path)
        except Exception as e:  # noqa: BLE001 - reported per frame, raised once at the end
            failed.append(f"{path}: {type(e).__name__}: {e}")
    return copied, missing, failed


def _identity(snap: AccountSnapshot) -> dict[str, Any]:
    """The account's id and creation time (in UTC: the text depends on the session's zone)."""
    account = json.loads(snap.files["account.json"])
    created = datetime.fromisoformat(account["created_at"]).astimezone(UTC).isoformat()
    return {"id": account["id"], "created_at": created}


def _write_snapshot(
    base: str, snap: AccountSnapshot, digest: str, missing: list[str], failed: list[str]
) -> str:
    """The snapshot's files, then its manifest, then latest.json last: a run that dies
    part-way never leaves a half-written snapshot named as the latest."""
    stamp = _stamp()
    folder = join(base, f"snapshots/{stamp}")
    for name, data in snap.files.items():
        put(join(folder, name), data, _content_type(name))
    manifest = {
        "account": snap.username,
        "exported_at": stamp,
        "sha256": digest,
        "counts": snap.counts,
        "files": {
            name: {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            for name, data in sorted(snap.files.items())
        },
        "frames_missing": missing,
        "frames_failed": [f.split(": ", 1)[0] for f in failed],
    }
    put(join(folder, "manifest.json"), json.dumps(manifest, indent=1).encode(), "application/json")
    latest = {"snapshot": stamp, "sha256": digest, "account": _identity(snap)}
    put(join(base, "latest.json"), json.dumps(latest).encode(), "application/json")
    return stamp


def _latest(base: str) -> dict[str, Any]:
    url = join(base, "latest.json")
    return json.loads(open_read(url)) if exists(url) else {}


async def archive_labels(pool: AsyncConnectionPool) -> dict[str, Any]:
    """Keep LABEL_ARCHIVE_ACCOUNTS' work under LABEL_ARCHIVE_URL/<slug>/: a new snapshot
    when anything changed, and every frame behind it."""
    accounts = _accounts()
    if not accounts:
        return {"accounts": {}}
    url = os.environ.get("LABEL_ARCHIVE_URL", "").strip()
    if not url:
        raise RuntimeError("LABEL_ARCHIVE_ACCOUNTS is set but LABEL_ARCHIVE_URL is not")
    results: dict[str, Any] = {}
    errors: list[str] = []
    for username in accounts:  # one account's failure never stops the others
        try:
            results[slug(username)] = await _archive_account(pool, url, username, errors)
        except Exception as e:  # noqa: BLE001 - collected, raised once at the end
            errors.append(f"{username}: {type(e).__name__}: {e}")
    if errors:
        raise ArchiveFailed("; ".join(errors))
    return {"accounts": results}


async def _archive_account(
    pool: AsyncConnectionPool, url: str, username: str, errors: list[str]
) -> dict[str, Any]:
    snap = await collect(pool, username)
    base = join(url, snap.slug)
    latest = await asyncio.to_thread(_latest, base)
    archived = latest.get("account")
    if archived and archived != _identity(snap):
        raise AccountChanged(
            f"the account named {username!r} is {_identity(snap)}, not the archived {archived}; "
            "latest.json is left as it is"
        )
    digest = content_hash(snap.files)
    copied, missing, failed = await asyncio.to_thread(_copy_frames, base, snap.frame_paths)
    stamp = None
    if latest.get("sha256") != digest:
        stamp = await asyncio.to_thread(_write_snapshot, base, snap, digest, missing, failed)
    if failed:
        errors.append(f"{username}: {len(failed)} frame(s) not copied, first {failed[0]}")
    return {
        "snapshot": stamp,
        "unchanged": stamp is None,
        "counts": snap.counts,
        "frames_copied": copied,
        "frames_missing": len(missing),
        "frames_failed": len(failed),
    }

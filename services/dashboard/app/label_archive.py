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

# Every other table an account owns rows in, except users itself and any table that
# holds a credential (user_sessions, user_tokens: their token_hash columns).
TABLES_SQL = """
    SELECT t.table_name
      FROM information_schema.tables t
     WHERE t.table_schema = 'public' AND t.table_type = 'BASE TABLE'
       AND t.table_name NOT IN ('users', 'detection_corrections')
       AND EXISTS (SELECT 1 FROM information_schema.columns c
                    WHERE c.table_schema = 'public' AND c.table_name = t.table_name
                      AND c.column_name = 'user_id')
       AND NOT EXISTS (SELECT 1 FROM information_schema.columns c
                        WHERE c.table_schema = 'public' AND c.table_name = t.table_name
                          AND c.column_name ~* '(token|password|hash|secret)')
     ORDER BY 1
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
    async with pool.connection() as conn, conn.transaction():
        await conn.execute("SET TRANSACTION READ ONLY")
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
            frame: dict[str, Any] = json.loads(line)["saved_frame"] or {}
            paths.update(p for p in (frame.get("image_path"), frame.get("coco_path")) if p)

        cur = await conn.execute(TABLES_SQL)
        for (table,) in await cur.fetchall():
            cur = await conn.execute(
                f'SELECT row_to_json(x)::text AS j FROM "{table}" x WHERE user_id = %s ORDER BY j',
                (uid,),
            )
            rows = [row[0] for row in await cur.fetchall()]
            if rows:
                files[f"tables/{table}.jsonl.gz"] = _jsonl_gz(rows)
                counts[table] = len(rows)

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


def _copy_frames(base: str, paths: list[str]) -> tuple[int, list[str]]:
    """Copy each frame not yet in the archive; a frame gone from storage is reported, not fatal
    (it would otherwise fail every run forever)."""
    copied, missing = 0, []
    for path in paths:
        dst = join(base, "frames/" + path.lstrip("/"))
        if exists(dst):
            continue
        try:
            data = open_read(frame_url(path))
        except FileNotFoundError:
            missing.append(path)
            continue
        put(dst, data, _content_type(path))
        copied += 1
    return copied, missing


def _write_snapshot(base: str, snap: AccountSnapshot, digest: str, missing: list[str]) -> str:
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
    }
    put(join(folder, "manifest.json"), json.dumps(manifest, indent=1).encode(), "application/json")
    latest = {"snapshot": stamp, "sha256": digest}
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
    for username in accounts:
        snap = await collect(pool, username)
        base = join(url, snap.slug)
        digest = content_hash(snap.files)
        copied, missing = await asyncio.to_thread(_copy_frames, base, snap.frame_paths)
        latest = await asyncio.to_thread(_latest, base)
        stamp = None
        if latest.get("sha256") != digest:
            stamp = await asyncio.to_thread(_write_snapshot, base, snap, digest, missing)
        results[snap.slug] = {
            "snapshot": stamp,
            "unchanged": stamp is None,
            "counts": snap.counts,
            "frames_copied": copied,
            "frames_missing": len(missing),
        }
    return {"accounts": results}

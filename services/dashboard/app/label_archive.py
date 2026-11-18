"""The label archive: everything the listed review accounts did, kept outside this database.

Staging's database is a demo copy that a rebuild or reset loses, and Roy's verdicts are
the only reviewer work worth keeping (spec infra
docs/superpowers/specs/2026-11-18-staging-label-archive-design.md). `collect` reads one
account's verdicts, added boxes and skips in a read-only transaction; the files it builds
are deterministic (gzip mtime 0, rows in a fixed order) so unchanged work hashes the same
and the job writes a new snapshot only when something changed.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from .exports import export_labeled_corrections

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

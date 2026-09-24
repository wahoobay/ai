"""Schema for the review tool (spec §5): tables, columns, constraints,
and that db/init.sql carries the same statements as the migration."""
from __future__ import annotations

import re
from pathlib import Path

import psycopg
import pytest
from review_fixtures import TEST_DSN

REPO = Path(__file__).resolve().parents[1]
MIGRATION = REPO / "db" / "migrations" / "2026-09-24-review-tool.sql"
needs_db = pytest.mark.skipif(not TEST_DSN, reason="TEST_DATABASE_URL not set")


def _body(sql: str) -> str:
    """The migration minus its header comments and the SET line, with
    whitespace collapsed, so it can be looked for inside init.sql."""
    lines = [ln for ln in sql.splitlines()
             if not ln.startswith("--") and not ln.startswith("SET lock_timeout")]
    return re.sub(r"\s+", " ", "\n".join(lines)).strip()


def test_migration_is_mirrored_in_init_sql():
    init = re.sub(r"\s+", " ", (REPO / "db" / "init.sql").read_text())
    assert _body(MIGRATION.read_text()) in init


def test_init_sql_creates_verdicts_with_restrict():
    init = (REPO / "db" / "init.sql").read_text()
    block = init[init.index("CREATE TABLE IF NOT EXISTS detection_corrections"):]
    block = block[: block.index(");")]
    assert "ON DELETE RESTRICT" in block and "ON DELETE CASCADE" not in block


@needs_db
def test_review_tables_and_columns_exist():
    with psycopg.connect(TEST_DSN) as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")}
        assert {"review_items", "review_skips", "review_added_boxes",
                "review_frames", "review_settings"} <= tables

        def cols(table):
            return {r[0] for r in conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
                (table,))}

        assert {"review_item_id", "label_source", "unidentifiable", "life_stage",
                "is_adjudication"} <= cols("detection_corrections")
        assert {"credit_opt_in", "credit_name", "orcid"} <= cols("users")
        assert {"hints", "hints_model", "hinted_at", "route_species", "exclude_user_id",
                "first_correction_id"} <= cols("review_items")


@needs_db
def test_migration_is_idempotent():
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        conn.execute(MIGRATION.read_text())
        conn.execute(MIGRATION.read_text())


@needs_db
def test_a_verdict_blocks_deleting_its_detection():
    with psycopg.connect(TEST_DSN) as conn:
        conn.execute("TRUNCATE detection_events, detection_corrections RESTART IDENTITY CASCADE")
        eid = conn.execute(
            """INSERT INTO detection_events (frame_id, source_name, det_conf, bbox, topk)
               VALUES (1, 'seahivecam', 0.9, '{1,2,30,40}', '[]') RETURNING id""").fetchone()[0]
        conn.execute("INSERT INTO detection_corrections (event_id, not_a_fish) VALUES (%s, true)",
                     (eid,))
        conn.commit()
        # An explicit ON DELETE RESTRICT (required by
        # test_init_sql_creates_verdicts_with_restrict below, and by Step 7
        # of the task brief) raises SQLSTATE 23001 (restrict_violation),
        # not 23503 (foreign_key_violation): PostgreSQL reserves
        # foreign_key_violation for the default ON DELETE NO ACTION.
        # psycopg.errors.RestrictViolation is a sibling of
        # ForeignKeyViolation under IntegrityError, not a subclass of it,
        # so the two are not interchangeable here.
        with pytest.raises(psycopg.errors.RestrictViolation):
            conn.execute("DELETE FROM detection_events WHERE id = %s", (eid,))
        conn.rollback()


@needs_db
def test_a_detection_without_verdicts_can_still_be_deleted():
    with psycopg.connect(TEST_DSN) as conn:
        conn.execute("TRUNCATE detection_events, detection_corrections RESTART IDENTITY CASCADE")
        eid = conn.execute(
            """INSERT INTO detection_events (frame_id, source_name, det_conf, bbox, topk)
               VALUES (1, 'seahivecam', 0.9, '{1,2,30,40}', '[]') RETURNING id""").fetchone()[0]
        conn.execute("DELETE FROM detection_events WHERE id = %s", (eid,))
        conn.commit()


@needs_db
def test_a_verdict_cannot_be_both_no_animal_and_unidentifiable():
    with psycopg.connect(TEST_DSN) as conn:
        conn.execute("TRUNCATE detection_events, detection_corrections RESTART IDENTITY CASCADE")
        eid = conn.execute(
            """INSERT INTO detection_events (frame_id, source_name, det_conf, bbox, topk)
               VALUES (1, 'seahivecam', 0.9, '{1,2,30,40}', '[]') RETURNING id""").fetchone()[0]
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                """INSERT INTO detection_corrections (event_id, not_a_fish, unidentifiable)
                   VALUES (%s, true, true)""", (eid,))
        conn.rollback()


@needs_db
def test_one_review_item_per_event_and_pass():
    with psycopg.connect(TEST_DSN) as conn:
        conn.execute("TRUNCATE detection_events, saved_frames RESTART IDENTITY CASCADE")
        eid = conn.execute(
            """INSERT INTO detection_events (frame_id, source_name, det_conf, bbox, topk)
               VALUES (1, 'seahivecam', 0.9, '{1,2,30,40}', '[]') RETURNING id""").fetchone()[0]
        sf = conn.execute(
            """INSERT INTO saved_frames (frame_id, source_name, reason, image_path, coco_path,
                                         num_fish)
               VALUES (1, 'seahivecam', 'timelapse', '/x.jpg', '/x.coco.json', 1)
               RETURNING id""").fetchone()[0]
        ins = """INSERT INTO review_items (event_id, pass, lane, saved_frame_id, source_name, ts,
                                           bbox)
                 VALUES (%s, %s, 'check', %s, 'seahivecam', now(), '{1,2,30,40}')"""
        conn.execute(ins, (eid, 1, sf))
        conn.execute(ins, (eid, 2, sf))
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(ins, (eid, 1, sf))
        conn.rollback()

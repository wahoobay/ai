-- Reviewer annotation tool: every schema change for phases 1-3
-- (docs/superpowers/specs/2026-09-24-reviewer-annotation-dataset-design.md, section 5).
--
-- Apply to the live database as one transaction, this file only:
--   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -1 -f db/migrations/2026-09-24-review-tool.sql
-- Every statement is idempotent. The same statements are appended to
-- db/init.sql for fresh databases; tests/test_review_schema.py fails if
-- the two drift apart.
SET lock_timeout = '5s';

CREATE TABLE IF NOT EXISTS review_items (
    id                  BIGSERIAL PRIMARY KEY,
    event_id            BIGINT      NOT NULL REFERENCES detection_events(id) ON DELETE CASCADE,
    pass                SMALLINT    NOT NULL DEFAULT 1 CHECK (pass IN (1, 2)),
    lane                TEXT        NOT NULL CHECK (lane IN ('check', 'identify')),
    saved_frame_id      BIGINT      NOT NULL REFERENCES saved_frames(id),
    source_name         TEXT        NOT NULL,
    ts                  TIMESTAMPTZ NOT NULL,
    bbox                INTEGER[]   NOT NULL,
    predicted           TEXT,
    predicted_p         REAL,
    hints               JSONB,
    hints_model         TEXT,
    hinted_at           TIMESTAMPTZ,
    route_species       TEXT,
    status              TEXT        NOT NULL DEFAULT 'open'
                        CHECK (status IN ('open', 'claimed', 'done', 'retired')),
    retired_reason      TEXT,
    claimed_by          BIGINT      REFERENCES users(id),
    claimed_until       TIMESTAMPTZ,
    exclude_user_id     BIGINT      REFERENCES users(id),
    first_correction_id BIGINT      REFERENCES detection_corrections(id),
    added_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    done_at             TIMESTAMPTZ,
    UNIQUE (event_id, pass)
);
CREATE INDEX IF NOT EXISTS review_items_open
    ON review_items (lane, pass, route_species) WHERE status = 'open';
CREATE INDEX IF NOT EXISTS review_items_claimed
    ON review_items (claimed_until) WHERE status = 'claimed';
CREATE INDEX IF NOT EXISTS review_items_unscored
    ON review_items (id) WHERE hints IS NULL AND status IN ('open', 'claimed');
CREATE INDEX IF NOT EXISTS review_items_camera_ts
    ON review_items (source_name, ts);
CREATE INDEX IF NOT EXISTS review_items_frame
    ON review_items (saved_frame_id);

CREATE TABLE IF NOT EXISTS review_skips (
    item_id     BIGINT      NOT NULL REFERENCES review_items(id) ON DELETE CASCADE,
    user_id     BIGINT      NOT NULL REFERENCES users(id),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (item_id, user_id)
);

CREATE TABLE IF NOT EXISTS review_added_boxes (
    id              BIGSERIAL PRIMARY KEY,
    saved_frame_id  BIGINT      NOT NULL REFERENCES saved_frames(id),
    bbox            INTEGER[]   NOT NULL,
    outcome         TEXT        NOT NULL CHECK (outcome IN ('species', 'unidentifiable')),
    label           TEXT,
    life_stage      TEXT,
    confidence      TEXT        NOT NULL DEFAULT 'certain'
                    CHECK (confidence IN ('certain', 'probable', 'uncertain')),
    celebrity_id    TEXT,
    user_id         BIGINT      NOT NULL REFERENCES users(id),
    session_id      BIGINT      REFERENCES user_sessions(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ,
    removed_at      TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS review_added_boxes_frame
    ON review_added_boxes (saved_frame_id) WHERE removed_at IS NULL;

CREATE TABLE IF NOT EXISTS review_frames (
    saved_frame_id  BIGINT      PRIMARY KEY REFERENCES saved_frames(id),
    completed_by    BIGINT      NOT NULL REFERENCES users(id),
    session_id      BIGINT      REFERENCES user_sessions(id),
    completed_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS review_settings (
    key         TEXT        PRIMARY KEY,
    value       JSONB       NOT NULL,
    updated_by  BIGINT      REFERENCES users(id),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE detection_corrections
    ADD COLUMN IF NOT EXISTS review_item_id  BIGINT REFERENCES review_items(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS label_source    TEXT
        CHECK (label_source IN ('model_guess', 'hint', 'search')),
    ADD COLUMN IF NOT EXISTS unidentifiable  BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS life_stage      TEXT,
    ADD COLUMN IF NOT EXISTS is_adjudication BOOLEAN NOT NULL DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS detection_corrections_review_item
    ON detection_corrections (review_item_id) WHERE review_item_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS detection_corrections_user
    ON detection_corrections (user_id, created_at DESC) WHERE user_id IS NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'detection_corrections_outcome_chk') THEN
        ALTER TABLE detection_corrections
            ADD CONSTRAINT detection_corrections_outcome_chk
            CHECK (NOT (not_a_fish AND unidentifiable));
    END IF;
END $$;

-- Human verdicts must never disappear because a detection was pruned.
-- Replace the cascading foreign key once; on a database already using
-- RESTRICT (fresh init.sql, or a second run) this block does nothing.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_constraint
                WHERE conname = 'detection_corrections_event_id_fkey'
                  AND conrelid = 'detection_corrections'::regclass
                  AND confdeltype = 'c') THEN
        ALTER TABLE detection_corrections
            ADD CONSTRAINT detection_corrections_event_id_restrict
            FOREIGN KEY (event_id) REFERENCES detection_events(id) ON DELETE RESTRICT NOT VALID;
        ALTER TABLE detection_corrections
            VALIDATE CONSTRAINT detection_corrections_event_id_restrict;
        ALTER TABLE detection_corrections DROP CONSTRAINT detection_corrections_event_id_fkey;
        ALTER TABLE detection_corrections
            RENAME CONSTRAINT detection_corrections_event_id_restrict
            TO detection_corrections_event_id_fkey;
    END IF;
END $$;

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS credit_opt_in BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS credit_name   TEXT,
    ADD COLUMN IF NOT EXISTS orcid         TEXT;

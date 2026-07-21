-- recall plugin schema. Idempotent — safe to run on every boot.
-- Tables:
--   conversation_history : every inbound + outbound message
--   recall_facts         : curated archival facts (acts like a queryable MEMORY.md)
--   user_profiles        : per-user prefs / tier / display name
--   tool_invocations     : every tool call logged for observability
--   recall_config        : runtime flags (kill-switch for auto-save, etc.)

CREATE TABLE IF NOT EXISTS conversation_history (
    id              BIGSERIAL PRIMARY KEY,
    user_phone      TEXT NOT NULL,             -- E.164, the per-user shard key
    tier            TEXT NOT NULL DEFAULT 'unknown',  -- admin / user / stranger
    direction       TEXT NOT NULL,             -- inbound (user→bot) | outbound (bot→user)
    message         TEXT NOT NULL,
    session_id      TEXT,
    platform        TEXT,                      -- e.g. 'inkbox'
    model           TEXT,                      -- which LLM produced this (outbound only)
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    extra           JSONB DEFAULT '{}'         -- room for tool_calls / token_usage / etc.
);

-- Per-user-recency lookups (recall_recent, recall_last_conversation)
CREATE INDEX IF NOT EXISTS idx_ch_phone_time
    ON conversation_history (user_phone, created_at DESC);

-- Full-text search across messages
CREATE INDEX IF NOT EXISTS idx_ch_fts
    ON conversation_history USING gin (to_tsvector('english', message));

-- Cleanup helper
CREATE INDEX IF NOT EXISTS idx_ch_created
    ON conversation_history (created_at);


CREATE TABLE IF NOT EXISTS recall_facts (
    id          BIGSERIAL PRIMARY KEY,
    user_phone  TEXT,                          -- NULL = global fact (admin-only)
    fact        TEXT NOT NULL,
    source      TEXT,                          -- 'drew_curated' | 'explicit_trigger' | 'digest_approved'
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    superseded_at TIMESTAMPTZ                  -- soft delete; non-null = no longer active
);

-- GIN index for tsvector fallback (used when embedding IS NULL)
CREATE INDEX IF NOT EXISTS idx_rf_fts
    ON recall_facts USING gin (to_tsvector('english', fact));

-- Active-facts index (superseded_at IS NULL filter)
CREATE INDEX IF NOT EXISTS idx_rf_phone_active
    ON recall_facts (user_phone, superseded_at)
    WHERE superseded_at IS NULL;

-- tier_visibility column — explicit access control per fact.
-- 'stranger' = visible to all callers (LEAST restrictive)
-- 'user'     = visible to user-tier and admin-tier callers
-- 'admin'    = visible to admin-tier callers only (MOST restrictive)
-- NOTE: schema comment "most restrictive" on older migrations was wrong.
-- 'stranger' is the default because extracted facts have no known sensitivity.
-- Auto-saved user facts use 'user' (set at insert time, not here).
ALTER TABLE IF EXISTS recall_facts
    ADD COLUMN IF NOT EXISTS tier_visibility TEXT NOT NULL DEFAULT 'stranger';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'recall_facts_tier_visibility_chk'
    ) THEN
        ALTER TABLE recall_facts
            ADD CONSTRAINT recall_facts_tier_visibility_chk
            CHECK (tier_visibility IN ('stranger', 'user', 'admin'));
    END IF;
END$$;

-- Backfill: drew_curated facts are admin-visible; everything else stays stranger.
UPDATE recall_facts SET tier_visibility = 'admin'
    WHERE source = 'drew_curated' AND tier_visibility = 'stranger';

CREATE INDEX IF NOT EXISTS idx_rf_tier_active
    ON recall_facts (tier_visibility, superseded_at)
    WHERE superseded_at IS NULL;

-- Dedup index: prevents exact-duplicate (user_phone, fact) active facts.
-- NULLS NOT DISTINCT requires PG15+; on PG14 this falls through to no dedup
-- for NULL-phone global facts (acceptable — global facts are admin-curated only).
DO $$
BEGIN
    -- PG15+ supports NULLS NOT DISTINCT on unique indexes
    IF current_setting('server_version_num')::int >= 150000 THEN
        IF NOT EXISTS (SELECT 1 FROM pg_indexes WHERE indexname = 'idx_rf_dedup') THEN
            EXECUTE '
                CREATE UNIQUE INDEX idx_rf_dedup
                ON recall_facts (user_phone, fact)
                NULLS NOT DISTINCT
                WHERE superseded_at IS NULL
            ';
        END IF;
    ELSE
        -- PG14 fallback: dedup only for non-NULL user_phone (user-scoped facts)
        CREATE UNIQUE INDEX IF NOT EXISTS idx_rf_dedup
            ON recall_facts (user_phone, fact)
            WHERE superseded_at IS NULL AND user_phone IS NOT NULL;
    END IF;
END$$;

-- pgvector embedding column (optional — graceful no-op when extension absent)
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_type WHERE typname = 'vector') THEN
        ALTER TABLE IF EXISTS recall_facts
            ADD COLUMN IF NOT EXISTS embedding vector(768);
    END IF;
END$$;


-- v3 Phase 3: per-user profile overrides
CREATE TABLE IF NOT EXISTS user_profiles (
    user_phone      TEXT PRIMARY KEY,
    display_name    TEXT,
    nickname        TEXT,
    tone_override   TEXT,
    enabled_skills  TEXT[],
    disabled_skills TEXT[],
    model_override  TEXT,
    prefs           JSONB NOT NULL DEFAULT '{}'::jsonb,
    upgraded_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_user_profiles_upgraded
    ON user_profiles (upgraded_at) WHERE upgraded_at IS NOT NULL;

-- v4 Phase 4: extend memories with per-user vector recall
ALTER TABLE IF EXISTS memories ADD COLUMN IF NOT EXISTS user_phone TEXT;
CREATE INDEX IF NOT EXISTS idx_memories_user
    ON memories (user_phone) WHERE user_phone IS NOT NULL;


-- phones.tz column (cron timezone)
ALTER TABLE IF EXISTS phones ADD COLUMN IF NOT EXISTS tz TEXT;
UPDATE phones SET tz = 'America/Chicago' WHERE tz IS NULL;


-- Phase 2 prep: tool-call observability.
CREATE TABLE IF NOT EXISTS tool_invocations (
    id              BIGSERIAL PRIMARY KEY,
    caller_phone    TEXT,
    caller_tier     TEXT,
    session_id      TEXT,
    tool_name       TEXT NOT NULL,
    toolset         TEXT,
    args_summary    TEXT,        -- truncated JSON, max 2000 chars, secrets-scrubbed
    result_summary  TEXT,        -- truncated, max 2000 chars
    ok              BOOLEAN NOT NULL,
    error           TEXT,
    duration_ms     INTEGER,
    invoked_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ti_caller_time
    ON tool_invocations (caller_phone, invoked_at DESC);
CREATE INDEX IF NOT EXISTS idx_ti_tool_name
    ON tool_invocations (tool_name, invoked_at DESC);
CREATE INDEX IF NOT EXISTS idx_ti_failures
    ON tool_invocations (invoked_at DESC) WHERE NOT ok;


-- Runtime configuration table.
-- Key=value pairs used as hot-reloadable flags. Changes take effect on next
-- request without a Railway redeploy (unlike env vars which trigger redeploy).
--
-- Known keys:
--   auto_save_enabled  : 'true' | 'false'  — kill-switch for auto-save (item 4)
CREATE TABLE IF NOT EXISTS recall_config (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Default: auto-save enabled. UPDATE this row to disable without redeploy.
INSERT INTO recall_config (key, value)
    VALUES ('auto_save_enabled', 'true')
    ON CONFLICT (key) DO NOTHING;

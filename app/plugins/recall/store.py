"""recall.store — write + read API over conversation_history + recall_facts.

Thin layer on top of db.py. Handlers in __init__.py call these.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from . import db as _db

logger = logging.getLogger(__name__)

# Smart-memory tuning knobs (2026-06-09). Kept here so both the write-time
# supersede path and the read-time ranking path read the SAME thresholds.
#
# SUPERSEDE_SIM_THRESHOLD is deliberately CONSERVATIVE: a new fact only retires
# an older one when their cosine similarity is >= this AND they share the same
# user shard. 0.92 means "near-identical restatement" (e.g. "Drew prefers
# PayPal" vs "Drew likes to be paid via PayPal"), not merely "same topic" — so
# we never lose two distinct facts about the same subject.
SUPERSEDE_SIM_THRESHOLD = 0.92


# ---------------------------------------------------------------------------
# Conversation history
# ---------------------------------------------------------------------------

def record_message(
    *,
    user_phone: str,
    direction: str,
    message: str,
    tier: str = "unknown",
    session_id: Optional[str] = None,
    platform: Optional[str] = None,
    model: Optional[str] = None,
    extra: Optional[dict] = None,
) -> None:
    """Insert one message. Silent no-op if DB unavailable."""
    if not message or not user_phone:
        return
    _db.execute(
        """
        INSERT INTO conversation_history
            (user_phone, tier, direction, message, session_id, platform, model, extra)
        VALUES (%s::text, %s::text, %s::text, %s::text, %s::text, %s::text, %s::text, %s::jsonb)
        """,
        (
            user_phone,
            tier,
            direction,
            message[:32000],  # safety cap so a runaway message can't blow up the column
            session_id,
            platform,
            model,
            json.dumps(extra or {}),
        ),
    )


def recent_messages(
    *,
    user_phone: Optional[str] = None,
    limit: int = 10,
) -> list[dict]:
    """Last N messages, newest first. Optionally scoped by user."""
    limit = max(1, min(int(limit), 200))
    if user_phone:
        return _db.fetch(
            """
            SELECT id, user_phone, direction, message, tier, session_id, platform, created_at
            FROM conversation_history
            WHERE user_phone = %s::text
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (user_phone, limit),
        )
    return _db.fetch(
        """
        SELECT id, user_phone, direction, message, tier, session_id, platform, created_at
        FROM conversation_history
        ORDER BY created_at DESC
        LIMIT %s
        """,
        (limit,),
    )


def search_messages(
    *,
    query: str,
    user_phone: Optional[str] = None,
    limit: int = 5,
) -> list[dict]:
    """Postgres FTS over message. Returns relevance-ranked rows."""
    query = (query or "").strip()
    limit = max(1, min(int(limit), 50))
    if not query:
        return []
    # plainto_tsquery handles unsanitized user input safely.
    if user_phone:
        return _db.fetch(
            """
            SELECT id, user_phone, direction, message, tier, session_id, platform, created_at,
                   ts_rank(to_tsvector('english', message), plainto_tsquery('english', %s)) AS rank
            FROM conversation_history
            WHERE user_phone = %s::text
              AND to_tsvector('english', message) @@ plainto_tsquery('english', %s)
            ORDER BY rank DESC, created_at DESC
            LIMIT %s
            """,
            (query, user_phone, query, limit),
        )
    return _db.fetch(
        """
        SELECT id, user_phone, direction, message, tier, session_id, platform, created_at,
               ts_rank(to_tsvector('english', message), plainto_tsquery('english', %s)) AS rank
        FROM conversation_history
        WHERE to_tsvector('english', message) @@ plainto_tsquery('english', %s)
        ORDER BY rank DESC, created_at DESC
        LIMIT %s
        """,
        (query, query, limit),
    )


def last_conversation(
    *,
    user_phone: str,
    max_turns: int = 30,
    gap_minutes: int = 30,
) -> list[dict]:
    """Pull the last continuous burst of messages with this user.

    "Continuous burst" = messages no further apart than `gap_minutes`. We
    grab everything in the last `gap_minutes` window from the most recent
    message, then cap at `max_turns` rows and return chronologically.

    The old implementation computed the LAG() gap but then discarded it,
    just returning the last N rows in chronological order — which would
    span multiple distinct sessions if the user hadn't messaged for a
    week and then sent one. The new query bounds the window correctly.
    """
    max_turns = max(1, min(int(max_turns), 200))
    gap_minutes = max(1, min(int(gap_minutes), 60 * 24 * 7))
    rows = _db.fetch(
        """
        WITH last_msg AS (
            SELECT MAX(created_at) AS ts FROM conversation_history
            WHERE user_phone = %s::text
        )
        SELECT id, direction, message, created_at
        FROM conversation_history, last_msg
        WHERE user_phone = %s::text
          AND last_msg.ts IS NOT NULL
          AND created_at >= last_msg.ts - (INTERVAL '1 minute' * %s::int)
        ORDER BY created_at ASC
        LIMIT %s
        """,
        (user_phone, user_phone, gap_minutes, max_turns),
    )
    return rows


# ---------------------------------------------------------------------------
# Archival facts (queryable replacement / augmentation for MEMORY.md)
# ---------------------------------------------------------------------------

_VALID_TIERS = ("stranger", "user", "admin")
_TIER_ORDER = {"stranger": 0, "user": 1, "admin": 2}


def _visible_tiers_for(caller_tier: str) -> tuple[str, ...]:
    """Return the set of tier_visibility values a caller is allowed to read.
    Fail-closed: unknown tier → stranger-only visibility (most restrictive)."""
    level = _TIER_ORDER.get(caller_tier, 0)  # default to stranger level
    return tuple(t for t in _VALID_TIERS if _TIER_ORDER[t] <= level)


def add_fact(
    *,
    fact: str,
    user_phone: Optional[str] = None,
    source: str = "drew_curated",
    tier_visibility: str = "stranger",
) -> Optional[int]:
    """Insert a curated or auto-saved fact. Returns new row id or None.

    Uses ON CONFLICT DO NOTHING against the dedup unique index so exact
    duplicates are silently skipped. Returns None on conflict or DB error.
    Logs whether the insert was a real save vs. a silent dedup conflict.

    tier_visibility: 'stranger' (default, fail-open — curated facts), 'user'
    (auto-saved user-specific facts), 'admin' (admin-only). Explicit_trigger
    callers should pass tier_visibility='user'.
    """
    fact = (fact or "").strip()
    if not fact:
        return None
    if tier_visibility not in _VALID_TIERS:
        tier_visibility = "stranger"
    rows = _db.fetch(
        """
        INSERT INTO recall_facts (user_phone, fact, source, tier_visibility)
        VALUES (%s::text, %s::text, %s::text, %s::text)
        ON CONFLICT DO NOTHING
        RETURNING id
        """,
        (user_phone, fact[:4000], source, tier_visibility),
    )
    if rows:
        new_id = rows[0]["id"]
        logger.debug("recall.store: saved fact id=%s source=%s", new_id, source)
        return new_id
    logger.debug(
        "recall.store: fact dedup hit (not saved) source=%s shard=%s", source, user_phone
    )
    return None


def supersede_near_duplicate(
    *,
    new_id: int,
    embedding: list[float],
    user_phone: Optional[str],
    threshold: float = SUPERSEDE_SIM_THRESHOLD,
) -> int:
    """Soft-delete (set superseded_at) any OLDER active fact in the same shard
    whose cosine similarity to `embedding` is >= `threshold`.

    Conservative by design — only near-identical restatements are retired, so
    two genuinely distinct facts about the same subject both survive. Scoped to
    the SAME user_phone (NULL-shard matches NULL-shard) so we never retire one
    user's fact from another user's write. Excludes `new_id` itself. Returns the
    number of rows superseded. Never raises (returns 0 on any DB error or when
    the embedding column doesn't exist).
    """
    if not embedding or new_id is None:
        return 0
    max_dist = 1.0 - float(threshold)
    try:
        # CTE with FOR UPDATE SKIP LOCKED serializes concurrent supersedes on
        # the same fact row without blocking unrelated writes.
        rows = _db.fetch(
            """
            WITH to_supersede AS (
                SELECT id FROM recall_facts
                WHERE id <> %s::bigint
                  AND superseded_at IS NULL
                  AND embedding IS NOT NULL
                  AND (
                        (user_phone = %s::text)
                     OR (user_phone IS NULL AND %s::text IS NULL)
                  )
                  AND (embedding <=> %s::vector) <= %s::float8
                FOR UPDATE SKIP LOCKED
            )
            UPDATE recall_facts SET superseded_at = NOW()
            WHERE id IN (SELECT id FROM to_supersede)
            RETURNING id
            """,
            (new_id, user_phone, user_phone, embedding, max_dist),
        )
        if rows:
            logger.info(
                "recall.store: superseded %d stale fact(s) duplicated by new "
                "fact id=%s (shard=%s)", len(rows), new_id, user_phone,
            )
        return len(rows)
    except Exception as e:
        logger.debug("recall.store: supersede query failed (no-op): %s", e)
        return 0


# CORE PIN: facts from these sources are curated identity/preference facts.
# They are pinned to the front of every ranked result (ORDER BY is_core DESC)
# so they are never trimmed by the inject layer before non-core filler.
_CORE_FACT_SOURCES = ("drew_curated", "explicit_trigger", "digest_approved")


def ranked_active_facts(
    *,
    user_phone: Optional[str],
    caller_tier: str = "stranger",
    query_embedding: Optional[list[float]] = None,
    query_text: Optional[str] = None,
    limit: int = 200,
) -> list[dict]:
    """Return this caller's ACTIVE facts ordered best-first.

    SHARD ISOLATION: only rows where user_phone = caller OR user_phone IS NULL
    (global) are returned — never another user's facts.

    RANKING — three tiers based on available signals:
    1. query_embedding + query_text present, embedding column exists:
       UNION ALL — vector branch (embedding IS NOT NULL, priority 0) ranked by
       cosine similarity; tsvector branch (embedding IS NULL, priority 1) ranked
       by ts_rank. Vector rows always sorted before tsvector rows.
    2. query_text only (no embedding): ts_rank ranking for all rows.
    3. Neither: pure recency.

    CORE PIN: facts in _CORE_FACT_SOURCES sort is_core=1 first so they are
    never trimmed by the inject layer before non-core filler.

    Returns rows with (id, fact, source, created_at, is_core, rank). Never
    raises — returns [] on DB error.
    """
    has_emb = _column_exists("recall_facts", "embedding")
    has_tv = _column_exists("recall_facts", "tier_visibility")
    limit = max(1, min(int(limit), 500))

    visible = _visible_tiers_for(caller_tier)
    core_expr = "CASE WHEN source = ANY(%s::text[]) THEN 1 ELSE 0 END"

    def _base_where() -> tuple[str, list]:
        """Common WHERE conditions + params (no branch-specific predicates)."""
        conds = ["superseded_at IS NULL", "(user_phone = %s::text OR user_phone IS NULL)"]
        params: list[Any] = [user_phone]
        if has_tv:
            conds.append("tier_visibility = ANY(%s::text[])")
            params.append(list(visible))
        return " AND ".join(conds), params

    try:
        if has_emb and query_embedding is not None and query_text:
            # Case 1: UNION ALL — vector rows by cosine sim, tsvector by ts_rank.
            base_where, base_params = _base_where()
            sql = f"""
                (
                  SELECT id, fact, source, created_at,
                         {core_expr} AS is_core,
                         0 AS priority,
                         (1.0 - (embedding <=> %s::vector))::float8 AS rank
                  FROM recall_facts
                  WHERE {base_where} AND embedding IS NOT NULL
                )
                UNION ALL
                (
                  SELECT id, fact, source, created_at,
                         {core_expr} AS is_core,
                         1 AS priority,
                         COALESCE(ts_rank(to_tsvector('english', fact),
                                          plainto_tsquery('english', %s::text)), 0) AS rank
                  FROM recall_facts
                  WHERE {base_where} AND embedding IS NULL
                )
                ORDER BY is_core DESC, priority ASC, rank DESC
                LIMIT %s
            """
            params = (
                [list(_CORE_FACT_SOURCES), query_embedding] + base_params
                + [list(_CORE_FACT_SOURCES), query_text] + base_params
                + [limit]
            )
            return _db.fetch(sql, tuple(params))

        elif query_text:
            # Case 2: tsvector ranking for all rows (no embedding query).
            base_where, base_params = _base_where()
            sql = f"""
                SELECT id, fact, source, created_at,
                       {core_expr} AS is_core,
                       COALESCE(ts_rank(to_tsvector('english', fact),
                                        plainto_tsquery('english', %s::text)), 0) AS rank
                FROM recall_facts
                WHERE {base_where}
                ORDER BY is_core DESC, rank DESC, created_at DESC
                LIMIT %s
            """
            params = [list(_CORE_FACT_SOURCES), query_text] + base_params + [limit]
            return _db.fetch(sql, tuple(params))

        else:
            # Case 3: pure recency.
            base_where, base_params = _base_where()
            sql = f"""
                SELECT id, fact, source, created_at,
                       {core_expr} AS is_core,
                       0.0::float8 AS rank
                FROM recall_facts
                WHERE {base_where}
                ORDER BY is_core DESC, created_at DESC
                LIMIT %s
            """
            params = [list(_CORE_FACT_SOURCES)] + base_params + [limit]
            return _db.fetch(sql, tuple(params))

    except Exception as e:
        logger.warning("recall.store: ranked_active_facts failed: %s", e)
        return []


def _column_exists(table: str, column: str) -> bool:
    """True iff `table.column` exists in the connected DB. Used to graceful-skip
    embedding/tier ranking when the column hasn't been migrated. Never raises."""
    try:
        rows = _db.fetch(
            """
            SELECT 1 FROM information_schema.columns
            WHERE table_name = %s::text AND column_name = %s::text
            LIMIT 1
            """,
            (table, column),
        )
        return bool(rows)
    except Exception:
        return False


def search_facts(
    *,
    query: str,
    user_phone: Optional[str] = None,
    limit: int = 5,
    caller_tier: str = "stranger",
) -> list[dict]:
    """FTS over recall_facts (active only).

    caller_tier filters tier_visibility: stranger sees only stranger-visible,
    user sees stranger+user, admin sees all. Defaults to 'stranger' (fail-closed)."""
    query = (query or "").strip()
    if not query:
        return []
    limit = max(1, min(int(limit), 50))
    visible = _visible_tiers_for(caller_tier)
    if user_phone:
        return _db.fetch(
            """
            SELECT id, fact, source, created_at,
                   ts_rank(to_tsvector('english', fact), plainto_tsquery('english', %s)) AS rank
            FROM recall_facts
            WHERE superseded_at IS NULL
              AND (user_phone = %s::text OR user_phone IS NULL)
              AND tier_visibility = ANY(%s::text[])
              AND to_tsvector('english', fact) @@ plainto_tsquery('english', %s)
            ORDER BY rank DESC, created_at DESC
            LIMIT %s
            """,
            (query, user_phone, list(visible), query, limit),
        )
    return _db.fetch(
        """
        SELECT id, fact, source, user_phone, created_at,
               ts_rank(to_tsvector('english', fact), plainto_tsquery('english', %s)) AS rank
        FROM recall_facts
        WHERE superseded_at IS NULL
          AND tier_visibility = ANY(%s::text[])
          AND to_tsvector('english', fact) @@ plainto_tsquery('english', %s)
        ORDER BY rank DESC, created_at DESC
        LIMIT %s
        """,
        (query, list(visible), query, limit),
    )


def supersede_fact(fact_id: int) -> bool:
    """Soft-delete a fact."""
    rows = _db.fetch(
        "UPDATE recall_facts SET superseded_at = NOW() WHERE id = %s RETURNING id",
        (fact_id,),
    )
    return bool(rows)


# ---------------------------------------------------------------------------
# Phase 3: user profile helpers (Task 3.2)
# ---------------------------------------------------------------------------

_PROFILE_COLS = (
    "user_phone", "display_name", "nickname", "tone_override",
    "enabled_skills", "disabled_skills", "model_override", "prefs",
    "upgraded_at", "created_at", "updated_at",
)


def get_user_profile(user_phone: str) -> Optional[dict]:
    """Return the profile row as a dict, or None if not found."""
    rows = _db.fetch(
        f"SELECT {', '.join(_PROFILE_COLS)} FROM user_profiles WHERE user_phone = %s::text",
        (user_phone,),
    )
    return rows[0] if rows else None


def upsert_user_profile(
    user_phone: str,
    *,
    caller_tier: Optional[str] = None,
    **fields: Any,
) -> None:
    """Insert or update a profile row. None values are ignored.

    Allowed fields: display_name, nickname, tone_override, enabled_skills,
    disabled_skills, model_override, prefs.

    `caller_tier` is an opt-in defense-in-depth gate. When provided and not
    'admin', raises PermissionError. When None, falls through (caller is
    responsible for gating). Existing tool-layer wrappers in
    app/tools/admin_intent.py already check caller_tier; pass it here too
    for belt-and-suspenders once that wiring is updated.
    """
    if caller_tier is not None and caller_tier != "admin":
        raise PermissionError("upsert_user_profile requires caller_tier='admin'")
    allowed = {
        "display_name", "nickname", "tone_override",
        "enabled_skills", "disabled_skills", "model_override", "prefs",
    }
    _col_types = {
        "user_phone": "text", "display_name": "text", "nickname": "text",
        "tone_override": "text", "enabled_skills": "text[]",
        "disabled_skills": "text[]", "model_override": "text", "prefs": "jsonb",
    }
    updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
    cols = list(updates.keys())
    insert_cols = ["user_phone"] + cols
    placeholders = ", ".join(
        f"%s::{_col_types.get(c, 'text')}" for c in insert_cols
    )
    if cols:
        on_conflict = (
            ", ".join(f"{c} = EXCLUDED.{c}" for c in cols)
            + ", updated_at = NOW()"
        )
    else:
        on_conflict = "updated_at = NOW()"
    sql = (
        f"INSERT INTO user_profiles ({', '.join(insert_cols)}) "
        f"VALUES ({placeholders}) "
        f"ON CONFLICT (user_phone) DO UPDATE SET {on_conflict}"
    )
    _db.execute(sql, [user_phone, *updates.values()])


def promote_user(
    user_phone: str,
    nickname: Optional[str] = None,
    *,
    caller_tier: Optional[str] = None,
) -> None:
    """Flip the profile to user tier (sets upgraded_at = NOW()).

    Nickname is optional — when omitted, the existing nickname is preserved.

    `caller_tier` is an opt-in defense-in-depth gate. When provided and not
    'admin', raises PermissionError. When None, falls through.
    """
    if caller_tier is not None and caller_tier != "admin":
        raise PermissionError("promote_user requires caller_tier='admin'")
    _db.execute(
        "INSERT INTO user_profiles (user_phone, nickname, upgraded_at) "
        "VALUES (%s::text, %s::text, NOW()) "
        "ON CONFLICT (user_phone) DO UPDATE SET "
        "upgraded_at = NOW(), "
        "nickname = COALESCE(EXCLUDED.nickname, user_profiles.nickname), "
        "updated_at = NOW()",
        (user_phone, nickname),
    )


def demote_user(user_phone: str, *, caller_tier: Optional[str] = None) -> None:
    """Clear upgraded_at (tier flips to stranger). Profile data preserved.

    `caller_tier` is an opt-in defense-in-depth gate. When provided and not
    'admin', raises PermissionError. When None, falls through.
    """
    if caller_tier is not None and caller_tier != "admin":
        raise PermissionError("demote_user requires caller_tier='admin'")
    _db.execute(
        "UPDATE user_profiles SET upgraded_at = NULL, updated_at = NOW() "
        "WHERE user_phone = %s::text",
        (user_phone,),
    )



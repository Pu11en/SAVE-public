"""Platform identity consolidator — one human → many sender_ids.

Phase 3 of the elegance refactor (2026-06-07). Replaces the DREW_PHONE env
var hardcoded admin-detection. See audit-research-2026-06-07.md pattern #2.

UNIQUE on sender_id alone (not on (platform, sender_id)) per max-grill R2-F3:
E.164 phones never collide with Telegram chat IDs which never collide with
UUIDs / emails. No platform inference needed at lookup time — platform is
metadata, not part of the key.

Usage at the gateway boundary:

    from app.proxy.identity import lookup_identity
    ident = lookup_identity("7414639817")
    # ident == {"user_id": "drew", "platform": "telegram", "tier": "admin"}

Fails CLOSED on DB error: returns stranger tier so a DB outage never
escalates an unknown caller. Hard-pin env vars (DREW_PHONE,
DREW_TELEGRAM_USER_ID) stay as a separate defense in tier_gate so Drew
is never locked out even on full DB outage.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)


def _db_lookup(sender_id: str) -> Optional[tuple[str, str, str]]:
    """Return (user_id, platform, tier) or None for unknown sender. Raises on DB error.

    Uses psycopg v3 — the ONLY driver the container ships (Dockerfile installs
    `psycopg[binary]>=3.1`; there is no psycopg2). The prior psycopg2 import
    raised "No module named psycopg2" in prod, so EVERY non-hard-pinned sender
    fail-closed to stranger and platform_identity could never promote a new
    user. Mirrors app/plugins/recall/db.py's psycopg-v3 usage.

    psycopg v3 notes vs psycopg2 (why this is a real port, not just an alias):
      * `with psycopg.connect(...) as conn:` COMMITS *and CLOSES* on clean exit
        (psycopg2's `with conn` only commits, leaving the conn open). We open a
        short-lived connection per lookup, so close-on-exit is exactly right.
      * We set `conn.autocommit = True` so the best-effort last_seen_at UPDATE
        is durable on this read path and never rolled back.
      * `connect_timeout` keeps a DB stall from blocking the gateway turn.
    """
    import psycopg  # psycopg v3 — lazy import so unit tests can patch sys.modules
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("DATABASE_URL not set")
    with psycopg.connect(url, connect_timeout=5) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                "SELECT user_id, platform, tier FROM platform_identity "
                "WHERE sender_id = %s",
                (sender_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            # Best-effort bump last_seen_at
            try:
                cur.execute(
                    "UPDATE platform_identity SET last_seen_at = NOW() "
                    "WHERE sender_id = %s",
                    (sender_id,),
                )
            except Exception:
                pass
            return row[0], row[1], row[2]


def lookup_identity(sender_id: str) -> dict:
    """Consolidate sender_id → {user_id, platform, tier}.

    Fails closed: unknown sender OR DB error returns tier=stranger.
    """
    if not sender_id:
        return {"user_id": None, "platform": None, "tier": "stranger"}
    try:
        result = _db_lookup(sender_id)
    except Exception as exc:
        logger.warning("identity.lookup failed for %s: %s", sender_id, exc)
        return {"user_id": None, "platform": None, "tier": "stranger"}
    if result is None:
        return {"user_id": None, "platform": None, "tier": "stranger"}
    user_id, platform, tier = result
    return {"user_id": user_id, "platform": platform, "tier": tier}


def autoadd_unknown(sender_id: str, platform: str = "unknown") -> None:
    """Idempotently add a row for an unseen sender. Caller decides platform
    string (we don't infer). Tier defaults to stranger.

    Replaces the auto-insert side-effect that tier_gate.lookup_tier did on
    the phones table. Best-effort: silently ignores failures.
    """
    if not sender_id:
        return
    try:
        import psycopg  # psycopg v3 — the only driver the container ships
        url = os.environ.get("DATABASE_URL", "").strip()
        if not url:
            return
        with psycopg.connect(url, connect_timeout=5) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO platform_identity (user_id, platform, sender_id, tier)
                    VALUES (%s, %s, %s, 'stranger')
                    ON CONFLICT (sender_id) DO UPDATE SET last_seen_at = NOW()
                    """,
                    (f"auto-{sender_id[:8]}", platform, sender_id),
                )
    except Exception as exc:
        logger.debug("identity.autoadd_unknown failed for %s: %s", sender_id, exc)

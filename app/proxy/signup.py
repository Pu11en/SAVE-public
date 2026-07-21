"""save AI signup endpoint handler.

Wired into `app/proxy/server.py` as the POST /api/signup branch.
Lives in its own module so the proxy stays slim and the signup logic
is unit-testable without spinning up a real HTTP server.

Wire shape:
    POST /api/signup
    Headers:
        X-Signup-Token: <SAVE_SIGNUP_TOKEN env var>
        Content-Type: application/json
    Body:
        {"name": "Alex", "phone": "+15551234567", "source": "vercel-landing"}

Responses (200):
    {"ok": true, "queued_welcome": true}        — new signup, welcome fired
    {"ok": true, "duplicate": true, "queued_welcome": false}
                                                — phone already in DB, no re-welcome
Errors:
    400 {"ok": false, "error": "invalid phone"}    — validation fail
    401 {"ok": false, "error": "auth"}             — missing/bad signup token
    429 {"ok": false, "error": "rate limited"}     — per-IP cap
    500 {"ok": false, "error": "..."}              — DB failure

Welcome is fired INLINE on success. Failure to send welcome does not
fail the HTTP response — the user is signed up regardless, and the
audit log captures the BB failure so the watchdog can replay later.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import re
import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple

logger = logging.getLogger("save_ai.signup")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

# E.164: leading "+", 8-15 digits total. We additionally normalize US-shaped
# 10-digit input to "+1NNNNNNNNNN" so the landing-page client side doesn't
# have to be perfect — but Vercel side ALSO normalizes, this is defense in
# depth.
_E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")
_DIGITS_ONLY_RE = re.compile(r"[^\d]")


def normalize_phone(raw: Optional[str]) -> Optional[str]:
    """Return E.164 form, or None if not normalizable."""
    if not raw or not isinstance(raw, str):
        return None
    s = raw.strip()
    if _E164_RE.match(s):
        return s
    # Strip everything that isn't a digit
    digits = _DIGITS_ONLY_RE.sub("", s)
    if not digits:
        return None
    # US 10-digit → +1NNN
    if len(digits) == 10:
        candidate = f"+1{digits}"
    # US 11-digit starting with 1 → +1NNN
    elif len(digits) == 11 and digits.startswith("1"):
        candidate = f"+{digits}"
    # Already had a leading + but failed regex because of junk chars
    elif s.startswith("+"):
        candidate = f"+{digits}"
    else:
        return None
    return candidate if _E164_RE.match(candidate) else None


def validate_name(raw: Any) -> Optional[str]:
    """Return cleaned 1-50 char name, or None if invalid."""
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s or len(s) > 50:
        return None
    return s


# ---------------------------------------------------------------------------
# Rate limiter (in-memory per-IP rolling window)
# ---------------------------------------------------------------------------

_RATE_LOCK = threading.Lock()
_RATE_BUCKETS: Dict[str, list] = {}  # ip -> [timestamps]
RATE_WINDOW_SECS = 60.0
RATE_MAX_PER_WINDOW = 5


def _rate_limit_check(ip: str, *, now_fn: Callable[[], float] = time.time) -> bool:
    """Return True if request is allowed, False if it should be 429'd."""
    if not ip:
        ip = "unknown"
    now = now_fn()
    cutoff = now - RATE_WINDOW_SECS
    with _RATE_LOCK:
        bucket = _RATE_BUCKETS.setdefault(ip, [])
        # prune stale
        bucket[:] = [t for t in bucket if t > cutoff]
        if len(bucket) >= RATE_MAX_PER_WINDOW:
            return False
        bucket.append(now)
        return True


def _reset_rate_limiter() -> None:
    """Test hook — clears the per-IP bucket."""
    with _RATE_LOCK:
        _RATE_BUCKETS.clear()


# ---------------------------------------------------------------------------
# DB helpers (psycopg, but lazy/injectable for tests)
# ---------------------------------------------------------------------------

def _open_conn() -> Any:
    url = os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_PUBLIC_URL")
    if not url:
        raise RuntimeError("DATABASE_URL not set; signup endpoint can't persist")
    import psycopg  # type: ignore
    return psycopg.connect(url, connect_timeout=5)


def _upsert_signup(
    *,
    phone: str,
    name: str,
    source: str,
    conn_factory: Callable[[], Any] = _open_conn,
) -> Tuple[bool, Optional[str]]:
    """Insert into phones, audit it. Returns (is_new, error_message).

    is_new=True  → row inserted fresh, welcome SHOULD fire.
    is_new=False → row already existed (any tier). Welcome SHOULD NOT fire.
    """
    try:
        conn = conn_factory()
    except Exception as exc:
        return False, f"db_connect_failed: {exc}"

    try:
        with conn.cursor() as cur:
            # Did the phone exist before this insert?
            cur.execute("SELECT tier FROM phones WHERE phone = %s", (phone,))
            existing = cur.fetchone()
            if existing is not None:
                # Bump last_seen to mark interest; no tier change, no welcome.
                cur.execute(
                    "UPDATE phones SET last_seen_at = now() WHERE phone = %s",
                    (phone,),
                )
                conn.commit()
                # Audit duplicate signup attempts so we can spot abuse.
                cur.execute(
                    "INSERT INTO admin_audit (admin_phone, action, target, nonce) "
                    "VALUES (%s, %s, %s, %s)",
                    (None, "funnel_signup_duplicate", phone, source),
                )
                conn.commit()
                return False, None

            # Fresh signup: insert as user tier, audit, done.
            cur.execute(
                "INSERT INTO phones (phone, tier, added_by, last_seen_at) "
                "VALUES (%s, 'user', %s, now()) "
                "ON CONFLICT (phone) DO NOTHING",
                (phone, "funnel"),
            )
            conn.commit()
            cur.execute(
                "INSERT INTO admin_audit (admin_phone, action, target, nonce) "
                "VALUES (%s, %s, %s, %s)",
                (None, "funnel_signup", phone, source),
            )
            conn.commit()
            # Best-effort: stash the user's display name in user_profiles if
            # the table exists. Recall plugin migration creates it. Wrapped
            # in its own try so absence doesn't fail the signup.
            try:
                cur.execute(
                    "INSERT INTO user_profiles (user_phone, display_name) "
                    "VALUES (%s, %s) "
                    "ON CONFLICT (user_phone) DO UPDATE SET display_name = EXCLUDED.display_name",
                    (phone, name),
                )
                conn.commit()
            except Exception as exc:  # pragma: no cover - dev DBs may lack the table
                logger.info("[signup] user_profiles upsert skipped: %s", exc)
        return True, None
    except Exception as exc:
        return False, f"db_write_failed: {exc}"
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Top-level handler — wired from server.py
# ---------------------------------------------------------------------------

def _auth_ok(header_token: str) -> bool:
    expected = os.environ.get("SAVE_SIGNUP_TOKEN", "")
    if not expected or not header_token:
        return False
    return hmac.compare_digest(header_token, expected)


def handle_signup(
    *,
    body_bytes: bytes,
    header_token: str,
    client_ip: str,
    conn_factory: Callable[[], Any] = _open_conn,
    welcome_fn: Optional[Callable[[str, str], bool]] = None,
    now_fn: Callable[[], float] = time.time,
) -> Tuple[int, Dict[str, Any]]:
    """Pure-function signup handler. Returns (http_status, body_dict).

    All side-effecting deps (DB, BlueBubbles, time) are injectable so
    tests can run without psycopg / urllib.
    """
    # 1. Auth
    if not _auth_ok(header_token):
        return 401, {"ok": False, "error": "auth"}

    # 2. Rate-limit per IP
    if not _rate_limit_check(client_ip, now_fn=now_fn):
        return 429, {"ok": False, "error": "rate limited"}

    # 3. Parse + validate body
    try:
        payload = json.loads(body_bytes.decode("utf-8", errors="replace") or "{}")
    except json.JSONDecodeError as exc:
        return 400, {"ok": False, "error": f"invalid json: {exc}"}
    if not isinstance(payload, dict):
        return 400, {"ok": False, "error": "body must be a JSON object"}

    name = validate_name(payload.get("name"))
    if name is None:
        return 400, {"ok": False, "error": "invalid name (1-50 chars required)"}

    phone = normalize_phone(payload.get("phone"))
    if phone is None:
        return 400, {"ok": False, "error": "invalid phone (must be E.164 or US 10-digit)"}

    source = payload.get("source") or "unknown"
    if not isinstance(source, str) or len(source) > 64:
        source = "unknown"

    # 4. Persist
    is_new, db_err = _upsert_signup(
        phone=phone, name=name, source=source, conn_factory=conn_factory,
    )
    if db_err is not None:
        logger.warning("[signup] db error: %s", db_err)
        return 500, {"ok": False, "error": db_err}

    if not is_new:
        return 200, {
            "ok": True,
            "duplicate": True,
            "queued_welcome": False,
        }

    # 5. Fire welcome (inline; non-fatal on failure)
    queued = False
    if welcome_fn is None:
        from app.skills.save.welcome import send_signup_welcome as _real_welcome
        welcome_fn = _real_welcome
    try:
        queued = bool(welcome_fn(name, phone))
    except Exception as exc:
        logger.warning("[signup] welcome dispatch raised: %s", exc)
        queued = False

    return 200, {
        "ok": True,
        "queued_welcome": queued,
    }


__all__ = [
    "RATE_MAX_PER_WINDOW",
    "RATE_WINDOW_SECS",
    "handle_signup",
    "normalize_phone",
    "validate_name",
]

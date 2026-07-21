"""
Cost gate — Drew-approval wrapper for paid tools.

V1_SPEC §18.2 + §19.4. Tools flagged `requires_approval: true` route through
this gate. Each call parks a row in `approvals` (Postgres) and notifies Drew
via the supplied notifier (alerter.py / Inkbox). Drew replies `approve <NONCE>`
or `deny <NONCE>` within 5 min. The wrapped tool runs only on approval.

Nonce: 6-char Crockford base32, single-use, 5-min validity, DB-unique.
Approvals fail closed: any error → not approved → tool does not run.
"""

from __future__ import annotations

import datetime
import json
import os
import secrets
import sys
import time
import uuid
from typing import Any, Callable, Optional, Tuple

APPROVAL_TIMEOUT_S = 300  # V1_SPEC §18.2 — 5 minutes
APPROVAL_POLL_S = 2
NONCE_LEN = 6
# Crockford base32 — no I/L/O/U to avoid SMS misreads
NONCE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


class CostGateRejected(RuntimeError):
    def __init__(self, tool: str, status: str, reason: str):
        super().__init__(f"cost_gate rejected {tool}: {status} ({reason})")
        self.tool = tool
        self.status = status
        self.reason = reason


def _connect():
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL not set — cost_gate cannot park approvals")
    try:
        import psycopg  # type: ignore
    except ImportError as e:
        raise RuntimeError(f"psycopg not installed: {e}")
    return psycopg.connect(url)


def _gen_nonce() -> str:
    return "".join(secrets.choice(NONCE_ALPHABET) for _ in range(NONCE_LEN))


def _gen_id() -> str:
    return str(uuid.uuid4())


def _utcnow():
    return datetime.datetime.now(datetime.timezone.utc)


def request_approval(
    tool: str,
    cost_estimate_usd: float,
    payload: Optional[dict] = None,
    notify: Optional[Callable[[str], None]] = None,
    conn=None,
) -> Tuple[str, str]:
    """
    Park an approval row, notify Drew, return (approval_id, nonce).

    `notify(msg)` is the channel to Drew (e.g. alerter.alert or Inkbox send).
    Notify failures are logged but do not block — the row exists, and Drew
    can still see it via :status or :admin logs.
    """
    approval_id = _gen_id()
    nonce = _gen_nonce()

    own_conn = False
    if conn is None:
        conn = _connect()
        own_conn = True
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO approvals (id, tool, cost_estimate_usd, request_payload, nonce, requested_at) "
                "VALUES (%s, %s, %s, %s::jsonb, %s, now())",
                (approval_id, tool, cost_estimate_usd, json.dumps(payload or {}), nonce),
            )
            conn.commit()
    finally:
        if own_conn:
            try:
                conn.close()
            except Exception:
                pass

    msg = f"{tool} ~${cost_estimate_usd:.2f}. Approve? Reply: approve {nonce}"
    if notify is not None:
        try:
            notify(msg)
        except Exception as e:
            sys.stderr.write(f"[cost_gate] notify failed: {e}\n")
    return approval_id, nonce


def parse_approval_command(text: Any) -> Optional[Tuple[str, Optional[str], str]]:
    """
    Parse Drew's approval reply.
    Returns (decision, phone_or_None, nonce_uppercase) or None.

    Recognized:
      approve <NONCE>             → ('approved', None, NONCE)
      approve +1xxx <NONCE>       → ('approved', '+1xxx', NONCE)
      Y <NONCE> / yes <NONCE>     → ('approved', None, NONCE)
      deny <NONCE>                → ('denied',   None, NONCE)
      N <NONCE> / no <NONCE>      → ('denied',   None, NONCE)
      cancel <NONCE>              → ('denied',   None, NONCE)
    """
    if not isinstance(text, str):
        return None
    parts = text.strip().split()
    if not parts:
        return None

    head = parts[0].lower().rstrip(":,;.")
    if head in ("approve", "y", "yes", "ok"):
        decision = "approved"
    elif head in ("deny", "n", "no", "cancel", "reject"):
        decision = "denied"
    else:
        return None

    phone: Optional[str] = None
    nonce: Optional[str] = None
    if len(parts) >= 3 and parts[1].startswith("+"):
        phone = parts[1]
        nonce = parts[2]
    elif len(parts) >= 2:
        nonce = parts[1]
    else:
        return None

    if not nonce:
        return None
    nonce = nonce.upper()
    if len(nonce) != NONCE_LEN or not all(c in NONCE_ALPHABET for c in nonce):
        return None
    return decision, phone, nonce


def decide(
    nonce: str,
    decision: str,
    decided_by: str = "drew",
    conn=None,
) -> Tuple[bool, str]:
    """
    Apply a decision to a parked approval by nonce. Returns (ok, reason).
    Reason on failure ∈ {invalid_decision, missing_nonce, unknown_nonce,
    already_approved, already_denied, already_expired, expired}.
    On success reason is the approval_id.
    """
    if decision not in ("approved", "denied"):
        return False, "invalid_decision"
    if not nonce:
        return False, "missing_nonce"

    own_conn = False
    if conn is None:
        conn = _connect()
        own_conn = True
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, decision, requested_at FROM approvals WHERE nonce = %s",
                (nonce.upper(),),
            )
            row = cur.fetchone()
            if row is None:
                return False, "unknown_nonce"
            approval_id, prior, requested_at = row
            if prior is not None:
                return False, f"already_{prior}"

            tz = requested_at.tzinfo
            now = datetime.datetime.now(tz) if tz else datetime.datetime.utcnow()
            age = (now - requested_at).total_seconds()
            if age > APPROVAL_TIMEOUT_S:
                cur.execute(
                    "UPDATE approvals SET decision='expired', decided_at=now() WHERE id=%s",
                    (approval_id,),
                )
                conn.commit()
                return False, "expired"

            cur.execute(
                "UPDATE approvals SET decision=%s, decided_at=now(), decided_by=%s WHERE id=%s",
                (decision, decided_by, approval_id),
            )
            conn.commit()
            return True, str(approval_id)
    finally:
        if own_conn:
            try:
                conn.close()
            except Exception:
                pass


def get_status(
    approval_id: str,
    conn=None,
) -> Tuple[Optional[str], Optional[datetime.datetime]]:
    """
    Returns (decision, requested_at). decision is None while pending.
    """
    own_conn = False
    if conn is None:
        conn = _connect()
        own_conn = True
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT decision, requested_at FROM approvals WHERE id = %s",
                (approval_id,),
            )
            row = cur.fetchone()
            return (row[0], row[1]) if row else (None, None)
    finally:
        if own_conn:
            try:
                conn.close()
            except Exception:
                pass


def await_decision(
    approval_id: str,
    timeout_s: int = APPROVAL_TIMEOUT_S,
    poll_s: float = APPROVAL_POLL_S,
    conn_factory: Optional[Callable] = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Tuple[str, str]:
    """
    Block until the approval resolves. Returns (status, reason).
    status ∈ {'approved','denied','expired'}. Marks row expired on timeout.
    """
    factory = conn_factory or _connect
    deadline = clock() + timeout_s

    while True:
        conn = factory()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT decision FROM approvals WHERE id = %s",
                    (approval_id,),
                )
                row = cur.fetchone()
        finally:
            try:
                conn.close()
            except Exception:
                pass

        if row is None:
            return "expired", "missing_row"
        decision = row[0]
        if decision in ("approved", "denied", "expired"):
            return decision, "decided"

        if clock() >= deadline:
            conn = factory()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE approvals SET decision='expired', decided_at=now() "
                        "WHERE id=%s AND decision IS NULL",
                        (approval_id,),
                    )
                    conn.commit()
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
            return "expired", "timeout"

        sleep_fn(poll_s)


def wrap(
    tool_name: str,
    cost_estimator: Callable[..., float],
    notify: Optional[Callable[[str], None]] = None,
):
    """
    Decorator for tools flagged requires_approval: true.

        @wrap("bright_data_query", lambda payload: 0.10, notify=alerter.send)
        def bright_data_query(payload): ...

    Run flow: request_approval → await_decision → fn() if approved,
    else raise CostGateRejected.
    """
    def deco(fn):
        def wrapped(*args, **kwargs):
            try:
                cost = float(cost_estimator(*args, **kwargs))
            except Exception as e:
                raise CostGateRejected(tool_name, "denied", f"cost_estimator_failed:{e}")
            payload = {
                "args_preview": [repr(a)[:200] for a in args[:4]],
                "kwargs_keys": list(kwargs.keys())[:10],
            }
            approval_id, _ = request_approval(tool_name, cost, payload, notify=notify)
            status, reason = await_decision(approval_id)
            if status != "approved":
                raise CostGateRejected(tool_name, status, reason)
            return fn(*args, **kwargs)

        wrapped.__wrapped__ = fn
        wrapped.__name__ = fn.__name__
        wrapped.__doc__ = fn.__doc__
        return wrapped
    return deco

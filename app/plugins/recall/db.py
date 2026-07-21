"""recall.db — thin Postgres helpers.

Uses psycopg (already in requirements per Dockerfile). Connection is one
shared module-level connection — sufficient for the bot's single-process
workload. On exception we distinguish connection errors from query errors:
only reset the connection when conn.closed is True (real TCP/socket loss).
Query errors (type cast, syntax) rollback + continue on the same connection.

Schema migration runs once at boot under a Postgres advisory lock so
multi-worker Railway boot doesn't race. IF NOT EXISTS on all DDL makes it
idempotent regardless of which worker holds the lock.

Process-level backoff: _LAST_CONNECT_ATTEMPT prevents a reconnect storm
after a transient DB failure. This is PROCESS-level (shared global), not
per-user. One failure puts the process's memory offline for 30s.
"""
from __future__ import annotations

import logging
import os
import pathlib
import threading
import time
from typing import Any, Iterable, Optional

try:
    import psycopg  # noqa: F401
    _PSYCOPG_AVAILABLE = True
except Exception:
    _PSYCOPG_AVAILABLE = False

logger = logging.getLogger(__name__)

# Log message constant — soak criteria depend on this exact string.
RECALL_FETCH_ERROR = "recall: fetch failed"

_SCHEMA_PATH = pathlib.Path(__file__).parent / "schema.sql"

_CONN_LOCK = threading.Lock()
_CONN = None
_SCHEMA_DONE = False
_LAST_CONNECT_ATTEMPT: float = 0.0
_CONNECT_BACKOFF_S = 30.0
_ADVISORY_LOCK_KEY = 47832  # arbitrary stable key for recall plugin migration


def _database_url() -> Optional[str]:
    return os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_PUBLIC_URL")


def _connect() -> Any:
    if not _PSYCOPG_AVAILABLE:
        raise RuntimeError("psycopg not installed")
    url = _database_url()
    if not url:
        raise RuntimeError("DATABASE_URL not set; recall plugin can't reach Postgres")
    import psycopg
    conn = psycopg.connect(url, connect_timeout=5)
    conn.autocommit = True
    return conn


def _get_conn() -> Any:
    """Lazy module-level connection with process-level backoff."""
    global _CONN, _SCHEMA_DONE, _LAST_CONNECT_ATTEMPT
    with _CONN_LOCK:
        if _CONN is None:
            now = time.monotonic()
            if now - _LAST_CONNECT_ATTEMPT < _CONNECT_BACKOFF_S:
                raise RuntimeError(
                    f"recall: DB reconnect suppressed (backoff {_CONNECT_BACKOFF_S}s); "
                    "last attempt too recent"
                )
            _LAST_CONNECT_ATTEMPT = now
            _CONN = _connect()
        if not _SCHEMA_DONE:
            _migrate(_CONN)
            _SCHEMA_DONE = True
        return _CONN


def _migrate(conn: Any) -> None:
    """Run schema.sql under an advisory lock.

    Uses pg_try_advisory_lock — non-blocking. If another worker holds it
    (returns false), we skip migration; IF NOT EXISTS on all DDL means
    whoever runs first is sufficient. Idempotent.
    """
    if not _SCHEMA_PATH.exists():
        logger.warning("recall schema.sql missing at %s", _SCHEMA_PATH)
        return
    sql = _SCHEMA_PATH.read_text()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (_ADVISORY_LOCK_KEY,))
            row = cur.fetchone()
            got_lock = row[0] if row else False
            if not got_lock:
                logger.info(
                    "recall: schema migration skipped (another worker holds advisory lock %d)",
                    _ADVISORY_LOCK_KEY,
                )
                return
            try:
                cur.execute(sql)
                logger.info("recall: schema migration ok")
            finally:
                cur.execute("SELECT pg_advisory_unlock(%s)", (_ADVISORY_LOCK_KEY,))
    except Exception as e:
        logger.warning("recall: schema migration failed: %s", e)


def _reset_conn() -> None:
    """Close and discard the connection. Does NOT reset _SCHEMA_DONE — schema
    migration only runs once per process lifetime, not after every reconnect."""
    global _CONN
    with _CONN_LOCK:
        if _CONN is not None:
            try:
                _CONN.close()
            except Exception:
                pass
        _CONN = None


def _maybe_discard_conn(conn: Any, exc: Exception) -> None:
    """After a query error: if conn is closed, discard it. Otherwise attempt
    rollback (catches poisoned connections where closed==False but the txn
    is aborted). Discard on rollback failure."""
    global _CONN
    if getattr(conn, "closed", False):
        _reset_conn()
        return
    try:
        conn.rollback()
    except Exception:
        _reset_conn()


def execute(sql: str, args: Iterable[Any] = ()) -> None:
    """Fire a write. Distinguishes connection errors from query errors."""
    conn = None
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(sql, args)
    except Exception as e:
        logger.warning("recall: execute failed: %s", e)
        if conn is not None:
            _maybe_discard_conn(conn, e)


def fetch(sql: str, args: Iterable[Any] = ()) -> list[dict]:
    """Fire a read. Returns list of dict-ified rows."""
    conn = None
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(sql, args)
            cols = [d[0] for d in cur.description] if cur.description else []
            rows = cur.fetchall()
        return [dict(zip(cols, r)) for r in rows]
    except Exception as e:
        logger.warning("%s: %s", RECALL_FETCH_ERROR, e)
        if conn is not None:
            _maybe_discard_conn(conn, e)
        return []


def init_schema() -> None:
    """Explicitly run the idempotent schema migration.

    Called by tests and boot code that want to ensure the schema is
    applied before issuing queries. A no-op if the DB is unreachable
    (caller decides whether that is an error).
    """
    if not _PSYCOPG_AVAILABLE or not _database_url():
        return
    try:
        _get_conn()
    except Exception as e:
        logger.warning("recall: init_schema failed: %s", e)


def is_available() -> bool:
    """Cheap probe — used by handlers to fail soft if DB is down."""
    if not _PSYCOPG_AVAILABLE or not _database_url():
        return False
    try:
        conn = _get_conn()
        # Execute a lightweight read probe to catch poisoned connections.
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
        return True
    except Exception:
        return False


_HEALTH_LAST_SAVE: Optional[str] = None  # ISO timestamp set by auto-save path


def record_last_save(ts: Optional[str]) -> None:
    """Update the global last-save timestamp (called by auto-save path)."""
    global _HEALTH_LAST_SAVE
    _HEALTH_LAST_SAVE = ts


def health() -> dict:
    """Return health dict for GET /health/memory endpoint.

    Executes a SELECT 1 probe — cannot falsely report db:ok from a stale
    connection. `last_save` is global (not per-user), no user data exposed.
    """
    ok = is_available()
    return {
        "db": "ok" if ok else "down",
        "last_save": _HEALTH_LAST_SAVE,
    }

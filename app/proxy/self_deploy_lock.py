"""Self-deploy idle-gate.

Prevents the bot from self-deploying while an active task is in flight.
Uses Postgres pg_try_advisory_lock for race-free acquire/release.

Used by item 7 of the 2026-06-06 stability plan ("echo dry-run") to gate
self-modifying deploys behind a Postgres-level mutex. `pg_try_advisory_lock`
is non-blocking and session-scoped — if another caller already holds the
lock, this returns False immediately rather than queueing.

Usage:
    with acquire_self_deploy_lock(conn) as got_lock:
        if not got_lock:
            return "another deploy in flight, try again"
        run_self_deploy(...)
"""
import contextlib

SELF_DEPLOY_LOCK_KEY = 7747474747  # arbitrary 64-bit int, save AI namespace


@contextlib.contextmanager
def acquire_self_deploy_lock(conn):
    """Context manager: acquires the advisory lock; yields True if acquired,
    False if another caller holds it. Always releases on exit.

    NOTE: `pg_advisory_unlock` is only issued when we actually acquired the
    lock — calling unlock on a lock we don't hold logs a Postgres warning
    and is wasteful.
    """
    got = False
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", (SELF_DEPLOY_LOCK_KEY,))
        got = cur.fetchone()[0]
    try:
        yield got
    finally:
        if got:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (SELF_DEPLOY_LOCK_KEY,))

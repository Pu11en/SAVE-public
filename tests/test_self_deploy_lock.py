"""Tests for app.proxy.self_deploy_lock — pg_try_advisory_lock gate.

Verifies:
  - When `pg_try_advisory_lock` returns True, the context manager yields True
    and `pg_advisory_unlock` runs on exit.
  - When it returns False (another holder), the manager yields False and we
    do NOT call unlock (would log a Postgres warning).
  - On exception inside the `with` body, unlock STILL runs if we acquired.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from app.proxy.self_deploy_lock import (
    SELF_DEPLOY_LOCK_KEY,
    acquire_self_deploy_lock,
)


def _make_conn(*, lock_result: bool):
    """Build a mock conn whose cursor().fetchone() returns (lock_result,)."""
    conn = MagicMock()
    cursor = MagicMock()
    cursor.fetchone.return_value = (lock_result,)
    # cursor() is a context manager — __enter__ returns the cursor mock.
    conn.cursor.return_value.__enter__.return_value = cursor
    conn.cursor.return_value.__exit__.return_value = False
    return conn, cursor


class AcquireSelfDeployLockTest(unittest.TestCase):
    def test_acquire_yields_true_on_clean_lock(self):
        conn, cursor = _make_conn(lock_result=True)
        with acquire_self_deploy_lock(conn) as got:
            self.assertTrue(got)
        # pg_try_advisory_lock executed with the namespace key
        first_call = cursor.execute.call_args_list[0]
        self.assertIn("pg_try_advisory_lock", first_call.args[0])
        self.assertEqual(first_call.args[1], (SELF_DEPLOY_LOCK_KEY,))

    def test_acquire_yields_false_when_held(self):
        conn, cursor = _make_conn(lock_result=False)
        with acquire_self_deploy_lock(conn) as got:
            self.assertFalse(got)
        # Only one execute (the try-lock probe); NO unlock.
        execs = [c.args[0] for c in cursor.execute.call_args_list]
        self.assertEqual(len(execs), 1)
        self.assertIn("pg_try_advisory_lock", execs[0])

    def test_unlock_called_on_exit(self):
        conn, cursor = _make_conn(lock_result=True)
        with acquire_self_deploy_lock(conn):
            pass
        execs = [c.args[0] for c in cursor.execute.call_args_list]
        # Two execs: the try-lock + the unlock
        self.assertEqual(len(execs), 2)
        self.assertIn("pg_try_advisory_lock", execs[0])
        self.assertIn("pg_advisory_unlock", execs[1])
        # And the unlock used the same key
        unlock_call = cursor.execute.call_args_list[1]
        self.assertEqual(unlock_call.args[1], (SELF_DEPLOY_LOCK_KEY,))

    def test_unlock_not_called_when_acquire_failed(self):
        conn, cursor = _make_conn(lock_result=False)
        with acquire_self_deploy_lock(conn):
            pass
        # Only the try-lock; we never held the lock so we never unlock.
        execs = [c.args[0] for c in cursor.execute.call_args_list]
        self.assertEqual(len(execs), 1)
        self.assertNotIn("pg_advisory_unlock", execs[0])

    def test_unlock_runs_even_when_body_raises(self):
        """Defensive: if user code raises inside the `with`, the lock must
        still be released so future deploys aren't blocked forever."""
        conn, cursor = _make_conn(lock_result=True)
        with self.assertRaises(RuntimeError):
            with acquire_self_deploy_lock(conn) as got:
                self.assertTrue(got)
                raise RuntimeError("simulated deploy error")
        execs = [c.args[0] for c in cursor.execute.call_args_list]
        self.assertEqual(len(execs), 2)
        self.assertIn("pg_advisory_unlock", execs[1])


if __name__ == "__main__":
    unittest.main()

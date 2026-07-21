"""
Unit tests for app/tools/cost_gate.

Uses FakeConn so tests do not need psycopg or a live DB.
Integration coverage lives in agentest cost-gate-approval.sim.ts.
"""

import datetime
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.tools import cost_gate  # noqa: E402


# ---- Fake Postgres ────────────────────────────────────────────────────

class FakeCursor:
    def __init__(self, store):
        self.store = store
        self._result = None

    def execute(self, query, params=()):
        q = " ".join(query.split()).lower()
        if q.startswith("insert into approvals"):
            (approval_id, tool, cost, payload_json, nonce) = params
            self.store["rows"][approval_id] = {
                "id": approval_id,
                "tool": tool,
                "cost_estimate_usd": cost,
                "request_payload": payload_json,
                "nonce": nonce,
                "requested_at": self.store.get("clock", cost_gate._utcnow()),
                "decision": None,
                "decided_at": None,
                "decided_by": None,
            }
            self.store["by_nonce"][nonce] = approval_id
            self._result = None
        elif q.startswith("select id, decision, requested_at from approvals where nonce"):
            nonce = params[0]
            aid = self.store["by_nonce"].get(nonce)
            if aid is None:
                self._result = None
            else:
                r = self.store["rows"][aid]
                self._result = (r["id"], r["decision"], r["requested_at"])
        elif q.startswith("select decision, requested_at from approvals where id"):
            aid = params[0]
            r = self.store["rows"].get(aid)
            self._result = (r["decision"], r["requested_at"]) if r else None
        elif q.startswith("select decision from approvals where id"):
            aid = params[0]
            r = self.store["rows"].get(aid)
            self._result = (r["decision"],) if r else None
        elif q.startswith("update approvals set decision='expired'"):
            aid = params[0]
            r = self.store["rows"].get(aid)
            if r and r["decision"] is None:
                r["decision"] = "expired"
                r["decided_at"] = cost_gate._utcnow()
            self._result = None
        elif q.startswith("update approvals set decision=%s, decided_at=now(), decided_by=%s where id"):
            decision, decided_by, aid = params
            r = self.store["rows"].get(aid)
            if r:
                r["decision"] = decision
                r["decided_by"] = decided_by
                r["decided_at"] = cost_gate._utcnow()
            self._result = None
        else:
            self._result = None

    def fetchone(self):
        return self._result

    def __enter__(self): return self
    def __exit__(self, *a): return False


class FakeConn:
    def __init__(self, store):
        self.store = store
        self.commits = 0

    def cursor(self): return FakeCursor(self.store)
    def commit(self): self.commits += 1
    def close(self): pass


def make_store():
    return {"rows": {}, "by_nonce": {}}


# ---- Tests ─────────────────────────────────────────────────────────────

class TestNonce(unittest.TestCase):
    def test_length_and_alphabet(self):
        for _ in range(200):
            n = cost_gate._gen_nonce()
            self.assertEqual(len(n), cost_gate.NONCE_LEN)
            for c in n:
                self.assertIn(c, cost_gate.NONCE_ALPHABET)

    def test_no_confusing_chars(self):
        for forbidden in ("I", "L", "O", "U"):
            self.assertNotIn(forbidden, cost_gate.NONCE_ALPHABET)


class TestParseApprovalCommand(unittest.TestCase):
    def test_approve_with_nonce(self):
        self.assertEqual(
            cost_gate.parse_approval_command("approve XYZ234"),
            ("approved", None, "XYZ234"),
        )

    def test_approve_with_phone(self):
        self.assertEqual(
            cost_gate.parse_approval_command("approve +15551234567 XYZ234"),
            ("approved", "+15551234567", "XYZ234"),
        )

    def test_y_alias(self):
        self.assertEqual(
            cost_gate.parse_approval_command("Y XYZ234"),
            ("approved", None, "XYZ234"),
        )

    def test_deny(self):
        self.assertEqual(
            cost_gate.parse_approval_command("deny XYZ234"),
            ("denied", None, "XYZ234"),
        )

    def test_n_alias(self):
        self.assertEqual(
            cost_gate.parse_approval_command("N XYZ234"),
            ("denied", None, "XYZ234"),
        )

    def test_lowercase_nonce_normalized(self):
        self.assertEqual(
            cost_gate.parse_approval_command("approve xyz234"),
            ("approved", None, "XYZ234"),
        )

    def test_trailing_punctuation(self):
        self.assertEqual(
            cost_gate.parse_approval_command("approve: XYZ234"),
            ("approved", None, "XYZ234"),
        )

    def test_wrong_length_rejected(self):
        self.assertIsNone(cost_gate.parse_approval_command("approve XYZ"))
        self.assertIsNone(cost_gate.parse_approval_command("approve XYZ234EXTRA"))

    def test_bad_alphabet_rejected(self):
        # 'I' and 'L' not in Crockford alphabet
        self.assertIsNone(cost_gate.parse_approval_command("approve ILOABC"))

    def test_unknown_verb(self):
        self.assertIsNone(cost_gate.parse_approval_command("maybe XYZ234"))

    def test_empty(self):
        self.assertIsNone(cost_gate.parse_approval_command(""))
        self.assertIsNone(cost_gate.parse_approval_command(None))

    def test_no_nonce(self):
        self.assertIsNone(cost_gate.parse_approval_command("approve"))


class TestRequestApproval(unittest.TestCase):
    def test_writes_row_and_returns_ids(self):
        store = make_store()
        conn = FakeConn(store)
        captured = []
        aid, nonce = cost_gate.request_approval(
            "voyage_embed", 0.10, payload={"n": 5},
            notify=captured.append, conn=conn,
        )
        self.assertTrue(aid)
        self.assertEqual(len(nonce), cost_gate.NONCE_LEN)
        self.assertIn(aid, store["rows"])
        self.assertEqual(store["rows"][aid]["tool"], "voyage_embed")
        self.assertEqual(store["rows"][aid]["nonce"], nonce)
        self.assertEqual(len(captured), 1)
        self.assertIn(nonce, captured[0])
        self.assertIn("$0.10", captured[0])

    def test_notify_failure_does_not_block(self):
        store = make_store()
        conn = FakeConn(store)
        def boom(msg): raise RuntimeError("inkbox down")
        aid, nonce = cost_gate.request_approval(
            "tool_x", 0.05, notify=boom, conn=conn,
        )
        # Row still parked even though notify exploded
        self.assertIn(aid, store["rows"])


class TestDecide(unittest.TestCase):
    def setUp(self):
        self.store = make_store()
        self.conn = FakeConn(self.store)
        self.aid, self.nonce = cost_gate.request_approval(
            "tool_x", 0.10, conn=self.conn,
        )

    def test_approve_valid_nonce(self):
        ok, reason = cost_gate.decide(self.nonce, "approved", conn=self.conn)
        self.assertTrue(ok)
        self.assertEqual(reason, self.aid)
        self.assertEqual(self.store["rows"][self.aid]["decision"], "approved")
        self.assertEqual(self.store["rows"][self.aid]["decided_by"], "drew")

    def test_deny_valid_nonce(self):
        ok, _ = cost_gate.decide(self.nonce, "denied", conn=self.conn)
        self.assertTrue(ok)
        self.assertEqual(self.store["rows"][self.aid]["decision"], "denied")

    def test_unknown_nonce(self):
        ok, reason = cost_gate.decide("ZZZZZZ", "approved", conn=self.conn)
        self.assertFalse(ok)
        self.assertEqual(reason, "unknown_nonce")

    def test_already_decided(self):
        cost_gate.decide(self.nonce, "approved", conn=self.conn)
        ok, reason = cost_gate.decide(self.nonce, "denied", conn=self.conn)
        self.assertFalse(ok)
        self.assertEqual(reason, "already_approved")

    def test_invalid_decision(self):
        ok, reason = cost_gate.decide(self.nonce, "maybe", conn=self.conn)
        self.assertFalse(ok)
        self.assertEqual(reason, "invalid_decision")

    def test_missing_nonce(self):
        ok, reason = cost_gate.decide("", "approved", conn=self.conn)
        self.assertFalse(ok)
        self.assertEqual(reason, "missing_nonce")

    def test_expired_by_age(self):
        # Force the row to look old.
        old = cost_gate._utcnow() - datetime.timedelta(seconds=cost_gate.APPROVAL_TIMEOUT_S + 10)
        self.store["rows"][self.aid]["requested_at"] = old
        ok, reason = cost_gate.decide(self.nonce, "approved", conn=self.conn)
        self.assertFalse(ok)
        self.assertEqual(reason, "expired")
        self.assertEqual(self.store["rows"][self.aid]["decision"], "expired")


class TestAwaitDecision(unittest.TestCase):
    def test_resolves_when_decided(self):
        store = make_store()
        conn = FakeConn(store)
        aid, nonce = cost_gate.request_approval("tool_x", 0.05, conn=conn)
        cost_gate.decide(nonce, "approved", conn=conn)

        # Re-issue a fresh FakeConn factory; both calls share `store`.
        status, _ = cost_gate.await_decision(
            aid,
            conn_factory=lambda: FakeConn(store),
            sleep_fn=lambda s: None,
        )
        self.assertEqual(status, "approved")

    def test_timeout_marks_expired(self):
        store = make_store()
        conn = FakeConn(store)
        aid, _nonce = cost_gate.request_approval("tool_x", 0.05, conn=conn)

        # Stub clock to jump past the deadline on second tick.
        t = [0.0]
        def clock(): return t[0]
        def sleep(_s): t[0] += 1000  # one tick blows past 300s deadline

        status, reason = cost_gate.await_decision(
            aid,
            timeout_s=300,
            poll_s=1,
            conn_factory=lambda: FakeConn(store),
            sleep_fn=sleep,
            clock=clock,
        )
        self.assertEqual(status, "expired")
        self.assertEqual(reason, "timeout")
        self.assertEqual(store["rows"][aid]["decision"], "expired")

    def test_missing_row(self):
        store = make_store()
        status, reason = cost_gate.await_decision(
            "no-such-id",
            conn_factory=lambda: FakeConn(store),
            sleep_fn=lambda s: None,
        )
        self.assertEqual(status, "expired")
        self.assertEqual(reason, "missing_row")


class TestWrap(unittest.TestCase):
    def test_approved_call_runs(self):
        store = make_store()

        def conn_factory(): return FakeConn(store)

        # Patch _connect so request_approval uses our store
        with patch.object(cost_gate, "_connect", side_effect=conn_factory):
            @cost_gate.wrap("paid_tool", lambda *a, **k: 0.25)
            def paid_tool(x):
                return x * 2

            # Pre-flight: kick a request to capture nonce, then auto-approve
            # Wrap calls request_approval inside; we'll race it by patching
            # _connect to a thread-safe approve.
            # Simpler approach: hook on request_approval to immediately decide.
            real_request = cost_gate.request_approval

            def auto_approving_request(*args, **kwargs):
                aid, nonce = real_request(*args, **kwargs)
                cost_gate.decide(nonce, "approved", conn=FakeConn(store))
                return aid, nonce

            with patch.object(cost_gate, "request_approval", side_effect=auto_approving_request):
                result = paid_tool(7)
        self.assertEqual(result, 14)

    def test_denied_call_raises(self):
        store = make_store()

        with patch.object(cost_gate, "_connect", side_effect=lambda: FakeConn(store)):
            @cost_gate.wrap("paid_tool", lambda *a, **k: 0.25)
            def paid_tool(x):
                return x * 2

            real_request = cost_gate.request_approval

            def auto_deny(*args, **kwargs):
                aid, nonce = real_request(*args, **kwargs)
                cost_gate.decide(nonce, "denied", conn=FakeConn(store))
                return aid, nonce

            with patch.object(cost_gate, "request_approval", side_effect=auto_deny):
                with self.assertRaises(cost_gate.CostGateRejected) as cm:
                    paid_tool(7)
        self.assertEqual(cm.exception.status, "denied")
        self.assertEqual(cm.exception.tool, "paid_tool")

    def test_cost_estimator_failure_rejects(self):
        @cost_gate.wrap("paid_tool", lambda *a, **k: (_ for _ in ()).throw(ValueError("bad")))
        def paid_tool(x):
            return x * 2

        with self.assertRaises(cost_gate.CostGateRejected) as cm:
            paid_tool(7)
        self.assertEqual(cm.exception.status, "denied")
        self.assertIn("cost_estimator_failed", cm.exception.reason)


if __name__ == "__main__":
    unittest.main()

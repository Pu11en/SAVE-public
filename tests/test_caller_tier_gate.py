"""Phase 3.5 security fix: defense-in-depth tier gate on profile mutations.

C-4 from the audit: promote_user / demote_user / upsert_user_profile had no
caller-tier guard at the store layer. The admin tools (app/tools/admin_intent.py)
already check caller_tier before calling these, but defense-in-depth says the
store layer should refuse non-admin even if a new caller bypasses the tool.

This test exercises the optional `caller_tier` kwarg added to all three helpers.
Existing callers (admin_intent tools, autocreate in save_tier_injector) do not
pass caller_tier and continue to work — verified by checking they're not
invoked here.
"""
from __future__ import annotations

import pytest

from app.plugins.recall import store as _store


def test_upsert_refuses_stranger_when_caller_tier_provided():
    with pytest.raises(PermissionError):
        _store.upsert_user_profile(
            "+15550000100", display_name="x", caller_tier="stranger"
        )


def test_upsert_refuses_user_when_caller_tier_provided():
    with pytest.raises(PermissionError):
        _store.upsert_user_profile(
            "+15550000100", display_name="x", caller_tier="user"
        )


def test_upsert_allows_admin_when_caller_tier_provided(monkeypatch):
    called = {"n": 0}
    def _fake_execute(sql, params):
        called["n"] += 1
    monkeypatch.setattr(_store._db, "execute", _fake_execute)
    _store.upsert_user_profile("+15550000100", display_name="x", caller_tier="admin")
    assert called["n"] == 1


def test_upsert_allows_none_caller_tier_backcompat(monkeypatch):
    """Existing callers (autocreate) don't pass caller_tier — must still work."""
    called = {"n": 0}
    def _fake_execute(sql, params):
        called["n"] += 1
    monkeypatch.setattr(_store._db, "execute", _fake_execute)
    _store.upsert_user_profile("+15550000100", display_name="x")
    assert called["n"] == 1


def test_promote_refuses_non_admin():
    with pytest.raises(PermissionError):
        _store.promote_user("+15550000100", caller_tier="stranger")
    with pytest.raises(PermissionError):
        _store.promote_user("+15550000100", caller_tier="user")


def test_promote_allows_admin(monkeypatch):
    called = {"n": 0}
    def _fake_execute(sql, params):
        called["n"] += 1
    monkeypatch.setattr(_store._db, "execute", _fake_execute)
    _store.promote_user("+15550000100", caller_tier="admin")
    assert called["n"] == 1


def test_promote_backcompat_no_caller_tier(monkeypatch):
    called = {"n": 0}
    def _fake_execute(sql, params):
        called["n"] += 1
    monkeypatch.setattr(_store._db, "execute", _fake_execute)
    _store.promote_user("+15550000100")
    assert called["n"] == 1


def test_demote_refuses_non_admin():
    with pytest.raises(PermissionError):
        _store.demote_user("+15550000100", caller_tier="stranger")
    with pytest.raises(PermissionError):
        _store.demote_user("+15550000100", caller_tier="user")


def test_demote_allows_admin(monkeypatch):
    called = {"n": 0}
    def _fake_execute(sql, params):
        called["n"] += 1
    monkeypatch.setattr(_store._db, "execute", _fake_execute)
    _store.demote_user("+15550000100", caller_tier="admin")
    assert called["n"] == 1


def test_demote_backcompat_no_caller_tier(monkeypatch):
    called = {"n": 0}
    def _fake_execute(sql, params):
        called["n"] += 1
    monkeypatch.setattr(_store._db, "execute", _fake_execute)
    _store.demote_user("+15550000100")
    assert called["n"] == 1

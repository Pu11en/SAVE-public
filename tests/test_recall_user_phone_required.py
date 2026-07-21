"""Phase 3.5 security fix: mandatory user_phone in recall read tools + mute semantics.

C-1/C-2 from the audit: recall_search, recall_recent, recall_summarize_window,
recall_facts_search previously accepted optional user_phone. When the LLM
omitted it, the SQL had no phone filter and returned rows from every user
— a textbook cross-tenant data leak exploitable by any stranger typing the
right prompt.

Fix: handlers now REQUIRE user_phone. Tool descriptions updated to drop the
"Omit to search across all users" wording that advertised the leak.

T1 (separate spec violation): enabled_skills=[] was supposed to mean "block
all skills" per spec §Decisions-resolved #4. The code at app/prompt/assembly.py
treated [] identically to None (both dropped the override). Now [] emits an
explicit "Skills allowed: none" mute marker.
"""
from __future__ import annotations

import pytest

from app.prompt.assembly import _format_overrides


# --- T1: enabled_skills mute semantics ---

def test_enabled_skills_empty_list_emits_mute_marker():
    profile = {"enabled_skills": []}
    out = _format_overrides(profile)
    assert "Skills allowed: none" in out
    assert "all skills disabled" in out


def test_enabled_skills_none_emits_no_allowed_line():
    profile = {"enabled_skills": None}
    out = _format_overrides(profile)
    assert "Allowed skills" not in out
    assert "Skills allowed: none" not in out


def test_enabled_skills_nonempty_emits_allowed_line():
    profile = {"enabled_skills": ["save", "impeccable"]}
    out = _format_overrides(profile)
    assert "Allowed skills for this user: save, impeccable" in out
    assert "Skills allowed: none" not in out


def test_empty_list_and_none_are_distinguished():
    """Spec invariant: [] and None must produce different output."""
    none_out = _format_overrides({"enabled_skills": None})
    empty_out = _format_overrides({"enabled_skills": []})
    assert none_out != empty_out, "[] and None must not be conflated"


# --- C-1/C-2: mandatory user_phone in recall handlers ---

def _import_handlers():
    import app.plugins.recall as recall_plugin
    return recall_plugin


def test_h_search_rejects_missing_user_phone(monkeypatch):
    recall_plugin = _import_handlers()
    monkeypatch.setattr(recall_plugin._db, "is_available", lambda: True)
    result = recall_plugin.h_search({"query": "hello"})
    assert "user_phone required" in result
    assert "ok" not in result.lower() or "required" in result.lower()


def test_h_search_rejects_empty_user_phone(monkeypatch):
    recall_plugin = _import_handlers()
    monkeypatch.setattr(recall_plugin._db, "is_available", lambda: True)
    result = recall_plugin.h_search({"query": "hello", "user_phone": ""})
    assert "user_phone required" in result


def test_h_recent_rejects_missing_user_phone(monkeypatch):
    recall_plugin = _import_handlers()
    monkeypatch.setattr(recall_plugin._db, "is_available", lambda: True)
    result = recall_plugin.h_recent({})
    assert "user_phone required" in result


def test_h_summarize_window_rejects_missing_user_phone(monkeypatch):
    recall_plugin = _import_handlers()
    monkeypatch.setattr(recall_plugin._db, "is_available", lambda: True)
    result = recall_plugin.h_summarize_window({})
    assert "user_phone required" in result


def test_h_facts_search_rejects_missing_user_phone(monkeypatch):
    recall_plugin = _import_handlers()
    monkeypatch.setattr(recall_plugin._db, "is_available", lambda: True)
    result = recall_plugin.h_facts_search({"query": "test"})
    assert "user_phone required" in result


def test_h_search_passes_phone_through(monkeypatch):
    """When user_phone is provided, it should reach the store layer."""
    recall_plugin = _import_handlers()
    monkeypatch.setattr(recall_plugin._db, "is_available", lambda: True)
    captured = {"phone": None, "limit": None}
    def fake_search_messages(*, query, user_phone, limit):
        captured["phone"] = user_phone
        captured["limit"] = limit
        return []
    monkeypatch.setattr(recall_plugin._store, "search_messages", fake_search_messages)
    recall_plugin.h_search({"query": "hi", "user_phone": "+15551234567"})
    assert captured["phone"] == "+15551234567"


def test_h_recent_passes_phone_through(monkeypatch):
    recall_plugin = _import_handlers()
    monkeypatch.setattr(recall_plugin._db, "is_available", lambda: True)
    captured = {"phone": None}
    def fake_recent(*, user_phone, limit):
        captured["phone"] = user_phone
        return []
    monkeypatch.setattr(recall_plugin._store, "recent_messages", fake_recent)
    recall_plugin.h_recent({"user_phone": "+15551234567"})
    assert captured["phone"] == "+15551234567"


def test_h_facts_search_passes_phone_through(monkeypatch):
    recall_plugin = _import_handlers()
    monkeypatch.setattr(recall_plugin._db, "is_available", lambda: True)
    captured = {"phone": None}
    def fake_search_facts(*, query, user_phone, limit, caller_tier="stranger"):
        captured["phone"] = user_phone
        return []
    monkeypatch.setattr(recall_plugin._store, "search_facts", fake_search_facts)
    recall_plugin.h_facts_search({"query": "x", "user_phone": "+15551234567"})
    assert captured["phone"] == "+15551234567"


# --- Tool schema descriptions no longer advertise the cross-tenant leak ---

def test_no_read_tool_schema_says_omit_to_search_across_all_users():
    """Read tools must not advertise the cross-tenant leak. The add-facts tool
    legitimately says 'Omit for global' (write a global fact) and is exempt."""
    recall_plugin = _import_handlers()
    read_tools = {
        "recall_search", "recall_recent", "recall_summarize_window",
        "recall_facts_search",
    }
    for schema, _handler in recall_plugin._TOOLS:
        if schema["name"] not in read_tools:
            continue
        params = schema.get("parameters", {}).get("properties", {})
        up = params.get("user_phone", {})
        desc = (up.get("description") or "").lower()
        assert "omit" not in desc, f"{schema['name']} schema still says 'Omit'"
        assert "across all users" not in desc, f"{schema['name']} schema leaks"


def test_user_phone_marked_required_in_required_lists():
    """The user_phone param should be in `required` for tools that need it."""
    recall_plugin = _import_handlers()
    for schema, _handler in recall_plugin._TOOLS:
        if schema["name"] in (
            "recall_search", "recall_recent",
            "recall_summarize_window", "recall_facts_search",
        ):
            required = schema.get("parameters", {}).get("required", [])
            assert "user_phone" in required, (
                f"{schema['name']} should list user_phone in required"
            )

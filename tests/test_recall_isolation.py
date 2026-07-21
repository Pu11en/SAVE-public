"""Multi-user isolation tests for the recall_facts store.

Requires a live DATABASE_URL. Without it, all tests skip cleanly.

Verifies:
  1. User A's facts never appear in User B's ranked_active_facts.
  2. User A's facts never appear in User B's search_facts.
  3. NULL-phone global facts do NOT appear for stranger-tier callers.
  4. NULL-phone global facts DO appear for admin-tier callers.
  5. tier_visibility='admin' facts are NOT visible to stranger-tier callers.
  6. tier_visibility='user' facts are NOT visible to stranger-tier callers.
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PHONE_A = "+15559990001"
PHONE_B = "+15559990002"


def _skip_if_no_db():
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("DATABASE_URL not set")
    try:
        import psycopg  # noqa: F401
    except ImportError:
        pytest.skip("psycopg not installed")


@pytest.fixture(autouse=True)
def require_db():
    _skip_if_no_db()
    from app.plugins.recall.db import init_schema
    init_schema()


@pytest.fixture(autouse=True)
def cleanup():
    yield
    if not os.environ.get("DATABASE_URL"):
        return
    import psycopg
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM recall_facts WHERE user_phone IN (%s, %s) "
                "OR (user_phone IS NULL AND fact LIKE %s)",
                (PHONE_A, PHONE_B, "isolation-test-global%"),
            )


def _add(fact, phone, tier_visibility="stranger"):
    from app.plugins.recall import store
    return store.add_fact(
        fact=fact,
        user_phone=phone,
        source="test",
        tier_visibility=tier_visibility,
    )


def _ranked(phone, tier="stranger"):
    from app.plugins.recall import store
    return store.ranked_active_facts(
        user_phone=phone,
        caller_tier=tier,
        query_text=None,
    )


def _search(phone, query, tier="stranger"):
    from app.plugins.recall import store
    return store.search_facts(
        query=query,
        user_phone=phone,
        caller_tier=tier,
    )


# ---------------------------------------------------------------------------
# Cross-user isolation
# ---------------------------------------------------------------------------

def test_ranked_user_a_cannot_see_user_b_facts():
    _add("user A prefers coffee", PHONE_A)
    _add("user B prefers tea", PHONE_B)

    rows_a = _ranked(PHONE_A, tier="user")
    texts_a = [r["fact"] for r in rows_a]
    assert any("coffee" in t for t in texts_a), "User A should see their own fact"
    assert not any("tea" in t for t in texts_a), "User A must NOT see User B's fact"


def test_ranked_user_b_cannot_see_user_a_facts():
    _add("user A prefers coffee", PHONE_A)
    _add("user B prefers tea", PHONE_B)

    rows_b = _ranked(PHONE_B, tier="user")
    texts_b = [r["fact"] for r in rows_b]
    assert any("tea" in t for t in texts_b), "User B should see their own fact"
    assert not any("coffee" in t for t in texts_b), "User B must NOT see User A's fact"


def test_search_user_a_cannot_see_user_b_facts():
    _add("user A drinks coffee", PHONE_A)
    _add("user B drinks tea", PHONE_B)

    rows = _search(PHONE_A, "drink", tier="user")
    texts = [r["fact"] for r in rows]
    assert not any("tea" in t for t in texts), "Search must not cross user boundaries"


# ---------------------------------------------------------------------------
# NULL-phone global facts (admin-curated)
# ---------------------------------------------------------------------------

def test_global_fact_invisible_to_stranger():
    _add("isolation-test-global: bots love rain", phone=None, tier_visibility="admin")

    rows = _ranked(PHONE_A, tier="stranger")
    texts = [r["fact"] for r in rows]
    assert not any("rain" in t for t in texts), (
        "NULL-phone admin fact must NOT appear for stranger-tier callers"
    )


def test_global_fact_visible_to_admin():
    _add("isolation-test-global: bots love rain", phone=None, tier_visibility="admin")

    rows = _ranked(PHONE_A, tier="admin")
    texts = [r["fact"] for r in rows]
    assert any("rain" in t for t in texts), (
        "NULL-phone admin fact must appear for admin-tier callers"
    )


# ---------------------------------------------------------------------------
# tier_visibility fail-closed
# ---------------------------------------------------------------------------

def test_admin_fact_not_visible_to_stranger():
    _add("secret admin fact", PHONE_A, tier_visibility="admin")

    rows = _ranked(PHONE_A, tier="stranger")
    texts = [r["fact"] for r in rows]
    assert not any("secret admin fact" in t for t in texts), (
        "admin tier_visibility fact must not be visible to stranger"
    )


def test_user_fact_not_visible_to_stranger():
    _add("private user note", PHONE_A, tier_visibility="user")

    rows = _ranked(PHONE_A, tier="stranger")
    texts = [r["fact"] for r in rows]
    assert not any("private user note" in t for t in texts), (
        "user tier_visibility fact must not be visible to stranger"
    )


def test_user_fact_visible_to_user_tier():
    _add("private user note", PHONE_A, tier_visibility="user")

    rows = _ranked(PHONE_A, tier="user")
    texts = [r["fact"] for r in rows]
    assert any("private user note" in t for t in texts), (
        "user tier_visibility fact must be visible to user-tier callers"
    )

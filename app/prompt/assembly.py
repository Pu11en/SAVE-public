"""Assemble the per-turn slim prompt block.

Layers:
  1. base SOUL.md (identity + voice + R-rules)        (~7KB) -- stable prefix
  2. skill index (name + 1-line description per skill) (~0.5KB) -- stable prefix
  3. per-user overrides (from user_profiles table)     (~0.2KB) -- per-call

==============================================================================
!!! CACHE STATUS — READ BEFORE TESTING CACHE BEHAVIOR !!!

  cache_control IS NOT WIRED THROUGH THE CURRENT CODE PATH.

  build_system_blocks() returns Anthropic-style content blocks with
  cache_control={"type": "ephemeral"} on the stable prefix.
  build_system_prompt() then FLATTENS them via "\\n".join(b["text"] ...),
  which DROPS cache_control before the string ever reaches Hermes or
  MiniMax. This is intentional (Hermes' pre_llm_call hook only accepts
  string payloads as of issue #17332), but it means:

  → cache_read WILL ALWAYS BE 0 in MiniMax logs.
  → Do NOT use "cache_read > 0" as a smoke test signal.
  → The minimax_cache_passthrough plugin is the half that WOULD work
    if/when the upstream half (block-shaped payload reaching the
    provider) is built.

  build_system_blocks is forward-looking infrastructure for callers that
  construct system messages directly (e.g. a future direct-MiniMax path
  or a Hermes fork that supports block payloads).
==============================================================================

The 80% per-turn context reduction (42KB → 8.5KB) is real and independent
of cache hits — it comes from suppressing Hermes's 140-skill auto-injection,
not from prompt caching.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from app.prompt.skill_index import build_skill_index
from app.plugins.recall.store import get_user_profile

_REPO = Path(__file__).resolve().parents[2]
_SOUL_PATH = _REPO / "profile" / "SOUL.md"
_USER_MD_PATH = _REPO / "profile" / "USER.md"
_SKILLS_ROOT = _REPO / "skills"


def _format_overrides(profile: dict | None) -> str:
    if not profile:
        return ""
    lines: list[str] = []
    if profile.get("nickname"):
        lines.append(f"- User goes by: {profile['nickname']}")
    elif profile.get("display_name"):
        lines.append(f"- User is: {profile['display_name']}")
    if profile.get("tone_override"):
        lines.append(f"- Tone override: {profile['tone_override']}")
    if profile.get("disabled_skills"):
        skills = ", ".join(profile["disabled_skills"])
        lines.append(f"- Disabled skills for this user: {skills}")
    if profile.get("enabled_skills") is not None:
        if len(profile["enabled_skills"]) > 0:
            skills = ", ".join(profile["enabled_skills"])
            lines.append(f"- Allowed skills for this user: {skills}")
        else:
            lines.append("- Skills allowed: none (all skills disabled for this user)")
    if profile.get("model_override"):
        lines.append(f"- Model override: {profile['model_override']}")
    if not lines:
        return ""
    return "\n## Per-user overrides\n" + "\n".join(lines)


def _build_stable_prefix(tier: str = "stranger") -> str:
    soul = _SOUL_PATH.read_text(encoding="utf-8") if _SOUL_PATH.exists() else ""
    skill_index = build_skill_index(_SKILLS_ROOT)
    parts = [soul.strip()]
    if skill_index:
        parts.append("\n## Available skills (call by name to load full instructions)")
        parts.append(skill_index)
    # W2 L1: USER.md (Drew's profile — phone, email, build philosophy) is
    # admin-only. Non-admin callers never see it in the per-turn block.
    # Pairs with start.sh's _merge_soul stripping USER.md from
    # $HERMES_HOME/SOUL.md so the Hermes resident system prompt is also
    # public-safe.
    if tier == "admin" and _USER_MD_PATH.exists():
        user_md = _USER_MD_PATH.read_text(encoding="utf-8").strip()
        if user_md:
            parts.append("\n---\n")
            parts.append(user_md)
    return "\n".join(parts).strip() + "\n"


def build_system_blocks(user_phone: Optional[str], tier: str) -> list[dict]:
    """Return Anthropic-style content blocks with cache_control on the
    stable base+index prefix. Per-user overrides stay outside the cache
    boundary so per-call variance doesn't bust the cached prefix.

    Args:
        user_phone: Caller's E.164 phone number, or None for anonymous.
        tier: Resolved tier string (e.g. "stranger", "user", "admin").

    Returns:
        List of content block dicts. First block (stable prefix) carries
        cache_control={"type": "ephemeral"}. Second block (per-user
        overrides), when present, has no cache_control so it never
        invalidates the cached prefix.
    """
    prefix = _build_stable_prefix(tier=tier)
    blocks: list[dict] = [{
        "type": "text",
        "text": prefix,
        "cache_control": {"type": "ephemeral"},
    }]

    profile = None
    if user_phone:
        try:
            profile = get_user_profile(user_phone)
        except Exception:
            profile = None
    overrides = _format_overrides(profile)
    if overrides:
        blocks.append({"type": "text", "text": overrides})
    return blocks


def build_system_prompt(user_phone: Optional[str], tier: str) -> str:
    """Return SOUL.md + slim skill index + per-user overrides as a single string.

    Delegates to build_system_blocks and flattens the text fields. The
    cache_control markers are intentionally dropped here — this path is used
    by the save_prompt_assembly Hermes plugin which injects a plain string
    context. Block-level cache_control is only meaningful when constructing
    Anthropic API requests directly (see build_system_blocks).

    !!! cache_read will always be 0 with this code path. See module
        docstring above for the full explanation. Do not interpret
        cache_read=0 in MiniMax logs as a regression. !!!

    Args:
        user_phone: Caller's E.164 phone number, or None for anonymous.
        tier: Resolved tier string (e.g. "stranger", "user", "admin").

    Returns:
        UTF-8 string ready for injection into the user message context.
    """
    blocks = build_system_blocks(user_phone=user_phone, tier=tier)
    return "\n".join(b["text"] for b in blocks).strip() + "\n"

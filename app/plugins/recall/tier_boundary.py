"""tier_boundary — detect tier change between recent history and current turn.

Phase 4 of the elegance refactor (2026-06-07). When detected, returns a
`boundary` dict that the prompt assembler injects as a fresh system message:
"TIER CHANGE NOTICE: Caller promoted from <old> to <current>." LLM then
sees the tier shift as intentional, not as contradiction.

Pattern source: audit-research-2026-06-07.md #3 (Tetrate + OWASP instruction
hierarchy).

Per max-grill R2-F8: use SET-BASED comparison of recent_turns tiers, not
just `recent_turns[0]`. Catches heterogeneous context where the immediately
prior turn was admin but earlier turns were stranger — that mixed signal
is exactly what anchored the LLM into stranger-mode last night.
"""
from __future__ import annotations

from typing import Optional

# How many trailing turns to summarize into the boundary
MAX_SUMMARY_TURNS = 5


def detect_tier_change(
    recent_turns: list[dict],
    current_tier: str,
) -> Optional[dict]:
    """If the recent history contains ANY turn at a tier other than
    `current_tier`, return a boundary descriptor. Else return None.

    `recent_turns` is rows from conversation_history sorted DESC by
    created_at. Each row has at least `tier` and `message` keys.
    """
    if not recent_turns or not current_tier:
        return None

    # Set-based detection (R2-F8): any tier in recent that disagrees with
    # current is enough to fire — even if recent_turns[0] happens to match.
    prior_tiers = {(t.get("tier") or "").strip() for t in recent_turns}
    prior_tiers.discard("")
    prior_tiers.discard(current_tier)
    if not prior_tiers:
        return None  # all recent turns match current — no anchoring risk

    # Deterministic pick of the conflicting tier for the notice text.
    prior_tier = sorted(prior_tiers)[0]

    summary_turns = recent_turns[:MAX_SUMMARY_TURNS]
    bullets = []
    for t in reversed(summary_turns):
        msg = (t.get("message") or "").strip()[:140]
        if msg:
            bullets.append(f"  - {msg}")
    summary = "\n".join(bullets) if bullets else "  (no prior content)"

    system_message = (
        f"TIER CHANGE NOTICE: Caller has been promoted from "
        f"`{prior_tier}` to `{current_tier}`. Prior context (as "
        f"{prior_tier}):\n{summary}\n\nAll subsequent turns are at "
        f"`{current_tier}` tier — answer with the full capability set "
        f"for that tier. Do NOT continue treating the caller as "
        f"{prior_tier}."
    )
    return {
        "from_tier": prior_tier,
        "to_tier": current_tier,
        "system_message": system_message,
        "summarized_turn_count": len(summary_turns),
    }

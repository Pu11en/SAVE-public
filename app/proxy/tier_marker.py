"""
Tier marker — proxy-injected authoritative tier in the user-visible text.

V1_SPEC §18.1 calls for the LLM to honor X-Save-Tier. Hermes does not
surface arbitrary HTTP headers to the model turn context, so the proxy
prepends a marker line to the message text and the LLM reads it from
SKILL.md instructions.

Format: `[SAVE-TIER:<tier>]\\n<original text>`

Stranger spoofing is prevented by:
  1. sanitize.py strips any line matching `[SAVE-TIER:...]` from stranger
     input BEFORE the proxy prepends the real marker.
  2. The marker is prepended only by the proxy. End users never write it.
  3. SKILL.md instructs the LLM that only the FIRST line of the message
     counts as the marker, and only when the message arrives via the
     proxy path.
"""

from __future__ import annotations

import re
from typing import Optional, Tuple

VALID_TIERS = ("stranger", "user", "admin")
MARKER_PREFIX = "[SAVE-TIER:"
MARKER_SUFFIX = "]"
# Match marker anywhere a line could start, case-insensitive, allows whitespace.
MARKER_RE = re.compile(r"\[\s*SAVE[-_ ]?TIER\s*:\s*\w+\s*\]\s*", re.IGNORECASE)


def prepend(text: str, tier: str) -> str:
    """Return text with an authoritative tier marker on the first line."""
    safe_tier = tier if tier in VALID_TIERS else "stranger"
    body = text if isinstance(text, str) else ""
    return f"{MARKER_PREFIX}{safe_tier}{MARKER_SUFFIX}\n{body}"


def strip_user_markers(text: str) -> Tuple[str, bool]:
    """
    Remove any user-written tier markers. Used by sanitize on stranger input
    BEFORE the proxy injects the real one.
    Returns (cleaned_text, was_modified).
    """
    if not isinstance(text, str) or not text:
        return text or "", False
    cleaned = MARKER_RE.sub("", text)
    return cleaned, cleaned != text


def parse_marker(text: str) -> Optional[str]:
    """
    Pull the tier value out of the first marker (if any). Returns tier
    string or None. Useful for tests / agentest.
    """
    if not isinstance(text, str):
        return None
    m = MARKER_RE.search(text)
    if not m:
        return None
    inner = m.group(0)
    # Extract token between colon and closing bracket
    sub = re.search(r":\s*(\w+)\s*\]", inner, re.IGNORECASE)
    if not sub:
        return None
    tier = sub.group(1).lower()
    return tier if tier in VALID_TIERS else None

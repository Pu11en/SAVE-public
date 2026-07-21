"""output_cleanup — Hermes plugin: deterministic post-generation output cleaner.

Enforces SOUL.md R3 (no markdown) and R27 (no tech-speak) AFTER the LLM
generates a reply. Pure string ops — instant, no LLM call, zero added latency.

What it does:
  (a) Strip markdown emphasis/headers/rules/code-fences to plain text.
  (b) Rewrite unambiguous dev-jargon terms using SOUL.md's R27 mapping:
        repo/repository → project
        deploy/deployed/deploying/deployment → go live / went live / going live / launch
        API → connection
        GitHub → the project
        Railway → the system
        cron → schedule
        webhook → notification
        JSON → data
        Docker/Dockerfile → the system
        LLM → assistant
        MCP → connection
        plugin → add-on
        subagent → helper
        database / vector / embedding → memory
        endpoint → address

  Terms intentionally NOT rewritten (too common in plain English):
        model, code, server, runtime, container, push, commit, script,
        skill, scheduler — these hit everyday prose and must not be touched.

  (c) Redact bare file paths (app/..., /opt/..., /home/...) and internal
      module/plugin names (self_heal, SOUL.md, save_prompt_assembly, etc.)
      per SOUL.md R27b.

Conservative: does NOT mangle normal prose. Rewrites only whole-word
occurrences in contexts where they clearly mean the tech term.

Gate: OUTPUT_CLEANUP_ENABLED env flag, default ON (set to 0 to disable).

Hook signature: transform_llm_output — return cleaned string or None if
unchanged. Hermes uses the returned string (if not None) as the final reply.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Feature flag
# ---------------------------------------------------------------------------

def _cleanup_enabled() -> bool:
    val = os.environ.get("OUTPUT_CLEANUP_ENABLED", "1").strip().lower()
    return val not in ("0", "false", "no", "off")


# ---------------------------------------------------------------------------
# (a0) Internal tool-error / CLI leakage — DELETE entirely (never show user)
# ---------------------------------------------------------------------------
# Some raw tool errors and CLI hints occasionally leak into a reply and just
# confuse the user (they reference internal config the user can't act on). The
# canonical offender is Hermes' send_message tool when no home channel is set:
#   "No home channel set for <platform> to determine where to send the
#    message. ... or set a home channel via: hermes config set <env> <id>"
# These are meaningless to an end user, so we DELETE them outright rather than
# rewrite. Each pattern is sentence/line-bounded ([^.\n]) so it can never eat
# the rest of a real reply.
_INTERNAL_ERROR_PATTERNS = [
    re.compile(r"\bno home channel set\b[^.\n]*\.?", re.I),
    re.compile(r"\b(?:or )?set a home channel\b[^.\n]*\.?", re.I),
    re.compile(r"\bhermes config set\b[^\n]*", re.I),
]


def _strip_internal_errors(text: str) -> str:
    """Delete known internal tool-error / CLI fragments from a reply."""
    s = str(text or "")
    for pat in _INTERNAL_ERROR_PATTERNS:
        s = pat.sub("", s)
    s = re.sub(r"[ \t]{2,}", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s


# ---------------------------------------------------------------------------
# (a) Markdown stripping
# ---------------------------------------------------------------------------

def _strip_markdown(text: str) -> str:
    """Strip markdown formatting that iMessage / SMS does not render.

    Patterns stripped (in order):
      - Code fences (``` ... ```) → plain text content
      - Inline code (`code`) → plain text
      - Horizontal rules (--- / *** / ___) → removed
      - Headers (# / ## / ### etc.) → plain text (no prefix)
      - Bold/italic (**x** / __x__ / *x* / _x_) → plain text
      - Blockquotes (> ...) → plain text
      - Bullet lists (- / * / +) → Unicode bullet "• "
      - Links ([text](url)) → "text url"
      - Collapse 3+ blank lines to 2

    Newlines preserved for readability.
    """
    s = str(text or "")

    # Code fences: ```lang\n...\n``` → just the content inside
    s = re.sub(r"```[a-zA-Z0-9]*\n?", "", s)
    s = re.sub(r"```", "", s)

    # Inline code: `foo` → foo
    s = re.sub(r"`([^`\n]+)`", r"\1", s)

    # Horizontal rules: lines of only ---/***/___ (3+ chars)
    s = re.sub(r"^\s*[-*_]{3,}\s*$", "", s, flags=re.MULTILINE)

    # Headers: # Header → Header
    s = re.sub(r"^#{1,6}\s+", "", s, flags=re.MULTILINE)

    # Bold: **text** or __text__ → text
    s = re.sub(r"\*\*([^*\n]+?)\*\*", r"\1", s)
    s = re.sub(r"__([^_\n]+?)__", r"\1", s)

    # Italic: *text* or _text_ → text (don't match across newlines)
    s = re.sub(r"\*([^*\n]+?)\*", r"\1", s)
    s = re.sub(r"(?<![A-Za-z0-9])_([^_\n]+?)_(?![A-Za-z0-9])", r"\1", s)

    # Blockquotes: > text → text
    s = re.sub(r"^\s*>\s?", "", s, flags=re.MULTILINE)

    # Bullet lists → plain text (strip bullet prefix, keep content)
    s = re.sub(r"^\s*[-*+]\s+", "", s, flags=re.MULTILINE)

    # Numbered lists kept as-is (1. 2. 3. renders fine in SMS)

    # Links: [text](url) → text url
    s = re.sub(r"\[([^\]\n]+)\]\(([^)\n]+)\)", r"\1 \2", s)

    # Collapse 3+ newlines to 2
    s = re.sub(r"\n{3,}", "\n\n", s)

    return s.strip()


# ---------------------------------------------------------------------------
# (a2) Break dense multi-sentence lines for iMessage readability
# ---------------------------------------------------------------------------

def _trim_option_blocks(text: str) -> str:
    """Trim A/B/C/D option lists to 1 short line per option.

    MiniMax ignores SOUL.md's rule about short options and writes multi-line
    descriptions per option. This strips everything after the first sentence
    of each option and caps options at 70 chars so they fit in iMessage bubbles.

    Only fires when 2+ options are detected (avoids mangling numbered lists).
    """
    # Detect option prefixes: "a)" "b)" "A)" "a." "A." at line start
    option_start_re = re.compile(r"^([a-dA-D][).]\s*)", re.MULTILINE)
    starts = option_start_re.findall(text)
    if len(starts) < 2:
        return text

    # Split text into option blocks: everything from one option prefix to the next
    # (or end of string). Keep non-option content (intro lines) intact.
    block_re = re.compile(
        r"(?=^[a-dA-D][).]\s)",
        re.MULTILINE,
    )
    parts = block_re.split(text)

    out_parts: list[str] = []
    for part in parts:
        m = re.match(r"^([a-dA-D][).]\s*)(.+)", part, re.DOTALL)
        if not m:
            # Non-option content — keep as-is
            out_parts.append(part)
            continue

        prefix = m.group(1)      # "a) "
        content = m.group(2).strip()

        # Take only the first line
        first_line = content.split("\n")[0].strip()

        # Take only the first sentence from that line
        first_sent_m = re.split(r"(?<=[.!?])\s", first_line)
        first_sent = first_sent_m[0].strip() if first_sent_m else first_line

        # If content is "Label: long description...", keep only the label
        colon_m = re.match(r"^([^:]{3,40}):.*$", first_sent)
        if colon_m:
            first_sent = colon_m.group(1).strip()

        # Cap at 40 chars — enough for ~7 words, fits in one iMessage bubble
        if len(first_sent) > 40:
            first_sent = first_sent[:37].rstrip() + "..."

        out_parts.append(prefix + first_sent)

    # Rejoin with newlines between option lines
    result = "\n".join(p.rstrip() for p in out_parts if p.strip())
    return result


def _break_dense_lines(text: str) -> str:
    """Split lines that jam multiple sentences together with no breathing room.

    Targets numbered list items like "1. Do A. Do B. Do C." — splits each
    sentence onto its own line so iMessage renders them readable.
    Also splits "Risk: ... Dodge: ..." onto separate lines.
    Only fires when 2+ sentences are packed on one line.
    """
    out_lines: list[str] = []
    for line in text.split("\n"):
        stripped = line.strip()

        # "1. sentence one. sentence two." → split sentences after the label
        m = re.match(r"^(\d+\.|•)\s+(.+)$", stripped)
        if m:
            label = m.group(1)
            content = m.group(2).strip()
            sentences = re.split(r"(?<=[.!?])\s+", content)
            if len(sentences) > 1:
                out_lines.append(f"{label} {sentences[0]}")
                for s in sentences[1:]:
                    if s.strip():
                        out_lines.append(s.strip())
                out_lines.append("")
                continue

        # "Risk: ... Dodge: ..." on one line → split at Dodge:
        if re.search(r"\bDodge:", stripped, re.I):
            parts = re.split(r"(?=\bDodge:)", stripped, flags=re.I)
            for p in parts:
                if p.strip():
                    out_lines.append(p.strip())
            out_lines.append("")
            continue

        out_lines.append(line)

    result = "\n".join(out_lines)
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result


# ---------------------------------------------------------------------------
# (a3) Ensure blank lines between thoughts for iMessage readability
# ---------------------------------------------------------------------------

def _add_paragraph_breaks(text: str) -> str:
    """Promote single newlines between non-empty lines to double newlines.

    iMessage renders \n as a line break and \n\n as a visible blank line.
    The model often writes multi-thought replies with only single newlines,
    making them look cramped. This ensures every logical line gets breathing
    room without touching single-line replies.
    """
    lines = text.split("\n")
    if len(lines) <= 2:
        return text

    out: list[str] = []
    for i, line in enumerate(lines):
        out.append(line)
        # Between two non-empty lines that aren't already separated by a blank,
        # insert a blank line so iMessage shows a gap between thoughts.
        if (
            line.strip()
            and i + 1 < len(lines)
            and lines[i + 1].strip()
        ):
            out.append("")
    result = "\n".join(out)
    # Collapse anything that became 3+ blank lines back to 2
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result


# ---------------------------------------------------------------------------
# (b) R27 tech-term rewrites
# ---------------------------------------------------------------------------
# Each entry: (compiled pattern, replacement).
# Patterns use word boundaries (\b) to avoid mangling prose like "deployed" in
# "the troops were deployed". Where context matters (e.g. "deploy" as a verb
# clearly meaning software deployment) we match on common verb forms.
# IMPORTANT: order matters — more specific patterns first.

_R27_REWRITES: list[tuple[re.Pattern, str]] = [
    # Internal names (R27b) — redact completely
    (re.compile(r"\bself[_-]heal(?:_propose|_execute|_deploy|_railway[_a-z]*)?\b", re.I), "[internal]"),
    (re.compile(r"\bSOUL\.md\b", re.I), "[internal]"),
    (re.compile(r"\bSKILL\.md\b", re.I), "[internal]"),
    (re.compile(r"\bsave_prompt_assembly\b", re.I), "[internal]"),
    (re.compile(r"\bsave_tier_injector\b", re.I), "[internal]"),
    (re.compile(r"\btier[_-]gate\b", re.I), "[internal]"),
    (re.compile(r"\btool[_-]call[_-]logger\b", re.I), "[internal]"),
    (re.compile(r"\brecall[_-](?:search|facts|recent|last|summarize|subagent|guard)\b", re.I), "[internal]"),
    (re.compile(r"\bminimax[_-]cache[_-]passthrough\b", re.I), "[internal]"),
    (re.compile(r"\bsave_jit_skill_loader\b", re.I), "[internal]"),
    (re.compile(r"\bhermes[_-]agent\b", re.I), "[internal]"),
    (re.compile(r"\binkbox\b", re.I), "[internal]"),
    (re.compile(r"\bbluebubbles\b", re.I), "[internal]"),
    (re.compile(r"\bweb[_-]extract\b", re.I), "[internal]"),
    (re.compile(r"\bregister[_-]tool\b", re.I), "[internal]"),
    (re.compile(r"\bpropose[_-](?:modify|delete|skill|railway)\b", re.I), "[internal]"),

    # File paths: redact relative app/... or absolute /opt/... /home/... paths
    # These are handled separately in _redact_paths, but add as fallback here.

    # R27 tech terms → plain English rewrites
    # Only unambiguous dev-jargon terms that would never appear in normal prose
    # with their tech meaning being incidental.

    # Railway / Docker / infra
    (re.compile(r"\bRailway\b"), "the system"),
    (re.compile(r"\bDocker(?:file)?\b", re.I), "the system"),
    # NOTE: container, runtime, server intentionally omitted — too common in plain English.

    # GitHub
    (re.compile(r"\bGitHub\b"), "the project"),
    (re.compile(r"\brepo(?:sitory|sitories)?\b", re.I), "project"),
    # NOTE: commit, push intentionally omitted — everyday English words.

    # Deploy variants — unambiguous in software context
    (re.compile(r"\bdeploy(?:ed|ing|ment|s)?\b", re.I), lambda m: {
        "deploy": "go live",
        "deployed": "went live",
        "deploying": "going live",
        "deployment": "launch",
        "deploys": "goes live",
    }.get(m.group(0).lower(), "go live")),

    # API / endpoint / webhook
    (re.compile(r"\bAPI\b"), "connection"),
    (re.compile(r"\bendpoint\b", re.I), "address"),
    (re.compile(r"\bwebhook\b", re.I), "notification"),

    # JSON — unambiguous acronym
    (re.compile(r"\bJSON\b"), "data"),
    # NOTE: code, script intentionally omitted — common English words.

    # LLM / MCP — unambiguous acronyms
    (re.compile(r"\bLLM\b"), "assistant"),
    (re.compile(r"\bMiniMax(?:-M3)?\b", re.I), "assistant"),
    (re.compile(r"\bMCP\b"), "connection"),

    # Memory-related tech terms
    (re.compile(r"\bembedding\b", re.I), "memory"),
    (re.compile(r"\bvector\b", re.I), "memory"),
    (re.compile(r"\bdatabase\b", re.I), "memory"),

    # Architecture terms
    (re.compile(r"\bplugin\b", re.I), "add-on"),
    # NOTE: skill, model, scheduler intentionally omitted — common English words.
    (re.compile(r"\bHermes\b"), "the system"),
    (re.compile(r"\bsubagent\b", re.I), "helper"),
    (re.compile(r"\bcron\b", re.I), "schedule"),
    (re.compile(r"\bOAuth\b", re.I), "login"),

    # ALL_CAPS_SNAKE_CASE env var names — must come before other rewrites
    # Matches names like RAILWAY_GIT_COMMIT_SHA, DREW_PHONE, BLUEBUBBLES_PASSWORD
    (re.compile(r"\b[A-Z][A-Z0-9]{2,}(?:_[A-Z0-9]+){1,}\b"), "[setting]"),

    # Dev version / git terms
    (re.compile(r"\bSHA\b"), "version"),
    (re.compile(r"\bcommit hash(?:es)?\b", re.I), "version"),
    (re.compile(r"\bgit (?:SHA|hash|commit)\b", re.I), "version"),

    # Environment variables
    (re.compile(r"\benv(?:ironment)? vars?\b", re.I), "settings"),
    (re.compile(r"\benvironment variables?\b", re.I), "settings"),

    # Plan/Trigger structural labels leaked from self_heal (strip the label)
    # These appear as labeled fields in bot planning output; strip wherever they appear.
    # e.g. "Plan: new status skill. Trigger: text 'status'" → "New status skill. When you say 'status'"
    (re.compile(r"\bPlan:\s*", re.I), ""),
    (re.compile(r"\bTrigger:\s*", re.I), "When you say "),
    (re.compile(r"\bGoal:\s*", re.I), ""),
    (re.compile(r"\bApproach:\s*", re.I), ""),
    (re.compile(r"\bImplementation:\s*", re.I), ""),

    # URL/port references
    (re.compile(r"\blocalhost:[0-9]+\b"), "[address]"),
    (re.compile(r"\bport [0-9]+\b", re.I), "[address]"),

    # git as noun ("git repo", "git push", "git commit" etc.)
    (re.compile(r"\bgit\b(?=\s+(?:repo|commit|push|pull|branch|log|merge|SHA|hash))", re.I), "project"),

    # URLs and HTTP
    (re.compile(r"\bhttps?://[^\s\"')\]]+"), "[link]"),     # full URL → [link]
    (re.compile(r"\bURL\b"), "link"),
    (re.compile(r"\bHTTPS?\b"), "secure connection"),
    (re.compile(r"\bstatus code\s+\d{3}\b", re.I), "error"),

    # Timeouts and failures
    (re.compile(r"\btimed?\s*out\b", re.I), "took too long"),
    (re.compile(r"\btimeout\b", re.I), "time limit"),
    (re.compile(r"\bretry(?:ing|ied)?\b", re.I), "trying again"),

    # Auth and credentials
    (re.compile(r"\bauth(?:entication|orization|orize|enticate)?\b", re.I), "login"),
    (re.compile(r"\bcredentials?\b", re.I), "login details"),
    (re.compile(r"\btoken(?:s)?\b(?!\s+of)", re.I), "session key"),  # "token of appreciation" untouched

    # Config and infra terms
    (re.compile(r"\bconfig(?:uration)?\b", re.I), "settings"),
    (re.compile(r"\bYAML\b"), "settings file"),
    (re.compile(r"\bCLI\b"), "the tool"),
    (re.compile(r"\basync(?:hronous(?:ly)?)?\b", re.I), "in the background"),
    (re.compile(r"\bsocket\b", re.I), "connection"),

    # Plain-English vocabulary simplification — safe replacements only.
    # Formal → casual: only safe noun/adverb replacements where a single
    # fixed replacement is always grammatically correct.
    (re.compile(r"\bSubsequently\b"), "Then"),      # sentence-start capital
    (re.compile(r"\bsubsequently\b"), "then"),
    (re.compile(r"\bHowever\b"), "But"),
    (re.compile(r"\bhowever\b"), "but"),
    (re.compile(r"\bTherefore\b"), "So"),
    (re.compile(r"\btherefore\b"), "so"),
    (re.compile(r"\bAdditionally\b"), "Also"),
    (re.compile(r"\badditionally\b"), "also"),
    (re.compile(r"\bEssentially\b"), "Basically"),
    (re.compile(r"\bessentially\b"), "basically"),
    (re.compile(r"\bnumerous\b", re.I), "a lot of"),
    (re.compile(r"\bin order to\b", re.I), "to"),
    (re.compile(r"\bat this point in time\b", re.I), "right now"),
    (re.compile(r"\bit is important to note(?: that)?\b", re.I), "note:"),
    (re.compile(r"\butilize\b", re.I), "use"),
    (re.compile(r"\butilized\b", re.I), "used"),
    (re.compile(r"\butilizes\b", re.I), "uses"),
    (re.compile(r"\butilizing\b", re.I), "using"),
    (re.compile(r"\bleverage\b", re.I), "use"),
    (re.compile(r"\bleveraged\b", re.I), "used"),
    (re.compile(r"\bleverages\b", re.I), "uses"),
    (re.compile(r"\bleveraging\b", re.I), "using"),
    # NOTE: "functionality" removed — "how it works" is a clause, not a noun,
    # so "the functionality you need" → "the how it works you need" is broken.
    # Let SOUL.md handle this instead.
    (re.compile(r"\bimplemented\b", re.I), "built"),
    (re.compile(r"\bimplementing\b", re.I), "building"),
    (re.compile(r"\binitialization\b", re.I), "startup"),
    (re.compile(r"\bsufficiently\b", re.I), "well enough"),
]


def _apply_r27_rewrites(text: str) -> str:
    """Apply R27 tech-term rewrites. Conservative — whole-word only."""
    for pattern, replacement in _R27_REWRITES:
        if callable(replacement):
            text = pattern.sub(replacement, text)
        else:
            text = pattern.sub(replacement, text)
    return text


# ---------------------------------------------------------------------------
# (c) Path and internal-name redaction
# ---------------------------------------------------------------------------

# File path patterns: app/foo/bar.py, /opt/..., /home/...
_PATH_PATTERNS = [
    re.compile(r"\bapp/[a-zA-Z0-9_./\-]+\.[a-zA-Z]{1,6}\b"),
    re.compile(r"/opt/[a-zA-Z0-9_./\-]+"),
    re.compile(r"/home/[a-zA-Z0-9_./\-]+"),
    re.compile(r"/var/[a-zA-Z0-9_./\-]+"),
    re.compile(r"/tmp/[a-zA-Z0-9_./\-]+"),
]

# Internal module/plugin names that should never appear in user-facing output
_INTERNAL_NAMES = [
    "self_heal", "SOUL.md", "save_prompt_assembly", "save_tier_injector",
    "tier_gate", "tool_call_logger", "minimax_cache_passthrough",
    "save_jit_skill_loader", "hermes-agent", "hermes_agent",
    "bluebubbles", "inkbox", "web_extract", "register_tool",
    "platform_identity", "local_log", "pending_responses",
    "generated_awaiting_preview", "quiet_hours_blocked",
    "plugin.yaml", "SKILL.md",
]
_INTERNAL_NAME_RE = re.compile(
    r"\b(" + "|".join(re.escape(n) for n in _INTERNAL_NAMES) + r")\b",
    re.I,
)


def _redact_paths_and_internals(text: str) -> str:
    """Redact bare file paths and internal module names."""
    for pat in _PATH_PATTERNS:
        text = pat.sub("[path]", text)
    text = _INTERNAL_NAME_RE.sub("[internal]", text)
    return text


# ---------------------------------------------------------------------------
# Collapse multiple adjacent [internal] / [path] markers
# ---------------------------------------------------------------------------

def _collapse_markers(text: str) -> str:
    """Collapse runs of adjacent redaction markers into a single marker."""
    text = re.sub(r"(\[internal\]\s*){2,}", "[internal] ", text)
    text = re.sub(r"(\[path\]\s*){2,}", "[path] ", text)
    return text


# ---------------------------------------------------------------------------
# Main cleaner
# ---------------------------------------------------------------------------

def clean(response_text: str) -> Optional[str]:
    """Run all cleanup passes. Returns cleaned text if anything changed,
    or None if the text was already clean (so Hermes can skip the copy).

    This function is pure string ops — NO I/O, NO LLM calls, instant.
    """
    if not response_text:
        return None

    original = response_text
    s = response_text

    s = _strip_internal_errors(s)
    s = _strip_markdown(s)
    s = _trim_option_blocks(s)
    s = _break_dense_lines(s)
    s = _add_paragraph_breaks(s)
    s = _apply_r27_rewrites(s)
    s = _redact_paths_and_internals(s)
    s = _collapse_markers(s)
    s = s.strip()

    # Guard: if cleanup emptied a non-empty reply (e.g. the whole message was
    # an internal error), keep the original rather than send a blank message.
    if not s and original.strip():
        return None

    if s == original.strip():
        return None
    return s


# ---------------------------------------------------------------------------
# Hermes hook
# ---------------------------------------------------------------------------

def _capture_reply(text: str) -> None:
    """Write reply to test-reply-buffer.jsonl so /last-reply works."""
    import json as _json, os as _os, time as _time
    from pathlib import Path as _Path
    try:
        buf = _Path(_os.environ.get("HERMES_HOME", "/opt/data")) / "test-reply-buffer.jsonl"
        entries: list = []
        if buf.exists():
            for _ln in buf.read_text(encoding="utf-8").splitlines():
                try:
                    entries.append(_json.loads(_ln))
                except Exception:
                    pass
        entries.append({"ts": _time.time(), "content": text})
        entries = entries[-100:]
        buf.write_text("\n".join(_json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    except Exception as _e:
        logger.warning("output_cleanup: reply capture failed: %s", _e)


def on_transform(
    *,
    response_text: str,
    session_id: str = "",
    platform: str = "",
    **_,
) -> Optional[str]:
    """transform_llm_output hook. Called by Hermes after every LLM generation.

    Returns the cleaned string, or None if unchanged (Hermes keeps original).
    Fully defensive — any exception falls back to None (no change) so a bug
    here can never block a reply.
    """
    if not _cleanup_enabled():
        # Capture raw (cleanup disabled) so /last-reply still works.
        _capture_reply(response_text)
        return None
    try:
        cleaned = clean(response_text)
        # Capture what the user actually receives (post-cleanup).
        _capture_reply(cleaned if cleaned is not None else response_text)
        return cleaned
    except Exception as exc:
        logger.warning("output_cleanup: clean() raised: %s", exc)
        _capture_reply(response_text)
        return None


def register(ctx) -> None:
    """Register the transform_llm_output hook with Hermes."""
    ctx.register_hook("transform_llm_output", on_transform)
    logger.info("output_cleanup: registered transform_llm_output hook")

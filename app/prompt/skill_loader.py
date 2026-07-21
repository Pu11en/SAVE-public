"""Just-in-time loader for skill bodies.

When the model decides to invoke a skill by name, this returns the SKILL.md
body (without frontmatter). Cache lives for one HTTP request lifecycle to
keep multi-turn ReAct loops from re-reading disk.
"""
from __future__ import annotations

import re
from pathlib import Path

_FRONTMATTER_RE = re.compile(r"^---\n.*?\n---\n?", re.DOTALL)


class SkillNotFound(LookupError):
    pass


def load_skill_body(
    name: str,
    skills_root: Path,
    cache: dict[str, str] | None = None,
) -> str:
    if cache is not None and name in cache:
        return cache[name]
    skill_md = skills_root / name / "SKILL.md"
    if not skill_md.is_file():
        raise SkillNotFound(name)
    text = skill_md.read_text(encoding="utf-8")
    body = _FRONTMATTER_RE.sub("", text, count=1).lstrip("\n")
    if cache is not None:
        cache[name] = body
    return body

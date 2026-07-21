"""Build a one-liner skill index from SKILL.md frontmatter.

Replaces eager full-SKILL.md injection. The model sees the skill name +
one-line description from frontmatter; when it decides to use a skill,
the JIT loader (see app/prompt/skill_loader.py — Task 1.3) reads the full body.
"""
from __future__ import annotations

import re
from pathlib import Path

_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---", re.DOTALL)


def _parse_frontmatter(md_text: str) -> dict[str, str]:
    m = _FRONTMATTER_RE.match(md_text)
    if not m:
        return {}
    out: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        out[k.strip()] = v.strip()
    return out


def build_skill_index(skills_root: Path) -> str:
    """Return `name: description` lines, one per skill, sorted by name."""
    if not skills_root.exists():
        return ""
    entries: list[tuple[str, str]] = []
    for skill_dir in sorted(skills_root.iterdir()):
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.is_file():
            continue
        fm = _parse_frontmatter(skill_md.read_text(encoding="utf-8"))
        name = fm.get("name") or skill_dir.name
        desc = fm.get("description") or "(no description)"
        entries.append((name, desc))
    entries.sort(key=lambda x: x[0])
    return "\n".join(f"{n}: {d}" for n, d in entries)

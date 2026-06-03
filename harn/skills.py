"""Skill discovery: each skill is a SKILL.md with YAML-ish frontmatter.

Only the lightweight index (name + description) is meant to live in the agent's
context at all times; the full body is loaded on demand via read_skill().
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


@dataclass
class Skill:
    name: str
    description: str
    path: Path

    def body(self) -> str:
        text = self.path.read_text(encoding="utf-8", errors="replace")
        return _FM_RE.sub("", text, count=1).strip()


def _frontmatter(text: str) -> dict:
    m = _FM_RE.match(text)
    fm: dict = {}
    if not m:
        return fm
    for line in m.group(1).splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            fm[key.strip().lower()] = val.strip()
    return fm


def discover(env_dir: Path) -> list[Skill]:
    skills_dir = env_dir / "skills"
    if not skills_dir.exists():
        return []
    out: list[Skill] = []
    for skill_md in sorted(skills_dir.glob("*/SKILL.md")):
        text = skill_md.read_text(encoding="utf-8", errors="replace")
        fm = _frontmatter(text)
        out.append(
            Skill(
                name=fm.get("name", skill_md.parent.name),
                description=fm.get("description", ""),
                path=skill_md,
            )
        )
    return out


def index(env_dir: Path) -> str:
    """A compact, token-cheap listing the agent can scan to decide what to load."""
    lines = [f"- {s.name}: {s.description}" for s in discover(env_dir)]
    return "\n".join(lines) if lines else "(no skills installed)"


def read_skill(env_dir: Path, name: str) -> str | None:
    for s in discover(env_dir):
        if s.name == name:
            return s.body()
    return None

"""Skill discovery: each skill is a SKILL.md with YAML-ish frontmatter.

Only the lightweight index (name + description) is meant to live in the agent's
context at all times; the full body is loaded on demand via read_skill().
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from . import tasks as _tasks

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


_LEARNED_HEADING = "## Learned (captured from the team)"


def append_learning(env_dir: Path, name: str, content: str,
                    description: str = "") -> Path:
    """Append a learned fact to a skill, creating the skill if it doesn't exist.

    This is how harn accumulates project knowledge: an answer or a discovered
    convention is saved into the matching skill so future agents read it instead
    of asking again. Returns the SKILL.md path.

    Locked (`_claim_lock`) around the FULL read-modify-write cycle (create-if-
    absent, read current body, append, write back) — two parallel-wave agents
    calling this for the same skill at the same instant would otherwise both
    read the same pre-append body and the second write would silently drop the
    first agent's entry. This is new risk as of Phase 3 (multiple real agent
    processes, not just one).
    """
    name = name.strip().lower().replace(" ", "-")
    content = content.strip()
    skill_dir = env_dir / "skills" / name
    md = skill_dir / "SKILL.md"
    with _tasks._claim_lock(env_dir):
        if not md.exists():
            skill_dir.mkdir(parents=True, exist_ok=True)
            desc = description.strip() or f"{name} standards and conventions for this project."
            md.write_text(
                f"---\nname: {name}\ndescription: {desc}\n---\n\n# {name}\n",
                encoding="utf-8",
            )
        text = md.read_text(encoding="utf-8", errors="replace").rstrip()
        entry = f"- {content}"
        if _LEARNED_HEADING in text:
            text += "\n" + entry + "\n"
        else:
            text += f"\n\n{_LEARNED_HEADING}\n{entry}\n"
        md.write_text(text, encoding="utf-8")
    return md


def write_skill_body(env_dir: Path, name: str, body: str,
                     description: str | None = None) -> Path:
    """Replace a skill's BODY (everything after the frontmatter), keeping its
    name/description frontmatter (or setting description if given). Creates the
    skill if missing. Used by the visual editor to save hand-edited skills."""
    name = name.strip().lower().replace(" ", "-")
    skill_dir = env_dir / "skills" / name
    md = skill_dir / "SKILL.md"
    existing_desc = ""
    if md.exists():
        existing_desc = _frontmatter(
            md.read_text(encoding="utf-8", errors="replace")).get("description", "")
    desc = (description if description is not None else existing_desc) or \
        f"{name} standards and conventions for this project."
    skill_dir.mkdir(parents=True, exist_ok=True)
    md.write_text(
        f"---\nname: {name}\ndescription: {desc.strip()}\n---\n\n{body.strip()}\n",
        encoding="utf-8",
    )
    return md

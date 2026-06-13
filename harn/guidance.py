"""On-demand guidance — situational protocol detail in ``harn_env/guidance/``.

The always-on core (AGENTS.md) carries only the hard, universal rules plus an
index of these topics. The agent pulls a topic body ONLY when the task is in
that situation (parallel work, UI design, browser verification, …) — progressive
disclosure that keeps the per-session context small.

Mirrors skills/services: a token-cheap index (topic + one-line summary), bodies
read on demand.
"""
from __future__ import annotations

import re
from pathlib import Path

DIRNAME = "guidance"
_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _dir(env_dir: Path) -> Path:
    return env_dir / DIRNAME


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "-", name.strip().lower()).strip("-")


def _frontmatter(text: str) -> dict:
    m = _FM_RE.match(text)
    fm: dict = {}
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                fm[k.strip().lower()] = v.strip()
    return fm


def list_topics(env_dir: Path) -> list[tuple[str, str]]:
    """[(topic, one-line summary)] for every guidance file."""
    d = _dir(env_dir)
    if not d.exists():
        return []
    out: list[tuple[str, str]] = []
    for p in sorted(d.glob("*.md")):
        fm = _frontmatter(p.read_text(encoding="utf-8", errors="replace"))
        out.append((fm.get("topic", p.stem), fm.get("summary", "")))
    return out


def index(env_dir: Path) -> str:
    lines = [f"- {t}: {s}" if s else f"- {t}" for t, s in list_topics(env_dir)]
    return "\n".join(lines) if lines else "(no guidance topics installed)"


def read(env_dir: Path, topic: str) -> str | None:
    """Body of one guidance topic (frontmatter stripped), or None if missing."""
    p = _dir(env_dir) / f"{_slug(topic)}.md"
    if not p.exists():
        return None
    return _FM_RE.sub("", p.read_text(encoding="utf-8", errors="replace"), count=1).strip()

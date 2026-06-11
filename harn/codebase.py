"""Codebase map — persistent AS-IS knowledge in ``harn_env/CODEBASE.md``.

One markdown file describing what the project IS: services and their
responsibilities, the stack, entry points, data flow, and the standards already
established in the code. The agent reads it at task pickup instead of
re-indexing/re-discovering the repo every session — fewer tokens, faster start,
and the AS-IS step of the pre-task protocol comes pre-answered.

Maintained by the agent: onboarding creates it, and the post-task reconcile
step keeps it current as the code evolves.
"""
from __future__ import annotations

from pathlib import Path

FILENAME = "CODEBASE.md"

TEMPLATE = """\
# Codebase map

> Maintained by the agent (update via `update_codebase_map` whenever the
> structure, stack, or responsibilities change). Keep it compact and factual —
> this file is loaded at every task pickup.

## Stack
- (languages, frameworks, build tools, test runner — with versions where pinned)

## Services / modules and responsibilities
- `<path>` — (single-sentence responsibility; key entry points)

## Data flow & storage
- (where state lives, how data moves between modules, external APIs)

## Established standards in the code
- (conventions already followed: error shape, naming, file layout, patterns —
   things a new change MUST stay consistent with)

## Gotchas
- (non-obvious constraints discovered while working here)
"""


def path(env_dir: Path) -> Path:
    return env_dir / FILENAME


def read(env_dir: Path) -> str | None:
    p = path(env_dir)
    if not p.exists():
        return None
    text = p.read_text(encoding="utf-8", errors="replace").strip()
    return text or None


def save(env_dir: Path, content: str) -> Path:
    env_dir.mkdir(parents=True, exist_ok=True)
    p = path(env_dir)
    p.write_text(content.rstrip() + "\n", encoding="utf-8")
    return p


def prompt_note(env_dir: Path, limit: int = 4000) -> str:
    """Injection block for task prompts: the map if present (head-truncated),
    or an instruction to create it."""
    text = read(env_dir)
    if text is None:
        return (
            "## Codebase map — MISSING\n"
            "`harn_env/CODEBASE.md` doesn't exist yet. After you've explored "
            "the code for this task, call `update_codebase_map` with a compact "
            "map (stack, services + responsibilities, data flow, established "
            "standards, gotchas) so future tasks skip re-discovery."
        )
    if len(text) > limit:
        text = text[:limit] + "\n…(truncated — read harn_env/CODEBASE.md for the rest)"
    return (
        "## Codebase map (AS IS — read this INSTEAD of re-indexing the repo)\n"
        + text
        + "\n\nIf this map is stale or missing something you discover, update "
          "it via `update_codebase_map` at the end of your work."
    )

"""Service registry — per-service knowledge in ``harn_env/services/<name>.md``.

One file per service/module/project describing its RESPONSIBILITY, STANDARDS,
and CONSTRAINTS — not the code itself (the code describes the code; these files
answer "do we even need this service for the current task, and what must any
change here respect?").

The pattern mirrors skills: a token-cheap index (name + one-line responsibility)
is injected at task pickup; the agent reads a service file ONLY when the task
touches it. Maintained by the agent: onboarding seeds it, the post-task
reconcile step keeps it current.

A legacy single-file ``harn_env/CODEBASE.md`` (pre-0.9) is still surfaced with
a hint to split it into services.
"""
from __future__ import annotations

import re
from pathlib import Path

DIRNAME = "services"
LEGACY_FILENAME = "CODEBASE.md"

_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)

TEMPLATE = """\
## Responsibility
- (single paragraph: what this service owns and does NOT own — enough to decide
   whether a task touches it at all)

## Standards
- (conventions any change here MUST follow: error shape, naming, layering,
   patterns already established in this service's code)

## Constraints
- (hard limits: performance budgets, compatibility promises, do-not-touch
   areas, security boundaries, external contracts)

## Gotchas
- (non-obvious traps discovered while working here)
"""


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


def list_services(env_dir: Path) -> list[tuple[str, str]]:
    """[(name, one-line responsibility)] for every registered service."""
    d = _dir(env_dir)
    if not d.exists():
        return []
    out: list[tuple[str, str]] = []
    for p in sorted(d.glob("*.md")):
        fm = _frontmatter(p.read_text(encoding="utf-8", errors="replace"))
        out.append((fm.get("name", p.stem), fm.get("responsibility", "")))
    return out


def index(env_dir: Path) -> str:
    """Token-cheap listing the agent scans to decide which services matter."""
    lines = [f"- {n}: {r}" if r else f"- {n}" for n, r in list_services(env_dir)]
    return "\n".join(lines) if lines else "(no services registered)"


def read(env_dir: Path, name: str) -> str | None:
    p = _dir(env_dir) / f"{_slug(name)}.md"
    if not p.exists():
        return None
    return _FM_RE.sub("", p.read_text(encoding="utf-8", errors="replace"), count=1).strip()


def save(env_dir: Path, name: str, responsibility: str, content: str) -> Path:
    """Write/replace a service file. `responsibility` is the one-line summary
    shown in the index; `content` is the markdown body (Responsibility /
    Standards / Constraints / Gotchas)."""
    slug = _slug(name)
    d = _dir(env_dir)
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{slug}.md"
    resp = " ".join(responsibility.split())  # one line
    p.write_text(
        f"---\nname: {slug}\nresponsibility: {resp}\n---\n\n"
        f"# {slug}\n\n{content.rstrip()}\n",
        encoding="utf-8",
    )
    return p


def legacy_map(env_dir: Path) -> str | None:
    p = env_dir / LEGACY_FILENAME
    if not p.exists():
        return None
    text = p.read_text(encoding="utf-8", errors="replace").strip()
    return text or None


def prompt_note(env_dir: Path) -> str:
    """Injection block for task prompts: the service index (cheap), with
    read-on-demand instructions — or how to seed the registry if empty."""
    services = list_services(env_dir)
    if not services:
        note = (
            "## Service registry — EMPTY\n"
            "`harn_env/services/` has no entries yet. As you explore the code "
            "for this task, register each service/module you understand via "
            "`save_service(name, responsibility, content)` — responsibility, "
            "standards, constraints (NOT a code walkthrough). Future tasks then "
            "skip re-discovery and instantly know which services matter."
        )
        legacy = legacy_map(env_dir)
        if legacy:
            note += (
                "\n\nA legacy single-file map exists (harn_env/CODEBASE.md) — "
                "split it into per-service entries when convenient."
            )
        return note
    return (
        "## Service registry (which parts of the system matter for this task?)\n"
        "Scan the index; call `read_service(name)` ONLY for services this task "
        "touches — each file states the service's responsibility, standards, "
        "and constraints:\n" + index(env_dir) +
        "\n\nIf a service you touch is missing/stale here, register/refresh it "
        "via `save_service` at the end of your work."
    )

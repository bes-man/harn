"""Append-only progress log shared by every agent.

This is the spine of harn's agent-agnostic continuity: whoever runs next
(Claude today, Codex tomorrow) reads the same log and the same task board, so
they pick up with full knowledge of what has happened and what is planned —
without relying on any single agent's private memory.

Stored at `harn_env/state/PROGRESS.md` as timestamped lines.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

_FILE = "PROGRESS.md"
_HEADER = "# Progress log\n\nAppend-only history every agent reads on pickup.\n"


def _path(env_dir: Path) -> Path:
    return env_dir / "state" / _FILE


def log(env_dir: Path, message: str, *, agent: str | None = None) -> None:
    """Append one timestamped line. Cheap and safe to call on every transition."""
    p = _path(env_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        p.write_text(_HEADER, encoding="utf-8")
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    who = f" [{agent}]" if agent else ""
    with p.open("a", encoding="utf-8") as fh:
        fh.write(f"- {stamp}{who} {message}\n")


def tail(env_dir: Path, lines: int = 20) -> str:
    """The most recent log lines, for injecting into the agent prompt."""
    p = _path(env_dir)
    if not p.exists():
        return ""
    entries = [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.startswith("- ")]
    return "\n".join(entries[-lines:])

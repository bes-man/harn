"""Per-task UI design artifacts.

For user-facing tasks the planning turn generates a static HTML mockup of the
interface *before* any code is written, the human approves it through the
normal HIL channel, and from then on it is the visual contract: the executor
builds to it, the oracle and the browser-verify turn check the real UI against
it.

One file per task: ``harn_env/design/<task_id>.html``.
"""
from __future__ import annotations

from pathlib import Path

DESIGN_DIRNAME = "design"


def design_path(env_dir: Path, task_id: str) -> Path:
    return env_dir / DESIGN_DIRNAME / f"{task_id}.html"


def save(env_dir: Path, task_id: str, html: str) -> Path:
    p = design_path(env_dir, task_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(html, encoding="utf-8")
    return p


def load(env_dir: Path, task_id: str) -> str | None:
    p = design_path(env_dir, task_id)
    if p.exists():
        return p.read_text(encoding="utf-8", errors="replace")
    return None


def exists(env_dir: Path, task_id: str) -> bool:
    return design_path(env_dir, task_id).exists()

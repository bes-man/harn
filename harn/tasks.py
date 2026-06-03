"""Reading PRDs and the task backlog from a harn_env."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Task:
    id: str
    path: Path
    title: str
    done: bool
    priority: int = 100  # lower = more important


_STATUS_RE = re.compile(r"^status:\s*(.+)$", re.IGNORECASE | re.MULTILINE)
_PRIORITY_RE = re.compile(r"^priority:\s*(\d+)$", re.IGNORECASE | re.MULTILINE)
_TITLE_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)


def _parse(path: Path) -> Task:
    text = path.read_text(encoding="utf-8", errors="replace")
    status_m = _STATUS_RE.search(text)
    prio_m = _PRIORITY_RE.search(text)
    title_m = _TITLE_RE.search(text)
    status = (status_m.group(1).strip().lower() if status_m else "todo")
    return Task(
        id=path.stem,
        path=path,
        title=title_m.group(1).strip() if title_m else path.stem,
        done=status in {"done", "complete", "completed"},
        priority=int(prio_m.group(1)) if prio_m else 100,
    )


def load_tasks(env_dir: Path) -> list[Task]:
    tasks_dir = env_dir / "tasks"
    if not tasks_dir.exists():
        return []
    return [_parse(p) for p in sorted(tasks_dir.glob("*.md"))]


def next_task(env_dir: Path) -> Task | None:
    pending = [t for t in load_tasks(env_dir) if not t.done]
    if not pending:
        return None
    pending.sort(key=lambda t: (t.priority, t.id))
    return pending[0]


def mark_done(task: Task, summary: str = "") -> None:
    text = task.path.read_text(encoding="utf-8", errors="replace")
    if _STATUS_RE.search(text):
        text = _STATUS_RE.sub("status: done", text, count=1)
    else:
        text = "status: done\n" + text
    if summary:
        text += f"\n\n<!-- harn: {summary} -->\n"
    task.path.write_text(text, encoding="utf-8")

"""Shared helpers for tests that need a minimal harn_env with task files."""
from __future__ import annotations

from pathlib import Path

from harn import tasks


def make_task(
    env_dir: Path,
    task_id: str,
    *,
    title: str = "Test task",
    status: str = tasks.TODO,
    priority: int = 10,
    prds: list[str] | None = None,
    skills: list[str] | None = None,
    description: str = "## What\nTest.\n\n## Done when\n- works",
) -> tasks.Task:
    """Write a minimal task (`<id>.md` + `<id>.state.json`) and return the Task."""
    (env_dir / "tasks").mkdir(parents=True, exist_ok=True)
    task = tasks.create_task(
        env_dir, title, description=description, prds=prds, priority=priority,
        skills=skills, task_id=task_id,
    )
    if status != tasks.TODO:
        task.status = tasks._normalize_status(status)
        tasks._save(task)
    return tasks._load_full(task.path)

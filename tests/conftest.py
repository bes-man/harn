"""Shared helpers for tests that need a minimal harn_env with JSON tasks."""
from __future__ import annotations

import json
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
    """Write a minimal JSON task file and return the Task object."""
    (env_dir / "tasks").mkdir(parents=True, exist_ok=True)
    path = env_dir / "tasks" / f"{task_id}.json"
    d = {
        "id": task_id,
        "title": title,
        "status": status,
        "priority": priority,
        "prds": prds or [],
        "epic": None,
        "user_story": None,
        "skills": skills or [],
        "subtasks": [],
        "description": description,
        "review_log": [],
    }
    path.write_text(json.dumps(d, indent=2), encoding="utf-8")
    return tasks._load(path)

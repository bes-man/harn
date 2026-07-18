"""Agent triggers (docs/superpowers/specs/2026-07-18-agent-triggers-design.md):
three ways to start a role's run — a Telegram slash command, a task entering
the role's status (auto mode), and the local HTTP API — all converging on
`dispatch_command`/`auto_scan`, which both call `roles_runner.run_role`.

No new daemon: Telegram commands are read the same way `harn watch` already
polls `getUpdates` for HIL answers (see `telegram.py`'s trust boundary),
and auto-scan is one more per-tick check inside that same loop.
"""
from __future__ import annotations

import re
from pathlib import Path

from . import config as config_mod
from . import ids
from . import loop as loop_mod
from . import roles as roles_mod
from . import roles_runner
from . import runner as runner_mod
from . import tasks as tasks_mod
from . import workflows as workflows_mod

_COMMAND_RE = re.compile(r"^/(\w+)(?:@\w+)?\s*(.*)$", re.DOTALL)


def parse_command(text: str) -> tuple[str, str] | None:
    """Split `/command rest of the text` into (command, rest). None if
    `text` isn't a slash command at all."""
    if not text:
        return None
    m = _COMMAND_RE.match(text.strip())
    if not m:
        return None
    return m.group(1), m.group(2).strip()


def dispatch_command(project_root: Path, env_dir: Path, command: str, arg: str,
                     *, cfg: "config_mod.Config | None" = None) -> dict:
    """The one code path Telegram commands and `POST /api/agents/<name>/run`
    both call. `command` is a role's `command:` (or `name:`) field; `arg` is
    either an existing task id or free text for a brand-new task.
    """
    role = roles_mod.find(env_dir, command)
    if role is None:
        return {"ok": False, "error": f"unknown command: /{command}"}
    if not arg.strip():
        return {"ok": False,
                "error": f"usage: /{role.command} <task-id-or-description>"}

    first_token = arg.strip().split(None, 1)[0]
    if ids.is_tracker_key(first_token):
        task = tasks_mod.find(env_dir, first_token)
        if task is None:
            return {"ok": False, "error": f"no task {first_token!r} on the board"}
        task_id = task.id
    else:
        title = arg.strip().splitlines()[0][:120]
        task = tasks_mod.create_task(env_dir, title, description=arg.strip())
        if task.status != role.status:
            tasks_mod.set_status(task, role.status, env_dir)
        task_id = task.id

    return roles_runner.run_role(project_root, env_dir, task_id, role.name, cfg=cfg)


def run_agent_payload(project_root: Path, env_dir: Path, payload: dict) -> dict:
    """`POST /api/agents/<name>/run` body handler — shares `dispatch_command`
    so Telegram and the API are the exact same code path (Studio's future
    "Run as <role>" button is the same call with `arg` = the task id)."""
    name = (payload.get("name") or "").strip()
    if not name:
        return {"ok": False, "error": "missing role 'name'"}
    task_id = (payload.get("task_id") or "").strip()
    text = (payload.get("text") or "").strip()
    arg = task_id or text
    if not arg:
        return {"ok": False, "error": "provide either 'task_id' or 'text'"}
    return dispatch_command(project_root, env_dir, name, arg)


def auto_scan(project_root: Path, env_dir: Path, *,
              cfg: "config_mod.Config | None" = None) -> dict | None:
    """One status-watch tick: a task whose status is serviced by a
    `trigger: auto` role, unclaimed, with no active run → launch exactly
    ONE. Candidates that already hit the step-attempts cap are skipped (auto
    mode must never burn budget in a crash loop) — they simply wait for a
    human to intervene; the board IS the queue, no separate queue state.
    Returns the launch result, or None if nothing was eligible this tick.
    """
    if runner_mod.active(env_dir) is not None:
        return None
    auto_roles = [r for r in roles_mod.discover(env_dir) if r.trigger == "auto"]
    if not auto_roles:
        return None
    by_status = {r.status: r for r in auto_roles}
    for task in sorted(tasks_mod.load_tasks(env_dir), key=lambda t: (t.priority, t.id)):
        role = by_status.get(task.status)
        if role is None or task.claimed_by:
            continue
        if _attempts_exhausted(env_dir, task, role):
            continue
        return dispatch_command(project_root, env_dir, role.command, task.id, cfg=cfg)
    return None


def _attempts_exhausted(env_dir: Path, task: "tasks_mod.Task",
                        role: "roles_mod.Role") -> bool:
    plan = workflows_mod.snapshot_for_task(env_dir, task.id, role.workflow or task.workflow)
    step_ids = [n["id"] for n in plan["nodes"] if n.get("kind") == "step" and n.get("id")]
    return any(
        (task.step_results.get(sid) or {}).get("attempts", 0) >= loop_mod._MAX_STEP_ATTEMPTS
        for sid in step_ids
    )

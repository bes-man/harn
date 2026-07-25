"""Agent triggers (docs/superpowers/specs/2026-07-18-agent-triggers-design.md):
three ways to start a role's run — a Telegram slash command, a task entering
the role's status (auto mode), and the local HTTP API — all converging on
`dispatch_command`/`auto_scan`, which both call `roles_runner.run_role`.

No new daemon: Telegram commands are read the same way `harn watch` already
polls `getUpdates` for HIL answers (see `telegram.py`'s trust boundary),
and auto-scan is one more per-tick check inside that same loop.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import config as config_mod
from . import ids
from . import loop as loop_mod
from . import roles as roles_mod
from . import roles_runner
from . import runner as runner_mod
from . import state as state_mod
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
                     *, cfg: "config_mod.Config | None" = None,
                     on_task_ready=None) -> dict:
    """The one code path Telegram commands and `POST /api/agents/<name>/run`
    both call. `command` is a role's `command:` (or `name:`) field; `arg` is
    either an existing task id or free text for a brand-new task.

    `on_task_ready(task_id, created)` — optional callback invoked once the
    task is resolved (found or newly created), BEFORE the role actually runs.
    The role run can take minutes; without this, a caller (e.g. the Telegram
    handler) has nothing to say until the whole thing finishes, which reads
    as "nothing happened" for as long as the run takes.
    """
    role = roles_mod.find(env_dir, command)
    if role is None:
        return {"ok": False, "error": f"unknown command: /{command}"}
    if not arg.strip():
        return {"ok": False,
                "error": f"usage: /{role.command} <task-id-or-description>"}

    first_token = arg.strip().split(None, 1)[0]
    created = False
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
        created = True

    if on_task_ready:
        on_task_ready(task_id, created)

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
    # One role per column. A dict comprehension here silently let the LAST
    # duplicate win, so a second role on the same column never ran and nothing
    # said why. Studio refuses to create that state, but role files are plain
    # .md a human can edit — so resolve it deterministically (first by the
    # sorted filename `discover` already uses) and say so out loud.
    by_status: dict[str, "roles_mod.Role"] = {}
    for r in auto_roles:
        if r.status in by_status:
            print(f"[harn] watch: column {r.status!r} is claimed by more than one "
                  f"auto agent ({by_status[r.status].name}, {r.name}) — using "
                  f"{by_status[r.status].name!r}. Give each column one agent.")
            continue
        by_status[r.status] = r
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


# Statuses a step lands in when its turn genuinely succeeded (mirrors
# roles_runner._STEP_DONE_STATUSES — a workflow is "unfinished" while any
# enabled step is NOT one of these).
_DONE_STATUSES = {"ok", "complete", "done"}


def unfinished_steps(env_dir: Path, task: "tasks_mod.Task") -> list[str]:
    """Step ids of `task`'s own plan that haven't successfully completed."""
    plan = workflows_mod.snapshot_for_task(env_dir, task.id, task.workflow)
    return [n["id"] for n in plan["nodes"]
            if n.get("kind") == "step" and n.get("id") and n.get("enabled") is not False
            and (task.step_results.get(n["id"]) or {}).get("status") not in _DONE_STATUSES]


_RESUME_LEDGER = "resume_attempts.json"


def _resume_ledger(env_dir: Path) -> dict:
    try:
        data = json.loads((env_dir / "state" / _RESUME_LEDGER).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_resume_ledger(env_dir: Path, data: dict) -> None:
    p = env_dir / "state" / _RESUME_LEDGER
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        p.write_text(json.dumps(data), encoding="utf-8")
    except OSError:                     # telemetry-grade: never break the tick
        pass


def _resume_budget_left(env_dir: Path, task: "tasks_mod.Task", limit: int) -> bool:
    """Has this task's SCHEDULED-resume budget been used up?

    Separate from the per-step attempt cap on purpose. That cap (2) exists to
    stop a crash loop inside ONE run and is spent entirely by a single run —
    so gating scheduled resumes on it would let a task retry exactly once and
    then never again, which is useless for the case this whole feature is
    for: a provider rate limit or expired session that lasts HOURS. The
    budget resets whenever the task actually makes progress (a step that
    wasn't done before is done now), so a task that keeps advancing keeps
    earning retries; only one that is stuck without progress burns through.
    """
    ledger = _resume_ledger(env_dir)
    entry = ledger.get(task.id) or {}
    done_now = len([s for s in task.step_results.values()
                    if isinstance(s, dict) and s.get("status") in _DONE_STATUSES])
    if done_now != entry.get("done"):        # progress since last resume → reset
        return True
    return int(entry.get("attempts") or 0) < limit


def _note_resume(env_dir: Path, task: "tasks_mod.Task") -> None:
    ledger = _resume_ledger(env_dir)
    entry = ledger.get(task.id) or {}
    done_now = len([s for s in task.step_results.values()
                    if isinstance(s, dict) and s.get("status") in _DONE_STATUSES])
    attempts = 0 if done_now != entry.get("done") else int(entry.get("attempts") or 0)
    ledger[task.id] = {"attempts": attempts + 1, "done": done_now}
    _save_resume_ledger(env_dir, ledger)


def _clear_step_attempts(task: "tasks_mod.Task", step_ids: list[str]) -> None:
    """Give the not-yet-done steps a fresh per-run attempt budget.

    A scheduled resume happens `resume_check_minutes` later against a
    possibly-changed world (rate limit lifted, session re-authenticated), so
    the previous run's exhausted attempt counters shouldn't veto it — exactly
    the reasoning `loop.answer()` already applies when a HUMAN intervenes.
    The scheduled-resume budget above is what bounds this instead.
    """
    changed = False
    for sid in step_ids:
        entry = task.step_results.get(sid)
        if isinstance(entry, dict) and entry.get("attempts"):
            task.step_results[sid] = {**entry, "attempts": 0}
            changed = True
    if changed:
        tasks_mod._save(task)


def resume_scan(project_root: Path, env_dir: Path, *,
                cfg: "config_mod.Config | None" = None) -> dict | None:
    """Relaunch ONE unfinished-but-unblocked task whose role run stopped.

    The gap this closes: a role run that dies on a TRANSIENT fault — a
    provider rate limit, an expired CLI session, a network blip — leaves the
    task stopped mid-workflow with no retry at all. Observed live: PRJ-001
    sat untouched overnight after its CLI's OAuth session expired, with the
    human's answer already recorded and several steps still pending.

    Deliberately conservative — it only fires when ALL of these hold:
      • no run is currently active (never races the live one);
      • the task is claimed by a REGISTERED role (so there's a defined way
        to continue it — this is the `--as <role>` resume path);
      • the task is not BLOCKED on a human answer. A pending question is a
        blocker, not a transient fault: retrying can't help and would just
        burn tokens on "still waiting" turns;
      • the task has at least one not-yet-successful step;
      • it still has SCHEDULED-resume budget left (see `_resume_budget_left`
        — a separate, progress-resetting counter, NOT the per-step attempt
        cap, which one run spends in full).

    Returns the launch result, or None when nothing was eligible.
    """
    cfg = cfg or config_mod.Config.load(env_dir)
    if runner_mod.active(env_dir) is not None:
        return None
    st = state_mod.State.load(env_dir / "state")
    blocked_task = st.current_task if st.phase == state_mod.BLOCKED else None
    for task in sorted(tasks_mod.load_tasks(env_dir), key=lambda t: (t.priority, t.id)):
        if task.status in (tasks_mod.DONE, tasks_mod.REVIEW) or not task.claimed_by:
            continue
        if task.id == blocked_task:
            continue
        role = roles_mod.find(env_dir, task.claimed_by)
        if role is None:
            continue
        pending = unfinished_steps(env_dir, task)
        if not pending:
            continue
        if not _resume_budget_left(env_dir, task, cfg.resume_max_attempts):
            continue
        _note_resume(env_dir, task)
        _clear_step_attempts(task, pending)
        return dispatch_command(project_root, env_dir, role.command or role.name,
                                task.id, cfg=cfg)
    return None

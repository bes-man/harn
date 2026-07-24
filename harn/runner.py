"""Background `harn run` launches triggered from the studio UI.

The studio is a passive local HTTP server (stdlib `http.server`) — it can't
execute an LLM agent turn itself. Instead, clicking "Launch" on a task spawns
`python -m harn run --task <id> [--auto] <project_root>` as a detached
subprocess and tracks it with a PID file, the same pattern already used by
`mcp_server._ensure_watch_running` for `harn watch`.

Only ONE UI-launched run may be active per project at a time: `harn run`'s task
pickup (`tasks.next_task`) isn't claim-locked the way the MCP `get_next_task
(worker=…)` path is (that's for deliberately parallel agents) — two concurrent
plain CLI loops would race writing the same STATE.json / task JSON files. The
existing `/api/progress` (events.jsonl) and the task's own fields (scratchpad,
decisions, review_log — via `tasks.to_dict`) already carry everything the board
needs to show live execution; this module only owns the process lifecycle.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path


def _pid_path(env_dir: Path) -> Path:
    return env_dir / "state" / "ui_run.pid"


def _log_path(env_dir: Path) -> Path:
    return env_dir / "state" / "ui_run.log"


def _last_run_path(env_dir: Path) -> Path:
    return env_dir / "state" / "ui_last_run.json"


def last_run(env_dir: Path) -> dict | None:
    """The outcome of the most recent UI-launched run, if available."""
    try:
        value = json.loads(_last_run_path(env_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _failure_reason(env_dir: Path, task_id: str) -> str:
    """Return the newest failed step's human-readable output, if any."""
    from . import tasks as tasks_mod
    task = tasks_mod.find(env_dir, task_id)
    if task is None:
        return ""
    for result in reversed(list(task.step_results.values())):
        if result.get("status") == "failed":
            return str(result.get("output") or "").strip()
    return ""


def _record_completion(env_dir: Path, info: dict, exit_code: int) -> None:
    result = {**info, "exit_code": exit_code, "finished_at": time.time()}
    reason = _failure_reason(env_dir, str(info.get("task_id") or ""))
    if reason:
        result["reason"] = reason
    _last_run_path(env_dir).write_text(json.dumps(result), encoding="utf-8")


def active(env_dir: Path) -> dict | None:
    """{"pid", "task_id", "auto", "started_at"} for the live UI-launched run, or
    None. Cleans up the PID file itself once the process has exited."""
    p = _pid_path(env_dir)
    if not p.exists():
        return None
    try:
        info = json.loads(p.read_text(encoding="utf-8"))
        os.kill(int(info["pid"]), 0)   # raises if the process is gone
        return info
    except (ProcessLookupError, ValueError, OSError, KeyError, json.JSONDecodeError):
        p.unlink(missing_ok=True)
        return None


def launch(project_root: Path, env_dir: Path, task_id: str, *,
           auto: bool = False, step: str | None = None,
           rerun: bool = False) -> dict:
    """Start `harn run --task <task_id>` in the background — the whole task
    loop by default, or exactly ONE step (`step=...`, optionally `rerun=True`
    to first restore that step's git checkpoint) for the studio UI's per-step
    Run/Rerun controls.

    Refuses if a run is already active for this project (single-runner-at-a-
    time — see module docstring). stdout/stderr go to state/ui_run.log so the
    board can show a live tail alongside the events.jsonl stage animation.
    """
    cur = active(env_dir)
    if cur:
        return {"ok": False,
                "error": f"a run is already active for {cur['task_id']} "
                         f"(pid {cur['pid']}) — stop it first"}
    if not task_id.strip():
        return {"ok": False, "error": "missing task_id"}
    state_dir = env_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    _last_run_path(env_dir).unlink(missing_ok=True)
    cmd = [sys.executable, "-m", "harn", "run", "--task", task_id]
    if step:
        cmd += ["--step", step]
        if rerun:
            cmd.append("--rerun")
    elif auto:
        cmd.append("--auto")
    else:
        # A role runner stamps its name on the task while it works.  A later
        # generic `harn run --task` cannot select that task: its claim belongs
        # to a different worker, so it exits with "No pending tasks."  Studio
        # full-task launches are resumptions, so continue through the same
        # registered role instead.  Do not apply this to one-step or auto
        # launches: those have intentionally different execution semantics.
        from . import roles as roles_mod
        from . import tasks as tasks_mod
        task = tasks_mod.find(env_dir, task_id)
        if task is not None and task.claimed_by:
            role = roles_mod.find(env_dir, task.claimed_by)
            if role is not None:
                cmd += ["--as", role.name]
    cmd.append(str(project_root))
    # PYTHONUNBUFFERED matters: stdout redirected to a real file (not a tty)
    # makes CPython fully block-buffer it, so every print() in the child sits
    # in an internal buffer and never reaches ui_run.log until the process
    # EXITS (confirmed directly: a child process's file-redirected output was
    # completely invisible while running, appearing only at exit). Without
    # this, the studio's "View log" panel reads an empty file for a run's
    # entire lifetime and shows "(no output yet)" even while it's actively
    # executing turns.
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    with open(_log_path(env_dir), "wb") as lf:
        proc = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT,
                                cwd=str(project_root), start_new_session=True,
                                env=env)
    # Nothing else ever calls .wait() on this Popen (the HTTP handler returns
    # immediately) — without reaping it, a finished child sits as a zombie
    # forever, and os.kill(pid, 0) in `active()` keeps reporting it as alive.
    # A background thread just to reap it costs nothing and never blocks.
    info = {"pid": proc.pid, "task_id": task_id, "auto": bool(auto),
            "step": step, "rerun": bool(rerun), "started_at": time.time()}
    _pid_path(env_dir).write_text(json.dumps(info), encoding="utf-8")
    def reap() -> None:
        _record_completion(env_dir, info, proc.wait())
    threading.Thread(target=reap, daemon=True).start()
    return {"ok": True, **info}


def stop(env_dir: Path) -> dict:
    """Best-effort SIGTERM of the active UI-launched run. A run killed mid-turn
    leaves its task IN_PROGRESS (never mid-write-corrupted — each task save is a
    single atomic file write); the next launch simply resumes it."""
    cur = active(env_dir)
    if not cur:
        return {"ok": False, "error": "no active run"}
    try:
        os.kill(int(cur["pid"]), 15)
    except OSError:
        pass
    _pid_path(env_dir).unlink(missing_ok=True)
    return {"ok": True, "task_id": cur["task_id"]}


def log_tail(env_dir: Path, n: int = 40) -> str:
    p = _log_path(env_dir)
    if not p.exists():
        return ""
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except OSError:
        return ""

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
    cmd = [sys.executable, "-m", "harn", "run", "--task", task_id]
    if step:
        cmd += ["--step", step]
        if rerun:
            cmd.append("--rerun")
    elif auto:
        cmd.append("--auto")
    cmd.append(str(project_root))
    with open(_log_path(env_dir), "wb") as lf:
        proc = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT,
                                cwd=str(project_root), start_new_session=True)
    # Nothing else ever calls .wait() on this Popen (the HTTP handler returns
    # immediately) — without reaping it, a finished child sits as a zombie
    # forever, and os.kill(pid, 0) in `active()` keeps reporting it as alive.
    # A background thread just to reap it costs nothing and never blocks.
    threading.Thread(target=proc.wait, daemon=True).start()
    info = {"pid": proc.pid, "task_id": task_id, "auto": bool(auto),
            "step": step, "rerun": bool(rerun), "started_at": time.time()}
    _pid_path(env_dir).write_text(json.dumps(info), encoding="utf-8")
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

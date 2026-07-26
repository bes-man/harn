"""Background `harn run` launches triggered from the studio UI.

The studio is a passive local HTTP server (stdlib `http.server`) — it can't
execute an LLM agent turn itself. Instead, clicking "Launch" on a task spawns
`python -m harn run --task <id> [--auto] <project_root>` as a detached
subprocess, the same pattern already used by
`mcp_server._ensure_watch_running` for `harn watch`.

Only ONE UI-launched run may be active per project at a time: `harn run`'s task
pickup (`tasks.next_task`) isn't claim-locked the way the MCP `get_next_task
(worker=…)` path is (that's for deliberately parallel agents) — two concurrent
plain CLI loops would race writing the same STATE.json / task JSON files. The
existing `/api/progress` (events.jsonl) and the task's own fields (scratchpad,
decisions, review_log — via `tasks.to_dict`) already carry everything the board
needs to show live execution; this module only owns the process lifecycle.

That lifecycle lives in `runstate` — an explicit state machine with a
heartbeat, reconciled on every read. This module used to own it directly via a
PID file, which could only answer "does this pid exist?"; a run that finished
its work and then hung kept the lock forever, leaving the board unstartable
(see runstate's module docstring for the full incident). Liveness questions
therefore go to `runstate`, never to a bare `os.kill` here.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import runstate as runstate_mod


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
    """Return the newest failed step's human-readable output, prefixed with
    that step's own title — a bare error message left the human unable to
    tell WHICH step it came from (observed live: the failure was on an
    early, already-"complete"-looking step, several steps before the one the
    human was actually looking at)."""
    from . import tasks as tasks_mod
    from . import workflows as workflows_mod
    task = tasks_mod.find(env_dir, task_id)
    if task is None:
        return ""
    for step_id, result in reversed(list(task.step_results.items())):
        if result.get("status") == "failed":
            text = str(result.get("output") or "").strip()
            plan = workflows_mod.load_task_plan(env_dir, task_id) or {}
            title = next((n.get("title") for n in plan.get("nodes", [])
                         if n.get("id") == step_id), None)
            return f"{title or step_id}: {text}" if text else ""
    return ""


def _record_completion(env_dir: Path, info: dict, exit_code: int) -> None:
    """Archive how the run ended, and close its lifecycle record.

    `ui_last_run.json` is the *archive* of the previous run (what the board
    shows after the fact); `runstate` is the *current* record (whether
    anything is running now). They're written together here but are never
    consulted for the same question, so they can't drift into disagreement.
    """
    from . import state as state_mod
    result = {**info, "exit_code": exit_code, "finished_at": time.time()}
    task_id = str(info.get("task_id") or "")
    # Blocked-on-a-question is not a failure — it's the funnel doing its job.
    # Check it FIRST: a role run that stops here can leave an EARLIER step's
    # stale "failed" output in step_results (e.g. today's retry-exhausted
    # attempt before a human unblocked it), which would otherwise paint an
    # already-resolved problem as this run's outcome.
    st = state_mod.State.load(env_dir / "state")
    if st.phase == state_mod.BLOCKED and st.current_task == info.get("task_id"):
        result["blocked"] = True
        result["reason"] = st.question or "Waiting on your answer."
        _last_run_path(env_dir).write_text(json.dumps(result), encoding="utf-8")
        runstate_mod.finish(env_dir, runstate_mod.BLOCKED,
                            reason=result["reason"], exit_code=exit_code,
                            task_id=task_id or None)
        return
    reason = _failure_reason(env_dir, task_id)
    if reason:
        result["reason"] = reason
    _last_run_path(env_dir).write_text(json.dumps(result), encoding="utf-8")
    runstate_mod.finish(
        env_dir,
        runstate_mod.FAILED if (reason or exit_code) else runstate_mod.DONE,
        reason=reason, exit_code=exit_code, task_id=task_id or None)


def active(env_dir: Path) -> dict | None:
    """The live UI-launched run, or None — reconciled, so a process that died
    or went silent is reaped here rather than blocking the next launch.

    Returns the historical dict shape ({"pid", "task_id", "auto", "step",
    "rerun", "started_at"}) so existing callers and the Studio client are
    unaffected, plus the state-machine fields for anything that wants them.
    """
    run = runstate_mod.active(env_dir)
    if run is None:
        return None
    return {"pid": run.pid, "task_id": run.task_id, "auto": run.auto,
            "step": run.only_step, "rerun": run.rerun,
            "started_at": run.started_at,
            "status": run.status, "current_step": run.current_step,
            "updated_at": run.updated_at}


def launch(project_root: Path, env_dir: Path, task_id: str, *,
           auto: bool = False, step: str | None = None,
           rerun: bool = False, as_role: str | None = None) -> dict:
    """Start `harn run --task <task_id>` in the background — the whole task
    loop by default, or exactly ONE step (`step=...`, optionally `rerun=True`
    to first restore that step's git checkpoint) for the studio UI's per-step
    Run/Rerun controls.

    `as_role`, when given, is authoritative: the caller already knows which
    role should own this run (e.g. the board column the task just landed on)
    and that decision is not second-guessed here. Omitted, the existing
    resumption inference below applies instead.

    Refuses if a run is already active for this project (single-runner-at-a-
    time — see module docstring). stdout/stderr go to state/ui_run.log so the
    board can show a live tail alongside the events.jsonl stage animation.
    """
    if not task_id.strip():
        return {"ok": False, "error": "missing task_id"}
    state_dir = env_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    # Claim the slot BEFORE spawning, under runstate's lock — checking first
    # and spawning after would let two near-simultaneous launches both see a
    # free runner and both start. The claim carries no pid yet (the process
    # doesn't exist); it's stamped in below, and released explicitly if the
    # spawn itself fails.
    claim = runstate_mod.begin(env_dir, task_id, auto=auto, only_step=step,
                               rerun=rerun)
    if not claim["ok"]:
        return {"ok": False, "error": claim["error"]}
    _last_run_path(env_dir).unlink(missing_ok=True)
    cmd = [sys.executable, "-m", "harn", "run", "--task", task_id]
    if step:
        cmd += ["--step", step]
        if rerun:
            cmd.append("--rerun")
    elif auto:
        cmd.append("--auto")
    elif as_role:
        cmd += ["--as", as_role]
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
            else:
                # Claimed by something that ISN'T a registered role — an agent
                # calling get_next_task(worker=...) picks its own id, and
                # "claude" is a common one. Without this the claim made the
                # task invisible to its own Resume: a plain `harn run --task`
                # runs as an unclaimed worker, `_eligible` rejects an
                # in_progress task owned by someone else, and the run reported
                # "No pending tasks. DONE." having done nothing (observed
                # live). Resume as the owner instead — which is exactly the
                # "resuming its own task" case `_eligible` already allows.
                cmd += ["--worker", task.claimed_by]
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
    try:
        with open(_log_path(env_dir), "wb") as lf:
            proc = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT,
                                    cwd=str(project_root), start_new_session=True,
                                    env=env)
    except Exception as exc:
        # The slot was claimed above; a spawn that never produced a process
        # must hand it straight back rather than leaving a phantom run that
        # only the staleness timeout could clear.
        runstate_mod.finish(env_dir, runstate_mod.FAILED,
                            reason=f"could not start the run process: {exc}",
                            task_id=task_id)
        return {"ok": False, "error": f"could not start the run process: {exc}"}
    # Nothing else ever calls .wait() on this Popen (the HTTP handler returns
    # immediately) — without reaping it, a finished child sits as a zombie
    # forever, and a liveness probe keeps reporting it as alive. A background
    # thread just to reap it costs nothing and never blocks.
    info = {"pid": proc.pid, "task_id": task_id, "auto": bool(auto),
            "step": step, "rerun": bool(rerun), "started_at": time.time()}
    run = runstate_mod.load(env_dir)
    run.pid = proc.pid
    run.updated_at = time.time()
    runstate_mod.save(env_dir, run)
    def reap() -> None:
        # Telemetry-grade: this is a detached daemon thread with no caller to
        # report to, so any exception here (json.dumps hitting an
        # unserializable value, a disk error) must never propagate — the
        # subprocess is already reaped either way.
        try:
            _record_completion(env_dir, info, proc.wait())
        except Exception:
            pass
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
    except (OSError, TypeError, ValueError):
        pass
    runstate_mod.finish(env_dir, runstate_mod.STOPPED,
                        reason="stopped by you", task_id=cur["task_id"])
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

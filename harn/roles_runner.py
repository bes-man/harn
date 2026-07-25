"""Run an agent role against one task — the "Running a role" section of
docs/superpowers/specs/2026-07-18-agent-roles-design.md.

`run_role` is the single entry point `harn run --task X --as ROLE` (CLI) and
Studio/dispatcher's "Run as ROLE" button both call. Trigger plumbing
(Telegram commands, status-watch auto mode, the HTTP API) is the companion
triggers spec — this module only knows how to execute ONE role run and
report the outcome; every trigger converges here.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from . import config as config_mod
from . import events as events_mod
from . import gitutil
from . import loop as loop_mod
from . import prhost
from . import roles as roles_mod
from . import secrets_store
from . import state as state_mod
from . import tasks as tasks_mod
from . import trackers as trackers_mod
from . import workflows as workflows_mod

# Statuses that mean "this step's turn already succeeded" — _run_steps skips
# straight past them on a resumed/re-dispatched role run instead of
# re-running the LLM turn from scratch every single time.
_STEP_DONE_STATUSES = {"ok", "complete", "done"}


def _step_ids_for(env_dir: Path, task: "tasks_mod.Task", workflow_name: str) -> list[str]:
    plan = workflows_mod.snapshot_for_task(env_dir, task.id, workflow_name or task.workflow)
    return [n["id"] for n in plan["nodes"] if n.get("kind") == "step" and n.get("id")]


def _run_steps(project_root: Path, env_dir: Path, task_id: str,
               step_ids: list[str], role_note: str) -> dict:
    """Run every step in order — but a role run is dispatched fresh on EACH
    trigger (a Telegram /command, the Studio Resume button, an auto-resume
    after answering a question), and without the two checks below it redid
    the whole workflow from step 1 every time: wasting real tokens re-running
    already-`ok` steps, and — observed live — aborting a resume entirely when
    a step that had already succeeded happened to fail on re-run (a transient
    CLI auth error), even though the actually-pending step was several steps
    further along.
    """
    task = tasks_mod.find(env_dir, task_id)
    done_ids = {sid for sid, r in (task.step_results if task else {}).items()
               if isinstance(r, dict) and r.get("status") in _STEP_DONE_STATUSES}
    for sid in step_ids:
        if sid in done_ids:
            continue
        result = loop_mod.run_step(project_root, env_dir, task_id, sid, role_note=role_note)
        if not result.get("ok"):
            return {"ok": False, "error": f"step {sid!r} failed", "step": result}
        # A step can succeed as a TURN (the agent called ask_user and ended
        # cleanly) while leaving the task genuinely blocked on a human
        # answer. Running the next step's turn anyway just burns tokens on
        # "still waiting" turns that can't make progress — stop the chain
        # here; the next trigger (another /command, Resume, or the
        # answer-triggered auto-resume) picks up cleanly since this step is
        # now recorded done_ids-eligible.
        st = state_mod.State.load(env_dir / "state")
        if st.phase == state_mod.BLOCKED:
            return {"ok": False, "error": "blocked — waiting on a human answer",
                    "blocked": True, "step": result}
    return {"ok": True}


def _run_in_place(project_root: Path, env_dir: Path, task_id: str,
                  step_ids: list[str], role_note: str) -> dict:
    return _run_steps(project_root, env_dir, task_id, step_ids, role_note)


def _run_in_worktree(project_root: Path, env_dir: Path, task_id: str,
                     step_ids: list[str], role_note: str) -> dict:
    """Reuses the parallel-wave worktree + patch machinery verbatim
    (`gitutil.create_worktree`/`diff_as_patch`/`apply_patch` — see
    `loop._run_parallel_wave`/`_merge_wave_patches`): the role's steps run
    against an isolated checkout of `project_root`; on success the resulting
    patch is applied back into the real working tree; `env_dir` (task/state
    bookkeeping) is never copied — it's read/written directly throughout, so
    task status/result/context land correctly regardless of isolation mode.
    """
    if not gitutil.is_repo(project_root):
        return _run_in_place(project_root, env_dir, task_id, step_ids, role_note)
    base_ref = gitutil.checkpoint(project_root, task_id, "role-worktree-base")
    if not base_ref:
        return _run_in_place(project_root, env_dir, task_id, step_ids, role_note)
    with tempfile.TemporaryDirectory(prefix=f"harn-role-{task_id}-") as tmp:
        wt = Path(tmp) / "worktree"
        if not gitutil.create_worktree(project_root, base_ref, wt):
            return _run_in_place(project_root, env_dir, task_id, step_ids, role_note)
        try:
            result = _run_steps(wt, env_dir, task_id, step_ids, role_note)
            if not result.get("ok"):
                return result
            patch = gitutil.diff_as_patch(wt, base_ref)
            if patch and not gitutil.apply_patch(project_root, patch):
                return {"ok": False, "error": "worktree patch did not apply cleanly"}
            return {"ok": True}
        finally:
            gitutil.remove_worktree(project_root, wt)


def run_role(project_root: Path, env_dir: Path, task_id: str, role_name: str,
            *, cfg: "config_mod.Config | None" = None) -> dict:
    cfg = cfg or config_mod.Config.load(env_dir)
    role = roles_mod.find(env_dir, role_name)
    if role is None:
        return {"ok": False, "error": f"no agent role {role_name!r}"}
    task = tasks_mod.find(env_dir, task_id)
    if task is None:
        return {"ok": False, "error": f"no task {task_id!r}"}

    missing = secrets_store.missing(env_dir, role.secrets)
    if missing:
        return {"ok": False, "error": "missing required secrets: " + ", ".join(missing)}

    warning = None
    if task.status != role.status:
        # The human is the authority on an explicit manual run — warn and
        # proceed rather than refuse. Auto triggers (companion spec) only
        # ever fire on a status match, so they never hit this branch.
        warning = (f"task status {task.status!r} does not match role status "
                   f"{role.status!r} — proceeding (manual run)")

    task.claimed_by = role.name
    task.claimed_at = tasks_mod._now_iso()
    if role.workflow:
        task.workflow = role.workflow
        task.workflow_confirmed = True
    tasks_mod._save(task)

    step_ids = _step_ids_for(env_dir, task, role.workflow)
    role_note = role.prompt_note()

    events_mod.emit(env_dir, "stage_start", task_id=task.id, stage="role_run",
                    role=role.name)
    with secrets_store.injected(env_dir, role.secrets):
        if role.isolation == "worktree":
            run_result = _run_in_worktree(project_root, env_dir, task.id, step_ids, role_note)
        else:
            run_result = _run_in_place(project_root, env_dir, task.id, step_ids, role_note)

    if not run_result.get("ok"):
        events_mod.emit(env_dir, "stage_end", task_id=task.id, stage="role_run",
                        role=role.name, ok=False, blocked=run_result.get("blocked", False))
        return {"ok": False, "error": run_result.get("error", "role run failed"),
                "warning": warning, "blocked": run_result.get("blocked", False)}

    task = tasks_mod.find(env_dir, task.id)
    verdict = "PASS"
    detail = ""
    if role.oracle:
        verdict, detail = _role_oracle_review(env_dir, cfg, task, project_root, role)

    if verdict == "FAIL":
        events_mod.emit(env_dir, "stage_end", task_id=task.id, stage="role_run",
                        role=role.name, ok=False, verdict=verdict)
        return {"ok": False, "error": detail or "oracle review failed",
                "task_id": task.id, "status": task.status, "warning": warning}

    if role.next_status:
        tasks_mod.set_status(task, role.next_status, env_dir)
        task = tasks_mod.find(env_dir, task.id)

    tracker = trackers_mod.for_task(task)
    tracker.push_status(task)
    tracker.push_result(task)

    pr_url = None
    if getattr(role, "push", False):
        pr_url = _push_and_open_pr(env_dir, project_root, task, role, cfg)

    events_mod.emit(env_dir, "stage_end", task_id=task.id, stage="role_run",
                    role=role.name, ok=True, verdict=verdict)
    return {"ok": True, "task_id": task.id, "status": task.status,
            "warning": warning, "pr_url": pr_url}


def _push_and_open_pr(env_dir: Path, project_root: Path, task: "tasks_mod.Task",
                      role: "roles_mod.Role", cfg: "config_mod.Config") -> str | None:
    """Commit -> push -> open PR after a successful role run. Opt-in
    (`role.push`) and fully guarded: any failure here degrades to None and
    NEVER fails the (already-succeeded) run."""
    try:
        branch = f"{cfg.git_branch_prefix}{task.id}"
        sha = gitutil.commit_to_branch(project_root, branch,
                                       f"{task.title} ({task.id})",
                                       exclude=("harn_env",))
        if not sha:
            return None
        if not gitutil.push_branch(project_root, cfg.git_push_remote, branch):
            return None
        base = cfg.git_pr_base or gitutil.default_branch(project_root) or "main"
        url = prhost.create_pr(project_root, base=base, head=branch,
                               title=f"{task.title} ({task.id})",
                               body=(task.result or task.description or "")[:4000])
        if url:
            task.result = (task.result + "\n\n" if task.result else "") + f"PR: {url}"
            tasks_mod._save(task)
        return url
    except Exception:
        return None


def _role_oracle_review(env_dir: Path, cfg: "config_mod.Config",
                        task: "tasks_mod.Task", project_root: Path,
                        role: "roles_mod.Role") -> tuple[str, str]:
    """A role's own post-run oracle check — deliberately NOT `loop.oracle_review`
    (that function unconditionally moves a FAIL to the hardcoded
    `changes_requested` status; the roles spec instead keeps the task in its
    CURRENT status on failure, recording the verdict for the next
    attempt/launch to pick up)."""
    adapter = loop_mod._pick_oracle_adapter(cfg)
    diff = loop_mod._git_diff(project_root)
    prompt = loop_mod._build_oracle_prompt(env_dir, cfg, task, diff)
    try:
        ores = adapter.run_turn(prompt, project_root,
                                **({"model": cfg.model} if cfg.model else {}))
    except Exception as e:
        events_mod.emit(env_dir, "error", task_id=task.id, stage="role_oracle",
                        detail=str(e)[:300])
        return ("PASS", "")
    verdict, detail = loop_mod._oracle_verdict(ores.text)
    task.review_log.append(tasks_mod.ReviewEntry(
        ts=tasks_mod._now_iso(),
        event=f"role_oracle_{verdict.lower()}",
        agent=adapter.name, comment=detail or None))
    tasks_mod._save(task)
    return (verdict, detail)

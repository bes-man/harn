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
from . import roles as roles_mod
from . import secrets_store
from . import tasks as tasks_mod
from . import trackers as trackers_mod
from . import workflows as workflows_mod


def _step_ids_for(env_dir: Path, task: "tasks_mod.Task", workflow_name: str) -> list[str]:
    plan = workflows_mod.snapshot_for_task(env_dir, task.id, workflow_name or task.workflow)
    return [n["id"] for n in plan["nodes"] if n.get("kind") == "step" and n.get("id")]


def _run_steps(project_root: Path, env_dir: Path, task_id: str,
               step_ids: list[str], role_note: str) -> dict:
    for sid in step_ids:
        result = loop_mod.run_step(project_root, env_dir, task_id, sid, role_note=role_note)
        if not result.get("ok"):
            return {"ok": False, "error": f"step {sid!r} failed", "step": result}
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
                        role=role.name, ok=False)
        return {"ok": False, "error": run_result.get("error", "role run failed"),
                "warning": warning}

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

    events_mod.emit(env_dir, "stage_end", task_id=task.id, stage="role_run",
                    role=role.name, ok=True, verdict=verdict)
    return {"ok": True, "task_id": task.id, "status": task.status, "warning": warning}


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

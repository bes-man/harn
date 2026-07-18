"""Studio's per-step Run/Rerun and whole-workflow Rerun wiring
(harn/studio.py's launch_step/rerun_workflow), on top of loop.run_step /
loop.rollback and runner.py's background-launch machinery."""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

from harn import studio, tasks, gitutil, workflows, transcript, events, state, ENV_DIRNAME


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _repo(tmp_path: Path) -> Path:
    _git(["init", "-q"], tmp_path)
    _git(["config", "user.email", "t@t.t"], tmp_path)
    _git(["config", "user.name", "t"], tmp_path)
    (tmp_path / "app.py").write_text("original\n")
    _git(["add", "-A"], tmp_path)
    _git(["commit", "-qm", "baseline"], tmp_path)
    return tmp_path


def _env(root: Path) -> Path:
    env = root / ENV_DIRNAME
    (env / "tasks").mkdir(parents=True)
    return env


def _plan(env: Path, task_id: str) -> None:
    workflows.save_task_plan(env, task_id, {"preamble": "", "nodes": [
        {"kind": "step", "id": "step-000001", "title": "Implement",
         "body": "do it", "required": [], "tools": [], "enabled": True},
        {"kind": "step", "id": "step-000002", "title": "Verify",
         "body": "check it", "required": [], "tools": [], "enabled": True}]})


def _fake_popen(pid=4242):
    proc = MagicMock()
    proc.pid = pid
    return proc


def test_launch_step_rejects_unknown_step(tmp_path):
    root = _repo(tmp_path)
    env = _env(root)
    t = tasks.create_task(env, "Add auth")
    _plan(env, t.id)
    r = studio.launch_step(env, {"task_id": t.id, "step_id": "not_real"})
    assert r["ok"] is False and "unknown step" in r["error"]


def test_launch_step_rejects_unknown_task(tmp_path):
    root = _repo(tmp_path)
    env = _env(root)
    r = studio.launch_step(env, {"task_id": "NOPE", "step_id": "step-000001"})
    assert r["ok"] is False


def test_launch_step_starts_background_process(tmp_path):
    root = _repo(tmp_path)
    env = _env(root)
    t = tasks.create_task(env, "Add auth")
    _plan(env, t.id)
    with patch("harn.runner.subprocess.Popen", return_value=_fake_popen()), \
         patch("harn.runner.os.kill"):
        r = studio.launch_step(env, {"task_id": t.id, "step_id": "step-000002",
                                      "rerun": True})
        assert r["ok"] is True and r["step"] == "step-000002" and r["rerun"] is True
        board = studio.board_payload(env)
        assert board["run"]["task_id"] == t.id
        assert board["run"]["step"] == "step-000002"


def test_rerun_workflow_requires_baseline(tmp_path):
    root = _repo(tmp_path)
    env = _env(root)
    t = tasks.create_task(env, "Add auth")   # never started -> no baseline_ref
    r = studio.rerun_workflow(env, {"task_id": t.id})
    assert r["ok"] is False and "baseline" in r["error"]


def test_rerun_workflow_unknown_task(tmp_path):
    root = _repo(tmp_path)
    env = _env(root)
    r = studio.rerun_workflow(env, {"task_id": "NOPE"})
    assert r["ok"] is False


def test_rerun_workflow_restores_and_relaunches(tmp_path):
    """subprocess.Popen is mocked at the module level `harn.runner` imports it
    from — but `subprocess` is a shared module object, so patching Popen there
    would ALSO break gitutil's real `git` subprocess calls this test needs to
    exercise for real. Mock runner.launch itself instead (its own machinery is
    already covered by test_runner.py) so the git rollback stays genuine."""
    root = _repo(tmp_path)
    env = _env(root)
    t = tasks.create_task(env, "Add auth")
    tasks.set_baseline(t, gitutil.head(root))
    t.stage_checkpoints["step-000001"] = gitutil.checkpoint(root, t.id, "step-000001")
    t.scratchpad = "old agent context"
    t.decisions = [tasks.Decision("old decision")]
    t.step_results = {"step-000001": {"status": "failed", "attempts": 2}}
    tasks._save(t)
    transcript.append(env, task_id=t.id, step_id="step-000001", run_id="old",
                      attempt=2, kind="error", phase="failed", text="old error")
    (root / "app.py").write_text("messed up by a bad attempt\n")

    fake_launch_result = {"ok": True, "pid": 4242, "task_id": t.id, "auto": False,
                          "step": None, "rerun": False, "started_at": 0}
    with patch("harn.runner.launch", return_value=fake_launch_result) as m:
        r = studio.rerun_workflow(env, {"task_id": t.id})

    assert r["ok"] is True
    assert (root / "app.py").read_text() == "original\n"   # rolled back
    fresh = tasks.find(env, t.id)
    assert fresh.status == tasks.TODO   # reopened
    assert fresh.stage_checkpoints == {}   # cleared by reopen
    assert fresh.step_results == {}
    assert fresh.scratchpad == ""
    assert fresh.decisions == []
    assert transcript.read(env, task_id=t.id)["entries"] == []
    m.assert_called_once_with(root, env, t.id, auto=False)


def test_launch_workflow_replaces_stale_snapshot_with_parallel_canvas_plan(tmp_path):
    root = _repo(tmp_path)
    env = _env(root)
    t = tasks.create_task(env, "Parallel fetch")
    _plan(env, t.id)  # stale two-step non-parallel plan
    current = tasks.find(env, t.id)
    current.step_results = {"step-000001": {"status": "ok", "attempts": 2}}
    current.review_log = [tasks.ReviewEntry("old", "failed", comment="old run")]
    tasks._save(current)
    transcript.append(env, task_id=t.id, step_id="step-000001", run_id="old",
                      attempt=1, kind="message", phase="completed", text="old context")
    events.emit(env, "error", task_id=t.id, stage="step-000001", detail="old error")
    canvas_plan = {"preamble": "", "nodes": [
        {"kind": "step", "id": "rate", "title": "Check rate", "parallel": "wave-a", "enabled": True},
        {"kind": "step", "id": "weather", "title": "Check weather", "parallel": "wave-a", "enabled": True},
    ]}
    launched = {"ok": True, "pid": 77, "task_id": t.id, "auto": False}

    with patch("harn.runner.active", return_value=None), \
         patch("harn.runner.launch", return_value=launched) as run:
        result = studio.launch_workflow(env, {
            "task_id": t.id, "workflow": "", "plan": canvas_plan, "auto": False})

    assert result["ok"] is True
    saved = workflows.load_task_plan(env, t.id)
    assert [n["id"] for n in saved["nodes"]] == ["rate", "weather"]
    assert {n["parallel"] for n in saved["nodes"]} == {"wave-a"}
    fresh = tasks.find(env, t.id)
    assert set(fresh.step_results) == {"rate", "weather"}
    assert all(r["status"] == "pending" for r in fresh.step_results.values())
    assert fresh.review_log == []
    assert transcript.read(env, task_id=t.id)["entries"] == []
    assert events.read(env, task_id=t.id) == []
    run.assert_called_once_with(root, env, t.id, auto=False)


def test_launch_workflow_restarts_prior_task_from_git_baseline_and_clears_context(tmp_path):
    root = _repo(tmp_path)
    env = _env(root)
    t = tasks.create_task(env, "Retry cleanly")
    tasks.set_baseline(t, gitutil.head(root))
    t.scratchpad = "stale reasoning"
    t.decisions = [tasks.Decision("stale choice")]
    t.changelog = [tasks.ChangeEntry("old", "stale change")]
    t.review_log = [tasks.ReviewEntry("old", "failed", comment="old error")]
    t.claimed_by = "old-agent"
    t.claimed_at = "old"
    t.stage_checkpoints = {"rate": gitutil.head(root)}
    t.step_results = {"rate": {"status": "blocked", "attempts": 2,
                                "output": "old failure"}}
    tasks._save(t)
    (root / "app.py").write_text("failed attempt\n")
    transcript.append(env, task_id=t.id, step_id="rate", run_id="old", attempt=2,
                      kind="error", phase="failed", text="old failure")
    events.new_run(env)
    events.emit(env, "stage_start", task_id=t.id, stage="rate")
    events.emit(env, "error", task_id=t.id, stage="rate", detail="old failure")
    events.emit(env, "context_read", task_id=t.id, kind="skill", name="old-skill")
    blocked = state.State(phase=state.BLOCKED, current_task=t.id,
                          current_step="rate", question="old question")
    blocked.save(env / "state")
    state.blocked_marker(env / "state").write_text("old question")
    canvas = {"preamble": "", "nodes": [
        {"kind": "step", "id": "rate", "title": "Check rate", "enabled": True},
    ]}

    with patch("harn.runner.active", return_value=None), \
         patch("harn.runner.launch", return_value={"ok": True, "task_id": t.id}):
        result = studio.launch_workflow(
            env, {"task_id": t.id, "workflow": "", "plan": canvas})

    assert result["ok"] is True
    assert (root / "app.py").read_text() == "original\n"
    fresh = tasks.find(env, t.id)
    assert fresh.scratchpad == "" and fresh.decisions == []
    assert fresh.changelog == [] and fresh.review_log == []
    assert fresh.claimed_by is None and fresh.claimed_at == ""
    assert fresh.stage_checkpoints == {}
    assert fresh.step_results == {"rate": {"status": "pending", "attempts": 0}}
    assert transcript.read(env, task_id=t.id)["entries"] == []
    assert events.read(env, task_id=t.id) == []
    assert studio.board_payload(env)["tasks"][0]["context_reads"] == []
    assert studio.progress_payload(env)["stages"] == {}
    clean_state = state.State.load(env / "state")
    assert clean_state.current_task is None and clean_state.question is None
    assert not state.blocked_marker(env / "state").exists()


def test_launch_workflow_keeps_context_when_git_rollback_fails(tmp_path):
    root = _repo(tmp_path)
    env = _env(root)
    t = tasks.create_task(env, "Do not erase on failure")
    tasks.set_baseline(t, gitutil.head(root))
    t.step_results = {"step-1": {"status": "failed", "attempts": 2}}
    tasks._save(t)
    transcript.append(env, task_id=t.id, step_id="step-1", run_id="old", attempt=2,
                      kind="error", phase="failed", text="keep failure")
    canvas = {"preamble": "", "nodes": [
        {"kind": "step", "id": "step-1", "title": "Implement", "enabled": True},
    ]}
    failed = gitutil.RollbackResult(False, "rollback failed", [])

    with patch("harn.runner.active", return_value=None), \
         patch("harn.loop.rollback", return_value=failed), \
         patch("harn.runner.launch") as launch:
        result = studio.launch_workflow(
            env, {"task_id": t.id, "workflow": "", "plan": canvas})

    assert result == {"ok": False, "error": "rollback failed"}
    assert tasks.find(env, t.id).step_results["step-1"]["attempts"] == 2
    assert transcript.read(env, task_id=t.id)["entries"][0]["text"] == "keep failure"
    launch.assert_not_called()

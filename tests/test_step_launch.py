"""Studio's per-step Run/Rerun and whole-workflow Rerun wiring
(harn/studio.py's launch_step/rerun_workflow), on top of loop.run_step /
loop.rollback and runner.py's background-launch machinery."""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

from harn import studio, tasks, gitutil, workflows, ENV_DIRNAME


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
    tasks._save(t)
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
    m.assert_called_once_with(root, env, t.id, auto=False)

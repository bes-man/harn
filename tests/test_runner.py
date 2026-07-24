"""Background per-task launches from the studio UI (harn/runner.py).

subprocess.Popen is mocked throughout — these tests verify the PID-file
lifecycle and single-runner-at-a-time guard, not that `harn run` itself works
(that's loop.py's job, covered elsewhere)."""
from __future__ import annotations

import json
import os
from unittest.mock import patch, MagicMock

from harn import roles, runner, tasks, ENV_DIRNAME


def _env(tmp_path):
    env = tmp_path / ENV_DIRNAME
    (env / "state").mkdir(parents=True)
    return env


def _fake_popen(pid=99999):
    proc = MagicMock()
    proc.pid = pid
    proc.wait.return_value = 0
    return proc


def test_no_run_active_initially(tmp_path):
    env = _env(tmp_path)
    assert runner.active(env) is None


def test_launch_writes_pid_file_and_returns_info(tmp_path):
    env = _env(tmp_path)
    with patch("harn.runner.subprocess.Popen", return_value=_fake_popen(4242)), \
         patch("harn.runner.os.kill"):   # pretend pid 4242 stays alive
        r = runner.launch(tmp_path, env, "PRJ-001", auto=False)
        assert r["ok"] is True and r["pid"] == 4242 and r["task_id"] == "PRJ-001"
        cur = runner.active(env)
        assert cur["pid"] == 4242 and cur["task_id"] == "PRJ-001"


def test_launch_command_includes_task_and_project(tmp_path):
    env = _env(tmp_path)
    with patch("harn.runner.subprocess.Popen", return_value=_fake_popen()) as m, \
         patch("harn.runner.os.kill"):
        runner.launch(tmp_path, env, "PRJ-002", auto=True)
    cmd = m.call_args[0][0]
    assert "--task" in cmd and "PRJ-002" in cmd
    assert "--auto" in cmd
    assert str(tmp_path) in cmd


def test_launch_resumes_task_through_its_claimed_role(tmp_path):
    """A Studio relaunch must continue the role that originally blocked.

    Otherwise a role-owned in-progress task is excluded by normal task
    selection as claimed by another worker, and the runner exits as if the
    board were empty.
    """
    env = _env(tmp_path)
    task = tasks.create_task(env, "Write the spec", task_id="PRJ-001")
    task.status = tasks.IN_PROGRESS
    task.claimed_by = "spec-writer"
    tasks._save(task)
    roles.save(env, {"name": "spec-writer", "status": tasks.IN_PROGRESS})

    with patch("harn.runner.subprocess.Popen", return_value=_fake_popen()) as m, \
         patch("harn.runner.os.kill"):
        runner.launch(tmp_path, env, task.id)

    cmd = m.call_args[0][0]
    assert cmd[cmd.index("--as") + 1] == "spec-writer"


def test_launch_step_includes_step_flag_not_auto(tmp_path):
    env = _env(tmp_path)
    with patch("harn.runner.subprocess.Popen", return_value=_fake_popen()) as m, \
         patch("harn.runner.os.kill"):
        r = runner.launch(tmp_path, env, "PRJ-003", step="step-000001", auto=True)
    cmd = m.call_args[0][0]
    assert "--step" in cmd and "step-000001" in cmd
    assert "--rerun" not in cmd
    assert "--auto" not in cmd   # step mode takes precedence over auto
    assert r["step"] == "step-000001" and r["rerun"] is False


def test_launch_step_rerun_includes_rerun_flag(tmp_path):
    env = _env(tmp_path)
    with patch("harn.runner.subprocess.Popen", return_value=_fake_popen()) as m, \
         patch("harn.runner.os.kill"):
        r = runner.launch(tmp_path, env, "PRJ-004", step="step-000002", rerun=True)
    cmd = m.call_args[0][0]
    assert "--step" in cmd and "step-000002" in cmd and "--rerun" in cmd
    assert r["step"] == "step-000002" and r["rerun"] is True


def test_launch_sets_pythonunbuffered_so_the_log_updates_live(tmp_path):
    # Regression: stdout redirected to a real file (not a tty) makes CPython
    # fully block-buffer it, so a child's print() output never reached
    # ui_run.log until the process EXITED -- the studio's "View log" panel
    # read an empty file for a run's entire lifetime, always showing "(no
    # output yet)" even while it was actively executing. Confirmed directly
    # by spawning a real subprocess with/without this env var.
    env = _env(tmp_path)
    with patch("harn.runner.subprocess.Popen", return_value=_fake_popen()) as m, \
         patch("harn.runner.os.kill"):
        runner.launch(tmp_path, env, "PRJ-010")
    assert m.call_args.kwargs["env"]["PYTHONUNBUFFERED"] == "1"


def test_active_reports_step_info(tmp_path):
    env = _env(tmp_path)
    with patch("harn.runner.subprocess.Popen", return_value=_fake_popen(555)), \
         patch("harn.runner.os.kill"):
        runner.launch(tmp_path, env, "PRJ-005", step="step-000003")
        cur = runner.active(env)
        assert cur["step"] == "step-000003"


def test_launch_refuses_when_already_active(tmp_path):
    env = _env(tmp_path)
    with patch("harn.runner.subprocess.Popen", return_value=_fake_popen(111)), \
         patch("harn.runner.os.kill"):
        runner.launch(tmp_path, env, "PRJ-001")
        r2 = runner.launch(tmp_path, env, "PRJ-002")
    assert r2["ok"] is False and "already active" in r2["error"]


def test_launch_rejects_empty_task_id(tmp_path):
    env = _env(tmp_path)
    r = runner.launch(tmp_path, env, "  ")
    assert r["ok"] is False


def test_active_cleans_up_stale_pid_file(tmp_path):
    env = _env(tmp_path)
    (env / "state" / "ui_run.pid").write_text(
        json.dumps({"pid": 999999999, "task_id": "PRJ-001", "auto": False,
                    "started_at": 0}), encoding="utf-8")
    assert runner.active(env) is None
    assert not (env / "state" / "ui_run.pid").exists()


def test_active_survives_a_real_live_process(tmp_path):
    """Use THIS test process's own pid as a real, guaranteed-alive pid instead
    of mocking os.kill, to exercise the real liveness check."""
    env = _env(tmp_path)
    (env / "state" / "ui_run.pid").write_text(
        json.dumps({"pid": os.getpid(), "task_id": "PRJ-009", "auto": False,
                    "started_at": 0}), encoding="utf-8")
    cur = runner.active(env)
    assert cur is not None and cur["task_id"] == "PRJ-009"


def test_stop_kills_and_clears_pid_file(tmp_path):
    env = _env(tmp_path)
    with patch("harn.runner.subprocess.Popen", return_value=_fake_popen(555)), \
         patch("harn.runner.os.kill") as mk:
        runner.launch(tmp_path, env, "PRJ-003")
        r = runner.stop(env)
    assert r["ok"] is True and r["task_id"] == "PRJ-003"
    mk.assert_any_call(555, 15)
    assert not (env / "state" / "ui_run.pid").exists()


def test_stop_with_no_active_run(tmp_path):
    env = _env(tmp_path)
    r = runner.stop(env)
    assert r["ok"] is False


def test_record_completion_keeps_failed_step_reason(tmp_path):
    env = _env(tmp_path)
    task = tasks.create_task(env, "Write spec", task_id="PRJ-001")
    task.step_results["research"] = {
        "status": "failed", "output": "Failed to authenticate: OAuth expired"
    }
    tasks._save(task)

    runner._record_completion(env, {"task_id": task.id}, 1)

    assert runner.last_run(env)["reason"] == "Failed to authenticate: OAuth expired"


def test_log_tail_missing_file(tmp_path):
    env = _env(tmp_path)
    assert runner.log_tail(env) == ""


def test_log_tail_returns_last_lines(tmp_path):
    env = _env(tmp_path)
    (env / "state" / "ui_run.log").write_text("\n".join(f"line{i}" for i in range(100)))
    tail = runner.log_tail(env, n=5)
    assert tail.splitlines() == [f"line{i}" for i in range(95, 100)]

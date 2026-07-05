"""Background per-task launches from the studio UI (harn/runner.py).

subprocess.Popen is mocked throughout — these tests verify the PID-file
lifecycle and single-runner-at-a-time guard, not that `harn run` itself works
(that's loop.py's job, covered elsewhere)."""
from __future__ import annotations

import json
import os
from unittest.mock import patch, MagicMock

from harn import runner, ENV_DIRNAME


def _env(tmp_path):
    env = tmp_path / ENV_DIRNAME
    (env / "state").mkdir(parents=True)
    return env


def _fake_popen(pid=99999):
    proc = MagicMock()
    proc.pid = pid
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


def test_log_tail_missing_file(tmp_path):
    env = _env(tmp_path)
    assert runner.log_tail(env) == ""


def test_log_tail_returns_last_lines(tmp_path):
    env = _env(tmp_path)
    (env / "state" / "ui_run.log").write_text("\n".join(f"line{i}" for i in range(100)))
    tail = runner.log_tail(env, n=5)
    assert tail.splitlines() == [f"line{i}" for i in range(95, 100)]

"""Background per-task launches from the studio UI (harn/runner.py).

subprocess.Popen is mocked throughout — these tests verify the run-record
lifecycle and single-runner-at-a-time guard, not that `harn run` itself works
(that's loop.py's job, covered elsewhere).

Liveness lives in `runstate`, so tests that need a fake pid to look alive
patch `harn.runstate.os.kill`, not runner's."""
from __future__ import annotations

import json
import os
import threading
import time
from unittest.mock import patch, MagicMock

from harn import roles, runner, runstate, tasks, ENV_DIRNAME


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
         patch("harn.runstate.os.kill"):   # pretend pid 4242 stays alive
        r = runner.launch(tmp_path, env, "PRJ-001", auto=False)
        assert r["ok"] is True and r["pid"] == 4242 and r["task_id"] == "PRJ-001"
        cur = runner.active(env)
        assert cur["pid"] == 4242 and cur["task_id"] == "PRJ-001"


def test_launch_command_includes_task_and_project(tmp_path):
    env = _env(tmp_path)
    with patch("harn.runner.subprocess.Popen", return_value=_fake_popen()) as m, \
         patch("harn.runstate.os.kill"):
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
         patch("harn.runstate.os.kill"):
        runner.launch(tmp_path, env, task.id)

    cmd = m.call_args[0][0]
    assert cmd[cmd.index("--as") + 1] == "spec-writer"


def test_launch_step_includes_step_flag_not_auto(tmp_path):
    env = _env(tmp_path)
    with patch("harn.runner.subprocess.Popen", return_value=_fake_popen()) as m, \
         patch("harn.runstate.os.kill"):
        r = runner.launch(tmp_path, env, "PRJ-003", step="step-000001", auto=True)
    cmd = m.call_args[0][0]
    assert "--step" in cmd and "step-000001" in cmd
    assert "--rerun" not in cmd
    assert "--auto" not in cmd   # step mode takes precedence over auto
    assert r["step"] == "step-000001" and r["rerun"] is False


def test_launch_step_rerun_includes_rerun_flag(tmp_path):
    env = _env(tmp_path)
    with patch("harn.runner.subprocess.Popen", return_value=_fake_popen()) as m, \
         patch("harn.runstate.os.kill"):
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
         patch("harn.runstate.os.kill"):
        runner.launch(tmp_path, env, "PRJ-010")
    assert m.call_args.kwargs["env"]["PYTHONUNBUFFERED"] == "1"


def test_active_reports_step_info(tmp_path):
    env = _env(tmp_path)
    with patch("harn.runner.subprocess.Popen", return_value=_fake_popen(555)), \
         patch("harn.runstate.os.kill"):
        runner.launch(tmp_path, env, "PRJ-005", step="step-000003")
        cur = runner.active(env)
        assert cur["step"] == "step-000003"


def test_launch_refuses_when_already_active(tmp_path):
    env = _env(tmp_path)
    with patch("harn.runner.subprocess.Popen", return_value=_fake_popen(111)), \
         patch("harn.runstate.os.kill"):
        runner.launch(tmp_path, env, "PRJ-001")
        r2 = runner.launch(tmp_path, env, "PRJ-002")
    assert r2["ok"] is False and "already active" in r2["error"]


def test_launch_rejects_empty_task_id(tmp_path):
    env = _env(tmp_path)
    r = runner.launch(tmp_path, env, "  ")
    assert r["ok"] is False


def test_active_reaps_a_run_whose_process_is_gone(tmp_path):
    """A record left behind by a process that died (SIGKILL, power cut) must
    not hold the single-runner lock: `active` reconciles it to CRASHED, which
    is terminal, so the next launch is allowed."""
    env = _env(tmp_path)
    runstate.save(env, runstate.Run(status=runstate.RUNNING, task_id="PRJ-001",
                                    pid=999999999, started_at=0, updated_at=0))
    assert runner.active(env) is None
    assert runstate.load(env).status == runstate.CRASHED


def test_active_survives_a_real_live_process(tmp_path):
    """Use THIS test process's own pid as a real, guaranteed-alive pid instead
    of mocking os.kill, to exercise the real liveness check."""
    env = _env(tmp_path)
    runstate.save(env, runstate.Run(status=runstate.RUNNING, task_id="PRJ-009",
                                    pid=os.getpid(), started_at=time.time(),
                                    updated_at=time.time()))
    cur = runner.active(env)
    assert cur is not None and cur["task_id"] == "PRJ-009"


def test_stop_kills_and_releases_the_runner(tmp_path):
    env = _env(tmp_path)
    # `wait()` must BLOCK for this to be the scenario under test: the default
    # fake returns instantly, so the reaper thread would settle the record as
    # DONE before stop() ever ran, and we'd be asserting against a run that
    # had already finished on its own rather than one being stopped.
    still_running = threading.Event()
    proc = _fake_popen(555)
    proc.wait.side_effect = lambda: (still_running.wait(5), 0)[1]
    with patch("harn.runner.subprocess.Popen", return_value=proc), \
         patch("harn.runstate.os.kill"), patch("harn.runner.os.kill") as mk:
        runner.launch(tmp_path, env, "PRJ-003")
        r = runner.stop(env)
        still_running.set()
    assert r["ok"] is True and r["task_id"] == "PRJ-003"
    mk.assert_any_call(555, 15)
    assert runstate.load(env).status == runstate.STOPPED
    assert runner.active(env) is None


def test_stop_with_no_active_run(tmp_path):
    env = _env(tmp_path)
    r = runner.stop(env)
    assert r["ok"] is False


def test_record_completion_keeps_failed_step_reason(tmp_path):
    """The reason is prefixed with the FAILED STEP'S OWN TITLE — a bare error
    message left no way to tell which step (of possibly several already
    marked complete) it actually came from."""
    from harn import workflows
    env = _env(tmp_path)
    task = tasks.create_task(env, "Write spec", task_id="PRJ-001")
    workflows.save_task_plan(env, task.id, {"preamble": "", "nodes": [
        {"kind": "step", "id": "research", "title": "Research"},
    ]})
    task.step_results["research"] = {
        "status": "failed", "output": "Failed to authenticate: OAuth expired"
    }
    tasks._save(task)

    runner._record_completion(env, {"task_id": task.id}, 1)

    assert runner.last_run(env)["reason"] == "Research: Failed to authenticate: OAuth expired"


def test_record_completion_reports_blocked_not_failed(tmp_path):
    """A run that stops because the agent asked a genuine question (state is
    BLOCKED for this task) is not a failure — observed live conflating the
    two: a role run that stopped waiting for a human answer showed a stale
    failed-step reason from an earlier, already-resolved retry instead of
    'waiting on your answer'."""
    from harn import state as state_mod
    env = _env(tmp_path)
    task = tasks.create_task(env, "Write spec", task_id="PRJ-001")
    # A stale failure from an earlier attempt must NOT leak into this run's
    # reported reason once the task is genuinely just waiting on a human.
    task.step_results["research"] = {
        "status": "failed", "output": "stale failure from a prior attempt"
    }
    tasks._save(task)
    state_dir = env / "state"
    st = state_mod.State(current_task=task.id)
    st.block("Which option — A or B?")
    st.save(state_dir)

    runner._record_completion(env, {"task_id": task.id}, 1)

    result = runner.last_run(env)
    assert result["blocked"] is True
    assert result["reason"] == "Which option — A or B?"


def test_record_completion_ignores_block_for_a_different_task(tmp_path):
    """The BLOCKED state is per-env, not per-task — a run for task A must not
    be reported as 'blocked' just because some OTHER task is the one
    currently waiting on an answer."""
    from harn import state as state_mod
    env = _env(tmp_path)
    task = tasks.create_task(env, "Write spec", task_id="PRJ-001")
    task.step_results["research"] = {"status": "failed", "output": "real failure"}
    tasks._save(task)
    state_dir = env / "state"
    st = state_mod.State(current_task="PRJ-999")
    st.block("Unrelated question")
    st.save(state_dir)

    runner._record_completion(env, {"task_id": task.id}, 1)

    result = runner.last_run(env)
    assert "blocked" not in result
    assert result["reason"] == "research: real failure"


def test_log_tail_missing_file(tmp_path):
    env = _env(tmp_path)
    assert runner.log_tail(env) == ""


def test_log_tail_returns_last_lines(tmp_path):
    env = _env(tmp_path)
    (env / "state" / "ui_run.log").write_text("\n".join(f"line{i}" for i in range(100)))
    tail = runner.log_tail(env, n=5)
    assert tail.splitlines() == [f"line{i}" for i in range(95, 100)]


def test_a_finished_run_frees_the_runner_even_if_its_process_lingers(tmp_path):
    """The exact incident this lifecycle rework exists for.

    A `harn run` finished its work and then hung (a Telegram retry loop). Its
    process stayed alive, so the old pid-file check reported "a run is active"
    indefinitely and Studio refused every launch, while the board showed the
    task as `todo` with all steps `pending` — a dead end with no way out.

    The run now releases the slot the moment its WORK is over (cli's `finally`),
    independently of when the process happens to exit. Uses this test's own pid
    so the process is genuinely, verifiably alive throughout.
    """
    from harn import cli
    env = _env(tmp_path)
    rs_begin = runstate.begin(env, "PRJ-001", pid=os.getpid())
    assert rs_begin["ok"] is True
    assert runner.active(env) is not None          # occupied while working

    cli._release_run_slot(env, 0)                  # work over; process lives on

    assert runstate.pid_alive(os.getpid()) is True  # still very much alive
    assert runner.active(env) is None               # …yet the runner is free
    with patch("harn.runner.subprocess.Popen", return_value=_fake_popen(777)), \
         patch("harn.runstate.os.kill"):
        assert runner.launch(tmp_path, env, "PRJ-002")["ok"] is True


def test_a_task_claimed_by_a_non_role_worker_is_resumed_as_that_worker(tmp_path):
    """Observed live: an agent claimed PRJ-001 via get_next_task(worker="claude"),
    which is an ADAPTER name, not a registered role. Resume then built a plain
    `harn run --task`, which runs as an unclaimed worker — and `_eligible`
    rejects an in_progress task owned by someone else — so the run exited
    "No pending tasks. DONE." having done nothing, with the board still
    looking fine. Resuming as the owner is exactly the "resuming its own
    task" case `_eligible` already allows."""
    env = _env(tmp_path)
    task = tasks.create_task(env, "Write the spec", task_id="PRJ-001")
    task.status = tasks.IN_PROGRESS
    task.claimed_by = "claude"          # an adapter name, no such role exists
    tasks._save(task)

    with patch("harn.runner.subprocess.Popen", return_value=_fake_popen()) as m, \
         patch("harn.runstate.os.kill"):
        runner.launch(tmp_path, env, task.id)

    cmd = m.call_args[0][0]
    assert cmd[cmd.index("--worker") + 1] == "claude"
    assert "--as" not in cmd            # there is no role to run as


def test_a_role_claim_still_resumes_through_the_role(tmp_path):
    """The role path must keep winning when the claim IS a role — it carries
    the role's own workflow and next_status, which --worker alone would not."""
    env = _env(tmp_path)
    task = tasks.create_task(env, "Write the spec", task_id="PRJ-002")
    task.status = tasks.IN_PROGRESS
    task.claimed_by = "spec-writer"
    tasks._save(task)
    roles.save(env, {"name": "spec-writer", "status": tasks.IN_PROGRESS})

    with patch("harn.runner.subprocess.Popen", return_value=_fake_popen()) as m, \
         patch("harn.runstate.os.kill"):
        runner.launch(tmp_path, env, task.id)

    cmd = m.call_args[0][0]
    assert cmd[cmd.index("--as") + 1] == "spec-writer"
    assert "--worker" not in cmd

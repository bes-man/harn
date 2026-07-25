"""The run-lifecycle state machine (harn/runstate.py).

The incident these guard against, in full: a `harn run` finished its work and
then hung in a Telegram retry loop. Its pid stayed alive, so the PID-file
liveness check kept reporting "a run is active" and Studio refused every new
launch — while the board showed the task as `todo` with every step `pending`.
The human had a dead end with no action available and nothing that would ever
clear it.

So the tests below care about one property above all: **there is always a way
forward**. No stored record, however wrong or stale, may leave the runner
permanently claimed.
"""
from __future__ import annotations

import os
import time

import pytest

from harn import runstate as rs


def _env(tmp_path):
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    return tmp_path


# --- the transition table -------------------------------------------------- #

def test_every_terminal_state_can_start_a_new_run():
    """The core anti-deadlock property: whatever a run ended as, the next one
    may begin. A terminal state that couldn't reach STARTING would be exactly
    the dead end this module exists to remove."""
    for status in rs.TERMINAL:
        assert rs.can_transition(status, rs.STARTING), status


def test_an_active_run_cannot_be_restarted_underneath_itself():
    for status in rs.ACTIVE:
        assert not rs.can_transition(status, rs.STARTING), status


def test_a_settled_run_cannot_go_back_to_running():
    for status in (rs.DONE, rs.FAILED, rs.STOPPED, rs.CRASHED, rs.BLOCKED):
        assert not rs.can_transition(status, rs.RUNNING), status


def test_finish_rejects_a_non_terminal_status(tmp_path):
    env = _env(tmp_path)
    rs.begin(env, "PRJ-001", pid=os.getpid())
    with pytest.raises(ValueError):
        rs.finish(env, rs.RUNNING)


# --- reconciliation: the deterministic recovery ---------------------------- #

def test_a_live_but_silent_run_is_reaped(tmp_path):
    """THE incident. The process is genuinely alive (this test's own pid), so
    a pid check alone says "still running" forever. Only the heartbeat can
    tell the difference between working and wedged."""
    env = _env(tmp_path)
    rs.save(env, rs.Run(status=rs.RUNNING, task_id="PRJ-001", pid=os.getpid(),
                        started_at=time.time() - 9999,
                        updated_at=time.time() - 9999))
    assert rs.pid_alive(os.getpid()) is True      # the pid check is fooled…
    assert rs.active(env) is None                 # …the state machine is not
    assert rs.load(env).status == rs.CRASHED
    assert "stopped making progress" in rs.load(env).reason


def test_a_heartbeating_run_is_left_alone(tmp_path):
    env = _env(tmp_path)
    rs.begin(env, "PRJ-001", pid=os.getpid())
    rs.heartbeat(env, step_id="step-2")
    live = rs.active(env)
    assert live is not None
    assert live.status == rs.RUNNING and live.current_step == "step-2"


def test_reconcile_is_deterministic_for_the_same_inputs(tmp_path):
    """Reconciliation must be a pure verdict on (record, liveness, now), not
    something that sometimes fires — "it clears itself eventually" is not a
    recovery story a human can rely on."""
    env = _env(tmp_path)
    rs.save(env, rs.Run(status=rs.RUNNING, task_id="PRJ-001", pid=os.getpid(),
                        started_at=0, updated_at=1000))
    at = 1000 + rs.DEFAULT_STALE_AFTER_S + 1
    verdicts = {rs.reconcile(env, now=at).status for _ in range(5)}
    assert verdicts == {rs.CRASHED}


def test_a_run_is_not_reaped_one_second_early(tmp_path):
    env = _env(tmp_path)
    rs.save(env, rs.Run(status=rs.RUNNING, task_id="PRJ-001", pid=os.getpid(),
                        started_at=0, updated_at=1000))
    at = 1000 + rs.DEFAULT_STALE_AFTER_S - 1
    assert rs.reconcile(env, now=at).status == rs.RUNNING


def test_a_starting_run_without_a_pid_yet_is_not_reaped(tmp_path):
    """`begin` claims the slot before the subprocess exists, so for a moment
    there is no pid to check. Treating "no pid" as "dead" would reap the run
    that is still being born."""
    env = _env(tmp_path)
    rs.begin(env, "PRJ-001")
    assert rs.active(env) is not None


# --- the launch gate ------------------------------------------------------- #

def test_begin_refuses_while_a_run_is_genuinely_active(tmp_path):
    env = _env(tmp_path)
    rs.begin(env, "PRJ-001", pid=os.getpid())
    second = rs.begin(env, "PRJ-002", pid=os.getpid())
    assert second["ok"] is False and "already active" in second["error"]


@pytest.mark.parametrize("ending", [rs.DONE, rs.FAILED, rs.STOPPED,
                                    rs.CRASHED, rs.BLOCKED])
def test_begin_is_allowed_after_any_ending(tmp_path, ending):
    env = _env(tmp_path)
    rs.save(env, rs.Run(status=ending, task_id="PRJ-001"))
    assert rs.begin(env, "PRJ-002", pid=os.getpid())["ok"] is True


def test_a_dead_process_does_not_block_the_next_launch(tmp_path):
    env = _env(tmp_path)
    rs.save(env, rs.Run(status=rs.RUNNING, task_id="PRJ-001", pid=999999999,
                        started_at=time.time(), updated_at=time.time()))
    assert rs.begin(env, "PRJ-002", pid=os.getpid())["ok"] is True


# --- ownership: no cross-run interference ---------------------------------- #

def test_a_foreign_pid_cannot_heartbeat_someone_elses_run(tmp_path):
    env = _env(tmp_path)
    rs.begin(env, "PRJ-001", pid=os.getpid())
    before = rs.load(env).updated_at
    assert rs.heartbeat(env, pid=os.getpid() + 12345) is None
    assert rs.load(env).updated_at == before


def test_a_late_finish_from_a_previous_run_cannot_kill_the_current_one(tmp_path):
    """The run process and the parent's reaper thread both call finish. A
    straggler from the run BEFORE must not close out the run that replaced
    it — that would hand the runner away mid-flight."""
    env = _env(tmp_path)
    rs.save(env, rs.Run(status=rs.RUNNING, task_id="PRJ-002", pid=4242,
                        started_at=time.time(), updated_at=time.time()))
    assert rs.finish(env, rs.DONE, pid=1111, task_id="PRJ-001") is None
    assert rs.load(env).status == rs.RUNNING


def test_finish_is_idempotent(tmp_path):
    env = _env(tmp_path)
    rs.begin(env, "PRJ-001", pid=os.getpid())
    assert rs.finish(env, rs.DONE) is not None
    assert rs.finish(env, rs.FAILED, reason="late") is None
    settled = rs.load(env)
    assert settled.status == rs.DONE and settled.reason != "late"


# --- never strand the caller ----------------------------------------------- #

def test_a_corrupt_record_reads_as_no_run(tmp_path):
    """Refusing to answer would strand the runner exactly the way the original
    bug did, so unreadable state means "nothing is running", not an error."""
    env = _env(tmp_path)
    (env / "state" / "run.json").write_text("{not json", encoding="utf-8")
    assert rs.load(env).status == rs.IDLE
    assert rs.active(env) is None
    assert rs.begin(env, "PRJ-001", pid=os.getpid())["ok"] is True


def test_an_unknown_status_reads_as_no_run(tmp_path):
    env = _env(tmp_path)
    (env / "state" / "run.json").write_text('{"status": "wat"}', encoding="utf-8")
    assert rs.load(env).status == rs.IDLE


def test_an_unknown_field_from_a_newer_version_is_ignored(tmp_path):
    env = _env(tmp_path)
    (env / "state" / "run.json").write_text(
        '{"status": "running", "task_id": "PRJ-001", "from_the_future": 1}',
        encoding="utf-8")
    assert rs.load(env).task_id == "PRJ-001"

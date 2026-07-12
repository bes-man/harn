"""Persisted cross-relaunch attempt cap for agent-turn steps.

Regression coverage for a live incident: a task whose step kept failing the
SAME way (an un-callable/permission-blocked tool) got relaunched several
times by studio/watch, and burned its full token budget on EACH relaunch --
because the existing retry guards (`enforcement_retried`, the budget's
`_RunSpend`) are local variables inside loop.run() that reset the moment a
NEW process starts. `_MAX_STEP_ATTEMPTS` persists the attempt count on
task.step_results[sid], so it survives across separate loop.run() calls
(simulating separate `harn run` process relaunches) and stops a doomed step
from being retried forever, one relaunch at a time.
"""
from __future__ import annotations

import subprocess

from harn import loop, tasks, workflows, scaffold, state, ENV_DIRNAME
from harn.adapters.base import AgentResult
from .conftest import make_task


class NeverUsesToolAdapter:
    """Always answers 'ok' but never calls the required tool/skill -- so
    _audit_step_usage marks it unused_required forever, on every attempt."""
    name = "fake"
    def __init__(self):
        self.calls = []
    def available(self):
        return True
    def run_turn(self, prompt, cwd, timeout=1800, *, model=None, effort=None,
                temperature=None):
        self.calls.append({"prompt": prompt})
        return AgentResult(ok=True, text="I need permission to use that tool.")


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True)


def _project(tmp_path):
    _git(["init", "-q"], tmp_path); _git(["config", "user.email", "t@t"], tmp_path)
    _git(["config", "user.name", "t"], tmp_path)
    scaffold.setup(tmp_path)
    env = tmp_path / ENV_DIRNAME
    for p in (env / "tasks").glob("*"):
        p.unlink()
    (env / "harn.toml").write_text(
        '[harn]\nagent = "fake"\n[feedback]\ntest_cmd = ""\nrequire_tests = false\n'
        "[loop]\nmax_iterations = 10\n[notify]\nwait_for_reply = false\n")
    _git(["add", "-A"], tmp_path); _git(["commit", "-qm", "base"], tmp_path)
    t = make_task(env, "PRJ-001", title="Check rate")
    plan = {"preamble": "", "nodes": [
        {"kind": "step", "title": "Check rate", "body": "call the tool",
         "id": "step-001", "agent": "", "model": "", "effort": "",
         "temperature": "", "required": ["some_tool"], "tools": ["some_tool"],
         "enabled": True}]}
    workflows.save_task_plan(env, t.id, plan)
    return env, t


def test_persisted_cap_blocks_before_a_third_relaunch_attempt(tmp_path, monkeypatch):
    env, t = _project(tmp_path)
    fake = NeverUsesToolAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])

    # Relaunch #1: a fresh loop.run() call -- one turn, then (via the
    # existing in-memory enforcement_retried) one in-process retry, then
    # BLOCKED (unused_required still unused after its one retry).
    loop.run(tmp_path, env)
    task = tasks.find(env, t.id)
    assert task.step_results["step-001"]["status"] == "blocked"
    assert task.step_results["step-001"]["attempts"] == 2
    calls_after_relaunch_1 = len(fake.calls)
    assert calls_after_relaunch_1 == 2

    # A human resumes without fixing anything (STATE.json BLOCKED -> READY,
    # exactly what a bare "harn run" relaunch does after `harn answer`/studio
    # Resume) -- clear the block marker but do NOT reset attempts here; that
    # only happens via loop.answer(), tested separately below.
    st = state.State.load(env / "state")
    st.answer("try again")
    st.save(env / "state")

    # Relaunch #2: a SEPARATE loop.run() call, simulating studio/watch
    # spawning a brand-new `harn run` process for the same still-in-progress
    # task. Before this fix, enforcement_retried/_RunSpend would both be
    # fresh again, so this would burn ANOTHER 1-2 full turns. With the
    # persisted cap, prior_attempts=2 >= _MAX_STEP_ATTEMPTS=2 trips BEFORE
    # any turn starts.
    loop.run(tmp_path, env)
    assert len(fake.calls) == calls_after_relaunch_1  # zero NEW adapter calls
    task = tasks.find(env, t.id)
    assert task.step_results["step-001"]["status"] == "blocked"
    q = state.State.load(env / "state").question
    assert "already been attempted 2 times" in q


def test_answer_resets_the_attempt_cap_for_the_current_task(tmp_path, monkeypatch):
    env, t = _project(tmp_path)
    fake = NeverUsesToolAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])

    loop.run(tmp_path, env)   # relaunch #1 -> blocked, attempts == 2
    loop.run(tmp_path, env)   # relaunch #2 -> cap trips, zero new calls
    calls_before_answer = len(fake.calls)

    # An explicit human answer (harn answer / studio's Submit) is the
    # designated escape hatch: it must reset the persisted counter so the
    # step gets a fresh allowance post-intervention.
    loop.answer(env, "fixed the permission, try again", source="cli")
    task = tasks.find(env, t.id)
    assert task.step_results["step-001"]["attempts"] == 0

    loop.run(tmp_path, env)   # relaunch #3, post-answer -> must make a NEW call
    assert len(fake.calls) > calls_before_answer

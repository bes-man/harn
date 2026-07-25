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


def test_unobserved_tool_usage_consumes_attempt_cap_and_blocks(tmp_path, monkeypatch):
    env, t = _project(tmp_path)
    fake = NeverUsesToolAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])

    loop.run(tmp_path, env)
    task = tasks.find(env, t.id)
    assert task.step_results["step-001"]["status"] == "blocked"
    assert task.step_results["step-001"]["usage"]["tools"]["some_tool"] == "unused_required"
    assert task.step_results["step-001"]["attempts"] == 2
    assert len(fake.calls) == 2


def test_answer_resets_the_attempt_cap_for_the_current_task(tmp_path, monkeypatch):
    env, t = _project(tmp_path)
    fake = NeverUsesToolAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])

    task = tasks.find(env, t.id)
    task.step_results["step-001"] = {"status": "blocked", "attempts": 2}
    tasks._save(task)
    st = state.State.load(env / "state")
    st.current_task = t.id
    st.block("attempt cap")
    st.save(env / "state")

    # An explicit human answer (harn answer / studio's Submit) is the
    # designated escape hatch: it must reset the persisted counter so the
    # step gets a fresh allowance post-intervention.
    loop.answer(env, "fixed the permission, try again", source="cli")
    task = tasks.find(env, t.id)
    assert task.step_results["step-001"]["attempts"] == 0


class AlwaysAuthFailsAdapter:
    """Stands in for the live incident this guards against: the adapter's own
    call fails outright (expired CLI auth, network error, crash) -- not a
    question, not failing tests, not missing tool usage. `run_turn` returns
    normally (no exception) but with `ok=False`."""
    name = "fake"
    def __init__(self):
        self.calls = []
    def available(self):
        return True
    def run_turn(self, prompt, cwd, timeout=1800, *, model=None, effort=None,
                temperature=None):
        self.calls.append({"prompt": prompt})
        return AgentResult(ok=False,
                           text="Failed to authenticate: OAuth session expired "
                                "and could not be refreshed")


def test_failed_agent_turn_blocks_after_the_attempt_cap_not_marked_ok(tmp_path, monkeypatch):
    """Regression for a live incident: loop.run()'s main step loop never
    checked `result.ok` -- an adapter that failed outright on every attempt
    (expired CLI auth) got ledgered as "ok" (with the error text AS the
    output) and the loop sailed through the rest of the workflow the same
    way, ending with the task silently submitted for review having done
    nothing. run_step()/roles_runner (the role-dispatched resume path)
    already checked this correctly -- this was the one path that didn't."""
    env, t = _project(tmp_path)
    fake = AlwaysAuthFailsAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])

    loop.run(tmp_path, env)
    task = tasks.find(env, t.id)
    # Blocked after the attempt cap -- NOT "ok", and NOT left "running".
    assert task.step_results["step-001"]["status"] == "blocked"
    assert task.step_results["step-001"]["attempts"] == 2
    assert len(fake.calls) == 2
    # The task never got anywhere near being submitted for review.
    assert task.status != tasks.REVIEW
    assert state.State.load(env / "state").phase == state.BLOCKED


def test_studio_retry_resets_only_the_requested_step_attempts(tmp_path, monkeypatch):
    from harn import studio
    env, t = _project(tmp_path)
    task = tasks.find(env, t.id)
    task.step_results["step-001"] = {"status": "blocked", "attempts": 2}
    tasks._save(task)
    st = state.State.load(env / "state")
    st.current_task = t.id
    st.block("attempt cap")
    st.save(env / "state")

    result = studio.reset_step_attempts_payload(env, t.id, "step-001")

    assert result["ok"] is True
    fresh = tasks.find(env, t.id)
    assert fresh.step_results["step-001"]["attempts"] == 0
    assert fresh.step_results["step-001"]["status"] == "pending"
    assert state.State.load(env / "state").phase != state.BLOCKED

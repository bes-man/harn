"""Post-step required/recommended skill+tool usage audit and retry-once
enforcement (Phase 4), sequential steps only."""
from __future__ import annotations

import subprocess

from harn import ENV_DIRNAME, events, loop, scaffold, state, tasks, workflows
from harn.adapters.base import AgentResult

from .conftest import make_task


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True)


def _project(tmp_path, nodes):
    _git(["init", "-q"], tmp_path)
    _git(["config", "user.email", "t@t"], tmp_path)
    _git(["config", "user.name", "t"], tmp_path)
    scaffold.setup(tmp_path)
    env = tmp_path / ENV_DIRNAME
    for p in (env / "tasks").glob("*"):
        p.unlink()
    (env / "harn.toml").write_text(
        '[harn]\nagent = "fake"\n[feedback]\ntest_cmd = ""\nrequire_tests = false\n'
        "[loop]\nmax_iterations = 20\n[notify]\nwait_for_reply = false\n")
    _git(["add", "-A"], tmp_path)
    _git(["commit", "-qm", "base"], tmp_path)
    t = make_task(env, "PRJ-001", title="Feat")
    workflows.save_task_plan(env, t.id, {"preamble": "", "nodes": nodes})
    return env, t


def _step(title, **kw):
    base = {"kind": "step", "title": title, "body": "", "id": "", "agent": "",
            "model": "", "effort": "", "temperature": "", "type": "",
            "command": "", "on_fail": "", "required": [],
            "skills_recommended": [], "tools": [], "tools_recommended": [],
            "enabled": True}
    base.update(kw)
    return base


class RecordingAdapter:
    name = "fake"

    def __init__(self, texts=None):
        self.calls = []
        self._texts = list(texts or ["done"])

    def available(self):
        return True

    def run_turn(self, prompt, cwd, timeout=1800, *, model=None, effort=None,
                temperature=None):
        self.calls.append({"prompt": prompt})
        i = min(len(self.calls) - 1, len(self._texts) - 1)
        return AgentResult(ok=True, text=self._texts[i])


def _env(tmp_path):
    project_root = tmp_path / "proj"
    project_root.mkdir()
    env = project_root / "harn_env"
    env.mkdir()
    return env, project_root


def test_audit_marks_used_recommended_and_unused_required(tmp_path):
    env, project_root = _env(tmp_path)
    task = tasks.create_task(env, "Do the thing")
    step = {"id": "s1", "required": ["standards"], "skills_recommended": ["ui"],
            "tools": ["run_tests"], "tools_recommended": ["read_design"]}
    events.emit(env, "context_read", task_id=task.id, step_id="s1",
                kind="skill", name="standards")
    events.emit(env, "tool_used", task_id=task.id, step_id="s1", tool="run_tests")
    usage = loop._audit_step_usage(env, task, step)
    assert usage["skills"]["standards"] == "used"
    assert usage["skills"]["ui"] == "unused_recommended"
    assert usage["tools"]["run_tests"] == "used"
    assert usage["tools"]["read_design"] == "unused_recommended"


def test_audit_flags_unused_required_skill(tmp_path):
    env, project_root = _env(tmp_path)
    task = tasks.create_task(env, "Do the thing")
    step = {"id": "s1", "required": ["standards"], "skills_recommended": [],
            "tools": [], "tools_recommended": []}
    usage = loop._audit_step_usage(env, task, step)
    assert usage["skills"]["standards"] == "unused_required"


def test_audit_ignores_events_scoped_to_a_different_step(tmp_path):
    env, project_root = _env(tmp_path)
    task = tasks.create_task(env, "Do the thing")
    step = {"id": "s1", "required": ["standards"], "skills_recommended": [],
            "tools": [], "tools_recommended": []}
    # This event is for a DIFFERENT step (s2) — must not count as "used" for s1.
    events.emit(env, "context_read", task_id=task.id, step_id="s2",
                kind="skill", name="standards")
    usage = loop._audit_step_usage(env, task, step)
    assert usage["skills"]["standards"] == "unused_required"


def test_required_and_unused_triggers_exactly_one_retry_then_blocked(tmp_path, monkeypatch):
    env, t = _project(tmp_path, [
        _step("Only step", id="s1", required=["standards"]),
    ])
    fake = RecordingAdapter(["done, but never called read_skill"])
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])
    loop.run(tmp_path, env)

    # Exactly two turns happened: the original attempt + one retry.
    assert len(fake.calls) == 2
    assert "standards" in fake.calls[1]["prompt"]
    assert "MUST" in fake.calls[1]["prompt"]

    fresh = tasks.find(env, t.id)
    assert fresh.step_results["s1"]["status"] == "blocked"
    state_dir = env / "state"
    st = state.State.load(state_dir)
    assert st.phase == state.BLOCKED
    assert "standards" in (st.question or "")
    assert state.blocked_marker(state_dir).exists()


def test_recommended_and_unused_never_retries(tmp_path, monkeypatch):
    env, t = _project(tmp_path, [
        _step("Only step", id="s1", skills_recommended=["ui"]),
    ])
    fake = RecordingAdapter(["done"])
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])
    loop.run(tmp_path, env)

    assert len(fake.calls) == 1   # no retry
    fresh = tasks.find(env, t.id)
    assert fresh.step_results["s1"]["status"] == "ok"
    assert fresh.step_results["s1"]["usage"]["skills"]["ui"] == "unused_recommended"


def test_required_and_used_advances_normally_no_retry(tmp_path, monkeypatch):
    """If the agent DID use the required tool (tool_used event tagged to this
    step), the step advances straight through — no retry, no block."""
    import os

    class UsesToolAdapter(RecordingAdapter):
        def run_turn(self, prompt, cwd, timeout=1800, *, model=None,
                    effort=None, temperature=None):
            self.calls.append({"prompt": prompt})
            # Simulate the MCP wrapper recording tool usage for this step —
            # HARN_STEP_ID/current_step resolution is exercised elsewhere
            # (test_tool_used_events.py); here we just emit the event the
            # audit consumes, scoped to this task/step.
            from harn import events as ev
            ev.emit(env, "tool_used", task_id=t.id, step_id="s1", tool="run_tests")
            return AgentResult(ok=True, text="done")

    env, t = _project(tmp_path, [
        _step("Only step", id="s1", tools=["run_tests"]),
    ])
    fake = UsesToolAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])
    loop.run(tmp_path, env)

    assert len(fake.calls) == 1
    fresh = tasks.find(env, t.id)
    assert fresh.step_results["s1"]["status"] == "ok"
    assert fresh.step_results["s1"]["usage"]["tools"]["run_tests"] == "used"

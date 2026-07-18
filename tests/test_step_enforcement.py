"""Post-step required/recommended skill+tool usage audit and retry-once
enforcement (Phase 4), sequential steps only."""
from __future__ import annotations

import subprocess
import sys

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


def test_required_and_unused_retries_once_then_blocks_workflow(tmp_path, monkeypatch):
    env, t = _project(tmp_path, [
        _step("Only step", id="s1", required=["standards"]),
    ])
    fake = RecordingAdapter(["done, but never called read_skill"])
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])
    loop.run(tmp_path, env)

    assert len(fake.calls) == 2

    fresh = tasks.find(env, t.id)
    assert fresh.step_results["s1"]["status"] == "blocked"
    assert fresh.step_results["s1"]["usage"]["skills"]["standards"] == "unused_required"
    state_dir = env / "state"
    st = state.State.load(state_dir)
    assert st.phase == state.BLOCKED
    assert state.blocked_marker(state_dir).exists()


def test_run_step_cannot_succeed_without_required_tool(tmp_path, monkeypatch):
    env, t = _project(tmp_path, [
        _step("Rate", id="s1", tools=["yahoo_finance"]),
    ])
    fake = RecordingAdapter(["EUR/USD is 1.1395 from the internet"])
    monkeypatch.setattr(loop, "_pick_adapter", lambda cfg: fake)

    result = loop.run_step(tmp_path, env, t.id, "s1")

    assert result["ok"] is False
    assert len(fake.calls) == 2
    fresh = tasks.find(env, t.id)
    assert fresh.step_results["s1"]["status"] == "blocked"
    assert fresh.step_results["s1"]["usage"]["tools"]["yahoo_finance"] == "unused_required"
    assert state.State.load(env / "state").phase == state.BLOCKED


def test_run_step_executes_required_zero_param_custom_tool_before_agent(tmp_path, monkeypatch):
    env, t = _project(tmp_path, [
        _step("Rate", id="s1", tools=["eur_usd_rate"]),
    ])
    command = f'{sys.executable} -c "print(\'1.2345\')"'
    from harn import tools
    tools.save(env, "eur_usd_rate", "Current EUR/USD rate", [], command)
    fake = RecordingAdapter(["EUR/USD is 1.2345"])
    monkeypatch.setattr(loop, "_pick_adapter", lambda cfg: fake)

    result = loop.run_step(tmp_path, env, t.id, "s1")

    assert result["ok"] is True
    assert len(fake.calls) == 1
    assert "eur_usd_rate" in fake.calls[0]["prompt"]
    assert "1.2345" in fake.calls[0]["prompt"]
    fresh = tasks.find(env, t.id)
    assert fresh.step_results["s1"]["usage"]["tools"]["eur_usd_rate"] == "used"
    used = [e for e in events.read(env, task_id=t.id)
            if e.get("event") == "tool_used"]
    assert [e.get("tool") for e in used] == ["eur_usd_rate"]


def test_full_workflow_executes_required_custom_tool_once(tmp_path, monkeypatch):
    env, t = _project(tmp_path, [
        _step("Rate", id="s1", tools=["eur_usd_rate"]),
    ])
    command = f'{sys.executable} -c "print(\'1.2345\')"'
    from harn import tools
    tools.save(env, "eur_usd_rate", "Current EUR/USD rate", [], command)
    fake = RecordingAdapter(["EUR/USD is 1.2345"])
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])

    loop.run(tmp_path, env)

    assert len(fake.calls) == 1
    assert "1.2345" in fake.calls[0]["prompt"]
    fresh = tasks.find(env, t.id)
    assert fresh.step_results["s1"]["status"] == "ok"


def test_run_step_blocks_failed_required_custom_tool_without_agent_tokens(tmp_path, monkeypatch):
    env, t = _project(tmp_path, [
        _step("Rate", id="s1", tools=["eur_usd_rate"]),
    ])
    command = f'{sys.executable} -c "import sys; print(\'provider down\'); sys.exit(9)"'
    from harn import tools
    tools.save(env, "eur_usd_rate", "Current EUR/USD rate", [], command)
    fake = RecordingAdapter(["must not run"])
    monkeypatch.setattr(loop, "_pick_adapter", lambda cfg: fake)

    result = loop.run_step(tmp_path, env, t.id, "s1")

    assert result["ok"] is False
    assert fake.calls == []
    assert "eur_usd_rate" in result["error"]
    assert "exit 9" in result["error"]
    fresh = tasks.find(env, t.id)
    assert fresh.step_results["s1"]["status"] == "blocked"
    assert fresh.step_results["s1"]["attempts"] == 0


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

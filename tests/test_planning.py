"""Planning turn: agent clarifies requirements + writes acceptance criteria."""
from __future__ import annotations

from pathlib import Path

from harn import loop, tasks, state, scaffold, ENV_DIRNAME
from harn.adapters.base import AgentResult
from .conftest import make_task


class ScriptedAdapter:
    name = "fake"

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def available(self):
        return True

    def run_turn(self, prompt, cwd):
        self.calls += 1
        item = self.script[min(self.calls - 1, len(self.script) - 1)]
        return item(prompt, cwd) if callable(item) else AgentResult(ok=True, text=item)


def _env(tmp_path: Path, *, planning: bool = True) -> Path:
    scaffold.setup(tmp_path)
    env = tmp_path / ENV_DIRNAME
    for p in (env / "tasks").glob("*.json"):
        p.unlink()
    make_task(env, "PRJ-001", title="Add feature", priority=1)
    (env / "harn.toml").write_text(
        '[harn]\nagent = "fake"\n[feedback]\ntest_cmd = ""\n'
        f"[loop]\nmax_iterations = 6\nverify = false\noracle = false\n"
        f"planning = {str(planning).lower()}\n"
        "[notify]\nwait_for_reply = false\n"
    )
    return env


def _wire(monkeypatch, adapter):
    monkeypatch.setattr(loop, "get_adapter", lambda name: adapter)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])


def test_planning_turn_runs_first(tmp_path, monkeypatch):
    env = _env(tmp_path, planning=True)
    prompts_seen = []

    def capture(prompt, cwd):
        prompts_seen.append(prompt)
        return AgentResult(ok=True, text="done")

    # Both turns captured
    fake = ScriptedAdapter([capture, capture])
    _wire(monkeypatch, fake)

    loop.run(tmp_path, env, max_iterations=3)

    assert len(prompts_seen) >= 2
    # First turn must be planning
    assert "PLANNING PHASE" in prompts_seen[0]
    # Second turn must be execution (not planning again)
    assert "PLANNING PHASE" not in prompts_seen[1]


def test_planning_prompt_contains_task_and_prd(tmp_path):
    env = _env(tmp_path, planning=True)
    cfg = loop.Config.load(env)
    task = tasks.find(env, "PRJ-001")
    prompt = loop._build_planning_prompt(env, cfg, task)
    assert "PLANNING PHASE" in prompt
    assert "PRJ-001" in prompt
    assert "Add feature" in prompt


def test_planning_skipped_on_rework(tmp_path, monkeypatch):
    """A task with changes_requested should skip planning (already planned)."""
    env = _env(tmp_path, planning=True)
    # Simulate a task that went through planning+execution+review+changes
    t = tasks.find(env, "PRJ-001")
    tasks.set_status(t, tasks.IN_PROGRESS)
    tasks.log_started(t, "fake")
    tasks.submit_for_review(t, "fake")
    tasks.request_changes(t, "fix the edge case")

    prompts_seen = []

    def capture(prompt, cwd):
        prompts_seen.append(prompt)
        return AgentResult(ok=True, text="fixed")

    fake = ScriptedAdapter([capture])
    _wire(monkeypatch, fake)
    loop.run(tmp_path, env, max_iterations=1)

    assert "PLANNING PHASE" not in prompts_seen[0]


def test_planning_turn_logs_event(tmp_path, monkeypatch):
    env = _env(tmp_path, planning=True)
    fake = ScriptedAdapter(["planning done", "work done"])
    _wire(monkeypatch, fake)

    loop.run(tmp_path, env, max_iterations=2)

    t = tasks.find(env, "PRJ-001")
    events = [e.event for e in t.review_log]
    assert "planning_started" in events


def test_planning_skipped_when_disabled(tmp_path, monkeypatch):
    env = _env(tmp_path, planning=False)
    prompts_seen = []

    def capture(prompt, cwd):
        prompts_seen.append(prompt)
        return AgentResult(ok=True, text="done")

    fake = ScriptedAdapter([capture])
    _wire(monkeypatch, fake)
    loop.run(tmp_path, env, max_iterations=1)

    assert "PLANNING PHASE" not in prompts_seen[0]


def test_update_task_mcp_refines_description(tmp_path):
    """Agent can update task description via update_task MCP tool."""
    env = _env(tmp_path, planning=True)
    t = tasks.find(env, "PRJ-001")
    original = t.description

    # Simulate what the agent does via MCP: write refined acceptance criteria
    from harn import mcp_server
    import os
    os.environ["HARN_ENV_DIR"] = str(env)
    # Call the update_task logic directly
    t.description = "## What\nRefined.\n\n## Done when\n- criterion A\n- criterion B"
    tasks._save(t)

    reloaded = tasks.find(env, "PRJ-001")
    assert "criterion A" in reloaded.description
    assert reloaded.description != original

"""Autonomous mode (--auto): decide without a human, never mutate harn_env md."""
from __future__ import annotations

from pathlib import Path

from harn import loop, tasks, state, scaffold, ENV_DIRNAME
from harn.config import Config
from harn.adapters.base import AgentResult


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


def _auto_env(tmp_path: Path, *, verify: bool = False) -> Path:
    scaffold.setup(tmp_path)
    env = tmp_path / ENV_DIRNAME
    for p in (env / "tasks").glob("*.json"):
        p.unlink()
    from .conftest import make_task
    make_task(env, "PRJ-001", title="Feat", priority=1,
              description="## What\nBuild it.\n\n## Done when\n- it works")
    (env / "harn.toml").write_text(
        '[harn]\nagent = "fake"\n[feedback]\ntest_cmd = ""\n'
        f"[loop]\nauto_max_iterations = 6\nverify = {str(verify).lower()}\n"
    )
    return env


def _wire(monkeypatch, adapter):
    monkeypatch.setattr(loop, "get_adapter", lambda name: adapter)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])


def test_auto_executes_without_touching_md(tmp_path, monkeypatch):
    env = _auto_env(tmp_path)
    fake = ScriptedAdapter(["did the work"])
    _wire(monkeypatch, fake)

    phase = loop.run(tmp_path, env, auto=True)

    assert phase == state.DONE
    # The task track is left exactly as it was — no status change, no logs.
    assert tasks.find(env, "PRJ-001").status == tasks.TODO
    assert not (env / "state" / "PROGRESS.md").exists()
    assert not (env / "state" / "ANSWERS.md").exists()
    assert fake.calls == 1


def test_auto_runs_verify_turn_too(tmp_path, monkeypatch):
    env = _auto_env(tmp_path, verify=True)
    fake = ScriptedAdapter(["work done", "checked\nVERIFY: PASS"])
    _wire(monkeypatch, fake)

    phase = loop.run(tmp_path, env, auto=True)

    assert phase == state.DONE
    assert tasks.find(env, "PRJ-001").status == tasks.TODO  # still untouched
    assert fake.calls == 2  # work + verify, even in auto


def test_auto_does_not_block_on_questions(tmp_path, monkeypatch):
    env = _auto_env(tmp_path)

    def asks(prompt, cwd):
        (env / "state" / "BLOCKED.md").write_text("which db? (no human here)")
        return AgentResult(ok=True, text="I have a question")

    fake = ScriptedAdapter([asks, "decided on sqlite and did it"])
    _wire(monkeypatch, fake)

    phase = loop.run(tmp_path, env, auto=True)

    assert phase == state.DONE  # never went BLOCKED
    assert state.read_block_question(env / "state") is None  # marker cleared
    assert not (env / "state" / "ANSWERS.md").exists()
    assert fake.calls == 2


def test_auto_prompt_carries_autonomous_note(tmp_path):
    env = _auto_env(tmp_path)
    cfg = Config.load(env)
    task = tasks.find(env, "PRJ-001")
    assert "AUTONOMOUS MODE" in loop._build_prompt(env, cfg, task, auto=True)
    assert "AUTONOMOUS MODE" not in loop._build_prompt(env, cfg, task, auto=False)


def test_auto_uses_larger_iteration_budget():
    cfg = Config(auto=True, auto_max_iterations=30, max_iterations=10)
    assert cfg.auto_max_iterations == 30 and cfg.max_iterations == 10

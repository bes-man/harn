"""The verify step: pass → review, fail → rework, ask_user → block."""
from __future__ import annotations

from pathlib import Path

from harn import loop, tasks, state, scaffold, ENV_DIRNAME
from harn.adapters.base import AgentResult


class ScriptedAdapter:
    """Returns scripted results per call. Items are strings (text) or callables
    (prompt, cwd) -> AgentResult. The last item repeats if calls overrun."""

    name = "fake"

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def available(self):
        return True

    def run_turn(self, prompt, cwd):
        self.calls += 1
        item = self.script[min(self.calls - 1, len(self.script) - 1)]
        if callable(item):
            return item(prompt, cwd)
        return AgentResult(ok=True, text=item)


def _env(tmp_path: Path) -> Path:
    scaffold.setup(tmp_path)
    env = tmp_path / ENV_DIRNAME
    for p in (env / "tasks").glob("*.json"):
        p.unlink()
    from .conftest import make_task
    make_task(env, "PRJ-001", title="Feat", priority=1,
              description="## What\nBuild it.\n\n## Done when\n- it works")
    (env / "harn.toml").write_text(
        '[harn]\nagent = "fake"\n[feedback]\ntest_cmd = ""\n'
        "[loop]\nmax_iterations = 6\nverify = true\nplanning = false\noracle = false\n"
        "[notify]\nwait_for_reply = false\n"
    )
    return env


def _wire(monkeypatch, adapter):
    monkeypatch.setattr(loop, "get_adapter", lambda name: adapter)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])


def test_verify_pass_goes_to_review(tmp_path, monkeypatch):
    env = _env(tmp_path)
    fake = ScriptedAdapter(["work done", "looks correct\nVERIFY: PASS"])
    _wire(monkeypatch, fake)

    phase = loop.run(tmp_path, env)

    assert phase == state.REVIEW
    assert tasks.find(env, "PRJ-001").status == tasks.REVIEW
    assert fake.calls == 3  # work turn + verify turn + reconcile turn


def test_verify_fail_loops_then_passes(tmp_path, monkeypatch):
    env = _env(tmp_path)
    fake = ScriptedAdapter([
        "partial work",                 # work turn 1
        "missing the X case\nVERIFY: FAIL",  # verify 1 → rework
        "added the X case",             # work turn 2
        "all good\nVERIFY: PASS",       # verify 2 → submit
    ])
    _wire(monkeypatch, fake)

    phase = loop.run(tmp_path, env)

    assert phase == state.REVIEW
    assert fake.calls == 5  # work×2 + verify×2 + reconcile×1
    # the progress log shows the verify-driven rework
    from harn import progress
    assert "verify found gaps" in progress.tail(env)


def test_verify_can_block_on_ask_user(tmp_path, monkeypatch):
    env = _env(tmp_path)

    def verify_asks(prompt, cwd):
        (env / "state" / "BLOCKED.md").write_text(
            "Context: the spec doesn't define X. Options: a) ... b) ... "
            "I recommend (a). Proceed?"
        )
        return AgentResult(ok=True, text="need a decision")

    fake = ScriptedAdapter(["work done", verify_asks])
    _wire(monkeypatch, fake)

    phase = loop.run(tmp_path, env)

    assert phase == state.BLOCKED
    # task is still mid-flight, not yet in review
    assert tasks.find(env, "PRJ-001").status == tasks.IN_PROGRESS
    assert fake.calls == 2

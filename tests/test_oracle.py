"""Oracle: independent agent verifies correctness + tech debt after verify."""
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


def _env(tmp_path: Path, *, oracle: bool = True) -> Path:
    scaffold.setup(tmp_path)
    env = tmp_path / ENV_DIRNAME
    for p in (env / "tasks").glob("*.json"):
        p.unlink()
    make_task(env, "PRJ-001", title="Add cache", priority=1)
    (env / "harn.toml").write_text(
        '[harn]\nagent = "fake"\n[feedback]\ntest_cmd = ""\n'
        f"[loop]\nmax_iterations = 8\nverify = false\n"
        f"planning = false\noracle = {str(oracle).lower()}\n"
        "[notify]\nwait_for_reply = false\n"
    )
    return env


def _wire(monkeypatch, work_adapter, oracle_adapter=None):
    monkeypatch.setattr(loop, "get_adapter", lambda name: work_adapter)
    if oracle_adapter:
        monkeypatch.setattr(loop, "_pick_oracle_adapter", lambda cfg: oracle_adapter)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])
    monkeypatch.setattr(loop, "_git_diff", lambda root: "diff --git a/cache.py\n+cache = {}")


def test_oracle_pass_goes_to_review(tmp_path, monkeypatch):
    env = _env(tmp_path, oracle=True)
    oracle = ScriptedAdapter(["work done"])
    oracle_called_prompts = []

    def oracle_turn(prompt, cwd):
        oracle_called_prompts.append(prompt)
        return AgentResult(ok=True, text="Looks good.\nORACLE: PASS")

    work = ScriptedAdapter(["implemented cache"])
    oracle.run_turn = oracle_turn
    _wire(monkeypatch, work, oracle)

    phase = loop.run(tmp_path, env)

    assert phase == state.REVIEW
    assert "ORACLE REVIEW" in oracle_called_prompts[0]
    assert tasks.find(env, "PRJ-001").status == tasks.REVIEW


def test_oracle_fail_loops_to_rework(tmp_path, monkeypatch):
    env = _env(tmp_path, oracle=True)
    oracle_calls = [0]

    def oracle_turn(prompt, cwd):
        oracle_calls[0] += 1
        if oracle_calls[0] == 1:
            return AgentResult(ok=True, text="Missing TTL handling.\nORACLE: FAIL — no TTL on cache entries")
        return AgentResult(ok=True, text="ORACLE: PASS")

    work = ScriptedAdapter(["first attempt", "fixed TTL"])
    oracle = ScriptedAdapter([])
    oracle.run_turn = oracle_turn

    _wire(monkeypatch, work, oracle)

    phase = loop.run(tmp_path, env)

    assert phase == state.REVIEW
    assert work.calls == 3           # work×2 + reconcile×1 (oracle failure re-runs work)
    assert oracle_calls[0] == 2      # oracle ran twice

    t = tasks.find(env, "PRJ-001")
    fail_events = [e for e in t.review_log if e.event == "oracle_fail"]
    assert len(fail_events) == 1
    assert "TTL" in fail_events[0].comment


def test_oracle_debt_passes_but_flags_human(tmp_path, monkeypatch):
    env = _env(tmp_path, oracle=True)

    def oracle_turn(prompt, cwd):
        return AgentResult(ok=True,
                           text="Works but dirty.\nORACLE: DEBT — cache key not namespaced")

    work = ScriptedAdapter(["done"])
    oracle = ScriptedAdapter([])
    oracle.run_turn = oracle_turn

    _wire(monkeypatch, work, oracle)

    phase = loop.run(tmp_path, env)

    # Debt doesn't block — still goes to review
    assert phase == state.REVIEW
    t = tasks.find(env, "PRJ-001")
    debt_events = [e for e in t.review_log if e.event == "oracle_debt"]
    assert len(debt_events) == 1
    assert "namespaced" in debt_events[0].comment


def test_oracle_verdict_parsing():
    assert loop._oracle_verdict("looks great\nORACLE: PASS") == ("PASS", "")
    assert loop._oracle_verdict("ORACLE: FAIL — missing TTL") == ("FAIL", "missing TTL")
    assert loop._oracle_verdict("ORACLE: DEBT — no namespacing") == ("DEBT", "no namespacing")
    assert loop._oracle_verdict("no verdict at all") == ("PASS", "")   # default pass


def test_oracle_prompt_contains_instructions_and_task(tmp_path):
    env = _env(tmp_path, oracle=True)
    cfg = loop.Config.load(env)
    task = tasks.find(env, "PRJ-001")
    prompt = loop._build_oracle_prompt(env, cfg, task, "diff content")
    assert "ORACLE REVIEW" in prompt
    assert "PRJ-001" in prompt
    assert "diff content" in prompt


def test_oracle_skipped_when_disabled(tmp_path, monkeypatch):
    env = _env(tmp_path, oracle=False)
    oracle_called = [False]

    def oracle_turn(prompt, cwd):
        oracle_called[0] = True
        return AgentResult(ok=True, text="ORACLE: PASS")

    oracle = ScriptedAdapter([])
    oracle.run_turn = oracle_turn
    work = ScriptedAdapter(["done"])
    _wire(monkeypatch, work, oracle)

    loop.run(tmp_path, env)

    assert not oracle_called[0]

"""Generic step engine helpers: per-step adapter/model resolution and the
one parameterized prompt builder that replaced the six stage-specific ones."""
from __future__ import annotations

from harn import loop, tasks, scaffold, ENV_DIRNAME
from harn.config import Config
from .conftest import make_task


def _step(**kw):
    base = {"kind": "step", "title": "Implement", "body": "One focused change.",
            "id": "step-aaaaaa", "agent": "", "model": "", "effort": "",
            "temperature": "", "required": [], "tools": [], "enabled": True}
    base.update(kw)
    return base


def test_step_overrides_from_node():
    cfg = Config()
    ov = loop._step_overrides(cfg, _step(model="opus", effort="high"))
    assert ov == {"model": "opus", "effort": "high"}


def test_step_overrides_model_falls_back_to_default():
    cfg = Config(model="sonnet")
    assert loop._step_overrides(cfg, _step()) == {"model": "sonnet"}
    assert loop._step_overrides(cfg, _step(model="opus"))["model"] == "opus"


def test_adapter_for_step_uses_step_agent(monkeypatch):
    class A: name = "claude"
    class B: name = "cursor"
    monkeypatch.setattr(loop, "get_adapter",
                        lambda n: B() if n == "cursor" else A())
    cfg = Config()
    assert loop._adapter_for_step(cfg, _step(agent="cursor"), A()).name == "cursor"
    assert loop._adapter_for_step(cfg, _step(), A()).name == "claude"
    # unknown agent name → default, never a crash
    def boom(n): raise ValueError("unknown")
    monkeypatch.setattr(loop, "get_adapter", boom)
    assert loop._adapter_for_step(cfg, _step(agent="nope"), A()).name == "claude"


def test_build_step_prompt_carries_step_and_task(tmp_path):
    scaffold.setup(tmp_path)
    env = tmp_path / ENV_DIRNAME
    for p in (env / "tasks").glob("*.json"):
        p.unlink()
    t = make_task(env, "PRJ-001", title="Feat",
                  description="## What\nBuild.\n\n## Done when\n- works")
    step = _step(title="Business requirements",
                 body="Interview stakeholders; write the BRD.",
                 required=["standards"], tools=["ask_user"])
    prompt = loop._build_step_prompt(env, Config.load(env), t, step)
    assert "Business requirements" in prompt
    assert "Interview stakeholders" in prompt
    assert "PRJ-001" in prompt
    assert "standards" in prompt          # required skill named
    assert "ask_user" in prompt           # step tool named
    assert loop._INSIGHT_NUDGE.splitlines()[0] in prompt   # insight nudge always on


def test_build_step_prompt_auto_mode_swaps_ask_for_decide(tmp_path):
    scaffold.setup(tmp_path)
    env = tmp_path / ENV_DIRNAME
    for p in (env / "tasks").glob("*.json"):
        p.unlink()
    t = make_task(env, "PRJ-001", title="Feat")
    step = _step()
    p_human = loop._build_step_prompt(env, Config.load(env), t, step, auto=False)
    p_auto = loop._build_step_prompt(env, Config.load(env), t, step, auto=True)
    assert "AUTONOMOUS MODE" in p_auto and "AUTONOMOUS MODE" not in p_human


def test_build_step_prompt_appends_feedback_tail(tmp_path):
    scaffold.setup(tmp_path)
    env = tmp_path / ENV_DIRNAME
    for p in (env / "tasks").glob("*.json"):
        p.unlink()
    t = make_task(env, "PRJ-001", title="Feat")
    p = loop._build_step_prompt(env, Config.load(env), t, _step(),
                                feedback_tail="2 tests failed")
    assert "2 tests failed" in p

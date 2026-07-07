"""Token usage capture: AgentResult helpers + Claude's JSON parsing (#8)."""
from __future__ import annotations

import json

from harn.adapters.base import AgentResult
from harn.adapters.claude import ClaudeAdapter


def test_agentresult_usage_helpers():
    r = AgentResult(ok=True, text="hi", input_tokens=1200, output_tokens=300,
                    cost_usd=0.0123)
    assert r.total_tokens == 1500
    assert "1200+300 tokens" in r.usage_str()
    assert "$0.0123" in r.usage_str()

    none = AgentResult(ok=True, text="hi")
    assert none.total_tokens is None
    assert none.usage_str() == ""


def test_claude_parses_json_usage():
    payload = json.dumps({
        "type": "result",
        "is_error": False,
        "result": "Implemented the endpoint.",
        "total_cost_usd": 0.042,
        "usage": {
            "input_tokens": 1000,
            "cache_read_input_tokens": 200,
            "output_tokens": 500,
        },
    })
    r = ClaudeAdapter._parse(True, payload, "")
    assert r.ok is True
    assert r.text == "Implemented the endpoint."
    assert r.input_tokens == 1200  # input + cache_read
    assert r.output_tokens == 500
    assert r.cost_usd == 0.042
    assert r.total_tokens == 1700


def test_claude_is_error_flag_marks_not_ok():
    payload = json.dumps({"is_error": True, "result": "boom", "usage": {}})
    r = ClaudeAdapter._parse(True, payload, "")
    assert r.ok is False
    assert r.text == "boom"


def test_claude_falls_back_on_non_json():
    r = ClaudeAdapter._parse(True, "plain text output", "some stderr")
    assert r.ok is True
    assert "plain text output" in r.text
    assert r.total_tokens is None  # no usage available


def test_prompt_is_cache_friendly_stable_first(tmp_path):
    """Headless prompt must order stable content first, volatile last, so the
    agent CLI's prefix cache reuses the maximum across turns."""
    from pathlib import Path
    from harn import scaffold, tasks, loop, ENV_DIRNAME
    from harn.config import Config
    scaffold.setup(tmp_path)
    env = tmp_path / ENV_DIRNAME
    t = tasks.create_task(env, "Add login", task_id="PRJ-001",
                          description="## What\nlogin\n## Done when\n- works")
    step = {"kind": "step", "id": "step-000001", "title": "Implement",
            "body": "do it", "required": [], "tools": [], "enabled": True}
    p = loop._build_step_prompt(env, Config.load(env), t, step,
                                feedback_tail="3 passed")
    i_skills = p.find("Available skills")        # stable
    i_task = p.find("Current task")              # task-stable
    i_board = p.find("Task board")               # volatile
    i_fb = p.find("Last feedback")               # volatile
    assert -1 < i_skills < i_task < i_board < i_fb

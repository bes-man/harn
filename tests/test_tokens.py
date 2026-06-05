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

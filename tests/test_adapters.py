"""Tests for the per-agent adapters.

These cover the agent-agnostic contract: each adapter builds the right headless
argv, reports availability from the binary on PATH, and degrades cleanly when
the CLI is missing or times out. No real agent CLI is invoked.
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from harn.adapters import get_adapter
from harn.adapters.base import AgentResult, resolve_binary


ALL_AGENTS = ["claude", "codex", "cursor", "antigravity", "qwen"]

EXPECTED_ARGV = {
    "claude": ["claude", "-p"],
    "codex": ["codex", "exec"],
    "cursor": ["cursor-agent", "-p"],
    "antigravity": ["antigravity", "exec"],
    "qwen": ["qwen", "-p"],
}


@pytest.mark.parametrize("agent", ALL_AGENTS)
def test_registry_returns_named_adapter(agent):
    adapter = get_adapter(agent)
    assert adapter.name == agent


def test_unknown_agent_raises():
    with pytest.raises(ValueError):
        get_adapter("does-not-exist")


@pytest.mark.parametrize("agent", ALL_AGENTS)
def test_missing_binary_reports_not_available(monkeypatch, agent):
    # available() also searches common install dirs beyond PATH, so a real
    # `claude` in ~/.local/bin would otherwise show up here — neutralize the
    # whole resolver to simulate "not installed anywhere".
    monkeypatch.setattr("harn.adapters.base.resolve_binary", lambda _b: None)
    monkeypatch.setattr("shutil.which", lambda _b: None)
    adapter = get_adapter(agent)
    assert adapter.available() is False
    result = adapter.run_turn("do the thing", Path("."))
    assert isinstance(result, AgentResult)
    assert result.ok is False
    assert "not found" in result.text.lower()


@pytest.mark.parametrize("agent", ALL_AGENTS)
def test_run_turn_builds_expected_argv(monkeypatch, agent):
    monkeypatch.setattr("shutil.which", lambda _b: "/usr/bin/" + _b)
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["cwd"] = kwargs.get("cwd")

        class P:
            returncode = 0
            stdout = "ok"
            stderr = ""

        return P()

    monkeypatch.setattr(subprocess, "run", fake_run)
    if agent in ("codex", "claude"):
        def fake_popen(argv, **kwargs):
            captured["argv"] = argv
            captured["cwd"] = kwargs.get("cwd")

            if agent == "claude":
                output = [json.dumps({"type": "result", "result": "ok"}) + "\n"]
            else:
                output = [
                    json.dumps({"type": "item.completed", "item": {
                        "id": "m1", "type": "agent_message", "text": "ok"}}) + "\n",
                    json.dumps({"type": "turn.completed", "usage": {}}) + "\n",
                ]

            class P:
                returncode = 0
                stdout = iter(output)
                stderr = iter(())

                def wait(self, timeout=None):
                    return 0

                def kill(self):
                    self.returncode = -9

            return P()

        monkeypatch.setattr(subprocess, "Popen", fake_popen)
    adapter = get_adapter(agent)
    result = adapter.run_turn("PROMPT", Path("/tmp/proj"))

    assert result.ok is True
    assert result.text == "ok"
    expected = EXPECTED_ARGV[agent]
    assert captured["argv"][1: len(expected)] == expected[1:]
    assert captured["argv"][0].endswith(expected[0])
    assert "PROMPT" in captured["argv"]
    assert captured["cwd"] == "/tmp/proj"


def test_claude_headless_bypasses_permissions_so_tools_can_run(monkeypatch):
    # Regression: headless `claude -p` denies every tool call without a human
    # to approve it, so a task needing MCP/Bash/Edit tools produced ZERO
    # tool_used events and burned its whole budget saying "I need permission".
    # The adapter must pass --permission-mode bypassPermissions.
    monkeypatch.setattr("shutil.which", lambda _b: "/usr/bin/" + _b)
    captured = {}

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv

        class P:
            returncode = 0
            stdout = iter([json.dumps({"type": "result", "result": "ok"}) + "\n"])
            stderr = iter(())
            def wait(self, timeout=None): return 0
            def kill(self): self.returncode = -9
        return P()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    get_adapter("claude").run_turn("PROMPT", Path("/tmp/proj"))
    argv = captured["argv"]
    assert "--permission-mode" in argv
    assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"


def test_claude_streams_agent_activity_before_the_final_result(monkeypatch):
    """Claude's normal json mode buffers everything until completion.  Harn
    must request stream-json so the sidebar receives live activity."""
    monkeypatch.setattr("shutil.which", lambda _b: "/usr/bin/claude")
    captured = {}

    class P:
        returncode = 0
        stdout = iter([
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": "Checking the source…"},
                {"type": "tool_use", "name": "city_weather"},
            ]}}) + "\n",
            json.dumps({"type": "result", "result": "Done", "usage": {
                "input_tokens": 3, "output_tokens": 2}}) + "\n",
        ])
        stderr = iter(())

        def wait(self, timeout=None):
            return 0

        def kill(self):
            self.returncode = -9

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        return P()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    seen = []
    result = get_adapter("claude").run_turn("PROMPT", Path("/tmp/proj"), on_event=seen.append)

    assert result.ok is True and result.text == "Done"
    assert captured["argv"][captured["argv"].index("--output-format") + 1] == "stream-json"
    assert "--verbose" in captured["argv"]
    assert any(e["kind"] == "message" and "Checking" in e["text"] for e in seen)
    assert any(e["kind"] == "tool" and e["title"] == "city_weather" for e in seen)


def test_cursor_headless_trusts_harn_workspace_without_prompting(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _b: "/usr/bin/" + _b)
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv

        class P:
            returncode = 0
            stdout = "ok"
            stderr = ""
        return P()

    monkeypatch.setattr(subprocess, "run", fake_run)
    get_adapter("cursor").run_turn("PROMPT", Path("/tmp/harn-wave/step-1"))
    argv = captured["argv"]
    assert "--trust" in argv
    assert argv.index("--trust") < argv.index("PROMPT")


def test_claude_reports_cache_read_tokens_separately(monkeypatch):
    # The budget guard excludes cache reads; the adapter must surface them.
    monkeypatch.setattr("shutil.which", lambda _b: "/usr/bin/" + _b)
    import json as _json

    def fake_popen(argv, **kwargs):
        class P:
            returncode = 0
            stdout = iter([_json.dumps({
                "type": "result",
                "result": "done",
                "usage": {"input_tokens": 100, "output_tokens": 20,
                          "cache_creation_input_tokens": 5000,
                          "cache_read_input_tokens": 400000},
                "total_cost_usd": 0.12,
            }) + "\n"])
            stderr = iter(())
            def wait(self, timeout=None): return 0
            def kill(self): self.returncode = -9
        return P()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    res = get_adapter("claude").run_turn("PROMPT", Path("/tmp/proj"))
    # input_tokens folds all three (display continuity); cache_read is ALSO
    # surfaced on its own so the budget can subtract it.
    assert res.input_tokens == 100 + 5000 + 400000
    assert res.cache_read_tokens == 400000
    assert res.total_tokens == 100 + 5000 + 400000 + 20


@pytest.mark.parametrize("agent", ALL_AGENTS)
def test_timeout_is_reported(monkeypatch, agent):
    monkeypatch.setattr("shutil.which", lambda _b: "/usr/bin/" + _b)

    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout", 0))

    monkeypatch.setattr(subprocess, "run", fake_run)
    timeout = 1
    if agent in ("codex", "claude"):
        class SlowOutput:
            def __iter__(self):
                time.sleep(0.05)
                return
                yield  # pragma: no cover

        class P:
            returncode = None
            stdout = SlowOutput()
            stderr = iter(())

            def wait(self, timeout=None):
                return self.returncode or -9

            def kill(self):
                self.returncode = -9

        monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: P())
        timeout = 0.01
    adapter = get_adapter(agent)
    result = adapter.run_turn("PROMPT", Path("."), timeout=timeout)
    assert result.ok is False
    assert "timed out" in result.text.lower()


# --- binary resolution beyond PATH (stripped-PATH / GUI-launched processes) - #

def test_resolve_binary_prefers_path(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _b: "/usr/bin/" + _b)
    assert resolve_binary("claude") == "/usr/bin/claude"


def test_resolve_binary_falls_back_to_common_dir(monkeypatch, tmp_path):
    # not on PATH, but installed in one of the extra dirs harn searches
    monkeypatch.setattr("shutil.which", lambda _b: None)
    fake = tmp_path / "claude"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setattr("harn.adapters.base._EXTRA_BIN_DIRS", (str(tmp_path),))
    assert resolve_binary("claude") == str(fake)


def test_resolve_binary_none_when_absent(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _b: None)
    monkeypatch.setattr("harn.adapters.base._EXTRA_BIN_DIRS", ())
    assert resolve_binary("claude") is None
    assert resolve_binary("") is None


def test_exec_uses_absolute_path_when_off_path(monkeypatch, tmp_path):
    """A CLI found only via the extra-dir search must be launched by its
    absolute path — subprocess.run resolves argv[0] via PATH only."""
    fake = tmp_path / "claude"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setattr("shutil.which", lambda _b: None)
    monkeypatch.setattr("harn.adapters.base._EXTRA_BIN_DIRS", (str(tmp_path),))
    captured = {}

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv

        class P:
            returncode = 0
            stdout = iter([json.dumps({"type": "result", "result": "ok"}) + "\n"])
            stderr = iter(())
            def wait(self, timeout=None): return 0
            def kill(self): self.returncode = -9

        return P()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    get_adapter("claude").run_turn("PROMPT", Path("."))
    assert captured["argv"][0] == str(fake)


# ---------------------------------------------------------------------------
# ClaudeAdapter._normalize_event — the tool_result capture extension (task
# context markdown + real capture design, spec B): a `type: "user"` stream
# record carries the actual RETURN VALUE of a tool call, correlated back to
# the tool's name via the id a preceding `type: "assistant"` tool_use event
# registered in the shared `name_by_id` map.
# ---------------------------------------------------------------------------
from harn.adapters.claude import ClaudeAdapter


def test_assistant_tool_use_registers_id_to_name():
    name_by_id: dict = {}
    events = ClaudeAdapter._normalize_event({
        "type": "assistant",
        "message": {"content": [
            {"type": "tool_use", "id": "toolu_1", "name": "read_skill"},
        ]},
    }, name_by_id)
    assert name_by_id == {"toolu_1": "read_skill"}
    assert events == [{"kind": "tool", "phase": "started",
                       "title": "read_skill", "text": ""}]


def test_user_tool_result_resolves_name_via_id_correlation():
    name_by_id = {"toolu_1": "read_skill"}
    events = ClaudeAdapter._normalize_event({
        "type": "user",
        "message": {"content": [
            {"type": "tool_result", "tool_use_id": "toolu_1",
             "content": [{"type": "text", "text": "skill body here"}]},
        ]},
    }, name_by_id)
    assert len(events) == 1
    assert events[0]["kind"] == "tool_result"
    assert events[0]["title"] == "read_skill"
    assert events[0]["text"] == "skill body here"


def test_tool_result_unknown_id_falls_back_to_generic_name():
    events = ClaudeAdapter._normalize_event({
        "type": "user",
        "message": {"content": [
            {"type": "tool_result", "tool_use_id": "toolu_missing",
             "content": "plain string result"},
        ]},
    }, {})
    assert events[0]["title"] == "Tool"
    assert events[0]["text"] == "plain string result"


def test_tool_result_over_cap_is_truncated_with_marker():
    huge = "x" * 5000
    events = ClaudeAdapter._normalize_event({
        "type": "user",
        "message": {"content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": huge},
        ]},
    }, {"toolu_1": "run_tests"})
    text = events[0]["text"]
    assert len(text) < 5000
    assert text.endswith("(truncated)")
    assert text.startswith("x" * 100)


def test_tool_result_under_cap_is_untouched():
    events = ClaudeAdapter._normalize_event({
        "type": "user",
        "message": {"content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": "short"},
        ]},
    }, {"toolu_1": "run_tests"})
    assert events[0]["text"] == "short"
    assert "truncated" not in events[0]["text"]


def test_assistant_text_and_tool_use_full_turn_sequence():
    """A realistic mini turn: text, then a tool call, then its result — the
    id→name map built up across the whole sequence, as loop._run_turn's
    on_event would see it record-by-record."""
    name_by_id: dict = {}
    all_events = []
    all_events += ClaudeAdapter._normalize_event({
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "Let me check that."}]},
    }, name_by_id)
    all_events += ClaudeAdapter._normalize_event({
        "type": "assistant",
        "message": {"content": [
            {"type": "tool_use", "id": "toolu_9", "name": "read_service"}]},
    }, name_by_id)
    all_events += ClaudeAdapter._normalize_event({
        "type": "user",
        "message": {"content": [
            {"type": "tool_result", "tool_use_id": "toolu_9",
             "content": [{"type": "text", "text": "service body"}]}]},
    }, name_by_id)
    kinds = [e["kind"] for e in all_events]
    assert kinds == ["message", "tool", "tool_result"]
    assert all_events[2]["title"] == "read_service"
    assert all_events[2]["text"] == "service body"


def test_non_assistant_non_user_record_yields_no_events():
    assert ClaudeAdapter._normalize_event({"type": "result"}, {}) == []
    assert ClaudeAdapter._normalize_event({"type": "system"}, {}) == []

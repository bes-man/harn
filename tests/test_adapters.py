"""Tests for the per-agent adapters.

These cover the agent-agnostic contract: each adapter builds the right headless
argv, reports availability from the binary on PATH, and degrades cleanly when
the CLI is missing or times out. No real agent CLI is invoked.
"""
from __future__ import annotations

import subprocess
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
    adapter = get_adapter(agent)
    result = adapter.run_turn("PROMPT", Path("/tmp/proj"))

    assert result.ok is True
    assert result.text == "ok"
    assert captured["argv"][: len(EXPECTED_ARGV[agent])] == EXPECTED_ARGV[agent]
    assert "PROMPT" in captured["argv"]
    assert captured["cwd"] == "/tmp/proj"


def test_claude_headless_bypasses_permissions_so_tools_can_run(monkeypatch):
    # Regression: headless `claude -p` denies every tool call without a human
    # to approve it, so a task needing MCP/Bash/Edit tools produced ZERO
    # tool_used events and burned its whole budget saying "I need permission".
    # The adapter must pass --permission-mode bypassPermissions.
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
    get_adapter("claude").run_turn("PROMPT", Path("/tmp/proj"))
    argv = captured["argv"]
    assert "--permission-mode" in argv
    assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"


def test_claude_reports_cache_read_tokens_separately(monkeypatch):
    # The budget guard excludes cache reads; the adapter must surface them.
    monkeypatch.setattr("shutil.which", lambda _b: "/usr/bin/" + _b)
    import json as _json

    def fake_run(argv, **kwargs):
        class P:
            returncode = 0
            stdout = _json.dumps({
                "result": "done",
                "usage": {"input_tokens": 100, "output_tokens": 20,
                          "cache_creation_input_tokens": 5000,
                          "cache_read_input_tokens": 400000},
                "total_cost_usd": 0.12,
            })
            stderr = ""
        return P()

    monkeypatch.setattr(subprocess, "run", fake_run)
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
    adapter = get_adapter(agent)
    result = adapter.run_turn("PROMPT", Path("."), timeout=1)
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

    def fake_run(argv, **kwargs):
        captured["argv"] = argv

        class P:
            returncode = 0
            stdout = "ok"
            stderr = ""

        return P()

    monkeypatch.setattr(subprocess, "run", fake_run)
    get_adapter("claude").run_turn("PROMPT", Path("."))
    assert captured["argv"][0] == str(fake)

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


# --- cheap auth preflight -------------------------------------------------- #
# Diagnosed live: a machine where the Claude DESKTOP app worked fine (it
# refreshes its token in-process and never writes it out) while every
# `claude -p` subprocess failed, because the CLI itself was logged out.
# "Claude works in my console" and "harn can run Claude" are genuinely
# different questions; harn always shells out, so only the CLI's own state
# answers the second, and asking it costs nothing.

def _auth_json(monkeypatch, payload, *, stdout=None):
    """Stub the CLI's own `auth status --json` answer."""
    from harn.adapters.claude import ClaudeAdapter
    from harn.adapters import base as base_mod
    monkeypatch.setattr(base_mod, "resolve_binary", lambda b: "/fake/claude")
    text = stdout if stdout is not None else json.dumps(payload)
    monkeypatch.setattr(ClaudeAdapter, "_exec",
                        lambda self, argv, cwd, timeout=10:
                            base_mod._Exec(ok=True, stdout=text, stderr=""))
    return ClaudeAdapter()


def test_claude_auth_status_reports_a_live_session(monkeypatch):
    a = _auth_json(monkeypatch, {"loggedIn": True, "authMethod": "subscription"})
    assert a.auth_status() == ("ok", "subscription")


def test_claude_auth_status_reports_a_logged_out_cli(monkeypatch):
    a = _auth_json(monkeypatch, {"loggedIn": False, "authMethod": "none"})
    state, detail = a.auth_status()
    assert state == "expired"
    assert "not signed in" in detail


def test_an_older_cli_without_the_subcommand_is_unknown_not_expired(monkeypatch):
    """Unparseable output is NOT proof of trouble — an older CLI simply may
    not have `auth status`. Crying wolf on a working setup would make the
    check worse than useless."""
    a = _auth_json(monkeypatch, None, stdout="error: unknown command 'auth'")
    assert a.auth_status() == ("unknown", "")


def test_a_missing_binary_is_unknown(monkeypatch):
    from harn.adapters.claude import ClaudeAdapter
    from harn.adapters import base as base_mod
    monkeypatch.setattr(base_mod, "resolve_binary", lambda b: None)
    assert ClaudeAdapter().auth_status() == ("unknown", "")


def test_auth_status_asks_the_cli_rather_than_parsing_its_credential_file(monkeypatch):
    """`claude setup-token` stores a long-lived subscription token in a
    different shape than an interactive login, so hand-parsing
    ~/.claude/.credentials.json would report a perfectly good setup-token
    login as broken. Ask the CLI instead."""
    seen = {}
    from harn.adapters.claude import ClaudeAdapter
    from harn.adapters import base as base_mod
    monkeypatch.setattr(base_mod, "resolve_binary", lambda b: "/fake/claude")
    def fake_exec(self, argv, cwd, timeout=10):
        seen["argv"] = argv
        return base_mod._Exec(ok=True, stdout=json.dumps({"loggedIn": True}), stderr="")
    monkeypatch.setattr(ClaudeAdapter, "_exec", fake_exec)
    ClaudeAdapter().auth_status()
    assert seen["argv"][1:] == ["auth", "status", "--json"]


def test_the_base_adapter_answers_unknown_rather_than_guessing():
    """Agent-agnostic default: most CLIs expose no cheap way to ask, and
    probing with a real turn would burn quota on every page load."""
    from harn.adapters.base import Adapter
    class Bare(Adapter):
        name = "bare"
        binary = "bare"
        def run_turn(self, prompt, cwd, timeout=1800, **kw):
            raise NotImplementedError
    assert Bare().auth_status() == ("unknown", "")


def test_claude_model_catalog_is_current():
    from harn.adapters.claude import ClaudeAdapter
    models = ClaudeAdapter.MODELS
    assert "claude-opus-5" in models
    assert "claude-sonnet-5" in models
    assert "claude-fable-5" in models
    # Aliases track whatever is current for the account, so they must stay.
    assert {"opus", "sonnet", "haiku"} <= set(models)
    # Superseded pins must not linger — they'd be offered in Studio's picker
    # long after they stopped being the right default.
    assert "claude-opus-4-8" not in models


# --- harn login: harn runs the flow, the human approves -------------------- #

def test_claude_login_uses_the_long_lived_token_not_a_session_login():
    """Both work on a subscription, but `auth login` mints the same
    short-lived session that expires in days and strands the next background
    run. `setup-token` is the documented long-lived one — picking it here is
    the difference between signing in once and signing in every few days."""
    from harn.adapters.claude import ClaudeAdapter
    from harn.adapters import base as base_mod
    import unittest.mock as mock
    with mock.patch.object(base_mod, "resolve_binary", lambda b: "/fake/claude"):
        assert ClaudeAdapter().login_command() == ["/fake/claude", "setup-token"]


def test_login_command_is_none_when_the_binary_is_missing():
    from harn.adapters.claude import ClaudeAdapter
    from harn.adapters import base as base_mod
    import unittest.mock as mock
    with mock.patch.object(base_mod, "resolve_binary", lambda b: None):
        assert ClaudeAdapter().login_command() is None


def test_the_base_adapter_admits_it_does_not_know_a_login():
    """Agent-agnostic: harn must not invent a login command for a CLI it
    doesn't know, it must say so."""
    from harn.adapters.base import Adapter
    class Bare(Adapter):
        name = "bare"
        binary = "bare"
        def run_turn(self, prompt, cwd, timeout=1800, **kw):
            raise NotImplementedError
    assert Bare().login_command() is None


# --- logout + per-agent auth, verified against the real CLIs --------------- #
# Every command below was confirmed by running the CLI's own --help on a
# machine that has them installed — not guessed from convention.

def _stub(monkeypatch, cls, stdout="", stderr=""):
    from harn.adapters import base as base_mod
    monkeypatch.setattr(base_mod, "resolve_binary", lambda b: f"/fake/{b}")
    monkeypatch.setattr(cls, "_exec",
                        lambda self, argv, cwd, timeout=10:
                            base_mod._Exec(ok=True, stdout=stdout, stderr=stderr))
    return cls()


def test_claude_logout_command():
    from harn.adapters.claude import ClaudeAdapter
    from harn.adapters import base as base_mod
    import unittest.mock as mock
    with mock.patch.object(base_mod, "resolve_binary", lambda b: "/fake/claude"):
        assert ClaudeAdapter().logout_command() == ["/fake/claude", "auth", "logout"]


def test_codex_reads_its_status_from_stderr(monkeypatch):
    """Verified live: `codex login status` writes its verdict to STDERR and
    leaves stdout empty. Reading only stdout reported a signed-in CLI as
    "unknown"."""
    from harn.adapters.codex import CodexAdapter
    a = _stub(monkeypatch, CodexAdapter, stdout="", stderr="Logged in using ChatGPT\n")
    assert a.auth_status() == ("ok", "Logged in using ChatGPT")


def test_codex_reports_a_signed_out_cli(monkeypatch):
    from harn.adapters.codex import CodexAdapter
    a = _stub(monkeypatch, CodexAdapter, stderr="Not logged in\n")
    state, _ = a.auth_status()
    assert state == "expired"


def test_cursor_reports_the_signed_in_account(monkeypatch):
    """This CLI names the account, which is worth surfacing when several are
    in play."""
    from harn.adapters.cursor import CursorAdapter
    a = _stub(monkeypatch, CursorAdapter,
              stdout=json.dumps({"isAuthenticated": True,
                                 "userInfo": {"email": "me@example.com"}}))
    assert a.auth_status() == ("ok", "me@example.com")


def test_cursor_reports_a_signed_out_cli(monkeypatch):
    from harn.adapters.cursor import CursorAdapter
    a = _stub(monkeypatch, CursorAdapter, stdout=json.dumps({"isAuthenticated": False}))
    assert a.auth_status()[0] == "expired"


def test_an_agent_with_no_known_auth_commands_says_so():
    """qwen's CLI reports its auth subcommand as removed, so harn must not
    invent one — Settings shows "no login command known" rather than a button
    that can't work."""
    from harn.adapters.qwen import QwenAdapter
    a = QwenAdapter()
    assert a.login_command() is None and a.logout_command() is None
    assert a.auth_status() == ("unknown", "")

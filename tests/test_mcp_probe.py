"""HARN_MCP_PROBE: a healthcheck subprocess must not mint a "chat" run_id.

Regression coverage for a live incident: the studio's MCP health badge polls
every 1.5s, each poll spawning `mcp_server.healthcheck()`'s `python -m harn
mcp` subprocess. Before this fix, that subprocess always called
`build_server(start_watch=True)`, which mints a fresh `events.new_run(kind=
"chat")` on every single poll -- flooding events.jsonl and clobbering the
shared "current run" pointer that `progress_payload()` uses to find the
active stage. The result: the Flow tab's live progress got stuck showing
"starting..." forever, even while a real background run was actively
executing turns under a different (correct) run_id that never won the
"most recent run_start" race.
"""
from __future__ import annotations

from harn import mcp_server


def test_serve_disables_start_watch_when_probe_env_set(monkeypatch):
    monkeypatch.setenv("HARN_MCP_PROBE", "1")
    calls = []

    class FakeMCP:
        def run(self, **kw):
            pass

    def fake_build_server(start_watch=True, register_custom=True):
        calls.append(start_watch)
        return FakeMCP()

    monkeypatch.setattr(mcp_server, "build_server", fake_build_server)
    mcp_server.serve()
    assert calls == [False]


def test_serve_keeps_start_watch_true_for_a_real_session(monkeypatch):
    monkeypatch.delenv("HARN_MCP_PROBE", raising=False)
    calls = []

    class FakeMCP:
        def run(self, **kw):
            pass

    def fake_build_server(start_watch=True, register_custom=True):
        calls.append(start_watch)
        return FakeMCP()

    monkeypatch.setattr(mcp_server, "build_server", fake_build_server)
    mcp_server.serve()
    assert calls == [True]


def test_healthcheck_marks_its_subprocess_as_a_probe(monkeypatch, tmp_path):
    captured = {}

    class FakeResult:
        stdout = ""

    def fake_run(cmd, input, capture_output, text, timeout, env):
        captured["env"] = env
        return FakeResult()

    monkeypatch.setattr("subprocess.run", fake_run)
    mcp_server.healthcheck(tmp_path, timeout=5)
    assert captured["env"].get("HARN_MCP_PROBE") == "1"


def test_probe_serve_never_touches_the_shared_run_id(monkeypatch, tmp_path):
    """End-to-end (within-process) proof: a probe-flagged serve() call must
    leave events.new_run/.run_id completely untouched, unlike a real session."""
    from harn import events as events_mod

    env_dir = tmp_path / "harn_env"
    (env_dir / "state").mkdir(parents=True)
    (env_dir / "tools").mkdir(parents=True)
    monkeypatch.setenv("HARN_ENV_DIR", str(env_dir))
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "yes")  # guards _ensure_watch_running

    class FakeMCP:
        def run(self, **kw):
            pass

    real_build_server = mcp_server.build_server

    def spying_build_server(start_watch=True, register_custom=True):
        real_build_server(start_watch=start_watch, register_custom=False)
        return FakeMCP()

    monkeypatch.setattr(mcp_server, "build_server", spying_build_server)

    before = events_mod.current_run(env_dir)
    monkeypatch.setenv("HARN_MCP_PROBE", "1")
    mcp_server.serve()
    assert events_mod.current_run(env_dir) == before  # untouched by the probe

    monkeypatch.delenv("HARN_MCP_PROBE", raising=False)
    mcp_server.serve()
    assert events_mod.current_run(env_dir) != before  # a REAL session still mints one

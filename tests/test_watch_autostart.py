"""MCP server auto-starts harn watch on build_server()."""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch, MagicMock

from harn.mcp_server import _ensure_watch_running


# `_ensure_watch_running` short-circuits under the test suite (PYTEST_CURRENT_TEST
# is set automatically by pytest) so tests calling build_server() directly can
# never leak a real detached daemon (see the function's own docstring for why
# this matters). These tests exercise its REAL PID-file logic — with
# subprocess.Popen mocked, so there's no real-leak risk — by explicitly
# clearing that env var first.

def test_starts_watch_when_no_pid_file(tmp_path, monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    env = tmp_path / "harn_env"
    state_dir = env / "state"
    state_dir.mkdir(parents=True)

    started = []

    class FakeProc:
        pid = 12345

    def fake_popen(cmd, **kw):
        started.append(cmd)
        return FakeProc()

    with patch("harn.mcp_server.subprocess.Popen", side_effect=fake_popen):
        _ensure_watch_running(env)

    assert len(started) == 1
    assert "watch" in started[0]
    assert (state_dir / "watch.pid").read_text() == "12345"


def test_skips_if_already_running(tmp_path, monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    env = tmp_path / "harn_env"
    state_dir = env / "state"
    state_dir.mkdir(parents=True)
    pid_file = state_dir / "watch.pid"
    pid_file.write_text(str(os.getpid()))  # current process is "running"

    started = []
    with patch("harn.mcp_server.subprocess.Popen", side_effect=lambda *a, **k: started.append(a)):
        _ensure_watch_running(env)

    assert started == []


def test_restarts_if_pid_dead(tmp_path, monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    env = tmp_path / "harn_env"
    state_dir = env / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "watch.pid").write_text("999999999")  # dead PID

    class FakeProc:
        pid = 42

    started = []

    def fake_popen(cmd, **kw):
        started.append(cmd)
        return FakeProc()

    with patch("harn.mcp_server.subprocess.Popen", side_effect=fake_popen):
        _ensure_watch_running(env)

    assert len(started) == 1
    assert (state_dir / "watch.pid").read_text() == "42"


def test_never_raises_on_popen_failure(tmp_path, monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    env = tmp_path / "harn_env"
    (env / "state").mkdir(parents=True)

    with patch("harn.mcp_server.subprocess.Popen", side_effect=OSError("no python")):
        _ensure_watch_running(env)  # must not raise


def test_skipped_entirely_under_pytest_even_with_no_pid_file(tmp_path):
    """The actual regression guard: under pytest (PYTEST_CURRENT_TEST set,
    which it always is during a test run and this test does NOT clear it),
    _ensure_watch_running must return immediately WITHOUT ever touching
    subprocess.Popen — proving a test that forgets to mock Popen entirely
    (like the real-world bug this fixes) still can't leak a process."""
    env = tmp_path / "harn_env"
    (env / "state").mkdir(parents=True)
    assert os.environ.get("PYTEST_CURRENT_TEST")  # sanity: pytest set it
    with patch("harn.mcp_server.subprocess.Popen") as mock_popen:
        _ensure_watch_running(env)
        mock_popen.assert_not_called()


# --- build_server(start_watch=) / tool_catalog() must NOT leak a daemon ---- #
#
# A studio UI page load (harn/studio.py's tools_catalog_payload -> tool_catalog)
# used to call build_server() unconditionally, which unconditionally called
# _ensure_watch_running() -- silently spawning a REAL, fully-detached `harn
# watch` background process (start_new_session=True, survives the studio
# server, the browser, everything but an explicit kill) every time the Tools
# tab loaded, for whatever env_dir happened to be active. Across a long
# session touching many scratch/preview projects this leaked dozens of
# permanent daemons, one of which was still polling a real project with the
# default "claude" agent and silently spent real API cost via a live oracle
# turn. tool_catalog() only needs the registered tools' docstrings -- it must
# never have this side effect.

def test_tool_catalog_does_not_start_watch(tmp_path, monkeypatch):
    from harn import mcp_server
    mcp_server._catalog_cache = None   # the cache is a module global; reset it
    env = tmp_path / "harn_env"
    (env / "state").mkdir(parents=True)
    monkeypatch.setenv("HARN_ENV_DIR", str(env))
    started = []
    monkeypatch.setattr(mcp_server, "_ensure_watch_running",
                        lambda *a, **k: started.append(True))
    mcp_server.tool_catalog()
    assert started == []
    mcp_server._catalog_cache = None   # don't leak the cache into other tests


def test_build_server_default_still_starts_watch(tmp_path, monkeypatch):
    """The REAL entry point (`harn mcp` / an actual agent session) must keep
    auto-starting watch — only the introspection-only tool_catalog() path
    opts out. Default behavior is unchanged."""
    from harn import mcp_server
    env = tmp_path / "harn_env"
    (env / "state").mkdir(parents=True)
    monkeypatch.setenv("HARN_ENV_DIR", str(env))
    started = []
    monkeypatch.setattr(mcp_server, "_ensure_watch_running",
                        lambda *a, **k: started.append(True))
    mcp_server.build_server()
    assert started == [True]

"""MCP server auto-starts harn watch on build_server()."""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch, MagicMock

from harn.mcp_server import _ensure_watch_running


def test_starts_watch_when_no_pid_file(tmp_path):
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


def test_skips_if_already_running(tmp_path):
    env = tmp_path / "harn_env"
    state_dir = env / "state"
    state_dir.mkdir(parents=True)
    pid_file = state_dir / "watch.pid"
    pid_file.write_text(str(os.getpid()))  # current process is "running"

    started = []
    with patch("harn.mcp_server.subprocess.Popen", side_effect=lambda *a, **k: started.append(a)):
        _ensure_watch_running(env)

    assert started == []


def test_restarts_if_pid_dead(tmp_path):
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


def test_never_raises_on_popen_failure(tmp_path):
    env = tmp_path / "harn_env"
    (env / "state").mkdir(parents=True)

    with patch("harn.mcp_server.subprocess.Popen", side_effect=OSError("no python")):
        _ensure_watch_running(env)  # must not raise

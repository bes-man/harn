"""Every MCP tool call emits a tool_used event scoped to the currently
claimed task and (for a sequential step) the currently running step."""
import os
from pathlib import Path

from harn import events, mcp_server, state, tasks


def _env(tmp_path, monkeypatch):
    project_root = tmp_path / "proj"
    project_root.mkdir()
    env = project_root / "harn_env"
    env.mkdir()
    monkeypatch.setenv("HARN_ENV_DIR", str(env))
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "yes")  # never auto-start watch
    return env


def test_every_tool_call_emits_tool_used_tagged_to_current_task_and_step(
        tmp_path, monkeypatch):
    env = _env(tmp_path, monkeypatch)
    task = tasks.create_task(env, "Do the thing")
    state_dir = env / "state"
    st = state.State(current_task=task.id, current_step="s1")
    st.save(state_dir)

    server = mcp_server.build_server(start_watch=False)
    tool = server._tool_manager._tools["list_skills"]
    tool.fn()

    evs = [e for e in events.read(env, task_id=task.id) if e["event"] == "tool_used"]
    assert len(evs) == 1
    assert evs[0]["tool"] == "list_skills"
    assert evs[0]["step_id"] == "s1"


def test_tool_call_with_no_claimed_task_emits_nothing(tmp_path, monkeypatch):
    env = _env(tmp_path, monkeypatch)
    server = mcp_server.build_server(start_watch=False)
    tool = server._tool_manager._tools["list_skills"]
    tool.fn()
    assert events.read(env) == []


def test_tool_call_uses_harn_step_id_env_over_state_current_step(
        tmp_path, monkeypatch):
    env = _env(tmp_path, monkeypatch)
    task = tasks.create_task(env, "Do the thing")
    state_dir = env / "state"
    st = state.State(current_task=task.id, current_step="s1")
    st.save(state_dir)
    monkeypatch.setenv("HARN_STEP_ID", "wave-member-2")

    server = mcp_server.build_server(start_watch=False)
    tool = server._tool_manager._tools["list_skills"]
    tool.fn()

    evs = [e for e in events.read(env, task_id=task.id) if e["event"] == "tool_used"]
    assert len(evs) == 1
    assert evs[0]["step_id"] == "wave-member-2"

"""Every MCP tool call emits a tool_used event scoped to the currently
claimed task and (for a sequential step) the currently running step."""
import asyncio
import os
from pathlib import Path

from harn import events, mcp_server, state, tasks, tools


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


def test_custom_tool_call_emits_tool_used_scoped_like_builtins(
        tmp_path, monkeypatch):
    """Custom tools register via mcp.add_tool (bypassing the mcp.tool usage
    wrapper), so the synthesized function must emit the tool_used event itself
    — otherwise Phase 4 usage badges always show custom tools as unused."""
    env = _env(tmp_path, monkeypatch)
    task = tasks.create_task(env, "Do the thing")
    state_dir = env / "state"
    st = state.State(current_task=task.id, current_step="s1")
    st.save(state_dir)
    tools.save(env, "echo_it", "echo a message", ["msg"], "echo {msg}")

    server = mcp_server.build_server(start_watch=False)
    tool = server._tool_manager.get_tool("echo_it")
    asyncio.run(tool.run({"msg": "hello"}))

    evs = [e for e in events.read(env, task_id=task.id) if e["event"] == "tool_used"]
    assert len(evs) == 1
    assert evs[0]["tool"] == "echo_it"
    assert evs[0]["step_id"] == "s1"


def test_parallel_custom_tool_uses_connector_task_and_step_env(tmp_path, monkeypatch):
    env = _env(tmp_path, monkeypatch)
    task = tasks.create_task(env, "Parallel tool")
    state.State().save(env / "state")  # parallel wave has no shared current_task
    monkeypatch.setenv("HARN_TASK_ID", task.id)
    monkeypatch.setenv("HARN_STEP_ID", "step-weather")
    tools.save(env, "city_weather", "weather", ["city"], "echo {city}")

    server = mcp_server.build_server(start_watch=False)
    asyncio.run(server._tool_manager.get_tool("city_weather").run({"city": "Madrid"}))

    evs = [e for e in events.read(env, task_id=task.id) if e["event"] == "tool_used"]
    assert [(e["tool"], e["step_id"]) for e in evs] == [("city_weather", "step-weather")]

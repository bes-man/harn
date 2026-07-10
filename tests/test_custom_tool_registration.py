"""A saved custom tool is dynamically registered and callable via the real
FastMCP server, alongside the ~35 built-in tools."""
import asyncio

from harn import mcp_server, tools


def _env(tmp_path, monkeypatch):
    project_root = tmp_path / "proj"
    project_root.mkdir()
    env = project_root / "harn_env"
    env.mkdir()
    monkeypatch.setenv("HARN_ENV_DIR", str(env))
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "yes")
    return env


def test_custom_tool_is_registered_and_callable(tmp_path, monkeypatch):
    env = _env(tmp_path, monkeypatch)
    tools.save(env, "echo_it", "echo a message", ["msg"], "echo {msg}")
    server = mcp_server.build_server(start_watch=False)
    registered = asyncio.run(server.list_tools())
    names = {t.name for t in registered}
    assert "echo_it" in names
    assert "list_skills" in names  # a built-in is still present alongside it

    tool = server._tool_manager.get_tool("echo_it")
    result = asyncio.run(tool.run({"msg": "hello"}))
    assert "hello" in str(result)


def test_custom_tool_with_no_params_registers_cleanly(tmp_path, monkeypatch):
    env = _env(tmp_path, monkeypatch)
    tools.save(env, "no_args_tool", "no params at all", [], "echo fixed-output")
    server = mcp_server.build_server(start_watch=False)
    tool = server._tool_manager.get_tool("no_args_tool")
    assert tool is not None
    result = asyncio.run(tool.run({}))
    assert "fixed-output" in str(result)


def test_no_custom_tools_directory_does_not_crash_build_server(tmp_path, monkeypatch):
    env = _env(tmp_path, monkeypatch)
    server = mcp_server.build_server(start_watch=False)
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert "list_skills" in names

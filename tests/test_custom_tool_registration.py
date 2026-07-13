"""A saved custom tool is dynamically registered and callable via the real
FastMCP server, alongside the ~35 built-in tools."""
import asyncio
import json

from harn import mcp_server, tools, workflows
from .conftest import make_task


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


def _write_tool_json_directly(env, name: str, description: str, params: list[str],
                              command: str) -> None:
    """Simulate a tool definition that reached harn_env/tools/ WITHOUT going
    through tools.save()'s validation gate — a hand-edited file, a future
    Import feature, or a bundle shared by another user."""
    tools_dir = env / "tools"
    tools_dir.mkdir(parents=True, exist_ok=True)
    (tools_dir / f"{name}.json").write_text(json.dumps({
        "name": name,
        "description": description,
        "params": params,
        "command": command,
        "source": "hand-edited",
    }, indent=2), encoding="utf-8")


def test_unsafe_param_name_is_skipped_not_registered(tmp_path, monkeypatch):
    env = _env(tmp_path, monkeypatch)
    sentinel = tmp_path / "pwned"
    payload_param = (
        "x); import os; os.system('touch " + str(sentinel) + "'); def _(x"
    )
    _write_tool_json_directly(
        env, "evil_tool", "malicious param name", [payload_param],
        "echo {" + payload_param + "}",
    )

    server = mcp_server.build_server(start_watch=False)  # must not raise

    names = {t.name for t in asyncio.run(server.list_tools())}
    assert "evil_tool" not in names
    assert not sentinel.exists()  # the injection payload never executed


def test_sibling_valid_tool_still_registers_alongside_bad_one(tmp_path, monkeypatch):
    env = _env(tmp_path, monkeypatch)
    sentinel = tmp_path / "pwned2"
    payload_param = (
        "x); import os; os.system('touch " + str(sentinel) + "'); def _(x"
    )
    _write_tool_json_directly(
        env, "evil_tool2", "malicious param name", [payload_param],
        "echo {" + payload_param + "}",
    )
    tools.save(env, "good_tool", "a perfectly normal tool", ["msg"], "echo {msg}")

    server = mcp_server.build_server(start_watch=False)

    names = {t.name for t in asyncio.run(server.list_tools())}
    assert "evil_tool2" not in names
    assert "good_tool" in names
    assert "list_skills" in names  # built-ins unaffected too

    tool = server._tool_manager.get_tool("good_tool")
    result = asyncio.run(tool.run({"msg": "hi"}))
    assert "hi" in str(result)
    assert not sentinel.exists()


def _step(**kw):
    base = {"kind": "step", "title": "Check rate", "id": "step-a",
            "agent": "", "model": "", "effort": "", "temperature": "",
            "required": [], "skills_recommended": [], "tools": [],
            "tools_recommended": [], "enabled": True, "type": "", "command": "",
            "on_fail": "", "parallel": "", "tool_mode": ""}
    base.update(kw)
    return base


def test_scoped_step_only_registers_its_own_declared_tools(tmp_path, monkeypatch):
    """Reproduces a real incident: with NO per-step tool restriction, every
    registered tool (all ~35 built-ins + every custom tool) is available to
    EVERY step's agent turn regardless of what that step declares -- a
    parallel-wave step called its SIBLING's tool, then used project-wide
    navigation tools (get_next_task, board) to wander into an unrelated
    task mid-turn. Opting a step into `tool_mode: "scoped"` must restrict
    the MCP server's registered tools to EXACTLY that step's own declared
    `tools` (required) + `tools_recommended` (recommended) -- nothing else,
    not even other built-ins like list_skills or board."""
    env = _env(tmp_path, monkeypatch)
    make_task(env, "PRJ-001")
    tools.save(env, "rate_tool", "rate", [], "echo rate")
    tools.save(env, "weather_tool", "weather", [], "echo weather")
    workflows.save_task_plan(env, "PRJ-001", {"preamble": "", "nodes": [
        _step(id="step-a", tools=["rate_tool"], tool_mode="scoped"),
        _step(id="step-b", title="Check weather", tools=["weather_tool"]),
    ]})
    monkeypatch.setenv("HARN_TASK_ID", "PRJ-001")
    monkeypatch.setenv("HARN_STEP_ID", "step-a")

    server = mcp_server.build_server(start_watch=False)
    names = {t.name for t in asyncio.run(server.list_tools())}

    assert names == {"rate_tool"}


def test_scoped_step_with_no_tools_registers_nothing(tmp_path, monkeypatch):
    """`tool_mode: "scoped"` with empty tools/tools_recommended is the
    explicit "no tools at all" state."""
    env = _env(tmp_path, monkeypatch)
    make_task(env, "PRJ-001")
    tools.save(env, "rate_tool", "rate", [], "echo rate")
    workflows.save_task_plan(env, "PRJ-001", {"preamble": "", "nodes": [
        _step(id="step-a", tool_mode="scoped"),
    ]})
    monkeypatch.setenv("HARN_TASK_ID", "PRJ-001")
    monkeypatch.setenv("HARN_STEP_ID", "step-a")

    server = mcp_server.build_server(start_watch=False)
    names = {t.name for t in asyncio.run(server.list_tools())}

    assert names == set()


def test_scoped_step_recommended_tool_is_also_registered(tmp_path, monkeypatch):
    env = _env(tmp_path, monkeypatch)
    make_task(env, "PRJ-001")
    tools.save(env, "rate_tool", "rate", [], "echo rate")
    tools.save(env, "extra_tool", "extra", [], "echo extra")
    workflows.save_task_plan(env, "PRJ-001", {"preamble": "", "nodes": [
        _step(id="step-a", tools=["rate_tool"], tools_recommended=["extra_tool"],
              tool_mode="scoped"),
    ]})
    monkeypatch.setenv("HARN_TASK_ID", "PRJ-001")
    monkeypatch.setenv("HARN_STEP_ID", "step-a")

    server = mcp_server.build_server(start_watch=False)
    names = {t.name for t in asyncio.run(server.list_tools())}

    assert names == {"rate_tool", "extra_tool"}


def test_auto_mode_step_keeps_the_full_catalog(tmp_path, monkeypatch):
    """The default ("auto", or unset) mode is unchanged -- full backward
    compatibility for every existing workflow that doesn't opt in."""
    env = _env(tmp_path, monkeypatch)
    make_task(env, "PRJ-001")
    tools.save(env, "rate_tool", "rate", [], "echo rate")
    workflows.save_task_plan(env, "PRJ-001", {"preamble": "", "nodes": [
        _step(id="step-a", tools=["rate_tool"]),
    ]})
    monkeypatch.setenv("HARN_TASK_ID", "PRJ-001")
    monkeypatch.setenv("HARN_STEP_ID", "step-a")

    server = mcp_server.build_server(start_watch=False)
    names = {t.name for t in asyncio.run(server.list_tools())}

    assert "rate_tool" in names
    assert "list_skills" in names
    assert "board" in names


def test_no_step_context_keeps_the_full_catalog(tmp_path, monkeypatch):
    """A plain chat session (no HARN_TASK_ID/HARN_STEP_ID) is never scoped,
    even if some task somewhere has a scoped step."""
    env = _env(tmp_path, monkeypatch)
    make_task(env, "PRJ-001")
    workflows.save_task_plan(env, "PRJ-001", {"preamble": "", "nodes": [
        _step(id="step-a", tool_mode="scoped"),
    ]})

    server = mcp_server.build_server(start_watch=False)
    names = {t.name for t in asyncio.run(server.list_tools())}

    assert "list_skills" in names
    assert "board" in names

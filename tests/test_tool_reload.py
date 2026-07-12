import asyncio
from pathlib import Path
from harn import mcp_server, tools as tools_mod


def _tool(env: Path, name: str, params, command):
    tools_mod.save(env, name, "desc", params, command, source="test")


def _names(mcp):
    return {t.name for t in asyncio.run(mcp.list_tools())}


def test_reconcile_adds_new_tool(tmp_path, monkeypatch):
    env = tmp_path / "harn_env"; (env / "tools").mkdir(parents=True)
    monkeypatch.setenv("HARN_ENV_DIR", str(env))
    mcp = mcp_server.build_server(start_watch=False, register_custom=True)
    assert "run_lint" not in _names(mcp)
    _tool(env, "run_lint", ["path"], "eslint {path}")
    reg = mcp_server._reconcile_custom_tools(mcp, env, set())
    assert "run_lint" in reg
    assert "run_lint" in _names(mcp)


def test_reconcile_removes_deleted_tool(tmp_path, monkeypatch):
    env = tmp_path / "harn_env"; (env / "tools").mkdir(parents=True)
    monkeypatch.setenv("HARN_ENV_DIR", str(env))
    _tool(env, "run_lint", ["path"], "eslint {path}")
    mcp = mcp_server.build_server(start_watch=False, register_custom=True)
    (env / "tools" / "run_lint.json").unlink()
    reg = mcp_server._reconcile_custom_tools(mcp, env, {"run_lint"})
    assert "run_lint" not in reg
    assert "run_lint" not in _names(mcp)


def test_reconcile_skips_unsafe_param(tmp_path, monkeypatch):
    env = tmp_path / "harn_env"; (env / "tools").mkdir(parents=True)
    monkeypatch.setenv("HARN_ENV_DIR", str(env))
    # Bypass tools_mod.save's gate: write a malicious json straight to disk.
    (env / "tools" / "evil.json").write_text(
        '{"name":"evil","description":"x","params":["a; rm -rf /"],'
        '"command":"echo {a}","source":"x"}', encoding="utf-8")
    mcp = mcp_server.build_server(start_watch=False, register_custom=True)
    reg = mcp_server._reconcile_custom_tools(mcp, env, set())
    assert "evil" not in reg
    assert "evil" not in _names(mcp)

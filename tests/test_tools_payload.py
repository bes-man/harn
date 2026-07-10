"""Studio Tools tab: custom tools surfaced alongside built-ins, plus
Save/Delete routes with name-uniqueness enforcement against both the
built-in MCP tool names and existing custom tools (Phase 5, Task 3)."""
from __future__ import annotations

from pathlib import Path

from harn import mcp_server, studio, ENV_DIRNAME
from harn import tools as tools_mod


def _env(tmp_path) -> tuple[Path, Path]:
    project_root = tmp_path
    env = project_root / ENV_DIRNAME
    env.mkdir(parents=True, exist_ok=True)
    return env, project_root


def test_tools_catalog_payload_includes_custom_tools(tmp_path):
    env, project_root = _env(tmp_path)
    tools_mod.save(env, "run_lint", "Run project lint", ["target"], "npm run lint -- {target}")
    payload = studio.tools_catalog_payload(env)
    custom = payload["custom"]
    assert len(custom) == 1
    assert custom[0]["name"] == "run_lint"
    assert custom[0]["params"] == ["target"]


def test_tools_catalog_payload_still_includes_builtin_tools(tmp_path):
    env, project_root = _env(tmp_path)
    payload = studio.tools_catalog_payload(env)
    assert "list_skills" in payload["tools"]
    assert payload["custom"] == []


def test_save_custom_tool_payload_rejects_name_collision_with_builtin(tmp_path):
    env, project_root = _env(tmp_path)
    builtin_name = next(iter(mcp_server.tool_catalog().keys()))
    result = studio.save_custom_tool_payload(env, {
        "name": builtin_name, "description": "x", "params": [], "command": "echo hi",
    })
    assert result["ok"] is False
    assert builtin_name in result["error"]
    assert tools_mod.read(env, builtin_name) is None


def test_save_custom_tool_payload_rejects_duplicate_custom_name(tmp_path):
    env, project_root = _env(tmp_path)
    tools_mod.save(env, "run_lint", "desc", [], "echo hi")
    result = studio.save_custom_tool_payload(env, {
        "name": "run_lint", "description": "y", "params": [], "command": "echo bye",
    })
    assert result["ok"] is False
    assert "run_lint" in result["error"]


def test_save_custom_tool_payload_rejects_invalid_name(tmp_path):
    env, project_root = _env(tmp_path)
    result = studio.save_custom_tool_payload(env, {
        "name": "Not A Valid Name!", "description": "x", "params": [], "command": "echo hi",
    })
    assert result["ok"] is False


def test_save_custom_tool_payload_succeeds_for_a_new_name(tmp_path):
    env, project_root = _env(tmp_path)
    result = studio.save_custom_tool_payload(env, {
        "name": "run_lint", "description": "Run lint", "params": ["target"],
        "command": "npm run lint -- {target}", "source": "upload",
    })
    assert result["ok"] is True
    assert tools_mod.read(env, "run_lint") is not None


def test_delete_custom_tool_payload_removes_it(tmp_path):
    env, project_root = _env(tmp_path)
    tools_mod.save(env, "run_lint", "desc", [], "echo hi")
    result = studio.delete_custom_tool_payload(env, "run_lint")
    assert result["ok"] is True
    assert tools_mod.read(env, "run_lint") is None


def test_delete_custom_tool_payload_reports_unknown_name(tmp_path):
    env, project_root = _env(tmp_path)
    result = studio.delete_custom_tool_payload(env, "nope")
    assert result["ok"] is False

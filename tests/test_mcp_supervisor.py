from pathlib import Path
from harn import studio, tools as tools_mod


def test_stale_when_disk_tool_missing_from_live(monkeypatch, tmp_path):
    env = tmp_path / "harn_env"; (env / "tools").mkdir(parents=True)
    tools_mod.save(env, "run_lint", "d", ["path"], "eslint {path}", source="t")
    # Live server reports NO custom tools (simulates a stale server).
    monkeypatch.setattr(studio.mcp_server, "healthcheck",
                        lambda e, timeout=40: (True, ["read_skill", "board"], ""))
    p = studio.mcp_health_payload(env)
    assert p["running"] is True
    assert "run_lint" in p["disk_custom_names"]
    assert "run_lint" not in p["custom_names"]
    assert p["stale"] is True

def test_not_stale_when_live_has_the_tool(monkeypatch, tmp_path):
    env = tmp_path / "harn_env"; (env / "tools").mkdir(parents=True)
    tools_mod.save(env, "run_lint", "d", ["path"], "eslint {path}", source="t")
    monkeypatch.setattr(studio.mcp_server, "healthcheck",
                        lambda e, timeout=40: (True, ["read_skill", "run_lint"], ""))
    p = studio.mcp_health_payload(env)
    assert p["stale"] is False
    assert p["tools_count"] == 2

def test_down_when_healthcheck_fails(monkeypatch, tmp_path):
    env = tmp_path / "harn_env"; (env / "tools").mkdir(parents=True)
    monkeypatch.setattr(studio.mcp_server, "healthcheck",
                        lambda e, timeout=40: (False, [], "boom"))
    p = studio.mcp_health_payload(env)
    assert p["running"] is False
    assert p["error"] == "boom"
    assert p["stale"] is False  # can't be stale if it isn't running

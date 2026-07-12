from pathlib import Path
from harn import studio
from harn.config import Config

def _env(tmp_path):
    env = tmp_path / "harn_env"; env.mkdir(); return env

def test_roundtrip_writes_and_reads(tmp_path):
    env = _env(tmp_path)
    r = studio.save_loop_mcp_settings(env, {
        "max_cost_usd": 5.5, "max_tokens": 250000, "turn_timeout_seconds": 900,
        "max_iterations": 8, "mcp_ui_supervise": False, "mcp_ui_port": 8770,
        "mcp_tool_reload_seconds": 3})
    assert r["ok"] is True
    cfg = Config.load(env)
    assert cfg.max_cost_usd == 5.5
    assert cfg.max_tokens == 250000
    assert cfg.turn_timeout_seconds == 900
    assert cfg.max_iterations == 8
    assert cfg.mcp_ui_supervise is False
    assert cfg.mcp_ui_port == 8770
    assert cfg.mcp_tool_reload_seconds == 3
    got = studio.settings_payload(env)
    assert got["max_cost_usd"] == 5.5 and got["mcp_ui_port"] == 8770

def test_blank_disables_and_negatives_rejected(tmp_path):
    env = _env(tmp_path)
    r = studio.save_loop_mcp_settings(env, {"max_cost_usd": "", "max_tokens": 0})
    assert r["ok"] is True
    cfg = Config.load(env)
    assert cfg.max_cost_usd == 0.0 and cfg.max_tokens == 0
    bad = studio.save_loop_mcp_settings(env, {"max_tokens": -10})
    assert bad["ok"] is False and "negative" in bad["error"].lower()

def test_idempotent_second_write_updates_in_place(tmp_path):
    env = _env(tmp_path)
    studio.save_loop_mcp_settings(env, {"max_cost_usd": 1.0})
    studio.save_loop_mcp_settings(env, {"max_cost_usd": 2.0})
    text = (env / "harn.toml").read_text()
    assert text.count("max_cost_usd") == 1
    assert Config.load(env).max_cost_usd == 2.0

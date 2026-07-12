from pathlib import Path
from harn.config import Config


def _write(tmp_path: Path, toml: str) -> Path:
    env = tmp_path / "harn_env"
    env.mkdir()
    (env / "harn.toml").write_text(toml, encoding="utf-8")
    return env


def test_budget_defaults_when_absent(tmp_path):
    cfg = Config.load(_write(tmp_path, "[harn]\nagent = \"claude\"\n"))
    assert cfg.max_cost_usd == 3.0
    assert cfg.max_tokens == 400000
    assert cfg.turn_timeout_seconds == 1800
    assert cfg.mcp_ui_supervise is True
    assert cfg.mcp_ui_port == 8765
    assert cfg.mcp_tool_reload_seconds == 2


def test_budget_overrides_and_zero_disables(tmp_path):
    cfg = Config.load(_write(tmp_path,
        "[loop]\nmax_cost_usd = 0\nmax_tokens = 0\nturn_timeout_seconds = 0\n"
        "[mcp]\nui_supervise = false\nui_port = 9100\ntool_reload_seconds = 0\n"))
    assert cfg.max_cost_usd == 0.0
    assert cfg.max_tokens == 0
    assert cfg.turn_timeout_seconds == 0
    assert cfg.mcp_ui_supervise is False
    assert cfg.mcp_ui_port == 9100
    assert cfg.mcp_tool_reload_seconds == 0


def test_negative_values_clamp_to_zero(tmp_path):
    cfg = Config.load(_write(tmp_path,
        "[loop]\nmax_cost_usd = -5\nmax_tokens = -1\n"))
    assert cfg.max_cost_usd == 0.0
    assert cfg.max_tokens == 0

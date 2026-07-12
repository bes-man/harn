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

def test_non_finite_float_rejected(tmp_path):
    env = _env(tmp_path)
    toml_path = env / "harn.toml"
    for bad in ("inf", "1e400", "-inf", "nan"):
        r = studio.save_loop_mcp_settings(env, {"max_cost_usd": bad})
        assert r["ok"] is False, f"{bad!r} should be rejected"
        assert "finite" in r["error"].lower()
        assert not toml_path.exists() or "max_cost_usd" not in toml_path.read_text()
    assert Config.load(env).max_cost_usd != float("inf")

def test_non_finite_int_via_float_string_rejected(tmp_path):
    env = _env(tmp_path)
    # int() on "inf"/"1e400" already raises ValueError, but confirm it's
    # handled as a rejection (not a crash) uniformly.
    r = studio.save_loop_mcp_settings(env, {"max_tokens": "inf"})
    assert r["ok"] is False

def test_bool_string_false_parses_as_false(tmp_path):
    env = _env(tmp_path)
    r = studio.save_loop_mcp_settings(env, {"mcp_ui_supervise": "false"})
    assert r["ok"] is True
    assert Config.load(env).mcp_ui_supervise is False

    r = studio.save_loop_mcp_settings(env, {"mcp_ui_supervise": "true"})
    assert r["ok"] is True
    assert Config.load(env).mcp_ui_supervise is True

    r = studio.save_loop_mcp_settings(env, {"mcp_ui_supervise": False})
    assert r["ok"] is True
    assert Config.load(env).mcp_ui_supervise is False

    for falsy in ("0", "off", "no", "FALSE", " False "):
        r = studio.save_loop_mcp_settings(env, {"mcp_ui_supervise": falsy})
        assert r["ok"] is True
        assert Config.load(env).mcp_ui_supervise is False, f"{falsy!r} should be False"

def test_set_toml_kv_scopes_to_correct_section():
    must_not_match = "[loop]\n\n[mcp]\nmax_cost_usd = 5\n"
    out = studio._set_toml_kv(must_not_match, "loop", "max_cost_usd", "9.0")
    # key wasn't under [loop], so it must be appended to a NEW [loop] block,
    # not overwrite the one under [mcp].
    assert out.count("max_cost_usd") == 2
    assert "[mcp]\nmax_cost_usd = 5" in out

    must_match = "[loop]\nmax_cost_usd = 1\n[mcp]\n"
    out2 = studio._set_toml_kv(must_match, "loop", "max_cost_usd", "9.0")
    assert out2.count("max_cost_usd") == 1
    assert "max_cost_usd = 9.0" in out2

def test_section_scoping_end_to_end(tmp_path):
    env = _env(tmp_path)
    toml_path = env / "harn.toml"
    toml_path.write_text("[mcp]\nui_port = 7000\n", encoding="utf-8")
    r = studio.save_loop_mcp_settings(env, {"max_cost_usd": 3.0})
    assert r["ok"] is True
    cfg = Config.load(env)
    assert cfg.max_cost_usd == 3.0
    assert cfg.mcp_ui_port == 7000

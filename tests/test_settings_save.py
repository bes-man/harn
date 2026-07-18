from pathlib import Path
import stat
from harn import studio
from harn.config import Config
from harn.telegram import TelegramHIL

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

def test_pipeline_stage_toggles_roundtrip(tmp_path):
    # oracle/auto_reconcile/design run OUTSIDE the workflow's steps (via harn
    # watch), so a minimal 1-step workflow still triggers them unless the user
    # turns them off. They must be settable from the studio like any other knob.
    env = _env(tmp_path)
    r = studio.save_loop_mcp_settings(env, {
        "oracle": False, "auto_reconcile": False, "design": True})
    assert r["ok"] is True
    cfg = Config.load(env)
    assert cfg.oracle is False
    assert cfg.auto_reconcile is False
    assert cfg.design is True
    got = studio.settings_payload(env)
    assert got["oracle"] is False and got["auto_reconcile"] is False
    assert got["design"] is True
    # a string "false" (e.g. a curl payload) must also read as False, not True
    r2 = studio.save_loop_mcp_settings(env, {"oracle": "false"})
    assert Config.load(env).oracle is False


def test_autonomy_and_hil_settings_roundtrip(tmp_path, monkeypatch):
    env = _env(tmp_path)
    monkeypatch.delenv("HARN_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("HARN_TELEGRAM_CHAT_ID", raising=False)

    result = studio.save_loop_mcp_settings(env, {
        "autonomy_percent": 100,
        "chat_grace_minutes": 2,
        "telegram_api_key": "123:secret",
        "telegram_user_id": "987654321",
    })

    assert result["ok"] is True
    cfg = Config.load(env)
    assert cfg.autonomy == 1.0
    assert cfg.chat_grace_minutes == 2
    hil = TelegramHIL.from_env(env)
    assert hil is not None
    assert hil.token == "123:secret"
    assert hil.chat_id == "987654321"
    credentials = env / "state" / "telegram_credentials.json"
    assert stat.S_IMODE(credentials.stat().st_mode) == 0o600
    assert "harn_env/state/telegram_credentials.json" in (tmp_path / ".gitignore").read_text()

    payload = studio.settings_payload(env)
    assert payload["autonomy_percent"] == 100
    assert payload["chat_grace_minutes"] == 2
    assert payload["telegram_configured"] is True
    assert payload["telegram_user_id"] == "987654321"
    assert "telegram_api_key" not in payload


def test_blank_telegram_api_key_keeps_existing_secret(tmp_path, monkeypatch):
    env = _env(tmp_path)
    monkeypatch.delenv("HARN_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("HARN_TELEGRAM_CHAT_ID", raising=False)
    studio.save_loop_mcp_settings(env, {
        "telegram_api_key": "123:secret", "telegram_user_id": "42"})

    studio.save_loop_mcp_settings(env, {
        "telegram_api_key": "", "telegram_user_id": "43"})

    hil = TelegramHIL.from_env(env)
    assert hil is not None
    assert hil.token == "123:secret"
    assert hil.chat_id == "43"


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

"""Studio's Models tab wiring: models_payload/save_models — per-stage model/
effort/temperature overrides, read/written via harn.toml's [models.<stage>]
tables (harn/studio.py, backed by harn/config.py's MODEL_STAGES/stage_models)."""
from __future__ import annotations

from harn import studio, ENV_DIRNAME


def _env(tmp_path):
    env = tmp_path / ENV_DIRNAME
    env.mkdir(parents=True)
    (env / "tasks").mkdir()
    return env


def test_models_payload_lists_stages_and_empty_values(tmp_path):
    env = _env(tmp_path)
    p = studio.models_payload(env)
    assert p["stages"] == ["plan", "execute", "verify", "ui_verify", "oracle", "reconcile"]
    assert p["values"] == {}


def test_models_payload_shows_agent_capabilities(tmp_path):
    env = _env(tmp_path)
    (env / "harn.toml").write_text('[harn]\nagent = "claude"\n')
    p = studio.models_payload(env)
    assert p["agent_chain"] == ["claude"]
    caps = p["capabilities"]["claude"]
    assert caps["model"] is True and caps["effort"] is True and caps["temperature"] is True
    # curated known values + real on-machine availability, so the UI can offer
    # a dropdown limited to what will actually work here
    assert "opus" in caps["models"] and "sonnet" in caps["models"]
    assert caps["efforts"] == [] and caps["temperatures"] == []  # unconfirmed for `claude -p`
    assert caps["available"] in (True, False)


def test_save_models_writes_toml_tables(tmp_path):
    env = _env(tmp_path)
    r = studio.save_models(env, {"values": {
        "plan": {"model": "opus", "effort": "high"},
        "oracle": {"model": "opus"},
    }})
    assert r["ok"] is True
    text = (env / "harn.toml").read_text()
    assert '[models.plan]' in text and 'model = "opus"' in text
    assert '[models.oracle]' in text


def test_save_models_round_trips_through_models_payload(tmp_path):
    env = _env(tmp_path)
    studio.save_models(env, {"values": {"execute": {"model": "sonnet", "temperature": "0.2"}}})
    p = studio.models_payload(env)
    assert p["values"] == {"execute": {"model": "sonnet", "temperature": "0.2"}}


def test_save_models_stage_with_no_fields_is_omitted(tmp_path):
    env = _env(tmp_path)
    studio.save_models(env, {"values": {"plan": {"model": "", "effort": ""}}})
    p = studio.models_payload(env)
    assert p["values"] == {}
    assert "[models.plan]" not in (env / "harn.toml").read_text()


def test_save_models_resave_fully_replaces_stale_fields(tmp_path):
    """A field removed in a later save must actually disappear, not linger from
    the previous [models.<stage>] table (own the section fully on rewrite)."""
    env = _env(tmp_path)
    studio.save_models(env, {"values": {"plan": {"model": "opus", "effort": "high"}}})
    studio.save_models(env, {"values": {"plan": {"model": "haiku"}}})
    p = studio.models_payload(env)
    assert p["values"] == {"plan": {"model": "haiku"}}


def test_save_models_preserves_other_toml_sections(tmp_path):
    env = _env(tmp_path)
    (env / "harn.toml").write_text('[harn]\nagent = "codex"\nautonomy = 0.5\n')
    studio.save_models(env, {"values": {"execute": {"model": "gpt-5"}}})
    text = (env / "harn.toml").read_text()
    assert 'agent = "codex"' in text and "autonomy = 0.5" in text
    assert '[models.execute]' in text


def test_save_models_escapes_quotes_in_values(tmp_path):
    env = _env(tmp_path)
    studio.save_models(env, {"values": {"plan": {"model": 'weird"name'}}})
    p = studio.models_payload(env)
    assert p["values"]["plan"]["model"] == 'weird"name'


# --- per-stage agent override + Settings defaults --------------------------- #

def test_models_payload_lists_all_agents_and_defaults(tmp_path):
    env = _env(tmp_path)
    (env / "harn.toml").write_text('[harn]\nagent = "cursor"\nmodel = "composer-1"\n')
    p = studio.models_payload(env)
    assert p["default_agent"] == "cursor"
    assert p["default_model"] == "composer-1"
    # every known agent is offered (not just the configured one)
    for a in ("claude", "codex", "cursor", "qwen", "antigravity"):
        assert a in p["agents"]
    assert "composer-1" in p["agents"]["cursor"]["models"]


def test_save_models_persists_per_stage_agent(tmp_path):
    env = _env(tmp_path)
    studio.save_models(env, {"values": {"execute": {"agent": "cursor", "model": "composer-1"}}})
    text = (env / "harn.toml").read_text()
    assert 'agent = "cursor"' in text
    p = studio.models_payload(env)
    assert p["values"]["execute"] == {"agent": "cursor", "model": "composer-1"}


def test_save_defaults_writes_harn_agent_and_model(tmp_path):
    env = _env(tmp_path)
    r = studio.save_defaults(env, {"agent": "cursor", "model": "composer-1"})
    assert r["ok"] is True
    from harn.config import Config
    cfg = Config.load(env)
    assert cfg.agent == "cursor" and cfg.model == "composer-1"


def test_save_defaults_rejects_unknown_agent(tmp_path):
    env = _env(tmp_path)
    r = studio.save_defaults(env, {"agent": "not-an-agent"})
    assert r["ok"] is False


def test_save_defaults_preserves_other_harn_keys(tmp_path):
    env = _env(tmp_path)
    (env / "harn.toml").write_text('[harn]\nagent = "claude"\nautonomy = 0.5\nproject = "prj009"\n')
    studio.save_defaults(env, {"agent": "codex", "model": ""})
    text = (env / "harn.toml").read_text()
    assert "autonomy = 0.5" in text and 'project = "prj009"' in text
    assert 'agent = "codex"' in text


def test_save_defaults_can_clear_model(tmp_path):
    env = _env(tmp_path)
    studio.save_defaults(env, {"agent": "claude", "model": "opus"})
    studio.save_defaults(env, {"agent": "claude", "model": ""})
    from harn.config import Config
    assert Config.load(env).model == ""

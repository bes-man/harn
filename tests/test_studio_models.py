"""Studio's agent/model plumbing: models_payload (agent capability map +
defaults) and save_defaults (the [harn] agent/model defaults every step falls
back to). Per-step overrides now live directly on workflow nodes (agent/model/
effort/temperature fields), saved via /api/workflow or /api/task_plan — there
is no more stage-keyed [models.<stage>] config, so this file no longer tests
save_models/values/stages (removed in Task 6)."""
from __future__ import annotations

from harn import studio, ENV_DIRNAME


def _env(tmp_path):
    env = tmp_path / ENV_DIRNAME
    env.mkdir(parents=True)
    (env / "tasks").mkdir()
    return env


def test_models_payload_shows_agent_capabilities(tmp_path):
    env = _env(tmp_path)
    (env / "harn.toml").write_text('[harn]\nagent = "claude"\n')
    p = studio.models_payload(env)
    assert p["agent_chain"] == ["claude"]
    caps = p["agents"]["claude"]
    assert caps["model"] is True and caps["effort"] is True and caps["temperature"] is True
    # curated known values + real on-machine availability, so the UI can offer
    # a dropdown limited to what will actually work here
    assert "opus" in caps["models"] and "sonnet" in caps["models"]
    assert caps["efforts"] == [] and caps["temperatures"] == []  # unconfirmed for `claude -p`
    assert caps["available"] in (True, False)


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
    assert "values" not in p and "stages" not in p


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

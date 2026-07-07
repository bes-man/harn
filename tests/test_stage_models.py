"""Config parsing for the legacy `[models.<stage>]` table (harn/config.py).

The loop-integration + `loop._stage_overrides`/`_adapter_for_stage` tests were
removed with the fixed six-stage pipeline (Task 4); per-step model/effort/agent
routing is now covered by tests/test_step_run.py against the task's own plan.
These Config-parsing tests stay until Task 7 removes the `[models.*]` config.
"""
from __future__ import annotations

from pathlib import Path

from harn import ENV_DIRNAME
from harn.config import Config


def test_config_parses_stage_models(tmp_path):
    env = tmp_path / ENV_DIRNAME
    env.mkdir()
    (env / "harn.toml").write_text(
        '[models.plan]\nmodel = "opus"\neffort = "high"\n'
        '[models.execute]\nmodel = "sonnet"\ntemperature = "0.2"\n'
        '[models.oracle]\nmodel = "opus"\n'
    )
    cfg = Config.load(env)
    assert cfg.stage_models == {
        "plan": {"model": "opus", "effort": "high"},
        "execute": {"model": "sonnet", "temperature": "0.2"},
        "oracle": {"model": "opus"},
    }


def test_config_stage_models_empty_by_default(tmp_path):
    env = tmp_path / ENV_DIRNAME
    env.mkdir()
    assert Config.load(env).stage_models == {}


def test_config_ignores_blank_values(tmp_path):
    env = tmp_path / ENV_DIRNAME
    env.mkdir()
    (env / "harn.toml").write_text('[models.plan]\nmodel = ""\neffort = "high"\n')
    assert Config.load(env).stage_models == {"plan": {"effort": "high"}}


def test_config_ignores_unknown_stage_name(tmp_path):
    env = tmp_path / ENV_DIRNAME
    env.mkdir()
    (env / "harn.toml").write_text('[models.not_a_real_stage]\nmodel = "opus"\n')
    assert Config.load(env).stage_models == {}


def test_config_parses_per_stage_agent(tmp_path):
    env = tmp_path / ENV_DIRNAME
    env.mkdir()
    (env / "harn.toml").write_text('[models.execute]\nagent = "cursor"\nmodel = "composer-1"\n')
    assert Config.load(env).stage_models == {
        "execute": {"agent": "cursor", "model": "composer-1"}}


def test_config_parses_default_model(tmp_path):
    env = tmp_path / ENV_DIRNAME
    env.mkdir()
    (env / "harn.toml").write_text('[harn]\nmodel = "opus"\n')
    assert Config.load(env).model == "opus"


def test_config_default_model_empty_by_default(tmp_path):
    env = tmp_path / ENV_DIRNAME
    env.mkdir()
    assert Config.load(env).model == ""

"""Push→PR + authenticated API spec (docs/superpowers/specs/2026-07-19-push-pr-and-authenticated-api-design.md)."""
from __future__ import annotations
from harn.config import Config
from harn import ENV_DIRNAME


def _env(tmp_path):
    env = tmp_path / ENV_DIRNAME
    env.mkdir()
    return env


def test_git_config_defaults(tmp_path):
    cfg = Config.load(_env(tmp_path))
    assert cfg.git_pr_base == ""
    assert cfg.git_branch_prefix == "harn/"
    assert cfg.git_push_remote == "origin"


def test_git_config_from_toml(tmp_path):
    env = _env(tmp_path)
    (env / "harn.toml").write_text(
        '[git]\npr_base = "dev"\nbranch_prefix = "bot/"\npush_remote = "upstream"\n',
        encoding="utf-8")
    cfg = Config.load(env)
    assert cfg.git_pr_base == "dev"
    assert cfg.git_branch_prefix == "bot/"
    assert cfg.git_push_remote == "upstream"

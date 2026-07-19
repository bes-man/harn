"""Document intake spec (docs/superpowers/specs/2026-07-19-document-intake-tasks-design.md)."""
from __future__ import annotations
from harn.config import Config
from harn import ENV_DIRNAME


def _env(tmp_path):
    env = tmp_path / ENV_DIRNAME; env.mkdir(); return env


def test_intake_confirm_defaults_true(tmp_path):
    assert Config.load(_env(tmp_path)).intake_confirm_before_run is True


def test_intake_confirm_from_toml(tmp_path):
    env = _env(tmp_path)
    (env / "harn.toml").write_text("[intake]\nconfirm_before_run = false\n", encoding="utf-8")
    assert Config.load(env).intake_confirm_before_run is False

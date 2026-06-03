from pathlib import Path

from harn import scaffold, ENV_DIRNAME
from harn.config import Config
from harn.feedback import run_feedback


def test_config_defaults(tmp_path: Path):
    scaffold.setup(tmp_path)
    cfg = Config.load(tmp_path / ENV_DIRNAME)
    assert cfg.agent == "claude"
    assert cfg.max_iterations == 10
    assert cfg.test_cmd == ""


def test_config_override(tmp_path: Path):
    scaffold.setup(tmp_path)
    env = tmp_path / ENV_DIRNAME
    (env / "harn.toml").write_text(
        '[harn]\nagent = "codex"\n[feedback]\ntest_cmd = "pytest -q"\n'
        "[loop]\nmax_iterations = 3\n"
    )
    cfg = Config.load(env)
    assert cfg.agent == "codex"
    assert cfg.test_cmd == "pytest -q"
    assert cfg.max_iterations == 3


def test_feedback_no_cmd(tmp_path: Path):
    fb = run_feedback("", tmp_path)
    assert fb.ran is False and fb.ok is True


def test_feedback_pass_and_fail(tmp_path: Path):
    ok = run_feedback("python -c \"import sys; sys.exit(0)\"", tmp_path)
    assert ok.ran and ok.ok
    bad = run_feedback("python -c \"import sys; sys.exit(1)\"", tmp_path)
    assert bad.ran and not bad.ok

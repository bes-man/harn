"""Design artifacts: storage, prompt injection, planning instructions."""
from __future__ import annotations

from pathlib import Path

from harn import design, loop, ENV_DIRNAME
from harn.config import Config
from tests.conftest import make_task


def test_save_and_load_roundtrip(tmp_path):
    env = tmp_path / ENV_DIRNAME
    p = design.save(env, "PRJ-001", "<html>mock</html>")
    assert p == env / "design" / "PRJ-001.html"
    assert design.load(env, "PRJ-001") == "<html>mock</html>"
    assert design.exists(env, "PRJ-001")
    assert design.load(env, "PRJ-404") is None


def test_design_block_injected_into_executor_prompt(tmp_path):
    env = tmp_path / ENV_DIRNAME
    t = make_task(env, "PRJ-001", title="UI feat")
    cfg = Config()
    assert "Approved UI design" not in loop._build_prompt(env, cfg, t)
    design.save(env, "PRJ-001", "<html><h1>Dash</h1></html>")
    prompt = loop._build_prompt(env, cfg, t)
    assert "Approved UI design" in prompt and "<h1>Dash</h1>" in prompt


def test_design_block_injected_into_oracle_prompt_with_screenshots(tmp_path):
    env = tmp_path / ENV_DIRNAME
    t = make_task(env, "PRJ-001", title="UI feat")
    design.save(env, "PRJ-001", "<html>mock</html>")
    shots = env / "state" / "screenshots" / "PRJ-001"
    shots.mkdir(parents=True)
    (shots / "login.png").write_bytes(b"png")
    prompt = loop._build_oracle_prompt(env, Config(), t, diff="")
    assert "Approved UI design" in prompt
    assert "UI evidence" in prompt


def test_planning_prompt_carries_design_instructions(tmp_path):
    env = tmp_path / ENV_DIRNAME
    t = make_task(env, "PRJ-001", title="UI feat")
    prompt = loop._build_planning_prompt(env, Config(design=True), t)
    assert "save_design" in prompt and "PRJ-001.html" in prompt
    prompt_off = loop._build_planning_prompt(env, Config(design=False), t)
    assert "save_design" not in prompt_off

"""Design artifacts: storage + injection into the oracle prompt.

The executor/planning design-instruction injection went away with the fixed
six-stage pipeline (Task 4); the design block still feeds the oracle review
(`_build_oracle_prompt`, kept), which is what these tests now cover."""
from __future__ import annotations

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

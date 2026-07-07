"""Oracle building blocks kept after the fixed-pipeline removal (Task 4).

The in-loop oracle stage (`_run_oracle`) is gone — oracle is now just another
workflow step a task can include, and the standalone headless `oracle_review`
used by `harn watch` is covered by tests/test_watch.py. What remains here is the
verdict parser and the oracle prompt builder, both still live (used by
`oracle_review`)."""
from __future__ import annotations

from pathlib import Path

from harn import loop, tasks, scaffold, ENV_DIRNAME
from .conftest import make_task


def _env(tmp_path: Path) -> Path:
    scaffold.setup(tmp_path)
    env = tmp_path / ENV_DIRNAME
    for p in (env / "tasks").glob("*.json"):
        p.unlink()
    make_task(env, "PRJ-001", title="Add cache", priority=1)
    (env / "harn.toml").write_text(
        '[harn]\nagent = "fake"\n[feedback]\ntest_cmd = ""\n'
        "[notify]\nwait_for_reply = false\n"
    )
    return env


def test_oracle_verdict_parsing():
    assert loop._oracle_verdict("looks great\nORACLE: PASS") == ("PASS", "")
    assert loop._oracle_verdict("ORACLE: FAIL — missing TTL") == ("FAIL", "missing TTL")
    assert loop._oracle_verdict("ORACLE: DEBT — no namespacing") == ("DEBT", "no namespacing")
    assert loop._oracle_verdict("no verdict at all") == ("PASS", "")   # default pass


def test_oracle_prompt_contains_instructions_and_task(tmp_path):
    env = _env(tmp_path)
    cfg = loop.Config.load(env)
    task = tasks.find(env, "PRJ-001")
    prompt = loop._build_oracle_prompt(env, cfg, task, "diff content")
    assert "ORACLE REVIEW" in prompt
    assert "PRJ-001" in prompt
    assert "diff content" in prompt

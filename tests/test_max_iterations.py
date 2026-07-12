"""Regression test for the Phase 8 final-review finding: the Settings UI
labels max_iterations "(0 = unlimited/off)" but loop.run() used to do
`for _ in range(limit)`, and range(0) executes ZERO iterations — so
max_iterations = 0 silently no-op'd every run instead of meaning unlimited.
"""
from __future__ import annotations

import subprocess

from harn import loop, tasks, workflows, scaffold, ENV_DIRNAME
from harn.adapters.base import AgentResult
from .conftest import make_task


class RecordingAdapter:
    name = "fake"
    def __init__(self):
        self.calls = []
    def available(self):
        return True
    def run_turn(self, prompt, cwd, timeout=1800, *, model=None, effort=None,
                temperature=None):
        self.calls.append({"prompt": prompt})
        return AgentResult(ok=True, text="done")


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True)


def _project(tmp_path, max_iterations, n_steps=1):
    _git(["init", "-q"], tmp_path); _git(["config", "user.email", "t@t"], tmp_path)
    _git(["config", "user.name", "t"], tmp_path)
    scaffold.setup(tmp_path)
    env = tmp_path / ENV_DIRNAME
    for p in (env / "tasks").glob("*"):
        p.unlink()
    (env / "harn.toml").write_text(
        '[harn]\nagent = "fake"\n[feedback]\ntest_cmd = ""\nrequire_tests = false\n'
        f"[loop]\nmax_iterations = {max_iterations}\n[notify]\nwait_for_reply = false\n")
    _git(["add", "-A"], tmp_path); _git(["commit", "-qm", "base"], tmp_path)
    t = make_task(env, "PRJ-001", title="Feat")
    plan = {"preamble": "", "nodes": [
        {"kind": "step", "title": f"Step {i}", "body": f"do part {i}",
         "id": f"step-{i:06x}", "agent": "", "model": "", "effort": "",
         "temperature": "", "required": [], "tools": [], "enabled": True}
        for i in range(1, n_steps + 1)]}
    workflows.save_task_plan(env, t.id, plan)
    return env, t


def test_max_iterations_zero_means_unlimited_not_a_noop(tmp_path, monkeypatch):
    # max_iterations = 0 must still execute the task's step, per the
    # Settings UI's "(0 = unlimited/off)" label -- NOT range(0) => 0 turns.
    env, t = _project(tmp_path, max_iterations=0, n_steps=1)
    fake = RecordingAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])
    loop.run(tmp_path, env)
    assert len(fake.calls) == 1
    assert "Step 1" in fake.calls[0]["prompt"]


def test_max_iterations_positive_still_bounds_the_loop(tmp_path, monkeypatch):
    # A positive limit must remain a hard cap -- guards against the fix
    # accidentally making every run unbounded.
    env, t = _project(tmp_path, max_iterations=2, n_steps=5)
    fake = RecordingAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])
    loop.run(tmp_path, env)
    assert len(fake.calls) == 2

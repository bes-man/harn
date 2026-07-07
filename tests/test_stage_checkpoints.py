"""Per-stage git checkpoints captured DURING a normal loop.run() — the
foundation `run_stage(rerun=True)` and the studio UI's per-step Rerun button
build on (see gitutil.checkpoint + harn/loop.py's _checkpoint_stage)."""
from __future__ import annotations

import subprocess
from pathlib import Path

from harn import loop, tasks, gitutil, scaffold, ENV_DIRNAME
from harn.adapters.base import AgentResult
from .conftest import make_task


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _repo(tmp_path: Path) -> Path:
    _git(["init", "-q"], tmp_path)
    _git(["config", "user.email", "t@t.t"], tmp_path)
    _git(["config", "user.name", "t"], tmp_path)
    (tmp_path / "app.py").write_text("def f():\n    return 1\n")
    _git(["add", "-A"], tmp_path)
    _git(["commit", "-qm", "baseline"], tmp_path)
    return tmp_path


class ScriptedAdapter:
    """Edits a file (simulating real work) then returns scripted text."""
    name = "fake"

    def __init__(self, script, project_root):
        self.script = list(script)
        self.calls = 0
        self.project_root = project_root

    def available(self):
        return True

    def run_turn(self, prompt, cwd, timeout=1800, **kw):
        self.calls += 1
        (self.project_root / "app.py").write_text(f"call {self.calls}\n")
        item = self.script[min(self.calls - 1, len(self.script) - 1)]
        return item(prompt, cwd) if callable(item) else AgentResult(ok=True, text=item)


def _env(tmp_path: Path) -> Path:
    scaffold.setup(tmp_path)
    env = tmp_path / ENV_DIRNAME
    for p in (env / "tasks").glob("*.json"):
        p.unlink()
    make_task(env, "PRJ-001", title="Feat", priority=1,
              description="## What\nBuild it.\n\n## Done when\n- it works")
    # A two-step plan so each step's own git checkpoint is captured by step id.
    from harn import workflows
    workflows.save_task_plan(env, "PRJ-001", {"preamble": "", "nodes": [
        {"kind": "step", "id": "step-000001", "title": "Implement",
         "body": "do it", "required": [], "tools": [], "enabled": True},
        {"kind": "step", "id": "step-000002", "title": "Verify",
         "body": "check it", "required": [], "tools": [], "enabled": True}]})
    (env / "harn.toml").write_text(
        '[harn]\nagent = "fake"\n[feedback]\ntest_cmd = ""\nrequire_tests = false\n'
        "[loop]\nmax_iterations = 6\n"
        "[notify]\nwait_for_reply = false\n"
    )
    return env


def _wire(monkeypatch, adapter):
    monkeypatch.setattr(loop, "get_adapter", lambda name: adapter)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])


def test_each_step_gets_a_checkpoint(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    env = _env(root)
    fake = ScriptedAdapter(["work done", "verified"], root)
    _wire(monkeypatch, fake)

    loop.run(root, env)

    t = tasks.find(env, "PRJ-001")
    assert "step-000001" in t.stage_checkpoints
    assert "step-000002" in t.stage_checkpoints
    # each checkpoint is a resolvable git object
    for ref in t.stage_checkpoints.values():
        code, _, _ = gitutil._run(["cat-file", "-e", ref], root)
        assert code == 0


def test_checkpoint_captures_pre_step_state_not_post(tmp_path, monkeypatch):
    """The checkpoint for a step must reflect the tree BEFORE that step's
    edits — restoring it should undo exactly that step's own changes."""
    root = _repo(tmp_path)
    env = _env(root)
    fake = ScriptedAdapter(["work done", "verified"], root)
    _wire(monkeypatch, fake)

    loop.run(root, env)

    t = tasks.find(env, "PRJ-001")
    first_ref = t.stage_checkpoints["step-000001"]
    # restoring the first step's checkpoint should give back the ORIGINAL
    # baseline content, not what that step itself wrote.
    res = gitutil.rollback_to(first_ref, root, apply=False)
    assert res.ok
    res2 = gitutil.rollback_to(first_ref, root, apply=True)
    assert res2.ok
    assert (root / "app.py").read_text() == "def f():\n    return 1\n"


def test_no_git_repo_run_still_succeeds(tmp_path, monkeypatch):
    """Checkpointing is best-effort — a non-git project must still work."""
    env = _env(tmp_path)   # tmp_path is NOT a git repo
    fake = ScriptedAdapter(["work done", "verified"], tmp_path)
    _wire(monkeypatch, fake)

    loop.run(tmp_path, env)

    t = tasks.find(env, "PRJ-001")
    assert t.stage_checkpoints == {}   # no repo -> no checkpoints, no crash

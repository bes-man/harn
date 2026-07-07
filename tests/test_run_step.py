"""loop.run_step: run (or rerun) exactly ONE agent turn for one step of a
task's own workflow plan, outside `run()`'s full multi-step cycle — the studio
UI's per-step Run/Rerun and `harn run --task ID --step STEP_ID [--rerun]`."""
from __future__ import annotations

import subprocess
from pathlib import Path

from harn import loop, tasks, gitutil, scaffold, workflows, ENV_DIRNAME
from harn.adapters.base import AgentResult
from .conftest import make_task


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _repo(tmp_path: Path) -> Path:
    _git(["init", "-q"], tmp_path)
    _git(["config", "user.email", "t@t.t"], tmp_path)
    _git(["config", "user.name", "t"], tmp_path)
    (tmp_path / "app.py").write_text("original\n")
    _git(["add", "-A"], tmp_path)
    _git(["commit", "-qm", "baseline"], tmp_path)
    return tmp_path


class WritingAdapter:
    """Writes distinguishable content to app.py each call, WITHOUT a trailing
    newline (so a test can tell whether a rerun undid the previous attempt's
    edit, and distinguish it from the '...\\n' baseline content)."""
    name = "fake"

    def __init__(self, project_root, texts=None):
        self.project_root = project_root
        self.texts = list(texts) if texts else None
        self.calls = 0

    def available(self):
        return True

    def run_turn(self, prompt, cwd, timeout=1800, **kw):
        self.calls += 1
        text = self.texts[min(self.calls - 1, len(self.texts) - 1)] if self.texts \
            else f"attempt {self.calls}"
        (self.project_root / "app.py").write_text(text)
        return AgentResult(ok=True, text=f"did work (call {self.calls})")


def _env(tmp_path: Path) -> Path:
    scaffold.setup(tmp_path)
    env = tmp_path / ENV_DIRNAME
    for p in (env / "tasks").glob("*.json"):
        p.unlink()
    make_task(env, "PRJ-001", title="Feat", priority=1)
    workflows.save_task_plan(env, "PRJ-001", {"preamble": "", "nodes": [
        {"kind": "step", "id": "step-000001", "title": "Implement",
         "body": "do it", "required": [], "tools": [], "enabled": True},
        {"kind": "step", "id": "step-000002", "title": "Verify",
         "body": "check it", "required": [], "tools": [], "enabled": True}]})
    (env / "harn.toml").write_text('[harn]\nagent = "fake"\n')
    return env


def _wire(monkeypatch, adapter):
    monkeypatch.setattr(loop, "get_adapter", lambda name: adapter)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])


def test_unknown_step_rejected(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    env = _env(root)
    r = loop.run_step(root, env, "PRJ-001", "not-a-real-step")
    assert r["ok"] is False and "unknown step id" in r["error"]


def test_unknown_task_rejected(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    env = _env(root)
    r = loop.run_step(root, env, "NOPE", "step-000001")
    assert r["ok"] is False


def test_run_step_executes_one_turn_and_checkpoints(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    env = _env(root)
    fake = WritingAdapter(root)
    _wire(monkeypatch, fake)

    r = loop.run_step(root, env, "PRJ-001", "step-000001")

    assert r["ok"] is True
    assert r["step_id"] == "step-000001"
    assert r["title"] == "Implement"
    assert fake.calls == 1
    assert (root / "app.py").read_text() == "attempt 1"
    t = tasks.find(env, "PRJ-001")
    assert "step-000001" in t.stage_checkpoints
    assert t.step_results["step-000001"]["status"] == "ok"


def test_run_step_runs_only_the_named_step_prompt(tmp_path, monkeypatch):
    """A run_step call for step-000002 must build ITS prompt (title/body),
    not step-000001's — proves the plan lookup wires the right node in."""
    root = _repo(tmp_path)
    env = _env(root)
    captured = {}

    class CapturingAdapter(WritingAdapter):
        def run_turn(self, prompt, cwd, timeout=1800, **kw):
            captured["prompt"] = prompt
            return super().run_turn(prompt, cwd, timeout=timeout, **kw)

    fake = CapturingAdapter(root)
    _wire(monkeypatch, fake)

    r = loop.run_step(root, env, "PRJ-001", "step-000002")
    assert r["ok"] is True
    assert fake.calls == 1
    assert "Verify" in captured["prompt"]
    assert "check it" in captured["prompt"]


def test_rerun_without_prior_checkpoint_still_runs(tmp_path, monkeypatch):
    """No checkpoint recorded yet for this step -> rollback_to is a no-op
    (best-effort), the turn still executes."""
    root = _repo(tmp_path)
    env = _env(root)
    fake = WritingAdapter(root)
    _wire(monkeypatch, fake)

    r = loop.run_step(root, env, "PRJ-001", "step-000001", rerun=True)
    assert r["ok"] is True


def test_rerun_discards_previous_attempts_edit(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    env = _env(root)
    fake = WritingAdapter(root, texts=["first attempt broke it",
                                       "second attempt, clean"])
    _wire(monkeypatch, fake)

    loop.run_step(root, env, "PRJ-001", "step-000001")
    assert (root / "app.py").read_text() == "first attempt broke it"

    r = loop.run_step(root, env, "PRJ-001", "step-000001", rerun=True)
    assert r["ok"] is True
    # rerun restores to BEFORE the first attempt (original), THEN the second
    # attempt's own edit lands on top of that clean state
    assert (root / "app.py").read_text() == "second attempt, clean"

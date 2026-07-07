"""loop.run_stage: run (or rerun) exactly ONE agent turn for one task, outside
`run()`'s full multi-stage cycle — the studio UI's per-step Run/Rerun and
`harn run --task ID --stage STAGE [--rerun]`."""
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
    (tmp_path / "app.py").write_text("original\n")
    _git(["add", "-A"], tmp_path)
    _git(["commit", "-qm", "baseline"], tmp_path)
    return tmp_path


class WritingAdapter:
    """Writes distinguishable content to app.py each call, so a test can tell
    whether a rerun undid the previous attempt's edit."""
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
            else f"attempt {self.calls}\n"
        (self.project_root / "app.py").write_text(text)
        return AgentResult(ok=True, text=f"did work (call {self.calls})")


def _env(tmp_path: Path) -> Path:
    scaffold.setup(tmp_path)
    env = tmp_path / ENV_DIRNAME
    for p in (env / "tasks").glob("*.json"):
        p.unlink()
    make_task(env, "PRJ-001", title="Feat", priority=1)
    (env / "harn.toml").write_text('[harn]\nagent = "fake"\n')
    return env


def _wire(monkeypatch, adapter):
    monkeypatch.setattr(loop, "get_adapter", lambda name: adapter)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])


def test_unknown_stage_rejected(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    env = _env(root)
    r = loop.run_stage(root, env, "PRJ-001", "not_a_real_stage")
    assert r["ok"] is False and "unknown stage" in r["error"]


def test_unknown_task_rejected(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    env = _env(root)
    r = loop.run_stage(root, env, "NOPE", "execute")
    assert r["ok"] is False and "no task" in r["error"]


def test_execute_stage_runs_and_checkpoints(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    env = _env(root)
    fake = WritingAdapter(root)
    _wire(monkeypatch, fake)

    r = loop.run_stage(root, env, "PRJ-001", "execute")

    assert r["ok"] is True and r["stage"] == "execute"
    assert (root / "app.py").read_text() == "attempt 1\n"
    t = tasks.find(env, "PRJ-001")
    assert "execute" in t.stage_checkpoints
    assert t.baseline_ref   # captured on first execute, same as the main loop


def test_rerun_without_prior_checkpoint_fails_cleanly(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    env = _env(root)
    r = loop.run_stage(root, env, "PRJ-001", "execute", rerun=True)
    assert r["ok"] is False and "no checkpoint" in r["error"]


def test_rerun_discards_previous_attempts_edit(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    env = _env(root)
    fake = WritingAdapter(root, texts=["first attempt broke it", "second attempt, clean"])
    _wire(monkeypatch, fake)

    loop.run_stage(root, env, "PRJ-001", "execute")
    assert (root / "app.py").read_text() == "first attempt broke it"

    r = loop.run_stage(root, env, "PRJ-001", "execute", rerun=True)
    assert r["ok"] is True
    # rerun restores to BEFORE the first attempt (original), THEN the second
    # attempt's own edit lands on top of that clean state
    assert (root / "app.py").read_text() == "second attempt, clean"


def test_rerun_restores_exact_pre_stage_state_even_with_extra_files(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    env = _env(root)

    class MessyAdapter(WritingAdapter):
        def run_turn(self, prompt, cwd, timeout=1800, **kw):
            self.calls += 1
            (self.project_root / "app.py").write_text("messed up\n")
            (self.project_root / "stray.py").write_text("junk\n")
            return AgentResult(ok=True, text="did work")

    fake = MessyAdapter(root)
    _wire(monkeypatch, fake)
    loop.run_stage(root, env, "PRJ-001", "execute")
    assert (root / "stray.py").exists()

    # rerun without the adapter writing anything new — proves the restore
    # itself (not a subsequent turn) is what cleans up
    class NoopAdapter(WritingAdapter):
        def run_turn(self, prompt, cwd, timeout=1800, **kw):
            return AgentResult(ok=True, text="noop")
    monkeypatch.setattr(loop, "get_adapter", lambda name: NoopAdapter(root))

    loop.run_stage(root, env, "PRJ-001", "execute", rerun=True)
    assert (root / "app.py").read_text() == "original\n"
    assert not (root / "stray.py").exists()


def test_verify_stage_reuses_run_verify_and_checkpoints(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    env = _env(root)
    fake = WritingAdapter(root, texts=["looks correct\nVERIFY: PASS"])
    _wire(monkeypatch, fake)

    r = loop.run_stage(root, env, "PRJ-001", "verify")
    assert r["ok"] is True and r["stage"] == "verify"
    t = tasks.find(env, "PRJ-001")
    assert "verify" in t.stage_checkpoints


def test_oracle_stage_runs_in_isolation(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    env = _env(root)
    fake = WritingAdapter(root, texts=["PASS: all good"])
    monkeypatch.setattr(loop, "_pick_oracle_adapter", lambda cfg: fake)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])

    r = loop.run_stage(root, env, "PRJ-001", "oracle")
    assert r["ok"] is True and r["stage"] == "oracle"
    t = tasks.find(env, "PRJ-001")
    assert "oracle" in t.stage_checkpoints


def test_reconcile_stage_runs_in_isolation(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    env = _env(root)
    fake = WritingAdapter(root, texts=["RECONCILE: DONE"])
    _wire(monkeypatch, fake)

    r = loop.run_stage(root, env, "PRJ-001", "reconcile")
    assert r["ok"] is True and r["stage"] == "reconcile"
    t = tasks.find(env, "PRJ-001")
    assert "reconcile" in t.stage_checkpoints

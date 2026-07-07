"""The step engine end-to-end: harn run walks the TASK'S OWN plan —
arbitrary step count, per-step agent/model, resume, checkpoints."""
from __future__ import annotations

from harn import loop, tasks, workflows, scaffold, gitutil, ENV_DIRNAME
from harn.adapters.base import AgentResult
from .conftest import make_task
import subprocess


class RecordingAdapter:
    name = "fake"
    def __init__(self):
        self.calls = []
    def available(self):
        return True
    def run_turn(self, prompt, cwd, timeout=1800, *, model=None, effort=None,
                temperature=None):
        self.calls.append({"prompt": prompt, "model": model, "effort": effort})
        return AgentResult(ok=True, text="done")


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True)


def _project(tmp_path, n_steps=10):
    _git(["init", "-q"], tmp_path); _git(["config", "user.email", "t@t"], tmp_path)
    _git(["config", "user.name", "t"], tmp_path)
    scaffold.setup(tmp_path)
    env = tmp_path / ENV_DIRNAME
    for p in (env / "tasks").glob("*"):
        p.unlink()
    (env / "harn.toml").write_text(
        '[harn]\nagent = "fake"\n[feedback]\ntest_cmd = ""\nrequire_tests = false\n'
        "[loop]\nmax_iterations = 40\n[notify]\nwait_for_reply = false\n")
    _git(["add", "-A"], tmp_path); _git(["commit", "-qm", "base"], tmp_path)
    t = make_task(env, "PRJ-001", title="Feat")
    plan = {"preamble": "", "nodes": [
        {"kind": "step", "title": f"Step {i}", "body": f"do part {i}",
         "id": f"step-{i:06x}", "agent": "", "model": "", "effort": "",
         "temperature": "", "required": [], "tools": [], "enabled": True}
        for i in range(1, n_steps + 1)]}
    workflows.save_task_plan(env, t.id, plan)
    return env, t


def test_engine_runs_every_enabled_step_in_order(tmp_path, monkeypatch):
    env, t = _project(tmp_path, n_steps=10)
    fake = RecordingAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])
    loop.run(tmp_path, env)
    assert len(fake.calls) == 10
    for i, c in enumerate(fake.calls, 1):
        assert f"Step {i}" in c["prompt"]


def test_per_step_model_and_agent_route_correctly(tmp_path, monkeypatch):
    env, t = _project(tmp_path, n_steps=3)
    plan = workflows.load_task_plan(env, t.id)
    plan["nodes"][0]["model"] = "opus"
    plan["nodes"][1]["agent"] = "other"
    workflows.save_task_plan(env, t.id, plan)
    fake, other = RecordingAdapter(), RecordingAdapter()
    other.name = "other"
    reg = {"fake": fake, "other": other}
    monkeypatch.setattr(loop, "get_adapter", lambda n: reg.get(n, fake))
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])
    loop.run(tmp_path, env)
    assert fake.calls[0]["model"] == "opus"      # step 1 override
    assert len(other.calls) == 1                 # step 2 ran on 'other'
    assert "Step 2" in other.calls[0]["prompt"]


def test_ledger_records_ok_and_resume_skips_done_steps(tmp_path, monkeypatch):
    env, t = _project(tmp_path, n_steps=4)
    t = tasks.find(env, t.id)
    t.step_results["step-000001"] = {"status": "ok"}
    tasks._save(t)
    fake = RecordingAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])
    loop.run(tmp_path, env)
    assert len(fake.calls) == 3                  # step 1 skipped
    assert "Step 2" in fake.calls[0]["prompt"]
    fresh = tasks.find(env, t.id)
    assert all(fresh.step_results[f"step-{i:06x}"]["status"] == "ok"
              for i in range(1, 5))


def test_disabled_step_is_skipped(tmp_path, monkeypatch):
    env, t = _project(tmp_path, n_steps=3)
    plan = workflows.load_task_plan(env, t.id)
    plan["nodes"][1]["enabled"] = False
    workflows.save_task_plan(env, t.id, plan)
    fake = RecordingAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])
    loop.run(tmp_path, env)
    assert len(fake.calls) == 2
    assert "Step 2" not in "".join(c["prompt"] for c in fake.calls)


def test_checkpoint_per_step_id(tmp_path, monkeypatch):
    env, t = _project(tmp_path, n_steps=2)
    fake = RecordingAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])
    loop.run(tmp_path, env)
    fresh = tasks.find(env, t.id)
    assert set(fresh.stage_checkpoints) == {"step-000001", "step-000002"}

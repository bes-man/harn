"""Per-task workflow snapshot: each task carries its OWN execution plan
(harn_env/tasks/<id>.workflow.json), copied from its preset at creation.
Presets are templates; editing one never touches existing tasks' plans."""
from __future__ import annotations

from harn import tasks, workflows, workflow, ENV_DIRNAME


def _env(tmp_path):
    env = tmp_path / ENV_DIRNAME
    (env / "tasks").mkdir(parents=True)
    workflow.write(env)
    return env


def test_create_task_snapshots_default_plan(tmp_path):
    env = _env(tmp_path)
    t = tasks.create_task(env, "Add auth")
    plan = workflows.load_task_plan(env, t.id)
    assert plan is not None
    steps = [n for n in plan["nodes"] if n["kind"] == "step"]
    assert steps and all(n["id"] for n in steps)   # ids stamped


def test_snapshot_is_isolated_from_preset(tmp_path):
    env = _env(tmp_path)
    t = tasks.create_task(env, "Add auth")
    plan = workflows.load_task_plan(env, t.id)
    step = next(n for n in plan["nodes"] if n["kind"] == "step")
    step["agent"] = "cursor"
    workflows.save_task_plan(env, t.id, plan)
    # the default preset/WORKFLOW.md is untouched
    global_parsed = workflow.parse(env)
    assert all(n.get("agent", "") == "" for n in global_parsed["nodes"])
    # and a NEW task doesn't inherit the edit
    t2 = tasks.create_task(env, "Other")
    plan2 = workflows.load_task_plan(env, t2.id)
    assert all(n.get("agent", "") == "" for n in plan2["nodes"])


def test_snapshot_idempotent(tmp_path):
    env = _env(tmp_path)
    t = tasks.create_task(env, "Add auth")
    plan = workflows.load_task_plan(env, t.id)
    step = next(n for n in plan["nodes"] if n["kind"] == "step")
    step["model"] = "opus"
    workflows.save_task_plan(env, t.id, plan)
    again = workflows.snapshot_for_task(env, t.id, None)   # second call
    s2 = next(n for n in again["nodes"] if n["kind"] == "step")
    assert s2["model"] == "opus"   # existing snapshot NOT overwritten


def test_activate_task_renders_snapshot_into_workflow_md(tmp_path):
    env = _env(tmp_path)
    t = tasks.create_task(env, "Add auth")
    plan = workflows.load_task_plan(env, t.id)
    plan["nodes"] = [n for n in plan["nodes"] if n["kind"] != "step"] + [
        {"kind": "step", "title": "Only step", "body": "do it", "id": "step-aaaaaa",
         "agent": "", "model": "", "effort": "", "temperature": "",
         "required": [], "tools": [], "enabled": True}]
    workflows.save_task_plan(env, t.id, plan)
    assert workflows.activate_task(env, t.id) is True
    assert "Only step" in (env / "WORKFLOW.md").read_text()


def test_step_results_ledger_round_trips(tmp_path):
    env = _env(tmp_path)
    t = tasks.create_task(env, "Add auth")
    t.step_results["step-aaaaaa"] = {"status": "ok", "tokens": 1234}
    tasks._save(t)
    fresh = tasks.find(env, t.id)
    assert fresh.step_results == {"step-aaaaaa": {"status": "ok", "tokens": 1234}}

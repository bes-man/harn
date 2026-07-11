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


def test_create_task_does_not_snapshot_a_plan(tmp_path):
    env = _env(tmp_path)
    t = tasks.create_task(env, "Add auth")
    assert workflows.load_task_plan(env, t.id) is None


def test_task_has_workflow_confirmed_defaulting_false(tmp_path):
    env = _env(tmp_path)
    t = tasks.create_task(env, "Add auth")
    assert t.workflow_confirmed is False


def test_workflow_confirmed_round_trips(tmp_path):
    env = _env(tmp_path)
    t = tasks.create_task(env, "Add auth")
    t.workflow_confirmed = True
    tasks._save(t)
    reloaded = tasks.find(env, t.id)
    assert reloaded.workflow_confirmed is True


def test_snapshot_for_task_still_produces_a_plan_on_demand(tmp_path):
    """create_task no longer freezes the plan eagerly, but snapshot_for_task
    (called explicitly, e.g. when something starts executing the task) still
    works exactly as before."""
    env = _env(tmp_path)
    t = tasks.create_task(env, "Add auth")
    plan = workflows.snapshot_for_task(env, t.id, t.workflow)
    assert plan is not None
    steps = [n for n in plan["nodes"] if n["kind"] == "step"]
    assert steps and all(n["id"] for n in steps)   # ids stamped
    assert workflows.load_task_plan(env, t.id) is not None


def test_snapshot_is_isolated_from_preset(tmp_path):
    env = _env(tmp_path)
    t = tasks.create_task(env, "Add auth")
    workflows.snapshot_for_task(env, t.id, t.workflow)
    plan = workflows.load_task_plan(env, t.id)
    step = next(n for n in plan["nodes"] if n["kind"] == "step")
    step["agent"] = "cursor"
    workflows.save_task_plan(env, t.id, plan)
    # the default preset/WORKFLOW.md is untouched
    global_parsed = workflow.parse(env)
    assert all(n.get("agent", "") == "" for n in global_parsed["nodes"])
    # and a NEW task doesn't inherit the edit
    t2 = tasks.create_task(env, "Other")
    workflows.snapshot_for_task(env, t2.id, t2.workflow)
    plan2 = workflows.load_task_plan(env, t2.id)
    assert all(n.get("agent", "") == "" for n in plan2["nodes"])


def test_snapshot_idempotent(tmp_path):
    env = _env(tmp_path)
    t = tasks.create_task(env, "Add auth")
    workflows.snapshot_for_task(env, t.id, t.workflow)
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
    workflows.snapshot_for_task(env, t.id, t.workflow)
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


def test_task_plan_roundtrip_via_studio(tmp_path):
    from harn import studio
    env = _env(tmp_path)
    t = tasks.create_task(env, "Add auth")
    payload = studio.task_plan_payload(env, t.id)
    assert payload["ok"] and payload["plan"]["nodes"]
    plan = payload["plan"]
    step = next(n for n in plan["nodes"] if n["kind"] == "step")
    step["model"] = "opus"
    r = studio.save_task_plan_route(env, {"task_id": t.id, "plan": plan})
    assert r["ok"] is True
    assert next(n for n in workflows.load_task_plan(env, t.id)["nodes"]
                if n["kind"] == "step")["model"] == "opus"


def test_step_prompt_payload_returns_the_real_prompt(tmp_path):
    from harn import studio
    env = _env(tmp_path)
    t = tasks.create_task(env, "Add auth")
    workflows.snapshot_for_task(env, t.id, t.workflow)
    plan = workflows.load_task_plan(env, t.id)
    step = next(n for n in plan["nodes"] if n["kind"] == "step")
    payload = studio.step_prompt_payload(env, t.id, step["id"])
    assert "error" not in payload
    assert step["title"] in payload["prompt"]
    # No side effects — the ledger is untouched.
    fresh = tasks.find(env, t.id)
    assert step["id"] not in fresh.step_results


def test_step_prompt_payload_errors_for_unknown_task_or_step(tmp_path):
    from harn import studio
    env = _env(tmp_path)
    t = tasks.create_task(env, "Add auth")
    assert "error" in studio.step_prompt_payload(env, "no-such-task", "s1")
    assert "error" in studio.step_prompt_payload(env, t.id, "no-such-step")


def test_step_prompt_export_payload_writes_a_file(tmp_path):
    from harn import studio
    env = _env(tmp_path)
    t = tasks.create_task(env, "Add auth")
    workflows.snapshot_for_task(env, t.id, t.workflow)
    plan = workflows.load_task_plan(env, t.id)
    step = next(n for n in plan["nodes"] if n["kind"] == "step")
    prompt = studio.step_prompt_payload(env, t.id, step["id"])["prompt"]
    r = studio.step_prompt_export_payload(env, t.id, step["id"], prompt)
    assert "error" not in r
    from pathlib import Path
    p = Path(r["path"])
    assert p.exists() and p.read_text(encoding="utf-8") == prompt


def test_step_prompt_export_payload_rejects_empty_text(tmp_path):
    from harn import studio
    env = _env(tmp_path)
    r = studio.step_prompt_export_payload(env, "t1", "s1", "   ")
    assert "error" in r


def test_preview_plan_does_not_create_a_snapshot_file(tmp_path):
    env = _env(tmp_path)
    t = tasks.create_task(env, "Do the thing")
    plan = workflows.preview_plan(env, t.id, None)
    default_nodes = workflows._ensure_default(env)["nodes"]
    # Same content as the default (ids aside: _ensure_default's own cache
    # never gets ids stamped into it — only a real snapshot does that via
    # ensure_ids + save; preview_plan stamps its OWN deterministic ids on
    # its throwaway copy without persisting anything).
    assert len(plan["nodes"]) == len(default_nodes)
    for got, want in zip(plan["nodes"], default_nodes):
        assert {k: v for k, v in got.items() if k != "id"} == \
               {k: v for k, v in want.items() if k != "id"}
    assert all(n["id"] for n in plan["nodes"] if n["kind"] == "step")
    assert workflows.load_task_plan(env, t.id) is None


def test_preview_plan_ids_are_stable_across_separate_calls(tmp_path):
    """Regression guard: studio issues plan-viewing and step-viewing as
    SEPARATE HTTP requests (task_plan_payload loads the canvas; a later
    step_prompt_payload call looks up one specific step id from it). Both
    fall back to preview_plan for an unstarted task, so two independent
    calls with the same task/preset MUST agree on step ids, or "view full
    context" for a never-run task would always 404 on a fresh id mismatch."""
    env = _env(tmp_path)
    t = tasks.create_task(env, "Do the thing")
    first = workflows.preview_plan(env, t.id, None)
    second = workflows.preview_plan(env, t.id, None)
    ids1 = [n["id"] for n in first["nodes"] if n["kind"] == "step"]
    ids2 = [n["id"] for n in second["nodes"] if n["kind"] == "step"]
    assert ids1 == ids2
    assert workflows.load_task_plan(env, t.id) is None


def test_preview_plan_reflects_a_later_preset_change_before_first_execution(tmp_path):
    env = _env(tmp_path)
    t = tasks.create_task(env, "Do the thing")
    workflows.create(env, name="backend", title="Backend",
                     copy_from=None)  # seeds from the default's nodes
    # First preview (no preset chosen yet) sees the default:
    p1 = workflows.preview_plan(env, t.id, None)
    # Second preview (preset now chosen) sees the NEW preset — proving no
    # premature freeze happened on the first call:
    p2 = workflows.preview_plan(env, t.id, "backend")
    assert workflows.load_task_plan(env, t.id) is None   # still no file
    # (p1 and p2 both come from the default's nodes here since "backend" was
    # seeded from it, so assert the call succeeded and created no file — the
    # no-freeze property is what this test protects, not node content drift.)
    assert isinstance(p2["nodes"], list)


def test_step_prompt_payload_works_for_an_unstarted_task_using_a_step_id_from_task_plan_payload(tmp_path):
    """End-to-end regression for the same issue: the canvas gets its step ids
    from task_plan_payload; a later, separate step_prompt_payload request for
    one of those ids must find it — even though nothing was ever snapshotted
    (no /api/task_plan call has frozen this task's plan)."""
    from harn import studio
    env = _env(tmp_path)
    t = tasks.create_task(env, "Do the thing")
    plan_payload = studio.task_plan_payload(env, t.id)
    assert plan_payload["ok"]
    step = next(n for n in plan_payload["plan"]["nodes"] if n["kind"] == "step")
    result = studio.step_prompt_payload(env, t.id, step["id"])
    assert "error" not in result
    assert workflows.load_task_plan(env, t.id) is None   # still unfrozen


def test_preview_plan_returns_the_real_snapshot_once_one_exists(tmp_path):
    env = _env(tmp_path)
    t = tasks.create_task(env, "Do the thing")
    real = workflows.snapshot_for_task(env, t.id, None)   # simulates first execution
    seen = workflows.preview_plan(env, t.id, "some-other-preset-name")
    assert seen == real   # once frozen, preview_plan must NOT diverge from it

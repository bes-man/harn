"""WORKFLOW.md: structured flow, per-step required skills, non-clobbering refresh,
agent-agnostic wiring, MCP read/save tools."""
from __future__ import annotations

import re as re_mod
from pathlib import Path

from harn import workflow, scaffold, skills, ENV_DIRNAME


# --- render / structure ---------------------------------------------------- #

def test_render_includes_full_flow(tmp_path):
    env = tmp_path / ENV_DIRNAME
    env.mkdir()
    text = workflow.render(env)
    for marker in ("Session start", "Pre-task protocol", "Tests", "Verify",
                   "Submit for review", "Reconcile", "Oracle"):
        assert marker in text
    assert "get_next_task" in text and "submit_for_review" in text


def test_render_has_per_step_required_skills(tmp_path):
    env = tmp_path / ENV_DIRNAME
    env.mkdir()
    text = workflow.render(env)
    assert "Skills (required:" in text
    # the snapshot block is delimited so refresh can target it
    assert workflow._SNAP_START in text and workflow._SNAP_END in text


def test_render_skills_snapshot_is_soft(tmp_path):
    env = tmp_path / ENV_DIRNAME
    env.mkdir()
    skills.append_learning(env, "frontend", "React 18")
    text = workflow.render(env)
    assert "NOT a fixed set" in text
    assert "frontend" in text


# --- required_skills parser ------------------------------------------------ #

def test_required_skills_parsed_per_step(tmp_path):
    env = tmp_path / ENV_DIRNAME
    workflow.write(env)
    req = workflow.required_skills(env)
    # every value is a non-empty skill list; empty `Skills (required: )` skipped
    assert all(v for v in req.values())
    flat = {s for v in req.values() for s in v}
    assert "standards" in flat        # required on multiple steps
    assert "testing" in flat          # required on the Tests step
    # a step heading maps to its declared skills
    pre = next((v for k, v in req.items() if "Pre-task" in k), [])
    assert "standards" in pre and "constraints" in pre


def test_required_skills_empty_without_file(tmp_path):
    env = tmp_path / ENV_DIRNAME
    env.mkdir()
    assert workflow.required_skills(env) == {}


# --- write is non-clobbering; refresh preserves edits ---------------------- #

def test_write_does_not_clobber_user_edits(tmp_path):
    env = tmp_path / ENV_DIRNAME
    workflow.write(env)
    p = env / "WORKFLOW.md"
    p.write_text(p.read_text() + "\n<!-- my custom note -->\n", encoding="utf-8")
    workflow.write(env)  # second call must NOT overwrite
    assert "my custom note" in p.read_text(encoding="utf-8")


def test_force_resets_to_template(tmp_path):
    env = tmp_path / ENV_DIRNAME
    workflow.write(env)
    p = env / "WORKFLOW.md"
    p.write_text("garbage", encoding="utf-8")
    workflow.write(env, force=True)
    assert "Project workflow" in p.read_text(encoding="utf-8")


def test_refresh_updates_snapshot_keeps_steps(tmp_path):
    env = tmp_path / ENV_DIRNAME
    workflow.write(env)
    p = env / "WORKFLOW.md"
    # user edits a step
    edited = p.read_text().replace("One focused change.",
                                   "One focused change. <!-- keep small -->")
    p.write_text(edited, encoding="utf-8")
    # a new skill appears, refresh the snapshot
    skills.append_learning(env, "database", "Postgres 16")
    workflow.refresh_skills(env)
    out = p.read_text(encoding="utf-8")
    assert "keep small" in out           # edit preserved
    assert "database" in out             # snapshot refreshed
    # snapshot markers still present and singular
    assert out.count(workflow._SNAP_START) == 1


# --- per-step execution fields (Id / Agent / Model / Effort / Temperature) - #

def test_parse_defaults_step_fields_to_empty(tmp_path):
    env = tmp_path / ENV_DIRNAME
    workflow.write(env)
    parsed = workflow.parse(env)
    for n in parsed["nodes"]:
        if n["kind"] == "step":
            assert n["id"] == "" and n["agent"] == "" and n["model"] == ""
            assert n["effort"] == "" and n["temperature"] == ""


def test_step_fields_round_trip_through_compose(tmp_path):
    env = tmp_path / ENV_DIRNAME
    workflow.write(env)
    parsed = workflow.parse(env)
    step = next(n for n in parsed["nodes"] if n["title"] == "Implement")
    step.update(id="step-3f8a9c", agent="cursor", model="composer-1",
                effort="high", temperature="0.2")
    workflow.save_parsed(env, parsed)
    re = workflow.parse(env)
    s2 = next(n for n in re["nodes"] if n["title"] == "Implement")
    assert s2["id"] == "step-3f8a9c" and s2["agent"] == "cursor"
    assert s2["model"] == "composer-1" and s2["effort"] == "high"
    assert s2["temperature"] == "0.2"
    # other steps untouched
    other = next(n for n in re["nodes"] if n["title"] == "Session start — orient")
    assert other["agent"] == ""            # untouched fields stay empty
    # save_parsed stamps a stable id on EVERY step (spec: ids on first save,
    # so task snapshots inherit the preset's ids)
    assert re_mod.fullmatch(r"step-[0-9a-f]{6}", other["id"])


def test_id_survives_rename(tmp_path):
    env = tmp_path / ENV_DIRNAME
    workflow.write(env)
    parsed = workflow.parse(env)
    step = next(n for n in parsed["nodes"] if n["title"] == "Implement")
    step["id"] = "step-aa11bb"
    step["title"] = "Build the thing"
    workflow.save_parsed(env, parsed)
    re = workflow.parse(env)
    assert next(n for n in re["nodes"]
                if n["title"] == "Build the thing")["id"] == "step-aa11bb"


def test_ensure_ids_fills_only_missing(tmp_path):
    env = tmp_path / ENV_DIRNAME
    workflow.write(env)
    parsed = workflow.parse(env)
    step = next(n for n in parsed["nodes"] if n["title"] == "Implement")
    step["id"] = "step-keepme"
    out = workflow.ensure_ids(parsed)
    ids = [n["id"] for n in out["nodes"] if n["kind"] == "step"]
    assert all(ids), "every step got an id"
    assert "step-keepme" in ids
    assert len(set(ids)) == len(ids), "ids are unique"
    for i in ids:
        if i != "step-keepme":
            assert re_mod.fullmatch(r"step-[0-9a-f]{6}", i), i


def test_legacy_stage_line_is_dropped_silently(tmp_path):
    env = tmp_path / ENV_DIRNAME
    workflow.write(env)
    p = env / "WORKFLOW.md"
    p.write_text(p.read_text().replace(
        "## 3. Implement", "## 3. Implement\nStage: execute"), encoding="utf-8")
    parsed = workflow.parse(env)
    step = next(n for n in parsed["nodes"] if n["title"] == "Implement")
    assert "Stage:" not in step["body"]
    assert "stage" not in step  # the key no longer exists


# --- setup / onboard wiring ------------------------------------------------ #

def test_setup_writes_workflow_and_lists_it(tmp_path):
    result = scaffold.setup(tmp_path)
    wf = tmp_path / ENV_DIRNAME / "WORKFLOW.md"
    assert wf.exists()
    assert "WORKFLOW.md" in result["created"]
    body = wf.read_text(encoding="utf-8")
    assert "security" in body and "standards" in body  # template skills present


def test_agents_md_points_to_workflow(tmp_path):
    scaffold.setup(tmp_path)
    agents = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert "read_workflow" in agents  # every agent is told to load it


# --- MCP tools ------------------------------------------------------------- #

def _tool(env: Path, name: str, monkeypatch):
    monkeypatch.setenv("HARN_ENV_DIR", str(env))
    monkeypatch.setattr("harn.mcp_server._ensure_watch_running", lambda e: None)
    from harn import mcp_server as ms
    srv = ms.build_server()
    return next(t.fn for t in srv._tool_manager._tools.values() if t.name == name)


def test_mcp_read_and_save_workflow(tmp_path, monkeypatch):
    scaffold.setup(tmp_path)
    env = tmp_path / ENV_DIRNAME
    read_fn = _tool(env, "read_workflow", monkeypatch)
    assert "Project workflow" in read_fn()

    save_fn = _tool(env, "save_workflow", monkeypatch)
    custom = "# Project workflow\n\n## 1. Do thing\nSkills (required: standards)\n"
    out = save_fn(content=custom)
    assert "saved" in out
    assert (env / "WORKFLOW.md").read_text(encoding="utf-8") == custom

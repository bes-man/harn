"""WORKFLOW.md: structured flow, per-step required skills, non-clobbering refresh,
agent-agnostic wiring, MCP read/save tools."""
from __future__ import annotations

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

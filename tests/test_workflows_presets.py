"""Named workflow presets (harn/workflows.py) + studio + task wiring.

A task can run under a named workflow; the loop renders it into WORKFLOW.md so
every agent (Claude/Codex/Cursor) reads the right flow from the one file they
all read. The project default (WORKFLOW.md) is used when no preset is chosen and
must survive switching to a preset and back — including manual edits.
"""
from __future__ import annotations

from harn import workflow, workflows, tasks, studio, ENV_DIRNAME


def _env(tmp_path):
    env = tmp_path / ENV_DIRNAME
    (env / "tasks").mkdir(parents=True)
    return env


# --- default seeding + listing --------------------------------------------- #

def test_default_is_seeded_from_workflow_md(tmp_path):
    env = _env(tmp_path)
    wf = workflows.load(env, "default")
    assert wf["name"] == "default"
    assert wf["nodes"], "default should mirror WORKFLOW.md steps"
    assert (env / workflow.FILENAME).exists()
    assert workflows.active_name(env) == "default"


def test_list_includes_default_first(tmp_path):
    env = _env(tmp_path)
    workflows.create(env, name="qa", title="QA", description="testing")
    names = [w["name"] for w in workflows.list_workflows(env)]
    assert names[0] == "default" and "qa" in names


# --- create / meta / slug -------------------------------------------------- #

def test_create_slugifies_and_seeds_from_default(tmp_path):
    env = _env(tmp_path)
    m = workflows.create(env, name="QA Sprint!", title="QA", version="2")
    assert m["name"] == "qa-sprint" and m["version"] == "2"
    wf = workflows.load(env, "qa-sprint")
    assert wf["nodes"] == workflows.load(env, "default")["nodes"]


def test_create_rejects_default_name(tmp_path):
    env = _env(tmp_path)
    import pytest
    with pytest.raises(ValueError):
        workflows.create(env, name="default")


def test_meta_rename(tmp_path):
    env = _env(tmp_path)
    workflows.create(env, name="old", title="Old")
    m = workflows.save_meta(env, "old", title="New", description="d",
                            version="3", new_name="fresh")
    assert m["name"] == "fresh" and m["title"] == "New" and m["version"] == "3"
    assert workflows.load(env, "old") is None
    assert workflows.load(env, "fresh") is not None


def test_default_cannot_be_renamed_or_deleted(tmp_path):
    env = _env(tmp_path)
    workflows.save_meta(env, "default", new_name="something")
    assert workflows.load(env, "default") is not None
    assert workflows.delete(env, "default") is False


# --- activation renders WORKFLOW.md (cross-agent contract) ------------------ #

def test_activate_renders_preset_into_workflow_md(tmp_path):
    env = _env(tmp_path)
    workflows.create(env, name="qa", title="QA")
    wf = workflows.load(env, "qa")
    wf["nodes"] = [{"title": "QA only", "body": "run tests", "kind": "step",
                    "enabled": True, "required": ["testing"], "tools": ["run_tests"]}]
    workflows.save(env, wf)
    workflows.activate(env, "qa")
    assert workflows.active_name(env) == "qa"
    md = (env / workflow.FILENAME).read_text()
    assert "QA only" in md
    # required_skills still parses the rendered file
    assert any("testing" in v for v in workflow.required_skills(env).values())


def test_switch_back_to_default_restores_it(tmp_path):
    env = _env(tmp_path)
    workflows.create(env, name="qa", title="QA")
    wf = workflows.load(env, "qa")
    wf["nodes"] = [{"title": "QA only", "body": "", "kind": "step",
                    "enabled": True, "required": [], "tools": []}]
    workflows.save(env, wf)
    workflows.activate(env, "qa")
    workflows.activate(env, "")          # back to default
    assert workflows.active_name(env) == "default"
    assert "QA only" not in (env / workflow.FILENAME).read_text()


def test_manual_default_edit_survives_switch(tmp_path):
    """While the default is active WORKFLOW.md is authoritative — a hand edit must
    be captured before switching to a preset and restored on switching back."""
    env = _env(tmp_path)
    workflow.write(env)                   # ensure WORKFLOW.md exists
    p = env / workflow.FILENAME
    p.write_text(p.read_text() + "\n## ZZ Manual\nSkills (required: standards)\n")
    workflows.create(env, name="qa", title="QA")
    workflows.activate(env, "qa")        # leaving default snapshots the edit
    workflows.activate(env, "default")   # restore
    assert "ZZ Manual" in (env / workflow.FILENAME).read_text()


def test_unknown_preset_falls_back_to_default(tmp_path):
    env = _env(tmp_path)
    assert workflows.activate(env, "nope") == "default"


def test_delete_active_preset_reverts_to_default(tmp_path):
    env = _env(tmp_path)
    workflows.create(env, name="qa", title="QA")
    workflows.activate(env, "qa")
    assert workflows.active_name(env) == "qa"
    workflows.delete(env, "qa")
    assert workflows.active_name(env) == "default"


# --- task wiring ----------------------------------------------------------- #

def test_task_persists_workflow_field(tmp_path):
    env = _env(tmp_path)
    t = tasks.create_task(env, "do thing", workflow="qa-sprint")
    assert tasks.find(env, t.id).workflow == "qa-sprint"


def test_task_without_workflow_is_none(tmp_path):
    env = _env(tmp_path)
    t = tasks.create_task(env, "do thing")
    assert tasks.find(env, t.id).workflow is None


# --- studio integration ---------------------------------------------------- #

def test_state_payload_exposes_switcher(tmp_path):
    env = _env(tmp_path)
    sp = studio.state_payload(env)
    assert "workflows" in sp and "active" in sp
    assert sp["active"] == "default"


def test_tools_catalog_reuses_real_mcp_docstrings(tmp_path):
    """Tools tab descriptions come from the actual MCP tool docstrings — single
    source of truth, never hand-duplicated and never drifts from what the agent
    itself reads via tools/list."""
    env = _env(tmp_path)
    payload = studio.tools_catalog_payload(env)
    assert "get_next_task" in payload["tools"]
    assert "highest-priority" in payload["tools"]["get_next_task"]


def test_apply_workflow_saves_to_active_preset(tmp_path):
    env = _env(tmp_path)
    studio.create_workflow(env, {"name": "design", "title": "Design"})
    studio.apply_workflow(env, {"preamble": "", "nodes": [
        {"title": "Audit", "body": "", "kind": "step", "enabled": True,
         "required": ["ui"], "tools": []}]})
    # saved to the active preset's JSON AND mirrored to WORKFLOW.md
    assert "Audit" in workflows.load(env, "design")["nodes"][0]["title"]
    assert "Audit" in (env / workflow.FILENAME).read_text()
    # the default preset is untouched
    assert all(n["title"] != "Audit" for n in workflows.load(env, "default")["nodes"])

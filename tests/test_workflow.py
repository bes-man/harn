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


# --- command steps + on-fail (Phase 2) --------------------------------- #

def test_parse_defaults_type_command_onfail_to_empty(tmp_path):
    env = tmp_path / ENV_DIRNAME
    workflow.write(env)
    parsed = workflow.parse(env)
    for n in parsed["nodes"]:
        if n["kind"] == "step":
            assert n["type"] == "" and n["command"] == "" and n["on_fail"] == ""


def test_command_step_round_trips_through_compose(tmp_path):
    env = tmp_path / ENV_DIRNAME
    workflow.write(env)
    parsed = workflow.parse(env)
    tests_step = next(n for n in parsed["nodes"] if n["title"] == "Tests")
    implement_step = next(n for n in parsed["nodes"] if n["title"] == "Implement")
    tests_step["type"] = "command"
    tests_step["command"] = "npm test"
    tests_step["on_fail"] = implement_step["id"] or "step-aaaaaa"
    if not implement_step["id"]:
        implement_step["id"] = "step-aaaaaa"
    workflow.save_parsed(env, parsed)

    reparsed = workflow.parse(env)
    t2 = next(n for n in reparsed["nodes"] if n["title"] == "Tests")
    assert t2["type"] == "command"
    assert t2["command"] == "npm test"
    assert t2["on_fail"] == implement_step["id"]
    # other steps stay untouched
    other = next(n for n in reparsed["nodes"] if n["title"] == "Verify")
    assert other["type"] == "" and other["command"] == "" and other["on_fail"] == ""


def test_compose_writes_onfail_as_target_title_not_id(tmp_path):
    env = tmp_path / ENV_DIRNAME
    workflow.write(env)
    parsed = workflow.parse(env)
    workflow.ensure_ids(parsed)
    tests_step = next(n for n in parsed["nodes"] if n["title"] == "Tests")
    implement_step = next(n for n in parsed["nodes"] if n["title"] == "Implement")
    tests_step["type"] = "command"
    tests_step["command"] = "npm test"
    tests_step["on_fail"] = implement_step["id"]
    text = workflow.compose(env, parsed)
    assert "On fail: Implement" in text
    # the On fail line carries the target's title, never its raw id (the id
    # itself legitimately appears elsewhere, in the target's own `Id:` line)
    assert f"On fail: {implement_step['id']}" not in text


def test_onfail_survives_target_rename(tmp_path):
    env = tmp_path / ENV_DIRNAME
    workflow.write(env)
    parsed = workflow.parse(env)
    workflow.ensure_ids(parsed)
    tests_step = next(n for n in parsed["nodes"] if n["title"] == "Tests")
    implement_step = next(n for n in parsed["nodes"] if n["title"] == "Implement")
    tests_step["type"] = "command"
    tests_step["command"] = "npm test"
    tests_step["on_fail"] = implement_step["id"]
    implement_step["title"] = "Build the thing"
    workflow.save_parsed(env, parsed)

    reparsed = workflow.parse(env)
    t2 = next(n for n in reparsed["nodes"] if n["title"] == "Tests")
    renamed = next(n for n in reparsed["nodes"] if n["title"] == "Build the thing")
    assert t2["on_fail"] == renamed["id"]


def test_onfail_dangling_reference_drops_to_empty_on_parse(tmp_path):
    env = tmp_path / ENV_DIRNAME
    workflow.write(env)
    p = env / "WORKFLOW.md"
    p.write_text(p.read_text().replace(
        "## 4. Tests", "## 4. Tests\nType: command\nCommand: npm test\n"
        "On fail: Not A Real Step Title"), encoding="utf-8")
    parsed = workflow.parse(env)
    step = next(n for n in parsed["nodes"] if n["title"] == "Tests")
    assert step["type"] == "command" and step["command"] == "npm test"
    assert step["on_fail"] == ""


def test_compose_omits_onfail_line_when_target_deleted(tmp_path):
    env = tmp_path / ENV_DIRNAME
    workflow.write(env)
    parsed = workflow.parse(env)
    workflow.ensure_ids(parsed)
    tests_step = next(n for n in parsed["nodes"] if n["title"] == "Tests")
    implement_step = next(n for n in parsed["nodes"] if n["title"] == "Implement")
    tests_step["type"] = "command"
    tests_step["command"] = "npm test"
    tests_step["on_fail"] = implement_step["id"]
    parsed["nodes"] = [n for n in parsed["nodes"] if n["title"] != "Implement"]
    text = workflow.compose(env, parsed)
    tests_block = text[text.index("## 4. Tests"):]
    tests_block = tests_block[:tests_block.index("\n## ", 1)] if "\n## " in tests_block[1:] else tests_block
    assert "On fail:" not in tests_block


def test_invalid_type_value_is_dropped_on_parse(tmp_path):
    env = tmp_path / ENV_DIRNAME
    workflow.write(env)
    p = env / "WORKFLOW.md"
    p.write_text(p.read_text().replace(
        "## 3. Implement", "## 3. Implement\nType: not_a_real_type"), encoding="utf-8")
    parsed = workflow.parse(env)
    step = next(n for n in parsed["nodes"] if n["title"] == "Implement")
    assert step["type"] == ""


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


# --- parallel steps (Phase 3) -------------------------------------------- #

def test_parse_defaults_parallel_to_empty(tmp_path):
    env = tmp_path / ENV_DIRNAME
    workflow.write(env)
    parsed = workflow.parse(env)
    for n in parsed["nodes"]:
        if n["kind"] == "step":
            assert n["parallel"] == ""


def test_parallel_round_trips_through_compose(tmp_path):
    env = tmp_path / ENV_DIRNAME
    workflow.write(env)
    parsed = workflow.parse(env)
    implement = next(n for n in parsed["nodes"] if n["title"] == "Implement")
    verify = next(n for n in parsed["nodes"] if n["title"] == "Verify")
    implement["parallel"] = "wave-1"
    workflow.save_parsed(env, parsed)

    reparsed = workflow.parse(env)
    i2 = next(n for n in reparsed["nodes"] if n["title"] == "Implement")
    assert i2["parallel"] == "wave-1"
    v2 = next(n for n in reparsed["nodes"] if n["title"] == "Verify")
    assert v2["parallel"] == ""


def test_compose_writes_parallel_line(tmp_path):
    env = tmp_path / ENV_DIRNAME
    workflow.write(env)
    parsed = workflow.parse(env)
    step = next(n for n in parsed["nodes"] if n["title"] == "Implement")
    step["parallel"] = "wave-1"
    text = workflow.compose(env, parsed)
    assert "Parallel: wave-1" in text


def test_parallel_omitted_when_empty(tmp_path):
    env = tmp_path / ENV_DIRNAME
    workflow.write(env)
    parsed = workflow.parse(env)
    text = workflow.compose(env, parsed)
    assert "Parallel:" not in text


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


# --- recommended: tier (Phase 4 Task 1) ------------------------------------- #

def test_skills_recommended_tier_parses(tmp_path):
    env = tmp_path / "harn_env"
    env.mkdir()
    (env / "WORKFLOW.md").write_text(
        "## 1. Implement\n"
        "Skills (required: standards; recommended: testing, ui)\n"
        "Tools (required: run_tests; recommended: read_design)\n",
        encoding="utf-8")
    parsed = workflow.parse(env)
    node = parsed["nodes"][0]
    assert node["required"] == ["standards"]
    assert node["skills_recommended"] == ["testing", "ui"]
    assert node["tools"] == ["run_tests"]
    assert node["tools_recommended"] == ["read_design"]


def test_old_bare_tools_line_still_parses_as_all_recommended(tmp_path):
    env = tmp_path / "harn_env"
    env.mkdir()
    (env / "WORKFLOW.md").write_text(
        "## 1. Implement\n"
        "Skills (required: standards)\n"
        "Tools: run_tests, read_design\n",
        encoding="utf-8")
    parsed = workflow.parse(env)
    node = parsed["nodes"][0]
    assert node["tools"] == []
    assert node["tools_recommended"] == ["run_tests", "read_design"]
    assert node["skills_recommended"] == []


def test_old_skills_required_with_no_recommended_still_parses(tmp_path):
    env = tmp_path / "harn_env"
    env.mkdir()
    (env / "WORKFLOW.md").write_text(
        "## 1. Implement\n"
        "Skills (required: standards, constraints)\n",
        encoding="utf-8")
    parsed = workflow.parse(env)
    node = parsed["nodes"][0]
    assert node["required"] == ["standards", "constraints"]
    assert node["skills_recommended"] == []


def test_recommended_tier_round_trips_through_compose(tmp_path):
    env = tmp_path / "harn_env"
    env.mkdir()
    (env / "WORKFLOW.md").write_text(
        "## 1. Implement\n"
        "Skills (required: standards; recommended: testing)\n"
        "Tools (required: run_tests; recommended: read_design)\n",
        encoding="utf-8")
    parsed = workflow.parse(env)
    composed = workflow.compose(env, parsed)
    reparsed_dir = tmp_path / "harn_env2"
    reparsed_dir.mkdir()
    (reparsed_dir / "WORKFLOW.md").write_text(composed, encoding="utf-8")
    reparsed = workflow.parse(reparsed_dir)
    node = reparsed["nodes"][0]
    assert node["required"] == ["standards"]
    assert node["skills_recommended"] == ["testing"]
    assert node["tools"] == ["run_tests"]
    assert node["tools_recommended"] == ["read_design"]

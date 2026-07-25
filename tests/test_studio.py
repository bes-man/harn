"""harn studio: workflow parse/compose round-trip + the editor's data layer."""
from __future__ import annotations

import json
import urllib.request
import urllib.error
from pathlib import Path

from harn import workflow, skills, studio, scaffold, transcript, ENV_DIRNAME
from .conftest import make_task


# --- parse / compose round-trip -------------------------------------------- #

def test_parse_extracts_nodes_and_required(tmp_path):
    env = tmp_path / ENV_DIRNAME
    workflow.write(env)
    parsed = workflow.parse(env)
    assert parsed["preamble"].startswith("# Project workflow")
    titles = [n["title"] for n in parsed["nodes"]]
    assert any("Pre-task" in t for t in titles)
    # a step's required skills are lifted into structured fields
    pre = next(n for n in parsed["nodes"] if "Pre-task" in n["title"])
    assert "standards" in pre["required"] and "constraints" in pre["required"]
    # bare "Tools: ..." line is sugar for all-recommended/none-required
    assert pre["tools"] == []
    assert pre["tools_recommended"]    # Tools line parsed
    assert pre["kind"] == "step" and pre["enabled"] is True
    # the step number is NOT part of the title (it's positional)
    assert not pre["title"][0].isdigit()
    # doc sections are notes, not steps
    note = next(n for n in parsed["nodes"] if n["title"] == "Rules that bite")
    assert note["kind"] == "note"
    # snapshot block is NOT a node
    assert not any("Skills you'll likely use" in t for t in titles)


def test_compose_renumbers_steps_by_order(tmp_path):
    env = tmp_path / ENV_DIRNAME
    workflow.write(env)
    parsed = workflow.parse(env)
    steps = [n for n in parsed["nodes"] if n["kind"] == "step"]
    # reverse the step order, keep notes where they are
    rev = list(reversed(steps))
    it = iter(rev)
    parsed["nodes"] = [next(it) if n["kind"] == "step" else n
                       for n in parsed["nodes"]]
    workflow.save_parsed(env, parsed)
    text = (env / "WORKFLOW.md").read_text(encoding="utf-8")
    # first step heading is now "## 1. <last original step>"
    first_step = rev[0]["title"]
    assert f"## 1. {first_step}" in text
    # re-parse keeps the new order
    re_steps = [n["title"] for n in workflow.parse(env)["nodes"]
                if n["kind"] == "step"]
    assert re_steps == [s["title"] for s in rev]


def test_disabled_round_trips_and_excluded_from_required(tmp_path):
    env = tmp_path / ENV_DIRNAME
    workflow.write(env)
    parsed = workflow.parse(env)
    impl = next(n for n in parsed["nodes"] if n["title"] == "Implement")
    impl["enabled"] = False
    workflow.save_parsed(env, parsed)
    text = (env / "WORKFLOW.md").read_text(encoding="utf-8")
    assert "Disabled: true" in text
    # round-trips
    again = next(n for n in workflow.parse(env)["nodes"] if n["title"] == "Implement")
    assert again["enabled"] is False
    # a disabled step's required skills are NOT collected
    req = workflow.required_skills(env)
    assert not any("Implement" in k for k in req)


def test_add_step_via_compose(tmp_path):
    env = tmp_path / ENV_DIRNAME
    workflow.write(env)
    parsed = workflow.parse(env)
    n_steps = sum(1 for n in parsed["nodes"] if n["kind"] == "step")
    parsed["nodes"].append({"title": "Deploy", "body": "ship it",
                            "required": ["standards"], "tools": ["run_tests"],
                            "kind": "step", "enabled": True})
    workflow.save_parsed(env, parsed)
    re = workflow.parse(env)
    steps = [n for n in re["nodes"] if n["kind"] == "step"]
    assert len(steps) == n_steps + 1
    assert steps[-1]["title"] == "Deploy"
    # the new last step is numbered N
    assert f"## {n_steps+1}. Deploy" in (env / "WORKFLOW.md").read_text(encoding="utf-8")


def test_compose_round_trips(tmp_path):
    env = tmp_path / ENV_DIRNAME
    workflow.write(env)
    parsed = workflow.parse(env)
    workflow.save_parsed(env, parsed)
    reparsed = workflow.parse(env)
    assert [n["title"] for n in reparsed["nodes"]] == \
           [n["title"] for n in parsed["nodes"]]
    assert reparsed["nodes"][3]["required"] == parsed["nodes"][3]["required"]


def test_edit_required_skills_persists(tmp_path):
    env = tmp_path / ENV_DIRNAME
    workflow.write(env)
    parsed = workflow.parse(env)
    # add a required skill to the Implement step
    impl = next(n for n in parsed["nodes"] if "Implement" in n["title"])
    impl["required"].append("security")
    workflow.save_parsed(env, parsed)
    req = workflow.required_skills(env)
    impl_req = next(v for k, v in req.items() if "Implement" in k)
    assert "security" in impl_req


# --- skills body write ----------------------------------------------------- #

def test_write_skill_body_keeps_frontmatter(tmp_path):
    env = tmp_path / ENV_DIRNAME
    skills.append_learning(env, "security", "old fact")
    skills.write_skill_body(env, "security", "# security\n\nNew curated body.")
    body = skills.read_skill(env, "security")
    assert "New curated body" in body
    assert "old fact" not in body          # body replaced
    # frontmatter/description preserved (index still lists it)
    assert "security" in skills.index(env)


# --- studio data layer (pure, no socket) ----------------------------------- #

def test_state_payload_shape(tmp_path):
    scaffold.setup(tmp_path)
    env = tmp_path / ENV_DIRNAME
    st = studio.state_payload(env)
    assert "workflow" in st and "skills" in st
    assert st["workflow"]["nodes"]
    assert all({"name", "description", "body"} <= set(s) for s in st["skills"])


def test_apply_workflow_saves(tmp_path):
    scaffold.setup(tmp_path)
    env = tmp_path / ENV_DIRNAME
    st = studio.state_payload(env)
    st["workflow"]["nodes"][0]["title"] = "0. Custom first step"
    studio.apply_workflow(env, st["workflow"])
    assert "Custom first step" in (env / "WORKFLOW.md").read_text(encoding="utf-8")


def test_apply_skill_saves(tmp_path):
    scaffold.setup(tmp_path)
    env = tmp_path / ENV_DIRNAME
    studio.apply_skill(env, {"name": "testing", "body": "# testing\n\nUse pytest -q."})
    assert "Use pytest -q" in (skills.read_skill(env, "testing") or "")


def test_apply_skill_rejects_empty_name(tmp_path):
    env = tmp_path / ENV_DIRNAME
    env.mkdir()
    out = studio.apply_skill(env, {"name": "", "body": "x"})
    assert out["ok"] is False


def test_add_then_delete_skill(tmp_path):
    scaffold.setup(tmp_path)
    env = tmp_path / ENV_DIRNAME
    studio.apply_skill(env, {"name": "deployment", "body": "# deployment\n\nblue/green."})
    assert "deployment" in skills.index(env)
    out = studio.delete_skill(env, {"name": "deployment"})
    assert out["ok"] is True
    assert "deployment" not in skills.index(env)
    assert not (env / "skills" / "deployment").exists()


def test_delete_skill_rejects_empty_name(tmp_path):
    env = tmp_path / ENV_DIRNAME
    env.mkdir()
    assert studio.delete_skill(env, {"name": ""})["ok"] is False


def test_delete_skill_missing_is_ok(tmp_path):
    scaffold.setup(tmp_path)
    env = tmp_path / ENV_DIRNAME
    assert studio.delete_skill(env, {"name": "nope"})["ok"] is True   # idempotent


def test_config_payload_reports_toggles(tmp_path):
    scaffold.setup(tmp_path)
    env = tmp_path / ENV_DIRNAME
    c = studio.config_payload(env)
    assert "semble" in c["toggles"] and "socraticcode" in c["toggles"]
    assert c["env"].endswith(ENV_DIRNAME)


def test_set_config_flag_round_trips(tmp_path):
    from harn.config import Config
    scaffold.setup(tmp_path)
    env = tmp_path / ENV_DIRNAME
    studio.set_config_flag(env, {"key": "semble", "value": False})
    assert Config.load(env).code_search_semble is False
    studio.set_config_flag(env, {"key": "semble", "value": True})
    assert Config.load(env).code_search_semble is True
    studio.set_config_flag(env, {"key": "socraticcode", "value": False})
    assert Config.load(env).code_search_socraticcode is False


def test_set_config_flag_rejects_unknown(tmp_path):
    env = tmp_path / ENV_DIRNAME
    env.mkdir()
    assert studio.set_config_flag(env, {"key": "evil", "value": True})["ok"] is False


def test_progress_payload_maps_stages(tmp_path):
    from harn import events
    scaffold.setup(tmp_path)
    env = tmp_path / ENV_DIRNAME
    events.new_run(env, kind="loop")
    events.emit(env, "stage_start", task_id="T", stage="execute")
    events.emit(env, "stage_end", task_id="T", stage="execute",
                dur_ms=8000, tok_in=1000, tok_out=200, cost_usd=0.02)
    events.emit(env, "stage_start", task_id="T", stage="verify")  # active, no end
    p = studio.progress_payload(env)
    assert p["stages"]["execute"]["status"] == "done"
    assert p["stages"]["verify"]["status"] == "active"
    assert p["active"] == "verify"
    assert p["totals"]["tok_in"] == 1000 and p["totals"]["cost_usd"] == 0.02


def test_progress_complete_when_run_ends_ok(tmp_path):
    from harn import events
    scaffold.setup(tmp_path)
    env = tmp_path / ENV_DIRNAME
    events.new_run(env, kind="loop")
    events.emit(env, "stage_start", task_id="T", stage="execute")
    events.emit(env, "stage_end", task_id="T", stage="execute", dur_ms=100)
    events.emit(env, "run_end", phase="DONE")
    p = studio.progress_payload(env)
    assert p["stages"]["execute"]["status"] == "complete"
    assert p["ended"] is True


def test_progress_for_task_is_not_stolen_by_unrelated_chat_run(tmp_path):
    """A MCP/chat session may start while a workflow step is running.  The
    flow sidebar must keep following its selected task, not the last global
    run_start event in the shared telemetry file."""
    from harn import events
    scaffold.setup(tmp_path)
    env = tmp_path / ENV_DIRNAME
    events.new_run(env, kind="loop")
    events.emit(env, "stage_start", task_id="PRJ-044", stage="step-weather")
    events.new_run(env, kind="chat")

    p = studio.progress_payload(env, task_id="PRJ-044")

    assert p["active"] == "step-weather"
    assert p["stages"]["step-weather"]["status"] == "active"


def test_node_stage_keyword_mapping():
    assert studio._node_stage("Implement") == "execute"
    assert studio._node_stage("Tests") == "test"
    assert studio._node_stage("UI verify (only user-facing work)") == "ui_verify"
    assert studio._node_stage("Verify") == "verify"
    assert studio._node_stage("Reconcile — grow the knowledge base") == "reconcile"
    assert studio._node_stage("Rules that bite") is None


def test_server_rejects_bad_env(tmp_path):
    scaffold.setup(tmp_path)
    env = tmp_path / ENV_DIRNAME
    from http.server import ThreadingHTTPServer
    import threading
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), studio._make_handler(env))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{httpd.server_port}"
        # default env (no ?env) works
        assert json.loads(urllib.request.urlopen(base + "/api/state").read())["workflow"]
        # explicit ?env to another valid project works
        other = tmp_path / "other"; scaffold.setup(other)
        oenv = other / ENV_DIRNAME
        import urllib.parse as up
        u = base + "/api/config?env=" + up.quote(str(oenv))
        assert json.loads(urllib.request.urlopen(u).read())["env"].endswith(ENV_DIRNAME)
        # a non-env path is rejected
        bad = base + "/api/state?env=" + up.quote(str(tmp_path / "nope"))
        try:
            urllib.request.urlopen(bad); assert False, "should 400"
        except urllib.error.HTTPError as e:
            assert e.code == 400
    finally:
        httpd.shutdown(); httpd.server_close()


def test_server_skill_delete_endpoint(tmp_path):
    scaffold.setup(tmp_path)
    env = tmp_path / ENV_DIRNAME
    from http.server import ThreadingHTTPServer
    import threading
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), studio._make_handler(env))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{httpd.server_port}"
        # security skill ships in the template
        assert (env / "skills" / "security").exists()
        payload = json.dumps({"name": "security"}).encode()
        req = urllib.request.Request(base + "/api/skill/delete", data=payload,
                                     headers={"Content-Type": "application/json"})
        assert json.loads(urllib.request.urlopen(req).read())["ok"] is True
        assert not (env / "skills" / "security").exists()
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_server_skill_export_endpoint(tmp_path):
    scaffold.setup(tmp_path)
    env = tmp_path / ENV_DIRNAME
    from http.server import ThreadingHTTPServer
    import threading
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), studio._make_handler(env))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{httpd.server_port}"
        # security skill ships in the template
        skill_path = env / "skills" / "security" / "SKILL.md"
        raw_bytes = skill_path.read_bytes()
        resp = urllib.request.urlopen(base + "/api/skill/export?name=security")
        assert resp.read() == raw_bytes
        assert resp.headers.get("Content-Disposition") == 'attachment; filename="security.md"'
        try:
            urllib.request.urlopen(base + "/api/skill/export?name=does-not-exist")
            assert False, "expected 404"
        except urllib.error.HTTPError as e:
            assert e.code == 404
    finally:
        httpd.shutdown()
        httpd.server_close()


# --- canvas layout (drag/drop positions) ----------------------------------- #

def test_layout_round_trip(tmp_path):
    env = tmp_path / ENV_DIRNAME
    (env / "state").mkdir(parents=True)
    assert studio.layout_payload(env) == {}          # none yet → auto-layout
    studio.apply_layout(env, {"Implement": {"x": 300, "y": 120},
                              "Tests": {"x": 620, "y": 240}})
    got = studio.layout_payload(env)
    assert got["Implement"] == {"x": 300, "y": 120}
    assert got["Tests"]["x"] == 620


def test_apply_layout_sanitises_bad_entries(tmp_path):
    env = tmp_path / ENV_DIRNAME
    (env / "state").mkdir(parents=True)
    studio.apply_layout(env, {"ok": {"x": 10, "y": 20},
                              "bad": {"x": "nope"},
                              "missing": {"x": 1},
                              "notdict": 5})
    got = studio.layout_payload(env)
    assert got == {"ok": {"x": 10, "y": 20}}         # only the valid entry kept


def test_layout_kept_out_of_workflow_md(tmp_path):
    scaffold.setup(tmp_path)
    env = tmp_path / ENV_DIRNAME
    studio.apply_layout(env, {"Implement": {"x": 9, "y": 9}})
    # positions live in state/, never in the agent-facing WORKFLOW.md
    assert (env / "state" / "studio_layout.json").exists()
    assert "studio_layout" not in (env / "WORKFLOW.md").read_text(encoding="utf-8")
    assert '"x": 9' not in (env / "WORKFLOW.md").read_text(encoding="utf-8")


def test_state_payload_includes_layout(tmp_path):
    scaffold.setup(tmp_path)
    env = tmp_path / ENV_DIRNAME
    studio.apply_layout(env, {"Tests": {"x": 5, "y": 6}})
    st = studio.state_payload(env)
    assert st["layout"]["Tests"] == {"x": 5, "y": 6}


def test_server_layout_endpoint(tmp_path):
    scaffold.setup(tmp_path)
    env = tmp_path / ENV_DIRNAME
    from http.server import ThreadingHTTPServer
    import threading
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), studio._make_handler(env))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{httpd.server_port}"
        payload = json.dumps({"Implement": {"x": 42, "y": 7}}).encode()
        req = urllib.request.Request(base + "/api/layout", data=payload,
                                     headers={"Content-Type": "application/json"})
        assert json.loads(urllib.request.urlopen(req).read())["ok"] is True
        state = json.loads(urllib.request.urlopen(base + "/api/state").read())
        assert state["layout"]["Implement"] == {"x": 42, "y": 7}
    finally:
        httpd.shutdown()
        httpd.server_close()


# --- live server smoke (binds an ephemeral port) --------------------------- #

def test_server_serves_html_and_api(tmp_path):
    scaffold.setup(tmp_path)
    env = tmp_path / ENV_DIRNAME
    from http.server import ThreadingHTTPServer
    import threading
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), studio._make_handler(env))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{httpd.server_port}"
        html = urllib.request.urlopen(base + "/").read().decode()
        assert "harn studio" in html
        state = json.loads(urllib.request.urlopen(base + "/api/state").read())
        assert state["workflow"]["nodes"]
        # POST a workflow edit
        payload = json.dumps(state["workflow"]).encode()
        req = urllib.request.Request(base + "/api/workflow", data=payload,
                                     headers={"Content-Type": "application/json"})
        res = json.loads(urllib.request.urlopen(req).read())
        assert res["ok"] is True
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_studio_polling_defers_panel_renders_while_interacting():
    """Polling must preserve editors, pointer drags, and browser text selections."""
    html = studio._HTML
    assert "function panelIsEditing(el)" in html
    assert "function panelHasTextSelection(el)" in html
    assert "window.getSelection" in html
    assert "!selection.isCollapsed" in html
    assert "function panelIsInteracting(el)" in html
    assert "ACTIVE_TEXT_DRAG_PANEL===el" in html
    assert "pollingCanReplace($('#listView'))" in html
    assert "pollingCanReplace($('#insp'))" in html


def test_studio_board_poll_renders_only_changed_data():
    html = studio._HTML
    assert "let BOARD_LIST_RENDER_KEY=null" in html
    assert "let BOARD_DETAIL_RENDER_KEY=null" in html
    assert "const listChanged=boardListRenderKey()!==BOARD_LIST_RENDER_KEY" in html
    assert "const detailChanged=boardDetailRenderKey()!==BOARD_DETAIL_RENDER_KEY" in html


def test_studio_activity_entries_show_second_precision_launch_time():
    """Reported gap: diagnosing a run that took over 2 minutes for a trivial
    task, every Agent activity entry looked the same age -- no way to see
    WHEN each tool call/message actually fired, only their relative order.
    Each entry's own `ts` (already second-precision from transcript.append)
    must render a local timestamp next to it — DATE included, not just
    HH:MM:SS: a step's attempts can span days (blocked overnight, resumed
    later), and clock-only stamps made entries from different days
    indistinguishable."""
    html = studio._HTML
    assert "function fmtEntryStamp(ts)" in html
    assert "fmtEntryStamp(e.ts)" in html


def test_studio_transcript_drops_streaming_duplicate_of_its_own_completed_entry():
    """Observed live: a streaming 'updated' chunk followed by a 'completed'
    entry carrying the EXACT SAME text rendered as two back-to-back blocks
    with identical content — read as a chronology/duplication bug. Only the
    completed entry (the real end-of-message timestamp) should render.

    The pairing match must tolerate two real-world mismatches between an
    "updated"/streaming record and its own terminal "completed"/"failed"
    twin: title CASING (observed live: "Claude" vs "claude", adapter.name
    lowercase) and KIND (loop.py's on_event tags the same turn's own output
    "message" on success but "error" on failure — an exact-kind compare left
    a failed turn's streaming chunk permanently undeduped against its own
    error/failed entry)."""
    html = studio._HTML
    assert "dup:!!(supersededBy&&supersededBy.text===entry.text)" in html
    assert ".filter(x=>!x.dup)" in html
    assert "const sameTitle=(a,b)=>String(a||'').toLowerCase()===String(b||'').toLowerCase();" in html
    assert "sameTitle(later.title,entry.title)" in html
    assert "const sameKind=(a,b)=>a===b||(['message','error'].includes(a)&&['message','error'].includes(b));" in html
    assert "sameKind(later.kind,entry.kind)" in html


def test_studio_run_result_shows_finished_date_and_stops_idle_animation():
    html = studio._HTML
    assert "function fmtDateTime(ts)" in html
    assert "fmtDateTime(lastRun.finished_at)" in html
    assert "const live=hasRun()&&!PROG.ended;" in html
    assert "const live=!!BOARD.run&&!settled" in html
    assert ".badge-unused-required{border-color:#e74c3c !important}" in html
    assert ".badge-unused-required{border-color:#e74c3c !important;animation:" not in html


def test_studio_run_result_distinguishes_blocked_from_failed_with_resume_button():
    """A run that stopped because the agent is waiting on a human answer
    (last_run.blocked) must read differently from an actual failure, and
    offer a Resume button right in the RUN WORKFLOW sidebar — not only
    inside the separate Run History panel."""
    html = studio._HTML
    assert "function lastRunNoticeHtml(filterTaskId)" in html
    assert "lastRun.blocked?'⏳ Waiting for your answer'" in html
    assert "lastRun.blocked?'blocked':lastRun.reason?'failed':''" in html
    assert "onclick=\"launchTask('${esc(lastRun.task_id)}',false)\">▶ Resume</button>" in html
    assert ".run-result.blocked{" in html
    assert ".run-result.failed{" in html
    # The same notice also renders in the Run progress SIDE PANEL
    # (renderRunHistory), scoped to that panel's own task — not just the
    # RUN WORKFLOW terminal node on the canvas.
    assert "lastRunNoticeHtml(taskId)+" in html
    assert "lastRun:BOARD.last_run||null," in html


def test_studio_step_editor_has_tool_mode_selector_and_cycle_tool():
    """Studio's per-step Tool mode control (auto/scoped) and the 3-state
    required/recommended/off tool cycle, backing mcp_server's per-step tool
    registration scoping."""
    html = studio._HTML
    assert "function cycleTool(name)" in html
    assert "setStepField('tool_mode'" in html
    assert 'value="scoped"' in html
    assert 'value="auto"' in html


def test_studio_step_editor_has_new_session_and_use_task_context_toggles():
    """Studio's per-step new_session/use_task_context toggles (spec C) — same
    off-by-default pattern as tool_mode: new_session is shown on every step,
    use_task_context only shown/active once new_session is on for that step."""
    html = studio._HTML
    assert "function setNewSession(on)" in html
    assert "function setUseTaskContext(on)" in html
    assert "onchange=\"setNewSession(this.checked)\"" in html
    assert "onchange=\"setUseTaskContext(this.checked)\"" in html
    assert "New session" in html
    assert "Use task context" in html
    # use_task_context checkbox markup only appears inside the newSessionOn
    # branch — i.e. it's conditionally rendered, not always shown.
    assert "newSessionOn?`" in html


def test_studio_task_detail_preserves_scroll_across_required_render():
    html = studio._HTML
    assert 'id="reviewLog"' in html
    # Comments scrolls with the rest of the task modal now, not on its own —
    # so only the panel-level scroll position needs preserving across a
    # required re-render.
    assert "panel.scrollTop=panelScroll" in html
    assert "reviewScroll" not in html


def test_studio_polling_discards_stale_responses():
    """A slower prior poll must not overwrite the result of a newer poll."""
    html = studio._HTML
    assert "let boardPollGeneration=0" in html
    assert "if(generation!==boardPollGeneration) return;" in html
    assert "let progressPollGeneration=0" in html
    assert "if(generation!==progressPollGeneration) return;" in html


def test_studio_settings_expose_autonomy_telegram_and_grace():
    html = studio._HTML
    assert 'id="setAutonomy"' in html
    assert "numField('setChatGrace'" in html
    assert 'id="setTelegramApiKey"' in html
    assert 'id="setTelegramUserId"' in html


def test_studio_blocked_question_supports_sidebar_options():
    html = studio._HTML
    assert "function questionOptions(question)" in html
    assert "submitAnswer(option.value)" in html
    assert "Recommended" in html


def test_flow_progress_poll_remains_live_while_an_editor_is_focused():
    html = studio._HTML
    progress = html[html.index("async function pollProgress()"):
                    html.index("/* ---------- board tab")]
    assert "PROG=next;" in progress
    assert "applyProgress();" in progress
    assert progress.index("applyProgress();") < progress.index("pollingCanReplace(term)")
    assert "if(term && pollingCanReplace(term)" in progress


def test_header_keeps_primary_actions_inside_viewport():
    html = studio._HTML
    assert '<div class="header-actions">' in html
    assert ".header-actions{display:flex" in html
    assert "@media (max-width:1500px)" in html
    assert ".proj,.toggles{display:none}" in html
    # The workflow description used to live inline in the header (pushing the
    # tab bar around on long descriptions, e.g. a verbose preset like
    # spec-writer's) — it's gone from the header entirely now, not just
    # hidden at narrow widths.
    assert 'id="wfDesc"' not in html
    assert ".wfdesc{" not in html


def test_flow_sidebar_renders_visual_step_timeline_and_usage_states():
    html = studio._HTML
    assert 'class="run-progress"' in html
    assert 'class="progress-rail"' in html
    assert 'class="run-step ${status}"' in html
    assert "usagePill('skill'" in html
    assert "usagePill('tool'" in html
    assert "usage-used" in html
    assert "usage-unused-recommended" in html
    assert "usage-unused-required" in html
    assert "replaceAll('_','-')" in html


def test_launching_flow_opens_visual_progress_sidebar():
    html = studio._HTML
    run = html[html.index("async function launchCurrentFlow()"):
               html.index("async function runWholeWorkflow()")]
    assert "RUN_HISTORY_OPEN=true" in run
    assert "RUN_HISTORY_MODE='execution'" in run
    assert run.index("RUN_HISTORY_OPEN=true") < run.index("post_('/api/tasks/launch_workflow'")


def test_launching_flow_warns_about_context_loss_only_for_a_restart():
    html = studio._HTML
    run = html[html.index("async function launchCurrentFlow()"):
               html.index("async function runWholeWorkflow()")]
    assert "taskHasExecutionHistory(task)" in run
    assert "Git will restore project files to the task baseline" in run
    assert "All saved execution context" in run
    assert "prior errors, attempts, transcripts, scratchpad, and decisions" in run
    assert run.index("taskHasExecutionHistory(task)") < run.index("post_('/api/tasks/launch_workflow'")


def test_restart_clears_optimistic_sidebar_artifacts_before_render():
    html = studio._HTML
    run = html[html.index("async function launchCurrentFlow()"):
               html.index("async function runWholeWorkflow()")]
    assert "const restarting=taskHasExecutionHistory(task)" in run
    assert "task.step_results={}" in run
    assert "task.review_log=[]" in run
    assert "task.context_reads=[]" in run
    assert "resetRunClientState(taskId)" in run
    assert run.index("task.step_results={}") < run.index("renderRunHistory();")


def test_launch_clears_transcript_again_after_server_confirms_clean_restart():
    html = studio._HTML
    run = html[html.index("async function launchCurrentFlow()"):
               html.index("async function runWholeWorkflow()")]
    post = run.index("post_('/api/tasks/launch_workflow'")
    success = run.index("if(!r.ok)")
    assert "resetRunClientState(taskId)" in run[success:]
    assert run.index("resetRunClientState(taskId)", success) > post


def test_blocked_sidebar_restart_uses_full_workflow_reset_not_attempt_only_retry():
    html = studio._HTML
    render = html[html.index("const renderRunStep=(n)=>"):
                  html.index("const rows=executionPlanGroups")]
    assert "Restart flow from scratch" in render
    assert "rerunSidebarWorkflow()" in render
    assert "retryBlockedStep" not in render


def test_sidebar_restart_warns_that_old_attempt_log_and_context_are_deleted():
    html = studio._HTML
    fn = html[html.index("async function rerunSidebarWorkflow()"):
              html.index("async function rerunWholeWorkflow()")]
    assert "Old attempts, errors, transcript, and saved execution context will be deleted" in fn


def test_all_clean_restart_actions_reset_client_run_context():
    html = studio._HTML
    assert "function resetRunClientState(taskId)" in html
    assert "RUN_TRANSCRIPT={taskId,cursor:0,entries:[]}" in html
    assert "PROG={stages:{},totals:{},active:null,ended:false}" in html
    for start, end in [
        ("async function launchCurrentFlow()", "async function runWholeWorkflow()"),
        ("async function rerunSidebarWorkflow()", "async function rerunWholeWorkflow()"),
        ("async function rerunWholeWorkflow()", "/* ---------- per-step Run/Rerun"),
    ]:
        assert "resetRunClientState(taskId)" in html[html.index(start):html.index(end)]


def test_resume_remains_non_destructive():
    html = studio._HTML
    resume = html[html.index("async function resumeFrozenFlow()"):
                  html.index("async function rerunSidebarWorkflow()")]
    assert "/api/tasks/launch" in resume
    assert "/api/tasks/launch_workflow" not in resume
    assert "/api/tasks/rerun_workflow" not in resume
    assert "resetRunClientState" not in resume


def test_run_history_renders_before_waiting_for_task_plan():
    html = studio._HTML
    fn = html[html.index("async function openRunHistory()"):
              html.index("function renderRunHistory()")]
    assert fn.index("renderRunHistory();") < fn.index("await (await fetch(api('/api/task_plan?")
    assert "RUN_HISTORY_PLAN={taskId, nodes:null};" in fn
    assert "if(RUN_HISTORY_MODE==='preview')" in fn
    assert "nodes:S.workflow.nodes" in fn
    assert "retryBlockedStep" in html


def test_finished_task_gets_execution_mode_not_preview():
    # A task that already ran to completion (no active BOARD.run) must still
    # show its real transcript, not the "Starts when this flow runs" preview
    # placeholder — so the execution-mode check must also look at the
    # selected task's own historical fields, not only the live BOARD.run.
    html = studio._HTML
    fn = html[html.index("async function openRunHistory()"):
              html.index("function renderRunHistory()")]
    assert "taskHasRunHistory" in fn
    assert "historyTask.step_results" in fn
    assert "historyTask.baseline_ref" in fn
    assert "historyTask.task_patch_refs" in fn
    assert "(BOARD.run&&BOARD.run.task_id===taskId)||taskHasRunHistory" in fn
    assert fn.index("taskHasRunHistory") < fn.index("if(RUN_HISTORY_MODE==='preview')")


def test_clicking_run_workflow_heading_opens_progress_sidebar():
    html = studio._HTML
    assert 'class="ttl run-launch" onclick="openRunHistory()"' in html


def test_transcript_payload_filters_and_pages(tmp_path):
    env = tmp_path / ENV_DIRNAME
    scaffold.setup(tmp_path)
    make_task(env, "PRJ-1")
    make_task(env, "PRJ-2")
    first = transcript.append(
        env, task_id="PRJ-1", step_id="step-a", run_id="r-1", attempt=1,
        kind="status", phase="started", title="Start", text="one")
    transcript.append(
        env, task_id="PRJ-1", step_id="step-b", run_id="r-1", attempt=1,
        kind="message", phase="completed", title="Agent", text="two")
    transcript.append(
        env, task_id="PRJ-2", step_id="step-a", run_id="r-2", attempt=1,
        kind="message", phase="completed", title="Agent", text="private")

    page = studio.transcript_payload(env, "PRJ-1", after=str(first["seq"]))
    assert page["ok"] is True
    assert [e["text"] for e in page["entries"]] == ["two"]
    assert studio.transcript_payload(env, "missing")["ok"] is False
    assert studio.transcript_payload(env, "PRJ-1", step_id="step-a")["entries"][0]["text"] == "one"


def test_run_sidebar_has_live_per_step_transcript():
    html = studio._HTML
    assert "async function pollRunTranscript()" in html
    assert "/api/tasks/transcript?task=" in html
    assert 'data-step-transcript="${esc(n.id)}"' in html
    assert "Waiting for agent output…" in html
    assert "function transcriptEntryHtml" in html
    assert "RUN_TRANSCRIPT.cursor" in html
    assert "rememberTranscriptOpen" in html


def test_run_sidebar_polling_uses_render_key_and_interaction_guard():
    html = studio._HTML
    assert "let RUN_HISTORY_RENDER_KEY=null" in html
    assert "function runHistoryRenderKey()" in html
    assert "function renderRunHistoryIfChanged()" in html
    assert "if(!pollingCanReplace($('#insp')))return" in html
    assert "runHistoryRenderKey()===RUN_HISTORY_RENDER_KEY" in html
    board = html[html.index("async function pollBoard()"):html.index("let BLOCKED_Q_TASK")]
    assert "renderRunHistoryIfChanged()" in board
    terminal = html[html.index("function renderFlowTerminal(el)"):
                    html.index("async function launchCurrentFlow()")]
    assert "renderRunHistory();" not in terminal
    assert terminal.count("renderRunHistoryIfChanged()") >= 3


def test_polling_dom_replacement_stops_for_any_document_selection():
    html = studio._HTML
    assert "function documentHasTextSelection()" in html
    assert "function pollingCanReplace(el)" in html
    assert "!documentHasTextSelection()" in html
    progress = html[html.index("async function pollProgress()"):
                    html.index("/* ---------- board tab")]
    assert "pollingCanReplace(term)" in progress
    board = html[html.index("async function pollBoard()"):
                 html.index("let BLOCKED_Q_TASK")]
    assert "pollingCanReplace($('#listView'))" in board
    assert "pollingCanReplace($('#taskDetailPanel'))" in board


def test_run_sidebar_preserves_nested_transcript_scroll():
    html = studio._HTML
    assert 'data-run-scroll="${esc(e.seq)}"' in html
    assert "const nestedScroll=new Map()" in html
    assert "panel.querySelectorAll('[data-run-scroll]')" in html
    assert "el.scrollTop=nestedScroll.get(el.dataset.runScroll)" in html


def test_flow_history_prefers_completed_task_with_step_results_after_reload():
    html = studio._HTML
    fn = html[html.index("function flowSelectedTaskId()"):
              html.index("function flowSelectedTask()")]
    assert "withResults" in fn
    assert "Object.keys(t.step_results||{}).length" in fn


def test_polling_tracks_native_form_interactions_beyond_active_element():
    html = studio._HTML
    assert "let ACTIVE_FORM_CONTROL=null" in html
    assert "function formControlFromEvent" in html
    assert "document.addEventListener('pointerdown',trackFormInteraction,true)" in html
    assert "document.addEventListener('focusin',trackFormInteraction,true)" in html
    assert "document.addEventListener('change',releaseFormInteraction,true)" in html
    assert "ACTIVE_FORM_CONTROL&&el.contains(ACTIVE_FORM_CONTROL)" in html


def test_every_poll_driven_form_render_has_editing_guard():
    html = studio._HTML
    progress = html[html.index("async function pollProgress()"):
                    html.index("/* ---------- board tab")]
    assert "pollingCanReplace(term)" in progress
    board = html[html.index("async function pollBoard()"):
                 html.index("let BLOCKED_Q_TASK")]
    assert "if(RUN_HISTORY_OPEN && tab==='flow')" in board
    assert "pollingCanReplace($('#listView'))" in board
    assert "pollingCanReplace($('#taskDetailPanel'))" in board
    blocked = html[html.index("async function pollBlockedQuestion()"):
                   html.index("async function submitAnswer")]
    assert "if(!pollingCanReplace(el)) return" in blocked


def test_run_sidebar_previews_canvas_and_uses_shared_launch_action():
    html = studio._HTML
    assert "let RUN_HISTORY_MODE='preview'" in html
    assert "RUN_HISTORY_MODE==='preview' ? S.workflow.nodes" in html
    assert "async function launchCurrentFlow()" in html
    assert "/api/tasks/launch_workflow" in html
    assert html.count('onclick="launchCurrentFlow()"') >= 2


def test_execution_plan_groups_parallel_wave_members():
    html = studio._HTML
    assert "function executionPlanGroups(steps)" in html
    assert 'class="run-wave"' in html
    assert "∥ parallel · ${esc(group.id)}" in html
    assert "group.steps.map(renderRunStep)" in html


def test_sidebar_runtime_status_uses_same_progress_ids_as_canvas():
    html = studio._HTML
    assert "function runtimeStepStatus(stepId,ledgerStatus)" in html
    assert "(PROG.stages||{})[stepId]" in html

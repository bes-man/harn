"""harn studio: workflow parse/compose round-trip + the editor's data layer."""
from __future__ import annotations

import json
import urllib.request
import urllib.error
from pathlib import Path

from harn import workflow, skills, studio, scaffold, ENV_DIRNAME


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
    assert pre["tools"]            # Tools line parsed
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

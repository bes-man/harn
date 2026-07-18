"""Agent builder + generation spec (docs/superpowers/specs/2026-07-19-agent-builder-and-generation-design.md)."""
from __future__ import annotations
from harn import roles, ENV_DIRNAME


def _env(tmp_path):
    env = tmp_path / ENV_DIRNAME
    (env / "agents").mkdir(parents=True)
    return env


def test_save_then_discover_round_trips(tmp_path):
    env = _env(tmp_path)
    p = roles.save(env, {
        "name": "analyst", "command": "an", "status": "analyzing",
        "next_status": "analyzed", "trigger": "manual", "oracle": False,
        "isolation": "worktree", "secrets": ["SSH_HOST"], "agent": "claude",
        "model": "sonnet", "workflow": "analyst-flow",
        "body": "## Role\nYou are the analyst.",
    })
    assert p.name == "analyst.md"
    r = roles.find(env, "analyst")
    assert r.name == "analyst" and r.command == "an" and r.status == "analyzing"
    assert r.next_status == "analyzed" and r.oracle is False
    assert r.isolation == "worktree" and r.secrets == ["SSH_HOST"]
    assert r.agent == "claude" and r.model == "sonnet" and r.workflow == "analyst-flow"
    assert "You are the analyst." in r.body()


def test_save_creates_agents_dir_if_missing(tmp_path):
    env = tmp_path / ENV_DIRNAME
    env.mkdir()
    roles.save(env, {"name": "dev", "status": "todo", "body": "x"})
    assert (env / "agents" / "dev.md").exists()


def test_delete_removes_role(tmp_path):
    env = _env(tmp_path)
    roles.save(env, {"name": "dev", "status": "todo", "body": "x"})
    assert roles.delete(env, "dev") is True
    assert roles.find(env, "dev") is None
    assert roles.delete(env, "dev") is False   # already gone


def test_generate_validates_against_catalogs(tmp_path, monkeypatch):
    from harn import agentgen, skills, tools
    from harn.config import Config
    env = tmp_path / ENV_DIRNAME
    (env / "agents").mkdir(parents=True)
    (env / "skills" / "standards").mkdir(parents=True)
    (env / "skills" / "standards" / "SKILL.md").write_text(
        "---\nname: standards\ndescription: d\n---\nbody", encoding="utf-8")

    import json as _json
    reply = _json.dumps({
        "role": {"name": "triager", "status": "todo", "next_status": "done",
                 "oracle": True, "body": "You triage."},
        "workflow": {"nodes": [
            {"kind": "step", "title": "Read", "required": ["standards", "ghost_skill"],
             "tools": ["nonexistent_tool"]}]},
    })

    class FakeAdapter:
        name = "fake"
        def available(self): return True
        def run_turn(self, prompt, cwd, timeout=1800, *, model=None, effort=None, temperature=None):
            from harn.adapters.base import AgentResult
            return AgentResult(ok=True, text=reply)

    monkeypatch.setattr(agentgen.loop_mod, "get_adapter", lambda n: FakeAdapter())
    out = agentgen.generate(env, Config(), "an agent that triages bug reports")
    assert out["role"]["name"] == "triager"
    # ghost_skill / nonexistent_tool are not in the catalog → dropped
    step = out["workflow"]["nodes"][0]
    assert step["required"] == ["standards"]
    assert step["tools"] == []
    assert "ghost_skill" in out["dropped"] and "nonexistent_tool" in out["dropped"]


def test_generate_unknown_status_falls_back_to_first(tmp_path, monkeypatch):
    from harn import agentgen
    from harn.config import Config
    env = tmp_path / ENV_DIRNAME
    (env / "agents").mkdir(parents=True)
    import json as _json
    reply = _json.dumps({"role": {"name": "x", "status": "bogus", "body": "b"},
                         "workflow": {"nodes": []}})

    class FakeAdapter:
        name = "fake"
        def available(self): return True
        def run_turn(self, *a, **k):
            from harn.adapters.base import AgentResult
            return AgentResult(ok=True, text=reply)
    monkeypatch.setattr(agentgen.loop_mod, "get_adapter", lambda n: FakeAdapter())
    out = agentgen.generate(env, Config(), "desc")
    assert out["role"]["status"] == "todo"   # first of default lifecycle


def test_generate_malformed_reply_never_raises(tmp_path, monkeypatch):
    from harn import agentgen
    from harn.config import Config
    env = tmp_path / ENV_DIRNAME
    (env / "agents").mkdir(parents=True)

    class FakeAdapter:
        name = "fake"
        def available(self): return True
        def run_turn(self, *a, **k):
            from harn.adapters.base import AgentResult
            return AgentResult(ok=True, text="not json at all")
    monkeypatch.setattr(agentgen.loop_mod, "get_adapter", lambda n: FakeAdapter())
    out = agentgen.generate(env, Config(), "desc")
    assert isinstance(out, dict) and "role" in out and "workflow" in out


def test_generate_non_dict_role_never_raises(tmp_path, monkeypatch):
    from harn import agentgen
    from harn.config import Config
    env = tmp_path / ENV_DIRNAME
    (env / "agents").mkdir(parents=True)
    import json as _json
    reply = _json.dumps({"role": "triager", "workflow": {"nodes": []}})

    class FakeAdapter:
        name = "fake"
        def available(self): return True
        def run_turn(self, *a, **k):
            from harn.adapters.base import AgentResult
            return AgentResult(ok=True, text=reply)
    monkeypatch.setattr(agentgen.loop_mod, "get_adapter", lambda n: FakeAdapter())
    out = agentgen.generate(env, Config(), "desc")
    assert isinstance(out, dict) and "role" in out
    assert out["role"]["name"] == "agent"   # fell back to default since role was a string


def test_generate_string_required_field_is_dropped_cleanly(tmp_path, monkeypatch):
    from harn import agentgen
    from harn.config import Config
    env = tmp_path / ENV_DIRNAME
    (env / "agents").mkdir(parents=True)
    (env / "skills" / "standards").mkdir(parents=True)
    (env / "skills" / "standards" / "SKILL.md").write_text(
        "---\nname: standards\ndescription: d\n---\nbody", encoding="utf-8")

    import json as _json
    reply = _json.dumps({
        "role": {"name": "x", "status": "todo", "body": "b"},
        "workflow": {"nodes": [
            {"kind": "step", "title": "Read", "required": "standards", "tools": 5}]},
    })

    class FakeAdapter:
        name = "fake"
        def available(self): return True
        def run_turn(self, *a, **k):
            from harn.adapters.base import AgentResult
            return AgentResult(ok=True, text=reply)
    monkeypatch.setattr(agentgen.loop_mod, "get_adapter", lambda n: FakeAdapter())
    out = agentgen.generate(env, Config(), "desc")
    step = out["workflow"]["nodes"][0]
    assert step["required"] == []
    assert step["tools"] == []
    for ch in ("s", "t", "a", "n", "d"):
        assert ch not in out["dropped"]


def test_generate_non_list_nodes_never_raises(tmp_path, monkeypatch):
    from harn import agentgen
    from harn.config import Config
    env = tmp_path / ENV_DIRNAME
    (env / "agents").mkdir(parents=True)
    import json as _json
    reply = _json.dumps({
        "role": {"name": "x", "status": "todo"},
        "workflow": {"nodes": 5},
    })

    class FakeAdapter:
        name = "fake"
        def available(self): return True
        def run_turn(self, *a, **k):
            from harn.adapters.base import AgentResult
            return AgentResult(ok=True, text=reply)
    monkeypatch.setattr(agentgen.loop_mod, "get_adapter", lambda n: FakeAdapter())
    out = agentgen.generate(env, Config(), "desc")
    assert isinstance(out, dict)
    assert out["workflow"]["nodes"] == []


def test_generate_node_missing_title_gets_default(tmp_path, monkeypatch):
    from harn import agentgen
    from harn.config import Config
    env = tmp_path / ENV_DIRNAME
    (env / "agents").mkdir(parents=True)
    import json as _json
    reply = _json.dumps({
        "role": {"name": "x", "status": "todo", "body": "b"},
        "workflow": {"nodes": [
            {"kind": "step", "body": "do x", "required": [], "tools": []}]},
    })

    class FakeAdapter:
        name = "fake"
        def available(self): return True
        def run_turn(self, *a, **k):
            from harn.adapters.base import AgentResult
            return AgentResult(ok=True, text=reply)
    monkeypatch.setattr(agentgen.loop_mod, "get_adapter", lambda n: FakeAdapter())
    out = agentgen.generate(env, Config(), "desc")
    step = out["workflow"]["nodes"][0]
    assert isinstance(step["title"], str) and step["title"].strip() != ""
    assert isinstance(step["body"], str)


def test_generate_node_nonstring_title_coerced(tmp_path, monkeypatch):
    from harn import agentgen
    from harn.config import Config
    env = tmp_path / ENV_DIRNAME
    (env / "agents").mkdir(parents=True)
    import json as _json
    reply = _json.dumps({
        "role": {"name": "x", "status": "todo", "body": "b"},
        "workflow": {"nodes": [
            {"kind": "step", "title": 123, "body": "do y", "required": [], "tools": []}]},
    })

    class FakeAdapter:
        name = "fake"
        def available(self): return True
        def run_turn(self, *a, **k):
            from harn.adapters.base import AgentResult
            return AgentResult(ok=True, text=reply)
    monkeypatch.setattr(agentgen.loop_mod, "get_adapter", lambda n: FakeAdapter())
    out = agentgen.generate(env, Config(), "desc")
    step = out["workflow"]["nodes"][0]
    assert isinstance(step["title"], str)
    assert step["title"] == "123"


def test_agents_payload_lists_and_save_delete_roundtrip(tmp_path):
    from harn import studio
    env = tmp_path / ENV_DIRNAME
    (env / "agents").mkdir(parents=True)
    (env / "tasks").mkdir(parents=True)
    r = studio.save_agent_payload(env, {"name": "dev", "status": "todo", "body": "b"})
    assert r["ok"] is True
    payload = studio.agents_payload(env)
    assert any(a["name"] == "dev" for a in payload["agents"])
    assert "todo" in [s if isinstance(s, str) else s["name"] for s in payload["statuses"]]
    d = studio.delete_agent_payload(env, {"name": "dev"})
    assert d["ok"] is True
    assert not any(a["name"] == "dev" for a in studio.agents_payload(env)["agents"])


def test_generate_agent_payload_never_writes(tmp_path, monkeypatch):
    from harn import studio, agentgen
    from harn.config import Config
    env = tmp_path / ENV_DIRNAME
    (env / "agents").mkdir(parents=True)
    (env / "tasks").mkdir(parents=True)
    monkeypatch.setattr(agentgen, "generate",
                        lambda e, c, d: {"role": {"name": "x", "status": "todo"},
                                         "workflow": {"nodes": []}, "dropped": []})
    r = studio.generate_agent_payload(env, env.parent, Config(), {"description": "d"})
    assert r["ok"] is True and r["draft"]["role"]["name"] == "x"
    assert list((env / "agents").glob("*.md")) == []   # nothing persisted


def test_studio_html_has_agents_tab():
    from harn import studio
    assert "showTab('agents')" in studio._HTML
    assert "/api/agents/generate" in studio._HTML
    assert "/api/agents/save" in studio._HTML


def test_save_agent_workflow_guards_each_step_and_restores_active():
    # Regression: saveAgentWorkflow() used to fire its create/activate/save
    # round-trip with no failure checks, so a rejected create (e.g. name
    # slugifies to "default") silently fell through to activate()'s
    # fallback-to-default behavior and overwrote the real active preset.
    # It also had no try/finally, so a mid-call network error left the
    # backend switched to the agent's workflow. Assert the guards exist
    # without pinning exact wording.
    from harn import studio
    src = studio._HTML
    start = src.index("async function saveAgentWorkflow(")
    end = src.index("\nasync function deleteAgent(", start)
    fn = src[start:end]
    assert "rc.ok" in fn  # create failure aborts before activate/save
    assert "try{" in fn and "finally {" in fn  # wasActive always restored

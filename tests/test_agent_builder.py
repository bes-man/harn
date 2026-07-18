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

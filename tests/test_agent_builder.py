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

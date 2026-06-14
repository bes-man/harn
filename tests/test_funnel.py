"""Clarification funnel → locked spec → lazy PRD (token saving, no fidelity loss)."""
from __future__ import annotations

from pathlib import Path

from harn import scaffold, tasks, loop, ENV_DIRNAME
from harn.config import Config


def _proj(tmp_path, prd_body: str = "") -> Path:
    scaffold.setup(tmp_path)
    env = tmp_path / ENV_DIRNAME
    if prd_body:
        (env / "prd" / "auth.md").write_text(prd_body)
    return env


def test_lock_spec_sets_flag_and_criteria(tmp_path):
    env = _proj(tmp_path)
    t = tasks.create_task(env, "Add login", task_id="PRJ-001",
                          description="## What\nx\n## Done when\n- TBD")
    assert t.spec_locked is False
    tasks.lock_spec(t, "- POST /login returns JWT\n- 401 on bad creds",
                    approach="bcrypt + jose",
                    decisions=[("15m token TTL", "security")])
    t2 = tasks.find(env, "PRJ-001")
    assert t2.spec_locked is True
    assert "POST /login returns JWT" in t2.description
    assert "Approach (locked)" in t2.description
    assert any("15m token TTL" in d.decision for d in t2.decisions)


def test_spec_locked_round_trips(tmp_path):
    env = _proj(tmp_path)
    t = tasks.create_task(env, "x", task_id="P1")
    tasks.lock_spec(t, "- done")
    assert tasks.find(env, "P1").spec_locked is True


def test_unlocked_injects_full_prd(tmp_path):
    env = _proj(tmp_path, "# Auth\n## Problem\nSEKRET_MARKER need login\n## Goal\nJWT")
    t = tasks.create_task(env, "Add login", task_id="P1", prds=["auth"],
                          description="## What\nx\n## Done when\n- TBD")
    p = loop._build_prompt(env, Config.load(env), t)
    assert "SEKRET_MARKER" in p                  # full PRD present for planning
    assert "spec locked" not in p.lower()


def test_locked_injects_compact_prd_reference(tmp_path):
    body = ("# Auth\n## Problem\nSEKRET_MARKER\n"
            + "noise line\n" * 200 + "## Goal\nJWT\n")
    env = _proj(tmp_path, body)
    t = tasks.create_task(env, "Add login", task_id="P1", prds=["auth"],
                          description="## What\nx\n## Done when\n- TBD")
    p_unlocked = loop._build_prompt(env, Config.load(env), t)
    tasks.lock_spec(t, "- POST /login returns JWT")
    t2 = tasks.find(env, "P1")
    p_locked = loop._build_prompt(env, Config.load(env), t2)
    assert "read_prd" in p_locked                # on-demand pointer
    assert "spec locked" in p_locked.lower()
    assert "noise line" not in p_locked          # full PRD body NOT injected
    assert len(p_locked) < len(p_unlocked)       # leaner on a real PRD


def test_planning_instructions_describe_funnel():
    text = loop._planning_instructions(Config())
    assert "funnel" in text.lower()
    assert "lock_spec" in text
    assert "highest-leverage" in text.lower()


def test_planning_cap_proceeds_without_lock(tmp_path, monkeypatch):
    """A planner that never calls lock_spec must not loop forever — after the
    cap, harn proceeds to execution."""
    from harn.adapters.base import AgentResult
    from tests.test_planning import _env, _wire, ScriptedAdapter
    env = _env(tmp_path, planning=True)
    seen = []

    def never_lock(prompt, cwd):
        seen.append("PLANNING PHASE" in prompt)
        return AgentResult(ok=True, text="thinking...")

    fake = ScriptedAdapter([never_lock] * 8)
    _wire(monkeypatch, fake)
    loop.run(tmp_path, env, max_iterations=6)

    # At most _MAX_PLAN_TURNS planning turns, then execution (a non-planning turn)
    assert seen.count(True) <= loop._MAX_PLAN_TURNS
    assert False in seen  # execution turn happened


def test_autonomy_reaches_chat_mode_get_next_task(tmp_path, monkeypatch):
    """[harn] autonomy must govern ask-vs-decide in CHAT mode (get_next_task),
    not only headless. autonomy=0 → METICULOUS; autonomy=1 → DECISIVE."""
    import asyncio
    import harn.mcp_server as ms
    env = _proj(tmp_path)
    monkeypatch.setenv("HARN_ENV_DIR", str(env))
    toml = env / "harn.toml"

    def next_task_text():
        srv = ms.build_server()
        return str(asyncio.new_event_loop().run_until_complete(
            srv.call_tool("get_next_task", {})))

    tasks.create_task(env, "Add login", task_id="PRJ-001",
                      description="## What\nx\n## Done when\n- works")
    toml.write_text(toml.read_text().replace("autonomy = 0.7", "autonomy = 0.0"))
    assert "METICULOUS" in next_task_text()

    tasks.create_task(env, "Add logout", task_id="PRJ-002",
                      description="## What\nx\n## Done when\n- works")
    toml.write_text(toml.read_text().replace("autonomy = 0.0", "autonomy = 1.0"))
    assert "DECISIVE" in next_task_text()

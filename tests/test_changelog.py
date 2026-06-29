"""Per-task changelog: record_change, render_changelog, config gate, MCP tools."""
from __future__ import annotations

import asyncio
from pathlib import Path

from harn import tasks, scaffold, loop, ENV_DIRNAME
from harn.config import Config
from tests.conftest import make_task


# --- model + persistence --------------------------------------------------- #

def test_record_change_appends_and_persists(tmp_path):
    env = tmp_path / ENV_DIRNAME
    t = make_task(env, "PRJ-001")
    tasks.record_change(t, "Added rate limiting to /login",
                        "token bucket, 5 req/min")
    reloaded = tasks.find(env, "PRJ-001")
    assert len(reloaded.changelog) == 1
    c = reloaded.changelog[0]
    assert c.summary == "Added rate limiting to /login"
    assert c.detail == "token bucket, 5 req/min"
    assert c.ts  # timestamped


def test_record_change_ignores_blank(tmp_path):
    env = tmp_path / ENV_DIRNAME
    t = make_task(env, "PRJ-001")
    tasks.record_change(t, "   ")
    assert tasks.find(env, "PRJ-001").changelog == []


def test_changelog_round_trips_with_old_tasks(tmp_path):
    """A task file written before changelog existed loads fine (no field)."""
    env = tmp_path / ENV_DIRNAME
    t = make_task(env, "PRJ-001")  # conftest writes no changelog key
    assert t.changelog == []
    tasks.record_change(t, "first change")
    assert len(tasks.find(env, "PRJ-001").changelog) == 1


# --- doc rendering --------------------------------------------------------- #

def test_render_changelog_structures_changes_and_decisions(tmp_path):
    env = tmp_path / ENV_DIRNAME
    t = make_task(env, "PRJ-001", title="Auth")
    tasks.record_change(t, "Added JWT login", "15m access TTL")
    t = tasks.find(env, "PRJ-001")
    tasks.record_decision(t, "Use jose for signing", "audited, maintained")

    md = tasks.render_changelog(env)
    assert "# Changelog" in md
    assert "PRJ-001 — Auth" in md
    assert "Added JWT login" in md
    assert "15m access TTL" in md
    assert "### Decisions" in md
    assert "Use jose for signing" in md


def test_render_changelog_scoped_to_one_task(tmp_path):
    env = tmp_path / ENV_DIRNAME
    a = make_task(env, "PRJ-001", title="A")
    b = make_task(env, "PRJ-002", title="B")
    tasks.record_change(a, "change in A")
    tasks.record_change(b, "change in B")
    md = tasks.render_changelog(env, "PRJ-001")
    assert "change in A" in md
    assert "change in B" not in md


def test_render_changelog_empty(tmp_path):
    env = tmp_path / ENV_DIRNAME
    make_task(env, "PRJ-001")
    assert tasks.render_changelog(env) == "(no changes logged yet)"


# --- config gate ----------------------------------------------------------- #

def test_log_changes_default_on(tmp_path):
    (tmp_path / "harn.toml").write_text("[harn]\nagent = \"fake\"\n")
    assert Config.load(tmp_path).log_changes is True


def test_log_changes_toggle_off(tmp_path):
    (tmp_path / "harn.toml").write_text("[log]\nchanges = false\n")
    assert Config.load(tmp_path).log_changes is False


# --- reconcile backstop ---------------------------------------------------- #

def test_reconcile_prompt_mentions_record_change_when_on(tmp_path):
    scaffold.setup(tmp_path)
    env = tmp_path / ENV_DIRNAME
    t = make_task(env, "PRJ-001")
    cfg = Config.load(env)  # default on
    prompt = loop._build_reconcile_prompt(env, cfg, t)
    assert "record_change" in prompt


def test_reconcile_prompt_omits_record_change_when_off(tmp_path):
    scaffold.setup(tmp_path)
    env = tmp_path / ENV_DIRNAME
    (env / "harn.toml").write_text(
        '[harn]\nagent = "fake"\n[log]\nchanges = false\n')
    t = make_task(env, "PRJ-001")
    cfg = Config.load(env)
    prompt = loop._build_reconcile_prompt(env, cfg, t)
    assert "record_change" not in prompt


# --- MCP tools ------------------------------------------------------------- #

def _tool(env: Path, name: str, monkeypatch):
    monkeypatch.setenv("HARN_ENV_DIR", str(env))
    monkeypatch.setattr("harn.mcp_server._ensure_watch_running", lambda e: None)
    from harn import mcp_server as ms
    srv = ms.build_server()
    return next(t.fn for t in srv._tool_manager._tools.values() if t.name == name)


def test_mcp_record_change_logs(tmp_path, monkeypatch):
    scaffold.setup(tmp_path)
    env = tmp_path / ENV_DIRNAME
    make_task(env, "PRJ-001")
    fn = _tool(env, "record_change", monkeypatch)
    out = fn(task_id="PRJ-001", summary="Shipped login", detail="JWT")
    assert "Change logged" in out
    assert len(tasks.find(env, "PRJ-001").changelog) == 1


def test_mcp_record_change_respects_gate(tmp_path, monkeypatch):
    scaffold.setup(tmp_path)
    env = tmp_path / ENV_DIRNAME
    (env / "harn.toml").write_text(
        '[harn]\nagent = "fake"\n[log]\nchanges = false\n')
    make_task(env, "PRJ-001")
    fn = _tool(env, "record_change", monkeypatch)
    out = fn(task_id="PRJ-001", summary="Shipped login")
    assert "off" in out.lower()
    assert tasks.find(env, "PRJ-001").changelog == []


def test_auto_changelog_backstop_writes_when_empty(tmp_path):
    from harn import loop
    from harn.config import Config
    env = tmp_path / ENV_DIRNAME
    t = make_task(env, "PRJ-001", title="Add login")
    tasks.record_decision(t, "Use bcrypt", "standard")
    t = tasks.find(env, "PRJ-001")
    loop._auto_changelog(env, Config(log_changes=True), t, "Shipped login flow")
    fresh = tasks.find(env, "PRJ-001")
    assert len(fresh.changelog) == 1
    c = fresh.changelog[0]
    assert c.summary == "Shipped login flow"
    assert "bcrypt" in c.detail            # decisions folded into the entry
    assert c.agent == "harn-auto"


def test_auto_changelog_skips_when_agent_logged(tmp_path):
    from harn import loop
    from harn.config import Config
    env = tmp_path / ENV_DIRNAME
    t = make_task(env, "PRJ-001")
    tasks.record_change(t, "agent's own entry")
    t = tasks.find(env, "PRJ-001")
    loop._auto_changelog(env, Config(log_changes=True), t, "backstop")
    fresh = tasks.find(env, "PRJ-001")
    assert len(fresh.changelog) == 1       # no duplicate; agent's entry kept
    assert fresh.changelog[0].summary == "agent's own entry"


def test_auto_changelog_respects_gate(tmp_path):
    from harn import loop
    from harn.config import Config
    env = tmp_path / ENV_DIRNAME
    t = make_task(env, "PRJ-001")
    loop._auto_changelog(env, Config(log_changes=False), t, "x")
    assert tasks.find(env, "PRJ-001").changelog == []


def test_mcp_generate_changelog_writes_file(tmp_path, monkeypatch):
    scaffold.setup(tmp_path)
    env = tmp_path / ENV_DIRNAME
    t = make_task(env, "PRJ-001", title="Auth")
    tasks.record_change(t, "Added login")
    fn = _tool(env, "generate_changelog", monkeypatch)
    out = fn(task_id="", write=True)
    assert (env / "CHANGELOG.md").exists()
    assert "Added login" in (env / "CHANGELOG.md").read_text()
    assert "Added login" in out
